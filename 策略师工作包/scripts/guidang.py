#!/usr/bin/env python3
"""旧稿归档（U13）：把不再使用的旧版本移到项目目录同级的“<项目目录名>-归档/”，腾出工作区。

每次归档须有策略师当次明确授权原话；当前版本正在使用的文件拒绝归档。独立任务旧成品版本的自动移出（W02，
banben_baoliu.py）复用本程序，依据记为“W02 保留规则”（用户 2026-10-06 确认），不逐次询问。记录保留原路径、指纹与去向，
历史核对把这些引用判为“已归档”而不是缺失。不删除：文件只是搬到工作区外，可按记录搬回。"""
import argparse
import hashlib
import json
import os
import shutil
import unicodedata
from pathlib import Path

from phase2_store import WorkflowError, local, now
from standalone_store import text
from workspace_lock import serialized

LOG = "project/records/archive.jsonl"
PROTECTED = ("project/records", "project/运行日志", "project/state.json", "project/inputs/登记请求")


def destination_root(root):
    root = Path(root).resolve()
    return root.parent / f"{root.name}-归档"


def archived_index(root):
    path = Path(root) / LOG
    if not path.is_file():
        return {}
    result = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("event") in {"archived", "restored"} and not {"path", "sha256"} <= set(row):
                    raise ValueError("字段不完整")
            except ValueError as exc:
                raise WorkflowError(f"归档记录 {LOG} 第 {number} 行损坏（{exc}），未修改原件；请核对该行") from None
            if row.get("event") == "archived":
                result[(row["path"], row["sha256"])] = row["destination"]
            elif row.get("event") == "restored":
                result.pop((row["path"], row["sha256"]), None)
    return result


def archived_copy_issue(root, key, destination):
    """“已归档”要有实物：归档件须存在且指纹与记录一致，否则返回问题说明。"""
    target = Path(root).resolve().parent / destination
    if target.is_symlink() or not target.is_file():
        return f"归档件缺失：{destination}"
    if hashlib.sha256(target.read_bytes()).hexdigest() != key[1]:
        return f"归档件与记录指纹不符：{destination}"
    return None


def _safe_destination(root):
    """归档目的地（项目同级“<目录名>-归档/”）不能是符号链接，避免写到别处。"""
    dest = destination_root(root)
    if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
        raise WorkflowError(f"{dest.name}/ 是符号链接或不是目录，拒绝归档（避免写到工作区以外的别处）")
    return dest


def _current(root):
    """当前版本正在使用的文件：独立任务当前来源/交付物、未结束分派的输入与候选、正式当前产物。"""
    from current_dependencies import current_references
    refs = {p for p, _ in current_references(root)}
    from agent_dispatch import inspect as dispatch_inspect
    for item in dispatch_inspect(root)["active"]:
        refs.update(r["path"] for key in ("input_files", "candidates") for r in item.get(key, []))
    return refs


def _fold(text):
    return unicodedata.normalize("NFC", text).casefold()


def _canonical(root, rel):
    """macOS 文件名不区分大小写、Unicode 形态可不同：逐级按目录里的真实名字还原（找不到就保留原写法）。"""
    current, parts = Path(root), []
    for part in Path(rel).parts:
        try:
            names = os.listdir(current)
        except OSError:
            names = []
        real = part if part in names else next((n for n in names if _fold(n) == _fold(part)), part)
        parts.append(real); current = current / real
    return "/".join(parts)


def _protected_ids(root):
    """受保护实体（记录、运行日志、状态文件、登记请求）的 (设备, inode)：不论写法、硬链接，都按真实文件核对。"""
    ids = set()
    for rel in PROTECTED:
        try:
            st = os.stat(Path(root) / rel)
            ids.add((st.st_dev, st.st_ino))
        except OSError:
            pass
    return ids


def _hits_protected(root, path, ids):
    for item in [path, *path.parents]:
        if not item.is_relative_to(root):
            break
        try:
            st = os.stat(item)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in ids:
            return True
    return False


def _normalized(root, value):
    """先解析（local：拒绝绝对路径与越界）再判断：./、..、重复斜杠、大小写、Unicode 形态都归一后才比对保护范围，
    并按 (设备, inode) 与受保护实体核对；路径上任何一级是符号链接都拒绝。"""
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError("--path 不能为空")
    path = local(root, value)
    rel = _canonical(root, path.relative_to(root).as_posix())
    path = root / rel
    current = root
    for part in Path(value).parts:
        if part in {".", ""}:
            continue
        current = current / part
        if current.is_symlink():
            raise WorkflowError(f"{value}：路径经过符号链接，不能归档")
    folded = _fold(rel)
    if not folded.startswith("project/") or any(folded == _fold(p) or folded.startswith(_fold(p) + "/") for p in PROTECTED) \
            or _hits_protected(root, path, _protected_ids(root)):
        raise WorkflowError(f"{value}（解析为 {rel}）：只能归档 project/ 下的成果或资料，不能归档记录、日志或状态文件")
    return path, rel


