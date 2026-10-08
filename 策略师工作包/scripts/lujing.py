#!/usr/bin/env python3
"""产物路径规范（U10）：把生图结果等过程 JSON 里的本机绝对路径改成工作包内相对路径。

只改尚未登记的过程文件；已被记录引用的文件不就地改（复制为新版后再登记）。工作包外的路径无法相对化，只报告。"""
import argparse
import json
from pathlib import Path

from phase2_store import WorkflowError, atomic_json, local


def _walk(value, root, changed, outside):
    if isinstance(value, dict):
        return {k: _walk(v, root, changed, outside) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, root, changed, outside) for v in value]
    if isinstance(value, str) and (value.startswith("/") or value.startswith("~/")):
        target = Path(value).expanduser()
        try:
            rel = target.resolve(strict=False).relative_to(root).as_posix()
        except ValueError:
            outside.append(value); return value
        changed.append({"from": value, "to": rel}); return rel
    return value


def relativize(root, file):
    root = Path(root).resolve()
    # 符号链接按用户给的写法逐级检查（解析之后链接已经消失，再查就形同虚设）
    current = root
    for part in Path(file).parts:
        if part in {".", ""}:
            continue
        current = current.parent if part == ".." else current / part
        if part != ".." and current.is_symlink():
            raise WorkflowError(f"{file} 经过符号链接，不就地修改")
    path = local(root, file)
    if path.suffix.lower() != ".json" or not path.is_file():
        raise WorkflowError("只处理项目内已存在的 JSON 过程文件")
    rel = path.relative_to(root).as_posix()  # 先解析再判断：./project/…、project/x/../… 都归一
    import hashlib
    fingerprint = hashlib.sha256(path.read_bytes()).hexdigest()
    records = root / "project/records"
    for record in sorted(p for p in records.rglob("*") if p.suffix in {".json", ".jsonl"} and p.is_file()):
        try:
            content = record.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if json.dumps(rel, ensure_ascii=False) in content or f'"{rel}"' in content or fingerprint in content:
            raise WorkflowError(f"{rel} 已被 {record.relative_to(root)} 引用（按规范化路径或内容指纹）；不能就地修改，请复制为新版后再登记")
    data = json.loads(path.read_text(encoding="utf-8")); changed, outside = [], []
    fixed = _walk(data, root, changed, outside)
    if changed:
        atomic_json(path, fixed)
    return {"file": rel, "changed": changed, "outside_workspace": outside,
            "status": "attention" if outside else "ok",
            "notice": "工作包外的路径无法相对化：先把文件归档进 project/ 再改引用" if outside else "已相对化"}


def main():
    parser = argparse.ArgumentParser(description="过程 JSON 绝对路径相对化")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("relativize"); r.add_argument("--workspace", type=Path, required=True); r.add_argument("--file", required=True)
    r.add_argument("--json", action="store_true")
    args = parser.parse_args()
    print(json.dumps(relativize(args.workspace, args.file), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    from yunxing_rizhi import cli
    _business_main = main

    def main():
        return cli(_business_main, __file__)

    try:
        raise SystemExit(main())
    except (WorkflowError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"未完成：{exc}") from None
