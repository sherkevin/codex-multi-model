/**
 * 「手机上能不能看到自定义模型」的回归测试。
 *
 * 为什么需要它：这个故障是**静默**的。手机端模型选择器渲染的是会话 metadata
 * 里的 `models` / `currentModelCode`；上游 codex 后端从不填这两个字段，于是
 * App 直接退回内置 GPT 清单，自定义模型一个都不显示——不报错、不告警，
 * 只是看上去「Happy 不支持」。同理，字段名写错（`code`/`value` 写成
 * `key`/`name`）也是一样的静默失败。所以两端都要钉住：
 *
 *   1. 我们**上报**的形状：直接从打过补丁的 happy bundle 里抠出真实的
 *      `syncCodexModelMetadata`，喂真实的 `model/list` 返回值跑一遍
 *      （不是手抄一份逻辑，手抄的那份永远是对的，没意义）。
 *   2. 手机**解析**的形状：用从线上 App bundle 逐字抄来的
 *      `getAvailableModels` / `resolveCurrentOption`
 *      （fixtures/webapp-model-picker.mjs）去解析上一步的产物。
 *   3. 镜像模式下的诚实性：垫片的 `warnIfModelChangeIgnored` 只在
 *      「用户真的换了模型」时提醒，不能每条消息都吵。
 *
 * 用法：
 *   node tools/happy-codex-shim/test-model-meta.mjs            # 用 fixture，秒级
 *   node tools/happy-codex-shim/test-model-meta.mjs --live      # 真调一次 model/list
 *   node tools/happy-codex-shim/test-model-meta.mjs --verbose
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';

import { getAvailableModels, resolveCurrentOption } from './fixtures/webapp-model-picker.mjs';

const VERBOSE = process.argv.includes('--verbose');
const LIVE = process.argv.includes('--live');
const HERE = path.dirname(new URL(import.meta.url).pathname);

let pass = 0, fail = 0;
const problems = [];
function check(name, cond, detail) {
  if (cond) { pass++; if (VERBOSE) console.log('  ok  ', name); }
  else { fail++; problems.push(`${name}${detail ? ' :: ' + detail : ''}`); console.log('  FAIL', name, detail ?? ''); }
}

/** 从源码里按花括号配平抠出一个完整函数（含 `async` 前缀）。 */
function extractFunction(src, marker) {
  const i = src.indexOf(marker);
  if (i < 0) return null;
  const j = src.indexOf('{', i);
  let depth = 0;
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') { depth--; if (depth === 0) return src.slice(i, k + 1); }
  }
  return null;
}

// ── 1. 真 codex 的 model/list 长什么样 ────────────────────────────
async function liveModelList() {
  const real = process.env.CODEX_REAL || '/opt/homebrew/bin/codex';
  const child = spawn(real, ['app-server', '--listen', 'stdio://'], { stdio: ['pipe', 'pipe', 'pipe'] });
  return new Promise((resolve, reject) => {
    const to = setTimeout(() => { child.kill(); reject(new Error('model/list timeout')); }, 25000);
    createInterface({ input: child.stdout }).on('line', line => {
      let m; try { m = JSON.parse(line); } catch { return; }
      if (m.id === 1) {
        child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'model/list', params: {} }) + '\n');
      } else if (m.id === 2) {
        clearTimeout(to); child.kill();
        resolve(m.result?.data ?? []);
      }
    });
    child.stdin.write(JSON.stringify({
      jsonrpc: '2.0', id: 1, method: 'initialize',
      params: { clientInfo: { name: 'model-meta-test', title: 'model-meta-test', version: '1.0' }, capabilities: { experimentalApi: true } },
    }) + '\n');
    child.on('error', e => { clearTimeout(to); reject(e); });
  });
}

// ── 2. 把补丁里真实的 syncCodexModelMetadata 抠出来跑 ──────────────
function findBundles() {
  const roots = [];
  const expand = p => (p.startsWith('~/') ? path.join(os.homedir(), p.slice(2)) : p);
  if (process.env.HAPPY_DIR) roots.push(expand(process.env.HAPPY_DIR));
  roots.push('/opt/homebrew/lib/node_modules/happy', '/usr/local/lib/node_modules/happy',
             path.join(os.homedir(), '.npm-global/lib/node_modules/happy'));
  const out = [];
  for (const root of roots) {
    const dist = path.join(root, 'dist');
    if (!fs.existsSync(dist)) continue;
    for (const f of fs.readdirSync(dist).sort()) {
      // happy 的 bundle 同时有 .mjs 和 .cjs 两份，补丁工具两份都打，
      // 这里也必须两份都验（cjs 的 logger 引用形态不一样，只测一份会漏）。
      if (!/^index-.*\.(mjs|cjs|js)$/.test(f) || f.endsWith('.orig-bak')) continue;
      const p = path.join(dist, f);
      let body;
      try { body = fs.readFileSync(p, 'utf8'); } catch { continue; }
      if (body.includes('async function syncCodexModelMetadata(opts) {')) out.push(p);
    }
  }
  return out;
}

