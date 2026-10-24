"""常驻监听：inbox 出现新音频 → 自动转写 → 生成纪要 → 归档。

    python watcher.py            # 前台常驻（systemd / 任务计划程序跑这个）
    python watcher.py --once     # 扫一遍就退出

设计要点：
- 单 worker 串行处理：本地 ASR 吃 CPU/GPU，并发只会互相拖死，串行最稳；
- watchdog 事件 + 定时全量扫描双保险（网络盘/NFS 上 inotify 事件可能丢）；
- 文件「大小+mtime 稳定 N 秒」才处理，避免读到同步中的半截文件。
"""

from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from config import SETTINGS, Settings, cli_settings, ensure_dirs
from pipeline import StateStore, iter_inbox, process_audio
from transcribe import release_models
from utils import PipelineError, get_logger, setup_logging, wait_until_stable

logger = get_logger("watcher")


class InboxHandler(FileSystemEventHandler):
    """只负责把候选路径丢进队列，重活交给 worker。"""

    def __init__(self, on_candidate):
        super().__init__()
        self._on_candidate = on_candidate

    def _notify(self, path: str) -> None:
        self._on_candidate(Path(path))

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._notify(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Syncthing 等工具是「写临时文件 → rename」，rename 才是真正完成信号
        if not event.is_directory:
            self._notify(getattr(event, "dest_path", event.src_path))

    def on_closed(self, event: FileSystemEvent) -> None:  # Linux inotify 才支持
        if not event.is_directory:
            self._notify(event.src_path)


class Watcher:
    def __init__(self, settings: Settings = SETTINGS):
        self.settings = settings
        self.queue: queue.Queue[Path] = queue.Queue()
        self.stop_event = threading.Event()
        self.state = StateStore(settings.work_dir / "state.json")
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._observer: Observer | None = None
        self._last_busy = time.monotonic()

    # ---------------- 队列 ----------------
    def submit(self, path: Path) -> None:
        key = str(path.resolve()) if path.exists() else str(path)
        with self._lock:
            if key in self._queued:
                return
            self._queued.add(key)
        self.queue.put(path)
        logger.debug("入队：%s", path.name)

    def _release(self, path: Path) -> None:
        key = str(path.resolve()) if path.exists() else str(path)
        with self._lock:
            self._queued.discard(key)

    # ---------------- 线程 ----------------
    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                path = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._handle(path)
            except Exception:
                logger.exception("worker 处理异常：%s", path)
            finally:
                self._release(path)
                self.queue.task_done()

    def _handle(self, path: Path) -> None:
        if path.suffix.lower() not in self.settings.audio_exts:
            return
        if not path.exists():
            return
        logger.info("发现新文件：%s", path.name)
        if not wait_until_stable(
            path, self.settings.stability_seconds, self.settings.stability_timeout
        ):
            logger.warning("文件长时间未稳定，先跳过，等待下一轮扫描：%s", path.name)
            return
        process_audio(path, self.state, self.settings)
        self._last_busy = time.monotonic()

    def _sweeper(self) -> None:
        """兜底扫描：掉事件、程序重启、同步工具延迟写入都能捞回来。"""
        while not self.stop_event.wait(self.settings.poll_interval_sec):
            try:
                for path in iter_inbox(self.settings):
                    if path.suffix.lower() in self.settings.audio_exts:
                        self.submit(path)
            except Exception:
                logger.exception("扫描 inbox 失败")

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        settings = self.settings
        ensure_dirs(settings)
        self._preflight()

        observer = Observer()
        observer.schedule(InboxHandler(self.submit), str(settings.inbox_dir), recursive=False)
        observer.daemon = True
        observer.start()
        self._observer = observer

        threading.Thread(target=self._worker, name="worker", daemon=True).start()
        threading.Thread(target=self._sweeper, name="sweeper", daemon=True).start()

        logger.info("监听已启动：%s", settings.inbox_dir)
        logger.info("结果目录：%s（失败文件：%s）", settings.processed_dir, settings.failed_dir)
        logger.info("按 Ctrl+C 停止")

        while not self.stop_event.wait(1.0):
            self._maybe_unload_idle()
        self.stop()

    def _maybe_unload_idle(self) -> None:
        """长时间没活就卸载模型，把显存让给同机其他服务。"""
        limit_min = self.settings.asr_idle_unload_min
        if limit_min <= 0:
            return
        if self.queue.qsize() > 0:
            return
        if time.monotonic() - self._last_busy < limit_min * 60:
            return
        try:
            if release_models():
                logger.info("空闲超过 %d 分钟，已卸载本地模型（收到新文件会自动重载）", limit_min)
                self._last_busy = time.monotonic()  # 避免反复尝试
        except Exception:
            logger.exception("卸载模型失败")

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        logger.info("正在停止监听…")
        self.stop_event.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        logger.info("已停止")

    def _preflight(self) -> None:
        s = self.settings
        logger.info("-" * 60)
        logger.info("ASR 后端    : %s (device=%s)", s.asr_backend, s.asr_device)
        if s.asr_backend == "funasr":
            logger.info("模型        : %s / %s / %s", s.funasr_model, s.funasr_vad, s.funasr_spk)
        else:
            logger.info("模型        : %s (diarize=%s)", s.whisperx_model, s.whisperx_diarize)
        logger.info("LLM         : %s @ %s", s.llm_model, s.llm_base_url)
        logger.info("API Key     : %s", "已配置" if s.llm_api_key else "⚠ 未配置（只会转写，不生成纪要）")
        logger.info("稳定性判定  : %ss / 扫描间隔 %ss", s.stability_seconds, s.poll_interval_sec)
        logger.info(
            "显存策略    : 空闲 %s 分钟后卸载模型",
            f"{s.asr_idle_unload_min}" if s.asr_idle_unload_min > 0 else "不卸载（常驻）",
        )
        logger.info("-" * 60)

        try:
            import shutil as _shutil

            if not _shutil.which(s.ffmpeg_bin):
                logger.error("⚠ 找不到 ffmpeg（%s），请先安装或设置 FFMPEG_BIN", s.ffmpeg_bin)
        except Exception:
            pass
        if not s.llm_api_key:
            logger.warning("未设置 LLM_API_KEY，报告阶段会失败（转写仍正常并会归档）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="监听 audios_inbox 并自动处理")
    parser.add_argument("--once", action="store_true", help="扫描一遍现有文件后退出")
    args = parser.parse_args(argv)

    # 目录监听只跑默认租户（多租户下每人一个 inbox，各自起一个 watcher 才能并行）
    settings = cli_settings()
    ensure_dirs(settings)
    setup_logging(settings.logs_dir, settings.log_level, "watcher")

    if args.once:
        state = StateStore(settings.work_dir / "state.json")
        files = iter_inbox(settings)
        logger.info("单次扫描：发现 %d 个文件", len(files))
        for path in files:
            if path.suffix.lower() not in settings.audio_exts:
                continue
            if wait_until_stable(path, settings.stability_seconds, settings.stability_timeout):
                process_audio(path, state, settings)
            else:
                logger.warning("文件未稳定，跳过：%s", path.name)
        return 0

    watcher = Watcher(settings)

    def _shutdown(signum, _frame):
        logger.info("收到信号 %s", signum)
        watcher.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError, AttributeError):
            pass

    try:
        watcher.start()
    except PipelineError as exc:
        logger.error("启动失败：%s", exc)
        return 1
    except KeyboardInterrupt:
        watcher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
