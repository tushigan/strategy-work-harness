(function () {
  'use strict';
  function recovery(value, data) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('草稿备份结构不符');
    BHCore.validate(value.envelope);
    BHCore.validateSnapshot(value.draft, data.fields);
    if (!BHFile.identity(value.envelope, data)) throw Error('草稿生成依据与文件不符');
    if (typeof value.base_revision !== 'string' || !value.envelope.history.some(v=>v.id===value.base_revision))
      throw Error('草稿缺少有效的基准版本');
    if (value.intent !== null && value.intent !== undefined) {
      const intent = value.intent;
      if (!intent || typeof intent !== 'object' || Array.isArray(intent) ||
          Object.keys(intent).some(k=>!['kind','note','restored_from'].includes(k)) ||
          !['save','restore','import','export'].includes(intent.kind) || typeof intent.note !== 'string')
        throw Error('草稿修订类型无效');
      if (intent.kind==='restore') {
        const target=value.envelope.history.find(v=>v.id===intent.restored_from);
        if (!target || JSON.stringify(target.snapshot)!==JSON.stringify(value.draft)) throw Error('草稿恢复引用不符');
      } else if (intent.restored_from!==null) throw Error('草稿不应含恢复引用');
    }
    if (!Array.isArray(value.raw_conflicts) || value.raw_conflicts.some(item=>!item ||
        typeof item.text!=='string' || typeof item.time!=='string')) throw Error('冲突备份结构不符');
    for (const item of value.raw_conflicts) if (item.bytes_base64 !== undefined) {
      if (typeof item.bytes_base64 !== 'string' || item.text !== '' ||
          item.bytes_base64.length % 4 !== 0 || !/^[A-Za-z0-9+/]*={0,2}$/.test(item.bytes_base64))
        throw Error('冲突原始字节备份无效');
      if (btoa(atob(item.bytes_base64)) !== item.bytes_base64) throw Error('冲突原始字节编码不规范');
    }
    return value;
  }
  function create(data) {
    const key = 'brand-house-draft:' + data.document_id + ':' + location.pathname;
    function read() {
      try {
        const raw = localStorage.getItem(key);
        if (!raw) return {value:null};
        const value = BHCore.parseJSON(raw);
        if (value.document_id !== data.document_id || typeof value.pending!=='boolean') throw Error('草稿归属或状态不符');
        recovery(value,data);
        return {value};
      } catch (error) { return {value:null, error:'草稿缓存不可用：' + error.message}; }
    }
    function write(value) {
      try {
        const text = JSON.stringify({document_id:data.document_id, ...value});
        localStorage.setItem(key, text);
        if (localStorage.getItem(key) !== text) throw Error('回读不符');
        return true;
      } catch (_) { return false; }
    }
    return {read, write};
  }
  globalThis.BHCache = {create,recovery};
}());
