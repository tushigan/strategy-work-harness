"""Shared, read-only policy for the Mac strategist runtime package."""
import json
from pathlib import Path
from pathlib import PurePosixPath
import re
import tomllib

AGENTS = (
    "project-controller", "strategy-author", "proposal-author", "deck-builder",
    "design-expression-reviewer", "independent-reviewer",
)
SKILLS = (
    "project-control", "contract-schedule", "project-import-template", "task-brief",
    "research-evidence", "brand-house", "editable-brand-house", "proposal-script",
    "html-deck", "huashu-design", "design-expression-review", "independent-review", "version-impact",
    "knowledge-connectors",
)
MANUALS = (
    "策略与品牌屋运行说明.md", "品牌屋HTML运行说明.md",
    "提案与设计检核运行说明.md", "项目检核与结项运行说明.md",
)
REQUIRED_RUNTIME_FILES = (
    "scripts/runtime_structure.py", "scripts/project_handoff.py", "scripts/workspace_lock.py",
    "scripts/agent_dispatch.py", "scripts/standalone_store.py", "scripts/standalone_tasks.py",
    "scripts/task_context.py", "scripts/project_memory_store.py", "scripts/project_memory.py",
    "scripts/task_receipts.py", "scripts/standalone_review.py",
    ".agents/skills/README.md", *MANUALS,
    *(f".codex/agents/{name}.toml" for name in AGENTS),
    *(f".agents/skills/{name}/SKILL.md" for name in SKILLS),
)
STANDALONE_RUNTIME_FILES = (
    "scripts/standalone_store.py", "scripts/standalone_tasks.py",
)
PROJECT_MEMORY_RUNTIME_FILES = (
    "scripts/task_context.py", "scripts/project_memory_store.py",
    "scripts/project_memory.py", "scripts/task_receipts.py", "scripts/standalone_review.py",
)
BASE_RUNTIME_FILES = tuple(name for name in REQUIRED_RUNTIME_FILES
                           if name not in STANDALONE_RUNTIME_FILES + PROJECT_MEMORY_RUNTIME_FILES)
ROUTING_FILES = ("AGENTS.md", ".agents/skills/project-control/SKILL.md",
                 ".codex/agents/project-controller.toml")
DEVELOPMENT_ROOTS = {"tests", "outputs", "harness-template", "Product-Spec.md",
                     "Product-Spec-CHANGELOG.md", "DEV-PLAN.md", "Design-Brief.md"}
DEVELOPMENT_SKILLS = {"product-spec-builder", "design-brief-builder", "design-maker",
                      "dev-builder", "dev-planner", "bug-fixer", "code-review",
                      "release-builder", "evolution-engine", "goal-creator", "skill-builder"}
STALE_RULE = re.compile(
    r"第六阶段未开发|正在本地复验|后续未开发|开发教练|全栈开发|产品开发全流程|"
    r"Product-Spec|DEV-PLAN|dev-builder|code-reviewer|evolution-runner|"
    r"第[一二三四五六]阶段运行说明")


def instruction_file(name):
    path = PurePosixPath(name)
    return (name in ROUTING_FILES or name in {"README.md", ".agents/skills/README.md"}
            or (path.suffix == ".md" and len(path.parts) == 1)
            or (name.startswith(".agents/skills/") and path.name == "SKILL.md")
            or (name.startswith(".codex/agents/") and path.suffix == ".toml")
            or (name.startswith("project/") and path.name == "README.md"))


def required_runtime_files_for(names):
    """Keep historical release packages usable while making current packages complete."""
    names = set(names)
    required = list(BASE_RUNTIME_FILES)
    if names.intersection(STANDALONE_RUNTIME_FILES + PROJECT_MEMORY_RUNTIME_FILES):
        required.extend(STANDALONE_RUNTIME_FILES)
    if names.intersection(PROJECT_MEMORY_RUNTIME_FILES):
        required.extend(PROJECT_MEMORY_RUNTIME_FILES)
    return tuple(required)


def release_manifest_paths(root):
    path = Path(root) / "package-inventory.json"
    if not path.is_file() or path.is_symlink():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        inventory = value.get("inventory") if isinstance(value, dict) else None
        if not isinstance(inventory, list):
            return None
        names = {item.get("path") for item in inventory if isinstance(item, dict)}
        return names if all(isinstance(name, str) for name in names) else None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def runtime_requirements_for_root(root):
    manifest_paths = release_manifest_paths(root)
    return (required_runtime_files_for(manifest_paths)
            if manifest_paths is not None else REQUIRED_RUNTIME_FILES)


def startup_structure_errors(root):
    entries, texts, errors = {}, {}, []
    for path in root.rglob("*"):
        if "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        name = path.relative_to(root).as_posix()
        kind = "symlink" if path.is_symlink() else ("directory" if path.is_dir() else "file")
        entries[name] = {"kind": kind}
        if kind == "file" and instruction_file(name):
            try:
                texts[name] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                errors.append(f"工作规则无法读取: {name}")
    for problem in structure_issues(entries, texts, runtime_requirements_for_root(root)):
        errors.append(f"工作包结构错误 {problem['code']}: {problem['path']}")
    return errors


def structure_issues(entries, texts, required_runtime_files=REQUIRED_RUNTIME_FILES):
    """Inspect already-read names/text, without importing candidate code or following links."""
    problems = []

    def add(code, name, line=None):
        problem = {"code": code, "path": name}
        if line is not None:
            problem["line"] = line
        if problem not in problems:
            problems.append(problem)

    for name in required_runtime_files:
        if name not in entries or entries[name].get("kind") != "file":
            add("missing_runtime_file", name)
    allowed_agents = {f".codex/agents/{name}.toml" for name in AGENTS}
    for name in sorted(allowed_agents):
        try:
            role = tomllib.loads(texts.get(name, ""))
            if (role.get("name") != PurePosixPath(name).stem
                    or not isinstance(role.get("developer_instructions"), str)
                    or not role["developer_instructions"].strip()):
                add("invalid_role_definition", name)
        except (ValueError, TypeError):
            add("invalid_role_definition", name)
    for name, item in entries.items():
        if item.get("excluded"):
            continue
        path = PurePosixPath(name)
        parts = path.parts
        if not parts:
            continue
        if (parts[0] in DEVELOPMENT_ROOTS
                or (len(parts) == 1 and re.match(r"Phase-\d+-", parts[0]))
                or name == ".codex/evolution" or name.startswith(".codex/evolution/")
                or name == ".codex/EVOLUTION.md"
                or path.suffix.lower() == ".ps1"):
            add("development_or_windows_entry", name)
        if name.startswith(".codex/agents/") and name not in allowed_agents:
            add("unexpected_agent", name)
        if len(parts) >= 3 and parts[:2] == (".agents", "skills"):
            skill = parts[2]
            if skill in DEVELOPMENT_SKILLS or (
                    item.get("kind") == "directory" and len(parts) == 3 and skill not in SKILLS):
                add("unexpected_skill", name)
    for name, text in texts.items():
        if instruction_file(name):
            for number, line in enumerate(text.splitlines(), 1):
                if STALE_RULE.search(line):
                    add("stale_or_development_instruction", name, number)
    for name in ROUTING_FILES:
        text = texts.get(name, "")
        for role in AGENTS:
            if role not in text:
                add("missing_role_route", name)
    return problems
