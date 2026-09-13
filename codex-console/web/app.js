"use strict";
/* codex-console 前端：纯配置器。所有读取实时、不缓存（桌面端会回写 config.toml）。 */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function flash(el, txt, err) { if (!el) return; el.textContent = txt; el.className = "msg" + (err ? " err" : ""); setTimeout(() => el.textContent = "", 4000); }
async function jget(u) { const r = await fetch(u); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status); return r.json(); }
async function jpost(u, b) { const r = await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) }); const d = await r.json().catch(() => ({})); if (!r.ok) throw new Error(d.error || r.status); return d; }

// 客户端路由状态（来自中转 /v1/routes 的生效值）
let STATE = { providers: {}, models: {}, prefix_routes: [] };
let BACKEND = { mode: "router", provider: "router", reason: "" };

// ── 一致性面板 ──
async function loadStatus() {
  let s; try { s = await jget("/api/status"); } catch (e) { return; }
  const li = s.login || {}, cf = s.config || {};
  $("#dotLogin").className = "dot " + (li.logged_in ? "up" : "down");
  $("#txtLogin").textContent = li.logged_in ? "已登录" : "未登录";
  $("#dotRouter").className = "dot " + (s.router ? "up" : "down");
  $("#txtRouter").textContent = s.router ? "中转在线" : "中转离线";
  $("#stLogin").textContent = li.logged_in ? (li.key_hint || "是") : "否";
  $("#stProvider").textContent = cf.model_provider || "—";
  $("#stModel").textContent = cf.model || "—";
  $("#stEffort").textContent = cf.model_reasoning_effort || "—";
  $("#stCatalog").textContent = s.catalog_models ?? "—";
}

