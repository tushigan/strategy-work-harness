(function (global) {
  "use strict";
  /* Lucide static icon data, ISC License. Copyright (c) 2026 Lucide Icons and Contributors.
   * Permission to use, copy, modify, and/or distribute this software for any purpose with or
   * without fee is hereby granted, provided that the above copyright notice and this permission
   * notice appear in all copies. THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL
   * WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
   * AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR
   * CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
   * WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
   * CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
   * Feather-derived icons: MIT License. Copyright (c) 2013-present Cole Bemis.
   * Permission is hereby granted, free of charge, to any person obtaining a copy of this software
   * and associated documentation files (the "Software"), to deal in the Software without restriction,
   * including without limitation the rights to use, copy, modify, merge, publish, distribute,
   * sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
   * furnished to do so, subject to the following conditions: The above copyright notice and this
   * permission notice shall be included in all copies or substantial portions of the Software.
   * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING
   * BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
   * NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
   * DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
   * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
   */
  const icons = {
    Save: [["path", {d: "M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"}], ["path", {d: "M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"}], ["path", {d: "M7 3v4a1 1 0 0 0 1 1h7"}]],
    Download: [["path", {d: "M12 15V3"}], ["path", {d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"}], ["path", {d: "m7 10 5 5 5-5"}]],
    Upload: [["path", {d: "M12 3v12"}], ["path", {d: "m17 8-5-5-5 5"}], ["path", {d: "M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"}]],
    History: [["path", {d: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"}], ["path", {d: "M3 3v5h5"}], ["path", {d: "M12 7v5l4 2"}]],
    Type: [["path", {d: "M12 4v16"}], ["path", {d: "M4 7V5a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v2"}], ["path", {d: "M9 20h6"}]],
    FileText: [["path", {d: "M6 22a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h8a2.4 2.4 0 0 1 1.704.706l3.588 3.588A2.4 2.4 0 0 1 20 8v12a2 2 0 0 1-2 2z"}], ["path", {d: "M14 2v5a1 1 0 0 0 1 1h5"}], ["path", {d: "M10 9H8"}], ["path", {d: "M16 13H8"}], ["path", {d: "M16 17H8"}]],
    X: [["path", {d: "M18 6 6 18"}], ["path", {d: "m6 6 12 12"}]],
    Eye: [["path", {d: "M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"}], ["circle", {cx: "12", cy: "12", r: "3"}]],
    RotateCcw: [["path", {d: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"}], ["path", {d: "M3 3v5h5"}]],
    Undo2: [["path", {d: "M9 14 4 9l5-5"}], ["path", {d: "M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11"}]],
    ArchiveRestore: [["rect", {width: "20", height: "5", x: "2", y: "3", rx: "1"}], ["path", {d: "M4 8v11a2 2 0 0 0 2 2h2"}], ["path", {d: "M20 8v11a2 2 0 0 1-2 2h-2"}], ["path", {d: "m9 15 3-3 3 3"}], ["path", {d: "M12 12v9"}]],
    AlignLeft: [["path", {d: "M21 5H3"}], ["path", {d: "M15 12H3"}], ["path", {d: "M17 19H3"}]],
    AlignCenter: [["path", {d: "M21 5H3"}], ["path", {d: "M17 12H7"}], ["path", {d: "M19 19H5"}]],
    AlignRight: [["path", {d: "M21 5H3"}], ["path", {d: "M21 12H9"}], ["path", {d: "M21 19H7"}]]
  };
  function node(tag, className = "", text = "") {
    const el = document.createElement(tag); el.className = className; el.textContent = text; return el;
  }
  function button(icon, label, action, text = "") {
    const el = node("button", "bh-button"); el.type = "button"; el.title = label;
    el.setAttribute("aria-label", label); el.onclick = action;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "2", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false"})) svg.setAttribute(key, value);
    for (const [tag, attrs] of icons[icon]) {
      const part = document.createElementNS(svg.namespaceURI, tag);
      for (const [key, value] of Object.entries(attrs)) part.setAttribute(key, value);
      svg.append(part);
    }
    el.append(svg); if (text) el.append(node("span", "", text)); return el;
  }
  function create(options) {
    const root = document.getElementById("bh-root"), byId = id => document.getElementById(id);
    const actions = byId("bh-actions"), panel = byId("bh-format-panel"), inputs = {}, aligns = {}, fontCache = new Map();
    let locked = false, viewing = false, chosen = null, previewId = null;
    actions.replaceChildren(); panel.replaceChildren();
    function show(id) { const dialog = byId(id); if (!dialog.open) dialog.showModal(); resizeText(); }
    function command(icon, label, name, text = "") {
      const el = button(icon, label, () => options.call(name), text); el.dataset.action = name; el.id = `bh-${name}`; return el;
    }
    const save = command("Save", "保存原文件", "save", "保存原文件"); save.classList.add("bh-primary-button"); actions.append(save);
    actions.append(command("Download", "导出HTML", "export", "导出HTML"));
    const upload = command("Upload", "导入品牌屋文件", "import"); upload.onclick = () => byId("bh-import-file").click(); actions.append(upload);
    for (const [icon, label, id] of [["Type", "文字与版式", "format"], ["History", "文件内历史", "history"], ["FileText", "来源与字段详情", "source"]]) {
      const el = button(icon, label, () => show(`bh-${id}-dialog`)); el.dataset.panel = id; el.id = `bh-${id}`;
      el.setAttribute("aria-haspopup", "dialog"); el.setAttribute("aria-controls", `bh-${id}-dialog`); actions.append(el);
    }
    const backup = command("FileText", "草稿备份", "exportDraft", "草稿备份"); backup.title = "下载JSON草稿备份";
    actions.append(command("ArchiveRestore", "恢复辅助草稿", "recoverDraft"), backup);
    const exit = command("Undo2", "退出历史预览", "exitPreview", "退出预览"); exit.hidden = true; actions.append(exit);
    root.querySelectorAll("[data-close]").forEach(slot => {
      const dialog = byId(slot.dataset.close); slot.replaceChildren(button("X", "关闭", () => dialog.close()));
    });
    byId("bh-import-file").onchange = event => {
      const file = event.target.files[0]; event.target.value = ""; if (file) options.call("import", file);
    };
    const note = byId("bh-note");
    note.oninput = () => { note.style.height = "auto"; note.style.height = `${note.scrollHeight + 2}px`; };
    const selectedLabel = node("p", "bh-selected-label"), grid = node("div", "bh-control-grid");
    const error = node("p", "bh-inline-error"); error.id = "bh-control-error"; error.setAttribute("role", "alert");
    panel.append(selectedLabel, grid);
    function control(name, label, type, values, target, change) {
      const wrap = node("label", "bh-control-label", label), input = node(type === "select" ? "select" : type === "textarea" ? "textarea" : "input");
      input.id = `bh-${name}`; wrap.htmlFor = input.id; input.setAttribute("aria-describedby", error.id);
      if (type === "select") values.forEach(([value, text]) => { const option = node("option", "", text); option.value = value; input.append(option); });
      else if (type !== "textarea") { input.type = type; if (values) [input.min, input.max, input.step] = values; }
      if (type === "textarea") { input.rows = 3; wrap.classList.add("bh-full"); }
      input[type === "textarea" ? "oninput" : "onchange"] = () => {
        if (locked || viewing) return;
        const invalid = type === "number" && (!input.value.trim() || !input.checkValidity());
        input.setAttribute("aria-invalid", String(invalid));
        error.textContent = invalid ? `${label}须在 ${input.min} 至 ${input.max} 之间，步长为 ${input.step}。` : "";
        if (!invalid) change(type === "number" ? Number(input.value) : input.value);
        if (type === "textarea") resizeText();
      };
      inputs[name] = input; wrap.append(input); target.append(wrap); return input;
    }
    const textInput = control("text", "文字", "textarea", null, grid, options.text);
    control("fontFamily", "字体", "select", [["system", "系统无衬线"], ["serif", "宋体 / 衬线"], ["mono", "等宽"], ["pingfang", "苹方"], ["yahei", "微软雅黑"]], grid, v => options.style("fontFamily", v));
    const fontState = node("span", "bh-font-state"); fontState.id = "bh-font-state"; fontState.setAttribute("role", "status"); inputs.fontFamily.parentElement.append(fontState);
    control("fontSize", "字号 (px)", "number", [10, 48, 1], grid, v => options.style("fontSize", v));
    control("fontWeight", "字重", "select", [[400, "常规"], [500, "中等"], [600, "半粗"], [700, "粗体"], [800, "特粗"]], grid, v => options.style("fontWeight", Number(v)));
    control("color", "颜色", "color", null, grid, v => options.style("color", v));
    control("lineHeight", "行高", "number", [1.2, 2.2, 0.1], grid, v => options.style("lineHeight", v));
    const alignment = node("fieldset"), segments = node("div", "bh-segments"); alignment.append(node("legend", "", "对齐"), segments); grid.append(alignment);
    for (const [value, icon, label] of [["left", "AlignLeft", "左对齐"], ["center", "AlignCenter", "居中"], ["right", "AlignRight", "右对齐"]]) {
      const el = button(icon, label, () => { if (!locked && !viewing) options.style("textAlign", value); });
      el.dataset.format = "true"; aligns[value] = el; segments.append(el);
    }
    const layout = node("section", "bh-layout"), layoutGrid = node("div", "bh-control-grid");
    layout.append(node("h3", "", "模型版式"), layoutGrid); panel.append(layout, error);
    control("columns", "列数上限", "select", [[1, "1 列"], [2, "2 列"], [3, "3 列"]], layoutGrid, v => options.layout("columns", Number(v)));
    control("width", "最大宽度 (px)", "number", [800, 1600, 1], layoutGrid, v => options.layout("width", v));
    control("gap", "间距 (px)", "number", [0, 32, 1], layoutGrid, v => options.layout("gap", v));
    control("padding", "内边距 (px)", "number", [8, 32, 1], layoutGrid, v => options.layout("padding", v));
    function resizeText() { if (byId("bh-format-dialog").open) { textInput.style.height = "auto"; textInput.style.height = `${textInput.scrollHeight + 2}px`; } }
    function fontStatus(name) {
      if (name === "system") return "当前：系统字体";
      if (fontCache.has(name)) return fontCache.get(name);
      const candidates = {serif: ["Songti SC", "Noto Serif CJK SC", "SimSun"], mono: ["SFMono-Regular", "Consolas", "Liberation Mono"], pingfang: ["PingFang SC", "Microsoft YaHei"], yahei: ["Microsoft YaHei", "PingFang SC"]};
      const context = document.createElement("canvas").getContext("2d"), sample = "策略标题 0123456789 WMWM";
      if (!context) return "当前：浏览器回退字体";
      const found = (candidates[name] || []).find(font => ["monospace", "serif"].some(base => {
        context.font = `16px ${base}`; const width = context.measureText(sample).width;
        context.font = `16px "${font}", ${base}`; return Math.abs(context.measureText(sample).width - width) > .1;
      }));
      const display = {"PingFang SC": "苹方", "Microsoft YaHei": "微软雅黑", "Songti SC": "宋体"};
      const message = `${found === candidates[name]?.[0] ? "当前" : "回退"}：${display[found] || found || ({serif: "系统衬线", mono: "系统等宽"}[name] || "系统无衬线")}`;
      fontCache.set(name, message); return message;
    }
    function selection(field, value, currentLayout) {
      chosen = field; selectedLabel.textContent = field ? field.label : "没有可编辑字段";
      if (document.activeElement !== textInput) textInput.value = value?.text ?? "";
      for (const [key, input] of Object.entries(inputs)) if (key !== "text" && document.activeElement !== input) input.value = value?.style?.[key] ?? currentLayout[key] ?? "";
      for (const [key, el] of Object.entries(aligns)) el.setAttribute("aria-pressed", String(key === value?.style?.textAlign));
      fontState.textContent = fontStatus(value?.style?.fontFamily || "system");
      resizeText();
    }
    function state(readOnly, busy) {
      viewing = readOnly; locked = busy; exit.hidden = !viewing;
      root.querySelectorAll("[data-action]").forEach(el => {
        const name = el.dataset.action;
        el.disabled = locked || !options.available(name) || (viewing && ["save", "export", "import", "recoverDraft"].includes(name));
      });
      panel.querySelectorAll("input,select,textarea,[data-format]").forEach(el => { el.disabled = locked || viewing || !chosen; });
      actions.querySelector('[data-panel="format"]').disabled = locked || !chosen;
      byId("bh-import-file").disabled = locked || viewing || !options.available("import");
      note.disabled = locked || viewing;
    }
    function history(data) {
      const list = byId("bh-history-list"); list.replaceChildren();
      for (const item of [...data.history].reverse()) {
        const row = node("li", "bh-history-row"), copy = node("div"), tools = node("div", "bh-history-actions"); row.dataset.revision = item.id;
        copy.append(node("strong", "", `v${item.version}${item.id === data.current ? " · 当前工作版" : ""}`), node("p", "", item.note || "未填写说明"));
        const date = new Date(item.created_at);
        copy.append(node("small", "", `${Number.isNaN(date.valueOf()) ? item.created_at : date.toLocaleString("zh-CN")} · ${item.kind}`));
        copy.append(node("small", "", item.id));
        if (item.restored_from) copy.append(node("small", "", `恢复自 ${item.restored_from}`));
        for (const [icon, label, action] of [["Eye", "预览", "preview"], ["RotateCcw", "恢复为新版本", "restore"]]) {
          const el = button(icon, `${label} v${item.version}`, () => { byId("bh-history-dialog").close(); options.call(action, item.id); });
          el.dataset.action = action; el.id = `bh-${action}-${item.id}`; tools.append(el);
        }
        row.append(copy, tools); list.append(row);
      }
      if (!data.history.length) list.append(node("li", "", "没有历史版本"));
      preview(previewId); state(viewing, locked);
    }
    function preview(id) {
      previewId = id;
      byId("bh-history-list").querySelectorAll('[data-action="preview"]').forEach(el => el.setAttribute("aria-pressed", String(el.id === `bh-preview-${id}`)));
    }
    return {selection, state, history, preview};
  }
  global.BHControls = {create};
})(globalThis);
