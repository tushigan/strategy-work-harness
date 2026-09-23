---
name: design-expression-review
description: 策略师收到设计师返回的设计稿后，按对应设计简报检查策略是否被视觉准确承接时使用。
---

# 设计稿策略表达检核

## 依赖和边界

Codex 自然语言对话是主入口，例如“按这个设计任务的当前简报逐页检查返回稿，给出可定位的红黄绿意见”。主控登记稿件，显式调用未参与设计或提交的 fresh `design-expression-reviewer` 写诊断候选及逐页视觉证据，再调用不同的 fresh `independent-reviewer` 重新读取原件、简报、全部来源、逐页画面和诊断候选，独立形成最终报告。主控回读最终报告后登记，不能直接把诊断者自报登记为通过。角色 TOML 不会自动 spawn，作者、诊断者和提交者不能换角色名自审，不要求策略师写 JSON。本版只聚焦 Mac，完整步骤及格式见根目录 `提案与设计检核运行说明.md`，编排规则以 `AGENTS.md` 为准。

1. 完整读本任务当前且已独立检核的设计简报及全部原文，提交请求的 `brief` 必须是 `{space:"phase3",key,version,sha256}` 引用，不能使用 `{path,sha256}`。简报设计字段以 `scripts/phase3_document.py` 的 `DESIGN_FIELDS` 为准，缺关键检查项只能资料不足，不补造策略依据。
2. 策略师收到稿件后，将原件放入项目，再交主控 `design-submit`。请求可用 `author_instance` 标明实际作者，提交者用 `--actor`。设计师不需要账号、网页入口或加入工作包，归档不等于交付批准。
3. 支持 PNG/JPG/JPEG、PDF、PPTX。图片检查内容与后缀一致并真实解码，按一页处理；PDF 用 `pypdf`、PPTX 用 `python-pptx` 解析，只确定可读结构和页数，均不代表已渲染。旧 `.ppt` 归档为 `format:unsupported`，请人工转 PDF/PPTX；其他解析失败保存 `format:unreadable` 和 `parse_error`。
4. 脚本不能自动识别缺字体。只有实际打开或渲染后的视觉核查才能发现字体异常，发现无法可靠核查就写限制；不能把页数解析、文件存在或脚本成功当通过。未读页面、不可读稿件和未覆盖范围写入 `uncovered`。

现有 Agent 命令，从工作包根目录执行并替换实际任务及提交编号：

```bash
python scripts/phase5.py design-submit --workspace . --task ITEM-002-design --input project/inputs/设计返回稿.pdf --request project/author-input/设计提交请求.json --actor ACTUAL-SUBMITTER
python scripts/phase5.py design-sources --workspace . --submission DESIGN-ACTUAL-ID
python scripts/phase5.py design-review --workspace . --submission DESIGN-ACTUAL-ID --input project/reviews/设计检核.json --author ACTUAL-REVIEWER
python scripts/phase5.py design-gate --workspace . --submission DESIGN-ACTUAL-ID
python scripts/phase5.py design-status --workspace . --submission DESIGN-ACTUAL-ID
```

## 工作流程

主控先取得 `design-sources`，返回实际原件、完整简报引用、必查页面、全部来源和从简报提取的 `strategy_checks`。`CHECK-001` 等编号来自 `design.observable_checks`，不能自己发明依据。独立实例实际逐页看画面、写报告，脚本只校验绑定、覆盖及文件，不代写判断。

报告至少包含以下字段。`visual_evidence` 不是页数元数据：每一页都要有实际渲染或打开后的视觉记录，不能只凭 PDF/PPTX 解析成功就填写通过。

