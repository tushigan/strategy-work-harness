---
name: html-deck
description: 当前逐字稿已独立检核且策略师确认后，制作、回读、独立检核和单独确认本地 HTML PPT；不处理设计稿检核。
---

# HTML PPT

## 内容与门禁

Codex 自然语言对话是主入口，例如“按这个任务已确认的完整逐字稿生成 HTML 演示稿，独立逐页检查后交我单独确认”。主控安排 `deck-builder` 准备请求，实际登记和检核记录由主控操作，不要求策略师写 JSON。本版只聚焦 Mac；完整步骤及格式见根目录 `提案与设计检核运行说明.md`，编排规则以 `AGENTS.md` 为准。

逐字稿是唯一策略来源。完整读取当前逐字稿、品牌屋、推导、任务简报及其独立报告，不以主控摘要代替实际文件。先提炼页级方案，区分屏幕要点与完整讲者文字；可忠实概括，不能改变结论、删掉限定条件或新增事实。原逐字稿、标题、听众和目的不改动。

`generate` 先调用 `proposal_script.gate(..., human=True)` 核对当前逐字稿；再核对请求中的简报、品牌屋正文及推导三个上游是否为当前版本、独立检核通过且与逐字稿依赖完全对应。旧指纹、缺确认、来源未检核均停止，不用浏览器缓存放行。

## 主控接口

模块：`scripts/html_deck.py`。根目录参数为工作包目录；任务编号沿用逐字稿任务编号；作者填写真实执行实例。请求必须含四个 `{space, key, version, sha256}` 完整版本引用和一份 `page_plan`；可另带 `visual_style:{path,sha256}` 提供本任务 CSS。已有 PPT 要重新生成时，另提供 `revision`，由主控绑定上一版引用并说明改动原因：

- `script`：`space=phase5`，`key=<task_id>::proposal-script`。
- `brand-house`、`derivation`、`brief`：`space=phase3`，分别指向相应正文的当前版本。
- `page_plan`：必填，绑定同一 `script_ref`，逐页描述标题、画面要点、章节与原文引用，以及按需的表格、图表、本地图片。完整格式见 [页级方案](references/page-plan.md)。四个版本引用本身不足以生成正式稿。

Python 接口：

```python
generate(root, task_id, author_instance, request)
inspect(root, task_id)
review_sources(root, key)
record_review(root, key, report_path)
gate(root, key, human=False)
confirm(root, key, evidence, actor, simulation=False)
status(root, key)
```

除 `generate/inspect` 外，`key` 为 `<task_id>::html-deck`；`human` 为关键字参数。报告及确认原文通过文件路径提供。模拟记录只可在已有 `test_mode=true` 的隔离测试工作包中使用，不能为放行而修改该标志。

主控现有入口示例：

```bash
python scripts/phase5.py deck-generate --workspace . --task ITEM-001-proposal --author ACTUAL-DECK-AUTHOR --request project/inputs/PPT版本请求.json
python scripts/phase5.py deck-sources --workspace . --task ITEM-001-proposal
python scripts/phase5.py deck-browser-check --workspace . --task ITEM-001-proposal
python scripts/phase5.py deck-review --workspace . --task ITEM-001-proposal --input project/reviews/PPT独立检核.json
python scripts/phase5.py deck-confirm --workspace . --task ITEM-001-proposal --actor 实际策略师 --input project/inputs/PPT确认原文.txt
python scripts/phase5.py deck-gate --workspace . --task ITEM-001-proposal --request project/author-input/内部确认门禁.json
```

## 生成与回溯

逐字稿的结构化文件必须唯一且与版本指纹一致，包含 `title`、`audience`、`purpose` 和非空 `sections`；每章包含唯一 `id`、`title`、`spoken_text`、`transition`。作者据此制作 `page_plan`，不是按字数切稿。封面保留提案标题，内容页写一个明确结论及必要支撑，并保留假设、样本范围和反证。每个章节至少有一页，逐页引用真实原文。程序自动保留对应章节全文和转场到折叠讲者稿；不会把完整口语段落默认投屏。

