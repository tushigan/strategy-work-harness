(function () {
  'use strict';
  const MAX_BYTES = 40 * 1024 * 1024;
  const digest = async value => Array.from(new Uint8Array(await crypto.subtle.digest(
    'SHA-256', typeof value === 'string' ? new TextEncoder().encode(value) : value)),
  x => x.toString(16).padStart(2, '0')).join('');
  async function read(handle) {
    const file = await handle.getFile();
    if (file.size > MAX_BYTES) throw Error('文件超过40MiB读取保护范围；原文件未改，请交给Agent分批核对');
    const bytes = new Uint8Array(await file.arrayBuffer());
    if (bytes.byteLength > MAX_BYTES) throw Error('文件超过40MiB读取保护范围；原文件未改');
    let raw = null;
    try { raw = new TextDecoder('utf-8', {fatal:true, ignoreBOM:true}).decode(bytes); }
    catch (_) { /* Preserve undecodable bytes without replacement characters. */ }
    return {bytes, raw, hash:await digest(bytes)};
  }
  function base64(bytes) {
    const chunks = [];
    for (let i = 0; i < bytes.length; i += 32768) chunks.push(String.fromCharCode(...bytes.subarray(i, i + 32768)));
    return btoa(chunks.join(''));
  }
  function documentOf(text) {
    if (typeof text !== 'string' || new TextEncoder().encode(text).length > MAX_BYTES) {
      throw Error('文件超过40MiB读取保护范围；原文件未改，请交给Agent分批核对');
    }
    const doc = new DOMParser().parseFromString(text, 'text/html');
    const nodes = doc.querySelectorAll('script#brand-house-data');
    if (nodes.length !== 1 || nodes[0].type !== 'application/json') throw Error('缺少唯一的品牌屋数据容器');
    return {doc, node:nodes[0]};
  }
  function parse(text) {
    return BHCore.parse(documentOf(text).node.textContent);
  }
  function shell(text) {
    const {doc, node} = documentOf(text);
    node.textContent = '';
    return '<!doctype html>\n' + doc.documentElement.outerHTML;
  }
  function serialize(template, data) {
    const {doc, node} = documentOf(template);
    node.textContent = BHCore.encode(data);
    return '<!doctype html>\n' + doc.documentElement.outerHTML;
  }
  function identity(a, b) {
    return a.document_id === b.document_id && a.project_id === b.project_id && a.task_id === b.task_id
      && JSON.stringify(a.source) === JSON.stringify(b.source) && JSON.stringify(a.fields) === JSON.stringify(b.fields);
  }
  function conflict(record, reason) {
    const error = Error(reason);
    error.name = 'ConflictError';
    error.externalText = record.raw ?? '';
    if (record.raw === null) error.externalBytes = base64(record.bytes);
    try { error.externalData = parse(record.raw); } catch (_) { /* Raw bytes stay in the recovery draft. */ }
    return error;
  }
  async function bind(handle, expected, template) {
    if (!handle || handle.kind !== 'file' || !/\.html?$/i.test(handle.name)) throw Error('请选择当前项目的HTML文件');
    const record = await read(handle);
    if (record.raw === null) throw conflict(record, '文件不是有效UTF-8；已保留原始字节，未覆盖');
    let actual;
    try { actual = parse(record.raw); }
    catch (_) { throw conflict(record, '所选文件无法识别为有效品牌屋；已保留原文，未覆盖'); }
    if (!identity(actual, expected)) throw Error('文件身份或生成依据不符；未覆盖其他项目');
    if (shell(record.raw) !== template || JSON.stringify(actual) !== JSON.stringify(expected)) {
      throw conflict(record, '文件已被其他窗口或程序修改；已停止覆盖，请保留双方后核对');
    }
    return {handle, hash:record.hash, raw:record.raw};
  }
  async function write(binding, candidate, template) {
    let stream, closed = false;
    const next = serialize(template, candidate);
    const wanted = await digest(next);
    async function unchanged() {
      const actual = await read(binding.handle);
      if (actual.hash !== binding.hash) throw conflict(actual, '检测到外部修改；保留两份内容，原文件未被本次覆盖');
    }
    try {
      await unchanged();
      stream = await binding.handle.createWritable({mode:'exclusive'});
      await unchanged();
      await stream.write(next);
      await unchanged();
      await stream.close();
      closed = true;
      const actual = await read(binding.handle);
      if (actual.hash !== wanted) throw conflict(actual, '写入后回读与本次版本不一致；结果待核对，草稿仍保留');
      return {handle:binding.handle, hash:wanted, raw:actual.raw, verified:true};
    } catch (error) {
      if (stream && !closed) {
        try { await stream.abort(); } catch (_) { /* A failed close may already have committed. */ }
      }
      if (error.name !== 'ConflictError') {
        try {
          const actual = await read(binding.handle);
          if (actual.hash === wanted) {
            return {handle:binding.handle, hash:wanted, raw:actual.raw, verified:true, recoveredAfterError:true};
          }
        } catch (_) { /* Unknown outcome remains a failure, never an assumed success. */ }
      }
      throw error;
    }
  }
  function download(text, name) {
    const type = /\.json$/i.test(name) ? 'application/json;charset=utf-8' : 'text/html;charset=utf-8';
    const url = URL.createObjectURL(new Blob([text], {type}));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = name;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  globalThis.BHFile = {parse, shell, serialize, identity, bind, write, digest, download};
}());
