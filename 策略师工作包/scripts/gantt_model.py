"""Semantic task model and the explicit baseline Excel adapter."""
import re

ADAPTER_VERSION = "0.2.1"
COMMON = {
    "client": "客户", "brand": "品牌", "contract": "合同", "amount": "合同金额（元）",
    "signed_date": "合同签约时间", "complete_date": "合同完成时间", "fee_note": "合同费用备注",
    "service_scope": "合同服务边界", "service_archive_url": "合同服务列项归档地址",
    "contract_url": "合同文件地址", "business_status": "合同商务状态",
    "contract_manager": "合同主项目经理", "assistant_manager": "合同助理项目经理",
    "project": "项目", "project_type": "项目类型", "project_status": "项目状态",
    "company": "负责公司", "background": "背景问题/目标界定",
}
TASK = {
    "title": "任务", "task_type": "任务类型", "status": "任务状态", "start": "任务开始",
    "start_half": "任务开始半天", "end": "任务截止", "end_half": "任务截止半天",
    "executors": "执行者（可多人）", "level": "任务等级", "deliverable": "交付物类型",
    "acceptance": "验收标准", "description": "任务说明", "planner": "策划负责人",
    "designer": "设计负责人", "strategy_director": "策略总监",
    "creative_director": "创意总监", "approver": "最终拍板人",
}
PROJECT_FIELDS = {"project", "project_type", "project_status", "company", "background"}
NODES = (
    ("research", "调研分析", "调研分析", "调研分析报告", "资料来源可追溯，事实和假设分开，研究结论完成内部检核"),
    ("draft", "品牌屋初稿", "品牌策略", "品牌屋正文与推导说明", "整体内容完成内部确认，不以生成首稿替代"),
    ("proposal", "内部提案", "品牌策略", "提案逐字稿与提报PPT", "逐字稿确认后制作PPT，PPT完成内部提报确认"),
    ("client", "客户确认", "品牌策略", "客户确认记录", "记录客户实际确认或反馈，不以提报结束替代确认"),
)
KINDS = {"product_strategy": "产品策略", "brand_design": "品牌设计", "packaging": "品牌设计"}


def people(value):
    if value is None:
        return []
    if not isinstance(value, (str, list)):
        raise ValueError("人员字段必须为姓名字符串或姓名列表")
    inputs = value if isinstance(value, list) else [value]
    if not all(isinstance(p, str) for p in inputs):
        raise ValueError("人员字段必须为姓名字符串或姓名列表")
    parts = [part for entry in inputs for part in re.split(r"[；;,，、\n]+", entry)]
    return list(dict.fromkeys(p.strip() for p in parts if p.strip()))


def to_row(common, task):
    row = {label: common.get(key, "") for key, label in COMMON.items()}
    row.update({label: task.get(key, "") for key, label in TASK.items()})
    row["执行者（可多人）"] = "；".join(people(task.get("executors")))
    for label in ("合同主项目经理", "合同助理项目经理", "策划负责人", "设计负责人",
                  "策略总监", "创意总监", "最终拍板人"):
        row[label] = "；".join(people(row[label]))
    note = task.get("date_basis", "")
    row["任务说明"] = (str(row["任务说明"]) + "\n排期依据：" + str(note)).strip()
    return row


def project_common(plan, task):
    return {**plan.get("common", {}), **plan.get("projects", {}).get(task.get("project_key"), {})}


def skeleton(items):
    tasks = []
    for item in items:
        if item["disposition"] != "included":
            continue
        iid = item["item_id"]
        if item["service_kind"] == "brand_house":
            previous = None
            for code, title, task_type, deliverable, acceptance in NODES:
                tid = f"{iid}-{code}"
                tasks.append({"task_id": tid, "source_item_ids": [iid], "title": title,
                              "task_type": task_type, "dependencies": [previous] if previous else [],
                              "deliverable": deliverable, "acceptance": acceptance})
                previous = tid
        else:
            tasks.append({"task_id": f"{iid}-delivery", "source_item_ids": [iid],
                          "title": item["title"], "task_type": KINDS.get(item["service_kind"], ""),
                          "dependencies": [], "deliverable": "", "acceptance": "",
                          "execution_route": "人工或外部执行" if item["service_kind"] == "product_strategy" else "待明确"})
    return tasks