输出位于 `project/outputs/proposal/<task_id>/html-deck/`：`presentation.html` 为当前单文件，`current.json` 为结构化版本，`versions/vNNNN.html` 和同名 JSON 保留不可覆盖历史。新版 `data-phase5-deck` 保存页级方案、完整讲者稿、逐页 citations、逐字稿及上游引用。图片同时内嵌到 HTML 并冻结到 assets 和版本快照，列入必读来源。旧格式文件可读取但不自动升级或补造批准；重新生成必须提供新方案。

逐字稿更新后，旧 PPT 文件和独立报告保留，但依赖检查显示 `stale`。只有新逐字稿重新检核并确认，且主控已有明确更新授权，才能生成对齐版；旧确认不迁移到新版，同版新报告也使旧确认失效。`revision.base` 绑定上一版，`trigger/reason` 为非空文字，`changes` 为非空文字列表。初稿后最多两轮自动修订，每轮独立复核；两轮后停止，人工 `allow-more` 精确绑定前版，将其 `record_id` 填下一版 `allowance_id`，仅增一轮、不清零累计次数。

## 视觉生产层

逐字稿确认后，`deck-builder` 调用随包 `huashu-design` 准备页级方案、素材和 CSS，主控把方案和 `visual_style` 与四个来源一起提交。正式视觉直接进入 `presentation.html`，不是旁边另一份文件；CSS 同时冻结到当前内容寻址文件与版本快照，列入 `deck-sources`。图片走受控 page_plan.image，不通过 CSS URL 加载。指纹不符、越出当前任务、资源 URL、脚本注入或不支持的样式被拒绝；细节见 Huashu 的 `references/harness-deck.md`。不带样式仍保留原模板。

已有品牌调性、方向或自主选择授权直接复用，不强制重选三版。样式或忠实的画面概括改动也创建新版本，重新检查最终 HTML、独立检核及策略师确认；涉及策略、事实或结论变化时先回逐字稿。正式 HTML 确认后可用 `scripts/deck_export.py` 生成绑定来源的 PDF；导出成功仍是待视觉检核的派生副本，需实际逐页回读及独立观察。原生门禁不自动审批 PDF，不支持把任意 Huashu 多文件或 PPTX 冒充已受控成品。

## PPT 独立检核与确认

先用 `deck-sources` 取得当前目标、完整来源清单和必查范围，再由主控显式调用不同的 fresh independent-reviewer 读取全部原文、实际 HTML、逐字稿与上游并逐页核对。角色 TOML 不会自动 spawn，作者不能改角色名自审。报告字段为 `target/reviewer_instance/status/simulation/scope/assessment/findings/uncovered/checked_sources`。通过报告必须覆盖 `cover`、全部章节编号、`display/navigation/editing/version_binding`，新版页级方案还须覆盖 `content_fidelity`；每条问题除 `level` 外写 `location/requirement/evidence/impact/action/decision_owner` 六项非空文字。`checked_sources` 覆盖 sources 清单中的全部实际文件与完整原文，不只登记成品。

必须实际检查显示、左右翻页、点击导航、文本编辑、草稿导出及离线重开；未做的写入 `uncovered`，不能报全部通过。报告还必须填写共用检核要求的 `assessment`：`task_fulfilment`、`evidence_reasoning`、`counterevidence`、`role_boundary`、`uncertainties`。生成成功和语法检查不是独立检核。PPT 自己通过后仍需策略师单独提供内部确认原文，再调用 `confirm`；逐字稿的确认不能替代 PPT 确认。正式使用前调用 `gate(..., human=True)`。

通过报告还必须有下列实际证据，不能用 `evidence` 字符串代替每页的 `file` 对象：

