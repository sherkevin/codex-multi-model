/**
 * probe-mirror-model-boundary.mjs — 实测「镜像模式能不能换模型」这条边界。
 *
 * 为什么要单独存一份探针：ADR 0013 里写了两条结论，而这两条都不是看代码推出来的，
 * 是跑出来的，且**反直觉**（官方明明有 `thread/settings/update`，schema 里
 * `model` 字段的说明就是 "Override the model for subsequent turns"）：
 *
 *   1. 第二个连接 `thread/resume` 被锁线程 -> "already has an active writer"
 *      （这条是镜像模式存在的前提，见 ADR 0008）
 *   2. 第二个连接 `thread/settings/update` -> "thread not found"。
 *      原因：settings/update 要求线程已在**这条连接**里加载，而加载只能靠
 *      resume，resume 恰恰被 writer lock 挡住。所以官方那个改模型的 RPC
 *      在镜像模式下**够不着**。
 *   3. `thread/queue/add` 带 `model` 字段 -> 被静默忽略。证据：带 `model` 与带
 *      一个乱编字段 `bogusFieldXyz` 的响应**逐字节相同**（ThreadQueueAddParams
 *      的 schema 里也只有 threadId / clientUserMessageId / input 三个字段，
 *      `additionalProperties` 未禁止，于是多余键被丢掉而不报错）。
 *
 * 三条合起来就是：手机在镜像会话里换模型，**什么都不会发生**。垫片的
 * `warnIfModelChangeIgnored` 正是为了不让这件事静默发生。
 *
 * 安全：全程在临时 CODEX_HOME 里跑（拷贝生产 config.toml + custom-model-catalog.json，
 * 但 sessions/state 独立），绝不碰生产会话，也不抢任何 writer lock。
 *
 * 用法：node tools/happy-codex-shim/probe-mirror-model-boundary.mjs   （约 1 分钟）
 */
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const PROD_HOME = process.env.PROD_CODEX_HOME || path.join(os.homedir(), '.codex');
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'mirror-model-probe-'));
const HOME = path.join(TMP, 'home');
const CWD = path.join(TMP, 'work');
fs.mkdirSync(HOME, { recursive: true });
fs.mkdirSync(CWD, { recursive: true });
// 只拷配置类文件，不拷 sessions / state_5.sqlite（那是生产数据）
for (const f of ['config.toml', 'custom-model-catalog.json', 'router-routes.json', 'auth.json', 'installation_id']) {
  const src = path.join(PROD_HOME, f);
  if (fs.existsSync(src)) fs.copyFileSync(src, path.join(HOME, f));
}
const CODEX = process.env.CODEX_REAL || '/opt/homebrew/bin/codex';

function conn(label) {
  const child = spawn(CODEX, ['app-server', '--listen', 'stdio://'], {
    stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, CODEX_HOME: HOME }, windowsHide: true,
  });
  child.stderr.on('data', () => {});
  const pending = new Map();
  let nextId = 1;
  createInterface({ input: child.stdout }).on('line', line => {
    let m; try { m = JSON.parse(line); } catch { return; }
    if (m.id !== undefined && pending.has(m.id)) { const r = pending.get(m.id); pending.delete(m.id); r(m); }
  });
  return {
    req(method, params) {
      const id = nextId++;
      return new Promise((resolve, reject) => {
        const to = setTimeout(() => reject(new Error(`${method} timeout`)), 45000);
        pending.set(id, m => { clearTimeout(to); resolve(m); });
        child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
      });
    },
    kill() { child.kill('SIGKILL'); },
  };
}

const out = [];
function report(tag, value) { out.push({ tag, value }); console.log(`${tag}: ${value}`); }

const A = conn('A');
await A.req('initialize', { clientInfo: { name: 'probe-A', title: 'probe-A', version: '1.0' }, capabilities: { experimentalApi: true } });
const started = await A.req('thread/start', { cwd: CWD, model: null, effort: null, config: {} });
const TID = started.result?.thread?.id;
if (!TID) { console.error('thread/start failed:', JSON.stringify(started).slice(0, 400)); process.exit(2); }
report('A thread/start', `${TID} model=${started.result?.model}`);
report('A 持有 writer lock', fs.existsSync(path.join(HOME, 'thread-writer-locks', `${TID}.lock`)));

// 跑一个极小回合，让 rollout 落盘；不落盘的话别的连接连 "thread not found" 都到不了
const turn = await A.req('turn/start', { threadId: TID, clientUserMessageId: 'probe-turn', input: [{ type: 'text', text: 'Reply with exactly: OK' }], model: null, effort: null });
report('A turn/start', turn.error ? `error: ${turn.error.message}` : 'ok（rollout 已落盘）');

const B = conn('B');
await B.req('initialize', { clientInfo: { name: 'probe-B', title: 'probe-B', version: '1.0' }, capabilities: { experimentalApi: true } });

const r1 = await B.req('thread/resume', { threadId: TID, cwd: CWD, model: null, modelProvider: null, approvalPolicy: null, sandbox: null, config: {} });
report('[1] B thread/resume', r1.error ? `error ${r1.error.code}: ${r1.error.message}` : '成功（预期是失败！）');

const r2 = await B.req('thread/settings/update', { threadId: TID, model: 'MiniMax-M3', effort: 'high' });
report('[2] B thread/settings/update', r2.error ? `error ${r2.error.code}: ${r2.error.message}` : `成功 -> ${JSON.stringify(r2.result).slice(0, 200)}`);

const q1 = await B.req('thread/queue/add', { threadId: TID, clientUserMessageId: 'p1', input: [{ type: 'text', text: 'probe' }], model: 'MiniMax-M3' });
const q2 = await B.req('thread/queue/add', { threadId: TID, clientUserMessageId: 'p2', input: [{ type: 'text', text: 'probe' }], bogusFieldXyz: 'MiniMax-M3' });
const strip = m => JSON.stringify(m).replace(/"id":\d+,?/g, '').replace(/"[0-9a-f]{8}-[0-9a-f-]{27}"/g, '"<uuid>"').replace(/"clientUserMessageId":"[^"]*"/g, '');
report('[3] queue/add 带 model vs 带乱编字段是否同形', strip(q1) === strip(q2) ? '完全相同 -> model 被静默丢弃' : `不同!\n  A=${strip(q1)}\n  B=${strip(q2)}`);

fs.writeFileSync(path.join(TMP, 'result.json'), JSON.stringify({ tmp: TMP, tid: TID, out }, null, 1));
A.kill(); B.kill();
console.log(`\n临时 CODEX_HOME（可删）: ${TMP}`);
