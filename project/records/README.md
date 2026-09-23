# 记录与版本

记录由工作包脚本生成，策略师通过自然语言交给 Agent 操作，不照通用示意手填批准事件。具体格式以根目录各业务运行说明及当前脚本为准。下列 phase2/phase3 等是固定存储名称，不是开发进度。

## 合同、计划与 Excel

- `phase2-artifacts.json`：版本、作者、文件指纹和依赖。
- `reviews.jsonl`、`confirmations.jsonl`：独立检核和人工确认，分别绑定 `target` 精确版本。
- `template-events.jsonl`、`export-checks.jsonl`：模板变更和实际文件检查。
- `phase2-decisions.jsonl`：增加自动修订额度等人工决定。
- `system-imports.jsonl`：实际项管系统上传结果。可上传不代表已经上传。

## 简报、研究与品牌屋

- `phase3-artifacts.json`：以 `task_id::kind` 标识的产出历史；正文及 Markdown 快照保存在各任务的 `versions/`。
- `phase3-sources.json`：原始资料版本、归属、提取定位和指纹；原件在 `project/inputs/evidence/`，全文提取在 `project/research/sources/`。
- `phase3-reviews.jsonl`：真实独立实例的报告及归档原件。报告绑定版本，作者不能自审。
- `phase3-confirmations.jsonl`：策略师内部确认或退回及原文，不代替客户确认。
- `phase3-tasks.jsonl`：既有计划任务的开始与完成，不另造一份任务计划。
- `feedback.jsonl`：原始反馈、转写、版本定位或待澄清状态；不清楚目标时不猜改。
- `phase3-decisions.jsonl`：对齐、增加修订额度或保留冲突后恢复等明确决定。
- `phase3-artifacts.json` 中的修订信息及对应差异文件保存新旧内容、修改原因、反馈关联；轮次从历史累计，不能通过换作者或改名称重置。
- `dependencies.json` 的 `phase3`：可重建的当前依赖视图，不替代版本历史。

JSONL 事件逻辑上只追加，通过原子替换保留全部旧行再写新事件；不得人工截断、清空或改写旧事件。`revision-counts.json` 是早期预留，不是清零修订次数的入口。

## HTML 品牌屋

`phase4-html.json` 保存 HTML 引用、指纹和对齐状态；完整展示内容及文件内历史在实际 HTML 内。`html-save-log.jsonl` 区分文件内事件、Agent 回读和浏览器回执，不能把读取到文件当成某次保存已经成功。规则见 `品牌屋HTML运行说明.md`。

## 逐字稿、演示稿和设计检核

以下文件仅在实际执行后由脚本产生，空包不预填。

- `phase5-artifacts.json`：`<task_id>::proposal-script`、`<task_id>::html-deck` 的版本、作者实例、当前文件、快照、依赖和修订原因。
- `phase5-events.jsonl`：`review`、`confirmation`、`allow_more`、`design_submission`、`design_review`、`task_started`、`task_completed` 事件。报告原件及确认/退回/追加授权原文归档在 `project/reviews/`。
- `design-submissions.json`：设计稿提交编号、作者及提交者实例、本任务简报版本引用、原件指纹、实际解析页数及策略检查项。解析页数不代表已经渲染。
- `phase5-pending.json`：尚未完成的本地写入交易，使用 `python scripts/phase5.py recover --workspace .` 检查恢复；遇到未登记人工修改保留冲突并停止，不覆盖。

逐字稿和演示稿分别报告、分别内部确认。`confirmation.review_id` 必须对应当前版本最新检核事件的 `record_id`；同版新增报告也会使旧确认失效，退回使用现有 `reject`。设计检核没有 `design-confirm` 命令，不把设计通过当成客户批准。

`revision.base` 精确绑定上一版，`changes` 为非空文字列表。初稿后自动修订最多两轮；第二轮后 `allow-more` 绑定上一版，返回的 `record_id` 填入下一版 `revision.allowance_id`，只增加一轮，累计轮次不重置。返回历史内容也须形成新版、重新检核和确认，不恢复旧批准。

报告及视觉证据的精确结构见 `提案与设计检核运行说明.md`：来源必须完整读取并逐一记录指纹，演示稿和设计稿需逐页图片证据；设计意见还需实际页码、区域及 `CHECK-xxx` 依据。设计诊断候选不直接登记为通过，须交不同的中立实例重新看原件和每页画面。截图存在不证明判断成立，主控仍须核读真实观察。

`task-start`、`task-complete` 只处理有效计划中的内部提案。完成事件绑定计划、本任务简报及逐字稿/演示稿当前引用，后两者均需最新有效内部确认；前置品牌屋任务的完成记录及 HTML 检核也须有效。新版、报告或上游改变会使旧完成依据失效，不凭简报完成客户确认、产品战略或包装。`status --refresh` 重算并写入 `state.json.phase5` 缓存，`impact-scan` 同时返回产出与设计状态；空包不为文档示例写入这些记录。主控回读后才能报告任务完成。

