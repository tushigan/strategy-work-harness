"""作者派工默认预算（U18）的唯一来源：agent_dispatch 的默认预算与 sync_hosts 生成的 Claude 作者角色 maxTurns 都从这里取。
maxTurns 是宿主侧静态上限，extend 追加的预算不会改它（见运行环境与命令“重试与消耗上限”）。"""
AUTHOR_BUDGET = {"minutes": 60, "tool_calls": 80}
