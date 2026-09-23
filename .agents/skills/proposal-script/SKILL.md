---
name: proposal-script
description: 本内部提案任务简报已检核、计划前置品牌屋正文及推导已检核并内部确认后，起草或修订完整逐字稿时使用。
---

# 提案逐字稿

## 入口和边界

Codex 自然语言对话是主入口，例如“继续这个内部提案任务，先核对前置确认，再起草可以直接念的完整逐字稿”。主控先读 `AGENTS.md`、项目状态、有效计划、任务记录、原文及最新检核/确认，安排 `proposal-author` 写候选稿，回读后用现有 CLI 登记，再交不同的 independent-reviewer 检核，不要求策略师写 JSON。具体结构、命令及报告示例见根目录 `提案与设计检核运行说明.md`，编排规则以 `AGENTS.md` 为准。本版只聚焦 Mac。

请求的 `phase3_refs` 必须恰好三个完整 `{space,key,version,sha256}` 引用：本任务 `brief`，计划前置任务中同一任务的 `brand-house` 和 `derivation`。`space` 为 `phase3`，版本为正整数，指纹为实际64位小写十六进制；不兼容 `dependencies`，不额外要求或接受 `summary` 替代三项。简报须当前且独立检核通过，正文和推导还须策略师内部确认，三份稿均无阻断缺口。

任务标题必须为“内部提案”，暂停、取消或已完成任务不能自动重启。主控用 `task-start` 检查计划、开始日、本任务简报及前置品牌屋任务完成记录，品牌屋前置还含有效 HTML 检核；未到开始日只能准备候选内容。`task-complete` 要求逐字稿和演示稿各有有效检核及内部确认，不由本技能凭简报完成客户确认、产品战略或包装。

现有 Agent 命令，均从工作包根目录执行，替换实际任务及实例：

```bash
python scripts/phase5.py script-publish --workspace . --input project/author-input/逐字稿.json --request project/author-input/逐字稿请求.json --author ACTUAL-AUTHOR
python scripts/phase5.py script-sources --workspace . --task ITEM-001-proposal
python scripts/phase5.py script-review --workspace . --task ITEM-001-proposal --input project/reviews/逐字稿检核.json
python scripts/phase5.py script-confirm --workspace . --task ITEM-001-proposal --input project/inputs/逐字稿确认原文.txt --actor 实际策略师
python scripts/phase5.py script-gate --workspace . --task ITEM-001-proposal --request project/author-input/内部确认门禁.json
```

`内部确认门禁.json` 为 `{"human":true}`；不传请求的 gate 只检查检核通过，不要求逐字稿自身内部确认。退回使用现有 `reject --key ITEM-001-proposal::proposal-script` 并提供策略师退回原文。`status --refresh` 更新当前项目缓存，`impact-scan` 同时列设计提交状态；空包不为示例执行这些写入。

## 章节结构

输入至少有 `task_id/title/audience/purpose/sections`。每章有唯一的 `id`、`title`、`spoken_text`、`transition`、非空 `claim_refs`。`spoken_text` 是可以现场直接讲的完整段落，不是标题、关键词或提纲；40个非空白字符只是机器底线，达到字数不证明稿件完整。`claim_refs` 必须是 `{key,section_id}` 对象，指向三个上游中实际存在的章节，不能只填证据编号或文字标签。

Markdown 会完整保留听众、提案目的、每章转场、逐字稿正文和论断来源，并列出上游的空间、键、版本和指纹。文件明确标注“提案稿不等于客户确认”。机器登记、语法通过或独立检核通过，都不能写成客户已经确认。

输出为 `project/outputs/proposal/<task_id>/proposal-script.json`、`.md`，历史在同目录 `versions/vNNNN.json`、`.md`。作者只写候选稿，主控登记版本，不直接改当前登记文件或快照。

## 修订和回退

首次生成不能带修订信息。修订请求必须提供：

```json
{
  "revision": {
    "base": {"space": "phase5", "key": "ITEM-001-proposal::proposal-script", "version": 1, "sha256": "上一版实际指纹"},
    "trigger": "review",
    "reason": "本次整体修订原因",
    "changes": ["opening：补充已确认策略的口语解释，保留原论断及证据边界"]
  }
}
```

以上为格式占位，不能直接当修订依据；完整请求仍需三个 `phase3_refs`。`base` 精确绑定上一版，`changes` 是非空文字列表，不能使用对象列表。新稿不继承旧检核或确认；同版新增报告也会使旧确认失效。已有修订时，下一轮前须有上一版有效独立检核。

初稿后自动修订最多两轮，第二轮后停下。策略师明确增加一轮后，主控用 `allow-more` 绑定上一版，将返回的 `record_id` 填下一版 `revision.allowance_id`；只增加一轮，不重置累计轮次。返回历史内容也必须绑定当前上一版、有效上游及原因生成新版，再检核并确认，旧演示稿随依赖变化待对齐。

## 独立检核和确认门禁

角色 TOML 不会自动 spawn。主控显式调用未参与这份产出任何版本撰写的 fresh 独立实例，不能作者换角色名自审。检核者完整读取 `script-sources` 的全部原文、JSON/Markdown、逐字稿章节与论断来源，长资料分批全文读，不以摘要替代。

报告有精确 `target/reviewer_instance/status/simulation/scope/assessment/findings/uncovered/checked_sources`。`assessment` 为 `task_fulfilment/evidence_reasoning/counterevidence/role_boundary/uncertainties` 五项非空实质说明。每条 `findings` 除 `level` 外还有 `location/requirement/evidence/impact/action/decision_owner` 六项非空文字。通过须覆盖全部章节及来源原文；有红灯或未覆盖范围不能通过，有黄灯只能 `passed_with_yellow`，不能用作者自报代替业务判断。

只有最新版本的独立检核通过后，才能进入策略师确认；`gate(..., human=True)` 会同时检查这两个条件。确认原文必须由实际策略师提供并保留，模拟确认只允许隔离测试项目。这里的确认是逐字稿内部确认，不是客户确认，也不能替代后续 PPT 自身的检核和确认。

本技能只处理项目内的逐字稿；需要外部资料时由主控使用 knowledge-connectors 并核实账号与授权。写入中断用现有 `recover` 检查，人工文件冲突保留双方并停止，不覆盖历史。本地生成和检核不代表真实客户批准或返工改善。

## 执行契约

- **触发**：品牌屋正文、推导和本次任务简报均已检核，且策略师已确认可以进入提案表达时启动；前置不齐只能整理候选，不得宣称正式逐字稿完成。
- **开始**：完整回读听众、提案目的、上游正文、证据边界和检核意见，先搭建“为什么这样判断—凭什么—对客户意味着什么”的演绎顺序，再写可直接念的段落和转场。
- **产出**：结构化逐字稿、可读 Markdown、章节与上游论断的绑定、修订原因和待确认点；每一章都必须是完整口语表达，不能用标题或提纲冒充。
- **停止**：上游指纹不一致、论断无法回查、前置确认缺失、章节来源或听众目的不清时停止生成正式稿；不为了顺畅而补造事实，不把独立检核或内部确认写成客户确认。
- **交接**：独立检核通过后交策略师进行线下内部确认；确认后才交 `html-deck`。逐字稿回退或上游变化会使演示稿进入待对齐，不沿用旧确认。
