(function (root) {
  'use strict';
  const validation = root.BHCore;
  if (!validation || !validation.checks) throw new Error('请先加载品牌屋数据校验脚本');
  const { validate, validateSnapshot, clone, parse, parseJSON, checks: v } = validation;
  const limit = { warningVersions: 100, blockedVersions: 300, warningBytes: 5 * 1024 ** 2, blockedBytes: 20 * 1024 ** 2 };
  const escapes = { '<': '\\u003c', '>': '\\u003e', '&': '\\u0026', '\u2028': '\\u2028', '\u2029': '\\u2029' };
  function serialize(data) { return JSON.stringify(data).replace(/[<>&\u2028\u2029]/g, c => escapes[c]); }
  function encode(data) { validate(data); return serialize(data); }
  function current(data) { validate(data); return data.history[data.history.length - 1]; }
  function measure(data) {
    const bytes = new TextEncoder().encode(serialize(data)).length, versions = data.history.length;
    return { bytes, versions, warning: versions >= limit.warningVersions || bytes >= limit.warningBytes,
      blocked: versions >= limit.blockedVersions || bytes >= limit.blockedBytes };
  }
  function capacity(data) { validate(data); return measure(data); }
  function compare(a, b) {
    v.json(a); v.json(b); v.snapshot(a); v.snapshot(b);
    const content = [], style = [], keys = [...new Set([...Object.keys(a.fields), ...Object.keys(b.fields)])].sort();
    for (const key of keys) {
      const left = Object.hasOwn(a.fields, key) ? a.fields[key] : null, right = Object.hasOwn(b.fields, key) ? b.fields[key] : null;
      if (!left || !right || left.text !== right.text) content.push(key);
      if (!left || !right || v.styles.some(property => left.style[property] !== right.style[property])) style.push(key);
    }
    return { content, style, layout: v.layouts.some(property => a.layout[property] !== b.layout[property]) };
  }
  function freshId(data) {
    const occupied = new Set([data.document_id, ...data.history.map(r => r.id), ...data.events.map(e => e.id), ...data.branches.map(b => b.id)].map(id => id.toLowerCase()));
    for (let attempt = 0; attempt < 8; attempt++) {
      const bytes = new Uint8Array(16);
      try { root.crypto.getRandomValues(bytes); } catch { throw new Error('当前环境缺少安全随机数能力，未新增版本'); }
      bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
      const hex = [...bytes].map(n => n.toString(16).padStart(2, '0')).join('');
      const id = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
      if (!occupied.has(id)) return id;
    }
    throw new Error('无法生成唯一版本 ID，未新增版本');
  }
  function append(data, snapshot, options = {}) {
    validate(data); validateSnapshot(snapshot, data.fields); v.json(options); v.shape(options, [], ['kind', 'note', 'restored_from']);
    const kind = options.kind === undefined ? 'save' : options.kind, restoredFrom = options.restored_from === undefined ? null : options.restored_from;
    v.ensure(['save', 'restore', 'import', 'export'].includes(kind), '新增版本类型只能是保存、恢复、导入或导出');
    if (options.note !== undefined) v.text(options.note);
    let target;
    if (kind === 'restore') {
      target = data.history.find(revision => revision.id === restoredFrom);
      v.ensure(target && v.sameSnapshot(snapshot, target.snapshot), '恢复必须引用已有版本并完整还原其快照');
    } else v.ensure(restoredFrom === null, '保存版本不能携带恢复引用');
    v.ensure(!measure(data).blocked, '历史已达容量保护上限，停止新增；原历史仍可完整读取和导出');
    const changes = compare(data.history[data.history.length - 1].snapshot, snapshot);
    const summary = target ? `恢复第${target.version}版` : `保存：${changes.content.length}项文字、${changes.style.length}项样式${changes.layout ? '及版式变更' : ''}`;
    const revision = { id: freshId(data), version: data.history.length + 1, parent: data.current, created_at: new Date().toISOString(),
      kind, note: options.note && options.note.trim() ? options.note : summary, restored_from: restoredFrom, snapshot };
    const next = { ...data, current: revision.id, history: [...data.history, revision] };
    // 可以刚好达到上限，但不能跨过；已在上限的文件只读，不裁切旧历史。
    const nextSize = measure(next);
    v.ensure(nextSize.versions <= limit.blockedVersions && nextSize.bytes <= limit.blockedBytes, '新增版本将超过容量上限，未新增也未删除历史；请完整导出后处理');
    return clone(next);
  }
  root.BHCore = Object.freeze({ validate, validateSnapshot, current, append, compare, capacity, encode, clone, parse, parseJSON });
})(globalThis);