/**
 * 在沙箱里执行 bundle 里抠出来的真函数，返回它写进 metadata 的内容。
 * 用 new Function 而不是 import bundle：bundle 是 9MB 的打包产物，
 * import 它会真的去连服务器。
 */
async function runPatchedFnWith(bundlePath, client, currentModel) {
  const src = fs.readFileSync(bundlePath, 'utf8');
  const fnSrc = extractFunction(src, 'async function syncCodexModelMetadata(opts) {');
  if (!fnSrc) throw new Error(`cannot extract syncCodexModelMetadata from ${bundlePath}`);
  // bundle 里 logger 是模块级绑定：mjs 是裸 `logger`，cjs 是 `api.logger`。
  const adapted = fnSrc.replace(/api\.logger\.debug\(/g, 'logger.debug(');
  const logs = [];
  const fn = new Function('logger', `${adapted}\nreturn syncCodexModelMetadata;`)({
    debug: (...a) => logs.push(a.map(String).join(' ')),
  });
  let metadata = { flavor: 'codex' };
  let threw = null;
  try {
    await fn({ client, session: { updateMetadata: m => { metadata = m(metadata); } }, currentModel });
  } catch (e) { threw = e; }
  return { metadata, logs, threw };
}

function runPatchedFn(bundlePath, modelListRows, currentModel) {
  return runPatchedFnWith(bundlePath, {
    request: async method => {
      if (method !== 'model/list') throw new Error(`unexpected request ${method}`);
      return { data: modelListRows };
    },
  }, currentModel);
}

// ── 3. 跑起来 ────────────────────────────────────────────────────
const fixture = JSON.parse(fs.readFileSync(path.join(HERE, 'fixtures/model-list.json'), 'utf8'));
let rows = fixture.result.data;
console.log(`model/list 数据源: ${LIVE ? '真 codex（--live）' : 'fixture ' + fixture.capturedAt}`);
if (LIVE) {
  rows = await liveModelList();
  console.log(`  真 codex 返回 ${rows.length} 个模型`);
}

const bundles = findBundles();
console.log(`打过补丁的 happy bundle: ${bundles.length} 份`);
check('至少找到一份已打补丁的 bundle', bundles.length > 0, '先跑 python3 tools/happy-patch.py');

// 对照组：证明「不打补丁就真的坏」，否则上面一片 PASS 没有意义。
// 两个独立事实：(a) 上游 bundle 里没有这个函数；(b) metadata 缺 models 时，
// App 的解析逻辑确实退回内置清单。
for (const b of bundles) {
  const bak = b + '.orig-bak';
  if (fs.existsSync(bak)) {
    const up = fs.readFileSync(bak, 'utf8');
    check(`${path.basename(bak)} 上游版本里没有 syncCodexModelMetadata`,
          !up.includes('syncCodexModelMetadata'));
    check(`${path.basename(bak)} 上游版本从不填 metadata.models`,
          !/models:\s*models/.test(up));
  }
}
check('metadata 缺 models 时手机回落到硬编码清单（对照组）',
      getAvailableModels('codex', { flavor: 'codex' }) === 'FALLBACK-to-hardcoded-list',
      String(getAvailableModels('codex', { flavor: 'codex' })).slice(0, 80));

for (const b of bundles) {
  const tag = path.basename(b);
  // 补丁内部会把清单缓存到 globalThis，跨 bundle 会串味，每份都先清一次。
  delete globalThis.__codexModelCatalog;
  // 建连后那次调用：还没有线程，所以不带 currentModelCode
  const fresh = await runPatchedFn(b, rows, undefined);
  check(`${tag} 上报了模型清单`, Array.isArray(fresh.metadata.models) && fresh.metadata.models.length > 0,
        JSON.stringify(Object.keys(fresh.metadata)));
  check(`${tag} 未知当前模型时不瞎写 currentModelCode`, fresh.metadata.currentModelCode === undefined,
        String(fresh.metadata.currentModelCode));

  // resume / 新开会话后那次调用：带上真实模型
  const cur = rows.find(r => !r.hidden)?.id ?? rows[0]?.id;
  const resumed = await runPatchedFn(b, rows, cur);
  check(`${tag} 当前模型角标如实上报`, resumed.metadata.currentModelCode === cur,
        `${resumed.metadata.currentModelCode} != ${cur}`);

  // ── 手机端解析这份 metadata ─────────────────────────────
  const opts = getAvailableModels('codex', resumed.metadata);
  check(`${tag} 手机不再回落到硬编码清单`, Array.isArray(opts), String(opts).slice(0, 80));
  if (!Array.isArray(opts)) continue;
  const codes = rows.filter(r => !r.hidden).map(r => r.id);
  const missing = codes.filter(c => !opts.some(o => o.key === c));
  check(`${tag} 清单里每个非隐藏模型在手机可见`, missing.length === 0, '缺: ' + missing.join(', '));
  const custom = codes.filter(c => !c.startsWith('gpt-') && !c.startsWith('codex-'));
  check(`${tag} 自定义模型确实出现在选择器里`, custom.length > 0 && custom.every(c => opts.some(o => o.key === c)),
        'custom=' + custom.join(','));
  check(`${tag} codex 会自动补一个 default 项`, opts[0].key === 'default', opts[0]?.key);
  const shown = opts.find(o => o.key === cur);
  check(`${tag} 显示名取 displayName 而非 code`, !!shown && shown.name !== shown.key && !!shown.name,
        shown ? `${shown.name} vs ${shown.key}` : 'not found');
  const badge = resolveCurrentOption(opts, [undefined, resumed.metadata.currentModelCode]);
  check(`${tag} 当前模型角标能被手机解析出来`, badge?.key === cur, JSON.stringify(badge));

  // ── 隐藏模型不该泄漏到手机（codex-auto-review 是内部审查模型）──
  const hidden = rows.filter(r => r.hidden).map(r => r.id);
  if (hidden.length) {
    const leaked = hidden.filter(h => opts.some(o => o.key === h));
    check(`${tag} 隐藏模型未泄漏`, leaked.length === 0, 'leaked: ' + leaked.join(','));
  }

  // ── model/list 失败时必须静默降级，绝不能让会话起不来 ──────
  delete globalThis.__codexModelCatalog; // 清掉上一轮的缓存，否则失败路径根本走不到
  const degraded = await runPatchedFnWith(b, { request: async () => { throw new Error('app-server said no'); } }, cur);
  check(`${tag} model/list 失败时静默降级`, degraded.metadata.models === undefined, JSON.stringify(degraded.metadata));
  check(`${tag} 失败时不会抛出`, degraded.threw === null, String(degraded.threw));
  check(`${tag} 失败时不会瞎写当前模型`, degraded.metadata.currentModelCode === undefined,
        String(degraded.metadata.currentModelCode));
  delete globalThis.__codexModelCatalog;
}

// ── 4. 垫片：镜像模式换不动模型，但只在真的换了时才说 ──────────────
console.log('\n垫片 warnIfModelChangeIgnored（镜像模式）:');
const shimSrc = fs.readFileSync(path.join(HERE, 'codex'), 'utf8');
const warnSrc = extractFunction(shimSrc, 'async function warnIfModelChangeIgnored(threadId, params) {');
check('垫片里有 warnIfModelChangeIgnored', !!warnSrc);
if (warnSrc) {
  const REAL = { model: 'qwen3.8-max', reasoningEffort: 'xhigh' };
  const cases = [
    ['happy 默认带 model（最常见，不该吵）', { model: 'qwen3.8-max', effort: 'xhigh' }, REAL, false],
    ['手机换成别的模型（该报）',              { model: 'gpt-5.5', effort: 'xhigh' },     REAL, true],
    ['手机只改推理档位（该报）',              { model: 'qwen3.8-max', effort: 'high' },  REAL, true],
    ['模型和档位都改（该报）',                { model: 'MiniMax-M3', effort: 'low' },    REAL, true],
    ['完全没带模型字段（不该报）',            {},                                        REAL, false],
    ['查不到线程真实值（不该瞎报）',          { model: 'gpt-5.5', effort: 'xhigh' },     null,  false],
  ];
  for (const [name, params, real, shouldWarn] of cases) {
    const notes = [];
    const fn = new Function('readThreadModel', 'note', 'log',
      `${warnSrc}\nreturn warnIfModelChangeIgnored;`)(
        async () => real,
        (method, p) => notes.push({ method, ...p }),
        () => {},
    );
    await fn('thread-x', params);
    const warned = notes.some(n => n.method === 'warning');
    check(name, warned === shouldWarn, `expected warn=${shouldWarn} got=${warned}`);
    if (warned && VERBOSE) console.log('        ', notes[0].message);
  }
}

// ── 5. 垫片 resume 必须报真实模型，且不能从 thread/read 取 ─────────
const resumeSrc = extractFunction(shimSrc, 'async function mirrorResume(id, params) {');
check('垫片 mirrorResume 走了 sqlite 读真实模型',
      !!resumeSrc && resumeSrc.includes('readThreadModel(threadId)'),
      resumeSrc ? 'no readThreadModel call' : 'function missing');
check('垫片 readThreadModel 用只读连接',
      shimSrc.includes("state_5.sqlite')}?mode=ro"), 'expected read-only URI');
check('垫片用 .timeout 而不是 PRAGMA busy_timeout（后者会把 5000 当结果回显）',
      shimSrc.includes("'.timeout 5000'") || shimSrc.includes('"-cmd", \'.timeout 5000\'') || /-cmd[^\n]*\.timeout 5000/.test(shimSrc),
      'sqlite3 invocation changed; re-verify parsing');

console.log(`\nchecks: ${pass} passed, ${fail} failed`);
if (fail) {
  console.log('\nFAILURES:');
  for (const p of [...new Set(problems)].slice(0, 25)) console.log('  -', p);
  process.exit(1);
}
console.log('\nALL PASS');
