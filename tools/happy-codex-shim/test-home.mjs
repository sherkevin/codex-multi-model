/**
 * 给测试建一个隔离的 CODEX_HOME。
 *
 * 为什么必须有：测试会真的 `thread/start` 并跑回合，如果沿用生产的
 * `~/.codex`，就会产生三类污染——
 *   1. 生产 `state_5.sqlite` 里多出测试线程（在手机/CLI 的会话列表里能看到）；
   2. `sessions/` 下多出测试 rollout；
 *   3. 测试线程会短暂持有 writer lock，而镜像守护进程**只镜像持锁线程**，
 *      于是守护会把测试线程也镜像上去（实测踩到，要 bootout 守护才能跑测试）。
 *
 * 隔离之后第 3 条自动消失：守护读的是生产 CODEX_HOME，看不见测试线程。
 *
 * 只拷配置类文件（config.toml / custom-model-catalog.json / router-routes.json /
 * auth.json / installation_id），**不拷** sessions 与 state_*.sqlite——那是生产数据。
 *
 * 用法：
 *   import { makeTestHome } from './test-home.mjs';
 *   const home = await makeTestHome();          // home.dir / home.env / home.cleanup()
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const CONFIG_FILES = [
  'config.toml',
  'custom-model-catalog.json',
  'router-routes.json',
  'auth.json',
  'installation_id',
];

export async function makeTestHome(label = 'codex-test') {
  const prod = process.env.PROD_CODEX_HOME || path.join(os.homedir(), '.codex');
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), `${label}-`));
  const dir = path.join(tmp, 'home');
  const work = path.join(tmp, 'work');
  fs.mkdirSync(dir, { recursive: true });
  fs.mkdirSync(work, { recursive: true });
  for (const f of CONFIG_FILES) {
    const src = path.join(prod, f);
    if (fs.existsSync(src)) fs.copyFileSync(src, path.join(dir, f));
  }
  return {
    dir,
    work,
    env: { ...process.env, CODEX_HOME: dir },
    cleanup() { fs.rmSync(tmp, { recursive: true, force: true }); },
  };
}
