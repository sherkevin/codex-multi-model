/**
 * 用 Happy 的真实调用序列压垫片。
 *
 * 为什么需要：test-e2e 用的是我们自己写的客户端，可能只调了「我们以为 Happy 会调」
 * 的方法。这个测试照着 happy 1.2.3 bundle 里 CodexAppServerClient 的实际代码复现
 * 完整序列（含 resume-backfill 补丁会触发的 thread/read includeTurns:true），
 * 确保垫片不会在任何一步把 Happy 卡住或喂错格式。
 *
 * 实测 Happy 侧只调这些 RPC：
 *   initialize, thread/start, thread/resume, thread/fork, thread/read,
 *   thread/rollback, thread/goal/set, thread/goal/clear, turn/start, turn/interrupt
 * 只认这些通知：
 *   thread/started, turn/started, turn/completed, thread/status/changed,
 *   thread/tokenUsage/updated, thread/goal/*, item/*, codex/event/*
 *
 * 全程跑在隔离 CODEX_HOME 里（见 test-home.mjs）：不往生产 state_5.sqlite 塞测试
 * 线程，也不会被镜像守护进程当成真窗口镜像走。这一条对「垫片从 sqlite 读真实
 * 模型」的覆盖同样重要——测试 home 里的线程记录就是垫片会读到的那份。
 *
 * 用法：node tools/happy-codex-shim/test-happy-sequence.mjs
 */
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { makeTestHome } from './test-home.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SHIM = path.join(HERE, 'codex');
const REAL = process.env.CODEX_REAL || '/opt/homebrew/bin/codex';
const MARK = 'HSEQ' + Date.now();
const TEST_HOME = await makeTestHome('hseq');
console.log('隔离 CODEX_HOME:', TEST_HOME.dir);

let pass = 0, fail = 0;
const check = (name, cond, detail) => {
  if (cond) { pass++; console.log('  PASS ', name, detail ?? ''); }
  else { fail++; console.log('  FAIL ', name, detail ?? ''); }
};
const sleep = ms => new Promise(r => setTimeout(r, ms));

function mk(label, cmd, args) {
  const proc = spawn(cmd, args, {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...TEST_HOME.env, CODEX_REAL: REAL, RUST_LOG: 'off' },
    cwd: TEST_HOME.work,
  });
  let n = 0; const pending = new Map(); const notes = [];
  createInterface({ input: proc.stdout }).on('line', line => {
    let m; try { m = JSON.parse(line); } catch { return; }
    if (m.id !== undefined && (m.result !== undefined || m.error !== undefined)) {
      const q = pending.get(String(m.id));
      if (q) { pending.delete(String(m.id)); m.error ? q.reject(new Error(m.error.message || JSON.stringify(m.error))) : q.resolve(m.result); }
    } else if (m.method) notes.push({ method: m.method, params: m.params });
  });
  proc.stderr.on('data', () => {});
  return {
    label, notes, proc,
    req(method, params, ms = 45000) {
      const id = ++n;
      return new Promise((resolve, reject) => {
        pending.set(String(id), { resolve, reject });
        try { proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'); }
        catch (e) { pending.delete(String(id)); reject(e); }
        setTimeout(() => { if (pending.has(String(id))) { pending.delete(String(id)); reject(new Error('TIMEOUT ' + method)); } }, ms);
      });
    },
    notify(method, params) { try { proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params }) + '\n'); } catch {} },
    kill() { try { proc.kill('SIGTERM'); } catch {} },
  };
}

const A = mk('A', REAL, ['app-server', '--listen', 'stdio://']);   // 桌面窗口
const B = mk('B', process.execPath, [SHIM, 'app-server', '--listen', 'stdio://']); // Happy 侧（垫片）

