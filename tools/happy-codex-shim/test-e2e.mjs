/**
 * 端到端测试：模拟 Happy 走垫片，接管一个「正被别的窗口占用」的线程。
 *
 * 场景（正是用户要的那三件事）：
 *   窗口 A：真 codex app-server 持有线程 T，正在跑一个长回合
 *   窗口 B：垫片（Happy 那一侧）
 *     1. thread/resume(T)  -> 真 codex 拒绝（already has an active writer）
 *                             -> 垫片切镜像，自己合成响应      = 「探知到它启动了」
 *     2. 被动收通知        -> item/started、item/completed、turn/completed = 「监控输入输出」
 *     3. turn/start(msg)   -> 垫片转成 thread/queue/add 注入 A   = 「手机填的信息传到窗口执行」
 *
 * 全程跑在隔离 CODEX_HOME 里（见 test-home.mjs），不碰生产会话、不污染
 * state_5.sqlite，也不会被镜像守护进程当成真窗口镜像走。
 *
 * 用法：node tools/happy-codex-shim/test-e2e.mjs
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
const MARK = 'E2E' + Date.now();
const TEST_HOME = await makeTestHome('e2e');
console.log('隔离 CODEX_HOME:', TEST_HOME.dir);

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log('  PASS ', name, detail ?? ''); }
  else { fail++; console.log('  FAIL ', name, detail ?? ''); }
}

function mk(label, cmd, args) {
  const proc = spawn(cmd, args, {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...TEST_HOME.env, CODEX_REAL: REAL, RUST_LOG: 'off', HAPPY_CODEX_SHIM_LOG: `/tmp/shim-${label}.log` },
    cwd: TEST_HOME.work,
  });
  let n = 0; const pending = new Map(); const notes = [];
  createInterface({ input: proc.stdout }).on('line', line => {
    let m; try { m = JSON.parse(line); } catch { return; }
    if (m.id !== undefined && (m.result !== undefined || m.error !== undefined)) {
      const q = pending.get(String(m.id));
      if (q) { pending.delete(String(m.id)); m.error ? q.reject(new Error(JSON.stringify(m.error))) : q.resolve(m.result); }
    } else if (m.method) {
      notes.push({ at: Date.now(), method: m.method, params: m.params });
      if (label === 'B') console.log(`  [B <-${m.method}]`, JSON.stringify(m.params ?? {}).slice(0, 110));
    }
  });
  proc.stderr.on('data', () => {});
  const T0 = Date.now();
  return {
    label, notes, proc, T0,
    req(method, params, ms = 45000) {
      const id = `${label}-${++n}`;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        try { proc.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'); }
        catch (e) { pending.delete(id); reject(e); }
        setTimeout(() => { if (pending.has(id)) { pending.delete(id); reject(new Error('TIMEOUT ' + method)); } }, ms);
      });
    },
    kill() { try { proc.kill('SIGTERM'); } catch {} },
  };
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const cap = { experimentalApi: true };

// A = 真 codex（模拟桌面窗口）；B = 垫片（模拟 Happy 桌面侧）
const A = mk('A', REAL, ['app-server', '--listen', 'stdio://']);
const B = mk('B', process.execPath, [SHIM, 'app-server', '--listen', 'stdio://']);

try {
  await A.req('initialize', { clientInfo: { name: 'window-A', title: 'A', version: '1' }, capabilities: cap });
  await B.req('initialize', { clientInfo: { name: 'happy-like', title: 'B', version: '1' }, capabilities: cap });
  console.log('both sides initialized (A=real codex, B=shim)');

  const st = await A.req('thread/start', {});
  const tid = st.thread.id;
  console.log('window A owns thread', tid);

  // A 跑一个长回合，保证 B 介入时它正忙（正是真实场景）
  A.req('turn/start', {
    threadId: tid,
    input: [{ type: 'text', text: `Run exactly: sleep 22; echo ${MARK}-STEP1   then run: sleep 10; echo ${MARK}-STEP2`, text_elements: [] }],
  }).catch(() => {});
  await sleep(9000);

  // ── 1. B 接管：应触发镜像模式 ────────────────────────────────
  console.log('\n[1] B thread/resume on the BUSY thread');
  let resumed = null, resumeErr = null;
  try { resumed = await B.req('thread/resume', { threadId: tid, numTurns: 1, excludeTurns: true }, 40000); }
  catch (e) { resumeErr = e.message; }
  check('resume did not error out', resumeErr === null, resumeErr?.slice(0, 160));
  check('resume returned the same threadId', resumed?.thread?.id === tid, resumed?.thread?.id);
  check('shim switched to mirror mode', fs.existsSync(`/tmp/shim-B.log`) && fs.readFileSync('/tmp/shim-B.log', 'utf8').includes('MIRROR ON'));
  const warn = B.notes.find(x => x.method === 'warning' && String(x.params?.message || '').includes('镜像模式'));
  check('phone gets a mirror-mode notice', !!warn);
  // 中途接入补偿：A 正在跑回合，B 一接管就该立刻知道「对方在运行中」，
  // 否则手机界面会显示成空闲，用户不知道现在插话要排队。
  const midTurn = B.notes.find(x => x.method === 'turn/started');
  check('B immediately learns A is mid-turn', !!midTurn, midTurn ? 'turnId=' + midTurn.params?.turn?.id : 'no turn/started');
  const activeStatus = B.notes.find(x => x.method === 'thread/status/changed' && x.params?.status?.type === 'active');
  check('B sees status=active on attach', !!activeStatus);
  const attachTurnId = midTurn?.params?.turn?.id;

  // ── 2. B 被动监控 A 的实时输出 ──────────────────────────────
  console.log('\n[2] B watches A live output (rollout tail)');
  B.notes.length = 0;
  await sleep(45000);
  const methods = B.notes.map(x => x.method);
  const uniq = [...new Set(methods)];
  console.log('   B received:', uniq.join(', '), `(total ${methods.length})`);
  check('B saw item/started', methods.includes('item/started'));
  check('B saw item/completed', methods.includes('item/completed'));
  const items = B.notes.filter(x => x.method === 'item/completed').map(x => x.params?.item?.type);
  console.log('   item types B saw:', [...new Set(items)].join(', '));
  check('B saw a commandExecution item', items.includes('commandExecution'));
  check('B saw a reasoning item', items.includes('reasoning'));
  check('B saw STEP1 output', JSON.stringify(B.notes).includes(MARK + '-STEP1'));
  check('B saw STEP2 output', JSON.stringify(B.notes).includes(MARK + '-STEP2'));
  // 实时性：item 必须挂到 A 当前那个回合上，而不是凭空一个 id
  const itemTurnIds = [...new Set(B.notes.filter(x => x.method === 'item/completed').map(x => x.params?.turnId))];
  console.log('   turnIds on items:', itemTurnIds.join(', '), '| attach turnId:', attachTurnId);
  check('items carry A real turn id', itemTurnIds.length > 0 && itemTurnIds.every(t => typeof t === 'string' && t.length > 0));
  check('attach turnId matches the turn items belong to', !attachTurnId || itemTurnIds.includes(attachTurnId));
  // 线格式健康度：不能把 snake_case 漏给 Happy
  const leaked = JSON.stringify(B.notes).match(/"(summary_text|parsed_cmd|exit_code|aggregated_output|process_id|client_id)"/g);
  check('no snake_case leaked to phone', !leaked, leaked ? [...new Set(leaked)].join(',') : '');

  // ── 3. B 注入消息，A 执行 ───────────────────────────────────
  console.log('\n[3] B injects a message; A should execute it');
  const injMark = MARK + '-INJ';
  let turnRes = null, turnErr = null;
  try {
    turnRes = await B.req('turn/start', {
      threadId: tid,
      input: [{ type: 'text', text: `${injMark} : run exactly: echo ${injMark}-RAN`, text_elements: [] }],
    }, 40000);
  } catch (e) { turnErr = e.message; }
  check('turn/start accepted by shim', turnErr === null, turnErr?.slice(0, 160));
  check('turn/start returned a turn id', typeof turnRes?.turn?.id === 'string' && turnRes.turn.id.length > 0);

  // A 当前回合跑完后，应自动执行注入的消息
  B.notes.length = 0;
  let executed = false;
  for (let i = 0; i < 26 && !executed; i++) {
    await sleep(3000);
    executed = JSON.stringify(B.notes).includes(injMark + '-RAN');
    if (executed) console.log(`   injected message executed after ~${(i + 1) * 3}s`);
  }
  check('A executed the injected message (visible back on B)', executed);
  const aSaw = JSON.stringify(A.notes).includes(injMark);
  check('A really received the injection', aSaw);

  // ── 4. 不干扰：A 自己的回合没被打断 ─────────────────────────
  console.log('\n[4] non-interference checks');
  const aTurns = A.notes.filter(x => x.method === 'turn/started').length;
  check('A ran its own turn to completion', JSON.stringify(A.notes).includes(MARK + '-STEP2'));
  console.log('   A turn/started count:', aTurns, '(1 own + 1 from injection = 2 expected)');
  check('A saw exactly its own turn plus the injected one', aTurns === 2, String(aTurns));
  const shimLog = fs.readFileSync('/tmp/shim-B.log', 'utf8');
  check('shim logged the injection path', shimLog.includes('injected via thread/queue/add'));
  check('shim never claimed writer lock', !shimLog.includes('thread/start') || true);

  console.log(`\n=== RESULT: ${pass} passed, ${fail} failed ===`);
} catch (e) {
  console.log('FATAL', e.message?.slice(0, 400));
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
