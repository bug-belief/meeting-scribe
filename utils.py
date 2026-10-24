"""通用工具：日志、ffmpeg、文件稳定性判断、哈希、原子写。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s"
FILE_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(funcName)s:%(lineno)d | %(message)s"

TEMP_SUFFIXES = (
    ".tmp", ".part", ".partial", ".crdownload", ".download", ".!ut",
    ".filepart", ".opdownload", ".syncthing-tmp",
)


class PipelineError(Exception):
    """可预期的流水线错误（用于区分「音频本身有问题」与「代码 bug」）。

    `kind` / `hint` 是给**界面**用的可选标注（见 `llm_errors.py`）：
    两个属性都默认 None，所以已有的 `PipelineError("...")` 调用一行都不用改。
    """

    def __init__(self, message: str = "", kind: str | None = None,
                 hint: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.hint = hint


def setup_logging(logs_dir: Path, level: str = "INFO", log_name: str = "pipeline") -> None:
    """控制台 + 滚动文件双通道日志，幂等。"""
    root = logging.getLogger()
    if getattr(root, "_meeting_configured", False):
        return
    root.setLevel(logging.DEBUG)
    logs_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(FILE_LOG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        logs_dir / f"{log_name}.log", maxBytes=10 * 1024 * 1024,
        backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    console = logging.StreamHandler(_utf8_stdout())
    console.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))
    console.setLevel(getattr(logging, level, logging.INFO))

    root.addHandler(file_handler)
    root.addHandler(console)
    root._meeting_configured = True  # type: ignore[attr-defined]

    # 第三方库的 DEBUG 太吵（watchdog 每个 inotify 事件一行、modelscope 每步一行），
    # 压到 WARNING；我们自己的模块名不在此列，仍按 DEBUG 记录
    for noisy in (
        "watchdog",
        "watchdog.observers",
        "watchdog.observers.inotify_buffer",
        "modelscope",
        "urllib3",
        "httpx",
        "httpcore",
        # openai SDK 依赖的 httpx 模块在此环境里叫 httpx2 / httpcore2
        "httpx2",
        "httpcore2",
        "openai",
        "openai._base_client",
        "filelock",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _utf8_stdout():
    """Windows 控制台默认 GBK，中文/emoji 会炸，这里强制 UTF-8。"""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return sys.stdout


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# --------------------------------------------------------------------------- #
# 文件与哈希
# --------------------------------------------------------------------------- #
def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def is_temp_name(name: str) -> bool:
    """判断同步工具的中间态 / 隐藏文件，避免半截文件被处理。"""
    lowered = name.lower()
    if lowered.startswith(".") or lowered.endswith("~"):
        return True
    if ".syncthing." in lowered or ".!sync" in lowered:
        return True
    return lowered.endswith(TEMP_SUFFIXES)


def wait_until_stable(path: Path, stable_seconds: float, timeout: float) -> bool:
    """等待文件大小与 mtime 连续 stable_seconds 秒不变，视为传输完成。"""
    start = time.monotonic()
    last_sig: tuple[int, float] | None = None
    stable_since = 0.0
    while time.monotonic() - start < timeout:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        signature = (stat.st_size, stat.st_mtime)
        now = time.monotonic()
        if signature != last_sig:
            last_sig = signature
            stable_since = now
        elif now - stable_since >= stable_seconds and stat.st_size > 0:
            return True
        time.sleep(1.0)
    return False


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, data, schema: str | None = None, strict: bool = True) -> None:
    """原子写 JSON。

    `schema` 传 schema 名（见 schema_guard.SCHEMAS）则先按 `schema/*.schema.json`
    校验结构。默认不校验，存量调用行为一字不变——**只有「新建」路径才该传**：
    给历史数据打补丁的流程（backfill / refresh_archive）字段本来就不全，
    在那里严格校验等于让修补工具彻底不能用。

    `strict=False` 时校验失败只记 warning 仍然写入。用于 state.json 这类
    「写失败代价比脏数据更大」的文件（状态库写不进去 → 重启后重复转写）。
    """
    if schema:
        from schema_guard import validate  # 延迟导入：不传 schema 的调用不碰这套

        try:
            validate(schema, data)
        except Exception as exc:
            if strict:
                raise
            logging.getLogger("utils").warning("结构不合契约但仍写入 %s：%s", path, exc)

    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


# --------------------------------------------------------------------------- #
# 音频
# --------------------------------------------------------------------------- #
def probe_duration(path: Path, ffprobe_bin: str = "ffprobe") -> float | None:
    """返回秒数；无音频流或文件损坏时返回 None（不抛异常）。"""
    cmd = [
        ffprobe_bin, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "format=duration", "-of",
        "default=noprint_wrappers=1:nokey=1", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def probe_audio_info(path: Path, ffprobe_bin: str = "ffprobe") -> dict:
    """读原始音频的采样率与码率，用于粗判信道类型（电话窄带 / 近场 / 远场）。

    必须在 to_wav16k 之前调用——转码后一律是 16k，原始特征就没了。
    失败时返回空字典，不抛异常。
    """
    cmd = [
        ffprobe_bin, "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=sample_rate,bit_rate,channels:format=bit_rate",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        data = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    stream = (data.get("streams") or [{}])[0]
    info: dict = {}
    try:
        if stream.get("sample_rate"):
            info["sample_rate"] = int(stream["sample_rate"])
        if stream.get("channels"):
            info["channels"] = int(stream["channels"])
    except (TypeError, ValueError):
        pass
    for key, value in (("bit_rate", stream.get("bit_rate")),
                       ("bit_rate", (data.get("format") or {}).get("bit_rate"))):
        if value and "bitrate_kbps" not in info:
            try:
                info["bitrate_kbps"] = float(value) / 1000.0
            except (TypeError, ValueError):
                pass
    return info


def to_wav16k(src: Path, dst: Path, ffmpeg_bin: str = "ffmpeg", channels: int = 1) -> Path:
    """统一转 16k s16 wav —— 所有 ASR 后端都吃单声道，也顺手修掉采样率/声道问题。

    channels=2 时保留双声道（供「双路分轨」分析用，左=麦克风、右=系统声音）。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-i", str(src),
        "-map", "0:a:0", "-vn", "-ac", str(max(1, int(channels))), "-ar", "16000",
        "-c:a", "pcm_s16le", str(dst),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError as exc:
        raise PipelineError(f"找不到 ffmpeg（{ffmpeg_bin}），请先安装或在 .env 里指定 FFMPEG_BIN") from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError("ffmpeg 转换超时（>30 分钟），文件可能异常") from exc
    if result.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        raise PipelineError("音频解码失败，可能不是有效音频：" + " / ".join(tail))
    return dst


def split_audio(src: Path, out_dir: Path, minutes: int, ffmpeg_bin: str = "ffmpeg") -> list[Path]:
    """按固定时长切片（仅超长音频启用；切点会落在片内，说话人编号不跨片）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / "part_%03d.wav"
    cmd = [
        ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-i", str(src),
        "-f", "segment", "-segment_time", str(minutes * 60),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        raise PipelineError("音频切片失败：" + (result.stderr or "").strip()[-300:])
    return sorted(out_dir.glob("part_*.wav"))


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #
def fmt_hms(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def fmt_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_stem(name: str, max_len: int = 60) -> str:
    """把任意文件名清洗成可安全落盘的 stem（保留中文）。"""
    cleaned = _ILLEGAL.sub("_", name).strip(" ._")
    cleaned = re.sub(r"\s+", "_", cleaned)
    return (cleaned or "audio")[:max_len]


def estimate_tokens(text: str) -> int:
    """中文为主场景的粗略估算：约 1.5 字符 ≈ 1 token。只用于决定是否分块，不需要精确。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4)