try {
  // ── Happy 启动时的确切握手 ────────────────────────────────────
  const init = await B.req('initialize', {
    clientInfo: { name: 'happy-codex', title: 'Happy Codex Client', version: '1.2.3' },
    capabilities: { experimentalApi: true },
  });
  check('initialize via shim', !!init?.userAgent, String(init?.userAgent).slice(0, 60));
  B.notify('initialized', {});

  await A.req('initialize', { clientInfo: { name: 'desktop-window', title: 'D', version: '1' }, capabilities: { experimentalApi: true } });
  const st = await A.req('thread/start', {});
  const tid = st.thread.id;
  console.log('  desktop window thread:', tid);

  // 桌面窗口开跑长回合
  A.req('turn/start', { threadId: tid, input: [{ type: 'text', text: `Run exactly: sleep 20; echo ${MARK}-X`, text_elements: [] }] }).catch(() => {});
  await sleep(9000);

  // ── Happy 的 --resume 路径（含 resume-backfill 补丁）──────────
  console.log('\n[resume path] Happy calls thread/resume then thread/read(includeTurns)');
  const resumed = await B.req('thread/resume', {
    threadId: tid, model: null, modelProvider: null, cwd: process.cwd(),
    approvalPolicy: null, sandbox: null, config: {}, baseInstructions: null,
    developerInstructions: null, persistExtendedHistory: true,
  });
  check('thread/resume succeeded through mirror', resumed?.thread?.id === tid, resumed?.thread?.id);

  // backfill 补丁紧接着调这个，必须能读到历史（走真 codex，不是垫片合成）
  const read = await B.req('thread/read', { threadId: tid, includeTurns: true });
  check('thread/read works while mirrored', !!read?.thread, read ? 'ok' : 'null');
  const histItems = (read?.thread?.turns || []).flatMap(t => t.items || []);
  console.log('   backfill would replay', histItems.length, 'items');
  check('backfill has items to replay', histItems.length > 0);
  // Happy 的 buildCodexThreadBackfillEnvelopes 吃的是 wire item，键必须 camelCase
  const badKeys = histItems.flatMap(it => Object.keys(it)).filter(k => k.includes('_'));
  check('backfill items are camelCase (Happy-parseable)', badKeys.length === 0, [...new Set(badKeys)].join(','));

  // ── Happy 发消息（sendTurn 的确切参数形状）────────────────────
  console.log('\n[send] Happy calls turn/start with its exact param shape');
  const turn = await B.req('turn/start', { threadId: tid, input: [{ type: 'text', text: `${MARK}-FROMPHONE : run exactly: echo ${MARK}-PHONE-RAN` }] });
  check('turn/start accepted', !!turn?.turn?.id, turn?.turn?.id);

  // Happy 之后等 turn/completed 或 status=idle 来结束「发送中」状态。
  // 注意：镜像模式会把 A 自己那轮的完成也转发过来，所以 settled 可能先于
  // 「注入的消息被消费」为真 —— 两件事要分开断言，不能拿 settled 当注入成功的证据。
  let settled = false;
  for (let i = 0; i < 30 && !settled; i++) {
    await sleep(2500);
    settled = B.notes.some(x =>
      (x.method === 'turn/completed' && x.params?.turn?.id) ||
      (x.method === 'thread/status/changed' && x.params?.status?.type === 'idle'));
    if (settled) console.log(`   turn settled after ~${((i + 1) * 2.5).toFixed(1)}s`);
  }
  check('Happy sees the turn settle (no 10-min hang)', settled);

  // 注入的消息由 A 在当前回合结束后消费，所以单独轮询等待（最长 ~75s）
  let delivered = false;
  for (let i = 0; i < 25 && !delivered; i++) {
    delivered = JSON.stringify(A.notes).includes(MARK + '-FROMPHONE');
    if (!delivered) await sleep(3000);
    else console.log(`   injection consumed by desktop window after ~${(i + 1) * 3}s of extra wait`);
  }
  check('phone message reached the desktop window', delivered);
  // 手机侧也应看到回音（镜像转发 A 执行注入消息的那一轮）
  const echoed = JSON.stringify(B.notes).includes(MARK + '-PHONE-RAN');
  check('phone sees its own message executed', echoed);

  // ── 通知白名单：垫片不能发明 Happy 不认识的方法 ────────────────
  console.log('\n[protocol hygiene]');
  const KNOWN = new Set(['thread/started', 'turn/started', 'turn/completed', 'thread/status/changed',
    'thread/tokenUsage/updated', 'thread/goal/updated', 'thread/goal/cleared',
    'remoteControl/status/changed', 'warning', 'mcpServer/startupStatus/updated', 'deprecationNotice']);
  const methods = new Set(B.notes.map(x => x.method));
  const unknown = [...methods].filter(m => !KNOWN.has(m) && !m.startsWith('item/') && !m.startsWith('codex/event'));
  console.log('   methods shim emitted:', [...methods].join(', '));
  check('no unknown notification methods', unknown.length === 0, unknown.join(','));

  // ── goal 相关（Happy 也调，不能因为镜像模式炸掉）──────────────
  console.log('\n[goal calls]');
  for (const [m, p] of [['thread/goal/get', { threadId: tid }], ['thread/goal/clear', { threadId: tid }]]) {
    try { await B.req(m, p, 15000); check(`${m} tolerated`, true); }
    catch (e) { check(`${m} tolerated`, false, e.message.slice(0, 100)); }
  }

  console.log(`\n=== RESULT: ${pass} passed, ${fail} failed ===`);
} catch (e) {
  console.log('FATAL', String(e.message).slice(0, 400));
  fail++;
} finally {
  A.kill(); B.kill();
  setTimeout(() => {
    console.log(`\nFINAL: ${pass} passed, ${fail} failed`);
    if (process.env.KEEP_TEST_HOME) console.log('测试 CODEX_HOME 保留在', TEST_HOME.dir);
    else TEST_HOME.cleanup();
    process.exit(fail ? 1 : 0);
  }, 800);
}
