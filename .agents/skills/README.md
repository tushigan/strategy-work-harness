# 项目内技能入口

本目录只放与当前工作包一起复制的规则入口，不复制全局技能的客户资料、凭据或未适配脚本。

- `project-control/SKILL.md`：总控如何读取状态、安排任务和记录交接。
- `independent-review/SKILL.md`：中立检核如何独立读取标准、证据和实际产出。
- `contract-schedule/SKILL.md`：合同列项、关键流程排期及人工确认。
- `project-import-template/SKILL.md`：绑定实际 Excel 模板、识别变更及导出。
- `task-brief/SKILL.md`：策略、设计和文案任务简报及策略转换清单。
- `research-evidence/SKILL.md`：采访准备、原文归档、证据冲突和研究分析。
- `brand-house/SKILL.md`：品牌屋正文、独立推导、先改稿再追问。
- `editable-brand-house/SKILL.md`：品牌屋 HTML 原文件保存、历史和正文对齐。
- `proposal-script/SKILL.md`：完整逐字稿、三个上游绑定、独立报告和单独内部确认。
- `html-deck/SKILL.md`：将已确认逐字稿组织成页面方案，分开画面与讲者文字，生成离线 HTML，逐页内容与视觉检核、草稿导出及单独确认。
- `huashu-design/SKILL.md`：基于已确认逐字稿准备页面方案、本地图片和视觉 CSS，经 `html-deck` 登记与检核后导出 PDF 待检副本；不改策略、不自审。
- `design-expression-review/SKILL.md`：按设计任务简报检查实际返回稿，逐页证据、区域和红黄绿依据。
- `version-impact/SKILL.md`：跨阶段影响扫描、历史保留及重新对齐。
- `knowledge-connectors/SKILL.md`：飞书/云端授权检索、项目归属、方法参考、精确授权回传和未知结果核对。

Codex 自然语言对话是主入口；结构及命令由主控执行，不要求策略师写 JSON。角色与流程见根目录 `AGENTS.md`，格式及边界见 `提案与设计检核运行说明.md`。角色 TOML 只定义职责，主控显式调 fresh 独立实例，不通过换角色名自审。

每个业务 Skill 都包含一段“执行契约”，统一说明五件事：什么情况触发、开始前读什么、必须产出什么、什么情况停止、交给谁继续。它是工作行为的最小共同格式，不代替各 Skill 的字段、命令和业务判断。通过脚本或结构检查只能证明入口和文件约束存在，不能代替策略师对内容、证据和客户结果的判断。

方法适配来源见 `方法来源.json`。不依赖源 Skill 的本机绝对路径，这些项目内规则随包复制。跨阶段及结项操作见根目录 `项目检核与结项运行说明.md`；外部适配不代表真实账号已接通，方法参考不提供外部操作权限。本地检查不证明客户批准或真实返工改善。

以上共 14 个业务 Skill，不包含开发技能。六个角色在 `.codex/agents/`：project-controller 总控，strategy-author 写策略内容，proposal-author 写逐字稿，deck-builder 制作演示稿，design-expression-reviewer 做设计诊断，independent-reviewer 检核所有正式产出。设计诊断须再由不同的中立实例核对原件与逐页证据，形成最终报告。`huashu-design` 样式必须接入 `html-deck` 的同一份正式 HTML，而非另产一个不受检核的视觉稿；PDF、自由多文件或 PPTX 都不自动继承 HTML 批准。
