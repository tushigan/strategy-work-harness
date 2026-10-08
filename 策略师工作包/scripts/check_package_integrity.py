"""检查统一包双宿主入口、资源一致性与可解析配置，不调用模型或外部服务。"""
import argparse
import json
from pathlib import Path
import tomllib
from runtime_structure import AGENTS, SKILLS
from sync_hosts import check as check_sync

HOST_FILES = (
    "AGENTS.md", "CLAUDE.md", ".codex/config.toml", ".codex/hooks.json",
    ".codex/hooks/validate-project.sh", ".claude/settings.json", ".claude/hooks/validate-project.sh",
    *(f".agents/skills/{name}/SKILL.md" for name in SKILLS),
    *(f".claude/skills/{name}/SKILL.md" for name in SKILLS),
    *(f".codex/agents/{name}.toml" for name in AGENTS),
    *(f".claude/agents/{name}.md" for name in AGENTS),
)

def check(root):
    root = Path(root)
    missing = [name for name in HOST_FILES if not (root / name).is_file()]
    errors = ["缺失必要文件: " + name for name in missing]
    for name in HOST_FILES:
        p = root / name
        if p.is_symlink():
            errors.append("入口不能使用符号链接: " + name)
        if p.is_file() and p.suffix in {".json", ".toml"}:
            try:
                text = p.read_text(encoding="utf-8")
                (json.loads if p.suffix == ".json" else tomllib.loads)(text)
            except (OSError, ValueError, UnicodeError):
                errors.append("配置无法解析: " + name)
    errors.extend(check_sync(root))
    return {"status": "fail" if errors else "pass", "missing": missing,
            "errors": errors, "skills_per_host": len(SKILLS), "roles_per_host": len(AGENTS),
            "messages": ["请复制整个文件夹，保留 .agents、.codex、.claude 三个隐藏目录。"] if errors else ["两边入口完整，技能与角色内容一致。"]}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", type=Path, default=Path("."))
    p.add_argument("--json", action="store_true")
    a = p.parse_args(); result = check(a.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2) if a.json else "\n".join(result["messages"] + result["errors"]))
    return int(result["status"] != "pass")

if __name__ == "__main__":
    raise SystemExit(main())