// ── 路由 / provider / model ──
async function loadRoutes() {
  let d; try { d = await jget("/api/routes"); } catch (e) { return; }
  const r = d.routing || {};
  STATE = { providers: r.providers || {}, models: r.models || {}, prefix_routes: r.prefix_routes || [] };
  BACKEND = d.backend || BACKEND;
  const disp = d.display || {};
  $("#stBackend").textContent = BACKEND.mode === "direct" ? "直连 " + BACKEND.provider : "中转 router";
  renderBackend(d);
  renderProviders();
  renderModels(disp);
  fillModelSelect(Object.keys(STATE.models));
}
function renderBackend(d) {
  const ok = BACKEND.mode === "direct";
  $("#backendBox").innerHTML =
    `<div class="mode-badge ${ok ? "direct" : "router"}">${ok ? "可直连（不经中转）" : "需经中转 router"}</div>
     <div class="mode-reason">${esc(BACKEND.reason)}</div>
     <div class="mode-cur">config.toml 当前 model_provider = <b>${esc(d.current_provider || "?")}</b>
       · 路由来源：${esc(d.source || "?")}${d.live ? "（中转实时）" : "（本地文件）"}</div>`;
  const prov = STATE.providers;
  let rows = Object.entries(STATE.models).map(([m, c]) => {
    const p = prov[c.provider] || {};
    return `<tr><td class="mono">${esc(m)}</td><td><span class="ptag">${esc(c.provider)}</span></td>
      <td class="mono small">${esc(p.base || "?")}</td><td class="mono small">${esc(p.key_env || "?")}</td>
      <td><span class="mode ${c.mode}">${esc(c.mode)}</span></td></tr>`;
  }).join("");
  (d.routing.prefix_routes || []).forEach(pr => {
    const p = prov[pr.provider] || {};
    rows += `<tr class="pfx"><td class="mono">${esc((pr.prefixes || []).join(" "))}*</td>
      <td><span class="ptag">${esc(pr.provider)}</span></td><td class="mono small">${esc(p.base || "?")}</td>
      <td class="mono small">${esc(p.key_env || "?")}</td><td><span class="mode ${pr.mode}">${esc(pr.mode)}</span></td></tr>`;
  });
  $("#routesTable").innerHTML =
    `<thead><tr><th>模型</th><th>真实 provider</th><th>base_url</th><th>AK 变量</th><th>模式</th></tr></thead><tbody>${rows}</tbody>`;
}
function renderProviders() {
  $("#providers").innerHTML = Object.entries(STATE.providers).map(([k, v]) => `
    <div class="pcard" data-pname="${esc(k)}">
      <div class="phead"><span class="pname">${esc(k)}</span>
        <button class="x" title="删除" onclick="this.closest('.pcard').dataset.deleted='1';this.closest('.pcard').style.opacity=.4">×</button></div>
      <div class="field"><label>base_url</label><input class="inp mono" data-f="base" value="${esc(v.base || "")}"></div>
      <div class="field"><label>key_env（AK 环境变量名）</label><input class="inp mono" data-f="key_env" value="${esc(v.key_env || "")}"></div>
    </div>`).join("") || "<p class='hint'>暂无 provider</p>";
}
function renderModels(disp) {
  const provOpts = Object.keys(STATE.providers);
  $("#models").innerHTML = Object.entries(STATE.models).map(([slug, c]) => {
    const dm = disp[slug] || {};
    const po = provOpts.map(p => `<option value="${esc(p)}" ${p === c.provider ? "selected" : ""}>${esc(p)}</option>`).join("");
    return `<div class="pcard" data-slug="${esc(slug)}">
      <div class="phead"><span class="pname">${esc(slug)}</span>
        <button class="x" title="删除" onclick="this.closest('.pcard').dataset.deleted='1';this.closest('.pcard').style.opacity=.4">×</button></div>
      <div class="grid2">
        <div class="field"><label>provider（走谁家额度）</label><select class="sel" data-f="provider">${po}</select></div>
        <div class="field"><label>模式</label><select class="sel" data-f="mode">
          <option value="passthrough" ${c.mode === "passthrough" ? "selected" : ""}>passthrough（原生 Responses）</option>
          <option value="translate" ${c.mode === "translate" ? "selected" : ""}>translate（上游仅 chat）</option></select></div>
      </div>
      <div class="grid2">
        <div class="field"><label>显示名</label><input class="inp" data-d="display_name" value="${esc(dm.display_name || "")}"></div>
        <div class="field"><label>描述</label><input class="inp" data-d="description" value="${esc(dm.description || "")}"></div>
      </div>
      <div class="grid2">
        <div class="field"><label>可见性</label><select class="sel" data-d="visibility">
          <option value="list" ${dm.visibility !== "hide" ? "selected" : ""}>list（显示）</option>
          <option value="hide" ${dm.visibility === "hide" ? "selected" : ""}>hide（隐藏）</option></select></div>
        <div class="field"><label>默认思考档</label><input class="inp mono" data-d="default_reasoning_level" value="${esc(dm.default_reasoning_level || "")}"></div>
      </div>
    </div>`;
  }).join("") || "<p class='hint'>暂无 model</p>";
}

// 收集 UI → STATE
function collectRoutes() {
  const providers = {};
  $$("#providers .pcard").forEach(c => { if (c.dataset.deleted) return; const o = {}; $$("[data-f]", c).forEach(f => o[f.dataset.f] = f.value.trim()); providers[c.dataset.pname] = o; });
  const models = {}; const dispModels = [];
  $$("#models .pcard").forEach(c => {
    if (c.dataset.deleted) return;
    const slug = c.dataset.slug;
    models[slug] = { provider: $('[data-f="provider"]', c).value, mode: $('[data-f="mode"]', c).value };
    const d = { slug }; $$("[data-d]", c).forEach(f => d[f.dataset.d] = f.value.trim()); dispModels.push(d);
  });
  return { providers, models, prefix_routes: STATE.prefix_routes, dispModels };
}

