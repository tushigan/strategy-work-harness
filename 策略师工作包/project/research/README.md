# 调研与证据目录

这里是调研计划、来源原文和证据稿的空白模板说明，不放真实客户内容。

研究计划、证据稿和分析稿由统一入口 `scripts/phase3.py` 调用 `phase3_store.paths`，按已有 `task_id` 和上游简报版本写入项目相对路径：

`project/research/<task_id>/research-plan/`

`project/research/<task_id>/evidence/`

`project/research/<task_id>/analysis/`

每个目录包含 `current.json`、同名 Markdown 和 `versions/`。来源原件归档在 `project/inputs/evidence/`，完整提取记录放在：

`project/research/sources/<source_id>/`

每个来源必须保留标题、归属、日期、出处、适用范围、文件路径、版本指纹和完整原文定位。定位可使用页码、表名/单元格、行号、访谈时间点或外部出版信息。长资料不得静默截断；提取不完整时不能进入正式证据。

证据条目区分 `fact`、`opinion`、`inference`、`hypothesis` 和 `future_capability`。三层来源只是组织方式，不是真理排序。矛盾资料并列保留，并写反证、限制和未决部分，不用多数投票。正文章节和证据字段以 `phase3_document.SECTIONS`、`phase3_sources.verify_entry` 为准，不在 README 另写一套格式。

品牌考古访谈、工厂/供应链、终端和经销商采访与观察由人线下完成；无法可靠读取语音时先提供转写。跨项目资料只能标记为方法参考，不能成为当前项目事实。

正式证据和分析由不同 fresh 实例独立检核，最多自动业务修订两轮；之后保留版本和反馈，交策略师人工返工及线下客户确认。所有引用使用项目相对路径。本阶段不做 HTML、逐字稿、PPT、实际设计稿检核或云端连接。
