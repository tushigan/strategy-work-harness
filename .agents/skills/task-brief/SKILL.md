---
name: task-brief
description: 已有正式任务或独立任务需要启动、续聊，或为策略、研究、文案、包装和终端物料建立任务简报时使用。只读取既有 task_id，不新建任务或修改排期。
---

# 任务简报

## 依赖与边界

1. 先读 `AGENTS.md`、`project/state.json`，并用 `scripts/task_context.py inspect` 查明 `task_id` 属于正式计划还是独立任务；再读对应任务记录、当前项目记忆、上游版本和最近检核/确认记录。
2. 任务必须是已经登记的正式任务或独立任务。找不到、已取消、已归档或必要依赖未满足时，停止正式产出，并说明仍可做的准备工作。
3. 正式任务检查计划中的到期日、状态和 `dependencies`；独立任务检查 `standalone-tasks.jsonl` 中的原话、来源、当前版本、交付要求和下一步。只报告和使用现状，不重复建任务、不改日期、不替任务改状态。
4. 只使用当前项目资料和明确标注的合成资料。其他项目只能作为方法参考，不能带入客户名称、数字、认知或能力。
5. 本 Skill 只产出任务简报，不做设计稿检核、HTML、逐字稿、PPT、实际设计稿或云端连接。

## 规划和交付

主控先列本次简报要检查的输入、待补问题和完成标准，再开始写。正式计划任务通过 `scripts/phase3.py` 执行，文件由 `phase3_store.paths` 写入：`project/briefs/<task_id>/brief/{current.json,brief.md,versions/}`；同一任务修订建立新版本，保留旧版本和版本指纹，不覆盖历史。

独立任务不调用 `phase3.py`，也不强制补齐整套品牌战略章节。简报至少写明目标、来源、范围、排除项、交付物、验收标准、负责人/日期和缺口，保存为任务专属的新文件版本，再通过 `standalone_tasks.py` 登记为当前交付物；接受换版须显式使用 `accept_changed_references:true`。作者通过 `agent_dispatch.py` 接收任务，完成后由不同实例用 `standalone_review.py` 检查当前任务版本。进度登记、作者自检、独立检核、用户确认和合同完成分别记录。

正式计划任务的简报 `sections` 必须严格按 `phase3_document.SECTIONS["brief"]` 排列：`problem`、`objectives`、`audience`、`scenario`、`confirmed_strategy`、`facts`、`judgments`、`assumptions`、`key_questions`、`deliverables`、`constraints`、`exclusions`、`acceptance`、`people_dates`、`missing_inputs`。每章都要有非空标题、正文、`evidence`、`counterevidence`、`assumptions`、`verification` 列表；顶层还要有 `task_id`、`title`、`brief_type`、`mode`、`gaps`、`questions`。`mode` 只能是 `draft` 或 `hypothesis`，不能自行写成已确认。

正式简报必须包含以下内容，并明确哪些已确认、哪些仍待决定：

- 任务与合同来源：任务编号、合同服务列项或其他当前项目来源、来源版本。
- 问题和目标：分别写经营目标、策略目标、交付目标，不能混成一句“做升级”。
- 对象和场景：目标人群、购买/使用情境、渠道或接触场景；未知处写缺口。
- 已确认策略：只引用当前项目已确认的上游版本；候选方向写成候选。
- 事实、判断、假设：三者分开。假设必须同时写验证方式，不能用语气把猜测伪装成事实。
- 关键问题：最多五个能改变判断或交付的待确认问题；歧义先问，不替业务拍板。
- 交付物：文件类型、范围、版本、数量和不包含的内容。
- 约束与排除：时间、预算、渠道、法规、材料、品牌边界，以及明确不做的阶段。
- 验收：按可观察结果写，不用“高级、好看、专业”等空泛词。
- 负责人和时间：沿用任务计划已有安排；新增信息只能记为待确认，不擅自改排期。
- 上游版本和缺口：列出依赖文件、版本、指纹/记录位置，以及缺输入时“不能做什么”和“仍可做什么”。

## 策略到业务物料的转换

当 `brief_type` 为 `brand_tone_upgrade`、`packaging`、`terminal_material` 或 `poster` 时，顶层 `design` 必须具备 `phase3_document.DESIGN_FIELDS` 的全部字段：`consumer_benefit`、`differentiated_claim`、`claim_evidence`、`information_priority`、`mandatory_information`、`red_lines`、`misreading_risks`、`creative_freedom`、`observable_checks`。`observable_checks` 每项必须有 `question`、`basis` 和 `level`，等级只能是 `red` 或 `yellow`。

必须从策略师可观察的角度写转换清单：

- 消费者利益：消费者应感知、理解或获得什么，不写设计师的审美目标。
- 差异化卖点及证据：卖点相较什么不同，凭什么成立；没有证据就标为假设或待补证据。
- 信息优先级：第一眼、进一步了解和必须保留的信息分别是什么。
- 必留信息：品牌、品名、规格、法规或业务指定信息等，逐项列出来源。
- 绝对避免项：会违反策略、触碰业务红线或造成重大误读的表达。
- 误读风险：消费者可能把它理解成什么，如何观察和复核。
- 自由发挥空间：只说明策略不限制的部分，不规定构图、色值、字体、版式或视觉手法。
- 可观察检核问题：每条绑定依据，并标记 `red` 或 `yellow`。`red` 是违反策略、触碰绝对避免项或重大误读，必须回改；`yellow` 是可商榷、需要补证据或可能削弱策略，保留创意空间。

不要把“策略到视觉转换”写成设计总监任务，也不要提前检核实际设计稿。设计稿检核属于后续阶段。

## 方法参考与适配边界

本 Skill 只吸收以下全局方法的通用工作方式，不复制其客户案例、资产或结论：`brand-house-assistant`（字段层级、竞争参照、当前/目标状态 RTB、逐字段检核）；`strategy-director`（经营目标、策略判断和交付目标分层）；`copywriting-skill`（事实/判断/假设/提案分开，先判断办法再写说法）；`packaging-expression-strategy`（包装表达的 readiness 检查和信息优先级）。这些方法在本项目只用于生成策略师简报，不替代本项目 schema，也不扩展到设计稿制作或设计审美判断。

## 缺口、修订和检核

输入不足时，简报仍可保存草稿，但每个缺口都要写：问题、不能做什么、仍可做什么、是否阻断完成。不得用空白字段假装完成。

正式业务产出由不同的 fresh 实例独立检核；作者不能自审或把机器校验当业务通过。最多自动业务修订两轮，每轮后先独立复核；达到上限仍有争议时保留原反馈、新旧版本和对应原因，交策略师决定。内部返工不自动增加客户审批节点。

## 执行契约

- **触发**：已有正式任务或独立任务需要开始、续做、换执行者，或准备设计、策略、文案、包装和终端物料工作时触发。
- **开始**：先说清本任务要解决的问题、不能替它决定的事项和最少待补资料；最多提出五个会改变结果的问题。
- **产出**：简报必须能让执行者知道做什么、依据什么、交什么、什么算完成，以及哪些红线不能碰；设计类任务同时产出策略到视觉的可观察检查项。
- **停止**：任务不存在、已暂停/取消/归档、依赖未完成、关键策略没有依据或验收无法观察时，只保存草稿/假设稿，不把空白字段写成完成。
- **交接**：策略任务交 `research-evidence` 或 `brand-house`，提案任务交 `proposal-script`，设计返回稿交 `design-expression-review`。