// ── 默认模型下拉 / config 顶层 ──
function fillModelSelect(ids) {
  const sel = $("#cfg_model"); const cur = sel.value;
  sel.innerHTML = ids.map(id => `<option value="${esc(id)}">${esc(id)}</option>`).join("");
  if (cur && ids.includes(cur)) sel.value = cur;
}
async function loadConfig() {
  const cfg = await jget("/api/config");
  const sel = $("#cfg_model");
  if (cfg.top.model && ![...sel.options].some(o => o.value === cfg.top.model)) sel.insertAdjacentHTML("afterbegin", `<option value="${esc(cfg.top.model)}">${esc(cfg.top.model)}</option>`);
  sel.value = cfg.top.model || "";
  $("#cfg_provider").value = cfg.top.model_provider || "";
  $("#cfg_effort").value = cfg.top.model_reasoning_effort || "";
  $("#cfg_review").value = cfg.top.review_model || "";
  loadSecrets(Object.values(STATE.providers).map(p => p.key_env).filter(Boolean));
}
async function loadSecrets(names) {
  let st; try { st = await jpost("/api/secrets", { names }); } catch (e) { return; }
  $("#secrets").innerHTML = Object.entries(st).map(([k, v]) => `
    <div class="secret-row"><span class="nm">${esc(k)}</span>
      <span class="badge ${v.set ? "set" : "unset"}">${v.set ? "已设置 " + esc(v.masked) + " · " + esc(v.source) : "未设置"}</span>
      <input class="inp mono" type="password" placeholder="新值（留空不改）" style="max-width:240px">
      <button class="btn" onclick="ccSetKey('${esc(k)}',this)">写入</button></div>`).join("") || "<p class='hint'>未发现 key_env</p>";
}
window.ccSetKey = async (k, btn) => {
  const inp = btn.parentElement.querySelector("input"); if (!inp.value) return;
  try { await jpost("/api/secrets", { set: { name: k, value: inp.value } }); inp.value = ""; flash($("#provMsg"), "已写入 " + k + "，记得重启中转"); }
  catch (e) { flash($("#provMsg"), "写入失败 " + e.message, true); }
  loadSecrets([k]);
};

// ── 运行模式（自定义 ↔ 原生）──
let MODE = { mode: "custom" };
let MODE_TARGET = null;