```json
{
  "target": {"space": "design-submission", "key": "提交编号", "sha256": "设计稿实际指纹"},
  "submission_id": "提交编号",
  "brief_ref": {"space": "phase3", "key": "ITEM-002-design::brief", "version": 1, "sha256": "简报实际指纹"},
  "reviewer_instance": "独立执行实例",
  "simulation": false,
  "scope": ["page 1"],
  "checked_pages": [1],
  "assessment": {
    "task_fulfilment": "任务完成度的实际判断",
    "evidence_reasoning": "策略依据及表达承接的实际判断",
    "counterevidence": "反证和误读风险的实际判断",
    "role_boundary": "策略检核与审美决定的职责判断",
    "uncertainties": "仍需验证的不确定性说明"
  },
  "visual_evidence": [
    {"page": 1, "rendered": true, "method": "实际打开或渲染方式及环境",
     "observation": "该页画面中实际观察到的策略表达",
     "file": {"path": "project/reviews/visual/design-page-001.png", "sha256": "实际图片指纹"}}
  ],
  "findings": [
    {"level": "green", "page": 1, "region": "正面主信息区", "basis_id": "CHECK-001",
     "location": "设计稿第1页正面主信息区", "requirement": "简报检查项的实际要求",
     "evidence": "该区域实际可观察证据", "impact": "对消费者理解的实际影响",
     "action": "保留或调整的具体建议", "decision_owner": "实际策略师"}
  ],
  "uncovered": [],
  "checked_sources": [
    {"path": "project/design-submissions/ITEM-002-design/实际提交文件.pdf", "sha256": "设计稿实际指纹"},
    {"path": "project/briefs/ITEM-002-design/brief/current.json", "sha256": "简报实际指纹"}
  ],
  "status": "passed"
}
```

这是单页格式占位，不是已完成检核；目标及指纹均从实际 sources 复制，`checked_sources` 必须完整替换为其全部已读原文清单，不能只用示例两条。通过须 `scope` 覆盖每个 `page N`，`checked_pages` 包含全部实际页码，`findings` 覆盖所有 `CHECK-xxx` 项，每条除六项非空文字外还需整数 `page`、具体 `region` 和有效 `basis_id`。

`visual_evidence` 每页含 `page/rendered:true/method/observation/file:{path,sha256}`，图片真实解码、指纹一致、至少100x100且非纯色。只填解析页数不算看过画面；未渲染、未读全页或不可读只能退回/资料不足。不可读且无可确认页码时用 `scope:["file readability"]`、`checked_pages:[]`、`visual_evidence:[]`、`findings:[]`，`uncovered` 说明具体限制，`checked_sources` 仍须覆盖原件及全部简报来源。真实项目 `simulation:false`，不得改 `test_mode` 放行。

## 红黄绿标准

- `red`：仅能引用 `strategy_checks` 中 `level:red` 的红清单项，并用具体区域证据说明违反策略或严重误读；不能把 `level:yellow` 依据升级为红灯。
- `yellow`：可以讨论、需要补证据或存在待验证风险，但仍可能是合理创意表达。黄色意见只能以 `passed_with_yellow` 通过。
- `green`：在实际检查范围内，设计对消费者利益、卖点/证据和信息优先级有合理承接。

个人喜好、构图风格、颜色、字体、版式和“好不好看”不能单独判红。该模块检核策略表达，不代替设计总监审美评审、印前生产检查或法规终审。存在红灯、未覆盖范围、不可读文件或不支持格式时不能通过；完整且无黄色意见才是 `passed`。

本接口没有 `design-confirm`，最终中立报告沿用现有 `design-review` 格式和命令，不新增审批接口。设计通过不等于内部提案完成、包装交付或客户批准。`status --refresh` 和 `impact-scan` 可列设计状态，上游简报更新使旧检核失效。需要外部资料时由主控使用 knowledge-connectors 并核实账号与授权，不把本地检核当真实返工改善证据。

## 执行契约

- **触发**：策略师收到设计师返回的实际设计稿，且当前设计任务简报已登记并能提供可观察的 `strategy_checks` 时启动。
- **开始**：先读取设计稿原件、简报、全部来源和红黄绿检查项；再按页实际打开或渲染，逐项判断策略是否被承接，先写诊断候选，再交不同实例做中立复核。
- **产出**：逐页视觉证据、按检查项定位的红黄绿意见、对消费者理解和卖点感知的影响、反证/不确定性以及明确由策略师决定的下一步；不得只写“好看/不好看”。
- **停止**：文件不可读、页面未实际查看、简报缺检查项、证据不能定位或把黄色风险升级成红灯时停止通过，保留未覆盖范围，不用页数解析和脚本成功代替视觉判断。
- **交接**：最终报告交策略师决定是否退回设计师；设计稿变化回到本 Skill 重新提交和检核，简报或上游策略变化先交 `version-impact`，不沿用旧报告。
