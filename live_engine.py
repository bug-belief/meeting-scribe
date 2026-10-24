"""实时转写引擎与会话（O-11 / C-1 从 server.py 整块搬出，逐字未改）。

这里装的是「跟 HTTP 无关」的那一半：流式模型单例、单个录音会话的状态机、
噪声幻听判别、归档收尾。`server.py` 只管路由与鉴权。

搬出来的动机：这 240 行原本和 27 个路由挤在同一个 `create_app()` 里，
改任何一处都要在 1,600 行里翻找。

⚠️ 三个进程级单例（`ENGINE` / `OFFLINE_ENGINE` / `SESSIONS`）**故意保留为模块级全局**：
流式模型 1.2GB，不可能每个 app 一份。测试里连续建两个 app 会复用同一个 ENGINE，
这是**现状行为，不是 bug**，别当内存泄漏去修。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

from config import Settings
from utils import get_logger, safe_stem, sha256_file

logger = get_logger("live")

# --------------------------------------------------------------------------- #
# 音频与会话语义常量
# --------------------------------------------------------------------------- #
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 9600          # 600 ms，paraformer 流式的推荐块长
BYTES_PER_SAMPLE = 2
# 判断「对方那一路有没有录到声音」至少要看 8 秒（与前端 watchPeerLevel 的 secs<8 对齐）
PEER_PROBE_MIN_SAMPLES = SAMPLE_RATE * 8
SILENCE_RMS = 0.006           # 低于此能量视为静音（16k float，-44 dBFS 附近）
SILENCE_FINALIZE_SEC = 0.75   # 连续静音超过该时长就把当前句定稿（0.9→0.75：少等 0.15s）
MAX_LINE_CHARS = 60           # 句太长强制断开，避免一整段没换行
# 噪声闸门：流式模型在**没有语音**时也会吐字，实测麦克风底噪 0.0007~0.002 就能
# 解码出「的的的的的」「亲我现在」这种碎片。绝对下限取 0.0035（真人说话 ≥0.02），
# 相对值取本底噪声中位数的 4 倍，用来适配外接麦/手机麦的不同增益。
NOISE_FLOOR_MIN = 0.0035
NOISE_FLOOR_RATIO = 4.0


# --------------------------------------------------------------------------- #
# 流式引擎（进程内单例，模型只加载一次）
# --------------------------------------------------------------------------- #
class StreamEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._lock = threading.Lock()
        self._gen_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self):
        from funasr import AutoModel

        from diarize import _resolve_device

        device = _resolve_device(self.settings.asr_device)
        t0 = time.time()
        logger.info("加载流式模型 paraformer-zh-streaming（device=%s）", device)
        self._model = AutoModel(
            model="paraformer-zh-streaming",
            device=device,
            hub=self.settings.funasr_hub,
            disable_update=True,
        )
        logger.info("流式模型就绪，%.1fs", time.time() - t0)

    def ensure_loaded(self) -> None:
        """预加载模型。首次约 20-30s，放在服务启动时做，别让用户点开始后干等。"""
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._load()

    def feed(self, chunk: np.ndarray, cache: dict, is_final: bool = False) -> str:
        self.ensure_loaded()
        # 模型对象本身不是线程安全的：多会话并发时串行化，实时性的损失可以接受
        with self._gen_lock:
            res = self._model.generate(
                input=chunk,
                cache=cache,
                is_final=is_final,
                chunk_size=[0, 10, 5],
                encoder_chunk_look_back=4,
                decoder_chunk_look_back=1,
            )
        item = res[0] if isinstance(res, list) and res else (res or {})
        return (item.get("text") or "").strip()


# --------------------------------------------------------------------------- #
# 会话
# --------------------------------------------------------------------------- #
class LiveSession:
    def __init__(self, settings: Settings, title: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.title = (title or "").strip() or f"会议 {datetime.now():%H%M}"
        self.started = time.time()
        self.settings = settings
        self.dir = settings.work_dir / "live" / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.raw_path = self.dir / "raw.pcm"
        self.raw = self.raw_path.open("wb")
        self.cache: dict = {}
        self.pending = ""           # 当前句已识别但未定稿的文本
        self.lines: list[dict] = []  # 定稿的行
        self.samples = 0
        self.silence_samples = 0
        self.line_start_ms = 0
        self.channels = 1           # 1=麦克风/系统声音，2=双路分轨（左麦克风、右系统）
        self.source = "mic"         # mic | system | dual
        # 助理播报时间线：{id, text, voice, start, end, spoken_chars, state}
        # 只有播出来的部分算数；被打断后作废的句子 state=cancelled，不参与标注
        self.events: list[dict] = []
        self.mic = ""               # 实际采集用的麦克风名（浏览器上报，用来分设备攒音色语料）
        # 当前这句的左右声道能量累计：用来判「这句是谁说的」（双路分轨才有意义）
        self.line_rms_l = 0.0
        self.line_rms_r = 0.0
        # 整场累计：右声道历史最大能量 + 已经过的有效采样数。
        # 用途只有一个——判断「对方那一路到底有没有录到声音」。
        # 电话外放时对方的声音从房间进麦克风，右声道全程近乎静音，
        # 此时按左右能量判归属会把对方的话也算成「我」，宁可先不标。
        self.peer_max_rms = 0.0
        self.peer_probe_samples = 0
        self.peer_who_warned = False   # 「右声道静音、不标 who」只告警一次，别刷屏
        # 噪声闸门用的两个量：本句峰值能量、最近的能量样本（估本底噪声）
        self.line_peak = 0.0
        self.rms_hist: list[float] = []

    def note_peer_level(self, rms_r: float, samples: int) -> None:
        """累计右声道的「存在证据」。只在双声道下有意义。"""
        if rms_r > self.peer_max_rms:
            self.peer_max_rms = float(rms_r)
        self.peer_probe_samples += max(0, int(samples))

    def peer_channel_alive(self) -> bool | None:
        """右声道是否真的录到了声音。None = 还判断不了（样本太少）。

        阈值 0.003 与前端 `watchPeerLevel` 保持一致：
        它已经用这个值判断「右声道一直是静音」并弹过提示，两处口径必须一样，
        否则会出现「前端说静音、后端照样标成对方」的矛盾。
        """
        # 至少观察 8 秒才有资格下结论（与前端 `secs < 8` 对齐）
        if self.peer_probe_samples < PEER_PROBE_MIN_SAMPLES:
            return None
        return self.peer_max_rms > 0.003

    def write(self, chunk: bytes) -> None:
        self.raw.write(chunk)
        self.samples += len(chunk) // (BYTES_PER_SAMPLE * self.channels)

    def close(self) -> Path:
        self.raw.close()
        wav_path = self.dir / f"{safe_stem(self.title)}.wav"
        with wave.open(str(wav_path), "wb") as fh:
            fh.setnchannels(self.channels)
            fh.setsampwidth(2)
            fh.setframerate(SAMPLE_RATE)
            with self.raw_path.open("rb") as src:
                while True:
                    block = src.read(1 << 20)
                    if not block:
                        break
                    fh.writeframes(block)
        return wav_path

    @property
    def duration(self) -> float:
        return self.samples / SAMPLE_RATE

    # ---------------- 噪声闸门 ----------------
    def note_rms(self, rms: float) -> None:
        """记一个能量样本：既更新本句峰值，也维护本底噪声的估计。"""
        self.rms_hist.append(float(rms))
        if len(self.rms_hist) > 900:      # 900 × 600ms ≈ 9 分钟，够稳也够跟得上环境变化
            del self.rms_hist[:400]
        if rms > self.line_peak:
            self.line_peak = rms

    def rms_floor(self) -> float:
        """本底噪声估计。取最近能量样本的中位数——说话只占少数时间，
        中位数基本等于"没人说话时"的电平，比固定阈值更抗麦克风增益差异。"""
        if not self.rms_hist:
            return NOISE_FLOOR_MIN
        vals = sorted(self.rms_hist)
        med = vals[len(vals) // 2]
        return max(NOISE_FLOOR_MIN, min(0.02, med * NOISE_FLOOR_RATIO))

    def reset_line(self) -> None:
        """清空当前句的全部记账（定稿、丢弃、断句后都要调）。"""
        self.pending = ""
        self.cache = {}          # 断句后清空上下文，避免上一句污染下一句
        self.line_start_ms = int(self.duration * 1000)
        self.silence_samples = 0
        self.line_rms_l = 0.0
        self.line_rms_r = 0.0
        self.line_peak = 0.0

    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "session": self.id,
            "title": self.title,
            "duration": round(self.duration, 1),
            "lines": [{k: v for k, v in x.items() if not k.startswith("_")} for x in self.lines],
            "pending": self.pending,
        }

    # ---- 助理播报时间线：用来把「自己放出去的声音」从人声里摘出来 ----
    def tts_window(self) -> tuple[float, float] | None:
        """当前正在播报的区间（没有就返回 None）。"""
        for ev in reversed(self.events):
            if ev.get("state") == "speaking":
                return float(ev.get("start") or 0.0), self.duration
        return None

    def is_echo(self, start: float, end: float, right_dominant: bool) -> bool:
        """这段是不是助理自己的声音被声卡又录了回来。

        只看时间重叠不够——用户完全可能在助理说话时插话（打断），
        所以还要求「这句的能量主要在右声道（系统声音）」，双路分轨下才成立。
        """
        span = max(0.01, end - start)
        for ev in self.events:
            s = float(ev.get("start") or 0.0)
            e = float(ev.get("end") or self.duration)
            if e <= s:
                continue
            ov = max(0.0, min(end, e) - max(start, s))
            if self.channels == 1:
                if ov >= 0.6 * span:
                    return True
                continue
            # 双路分轨：右声道为主 + 与播报有实质重叠，就是回声。
            # 用 1 秒兜底，是为了容忍「这句的时间戳被前一段静音带偏」的情况。
            if right_dominant and (ov >= 0.6 * span or ov >= 1.0):
                return True
        return False


# --------------------------------------------------------------------------- #
# 进程级单例
# --------------------------------------------------------------------------- #
SESSIONS: dict[str, LiveSession] = {}
ENGINE: StreamEngine | None = None
# 离线引擎（paraformer-zh + ct-punc），只给 /api/asr/quick 用，首次调用才加载
OFFLINE_ENGINE = None


def chunk_rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk**2))) if chunk.size else 0.0


# --------------------------------------------------------------------------- #
# 离线收尾：整段走完整管线
# --------------------------------------------------------------------------- #
def log_live_asr(session: "LiveSession") -> None:
    """记一笔实时流式转写的成本。

    流式识别是跟着音频跑的：算力消耗约等于音频时长，所以 compute_sec 直接用
    `samples / 采样率`，比墙钟更准（墙钟里还混着等人说话的时间）。

    **必须在 `finalize` 的 relabel 之前调用**：否则这条记录后写，
    ref 停在 session id 上，永远并不到 meeting_id 名下。
    幂等：只记一次，异常断开与正常收尾都走这里。
    """
    if getattr(session, "_cost_logged", False):
        return
    try:
        from costs import asr_cost, log as log_cost

        audio_sec = session.samples / float(SAMPLE_RATE)
        if audio_sec >= 1:
            billed = asr_cost(audio_sec, session.settings.funasr_model, compute_sec=audio_sec)
            log_cost("asr", ref=session.id, scene="live",
                     owner=session.settings.owner, **billed)
        session._cost_logged = True
    except Exception as exc:  # pragma: no cover - 记账永远是可选的
        logger.debug("实时 ASR 记账跳过：%s", exc)


def finalize(session: LiveSession) -> dict:
    """把会话音频交给 pipeline：转写（含说话人/声纹）→ 纪要 → 归档。"""
    from pipeline import StateStore, process_audio

    settings = session.settings
    wav_path = session.close()
    # 先把实时那一遍的账记上，下面 relabel 才带得走
    log_live_asr(session)
    digest = sha256_file(wav_path)
    state = StateStore(settings.work_dir / "state.json")
    state.update(digest, status="pending", attempts=0, next_retry=0)
    ok = process_audio(wav_path, state, settings, dual=session.channels > 1,
                       events=session.events, mic=session.mic)
    entry = state.get(digest) or {}
    out_dir = Path(entry.get("out_dir") or "")

    result = {
        "type": "done",
        "session": session.id,
        "ok": bool(ok),
        "duration": round(session.duration, 1),
        # ⚠️ B-5 同族：原来返回 str(out_dir) 绝对路径，会随 WS done 消息发到浏览器。
        # 前端 setPlayback 只取最后一段目录名，改 basename 无副作用。
        "archive": out_dir.name if out_dir else "",
        "source": session.source,
        "channels": session.channels,
        "transcript": "",
        "report": "",
        "notes": [],
    }
    if out_dir.is_dir():
        for name, key in (("transcript.md", "transcript"), ("report.md", "report")):
            path = out_dir / name
            if path.is_file():
                result[key] = path.read_text(encoding="utf-8")
        meta = out_dir / "meta.json"
        if meta.is_file():
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
                result["elapsed"] = info.get("elapsed_sec")
                result["meeting_id"] = info.get("meeting_id") or ""
            except Exception:
                pass
        # 实时期间的账（流式 ASR / 代答 / 合成）原本挂在 session 上，
        # 归档后合并到 meeting_id，并把纪要里的成本块按「实时 + 归档」重算一遍
        if result.get("meeting_id"):
            try:
                from costs import append_to_report, relabel

                relabel(session.id, result["meeting_id"])
                append_to_report(out_dir / "report.md", result["meeting_id"])
                # 成本块变了，重新读一遍给前端
                if (out_dir / "report.md").is_file():
                    result["report"] = (out_dir / "report.md").read_text(encoding="utf-8")
            except Exception as exc:
                logger.debug("成本合并跳过：%s", exc)
        speakers = out_dir / "transcript.json"
        if speakers.is_file():
            try:
                data = json.loads(speakers.read_text(encoding="utf-8"))
                result["notes"] = data.get("notes") or []
                result["speaker_names"] = data.get("speaker_names") or {}
            except Exception:
                pass
    else:
        result["error"] = entry.get("last_error") or "归档目录不存在，请查看服务日志"
    return result


# --------------------------------------------------------------------------- #
# 噪声幻听判别（纯函数，无状态，可单测）
# --------------------------------------------------------------------------- #
_NOISE_STRIP = "，。、！？；：,.!?;:~～…—-— \t\n"


def looks_like_noise(text: str) -> bool:
    """判别「噪声幻听」——没人说话时模型硬编出来的字。

    实测（2026-10-19 会话 M20260101-100000）：麦克风底噪 0.0007~0.0018 时，
    paraformer 流式会稳定吐出
        「的就是我个的的的的」「我的的的的的的」「小等等就是好的的的…是是是是」
    这类碎片。它们的特征是**单字刷屏或整句复读**，真实语句几乎不会长这样。
    单字反复是模型在静音上"续写"的结果，光看文本就能判掉一大半，
    再用能量闸门（真人 0.02+ vs 底噪 0.002）兜住「亲我现在」这种不重复的短碎片。

    注意：这里只做**形态**判断，不做语义判断——不猜用户想说什么，
    所以宁可漏（多显示一句）也不误杀（把真话删掉）。阈值就是按这个取向定的。
    """
    t = "".join(ch for ch in str(text or "") if ch not in _NOISE_STRIP)
    if len(t) < 2:
        return False
    counts: dict[str, int] = {}
    for ch in t:
        counts[ch] = counts.get(ch, 0) + 1
    top = max(counts.values())
    # 单字刷屏：「的的的的」「是是是是」。真人不会把同一个字连说 4 遍还占一半篇幅。
    if top >= 4 and top >= 0.45 * len(t):
        return True
    # 连续同字：模型在静音上会「续写」，常常是「的的的的…是是是是…」交替刷屏，
    # 单看最高频字不到一半，但连写片段加起来能占六成。
    if len(t) >= 6:
        runs: list[int] = []
        cur = 1
        for i in range(1, len(t)):
            if t[i] == t[i - 1]:
                cur += 1
            else:
                runs.append(cur)
                cur = 1
        runs.append(cur)
        if sum(r for r in runs if r >= 3) >= 0.4 * len(t):
            return True
    # 极短的全重复：「我我」「嗯嗯」。这类在底噪上最常见，一句话里没有别的信息量。
    # 注意只到两字：「哈哈哈」是笑，不是幻听。
    if len(t) <= 2 and top == len(t):
        return True
    # 整句原样复读：「我我」「嗯嗯嗯嗯」之外的长句复读
    if len(t) >= 6 and len(t) % 2 == 0:
        half = len(t) // 2
        if t[:half] == t[half:]:
            return True
    return False
