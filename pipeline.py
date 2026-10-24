"""单文件流水线：校验 → 去重 → 转写 → 纪要 → 归档。

被 watcher.py 调用，也可手动批量处理：
    python pipeline.py --once                 # 处理 audios_inbox 里现有文件
    python pipeline.py --file audios_inbox/a.m4a
"""

from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from config import SETTINGS, Settings, cli_settings, ensure_dirs
from summarize import summarize_transcript
from transcribe import transcribe_file
from utils import (
    PipelineError,
    fmt_size,
    get_logger,
    is_temp_name,
    probe_duration,
    read_json,
    safe_stem,
    setup_logging,
    sha256_file,
    wait_until_stable,
    write_json,
)

logger = get_logger("pipeline")


# --------------------------------------------------------------------------- #
# 去重状态库
# --------------------------------------------------------------------------- #
class StateStore:
    """记录每个文件哈希的处理状态，防止重启/重传导致重复处理。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = read_json(path, {})

    def get(self, digest: str) -> dict | None:
        with self._lock:
            return self._data.get(digest)

    def update(self, digest: str, **fields) -> dict:
        with self._lock:
            entry = self._data.setdefault(digest, {})
            entry.update(fields)
            entry["updated"] = datetime.now().isoformat(timespec="seconds")
            # strict=False：状态库写不进去会导致重启后重复转写整个归档，
            # 那比多一条格式不规范的记录严重得多，所以只告警不阻断。
            write_json(self.path, self._data, schema="state", strict=False)
            return dict(entry)

    def should_process(self, digest: str, settings: Settings) -> tuple[bool, str]:
        entry = self.get(digest)
        if not entry:
            return True, "新文件"
        if entry.get("status") == "done":
            return False, f"已处理过（{entry.get('out_dir')}）"
        if entry.get("status") == "failed":
            attempts = int(entry.get("attempts", 0))
            if attempts >= settings.max_attempts:
                return False, f"失败已达上限 {attempts} 次"
            if time.time() < float(entry.get("next_retry", 0)):
                return False, "失败冷却中"
            return True, f"重试第 {attempts + 1} 次"
        return True, "上次处理中断"


# --------------------------------------------------------------------------- #
# 归档
# --------------------------------------------------------------------------- #
def _archive_dir_for(audio: Path, digest: str, settings: Settings) -> Path:
    base = f"{datetime.now():%Y-%m-%d}_{safe_stem(audio.stem)}"
    candidate = settings.processed_dir / base
    if not candidate.exists():
        return candidate
    # 同名但内容不同的录音（比如「会议」录了两次）
    for index in range(2, 100):
        candidate = settings.processed_dir / f"{base}-{index}"
        if not candidate.exists():
            return candidate
    return settings.processed_dir / f"{base}-{digest[:6]}"


def _stamp_meeting_id(target: Path, meeting_id: str) -> None:
    """把唯一 ID 写进 meta / 转写稿 / 纪要 / transcript.json。

    只改每份文件顶部的元信息，不动正文。即使以后目录被改名，
    ID 也跟着文件走，用户报一个 ID 就能定位到音频、稿子、纪要和归属人。
    """
    meta_path = target / "meta.json"
    if meta_path.is_file():
        try:
            meta = read_json(meta_path, {})
            meta["meeting_id"] = meeting_id
            write_json(meta_path, meta)
        except Exception:
            logger.warning("写入 meeting_id 失败：%s", meta_path)

    for name in ("transcript.md", "report.md"):
        path = target / name
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            if any(x.startswith("meeting_id:") for x in lines[:6]):
                continue
            if lines and lines[0].strip() == "---":
                lines.insert(1, f"meeting_id: {meeting_id}")
            else:
                lines.insert(0, f"meeting_id: {meeting_id}")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            logger.warning("写入 meeting_id 失败：%s", path)

    data_path = target / "transcript.json"
    if data_path.is_file():
        try:
            data = read_json(data_path, {})
            data["meeting_id"] = meeting_id
            write_json(data_path, data)
        except Exception:
            logger.warning("写入 meeting_id 失败：%s", data_path)


def _attach_cost(run_dir: Path, target: Path, meeting_id: str) -> None:
    """把这次转写/纪要的记账从「工作目录名」改挂到 meeting_id，并写进纪要末尾。

    归档前的 ref 只能是工作目录名（那时还没有 meeting_id），这里一次性对齐；
    之后用 `meeting_id` 查成本就查得到。整块失败不影响归档。
    """
    try:
        from costs import append_to_report, relabel

        moved = relabel(run_dir.name, meeting_id)
        if moved:
            logger.debug("成本记录改挂 %s → %s（%d 条）", run_dir.name, meeting_id, moved)
        append_to_report(target / "report.md", meeting_id)
    except Exception as exc:
        logger.debug("成本块写入跳过：%s", exc)


def archive(
    audio: Path, run_dir: Path, digest: str, settings: Settings, extra: dict | None = None
) -> Path:
    """把音频 + 转写 + 纪要一起搬进 processed/<日期>_<名称>/。

    归档时分配唯一 meeting_id：`M20260101-090000-aaaa`
    （M + 日期 + 时间 + 音频哈希前 4 位），全局可读、可搜索、可对账。
    """
    target = _archive_dir_for(audio, digest, settings)

    meeting_id = f"M{datetime.now():%Y%m%d-%H%M%S}-{digest[:4]}"

    # 目标音频名要在「动盘之前」定下来：meta 的 audio/size 依赖它，而 meta 必须
    # 校验通过才允许搬文件。否则校验失败会留下「有音频、没 meta」的半成品目录，
    # 下次重跑时 _archive_dir_for 看到目录已存在就会另起一个 -2，变成重复归档。
    # moved_audio.exists() 查的是磁盘而不是 mkdir 结果，提前判断语义不变。
    moved_audio = target / audio.name
    if moved_audio.exists():
        moved_audio = target / f"{audio.stem}_{digest[:6]}{audio.suffix}"

    meta = {
        "meeting_id": meeting_id,
        "audio": moved_audio.name,
        "sha256": digest,
        "size": audio.stat().st_size,
        "title": audio.stem,
        "owner": settings.owner,
        "archived_at": datetime.now().isoformat(timespec="seconds"),
        **(extra or {}),
    }
    # 唯一一处严格校验，且在动盘之前：新建归档的 meta 必须字段齐全。它是对账、
    # 查成本、定位音频的唯一入口，缺 meeting_id / sha256 会让后续所有流程失去锚点。
    # 后续给这份 meta 补字段（_stamp_meeting_id）不再校验——刚写出的东西必然合规。
    from schema_guard import validate as validate_schema

    validate_schema("meta", meta)

    target.mkdir(parents=True, exist_ok=True)
    shutil.move(str(audio), str(moved_audio))

    for name in ("transcript.md", "transcript.json", "report.md", "speakers.json"):
        src = run_dir / name
        if src.exists():
            shutil.copy2(src, target / name)

    write_json(target / "meta.json", meta, schema="meta")
    _stamp_meeting_id(target, meeting_id)
    _attach_cost(run_dir, target, meeting_id)

    if settings.keep_wav:
        wav_dir = run_dir / "_wav"
        if wav_dir.exists():
            shutil.move(str(wav_dir), str(target / "_wav"))
    shutil.rmtree(run_dir, ignore_errors=True)
    return target


# --------------------------------------------------------------------------- #
# 单文件处理
# --------------------------------------------------------------------------- #
def _basic_checks(audio: Path, settings: Settings) -> None:
    if is_temp_name(audio.name):
        raise PipelineError("这是同步工具的临时文件，跳过")
    if audio.suffix.lower() not in settings.audio_exts:
        raise PipelineError(f"不支持的扩展名 {audio.suffix}")
    size = audio.stat().st_size
    if size < settings.min_file_size_kb * 1024:
        raise PipelineError(f"文件过小（{fmt_size(size)} < {settings.min_file_size_kb}KB），可能是空音频")


def process_audio(
    audio: Path, state: StateStore, settings: Settings = SETTINGS, dual: bool = False,
    events: list[dict] | None = None, mic: str = "",
) -> bool:
    """处理一个音频。返回 True 表示成功（含「已处理过」这种正常跳过）。

    dual=True：左声道=麦克风、右声道=系统声音的双路分轨录音，按声道判定说话人。
    events：助理播报时间线（见 transcribe.apply_assistant_timeline）。
    """
    audio = Path(audio).resolve()
    logger.info("=" * 72)
    logger.info("开始处理：%s", audio.name)

    try:
        _basic_checks(audio, settings)
    except PipelineError as exc:
        logger.warning("跳过 %s：%s", audio.name, exc)
        return True

    digest = sha256_file(audio)
    ok, reason = state.should_process(digest, settings)
    if not ok:
        logger.info("跳过 %s：%s", audio.name, reason)
        if "已处理过" in reason:
            dup_dir = settings.processed_dir / "_duplicates"
            dup_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(audio), str(dup_dir / f"{safe_stem(audio.stem)}_{digest[:6]}{audio.suffix}"))
            logger.info("重复文件已移入 %s", dup_dir)
        return True

    duration = probe_duration(audio, settings.ffprobe_bin)
    if duration is None:
        logger.error("无法读取音频时长（文件损坏或非音频）：%s", audio.name)
        state.update(digest, status="failed", attempts=settings.max_attempts,
                     file=audio.name, last_error="无法读取音频时长", next_retry=0)
        return False

    attempts = int((state.get(digest) or {}).get("attempts", 0)) + 1
    run_dir = settings.work_dir / f"{safe_stem(audio.stem)}-{digest[:8]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    try:
        # 1) 转写
        transcribe_file(audio, run_dir, settings, dual=dual, events=events)
        transcript_md = run_dir / "transcript.md"
        if not transcript_md.exists():
            raise PipelineError("转写未产出 transcript.md")

        # 2) 纪要
        out_report = run_dir / "report.md"
        try:
            summarize_transcript(transcript_md, out_report, settings)
        except PipelineError as exc:
            # 纪要失败不应该丢掉转写结果：归档音频+转写，标注报告缺失
            logger.error("纪要生成失败，保留转写稿继续归档：%s", exc)
            out_report.write_text(
                f"# 会议纪要生成失败\n\n> {exc}\n\n"
                f"转写稿已保留，可稍后手动执行：\n"
                f"`python summarize.py --transcript \"{transcript_md}\"`\n",
                encoding="utf-8",
            )

        # 3) 归档
        target = archive(
            audio, run_dir, digest, settings,
            extra={"duration": duration, "attempts": attempts, "dual": dual,
                   "assistant_events": events or [],
                   "mic": mic or "",
                   "elapsed_sec": round(time.time() - started, 1)},
        )
        state.update(
            digest, status="done", file=audio.name, attempts=attempts,
            out_dir=str(target), duration=duration, last_error="", next_retry=0,
        )

        # 4) 音色语料：把这次录音里「确定是本人」的干净片段攒进语料池，
        #    以后重建参考音用。CosyVoice2 不训练，只换参考音——参考音越干净越像。
        #    这一步失败绝不能影响转写/归档，所以整块吞掉异常。
        if getattr(settings, "voice_corpus_auto", True):
            try:
                from voice_corpus import collect_from_archive

                got = collect_from_archive(target, settings)
                if got.get("added"):
                    logger.info("音色语料：新增 %d 段（池内共 %d 段）",
                                got["added"], got.get("total", 0))
            except Exception as exc:
                logger.warning("音色语料收集跳过：%s", exc)

        logger.info("✅ 完成：%s（用时 %.1fs）", target, time.time() - started)
        return True

    except PipelineError as exc:
        logger.error("❌ 处理失败：%s → %s", audio.name, exc)
        return _handle_failure(audio, digest, attempts, str(exc), run_dir, state, settings)
    except Exception as exc:
        logger.exception("❌ 未预期异常：%s", audio.name)
        return _handle_failure(audio, digest, attempts, f"{type(exc).__name__}: {exc}", run_dir, state, settings)


def _handle_failure(
    audio: Path, digest: str, attempts: int, error: str,
    run_dir: Path, state: StateStore, settings: Settings,
) -> bool:
    """失败重试策略：先留在 inbox 冷却重试，超过上限才移入 _failed。"""
    if attempts >= settings.max_attempts:
        failed_target = settings.failed_dir / f"{datetime.now():%Y-%m-%d_%H%M}_{audio.name}"
        failed_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(audio), str(failed_target))
        except OSError as exc:
            logger.error("移动失败文件出错：%s", exc)
        (settings.failed_dir / f"{failed_target.stem}.error.txt").write_text(error, encoding="utf-8")
        logger.error("已达最大重试次数，文件移入 %s", settings.failed_dir)
        state.update(digest, status="failed", attempts=attempts, file=audio.name,
                     last_error=error, next_retry=0)
    else:
        retry_at = time.time() + settings.retry_cooldown_sec * attempts
        logger.warning("将在 %.0f 分钟后重试（第 %d/%d 次）",
                       settings.retry_cooldown_sec * attempts / 60, attempts, settings.max_attempts)
        state.update(digest, status="failed", attempts=attempts, file=audio.name,
                     last_error=error, next_retry=retry_at)
    shutil.rmtree(run_dir, ignore_errors=True)
    return False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def iter_inbox(settings: Settings) -> list[Path]:
    if not settings.inbox_dir.exists():
        return []
    return sorted(
        p for p in settings.inbox_dir.iterdir()
        if p.is_file() and not is_temp_name(p.name)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="手动跑一遍流水线")
    parser.add_argument("--once", action="store_true", help="处理 inbox 中所有音频")
    parser.add_argument("--file", help="只处理指定音频")
    parser.add_argument("--force", action="store_true", help="忽略去重记录，强制重跑")
    args = parser.parse_args(argv)

    settings = cli_settings()
    ensure_dirs(settings)
    setup_logging(settings.logs_dir, settings.log_level, "pipeline")
    state = StateStore(settings.work_dir / "state.json")

    if args.file:
        targets = [Path(args.file).expanduser()]
    elif args.once:
        targets = iter_inbox(settings)
    else:
        parser.error("需要 --once 或 --file")

    if not targets:
        logger.info("inbox 为空：%s", settings.inbox_dir)
        return 0

    failures = 0
    for audio in targets:
        if not audio.exists():
            logger.error("文件不存在：%s", audio)
            failures += 1
            continue
        if not wait_until_stable(audio, settings.stability_seconds, settings.stability_timeout):
            logger.error("文件在超时时间内未稳定（可能在传输中）：%s", audio.name)
            failures += 1
            continue
        if args.force:
            digest = sha256_file(audio)
            state.update(digest, status="pending", attempts=0, next_retry=0)
        if not process_audio(audio, state, settings):
            failures += 1

    logger.info("批量处理结束：%d 个，失败 %d 个", len(targets), failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
