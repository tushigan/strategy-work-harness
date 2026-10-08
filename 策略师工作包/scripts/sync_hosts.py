"""从共同技能与角色源生成 Claude 入口；拒绝覆盖单独修改的派生文件。"""
import argparse
import hashlib
import json
from pathlib import Path
import tomllib

from author_budget import AUTHOR_BUDGET

MANIFEST = ".claude/宿主同步清单.json"
AUTHOR_MAX_TURNS = AUTHOR_BUDGET["tool_calls"]

def digest(data):
    return hashlib.sha256(data).hexdigest()

def expected(root):
    for name in (".agents", ".agents/skills", ".codex", ".codex/agents"):
        safe(root, name)
    result = {"CLAUDE.md": b"# \xe7\xad\x96\xe7\x95\xa5\xe5\xb8\x88\xe7\xbb\x9f\xe4\xb8\x80\xe5\xb7\xa5\xe4\xbd\x9c\xe5\x8c\x85\n\n@AGENTS.md\n"}
    for p in sorted((root / ".agents/skills").rglob("*")):
        if "__pycache__" in p.parts or p.name == ".DS_Store":
            continue
        if p.is_symlink():
            raise ValueError("技能源不能使用符号链接")
        if p.is_file():
            name = ".claude/skills/" + p.relative_to(root / ".agents/skills").as_posix()
            result[name] = p.read_bytes()
    for p in sorted((root / ".codex/agents").glob("*.toml")):
        if p.is_symlink():
            raise ValueError("角色源不能使用符号链接")
        d = tomllib.loads(p.read_text(encoding="utf-8"))
        if d.get("name") != p.stem or not d.get("description") or not d.get("developer_instructions"):
            raise ValueError("角色定义不完整")
        role_tools = "Read, Write, Bash, Glob, Grep" if p.stem in {"independent-reviewer", "design-expression-reviewer"} else "Read, Write, Edit, Bash, Glob, Grep"
        if p.stem == "project-controller":
            role_tools += ", TodoWrite, Agent"
        # Claude 子代理可用 maxTurns 硬性限制轮次（官方 frontmatter，2026-10-06 查证；未在真实宿主实测）。
        # 轮次不等于工具调用次数；时长与调用预算仍按派工规则与进度文件执行。Codex 无对应项。
        turns = f"\nmaxTurns: {AUTHOR_MAX_TURNS}" if p.stem in {"deck-builder", "strategy-author", "proposal-author"} else ""
        text = "---\nname: " + d["name"] + "\ndescription: " + json.dumps(d["description"], ensure_ascii=False) + "\ntools: " + role_tools + turns + "\n---\n\n" + d["developer_instructions"].strip() + "\n"
        result[f".claude/agents/{p.stem}.md"] = text.encode()
    return result

def actual_files(root):
    result = set()
    for directory in (".claude/skills", ".claude/agents"):
        for p in (root / directory).rglob("*"):
            if "__pycache__" not in p.parts and p.name != ".DS_Store" and (p.is_file() or p.is_symlink()):
                result.add(p.relative_to(root).as_posix())
    return result

def safe(root, name):
    path = root / name
    if any(p.is_symlink() for p in (path, *path.parents) if p != root.parent):
        raise ValueError("同步目录不能使用符号链接")
    return path

def check(root):
    root = Path(root).resolve()
    errors = []
    try:
        want = expected(root)
        for name, data in want.items():
            p = safe(root, name)
            if not p.is_file() or p.read_bytes() != data:
                errors.append("入口缺失或与共同源不一致: " + name)
        for name in sorted(actual_files(root) - want.keys()):
            errors.append("未登记的宿主副本: " + name)
        manifest = safe(root, MANIFEST)
        declared = json.loads(manifest.read_text())
        if declared != {k: digest(v) for k, v in want.items()}:
            errors.append("宿主同步清单与当前源不一致")
    except (OSError, ValueError, TypeError, KeyError):
        errors.append("统一入口或同步清单无法读取；从完整包恢复后重新检查")
    return errors

def sync(root):
    root = Path(root).resolve()
    want = expected(root)
    manifest = safe(root, MANIFEST)
    old = json.loads(manifest.read_text()) if manifest.is_file() else {}
    conflicts = []
    if not isinstance(old, dict):
        raise ValueError("同步清单格式错误")
    for name in actual_files(root) - want.keys():
        conflicts.append(name)
    # 先检查所有目标；发现冲突时不写任何派生文件。
    for name, data in want.items():
        p = safe(root, name)
        if p.exists() and (not p.is_file() or (p.read_bytes() != data and digest(p.read_bytes()) != old.get(name))):
            conflicts.append(name)
    if conflicts:
        raise ValueError("存在单独修改或未登记的副本，先合并回共同源: " + "、".join(sorted(set(conflicts))))
    for name, data in want.items():
        p = safe(root, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps({k: digest(v) for k, v in want.items()}, ensure_ascii=False, indent=2) + "\n")
    return len(want)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "sync"))
    parser.add_argument("--workspace", type=Path, default=Path("."))
    args = parser.parse_args()
    try:
        root = args.workspace.resolve()
        if args.command == "sync":
            print(f"已同步 {sync(root)} 个入口和资源文件")
        errors = check(root)
        if errors:
            print("\n".join(errors))
            return 1
        print("两边入口与共同源一致")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(str(exc))
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
