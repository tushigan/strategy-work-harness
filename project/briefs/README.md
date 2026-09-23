# 任务简报目录

这里是任务简报的空白模板说明，不放真实客户内容。

正式简报由统一入口 `scripts/phase3.py` 调用 `phase3_store.paths`，根据 `project/tasks/task-plan.json` 中已有的 `task_id` 写入稳定目录：

`project/briefs/<task_id>/brief/{current.json,brief.md,versions/}`

同一任务的修订要建立新版本，保留旧版本、来源和检核记录。简报应绑定当前任务、合同或上游文件版本，并区分事实、判断、假设和缺口。

`phase3_document.SECTIONS["brief"]` 固定章节为：`problem`、`objectives`、`audience`、`scenario`、`confirmed_strategy`、`facts`、`judgments`、`assumptions`、`key_questions`、`deliverables`、`constraints`、`exclusions`、`acceptance`、`people_dates`、`missing_inputs`。支持的 `brief_type` 包括 `strategy`、`research`、`copywriting`、`brand_tone_upgrade`、`packaging`、`terminal_material` 和 `poster`。后四类还必须有完整的 `design` 字段和红黄等级的 `observable_checks`，只写策略师可观察的消费者利益、差异化卖点及证据、信息优先级、必留信息、绝对避免项、误读风险、可观察问题和自由发挥空间，不规定构图、色值、字体或视觉手法。

缺输入时，文件要明确写出不能做什么、仍可做什么和是否阻断完成。正式业务产出必须由不同 fresh 实例独立检核，最多自动业务修订两轮；人工返工和线下客户确认不由本目录替代。

本阶段不包含 HTML、逐字稿、PPT、实际设计稿检核或云端连接。所有引用使用项目相对路径。
