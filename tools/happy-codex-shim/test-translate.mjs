/**
 * 翻译器回归测试：用真实 rollout 文件喂 toWireItem，校验输出符合
 * app-server 线格式（字段名 camelCase、required 齐全、类型正确）。
 *
 * 之所以要这个测试：rollout 是 snake_case，线格式是 camelCase，
 * 两者字段还不是一一对应（command 数组 vs 字符串、duration{secs,nanos}
 * vs durationMs、changes 对象 vs 数组）。这些映射错了手机端就白屏，
 * 而 Happy 不会报错，只会静默丢事件 —— 只能靠断言守住。
 *
 * 用法：node tools/happy-codex-shim/test-translate.mjs [--verbose]
 */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const VERBOSE = process.argv.includes('--verbose');
const CODEX_HOME = process.env.CODEX_HOME || path.join(os.homedir(), '.codex');

// ── 把 shim 里的翻译函数单独抠出来跑（shim 是 CLI，不能直接 import 执行）──
const shimSrc = fs.readFileSync(path.join(path.dirname(new URL(import.meta.url).pathname), 'codex'), 'utf8');
const start = shimSrc.indexOf('const CAMEL =');
const end = shimSrc.indexOf('// ═', shimSrc.indexOf('function toWireItem'));
if (start < 0 || end < 0) { console.error('FAIL: cannot locate translator block in shim'); process.exit(2); }
const translator = await import('data:text/javascript,' + encodeURIComponent(
  shimSrc.slice(start, end) + '\nexport { toWireItem, plainCwd, commandString, durationMs, textOf };\n'
));
const { toWireItem } = translator;

// ── 线格式要求（来自 codex app-server generate-json-schema）──
const REQUIRED = {
  userMessage: ['content', 'id', 'type'],
  agentMessage: ['id', 'text', 'type'],
  reasoning: ['id', 'type'],
  commandExecution: ['command', 'commandActions', 'cwd', 'id', 'status', 'type'],
  fileChange: ['changes', 'id', 'status', 'type'],
  mcpToolCall: ['arguments', 'id', 'server', 'status', 'tool', 'type'],
};

let pass = 0, fail = 0;
const problems = [];
function check(name, cond, detail) {
  if (cond) { pass++; if (VERBOSE) console.log('  ok  ', name); }
  else { fail++; problems.push(`${name}${detail ? ' :: ' + detail : ''}`); console.log('  FAIL', name, detail ?? ''); }
}

function scanRollouts(limit = 40) {
  const root = path.join(CODEX_HOME, 'sessions');
  const out = [];
  const walk = d => {
    let ents; try { ents = fs.readdirSync(d, { withFileTypes: true }); } catch { return; }
    for (const e of ents) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name.startsWith('rollout-') && e.name.endsWith('.jsonl')) out.push(p);
    }
  };
  walk(root);
  out.sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
  return out.slice(0, limit);
}

const files = scanRollouts();
console.log(`scanning ${files.length} recent rollout files…`);

const seenTypes = new Set();
let itemCount = 0;

for (const f of files) {
  let txt;
  try { txt = fs.readFileSync(f, 'utf8'); } catch { continue; }
  for (const line of txt.split('\n')) {
    if (!line.trim()) continue;
    let rec; try { rec = JSON.parse(line); } catch { continue; }
    const p = rec?.payload;
    if (rec?.type !== 'event_msg' || p?.type !== 'item_completed' || !p.item) continue;

    const raw = p.item;
    const wire = toWireItem(raw);
    itemCount++;

    // 允许返回 null（ContextCompaction 等手机端不渲染的类型）
    if (wire === null) { seenTypes.add(raw.type + '(skipped)'); continue; }
    seenTypes.add(raw.type + '->' + wire.type);

    const tag = `${path.basename(f)}:${raw.type}#${String(raw.id).slice(0, 8)}`;
    const req = REQUIRED[wire.type];
    if (!req) { check(tag + ' known wire type', false, 'unexpected type ' + wire.type); continue; }
    for (const k of req) {
      check(`${tag} has required .${k}`, wire[k] !== undefined, 'missing');
    }
    // 类型校验
    if (wire.type === 'commandExecution') {
      check(tag + ' command is string', typeof wire.command === 'string', typeof wire.command);
      check(tag + ' command non-empty', wire.command.length > 0);
      check(tag + ' commandActions is array', Array.isArray(wire.commandActions));
      check(tag + ' cwd has no file:// ', typeof wire.cwd === 'string' && !wire.cwd.startsWith('file://'), String(wire.cwd).slice(0, 60));
      check(tag + ' exitCode int|null', wire.exitCode === null || Number.isInteger(wire.exitCode), String(wire.exitCode));
      check(tag + ' durationMs int|null', wire.durationMs === null || Number.isInteger(wire.durationMs), String(wire.durationMs));
      check(tag + ' status valid', ['inProgress', 'completed', 'failed', 'declined'].includes(wire.status), String(wire.status));
      check(tag + ' source camelCase', /^[a-z][a-zA-Z]*$/.test(wire.source ?? ''), String(wire.source));
    }
    if (wire.type === 'agentMessage') {
      check(tag + ' text is string', typeof wire.text === 'string');
    }
    if (wire.type === 'userMessage') {
      check(tag + ' content is array', Array.isArray(wire.content));
      check(tag + ' content[0].text string', typeof wire.content?.[0]?.text === 'string' || wire.content.length === 0);
    }
    if (wire.type === 'reasoning') {
      check(tag + ' summary is array', Array.isArray(wire.summary));
      check(tag + ' summary all strings', wire.summary.every(x => typeof x === 'string'));
    }
    if (wire.type === 'fileChange') {
      check(tag + ' changes is array', Array.isArray(wire.changes));
      for (const c of wire.changes) {
        check(tag + ' change has path/kind/diff', typeof c.path === 'string' && c.kind && typeof c.kind.type === 'string' && typeof c.diff === 'string');
      }
    }
    // 绝不能出现 snake_case 键
    const snake = Object.keys(wire).filter(k => k.includes('_'));
    check(tag + ' no snake_case keys', snake.length === 0, snake.join(','));
    // 必须能 JSON 序列化
    try { JSON.parse(JSON.stringify(wire)); check(tag + ' JSON round-trip', true); }
    catch (e) { check(tag + ' JSON round-trip', false, e.message); }
  }
}

console.log('\nitem types exercised:');
for (const t of [...seenTypes].sort()) console.log('  ', t);
console.log(`\nitems translated: ${itemCount}`);
console.log(`checks: ${pass} passed, ${fail} failed`);
if (fail) {
  console.log('\nFAILURES:');
  for (const p of [...new Set(problems)].slice(0, 25)) console.log('  -', p);
  process.exit(1);
}
console.log('\nALL PASS');
