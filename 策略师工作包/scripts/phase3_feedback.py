"""Keep original feedback, resolve unique targets, and never invent audio transcripts."""
import importlib.util
from pathlib import Path
import re
import shutil

from phase2_store import (WorkflowError, archive, local, read_json,
                         test_mode)
from phase3_events import append, events
from phase3_io import registry
from phase3_sources import file_valid
from phase3_store import ref

AUDIO = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".webm"}
TEXT = {".txt", ".md", ".srt", ".vtt"}


def capabilities():
    return {"text_formats": sorted(TEXT), "audio_archive_formats": sorted(AUDIO),
            "ffmpeg_found": bool(shutil.which("ffmpeg")),
            "whisper_cli_found": bool(shutil.which("whisper")),
            "whisper_library_found": importlib.util.find_spec("whisper") is not None,
            "automatic_transcription": False,
            "note": "工具或库存在不等于已验证模型/语言/说话人识别。本阶段不自动转写；保留音频并要求可靠转写稿。"}


def candidates(root, text, explicit=None):
    histories = registry(root)["artifacts"]
    if explicit:
        matches = [m for versions in histories.values() for m in versions if ref(m) == explicit]
        if len(matches) != 1:
            raise WorkflowError("指定反馈版本不存在")
        return matches
    task_ids = {m["task_id"] for history in histories.values() for m in history}
    mentioned = set(re.findall(r"(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9]*-\d+"
                               r"(?:-[A-Za-z0-9_-]+)*)(?![A-Za-z0-9_-])", text))
    keys = set(re.findall(r"([A-Za-z0-9_-]+::[A-Za-z-]+)", text))
    paths = set(re.findall(r"project/[^\s，。；？,;]+?\.(?:md|json)", text))
    known_paths = {m[field] for history in histories.values() for m in history
                   for field in ("path", "markdown_path", "snapshot_path", "markdown_snapshot")}
    if mentioned - task_ids or keys - set(histories) or paths - known_paths:
        return []
    pool = [history[-1] for history in histories.values()]
    named = [m for m in pool if m["key"] in text or m["markdown_path"] in text]
    if named:
        pool = named
    else:
        task_named = [m for m in pool if m["task_id"] in text]
        if task_named:
            pool = task_named
        hints = {"brand-house": ("品牌屋",), "brief": ("简报",), "analysis": ("研究分析", "调研分析"),
                 "derivation": ("推导说明",), "research-plan": ("访谈提纲", "调研清单"),
                 "evidence": ("证据表",), "summary": ("汇总稿",)}
        kinds = {k for k, words in hints.items() if any(w in text for w in words)}
        if kinds:
            pool = [m for m in pool if m["kind"] in kinds]
    versions = {int(value) for match in re.findall(r"(?<![A-Za-z0-9])v0*(\d+)(?!\d)|第(\d+)版", text, re.I)
                for value in match if value}
    if not versions and any(word in text for word in ("旧版", "上一版", "前一版", "之前那版")):
        history_pool = [m for k in {m["key"] for m in pool} for m in histories[k]]
        return history_pool if len(history_pool) > 1 else []
    if versions:
        keys = {m["key"] for m in pool}
        pool = [m for k in keys for m in histories[k] if m["version"] in versions]
    return pool


def ingest_feedback(root, input_path, actor, feedback_role="strategist", target=None,
                    transcript_path=None, simulation=False):
    input_path = Path(input_path)
    if not actor.strip() or feedback_role not in {"strategist", "director", "client"}:
        raise WorkflowError("反馈需实际提供者与有效角色；不代表其批准")
    if simulation and not test_mode(root):
        raise WorkflowError("模拟反馈只允许隔离测试")
    suffix = input_path.suffix.lower()
    if suffix not in TEXT | AUDIO:
        raise WorkflowError("不支持的反馈格式；请提供文字记录或列明的音频格式")
    original = archive(root, input_path, "evidence")
    event = {"actor": actor, "feedback_role": feedback_role, "simulation": simulation,
             "evidence": original, "target": None, "status": "needs_transcript"}
    if suffix in AUDIO:
        if transcript_path is None:
            return append(root, "feedback", {**event, "capabilities": capabilities(),
                          "question": "请提供对应音频的可靠转写稿，并保留时间点和不确定说话人。"})
        if Path(transcript_path).suffix.lower() not in TEXT:
            raise WorkflowError("转写稿须为 TXT/MD/SRT/VTT")
        transcript = archive(root, transcript_path, "evidence")
        event["transcript"] = transcript
        text = local(root, transcript["path"]).read_text(encoding="utf-8-sig")
        event["transcription_provenance"] = "由策略师提供；未声称机器已听取或核验原音频"
    else:
        text = input_path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise WorkflowError("反馈原文为空")
    matches = candidates(root, text, target)
    event.update({"text": text, "candidates": [ref(m) for m in matches],
                  "status": "bound" if len(matches) == 1 else "ambiguous"})
    if len(matches) == 1:
        event["target"] = ref(matches[0])
    else:
        event["question"] = "这份反馈对应哪个任务、哪份文件和哪个版本？请从候选中指定；尚未修改任何稿件。"
    return append(root, "feedback", event)


def bind_feedback(root, feedback_id, target, evidence_path, actor, simulation=False):
    from phase3_decisions import valid_event
    original = next((e for e in events(root, "feedback") if e["record_id"] == feedback_id), None)
    if not original or original["status"] != "ambiguous" or not valid_event(root, original):
        raise WorkflowError("只能澄清原文仍有效的歧义反馈")
    if target not in original["candidates"] or not actor.strip():
        raise WorkflowError("澄清目标不在原候选中或缺确认者")
    if simulation and not test_mode(root):
        raise WorkflowError("模拟澄清只允许隔离测试")
    if not Path(evidence_path).read_text(encoding="utf-8").strip():
        raise WorkflowError("须保存用户对版本的澄清原文")
    return append(root, "feedback", {
        "status": "bound", "target": target, "parent_feedback_id": feedback_id,
        "text": original["text"], "actor": actor, "simulation": simulation,
        "feedback_role": original["feedback_role"], "evidence": original["evidence"],
        **{k: original[k] for k in ("transcript", "transcription_provenance") if k in original},
        "clarification": archive(root, evidence_path, "evidence")})
