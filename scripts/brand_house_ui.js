(function (global) {
  "use strict";
  const clone = value => JSON.parse(JSON.stringify(value));
  const fonts = {
    system: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif',
    serif: '"Songti SC", "Noto Serif CJK SC", SimSun, serif',
    mono: '"SFMono-Regular", Consolas, "Liberation Mono", "PingFang SC", monospace',
    pingfang: '"PingFang SC", "Microsoft YaHei", sans-serif',
    yahei: '"Microsoft YaHei", "PingFang SC", sans-serif'
  };
  const defaults = {fontFamily: "system", fontSize: 18, fontWeight: 400, color: "#202424", textAlign: "left", lineHeight: 1.6};
  const tiers = [["vision"], ["mission", "values"], ["positioning"], ["category", "audience", "differentiated_value"], ["rtb"], ["personality"], ["proposition"], ["slogan"], ["notes"]];
  function node(tag, className = "", text = "") {
    const el = document.createElement(tag); el.className = className; el.textContent = text; return el;
  }
  function mount(initialData, callbacks = {}) {
    if (!global.BHControls) throw new Error("缺少品牌屋编辑控件脚本");
    let data = clone(initialData), draft, shown, readOnly = false, working = false, selected = null, previewId = null;
    let sourceText = JSON.stringify(data.source, null, 2);
    const root = document.getElementById("bh-root"), model = document.getElementById("bh-model"), elements = new Map();
    const current = global.BHCore?.current ? global.BHCore.current(data) : data.history[data.history.length - 1];
    draft = clone(current.snapshot); shown = draft;
    function status(message, level = "info") {
      const el = document.getElementById("bh-status"); el.textContent = String(message ?? "");
      el.dataset.level = ["error", "failed"].includes(level) ? "error" : level;
      el.setAttribute("role", el.dataset.level === "error" ? "alert" : "status");
      el.setAttribute("aria-live", el.dataset.level === "error" ? "assertive" : "polite");
    }
    function call(name, ...args) {
      if (working || (readOnly && ["change", "save", "export", "import", "recoverDraft"].includes(name))) return;
      if (typeof callbacks[name] !== "function") return status("此操作尚未连接主控。", "warning");
      try {
        const result = callbacks[name](...args);
        if (result && typeof result.catch === "function") result.catch(error => status(error.message || "操作失败，草稿仍保留。", "error"));
      } catch (error) { status(error.message || "操作失败，草稿仍保留。", "error"); }
    }
    const controls = global.BHControls.create({
      call, available: name => typeof callbacks[name] === "function",
      text: text => edit("text", text), style: (name, value) => edit("style", value, name),
      layout: (name, value) => { if (!readOnly && !working) { draft.layout[name] = value; applyLayout(); changed(); } }
    });
    function value(field) { return shown.fields[field.key]; }
    function fieldName(field) {
      const title = data.fields.find(item => item.group === field.group && item.role === "title");
      return `${title ? value(title).text || field.group : field.group} / ${field.label}`;
    }
    function style(el, field) {
      const s = {...defaults, ...value(field).style};
      el.style.fontFamily = fonts[s.fontFamily] || fonts.system;
      el.style.fontSize = `${s.fontSize}px`; el.style.fontWeight = s.fontWeight;
      el.style.color = s.color; el.style.textAlign = s.textAlign; el.style.lineHeight = s.lineHeight;
    }
    function syncControls() {
      const field = data.fields.find(f => f.key === selected), item = field ? value(field) : null;
      controls.selection(field ? {...field, label: fieldName(field)} : null, item ? {...item, style: {...defaults, ...item.style}} : null, shown.layout);
      controls.state(readOnly, working);
    }
    function select(field) {
      if (selected) elements.get(selected)?.removeAttribute("data-selected");
      selected = field.key; elements.get(selected)?.setAttribute("data-selected", "true");
      syncControls(); source();
    }
    function changed() {
      syncControls(); call("change", clone(draft));
    }
    function edit(kind, input, property) {
      if (readOnly || working || !selected) return;
      const field = data.fields.find(f => f.key === selected), item = draft.fields[selected], el = elements.get(selected);
      if (kind === "text") {
        item.text = input; if (document.activeElement !== el) el.textContent = input;
        if (field.role === "title") for (const sibling of data.fields.filter(f => f.group === field.group)) elements.get(sibling.key)?.setAttribute("aria-label", fieldName(sibling));
      }
      else { item.style[property] = input; style(el, field); }
      changed();
    }
    function insertText(el, text) {
      const selection = global.getSelection();
      if (!selection.rangeCount || !el.contains(selection.getRangeAt(0).commonAncestorContainer)) return;
      const range = selection.getRangeAt(0); range.deleteContents();
      const textNode = document.createTextNode(text); range.insertNode(textNode);
      range.setStartAfter(textNode); range.collapse(true); selection.removeAllRanges(); selection.addRange(range);
      el.dispatchEvent(new Event("input", {bubbles: true}));
    }
    function editable(field, tag = "div") {
      const el = node(tag, "bh-field", value(field).text); el.dataset.key = field.key;
      el.dataset.role = field.role; el.id = `bh-field-${field.key}`; el.tabIndex = 0;
      el.setAttribute("role", "textbox"); el.setAttribute("aria-multiline", "true");
      el.setAttribute("aria-label", fieldName(field)); el.spellcheck = false; style(el, field);
      el.onfocus = () => select(field);
      el.oninput = event => { if (!event.isComposing && !readOnly && !working) { selected = field.key; edit("text", el.innerText); } };
      el.oncompositionend = () => { if (!readOnly && !working) { selected = field.key; edit("text", el.innerText); } };
      el.onpaste = event => { event.preventDefault(); if (!readOnly && !working) insertText(el, event.clipboardData.getData("text/plain")); };
      el.ondrop = event => event.preventDefault();
      elements.set(field.key, el); return el;
    }
    function bucket(field) {
      const path = field.path;
      if (!Array.isArray(path)) return null;
      if (path[0] === "sections") return String(path[2] || "support");
      return path.slice(0, path[0] === "gaps" ? 2 : 1).join(".") || "support";
    }
    function section(group, fields) {
      const section = node("section", group === "header" ? "bh-model-header" : "bh-section"); section.dataset.group = group;
      const heading = node("header", "bh-heading"), primary = node("div", "bh-primary"), supports = node("div", "bh-supports"), buckets = new Map();
      const title = fields.find(field => field.role === "title");
      if (title) section.setAttribute("aria-labelledby", `bh-field-${title.key}`);
      function support(name) {
        if (!buckets.has(name)) { const el = node("div", "bh-support-group"); el.dataset.support = name; buckets.set(name, el); supports.append(el); }
        return buckets.get(name);
      }
      fields.forEach((field, index) => {
        if (group === "header") { section.append(editable(field, field === title ? "h1" : "div")); return; }
        if (field.role === "title") { heading.append(editable(field, "h2")); return; }
        if (field.role === "support") { support(bucket(field) || "support").append(editable(field)); return; }
        if (field.role === "label") {
          const next = fields.slice(index + 1).find(f => f.role !== "label");
          const known = ["counterevidence", "evidence", "assumptions", "verification"].find(key => field.key.split(/[_-]/).includes(key));
          const target = known || (next?.role === "support" ? bucket(next) : null);
          (target ? support(target) : primary).append(editable(field, "h3")); return;
        }
        primary.append(editable(field));
      });
      if (group !== "header") { if (heading.childElementCount) section.append(heading); section.append(primary, supports); }
      return section;
    }
    function applyLayout() {
      for (const key of ["columns", "width", "gap", "padding"]) model.style.setProperty(`--bh-${key}`, `${shown.layout[key]}${key === "columns" ? "" : "px"}`);
    }
    function states() {
      model.setAttribute("aria-busy", String(working)); root.dataset.readOnly = String(readOnly);
      const preview = data.history.find(item => item.id === previewId);
      document.getElementById("bh-mode").textContent = readOnly ? `历史预览${preview ? ` v${preview.version}` : ""} · 只读` : "";
      for (const el of elements.values()) {
        el.contentEditable = readOnly || working ? "false" : "plaintext-only";
        el.setAttribute("aria-readonly", String(readOnly || working));
      }
      controls.state(readOnly, working);
    }
    function render(snapshot, preview = false) {
      readOnly = Boolean(preview);
      if (!readOnly) draft = clone(snapshot);
      shown = readOnly ? clone(snapshot) : draft;
      const fragment = document.createDocumentFragment(), groups = new Map(); elements.clear();
      for (const field of data.fields) {
        if (!shown.fields[field.key]) throw new Error(`字段缺少内容：${field.key}`);
        if (!groups.has(field.group)) groups.set(field.group, []); groups.get(field.group).push(field);
      }
      if (groups.has("header")) { fragment.append(section("header", groups.get("header"))); groups.delete("header"); }
      for (const tier of tiers) {
        const row = node("div", `bh-tier${tier.length === 2 ? " bh-tier-two" : tier.length === 3 ? " bh-tier-three" : ""}`);
        for (const group of tier) if (groups.has(group)) { row.append(section(group, groups.get(group))); groups.delete(group); }
        if (row.childElementCount) fragment.append(row);
      }
      for (const [group, fields] of groups) fragment.append(section(group, fields));
      model.replaceChildren(fragment); applyLayout();
      const field = data.fields.find(f => f.key === selected) || data.fields[0];
      if (field) select(field); else syncControls();
      states();
    }
    function source() {
      const panel = document.getElementById("bh-source-panel"), info = node("dl", "bh-source-list");
      const field = data.fields.find(f => f.key === selected);
      function entry(label, text) { info.append(node("dt", "", label), node("dd", "", String(text))); }
      entry("项目", data.project_id); entry("任务", data.task_id); entry("文档", data.document_id); entry("当前工作版", data.current);
      if (field) {
        entry("当前字段", field.label); entry("字段标识", field.key); entry("字段分组", `${field.group} / ${field.role}`);
        entry("来源路径", field.path === null ? "模型标签，无来源正文路径" : JSON.stringify(field.path));
        let original = data.source.document;
        for (const key of field.path || []) original = original != null && Object.prototype.hasOwnProperty.call(original, key) ? original[key] : undefined;
        if (field.path !== null) entry("原始值", original === undefined ? "该路径不存在于来源文档" : typeof original === "string" ? original : JSON.stringify(original, null, 2));
      }
      const pre = node("pre", "bh-source-pre", sourceText);
      panel.replaceChildren(info, node("h3", "bh-source-panel-heading", "完整来源数据（只读）"), pre);
    }
    function history(next) {
      controls.history(next);
      const revision = next.history.find(item => item.id === next.current);
      document.getElementById("bh-revision").textContent = revision ? `当前 v${revision.version}` : "";
    }
    function setData(next) { data = clone(next); sourceText = JSON.stringify(data.source, null, 2); history(data); source(); }
    function busy(flag) { working = Boolean(flag); states(); }
    function setDraft(snapshot) { render(snapshot, false); }
    function setPreview(id) { previewId = id; controls.preview(id); states(); }
    render(draft, false); history(data);
    return {render, history, status, busy, getDraft: () => clone(draft), setDraft, setData, getNote: () => document.getElementById("bh-note").value.trim(), setPreview};
  }
  global.BHUI = {mount};
})(globalThis);
