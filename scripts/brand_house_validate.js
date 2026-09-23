(function (root) {
  'use strict';
  const forbidden = new Set(['__proto__', 'constructor', 'prototype']);
  const groups = ['header', 'category', 'audience', 'differentiated_value', 'positioning', 'mission',
    'vision', 'values', 'rtb', 'personality', 'proposition', 'slogan', 'notes'];
  const styles = ['fontFamily', 'fontSize', 'fontWeight', 'color', 'textAlign', 'lineHeight'];
  const layouts = ['columns', 'width', 'gap', 'padding'];
  const dataKeys = ['schema_version', 'document_id', 'project_id', 'task_id', 'source', 'fields', 'current', 'history', 'events', 'branches'];
  const kinds = ['generated', 'save', 'restore', 'import', 'export'];
  const statuses = ['attempted', 'readback_verified', 'export_requested', 'failed', 'cancelled', 'conflict'];
  function fail(message) { throw new Error(message); }
  function ensure(condition, message) { if (!condition) fail(message); }
  function record(value) { ensure(value !== null && typeof value === 'object' && !Array.isArray(value), '数据项必须是普通对象'); }
  function shape(value, required, optional = []) {
    record(value);
    ensure(required.every(k => Object.hasOwn(value, k)) && Object.keys(value).every(k => required.includes(k) || optional.includes(k)),
      '数据结构缺少必要字段或含未知附加字段');
  }
  function text(value, nonempty = false) {
    ensure(typeof value === 'string' && (!nonempty || value.trim().length > 0), '文字字段必须是字符串，标识和说明不得为空');
  }
  function identifier(value) {
    ensure(typeof value === 'string' && /^[A-Za-z_][A-Za-z0-9_.:-]*$/.test(value) && value.split(/[.:]/).every(part => !forbidden.has(part)), '字段标识含非法字符或危险名称');
  }
  function uuid(value) {
    ensure(typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value), '文档或记录 ID 必须是有效 UUID');
  }
  function timestamp(value) {
    const m = typeof value === 'string' && /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/.exec(value);
    ensure(m, '时间必须是带时区的 ISO 时间');
    const [year, month, day, hour, minute, second] = m.slice(1, 7).map(Number);
    const days = [31, year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    ensure(month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1] && hour < 24 && minute < 60 && second < 60 && Number.isFinite(Date.parse(value)), '时间包含无效日期或时刻');
  }
  // 检查属性描述符而不是读取属性，避免 getter、隐藏字段和非 JSON 值被悄悄执行或丢失。
  function json(value) {
    const active = new WeakSet(), stack = [{ value, depth: 0 }];
    while (stack.length) {
      const item = stack.pop(), v = item.value;
      if (item.leave) { active.delete(v); continue; }
      if (v === null || typeof v === 'string' || typeof v === 'boolean') continue;
      if (typeof v === 'number') {
        ensure(Number.isFinite(v) && (!Number.isInteger(v) || Number.isSafeInteger(v)), '数值必须有限且整数可精确表示，不能使用 NaN 或无穷大'); continue;
      }
      ensure(typeof v === 'object', '数据只允许 JSON 类型，不能含空值占位、函数或特殊类型');
      ensure(item.depth <= 256 && !active.has(v), '数据存在循环引用或嵌套过深，未截断任何数据');
      const array = Array.isArray(v), prototype = Object.getPrototypeOf(v), keys = Reflect.ownKeys(v);
      ensure(array ? prototype === Array.prototype : prototype === Object.prototype || prototype === null, '不允许自定义原型或特殊对象');
      if (array) ensure(keys.length === v.length + 1, '数组不能含空洞、隐藏属性或额外字段');
      active.add(v); stack.push({ value: v, leave: true });
      for (const key of keys) {
        ensure(typeof key === 'string' && !forbidden.has(key), '数据键名不能含原型注入名称或符号');
        if (array && key === 'length') continue;
        if (array) ensure(/^(0|[1-9]\d*)$/.test(key) && Number(key) < v.length, '数组只能使用连续索引');
        const descriptor = Object.getOwnPropertyDescriptor(v, key);
        ensure(descriptor && Object.hasOwn(descriptor, 'value') && descriptor.enumerable, '不允许访问器或不可枚举属性');
        stack.push({ value: descriptor.value, depth: item.depth + 1 });
      }
    }
    return value;
  }
  function clone(value) {
    json(value);
    function copy(v) {
      if (v === null || typeof v !== 'object') return v;
      if (Array.isArray(v)) return v.map(copy);
      return Object.fromEntries(Object.entries(v).map(([key, child]) => [key, copy(child)]));
    }
    return copy(value);
  }
  function parseJSON(raw) {
    text(raw);
    let data;
    try { data = JSON.parse(raw); } catch { fail('JSON 文本格式错误，未导入数据'); }
    // 语法由原生解析器判断；另扫原文键名，避免 JSON.parse 的重复键覆盖行为。
    const stack = [];
    for (let i = 0; i < raw.length; i++) {
      const token = raw[i];
      if (token === '{' || token === '[') stack.push(token === '{' ? new Set() : null);
      else if (token === '}' || token === ']') stack.pop();
      else if (token === '"') {
        const start = i++;
        for (; i < raw.length && raw[i] !== '"'; i++) if (raw[i] === '\\') i++;
        let next = i + 1;
        while (next < raw.length && /[\t\n\r ]/.test(raw[next])) next++;
        if (raw[next] === ':') {
          const key = JSON.parse(raw.slice(start, i + 1)), keys = stack[stack.length - 1];
          ensure(keys && !keys.has(key), 'JSON 对象包含重复键，不能覆盖已有数据'); keys.add(key);
        }
      }
    }
    return json(data);
  }
  function parse(raw) { return validate(parseJSON(raw)); }
  function fieldList(fields, document) {
    ensure(Array.isArray(fields) && fields.length > 0, '字段清单必须是非空数组');
    const keys = new Set();
    for (const field of fields) {
      shape(field, ['key', 'group', 'role', 'label', 'path']); identifier(field.key); text(field.label);
      ensure(!keys.has(field.key), '字段标识重复'); keys.add(field.key);
      ensure(groups.includes(field.group) && ['title', 'body', 'support', 'label'].includes(field.role), '字段分组或角色不在白名单');
      if (field.path === null) continue;
      ensure(Array.isArray(field.path) && field.path.length > 0, '来源路径必须是非空键或索引数组');
      let value = document;
      for (const segment of field.path) {
        ensure(typeof segment === 'string' && segment.length > 0 && !forbidden.has(segment) || Number.isSafeInteger(segment) && segment >= 0, '来源路径含非法键或索引');
        if (document === undefined) continue;
        ensure(value !== null && typeof value === 'object' && (Array.isArray(value) ? Number.isSafeInteger(segment) : typeof segment === 'string') && Object.hasOwn(value, segment), '来源路径不存在或键与数组索引类型不符');
        value = value[segment];
      }
    }
    return [...keys];
  }
  function range(value, min, max, integer = false) {
    ensure(typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max && (!integer || Number.isInteger(value)), '样式或布局数值超出允许范围或类型错误');
  }
  function snapshot(value, keys) {
    shape(value, ['fields', 'layout']); record(value.fields);
    const actual = Object.keys(value.fields); ensure(actual.length > 0, '快照字段不能为空'); actual.forEach(identifier);
    if (keys) ensure(actual.length === keys.length && keys.every(k => Object.hasOwn(value.fields, k)), '快照字段必须完整匹配固定字段清单');
    for (const field of Object.values(value.fields)) {
      shape(field, ['text', 'style']); text(field.text); shape(field.style, styles);
      const s = field.style;
      ensure(['system', 'serif', 'mono', 'pingfang', 'yahei'].includes(s.fontFamily), '字体不在白名单');
      range(s.fontSize, 10, 48, true); ensure([400, 500, 600, 700, 800].includes(s.fontWeight), '字重不在白名单');
      ensure(typeof s.color === 'string' && /^#[0-9a-f]{6}$/i.test(s.color), '颜色必须是六位十六进制色值');
      ensure(['left', 'center', 'right'].includes(s.textAlign), '文字对齐方式不在白名单'); range(s.lineHeight, 1.2, 2.2);
    }
    shape(value.layout, layouts); ensure([1, 2, 3].includes(value.layout.columns), '布局列数只能是1、2或3');
    range(value.layout.width, 800, 1600); range(value.layout.gap, 0, 32); range(value.layout.padding, 8, 32);
    return value;
  }
  function sameSnapshot(a, b) {
    const keys = Object.keys(a.fields);
    return keys.length === Object.keys(b.fields).length && keys.every(k => Object.hasOwn(b.fields, k) &&
      a.fields[k].text === b.fields[k].text && styles.every(p => a.fields[k].style[p] === b.fields[k].style[p])) &&
      layouts.every(p => a.layout[p] === b.layout[p]);
  }
  function sameData(a, b) {
    if (a === b) return true;
    if (a === null || b === null || typeof a !== 'object' || typeof b !== 'object' || Array.isArray(a) !== Array.isArray(b)) return false;
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every(k => Object.hasOwn(b, k) && sameData(a[k], b[k]));
  }
  function validateSnapshot(value, fields) { json({ value, fields }); return snapshot(value, fieldList(fields)); }
  function document(data, external = false) {
    shape(data, external ? dataKeys.filter(k => k !== 'branches') : dataKeys, external ? ['branches'] : []);
    ensure(data.schema_version === '1.0', '不支持此数据格式版本'); uuid(data.document_id); text(data.project_id, true); text(data.task_id, true);
    shape(data.source, ['body', 'dependencies', 'document']); record(data.source.body); record(data.source.document);
    ensure(Array.isArray(data.source.dependencies), '来源依赖必须是数组'); data.source.dependencies.forEach(record);
    const keys = fieldList(data.fields, data.source.document), revisions = new Map(), ids = new Set([data.document_id.toLowerCase()]);
    const unique = id => { uuid(id); ensure(!ids.has(id.toLowerCase()), '文档内记录 ID 重复'); ids.add(id.toLowerCase()); };
    ensure(Array.isArray(data.history) && data.history.length > 0, '历史必须包含至少一个版本');
    for (const [index, revision] of data.history.entries()) {
      shape(revision, ['id', 'version', 'parent', 'created_at', 'kind', 'note', 'restored_from', 'snapshot']); unique(revision.id);
      ensure(revision.version === index + 1 && revision.parent === (index ? data.history[index - 1].id : null), '历史版本必须连续且父版本必须是上一版');
      timestamp(revision.created_at); text(revision.note); ensure(kinds.includes(revision.kind) && (index === 0 ? revision.kind === 'generated' : revision.kind !== 'generated'), '历史版本类型无效或初始生成位置错误');
      snapshot(revision.snapshot, keys);
      if (revision.kind === 'restore') {
        const target = revisions.get(revision.restored_from);
        ensure(target && sameSnapshot(target.snapshot, revision.snapshot), '恢复必须引用先前已有版本并完整还原其快照');
      } else ensure(revision.restored_from === null, '非恢复版本不得包含恢复引用');
      revisions.set(revision.id, revision);
    }
    ensure(data.current === data.history[data.history.length - 1].id, '当前版本必须指向历史末版');
    ensure(Array.isArray(data.events), '事件记录必须是数组');
    for (const event of data.events) {
      shape(event, ['id', 'time', 'action', 'status', 'detail', 'revision_id']); unique(event.id); timestamp(event.time);
      text(event.action, true); text(event.detail); ensure(statuses.includes(event.status), '事件状态不在白名单');
      ensure(revisions.has(event.revision_id), '事件必须引用本历史已有版本');
    }
    const branches = data.branches === undefined ? [] : data.branches;
    ensure(Array.isArray(branches) && (!external || branches.length === 0), '冲突分支不得循环嵌套外部分支');
    const bases = new Set(revisions.keys());
    for (const branch of branches) {
      shape(branch, ['id', 'time', 'reason', 'base_revision', 'local_snapshot', 'external']); unique(branch.id); timestamp(branch.time); text(branch.reason, true);
      snapshot(branch.local_snapshot, keys); document(branch.external, true);
      ensure(['project_id', 'document_id', 'task_id'].every(k => branch.external[k] === data[k]), '冲突外部数据必须属于同一项目、文档和任务，不能混入其他品牌屋');
      ensure(sameData(branch.external.source, data.source) && sameData(branch.external.fields, data.fields), '冲突外部数据的来源与字段必须和主数据完全一致，不能替换上游');
      branch.external.history.forEach(revision => bases.add(revision.id));
    }
    // 所有外部历史通过后才查基准，平铺分支的排列顺序不能影响引用有效性。
    for (const branch of branches) ensure(bases.has(branch.base_revision), '冲突基准必须引用主历史或已完整保留的外部历史版本');
    return data;
  }
  function validate(data) { json(data); return document(data); }
  root.BHCore = Object.freeze({ validate, validateSnapshot, clone, parse, parseJSON,
    checks: Object.freeze({ json, shape, text, ensure, snapshot, sameSnapshot, styles, layouts }) });
})(globalThis);
