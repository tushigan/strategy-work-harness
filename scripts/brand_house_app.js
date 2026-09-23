(function () {
  'use strict';
  function boot() {
  const original = '<!doctype html>\n' + document.documentElement.outerHTML;
  const template = BHFile.shell(original);
  let data, ui;
  try { data = BHFile.parse(original); }
  catch (error) {
    document.body.textContent = '品牌屋数据无法读取，未改原文件：' + error.message;
    return;
  }
  let anchor = BHCore.clone(data), binding = null, draft = BHCore.clone(BHCore.current(data).snapshot);
  let preview = null, pending = false, working = false, intent = null, rawConflicts = [];
  const cache = BHCache.create(data);
  const cached = cache.read();
  let recovery = cached.value;
  function status(message, level = 'info') { ui.status(message, level); }
  function event(action, state, detail, revision = data.current) {
    data.events.push({id:crypto.randomUUID(), time:new Date().toISOString(), action,
      status:state, detail, revision_id:revision});
  }
  function store() {
    const okay = cache.write({base_revision:anchor.current, envelope:data, draft, pending,
      intent, raw_conflicts:rawConflicts, time:new Date().toISOString()});
    if (!okay) status('草稿缓存失败；当前修改只在本窗口，请导出草稿后再关闭', 'error');
    return okay;
  }
  function branch(external, reason) {
    const preserved = BHCore.clone(external);
    for (const item of preserved.branches) {
      const existing = data.branches.find(b => b.id === item.id);
      if (existing && JSON.stringify(existing) !== JSON.stringify(item)) throw Error('分支编号冲突，未合并；请备份双方后交给Agent');
      if (!existing) data.branches.push(BHCore.clone(item));
    }
    preserved.branches = [];
    data.branches.push({id:crypto.randomUUID(), time:new Date().toISOString(), reason,
      base_revision:anchor.current, local_snapshot:BHCore.clone(draft), external:preserved});
  }
  function onError(error, action) {
    const type = error.name === 'AbortError' ? 'cancelled' : error.name === 'ConflictError' ? 'conflict' : 'failed';
    if (typeof error.externalText === 'string') rawConflicts.push({time:new Date().toISOString(),
      text:error.externalText, ...(error.externalBytes ? {bytes_base64:error.externalBytes} : {})});
    if (error.externalData && BHFile.identity(error.externalData, data)) {
      try { branch(error.externalData, error.message); } catch (_) { /* Both raw branches remain available for backup. */ }
    }
    event(action, type, error.message);
    pending = true;
    const retained = store();
    const message = type === 'cancelled' ? '已取消，未完成写回；修改仍保留在草稿中' : error.message + '；未确认保存成功，草稿仍保留';
    status(message + (retained ? '' : '。缓存也失败，请立即导出草稿备份'), 'error');
  }
  function change(snapshot) {
    if (working || preview) return;
    draft = BHCore.clone(snapshot);
    pending = true;
    intent = null;
    if (store()) status('有未保存修改', 'warning');
  }
  function candidate(kind) {
    const revision = intent || {kind, note:ui.getNote(), restored_from:null};
    return BHCore.append(data, draft, revision);
  }
  function capacityNote() {
    const size = BHCore.capacity(data);
    return size.blocked ? '；历史已达容量上限，停止新增，未保存稿请草稿备份' :
      size.warning ? '；历史接近容量上限，请交Agent核对，旧版本不会自动删除' : '';
  }
  async function save() {
    if (working || preview) return;
    if (data.project_id === 'UNINITIALIZED') return status('通用模板没有项目归属，请先由Agent生成项目品牌屋', 'warning');
    if (typeof showOpenFilePicker !== 'function' || !isSecureContext) {
      onError(Error('此浏览器不支持原文件写回；请使用完整HTML导出'), 'writeback');
      return;
    }
    working = true;
    ui.busy(true);
    let chosen;
    try {
      // Open the picker immediately within the user's click activation.
      if (!binding) [chosen] = await showOpenFilePicker({multiple:false,
        types:[{description:'项目品牌屋HTML', accept:{'text/html':['.html']}}]});
      if (chosen) binding = await BHFile.bind(chosen, anchor, template);
      const next = candidate('save');
      next.events.push({id:crypto.randomUUID(), time:new Date().toISOString(), action:'writeback',
        status:'attempted', detail:'等待流关闭及磁盘回读；本条不是成功回执', revision_id:next.current});
      const result = await BHFile.write(binding, next, template);
      binding = result;
      anchor = BHCore.clone(next);
      data = next;
      event('writeback', 'readback_verified', '流已关闭，实际文件字节与待保存HTML一致');
      pending = false;
      intent = null;
      ui.setData(data);
      ui.setDraft(draft);
      const retained = store();
      status('已写回所选原文件，并回读确认 v' + BHCore.current(data).version + capacityNote() +
        (retained ? '' : '；辅助缓存失败，文件内版本与历史已保存'), retained ? 'success' : 'warning');
    } catch (error) { onError(error, 'writeback'); }
    finally { working = false; ui.busy(false); }
  }
  function exportFile() {
    if (working || preview) return;
    try {
      const atLimit = BHCore.capacity(data).blocked;
      const next = atLimit ? BHCore.clone(data) : candidate('export');
      next.events.push({id:crypto.randomUUID(), time:new Date().toISOString(), action:'export',
        status:'export_requested', detail:'仅请求下载，无法确认系统下载完成；原文件未更新', revision_id:next.current});
      BHFile.download(BHFile.serialize(template, next), '品牌屋-完整历史.html');
      data = next;
      pending = true;
      if (!atLimit) intent = null;
      ui.setData(data);
      const retained = store();
      status('已请求导出完整HTML；原文件未更新，请核对下载文件' +
        (atLimit ? '。已达容量上限，本次只导出已有快照；未保存修改请另做草稿备份' : capacityNote()) +
        (retained ? '' : '。缓存失败，请勿关闭未核对的草稿'), 'warning');
    } catch (error) { onError(error, 'export'); }
  }
  function exportDraft() {
    if (working) return;
    try {
      const backup = {format:'brand-house-recovery-1', envelope:data, draft, intent,
        raw_conflicts:rawConflicts, base_revision:anchor.current, time:new Date().toISOString()};
      BHFile.download(JSON.stringify(backup), '品牌屋-未保存草稿备份.json');
      event('recovery-export', 'export_requested', '请求下载完整历史与未保存稿备份；不是原HTML写回');
      const retained=store();
      status('已请求草稿备份下载，包含完整历史和未保存内容；请核对文件' +
        (retained ? '' : '。缓存失败，请勿关闭未核对的草稿'), 'warning');
    } catch (error) { status('草稿备份未完成：' + error.message, 'error'); }
  }
  async function importFile(file) {
    if (!file || working || preview) return;
    working = true;
    ui.busy(true);
    status('正在读取导入文件，尚未保存', 'info');
    try {
      const raw = await file.text();
      const parsed = /\.json$/i.test(file.name) ? BHCore.parseJSON(raw) : BHFile.parse(raw);
      const backup = parsed?.format === 'brand-house-recovery-1' ? BHCache.recovery(parsed,data) : null;
      const incoming = BHCore.validate(backup ? backup.envelope : parsed);
      if (!BHFile.identity(incoming, data)) throw Error('导入文件不属于当前品牌屋或其生成依据不同');
      branch(incoming, '手动导入；双方完整历史留在分支中，尚未写回');
      draft = BHCore.clone(backup ? backup.draft : BHCore.current(incoming).snapshot);
      if (backup && Array.isArray(backup.raw_conflicts)) rawConflicts.push(...backup.raw_conflicts);
      pending = true;
      intent = {kind:'import', note:'从所选文件导入，保留原内容分支', restored_from:null};
      ui.setDraft(draft);
      const retained=store();
      status('已载入导入草稿，双方历史保留；尚未保存' +
        (retained ? '' : '。缓存失败，请立即做草稿备份'), 'warning');
    } catch (error) { onError(error, 'import'); }
    finally { working = false; ui.busy(false); }
  }
  function showPreview(id) {
    const version = data.history.find(v => v.id === id);
    if (!version || working) return;
    preview = id;
    ui.setPreview?.(id);
    ui.render(version.snapshot, true);
    status('正在只读查看 v' + version.version + '；当前稿和未保存修改未变', 'info');
  }
  function exitPreview() {
    if (working) return;
    preview = null; ui.setPreview?.(null); ui.render(draft, false);
    status(pending ? '有未保存草稿' : '已返回当前稿');
  }
  async function restore(id) {
    if (working) return;
    const version = data.history.find(v => v.id === id);
    if (!version) return;
    if (pending) branch(data, '恢复历史前保留未保存草稿');
    draft = BHCore.clone(version.snapshot);
    intent = {kind:'restore', note:'从 v' + version.version + ' 恢复；不恢复批准', restored_from:id};
    pending = true;
    preview = null;
    ui.setPreview?.(null);
    ui.setDraft(draft);
    store();
    await save();
  }
  function recoverDraft() {
    if (!recovery || working || preview) return;
    if (recovery.pending) {
      branch(recovery.envelope, '恢复辅助缓存中的未保存稿；不替换当前文件历史');
      draft = BHCore.clone(recovery.draft);
      intent = recovery.intent || {kind:'import', note:'恢复本地未保存草稿', restored_from:null};
      if (intent.restored_from && !data.history.some(v=>v.id===intent.restored_from)) intent = null;
      rawConflicts = rawConflicts.concat(recovery.raw_conflicts || []);
      pending = true;
      ui.setDraft(draft);
      const retained=store();
      status('未保存稿已恢复；磁盘当前版和后续历史未被替换' +
        (retained ? '' : '。缓存失败，请立即做草稿备份'), 'warning');
    }
    recovery = null;
  }
  ui = BHUI.mount(data, {change, save, export:()=>exportFile(), import:importFile,
    preview:showPreview, restore, exitPreview, recoverDraft, exportDraft});
  if (cached.error) status(cached.error + '；文件内历史不受影响', 'warning');
  else if (recovery?.pending) status('发现未保存草稿；可选择恢复，文件当前版未被替换', 'warning');
  else if (data.project_id === 'UNINITIALIZED') status('通用模板，尚未绑定项目', 'warning');
  else status('文件内 v' + BHCore.current(data).version + '；保存时需选择本文件授权' + capacityNote());
  addEventListener('beforeunload', e => { if (pending || working) { e.preventDefault(); e.returnValue = ''; } });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true});
  else boot();
}());
