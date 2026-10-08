# 导出目录

本阶段 Excel 的唯一正式归档位置是 `project/outputs/versions/gantt/vNNNN.xlsx`，本目录不复制第二份，以免内容不一致。
`project/records/export-checks.jsonl` 记录模板指纹、计划版本、适配器版本、回读和显示素材；首次生成时 `system-imports.jsonl` 记录 `not_uploaded`，有后续真实人工上传记录时保留其结果，不因刷新或重跑检查重置。
只有 `phase2_review.py status gantt --workspace .` 显示 `ready_for_manual_upload` 才能交策略师人工上传。
生成文件、独立检核、策略师确认和系统实际导入是不同的步骤。模拟测试文件严禁上传。