```json
{
  "browser_checks": {
    "display": true, "navigation": true, "editing": true,
    "export_reopen": true, "offline": true,
    "evidence": {"path": "project/reviews/browser/实际运行编号/browser-result.json", "sha256": "实际文件指纹"}
  },
  "visual_evidence": [
    {"page": 1, "rendered": true, "observation": "该页实际画面观察",
     "file": {"path": "project/reviews/visual/deck-page-001.png", "sha256": "实际图片指纹"},
     "method": "实际浏览器打开并截图的方法及环境",
     "content_fidelity": "逐页对照引用和讲者稿，说明提炼是否保留结论和限定条件",
     "media_observation": "核对图表单位、数值、比例及图片用途；无媒体时注明"}
  ]
}
```

这是格式占位。必须先由主控运行 `deck-browser-check` 实际启动Mac Chrome，成功后自动追加 `browser_completed` 事件；使用已安装Node/Playwright/Chrome，缺依赖就保留未完成状态。把命令返回的 `result` 原样填入 `browser_checks.evidence`，逐页查看本次结果中的桌面及窄屏截图，`visual_evidence.file` 引用对应页截图并写实际观察。每个页面须有 `page/rendered/observation/method/file`；图片可解码、至少100x100、非纯色，不同页不能复用同一图片指纹。执行结果同时绑定当前HTML、执行器、导出副本和无缓存重开证据。手写JSON、旧格式检查或任意图片不能代替该命令；旧报告保留但须补跑、重审和重新确认。红灯或未覆盖不能通过，黄灯只能附黄灯通过。机器记录不替代业务检核，也不对能改动全部本地文件的人提供防篡改认证。

`内部确认门禁.json` 为 `{"human":true}`。退回用现有 `reject --key <task_id>::html-deck` 并提供策略师原话。演示稿确认后主控才可用 `task-complete` 完成内部提案，前置品牌屋任务的完成记录及 HTML 检核仍须有效；该接口不代客户确认、产品战略或包装交付。`status --refresh` 刷新任务、产出及设计状态，`impact-scan` 同时列设计提交。

## 浏览器限制

HTML 零网络依赖，可本地打开；静态页面展示的是生成时的版本信息，无法自行查询磁盘版本库，正式状态以主控重新调用接口为准。文字编辑仅产生未确认草稿，`localStorage` 只是辅助缓存，缓存被禁用或满时明确提示。导出会把编辑记录写入独立 HTML 副本，可在没有原缓存的浏览器恢复；下载发起不等于下载成功，更不等于原文件写回。

本模块不提供直接写回原文件、浏览器草稿自动登记或策略自动同步，也没有品牌屋 HTML 那种文件内历史编辑能力。文字调整先交主控判断：仅忠实画面概括变化可修订 page_plan 并重检演示稿；策略意思改变先回逐字稿流程。不要覆盖已登记文件或修改历史。`inspect` 不读取浏览器缓存，也不声称完成浏览器显示验收。浏览器兼容性、下载结果、无缓存重开及真实显示效果需要主控记录实测环境；不承诺未测浏览器、演讲者窗口或原生 PowerPoint 文件。设计稿策略检核使用 design-expression-review；外部资料使用 knowledge-connectors 并核实账号与授权，不把本地检查当客户批准或返工改善证据。

## 执行契约

- **触发**：完整逐字稿已经独立检核并由策略师确认，且需要制作或重新对齐本地 HTML 演示稿时启动；只有改样式或查看单页时不绕过逐字稿门禁。
- **开始**：先核对逐字稿和三个 phase3 上游的版本、指纹、检核和确认，再检查 Mac 浏览器运行条件；任何旧依赖只能标记为待对齐。
- **产出**：离线 HTML 当前稿、不可覆盖的历史、逐页原文映射、实际浏览器检查证据、独立 PPT 检核报告和单独的策略师确认记录。
- **停止**：逐字稿或上游失效、浏览器未实际运行、页面未逐页检查、导出/离线重开未验证或来源映射不完整时停止放行；生成成功不等于提报稿确认，更不等于客户确认。
- **交接**：通过独立检核后交策略师单独确认，再由主控更新任务状态；逐字稿、品牌屋或推导变化时回到版本影响检查，不从旧 PPT 直接修正文案。