async function loadMode() {
  let m; try { m = await jget("/api/mode"); } catch (e) { return; }
  MODE = m; MODE_TARGET = m.mode;
  $("#stMode").textContent = m.mode === "native" ? "原生 ChatGPT" : "自定义 中转";
  $$("#modeSeg .seg-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === m.mode));
  const info = $("#modeInfo");
  const np = m.native_profile || {};
  if (m.mode === "custom") {
    info.innerHTML = `当前 <b>自定义</b> · model=<code>${esc(m.model || "?")}</code> · provider=<code>${esc(m.model_provider || "?")}</code> · effort=<code>${esc(m.model_reasoning_effort || "?")}</code> · 登录=<code>apikey 绕过</code>。`
      + `<br>切到「原生」<b>完全割裂</b>：还原 ChatGPT 登录`
      + (m.has_oauth_backup ? `（<code>${esc(m.oauth_backup)}</code>）` : `（⚠ 无登录备份，需 <code>codex login</code>）`)
      + `，model→<code>${esc(np.model || "?")}</code>、effort→<code>${esc(np.model_reasoning_effort || "默认")}</code>，并<b>删除</b> model_provider / 自定义模型目录 / review_model / model_providers 表。`
      + `共享设置（hooks / mcp_servers / plugins / features）两态都不动，不会 drift。`;
  } else {
    info.innerHTML = `当前 <b>原生</b> · model=<code>${esc(m.model || "?")}</code> · ChatGPT 登录${m.has_oauth_tokens ? "（OAuth）" : ""} · 无中转、无自定义模型目录。`
      + `<br>切到「自定义」会精确还原中转 + 第三方模型 + 自定义目录 + apikey 免登录。`;
  }
}

function selectMode(target) {
  MODE_TARGET = target;
  $$("#modeSeg .seg-btn").forEach(b => b.classList.toggle("active", b.dataset.mode === target));
}

// ── 动作 ──
async function refreshAll() { await loadStatus(); await loadMode(); await loadRoutes(); await loadConfig(); }

function bind() {
  $("#refresh").onclick = refreshAll;
  $$("#modeSeg .seg-btn").forEach(b => b.onclick = () => selectMode(b.dataset.mode));
  $("#applyMode").onclick = async () => {
    const target = MODE_TARGET;
    if (!target || target === MODE.mode) { flash($("#modeMsg"), "已经在该模式，无需切换"); return; }
    const warn = target === "native"
      ? "切到「原生」：还原 ChatGPT 登录、model/provider 退回原生、停用中转。需重启桌面 App 生效。继续？"
      : "切到「自定义」：恢复中转 + 第三方模型 + apikey 免登录。需重启桌面 App 生效。继续？";
    if (!confirm(warn)) return;
    $("#applyMode").disabled = true; flash($("#modeMsg"), "切换中…");
    try {
      const r = await jpost("/api/mode", { target });
      let msg = r.detail || ("已切到 " + r.mode);
      if (r.need_login) msg += " ⚠ 登录态可能已过期，请在终端跑一次 codex login";
      flash($("#modeMsg"), msg, !!r.need_login);
      await loadMode(); await loadStatus(); await loadConfig();
      if (confirm("已切换。现在重启 Codex 桌面 App 让它生效？（会关闭在途会话）")) {
        await jpost("/api/restart", { target: "desktop" });
        flash($("#modeMsg"), "已重启桌面 App，稍候刷新"); setTimeout(refreshAll, 4000);
      }
    } catch (e) { flash($("#modeMsg"), "切换失败 " + e.message, true); }
    finally { $("#applyMode").disabled = false; }
  };
  $("#addProvider").onclick = () => {
    const n = prompt("provider 名字（如 openai / my-gateway）："); if (!n) return;
    STATE.providers[n] = { base: "", key_env: "" }; renderProviders();
  };
  $("#addModel").onclick = () => {
    const slug = prompt("模型 slug（上游认的真名，如 vendor/model）："); if (!slug) return;
    const pk = Object.keys(STATE.providers); if (!pk.length) return alert("请先新增 provider");
    STATE.models[slug] = { provider: pk[0], mode: "passthrough" };
    renderModels({}); fillModelSelect(Object.keys(STATE.models));
  };
  $("#saveRoutes").onclick = async () => {
    const { providers, models, prefix_routes, dispModels } = collectRoutes();
    try {
      const r = await jpost("/api/routes", { providers, models, prefix_routes });
      await jpost("/api/catalog", { models: dispModels });
      BACKEND = r.backend || BACKEND;
      flash($("#provMsg"), "已保存路由+目录；重启中转/桌面 App 生效。推荐后端：" + (BACKEND.mode === "direct" ? "直连 " + BACKEND.provider : "中转"));
      await loadRoutes(); await loadConfig();
    } catch (e) { flash($("#provMsg"), "保存失败 " + e.message, true); }
  };
  $("#applyBackend").onclick = async () => {
    const target = BACKEND.provider;
    if (!confirm(`把 config.toml 的 model_provider 设为「${target}」？\n（${BACKEND.reason}）`)) return;
    try { await jpost("/api/config", { model_provider: target }); flash($("#backendMsg"), "已设 model_provider=" + target + "（新会话生效）"); loadStatus(); loadConfig(); }
    catch (e) { flash($("#backendMsg"), "失败 " + e.message, true); }
  };
  $("#saveTop").onclick = async () => {
    try { await jpost("/api/config", { model: $("#cfg_model").value, model_provider: $("#cfg_provider").value, model_reasoning_effort: $("#cfg_effort").value, review_model: $("#cfg_review").value }); flash($("#topMsg"), "已保存（新会话生效）"); loadStatus(); }
    catch (e) { flash($("#topMsg"), "失败 " + e.message, true); }
  };
  $("#fixAuth").onclick = async () => {
    try { const r = await jpost("/api/auth", {}); flash($("#authMsg"), r.detail || "已修复"); loadStatus(); }
    catch (e) { flash($("#authMsg"), "失败 " + e.message, true); }
  };
  $("#restartRouter").onclick = async () => {
    flash($("#backendMsg"), "重启中转中…");
    try { const r = await jpost("/api/restart", { target: "router" }); flash($("#backendMsg"), r.detail || "已重启中转", !r.ok); setTimeout(refreshAll, 1800); }
    catch (e) { flash($("#backendMsg"), "失败 " + e.message, true); }
  };
  $("#restartDesktop").onclick = async () => {
    if (!confirm("重启 Codex 桌面 App 会关闭当前所有在途会话。确定继续？")) return;
    $("#restartDesktop").disabled = true; flash($("#restartMsg"), "重启中…");
    try { const r = await jpost("/api/restart", { target: "desktop" }); flash($("#restartMsg"), r.detail || "已重启"); setTimeout(loadStatus, 4000); }
    catch (e) { flash($("#restartMsg"), "失败 " + e.message, true); }
    finally { $("#restartDesktop").disabled = false; }
  };
}

bind(); refreshAll();
