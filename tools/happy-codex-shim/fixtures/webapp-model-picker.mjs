/**
 * 手机端（app.happy.engineering 网页/App bundle）解析模型清单的真实逻辑。
 *
 * 这几个函数是从线上 bundle 里**逐字抄下来**的（2026-09-14，
 * `getAvailableModels` 位于 web-index.js 偏移 5823780 处的模块），
 * 目的只有一个：断言我们上报的 metadata 形状真的能被手机渲染成自定义模型，
 * 而不是靠「看起来像」来判断。字段名错一个（code/value vs key/name），
 * 手机不会报错，只会静默退回内置 GPT 清单。
 *
 * 上游 bundle 更新后要重新核对这几行（见 test-model-meta.mjs 的说明）。
 */

// e.mapMetadataOptions：metadata.models -> 选择器选项
function o(n) {
  return n && 0 !== n.length
    ? n.map(n => ({ key: n.code, name: n.value, description: n.description ?? null }))
    : [];
}

// e.findOptionByKey
function N(n, o) {
  return o ? n.find(n => n.key === o) ?? null : null;
}

// e.getAvailableModels 的非-rig 分支（rig 分支是另一套 metadata，与 codex 无关）。
// 原文：
//   const l=o(t?.models);
//   if(l.length>0)return'codex'!==n||l.some(n=>'default'===n.key)?l:[{key:'default',name:'default model',description:null},...l];
//   return c(n,P(n,s),p)        // <- 没有 models 时回落到硬编码清单
// 这里把最后一行换成可识别的哨兵值，方便断言「到底回落了没有」。
function getAvailableModels(n, t) {
  const l = o(t?.models);
  if (l.length > 0) {
    return 'codex' !== n || l.some(n => 'default' === n.key)
      ? l
      : [{ key: 'default', name: 'default model', description: null }, ...l];
  }
  return 'FALLBACK-to-hardcoded-list';
}

// e.resolveCurrentOption：从候选里挑出「当前模型」角标
function resolveCurrentOption(n, o) {
  for (const t of o) {
    const o = N(n, t);
    if (o) return o;
  }
  return null;
}

export { o as mapMetadataOptions, N as findOptionByKey, getAvailableModels, resolveCurrentOption };
