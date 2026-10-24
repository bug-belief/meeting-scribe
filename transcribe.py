"""本地 ASR + 说话人分离。

用法：
    python transcribe.py --input audios_inbox/会议.m4a
    python transcribe.py --input audios_inbox --force          # 批量
    python transcribe.py --input a.m4a --backend whisperx

输出（落在 --output-dir，默认 work/<stem>/）：
    transcript.md    带时间戳与说话人标签的 Markdown
    transcript.json  结构化分段，供 summarize.py 使用
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from config import SETTINGS, Settings, cli_settings, ensure_dirs
from utils import (
    PipelineError,
    estimate_tokens,
    fmt_hms,
    get_logger,
    probe_audio_info,
    probe_duration,
    safe_stem,
    setup_logging,
    sha256_file,
    split_audio,
    to_wav16k,
    write_json,
)

logger = get_logger("transcribe")

# 助理（本地合成的语音分身）用的伪 speaker id。
# 双声道模式下左 0-999、右 1000-1999，取 9001 不会撞车。
ASSISTANT_SPEAKER = 9001
ASSISTANT_LABEL = "助理（我的声音）"
ASSISTANT_LABEL_SYS = "助理（电脑声音）"


def assistant_label(events: list[dict] | None, fallback: str = ASSISTANT_LABEL) -> str:
    """助理那句话该叫什么，取决于用的是谁的音色。

    克隆音色 → 「我的声音」（听起来就是他本人）；
    电脑音色 `sys:*` → 「电脑声音」（一听就是机器，标签也别再说成本人）。
    一次会话里混用两种就按出现最多的那种算。
    """
    names = [str(e.get("label") or "") for e in (events or []) if e.get("kind", "tts") == "tts"]
    names = [n for n in names if n]
    if names:
        return max(set(names), key=names.count)
    for e in events or []:
        if str(e.get("voice") or "").startswith("sys:"):
            return ASSISTANT_LABEL_SYS
    return fallback


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Segment:
    start: float
    end: float
    speaker: int
    text: str


@dataclass
class Transcript:
    source: Path
    duration: float
    backend: str
    segments: list[Segment] = field(default_factory=list)
    language: str = "zh"
    notes: list[str] = field(default_factory=list)
    # 声纹库识别出的人名：{speaker_id: "张三"}
    speaker_names: dict[int, str] = field(default_factory=dict)
    speaker_info: dict = field(default_factory=dict)

    @property
    def speaker_count(self) -> int:
        # 助理是「合成音」不是人，不计入说话人数
        ids = {seg.speaker for seg in self.segments if seg.speaker != ASSISTANT_SPEAKER}
        return len(ids) or 1


# --------------------------------------------------------------------------- #
# 后端 1：FunASR（中文首选，CPU 可跑，一条流水线出说话人）
# --------------------------------------------------------------------------- #
class FunASRBackend:
    name = "funasr"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:
            try:
                from diarize import FunASREngine
            except ImportError as exc:
                raise PipelineError(
                    "未安装 funasr。请在项目目录执行：bash deploy/bootstrap.sh deps-gpu"
                ) from exc
            self._engine = FunASREngine(self.settings)
        return self._engine

    def transcribe(self, wav_path: Path, duration: float, channel: str | None = None,
                   stereo_path: Path | None = None
                   ) -> tuple[list[Segment], list[str], dict]:
        """整段识别 + 自研说话人分离 + 声纹识别（原因见 diarize.py 顶部注释）。"""
        from diarize import diarize

        t0 = time.time()
        store = None
        if self.settings.speaker_enabled:
            try:
                from speakers import SpeakerStore

                store = SpeakerStore(self.settings.speaker_store, self.settings.speaker_max_templates)
            except Exception as exc:  # 库坏了不能拖垮转写
                logger.warning("声纹库加载失败，跳过人名识别：%s", exc)
        raw, notes, info = diarize(
            wav_path, duration, self.engine, self.settings, store=store, channel=channel,
            stereo_path=stereo_path,
        )
        if not raw:
            raise PipelineError("ASR 返回了结果但没有有效文本")

        segments = [
            Segment(start=start, end=max(end, start), speaker=int(spk), text=text)
            for start, end, spk, text in raw
        ]
        logger.info(
            "FunASR 转写完成：%d 段 / %.1fs（音频 %s）",
            len(segments), time.time() - t0, fmt_hms(duration),
        )
        return segments, notes, info


# --------------------------------------------------------------------------- #
# 后端 2：WhisperX（多语种更强，需 HF Token 才能分离说话人）
# --------------------------------------------------------------------------- #
class WhisperXBackend:
    name = "whisperx"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._asr = None
        self._align_models: dict[str, tuple] = {}
        self._diarizer = None

    def _device(self) -> str:
        if self.settings.asr_device in {"cpu", "cuda"}:
            return self.settings.asr_device
        try:
            import torch

            return "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:
            return "cpu"

    def transcribe(self, wav_path: Path, duration: float, channel: str | None = None,
                   stereo_path: Path | None = None
                   ) -> tuple[list[Segment], list[str]]:
        try:
            import whisperx
        except ImportError as exc:
            raise PipelineError(
                "未安装 whisperx。请执行：pip install -r requirements-whisperx.txt"
            ) from exc

        notes: list[str] = []
        device = self._device()
        compute_type = "float16" if device == "cuda" else "int8"

        if self._asr is None:
            logger.info("加载 WhisperX model=%s device=%s", self.settings.whisperx_model, device)
            self._asr = whisperx.load_model(
                self.settings.whisperx_model, device,
                compute_type=compute_type,
                language=self.settings.asr_language or None,
            )

        audio = whisperx.load_audio(str(wav_path))
        language = self.settings.asr_language or None
        t0 = time.time()
        result = self._asr.transcribe(
            audio, batch_size=self.settings.whisperx_batch_size, language=language
        )
        language = result.get("language") or language or "zh"
        logger.info("WhisperX 转写完成，耗时 %.1fs", time.time() - t0)

        # 对齐（拿到词级时间戳，说话人归属更准）
        try:
            if language not in self._align_models:
                self._align_models[language] = whisperx.load_align_model(
                    language_code=language, device=device
                )
            align_model, align_meta = self._align_models[language]
            result = whisperx.align(
                result["segments"], align_model, align_meta, audio, device,
                return_char_alignments=False,
            )
        except Exception as exc:
            notes.append(f"词级对齐失败，已跳过（{exc}）")
            logger.warning(notes[-1])

        # 说话人分离
        if self.settings.whisperx_diarize:
            if not self.settings.hf_token:
                notes.append("未配置 HF_TOKEN，跳过说话人分离，全部标记为 Speaker 1")
                logger.warning(notes[-1])
            else:
                try:
                    if self._diarizer is None:
                        self._diarizer = whisperx.DiarizationPipeline(
                            use_auth_token=self.settings.hf_token, device=device
                        )
                    kwargs = {}
                    if self.settings.whisperx_min_speakers:
                        kwargs["min_speakers"] = self.settings.whisperx_min_speakers
                    if self.settings.whisperx_max_speakers:
                        kwargs["max_speakers"] = self.settings.whisperx_max_speakers
                    diarize_df = self._diarizer(audio, **kwargs)
                    result = whisperx.assign_word_speakers(diarize_df, result)
                except Exception as exc:
                    notes.append(f"说话人分离失败，仅保留转写（{exc}）")
                    logger.warning(notes[-1])

        segments: list[Segment] = []
        for seg in result.get("segments", []):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            label = str(seg.get("speaker") or "SPEAKER_00")
            digits = "".join(ch for ch in label if ch.isdigit()) or "0"
            segments.append(
                Segment(
                    start=float(seg.get("start", 0.0)),
                    end=float(seg.get("end", 0.0)),
                    speaker=int(digits),
                    text=text,
                )
            )
        if not segments:
            raise PipelineError("WhisperX 未返回有效文本")
        return segments, notes


# --------------------------------------------------------------------------- #
# 后处理
# --------------------------------------------------------------------------- #
_CJK_PUNCT = "。！？；：，、）】」』"


def _overlap(a1: float, a2: float, b1: float, b2: float) -> float:
    return max(0.0, min(a2, b2) - max(a1, b1))


def _covered(segments: list[Segment], s: float, e: float) -> float:
    """区间 [s, e] 被现有句子盖住的比例（各段重叠取并集，不重复计数）。"""
    spans = sorted(
        (max(s, x.start), min(e, x.end))
        for x in segments if min(e, x.end) > max(s, x.start)
    )
    total = 0.0
    cur_a = cur_b = None
    for a, b in spans:
        if cur_a is None:
            cur_a, cur_b = a, b
        elif a <= cur_b:
            cur_b = max(cur_b, b)
        else:
            total += cur_b - cur_a
            cur_a, cur_b = a, b
    if cur_a is not None:
        total += cur_b - cur_a
    return total / max(0.01, e - s)


def _dedupe_repeat(text: str) -> str:
    """把「嗯，你说，我听着呢。嗯，你说，我听着呢。」这类原样复读压成一半。

    ASR 有时会把同一句播报听成两遍（声卡回环 + 麦克风漏音），
    合并相邻句后就成了复读。只在「前后半段完全一致」时动手，不动别的内容。
    """
    t = str(text or "").strip()
    if len(t) >= 6 and len(t) % 2 == 0:
        half = len(t) // 2
        if t[:half] == t[half:]:
            return t[:half]
    return t


def apply_assistant_timeline(
    segments: list[Segment], events: list[dict], label: str | None = None
) -> tuple[list[Segment], int, int]:
    """把「助理说过的话」从人声里摘出来。

    为什么不用声纹
    --------------
    助理的声音就是克隆本主的，声纹距离跟真人几乎重合，靠聚类必然混。
    但我们**知道它什么时候开口**（页面播报时上报的时间线），用区间对齐是确定的。

    两件事
    ------
    1. 标注 + 纠字：句子时间区间与播报区间重叠 ≥15% → 判给助理；
       同时**用播报日志里的原文替换 ASR 文本**。ASR 转录「克隆出来的自己」
       会把「11到17度」听成「一七度」、把「69%」听成「六和九」，
       而播报日志里存的就是要说的那句话，逐字准确，没有理由不用。
    2. 注入：助理的声音多数情况**录不进来**（TTS→虚拟声卡→微信，本地不回环）。
       这类台词在录音里没有声学痕迹，但对方确实听到了，所以要按时间线补进稿子，
       文本只取实际播出的那部分（spoken_chars）。
    """
    evs = [e for e in (events or []) if e.get("state") in {"done", "partial", "speaking"}]
    if not evs:
        return segments, 0, 0

    def _event_text(ev: dict) -> str:
        text = str(ev.get("text") or "")
        spoken = int(ev.get("spoken_chars") or 0)
        if spoken > 0:
            text = text[:spoken]
        return text.strip()

    marked = 0
    replaced = 0
    for ev in evs:
        s = float(ev.get("start") or 0.0)
        e = float(ev.get("end") or 0.0)
        if e <= s:
            continue
        exact = _event_text(ev)
        span = e - s
        hits = []
        for seg in segments:
            dur = max(0.01, seg.end - seg.start)
            ov = _overlap(seg.start, seg.end, s, e)
            if ov >= 0.15 * dur:
                hits.append((seg, ov))
        if not hits:
            continue
        hits.sort(key=lambda x: x[0].start)

        for seg, _ in hits:
            seg.speaker = ASSISTANT_SPEAKER
            marked += 1
        if not exact:
            continue

        if len(hits) == 1 and hits[0][1] >= 0.7 * span:
            # 一段就把整句播报盖住了：直接用日志原文，标点最完整
            seg = hits[0][0]
            if seg.text != exact:
                seg.text = exact
                replaced += 1
            continue

        # 一句播报被 ASR 切成几段：按时间比例把原文切开，避免每段都写整句
        # （以前就是这么重复的：「明天有小雨…」在稿子里出现两遍）
        # 切点要避开数字 / 百分号 / 英文单词，否则会出现「69，%」这种断法。
        def _snap(text: str, idx: int) -> int:
            idx = max(0, min(len(text), idx))
            if 0 < idx < len(text):
                prev, cur = text[idx - 1], text[idx]
                if (prev.isdigit() or prev in ".%") and (cur.isdigit() or cur in ".%"):
                    while idx < len(text) and (text[idx].isdigit() or text[idx] in ".%"):
                        idx += 1
                elif prev.isascii() and prev.isalpha() and cur.isascii() and cur.isalpha():
                    while idx < len(text) and text[idx].isascii() and text[idx].isalpha():
                        idx += 1
            return idx

        cuts = [0]
        closed = str(ev.get("state") or "done") != "partial"
        for seg, _ in hits[:-1]:
            ratio = min(1.0, max(0.0, (seg.end - s) / span))
            cuts.append(_snap(exact, int(round(ratio * len(exact)))))
        cuts.append(len(exact))
        for index, (seg, _) in enumerate(hits):
            piece = exact[cuts[index]:cuts[index + 1]].strip("，,。.、 ")
            if len(piece) >= 2:
                # 切点通常落在标点上，被 strip 掉后补回来，否则读起来断句全没了
                if index < len(hits) - 1 and piece[-1] not in "，,。！？；：":
                    piece += "，"
                elif index == len(hits) - 1 and closed and piece[-1] not in "。！？":
                    piece += "。"
                if seg.text != piece:
                    seg.text = piece
                    replaced += 1

    injected: list[Segment] = []
    for ev in evs:
        s = float(ev.get("start") or 0.0)
        e = float(ev.get("end") or 0.0)
        if e <= s:
            continue
        span = e - s
        # 录音里已经有这段（哪怕被 ASR 切成几段），就不用补——
        # 以前只比较「单段重叠 ≥30%」，一段 42.8→43.5 的空档就让整句又补了一遍，
        # 合并后就成了「嗯，你说，我听着呢。嗯，你说，我听着呢。」
        if _covered(segments, s, e) >= 0.45:
            continue
        text = _event_text(ev)
        if not text:
            continue
        injected.append(Segment(start=s, end=max(e, s + 0.4),
                                speaker=ASSISTANT_SPEAKER, text=text))

    for seg in segments:
        if seg.speaker == ASSISTANT_SPEAKER:
            seg.text = _dedupe_repeat(seg.text)

    out = sorted(list(segments) + injected, key=lambda x: (x.start, x.end))
    return out, marked, len(injected)


def _join_text(left: str, right: str) -> str:
    if not left:
        return right
    if left[-1].isascii() and left[-1].isalnum() and right[0].isascii() and right[0].isalnum():
        return f"{left} {right}"
    if left[-1] in _CJK_PUNCT:
        return left + right
    return left + right


def merge_turns(segments: list[Segment], max_gap: float = 2.0, max_chars: int = 220) -> list[Segment]:
    """把同一说话人、间隔很短的相邻句合并成一段，减少 Markdown 行数（也省 token）。"""
    merged: list[Segment] = []
    for seg in segments:
        if (
            merged
            and seg.speaker == merged[-1].speaker
            and seg.start - merged[-1].end <= max_gap
            and len(merged[-1].text) < max_chars
        ):
            merged[-1].end = max(merged[-1].end, seg.end)
            # 同一句被听成两遍时不要接成复读（助理段尤其容易：回环 + 漏音）
            if seg.text and seg.text.strip() != merged[-1].text.strip():
                merged[-1].text = _join_text(merged[-1].text, seg.text)
        else:
            merged.append(Segment(seg.start, seg.end, seg.speaker, seg.text))
    return merged


def render_markdown(transcript: Transcript, title: str, prefix: str | None = None) -> str:
    prefix = (prefix or SETTINGS.speaker_label_prefix or "Speaker").strip()
    names = transcript.speaker_names or {}
    ids = sorted({seg.speaker for seg in transcript.segments})
    legend = "、".join(names.get(i) or f"{prefix} {i + 1}" for i in ids) if ids else "未识别"
    if (transcript.speaker_info or {}).get("mode") == "dual":
        tag = "（按声道分轨判定：左声道=你、右声道=对方）"
    elif names:
        tag = "（★ 为声纹库自动匹配，可人工修正）"
    else:
        tag = ""
    lines = [
        "---",
        f"source: {transcript.source.name}",
        f"title: {title}",
        f"duration: {fmt_hms(transcript.duration)}",
        f"backend: {transcript.backend}",
        f"speakers: {transcript.speaker_count}",
        f"language: {transcript.language}",
        f"created: {datetime.now().isoformat(timespec='seconds')}",
        f"chars: {sum(len(s.text) for s in transcript.segments)}",
        "---",
        "",
        f"# 转写稿 · {title}",
        "",
        f"> 时长 {fmt_hms(transcript.duration)} ｜ 说话人 {transcript.speaker_count} 位 ｜ 本地 {transcript.backend} 转写",
        f"> 说话人：{legend}{tag}",
        "",
    ]
    for seg in transcript.segments:
        label = names.get(seg.speaker) or f"{prefix} {seg.speaker + 1}"
        lines.append(f"**[{fmt_hms(seg.start)}] {label}**：{seg.text}")
        lines.append("")
    if transcript.notes:
        lines.append("---")
        lines.append("")
        lines.append("> 转写提示：" + "；".join(transcript.notes))
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
# 后端 3：云端 ASR（只替换「整段识别」这一步，VAD/声纹/聚类仍用本地）
#
#   技术前提：cam++(28MB) 与 paraformer(848MB) 是两个独立模型先后串联，
#   换掉后者不影响前者。所以云端化之后「认人」能力还在（见 cloud_asr.py 顶部）。
#
#   两种运行形态：
#     ① 装了 requirements-asr-local.txt → 云端识别 + 本地声纹（推荐，约 1GB）
#     ② 只装 requirements.txt          → 云端识别 + 单人稿（无认人能力）
# --------------------------------------------------------------------------- #
class _InjectedCloudEngine:
    """把已经拿到的 token 注入 diarize 流程，避免重复请求云端。

    `diarize()` 内部只调 `engine.tokens()` 拿识别结果，其余（speech_regions /
    spk_embed / punc）都走本地模型。所以这里只包一层：tokens 直接返回注入值，
    其余属性转发给真正的 FunASREngine。
    """

    def __init__(self, settings: Settings, tokens: list):
        from diarize import FunASREngine

        self._inner = FunASREngine(settings)
        self._tokens = tokens

    def tokens(self, wav_path):  # noqa: ARG002 —— 签名要与 FunASREngine 一致
        return self._tokens

    def __getattr__(self, item):
        return getattr(self._inner, item)


def _funasr_available() -> bool:
    """本地模型能不能用？只看 funasr 装没装，不加载模型。"""
    import importlib.util

    return importlib.util.find_spec("funasr") is not None


def _tokens_to_single_speaker(tokens: list, min_turn_ms: int = 1000,
                              max_gap_ms: int = 700) -> list[tuple[float, float, int, str]]:
    """没有本地声纹时：按停顿切段，全部标 speaker 0。

    不做逐字一段：那样稿子会被切成几百行，读不了也存不下。
    按 max_gap_ms 的停顿归句，是「单人稿」最可读的形态。
    """
    if not tokens:
        return []
    groups: list[list] = [[tokens[0]]]
    for tok in tokens[1:]:
        if tok.start - groups[-1][-1].end <= max_gap_ms:
            groups[-1].append(tok)
        else:
            groups.append([tok])
    # 过短的段并入前一段（避免「嗯」「对」单独成行）
    merged: list[list] = []
    for g in groups:
        if merged and (g[-1].end - g[0].start) < min_turn_ms:
            merged[-1].extend(g)
        else:
            merged.append(g)
    return [
        (g[0].start / 1000.0, g[-1].end / 1000.0, 0, "".join(t.text for t in g).strip())
        for g in merged
        if "".join(t.text for t in g).strip()
    ]


class CloudBackend:
    name = "cloud"

    def __init__(self, settings: Settings):
        self.settings = settings

    def transcribe(self, wav_path: Path, duration: float, channel: str | None = None,
                   stereo_path: Path | None = None
                   ) -> tuple[list[Segment], list[str], dict]:
        from cloud_asr import cloud_tokens

        t0 = time.time()
        tokens = cloud_tokens(wav_path, self.settings)   # 唯一一次云端调用
        notes: list[str] = []

        if not _funasr_available():
            raw = _tokens_to_single_speaker(tokens)
            notes.append(
                "未安装 funasr：跳过说话人分离与声纹认人，本次为单人稿。"
                "需要「谁在说」请装 requirements-asr-local.txt。"
            )
            info = {"channel": (channel or self.settings.speaker_channel or "unknown"),
                    "names": {}, "assignments": []}
        else:
            from diarize import diarize

            store = None
            if self.settings.speaker_enabled:
                try:
                    from speakers import SpeakerStore

                    store = SpeakerStore(self.settings.speaker_store,
                                         self.settings.speaker_max_templates)
                except Exception as exc:      # 库坏了不能拖垮转写
                    logger.warning("声纹库加载失败，跳过人名识别：%s", exc)
            engine = _InjectedCloudEngine(self.settings, tokens)
            raw, notes, info = diarize(
                wav_path, duration, engine, self.settings, store=store,
                channel=channel, stereo_path=stereo_path,
            )

        if not raw:
            raise PipelineError("云端 ASR 返回了结果但没有有效文本")
        segments = [
            Segment(start=start, end=max(end, start), speaker=int(spk), text=text)
            for start, end, spk, text in raw
        ]
        logger.info("云端转写完成：%d 段 / %.1fs（音频 %s）",
                    len(segments), time.time() - t0, fmt_hms(duration))
        return segments, notes, info


# --------------------------------------------------------------------------- #
_BACKEND_CACHE: dict[str, object] = {}


def _make_backend(settings: Settings, backend: str | None):
    """同一进程内复用后端实例——模型只加载一次（冷启动约 10s），后续文件直接跑。"""
    name = (backend or settings.asr_backend).lower()
    if name not in {"funasr", "whisperx", "cloud"}:
        raise PipelineError(f"未知 ASR 后端：{name}（可选 funasr / whisperx / cloud）")
    cached = _BACKEND_CACHE.get(name)
    if cached is None:
        cached = {
            "funasr": FunASRBackend,
            "whisperx": WhisperXBackend,
            "cloud": CloudBackend,
        }[name](settings)
        _BACKEND_CACHE[name] = cached
    return cached


def release_models() -> bool:
    """卸载已加载的本地模型，把显存还给别的程序。返回是否真的释放了什么。"""
    released = False
    for backend in list(_BACKEND_CACHE.values()):
        engine = getattr(backend, "_engine", None)
        if engine is not None and hasattr(engine, "unload"):
            released = bool(engine.unload()) or released
        for attr in ("_asr", "_align_models", "_diarizer", "_model"):
            if getattr(backend, attr, None) is not None:
                setattr(backend, attr, None)
                released = True
    if released:
        import gc

        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                free, total = torch.cuda.mem_get_info()
                logger.info(
                    "已释放本地模型，显存可用 %.1f / %.1f GiB", free / 2**30, total / 2**30
                )
        except Exception:
            pass
    return released


def _unpack(result):
    """兼容「2 元组 / 3 元组」两种后端返回。"""
    if isinstance(result, tuple) and len(result) == 3:
        return result
    segments, notes = result
    return segments, notes, {}


def _name_dual_speakers(speaker_info: dict, self_name: str, peer_name: str) -> str | None:
    """diarize 已经按声道+声纹分好簇，这里只负责起名字。

    两种情形，取决于右声道到底有没有录到声音：
      ① 右声道有效（> CHANNEL_ACTIVE_MIN_SEC 有声）
         → 左声道依次「我、我2…」，右声道依次「对方、对方2…」；
           声纹库认得谁就用真名替换。
      ② 右声道基本静音（典型是电话外放：对方的声音从房间进麦克风）
         → 这时**左边出现两个人并不代表有两个"我"**。第一个给真名或「我」，
           其余一律叫「Speaker 2、Speaker 3」——
           它们只是「麦克风里的另一个人」，硬叫「我2」会把对方说成自己。
    """
    sides = {int(k): v for k, v in (speaker_info.get("channel_map") or {}).items()}
    if not sides:
        return None
    # 阈值定义在 diarize（音频分析归它管），这里延迟导入避免顶部拉起重依赖
    from diarize import CHANNEL_ACTIVE_MIN_SEC

    known = {int(k): v for k, v in (speaker_info.get("names") or {}).items()}

    activity = speaker_info.get("channel_activity") or {}
    right_sec = float(activity.get("right") or 0.0)
    right_alive = right_sec >= CHANNEL_ACTIVE_MIN_SEC

    counts = {"left": 0, "right": 0}
    extra = 0                      # 右声道不可靠时，麦克风侧第 2 人起的序号
    names: dict[int, str] = {}
    for spk in sorted(sides):
        side = sides[spk]
        counts[side] += 1
        if side == "left" and not right_alive and counts[side] > 1:
            # 麦克风里多出来的那个人：不知道是谁，就叫 Speaker N（N 从 2 起，
            # 因为第 1 个已经占了「我」这个位置）
            extra += 1
            names[spk] = known.get(spk) or f"Speaker {extra + 1}"
            continue
        base = self_name if side == "left" else peer_name
        names[spk] = known.get(spk) or (base if counts[side] == 1 else f"{base}{counts[side]}")

    speaker_info["names"] = {str(k): v for k, v in names.items()}
    left_n = sum(1 for v in sides.values() if v == "left")
    right_n = len(sides) - left_n
    if not right_alive:
        return (
            f"双声道分轨：右声道仅 {right_sec:.1f}s 有声，判定该路基本静音——"
            f"麦克风侧 {left_n} 人（第 2 人起标 Speaker N，不冒充「{self_name}」），"
            f"对方侧 {right_n} 人"
        )
    return f"双声道分轨：麦克风侧 {left_n} 人、对方侧 {right_n} 人，按声道与声纹逐句判定"


def _apply_channel_names(
    stereo_wav: Path,
    segments: list[Segment],
    speaker_info: dict,
    self_name: str,
    peer_name: str,
) -> str | None:
    """双声道分轨命名：左声道=本人麦克风、右声道=系统声音（对方）。

    不看声纹，直接按每个说话人簇在左右声道的能量占比判定。窄带、两人声音接近、
    重叠说话这些把声纹搞崩的场景，声道是硬证据——因为它来自两条物理上分开的流。
    返回写进 notes 的一句说明；失败返回 None（调用方保留声纹结果）。
    """
    try:
        import soundfile as sf

        data, sr = sf.read(str(stereo_wav), dtype="float32", always_2d=True)
    except Exception as exc:
        logger.warning("双声道分析失败，退回声纹结果：%s", exc)
        return None
    if data.shape[1] < 2 or not segments:
        return None

    # 逐段判定：每一句自己比左右能量，而不是整场一个簇说了算。
    # 之前的做法是「簇→声道」，一旦聚类把所有人并成一个簇（很常见），
    # 整场就只剩一个方向，你说的话也会被判给对方。
    pad = int(0.08 * sr)
    sides: list[str | None] = []
    for seg in segments:
        a = max(0, int(seg.start * sr) - pad)
        b = min(len(data), int(seg.end * sr) + pad)
        if b - a < int(0.05 * sr):
            sides.append(None)
            continue
        block = data[a:b]
        left = float((block[:, 0] ** 2).sum())
        right = float((block[:, 1] ** 2).sum())
        sides.append(None if left + right <= 1e-9 else ("left" if left >= right else "right"))

    # 孤立判定修正：同一个声纹簇里，零星被噪声翻面的句子按簇内多数归位
    votes: dict[int, dict[str, int]] = {}
    for seg, side in zip(segments, sides):
        if side and seg.speaker != ASSISTANT_SPEAKER:
            votes.setdefault(seg.speaker, {"left": 0, "right": 0})[side] += 1
    for i, (seg, side) in enumerate(zip(segments, sides)):
        if side is None:
            tally = votes.get(seg.speaker) or {}
            sides[i] = max(tally, key=tally.get) if tally else None

    if not any(sides):
        return None

    # 同一侧可能有多个簇（右声道里坐着好几个人）：按首次出现顺序编号，
    # 第一个叫「对方」，后面依次「对方2、对方3」。
    #
    # ⚠️ 但右声道若基本静音（电话外放，对方声音从麦克风进来），
    # 「麦克风侧有 2 人」不代表有 2 个「我」，第 2 人起改标 Speaker N。
    from diarize import CHANNEL_ACTIVE_MIN_SEC, _channel_activity

    act = speaker_info.get("channel_activity") or _channel_activity(data, sr)
    speaker_info["channel_activity"] = act
    right_alive = float(act.get("right") or 0.0) >= CHANNEL_ACTIVE_MIN_SEC

    key_to_id: dict[tuple[str, int], int] = {}
    names: dict[int, str] = {}
    mapping: dict[int, str] = {}
    seen: dict[str, int] = {"left": 0, "right": 0}
    last_side = "left"
    for seg, side in zip(segments, sides):
        # 助理段已经由时间线确定，不能再被声道重编号，否则会被改回「我/对方」
        if seg.speaker == ASSISTANT_SPEAKER:
            continue
        side = side or last_side
        last_side = side
        key = (side, seg.speaker)
        new_id = key_to_id.get(key)
        if new_id is None:
            new_id = len(key_to_id)
            key_to_id[key] = new_id
            seen[side] += 1
            if side == "left" and not right_alive and seen[side] > 1:
                # 麦克风里多出来的那个人：不知道是谁，别冒充「我」
                names[new_id] = f"Speaker {seen[side]}"
            else:
                base = self_name if side == "left" else peer_name
                names[new_id] = base if seen[side] == 1 else f"{base}{seen[side]}"
            mapping[new_id] = side
        seg.speaker = new_id

    speaker_info["names"] = {str(k): v for k, v in sorted(names.items())}
    speaker_info["mode"] = "dual"
    speaker_info["channel_map"] = {str(k): v for k, v in sorted(mapping.items())}
    logger.info("双声道分轨：%s（右声道 %.1fs 有声，%s）",
                mapping, float(act.get("right") or 0.0),
                "有效" if right_alive else "基本静音，麦克风侧多人不编号为我N")
    if not right_alive:
        return (
            f"双声道分轨：右声道仅 {float(act.get('right') or 0):.1f}s 有声，判定该路基本静音；"
            f"麦克风侧第 2 人起标 Speaker N（逐句能量判定，未经声纹比对）"
        )
    return (
        f"双声道分轨：左声道={self_name}、右声道={peer_name}，"
        f"按每句话的左右声道能量逐句判定，未经声纹比对"
    )


def transcribe_file(
    audio: Path,
    out_dir: Path,
    settings: Settings = SETTINGS,
    backend: str | None = None,
    split_minutes: int | None = None,
    keep_wav: bool | None = None,
    dual: bool = False,
    events: list[dict] | None = None,
) -> tuple[Path, Path]:
    """转写单个音频，返回 (transcript.md, transcript.json)。异常统一抛 PipelineError。

    dual=True 表示这是「双路分轨」录音（左=麦克风、右=系统声音），
    收尾时按声道判定说话人，而不是靠声纹聚类猜。

    events 是助理播报时间线（页面上的 tts_begin / tts_end），
    用来把「合成音」从人声里摘出来，并把没录进来的台词补进稿子。
    """
    audio = Path(audio)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(audio, settings.ffprobe_bin)
    if duration is None:
        raise PipelineError(f"无法读取音频时长，文件可能损坏或不是音频：{audio.name}")
    if duration < settings.min_duration_sec:
        raise PipelineError(f"音频过短（{duration:.1f}s < {settings.min_duration_sec}s），跳过")

    # 信道判定必须在转 16k 之前做：原始采样率/码率是判断"电话窄带"的唯一线索
    channel = (settings.speaker_channel or "unknown").lower()
    if channel == "unknown":
        try:
            from speakers import channel_for_audio

            raw_info = probe_audio_info(audio, settings.ffprobe_bin)
            channel = channel_for_audio(raw_info.get("sample_rate"), raw_info.get("bitrate_kbps"))
            logger.info("信道判定：%s（采样率 %s / 码率 %s kbps）", channel,
                        raw_info.get("sample_rate"), raw_info.get("bitrate_kbps"))
        except Exception as exc:
            logger.warning("信道判定失败，按 unknown 处理：%s", exc)
            channel = "unknown"

    logger.info("开始转写：%s（%.1f 分钟）", audio.name, duration / 60)
    asr_started = time.time()

    wav_dir = out_dir / "_wav"
    wav_path = to_wav16k(audio, wav_dir / f"{safe_stem(audio.stem)}.wav", settings.ffmpeg_bin)
    stereo_path: Path | None = None
    if dual:
        # 另外转一份双声道：ASR 只吃单声道，声道信息单独留给说话人判定
        try:
            stereo_path = to_wav16k(
                audio, wav_dir / f"{safe_stem(audio.stem)}-stereo.wav",
                settings.ffmpeg_bin, channels=2,
            )
        except PipelineError as exc:
            logger.warning("双声道转码失败，按普通录音处理：%s", exc)

    asr = _make_backend(settings, backend)
    limit = settings.asr_split_minutes if split_minutes is None else split_minutes
    notes: list[str] = []
    segments: list[Segment] = []
    speaker_info: dict = {}

    if limit and duration > limit * 60:
        # 超长音频才切片，避免一次性塞入模型；代价是说话人编号不跨片连续
        logger.warning("音频超过 %d 分钟，启用切片转写（说话人编号不跨片）", limit)
        parts = split_audio(wav_path, wav_dir / "parts", limit, settings.ffmpeg_bin)
        offset = 0.0
        for index, part in enumerate(parts, start=1):
            part_duration = probe_duration(part, settings.ffprobe_bin) or limit * 60
            part_segments, part_notes, _ = _unpack(asr.transcribe(part, part_duration, channel=channel))
            notes.extend(n for n in part_notes if n not in notes)
            for seg in part_segments:
                segments.append(
                    Segment(
                        start=seg.start + offset,
                        end=seg.end + offset,
                        speaker=(index - 1) * 100 + seg.speaker,
                        text=seg.text,
                    )
                )
            offset += part_duration
            logger.info("切片 %d/%d 完成", index, len(parts))
        notes.append(f"音频按 {limit} 分钟切片转写，同一说话人在不同切片中编号不同")
    else:
        segments, notes, speaker_info = _unpack(
            asr.transcribe(wav_path, duration, channel=channel, stereo_path=stereo_path)
        )

    # 助理（语音分身）时间线：必须在 merge_turns 之前。
    # merge 会按说话人把相邻句并成一段；如果先 merge，助理那几句会和前后人声
    # 合并成同一段，整段就被判成助理（连带把真人的话也吃掉）。
    # 反过来先标时间线，助理段拿到伪 id 9001，merge 时自然和人声分开。
    assistant_marked = assistant_injected = 0
    if events:
        segments, assistant_marked, assistant_injected = apply_assistant_timeline(segments, events)

    segments = merge_turns(segments)
    if stereo_path is not None and speaker_info:
        if speaker_info.get("mode") == "dual" and speaker_info.get("channel_map"):
            # diarize 已经按左右声道各自聚类，这里只起名字
            note = _name_dual_speakers(
                speaker_info, settings.dual_self_name, settings.dual_peer_name
            )
        else:
            # 声道分析在 diarize 里失败时的兜底：按每句的能量强行判左右
            note = _apply_channel_names(
                stereo_path, segments, speaker_info,
                settings.dual_self_name, settings.dual_peer_name,
            )
        if note:
            notes.append(note)

    # 助理时间线已经在 merge 之前处理完，这里只补一句说明
    if assistant_marked or assistant_injected:
        notes.append(
            f"助理时间线：{len(events)} 条播报记录，改写 {assistant_marked} 句、"
            f"补充 {assistant_injected} 句（合成音未进录音，文本取自播报日志）"
        )

    speaker_names = {int(k): v for k, v in (speaker_info.get("names") or {}).items()}
    if any(seg.speaker == ASSISTANT_SPEAKER for seg in segments):
        speaker_names[ASSISTANT_SPEAKER] = assistant_label(events)
    transcript = Transcript(
        source=audio,
        duration=duration,
        backend=asr.name,
        segments=segments,
        language=settings.asr_language or "zh",
        notes=notes,
        speaker_names=speaker_names,
        speaker_info=speaker_info,
    )

    md_path = out_dir / "transcript.md"
    json_path = out_dir / "transcript.json"
    title = audio.stem
    md_path.write_text(render_markdown(transcript, title, settings.speaker_label_prefix), encoding="utf-8")
    write_json(
        json_path,
        {
            "source": str(audio),
            "sha256": sha256_file(audio),
            "duration": duration,
            "backend": transcript.backend,
            "language": transcript.language,
            "speaker_count": transcript.speaker_count,
            "speaker_names": speaker_names,
            "speaker_mode": speaker_info.get("mode") or "voiceprint",
            "channel_map": speaker_info.get("channel_map") or {},
            "speaker_assignments": [
                {k: v for k, v in a.items() if k != "embedding"}
                for a in (speaker_info.get("assignments") or [])
            ],
            "notes": transcript.notes,
            "assistant_events": events or [],
            "segments": [asdict(seg) for seg in segments],
        },
    )
    # 带 embedding 的识别结果单独落盘（体积大、含生物特征，供人工确认后入库）
    if speaker_info.get("assignments"):
        write_json(out_dir / "speakers.json", speaker_info)

    text_chars = sum(len(seg.text) for seg in segments)
    logger.info(
        "转写完成：%d 段 / %d 字 / 约 %d tokens → %s",
        len(segments), text_chars, estimate_tokens(
            "".join(seg.text for seg in segments)
        ), md_path.name,
    )

    if not (settings.keep_wav if keep_wav is None else keep_wav):
        shutil.rmtree(wav_dir, ignore_errors=True)
    _log_cost(settings, asr.name, duration, time.time() - asr_started, out_dir.name)
    return md_path, json_path


def _log_cost(
    settings: Settings, backend: str, audio_sec: float, compute_sec: float, ref: str
) -> None:
    """记一笔转写成本。本地模型实付 0，折算价用来对照「上云要多少」。"""
    try:
        from costs import asr_cost, log as log_cost

        model = settings.funasr_model if backend == "funasr" else backend
        billed = asr_cost(audio_sec, model, compute_sec=compute_sec)
        log_cost("asr", ref=ref, scene="archive", owner=settings.owner, **billed)
    except Exception as exc:  # pragma: no cover - 记账永远是可选的
        logger.debug("成本记账跳过：%s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地音频转写 + 说话人分离")
    parser.add_argument("--input", required=True, help="音频文件或目录")
    parser.add_argument("--output-dir", help="输出目录；默认 work/<文件名>/")
    parser.add_argument("--backend", choices=["funasr", "whisperx"], help="覆盖 .env 的 ASR_BACKEND")
    parser.add_argument("--split-minutes", type=int, help="覆盖 ASR_SPLIT_MINUTES，0 表示不切片")
    parser.add_argument("--keep-wav", action="store_true", help="保留转换后的 16k wav")
    parser.add_argument("--dual", action="store_true",
                        help="双路分轨录音：左声道=麦克风（你）、右声道=系统声音（对方）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 transcript.md")
    args = parser.parse_args(argv)

    settings = cli_settings()
    ensure_dirs(settings)
    setup_logging(settings.logs_dir, settings.log_level, "transcribe")

    targets: list[Path] = []
    src = Path(args.input).expanduser()
    if src.is_dir():
        targets = sorted(
            p for p in src.iterdir()
            if p.is_file() and p.suffix.lower() in settings.audio_exts
        )
        if not targets:
            logger.warning("目录内没有可处理的音频：%s", src)
            return 0
    elif src.is_file():
        targets = [src]
    else:
        logger.error("输入不存在：%s", src)
        return 2

    failures = 0
    for audio in targets:
        out_dir = Path(args.output_dir) if args.output_dir else settings.work_dir / safe_stem(audio.stem)
        if (out_dir / "transcript.md").exists() and not args.force:
            logger.info("已存在转写结果，跳过（--force 可覆盖）：%s", out_dir / "transcript.md")
            continue
        try:
            transcribe_file(
                audio, out_dir, settings,
                backend=args.backend,
                split_minutes=args.split_minutes,
                keep_wav=args.keep_wav,
                dual=args.dual,
            )
        except PipelineError as exc:
            failures += 1
            logger.error("[%s] %s", audio.name, exc)
        except Exception:
            failures += 1
            logger.exception("[%s] 未预期异常", audio.name)

    if failures:
        logger.warning("完成，失败 %d / %d", failures, len(targets))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