def _expand(root, values):
    files = []
    for value in values:
        path, rel = _normalized(root, value)
        if path.is_dir():
            ids = _protected_ids(root)
            for p in sorted(path.rglob("*")):
                if p.is_symlink():
                    raise WorkflowError(f"{rel} 下有符号链接 {p.relative_to(root).as_posix()}，不能归档")
                if p.is_file():
                    if _hits_protected(root, p, ids):
                        raise WorkflowError(f"{rel} 下的 {p.relative_to(root).as_posix()} 是记录、日志或状态文件（硬链接），不能归档")
                    files.append(p)
        elif path.is_file():
            files.append(path)
        else:
            raise WorkflowError(f"{value}：不存在")
    return files


def _precheck(root, dest, plan):
    """移动前整批核对：目的地是符号链接或越界、目标路径上有符号链接、同名不同内容、来源变了，任何一项都整批不移。"""
    if dest.is_symlink():
        raise WorkflowError(f"归档目录 {dest.name}/ 是符号链接，整批未移动")
    problems = []
    for item in plan:
        source = root / item["path"]; target = root.parent / item["destination"]
        probe = target
        while not probe.exists() and probe != dest and probe.parent != probe:
            probe = probe.parent
        if probe.is_symlink() or not probe.resolve().is_relative_to(dest.resolve()):
            problems.append(f"{item['destination']}：目标越出 {dest.name}/ 或经过符号链接")
        elif target.is_symlink() or (target.exists() and (not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"])):
            problems.append(f"{item['destination']}：归档处已有不同内容的同名文件")
        if source.is_symlink() or not source.is_file():
            problems.append(f"{item['path']}：来源已不是普通文件")
    if problems:
        raise WorkflowError("移动前核对未通过，整批未移动：" + "；".join(problems[:5]))


def _plan(root, values):
    """归档计划：目的地、路径解析与保护范围、展开目录、正在使用的文件；任何一项不符即抛出（不移动）。"""
    dest = _safe_destination(root)
    files = _expand(root, values); busy = _current(root)
    busy = {_fold(b) for b in busy}
    in_use = [p.relative_to(root).as_posix() for p in files if _fold(p.relative_to(root).as_posix()) in busy]
    if in_use:
        raise WorkflowError("这些文件是当前版本正在使用的来源/交付物/分派文件，不能归档：" + "、".join(in_use[:10]))
    plan = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        plan.append({"path": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size,
                     "destination": f"{root.name}-归档/{rel}"})
    return dest, plan


def preflight(root, values):
    """v1.7.2 F03：与 archive 移动前完全相同的核对（计划 + 整批预检），只核对不移动；W02 dry-run 用它预演，报错原文与实际执行一致。"""
    root = Path(root).resolve()
    dest, plan = _plan(root, values)
    _precheck(root, dest, plan)
    return plan


def archive(root, values, words, actor, dry_run=False, basis=None, extra=None):
    from registration_guard import actor_check
    actor_check(actor)
    if not isinstance(words, str) or not words.strip():
        raise WorkflowError("归档须有策略师当次明确授权原话（--words），未授权不执行")
    text(words, "归档授权原话")
    root = Path(root).resolve()
    with serialized(root):
        dest, plan = _plan(root, values)
        summary = {"files": len(plan), "bytes": sum(x["bytes"] for x in plan), "destination_root": f"{root.name}-归档/"}
        if dry_run:
            return {"status": "dry_run", **summary, "items": plan[:50]}
        _precheck(root, dest, plan)  # 整批先查（冲突、越界、符号链接），有问题一个都不移
        log = local(root, LOG); log.parent.mkdir(parents=True, exist_ok=True)
        moved = []
        for item in plan:
            source = root / item["path"]; target = root.parent / item["destination"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.parent.resolve().is_relative_to(dest.resolve()) or dest.is_symlink():
                raise WorkflowError(f"归档目标越出 {dest.name}/（目录被换成符号链接？），未移动：{item['path']}")
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
                    raise WorkflowError(f"归档目录已有不同内容的同名文件，未覆盖：{item['destination']}")
            else:
                shutil.copy2(source, target)
                if hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
                    target.unlink(); raise WorkflowError(f"复制后指纹不符，未移动：{item['path']}")
            row = {"schema_version": 1, "event": "archived", **item, "words": words, "actor": actor, "created_at": now(),
                   **({"basis": basis} if basis else {}), **(extra or {})}
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n"); stream.flush(); os.fsync(stream.fileno())
            source.unlink(); moved.append(item["path"])
        for folder in sorted({(root / p).parent for p in moved}, key=lambda p: -len(p.parts)):
            try:
                while folder != root / "project" and not any(folder.iterdir()):
                    folder.rmdir(); folder = folder.parent
            except OSError:
                pass
        return {"status": "archived", **summary, "record_path": LOG,
                "notice": "已移到工作区外；记录保留原路径与指纹，历史核对显示“已归档”。需要时按记录搬回原位"}


def main():
    parser = argparse.ArgumentParser(description="旧稿归档到项目同级“<项目目录名>-归档/”，须策略师当次授权")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--path", action="append", required=True)
    parser.add_argument("--words", default="")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    print(json.dumps(archive(args.workspace, args.path, args.words, args.actor, args.dry_run), ensure_ascii=False, indent=2))
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