## 状态与移交

- `agent-dispatch.jsonl`：SubAgent 显式分派历史，绑定角色、任务原文、输入指纹、上游版本、当前任务引用、项目记忆版本、权限、完成标准、实际实例和回传候选。任务或项目记忆在分派期间变化会阻断旧分派继续，须重新准备。`returned_for_readback` 仅表示候选已交回；当前登记主控附回读证据后登记 `completed`，解除当前任务占用，但不代表独立检核或人工确认。`controller_taken_over` 记录新对话的真实接手者、前任和原主控、原因及证据，不改变原分派状态；历次接手证据也须保持原指纹。取消、接手、回读和再次分派均保留历史；无法调用真实独立实例不能登记 `assigned`。
- `project-identity.jsonl`：最小项目身份的追加历史。项目编号、项目名、客户名和品牌名可在合同计划前确认；这不代表合同已识别或项目已经立项。
- `project-memory.jsonl`：当前工作包的长期项目事实。每条保存项目编号、对象、主题、当前值、被替代值、范围、来源、原因、状态和修订号；项目编号必须与当前最小项目身份一致，换成另一项目身份后拒绝采用旧项目记录。`confirmed` 才进入新分派上下文，`needs_confirmation` 只提示，`retired` 保留历史。来源文件改变、日志损坏或并发版本冲突会阻断采用；不把损坏误报为无记忆。空包不预填客户事实，本地记忆也不会自动写入云端大脑。
- `task-receipts.jsonl`：任务操作回执、独立任务之间的版本关系及独立任务与正式计划的明确关联。成功回执必须带实际返回或回读证据；失败和结果未知分别保留。Brief→PPT 等关系绑定上游当前交付版本，上游换版会提示下游重新对齐。关联正式计划不自动复制完成状态或进入合同统计。
- `handoff-manifest.json`：最近一次项目交接清单入口，绑定当前阶段、下一步、待恢复事项、角色调用规则、外部依赖和工作区文件指纹；不可覆盖的历史正文在 `handoffs/<manifest_id>.json`。接收方必须绑定移交方提供的清单编号。该文件不把客户确认或独立检核变成已通过。
- `handoff-events.jsonl`：交接清单生成、接收或拒绝事件，只追加保存。接收方必须先回读同一批文件；发现文件列表或指纹变化时拒绝静默接收。

- `control-artifacts.json`：总控汇总、结项报告和人工交付产出的版本、作者及依赖。
- `phase6-events.jsonl`：总控检核、内部/客户决定、任务交付、影响扫描、结项及结项后变更。相同原话在相反决定之后再次提供属于新决定，不能沿用旧事件。
- `phase6-evidence/`：项目检核、交付、结项的实际报告及决定原文，按文件指纹归档。
- `connector-events.jsonl`：授权范围、来源散列、获取时间、外部读取/回传和回读状态。不保存凭据或他客正文，`pending/unknown` 不表示已同步。
- `package-materials.json` 只在合成移交试验中逐文件声明实际核对过的资料；不在空包预填，不替真实客户资料的分发授权。

人工任务交付记录须绑定当前简报及最新检核。所有签约列项实际交付、内部确认和客户确认均齐全后，才可登记独立检核及内部确认过的结项报告。任何变更保留原结项，实时检查可能返回 `change_pending`；执行刷新后再更新状态缓存，不覆盖过去。

任务完成、机器通过、内部确认、客户确认和系统导入互不替代。`state.json` 是缓存入口，继续工作前由状态命令依据真实文件和事件重算。新版、原文变化、人工编辑或报告失效都可能撤销当前可用状态，旧报告仍保留。

发现手动修改时保留双方，不直接覆盖；中断先运行 `python scripts/project_handoff.py inspect --workspace . --json`，恢复决定需绑定当时文件指纹。移交复制整个目录，包括隐藏配置、原文、版本和事件，不只传最终 Markdown。记录中不得含凭据、密码或开发机绝对路径。

## 独立任务

`standalone-tasks.jsonl` 仅在真实登记后创建，保存每次任务完整版本、原话、来源/交付物指纹、进度、下一步和更新原因。使用 `scripts/standalone_tasks.py`，不手填或改写历史。普通信息更新沿用原文件指纹；只有明确 `accept_changed_references:true` 才接受已经变化的文件为新版。新对话及交接从统一恢复报告 `workflow_snapshot.standalone` 读取；归档保留，损坏阻断交接。当前交付物由另一实例通过 `standalone_review.py` 绑定任务版本检核，报告归档到现有 Phase 6 证据记录；任务完成、独立检核、用户确认、客户批准和合同交付互不替代。
