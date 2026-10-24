"""FunASR 推理引擎 + 自研说话人分离。

为什么不用 FunASR 内置的 sentence_info + spk_model？
----------------------------------------------------
实测 funasr 1.4.16（2026-10）在三件套 paraformer-zh + fsmn-vad + ct-punc + cam++ 下：

* `spk_mode="vad_segment"`：VAD 会把连续几句不同说话人的话合成一段，
  结果整段只有一个 spk，说话人分离实际失效（实测 39s 双人对话只分出 2 段，
  第二段把 4 句话全归给同一个人）。
* `spk_mode="punc_segment"`：句子文本整体错位一个字符
  （"链路。"变成"链路好。"，下一句开头少一个"好"），属于上游 bug。

所以这里自己实现：

1. `fsmn-vad` 找语音区间（不做说话人判断）；
2. 语音区间内按 3s 窗 / 1.5s 步长切片，用 `cam++` 逐片取声纹；
3. 全量声纹做层次聚类（余弦距离），得到"时间窗 → 说话人"时间线；
4. 整段音频跑一次 `paraformer`（**不加 vad/punc/spk**），拿到字符级时间戳与干净文本；
5. 每个字符按时间戳落在哪些窗里做加权投票，得到说话人；
6. 连续同说话人的字符归为一个 turn，最后对每个 turn 跑 `ct-punc` 加标点。

这样文本准确率等于"整段离线识别"，标点由整句生成，说话人边界由声纹决定。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from config import Settings
from utils import PipelineError, get_logger

logger = get_logger("diarize")

# ct-punc 单次输入上限，超长按此切分后再拼
PUNC_CHUNK_CHARS = 400


@dataclass
class Token:
    """一个识别单元（中文基本是单字），带绝对时间戳（毫秒）。"""

    text: str
    start: int
    end: int
    speaker: int = 0

    @property
    def mid(self) -> float:
        return (self.start + self.end) / 2


@dataclass
class Window:
    """一个声纹窗口。"""

    start: int
    end: int
    speaker: int = 0


def _resolve_device(wanted: str) -> str:
    if wanted in {"cpu", "cuda", "mps"}:
        if wanted == "cuda":
            return "cuda:0"
        return wanted
    try:
        import torch

        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


class FunASREngine:
    """模型只加载一次，供批量转写与后续实时链路复用。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.device = _resolve_device(settings.asr_device)
        self._asr = None
        self._vad = None
        self._punc = None
        self._spk = None

    # ---------------- 模型懒加载 ----------------
    @staticmethod
    def _auto_model(**kwargs):
        from funasr import AutoModel

        return AutoModel(disable_update=True, **kwargs)

    def asr(self):
        if self._asr is None:
            t0 = time.time()
            logger.info("加载 ASR：%s（device=%s）", self.settings.funasr_model, self.device)
            self._asr = self._auto_model(
                model=self.settings.funasr_model,
                # 必须挂 VAD：不挂的话整段音频会被当成一个 chunk，
                # 超过模型窗口的部分直接丢字（实测 39s 双人对话丢 35 字）
                vad_model=self.settings.funasr_vad,
                device=self.device,
                hub=self.settings.funasr_hub,
            )
            logger.info("ASR 就绪，%.1fs", time.time() - t0)
        return self._asr

    def vad(self):
        if self._vad is None:
            self._vad = self._auto_model(
                model=self.settings.funasr_vad,
                device=self.device,
                hub=self.settings.funasr_hub,
                # 单段上限压低，避免一口气切出几十秒的长段
                max_single_segment_time=max(5000, self.settings.diar_window_ms * 4),
            )
        return self._vad

    def punc(self):
        if self._punc is None:
            self._punc = self._auto_model(
                model=self.settings.funasr_punc,
                device=self.device,
                hub=self.settings.funasr_hub,
            )
        return self._punc

    def spk(self):
        if self._spk is None:
            self._spk = self._auto_model(
                model=self.settings.funasr_spk,
                device=self.device,
                hub=self.settings.funasr_hub,
            )
        return self._spk

    # ---------------- 基础能力 ----------------
    def speech_regions(self, wav_path) -> list[tuple[int, int]]:
        """返回语音区间（毫秒）。VAD 失败时退化为整段。"""
        try:
            out = self.vad().generate(input=str(wav_path), chunk_size=200)
            value = (out[0] or {}).get("value") if out else None
            regions = [(int(a), int(b)) for a, b in (value or []) if int(b) > int(a)]
            if regions:
                return regions
        except Exception as exc:
            logger.warning("VAD 失败，退化为整段：%s", exc)
        return []

    def tokens(self, wav_path) -> list[Token]:
        """整段识别，返回字符级时间戳。不带 vad/punc/spk，避免上游 bug。"""
        t0 = time.time()
        try:
            raw = self.asr().generate(
                input=str(wav_path),
                batch_size_s=self.settings.funasr_batch_size_s,
            )
        except ModuleNotFoundError as exc:
            # 缺本地模型时给**可执行**的指引，而不是只甩一句 "No module named 'funasr'"。
            # P2-9 陌生人验证实测：原文案只有一行 ERROR 日志，新人不知道下一步做什么。
            missing = getattr(exc, "name", "") or "funasr"
            raise PipelineError(
                f"本地转写需要 {missing}，当前环境没装。三选一：\n"
                "  ① 装本地模型（约 0.7–1 GB，可离线、可认人）：\n"
                "       pip install -r requirements-asr-local.txt\n"
                "  ② 改用云端 ASR（不用显卡，但没有说话人区分）：\n"
                "       ASR_BACKEND=cloud  ASR_CLOUD_API_KEY=<你的key>\n"
                "       接口要 OpenAI 兼容（/audio/transcriptions）；别家改 ASR_CLOUD_BASE_URL\n"
                "  ③ 只想起个 Web 面板看看：把 ASR_BACKEND 留着不管，实时字幕会提示同样的信息"
            ) from exc
        except Exception as exc:
            raise PipelineError(f"FunASR 识别失败：{exc}") from exc
        item = raw[0] if isinstance(raw, list) and raw else (raw or {})
        text = (item.get("text") or "").strip()
        stamps = item.get("timestamp") or []
        if not text:
            raise PipelineError("ASR 无任何输出，可能是纯静音或音频损坏")

        pieces = text.split()
        tokens: list[Token] = []
        if len(stamps) == len(pieces):
            for word, (s, e) in zip(pieces, stamps):
                tokens.append(Token(text=word, start=int(s), end=int(e)))
        else:
            # 没有时间戳就退化成均匀分布，避免整体失败
            logger.warning("时间戳数量不匹配（%d 字 / %d 段），退化为均匀分布", len(pieces), len(stamps))
            step = max(1, int(1000 * 30 / max(1, len(pieces))))
            for i, word in enumerate(pieces):
                tokens.append(Token(text=word, start=i * step, end=(i + 1) * step))
        logger.info("ASR 完成：%d 字，耗时 %.1fs", len(tokens), time.time() - t0)
        return tokens

    def embed(self, pcm):
        """单段 PCM（float32, 16k）→ 归一化声纹向量。"""
        import numpy as np

        out = self.spk().generate(input=pcm, fs=16000)
        emb = out[0]["spk_embedding"]
        emb = emb.detach().cpu().numpy() if hasattr(emb, "detach") else np.asarray(emb)
        emb = emb.reshape(-1).astype("float32")
        norm = float(np.linalg.norm(emb))
        return emb / norm if norm > 0 else emb

    def punctuate(self, text: str) -> str:
        """给一段纯文本加标点。超长自动切段。"""
        text = text.strip()
        if not text:
            return text
        model = self.punc()
        pieces = [text[i : i + PUNC_CHUNK_CHARS] for i in range(0, len(text), PUNC_CHUNK_CHARS)]
        out = []
        for piece in pieces:
            res = model.generate(input=piece)
            item = res[0] if isinstance(res, list) and res else (res or {})
            out.append((item.get("text") or piece).strip())
        return "".join(out)

    # ---------------- 显存回收 ----------------
    def unload(self) -> bool:
        """把四个模型全放掉，腾出显存（服务空闲时用，下次处理会重新加载）。"""
        import gc

        loaded = any(x is not None for x in (self._asr, self._vad, self._punc, self._spk))
        if not loaded:
            return False
        self._asr = self._vad = self._punc = self._spk = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        logger.info("已卸载本地模型（device=%s），下次处理会重新加载", self.device)
        return True


# --------------------------------------------------------------------------- #
# 说话人分离
# --------------------------------------------------------------------------- #
def _build_windows(regions: list[tuple[int, int]], total_ms: int, win: int, hop: int) -> list[tuple[int, int]]:
    """把语音区间切成声纹窗口。"""
    if not regions:
        regions = [(0, total_ms)]
    windows: list[tuple[int, int]] = []
    for start, end in regions:
        if end - start <= win * 1.5:
            windows.append((start, end))
            continue
        t = start
        while t < end:
            t_end = min(t + win, end)
            if t_end - t >= max(600, win // 3):
                windows.append((t, t_end))
            if t_end >= end:
                break
            t += hop
    return windows


def _cluster(vectors, min_sim: float, max_speakers: int, merge_sim: float = 0.55) -> list[int]:
    """余弦层次聚类，返回每个窗口的说话人编号。"""
    import numpy as np

    if len(vectors) == 1:
        return [0]
    try:
        from scipy.cluster.hierarchy import fcluster, linkage
    except ImportError:  # 极端情况：没有 scipy 就当成单人
        logger.warning("缺少 scipy，跳过说话人分离")
        return [0] * len(vectors)

    mat = np.vstack(vectors)
    dist = max(0.05, 1.0 - min_sim)  # 余弦距离阈值
    for attempt in range(6):
        tree = linkage(mat, method="average", metric="cosine")
        labels = np.asarray(fcluster(tree, dist, criterion="distance"))
        k = len(set(labels.tolist()))
        if k <= max_speakers or dist >= 0.85:
            break
        dist += 0.08  # 说话人过多 → 放宽阈值重聚
        logger.warning("聚出 %d 个说话人，超过上限 %d，放宽到 %.2f 重试", k, max_speakers, dist)

    labels = labels - 1  # fcluster 从 1 开始，转成 0 起
    labels = _refine(mat, labels)
    labels = _merge_by_centroid(mat, labels, merge_sim, max_speakers)
    labels = _drop_tiny(mat, labels)
    return [int(x) for x in labels.tolist()]


def _merge_by_centroid(mat, labels, merge_sim: float, max_speakers: int):
    """把质心过于相似的两个簇合并。

    电话录音是窄带 + 听筒二次编码，两人声纹会被压缩得很近，层次聚类容易
    把同一人拆成几簇（实测约 100s 电话录音在阈值 0.5 下散成 5 簇，其中 2 簇各
    只有 2~5 个窗口）。这里用质心相似度兜底合并，直到没有"明显是同一人"的簇。
    """
    import numpy as np

    labels = np.asarray(labels)
    for _ in range(12):
        k = int(labels.max()) + 1 if labels.size else 0
        if k <= 1:
            return labels
        cents = _centroids(mat, labels)
        sim = cents @ cents.T
        np.fill_diagonal(sim, -2.0)
        i, j = np.unravel_index(int(np.argmax(sim)), sim.shape)
        best = float(sim[i, j])
        # 两种情况停：簇数已在人数上限内，且最相似的一对也不算"同一人"
        if best < merge_sim and k <= max_speakers:
            break
        # 其余情况合并最相似的一对（同一人，或簇数超上限需要收敛）
        labels[labels == j] = i
        uniq = sorted(set(labels.tolist()))
        labels = np.array([uniq.index(x) for x in labels.tolist()])
        logger.info("合并相似簇 %d←%d（质心相似度 %.2f），当前 %d 人", i, j, best, len(uniq))
    return labels


def _centroids(mat, labels):
    import numpy as np

    cents = []
    for k in range(int(labels.max()) + 1):
        group = mat[labels == k]
        if len(group) == 0:  # 空簇留一个占位，避免下标错位
            cents.append(np.zeros(mat.shape[1], dtype="float32"))
            continue
        c = group.mean(axis=0)
        n = float(np.linalg.norm(c))
        cents.append(c / n if n > 0 else c)
    return np.vstack(cents)


def _refine(mat, labels, rounds: int = 4):
    """用质心做余弦 k-means 精修：跨说话人边界的窗口声纹混杂，层次聚类容易多分一类。"""
    import numpy as np

    labels = np.asarray(labels)
    if len(set(labels.tolist())) <= 1:
        return labels
    for _ in range(rounds):
        sims = mat @ _centroids(mat, labels).T
        new = sims.argmax(axis=1)
        if (new == labels).all():
            break
        labels = new
    # 重新压成 0..k-1
    uniq = sorted(set(labels.tolist()))
    return np.array([uniq.index(x) for x in labels.tolist()])


def _drop_tiny(mat, labels):
    """把只占 1 个窗口的孤立簇并到最相似的簇里。

    但窗口总数很少时不丢：短音频里 1 个窗口可能是真人说的唯一一句话，
    统计意义上的"噪声"判断在这里不成立（实测 6 个窗口时把对方第二个人吞掉）。
    """
    import numpy as np

    labels = np.asarray(labels)
    k = int(labels.max()) + 1
    if k <= 1:
        return labels
    if len(labels) < 8:
        return labels
    counts = np.bincount(labels, minlength=k)
    # 占比不到 3% 的簇（且不大于 2 个窗口）视为噪声或静音残片
    limit = max(2, int(round(0.03 * len(labels))))
    small = [i for i in range(k) if counts[i] <= limit and counts[i] < counts.max()]
    if not small or len(small) == k:
        return labels
    cents = _centroids(mat, labels)
    keep = [i for i in range(k) if i not in small]
    for idx in np.where(np.isin(labels, small))[0]:
        sims = [float(mat[idx] @ cents[j]) for j in keep]
        labels[idx] = keep[int(np.argmax(sims))]
    uniq = sorted(set(labels.tolist()))
    return np.array([uniq.index(x) for x in labels.tolist()])


def _assign_speakers(tokens: list[Token], windows: list[Window]) -> None:
    """按时间重叠度给每个字符投票。"""
    if not windows:
        return
    ws = sorted(windows, key=lambda w: w.start)
    starts = [w.start for w in ws]
    import bisect

    for tok in tokens:
        total = 0.0
        votes: dict[int, float] = {}
        i = max(0, bisect.bisect_right(starts, tok.start) - 2)
        while i < len(ws) and ws[i].start < tok.end:
            w = ws[i]
            overlap = min(tok.end, w.end) - max(tok.start, w.start)
            if overlap > 0:
                votes[w.speaker] = votes.get(w.speaker, 0.0) + overlap
                total += overlap
            i += 1
        if total > 0:
            tok.speaker = max(votes.items(), key=lambda kv: kv[1])[0]
        else:
            # 落在窗口缝隙里：就近取一个
            nearest = min(ws, key=lambda w: min(abs(tok.start - w.end), abs(w.start - tok.end)))
            tok.speaker = nearest.speaker


def _group_turns(tokens: list[Token], min_turn_ms: int, max_gap_ms: int = 700) -> list[list[Token]]:
    """连续同说话人的字符归为一个 turn，并吸收过短的抖动 turn。

    除说话人变化外，停顿超过 `max_gap_ms` 也强制切段。
    电话录音里一方声音经听筒进来偏弱，声纹常被判成同一人，若只按说话人切，
    整通电话会被并成一大段（实测约 100s 通话并成 1 段 60 秒），完全不可读。
    """
    turns: list[list[Token]] = []
    for tok in tokens:
        gap = tok.start - turns[-1][-1].end if turns else 0
        same = bool(turns) and turns[-1][0].speaker == tok.speaker
        if same and gap <= max_gap_ms:
            turns[-1].append(tok)
        else:
            turns.append([tok])

    # 过短 turn 合并到前一个（第一个则并入后一个）
    merged: list[list[Token]] = []
    for turn in turns:
        dur = turn[-1].end - turn[0].start
        if merged and dur < min_turn_ms:
            merged[-1].extend(turn)
        else:
            merged.append(turn)
    if len(merged) > 1:
        first = merged[0]
        if first[-1].end - first[0].start < min_turn_ms:
            merged[1][:0] = first
            merged.pop(0)
    return merged


# 一路声道「算存在」的最小有声时长。低于这个值就认为该路没录到东西——
# 电话外放时对方的声音从房间进麦克风，右声道全程只有 0.4s 底噪，
# 若还按「哪边大」判归属，两个人都会被算成左侧。
CHANNEL_ACTIVE_MIN_SEC = 1.0


def _channel_activity(data, sr: int) -> dict:
    """量左右声道各自「响了多久」（秒）。

    和 `_span_sides` 的区别：那个只看每个窗口哪边大，静音窗口会把 last 沿用到满；
    这个直接统计**超过门限**的时间，用来回答「这一路到底有没有声音」。

    门限取均值振幅 0.004（约 -48 dBFS）：比实测底噪（0.0007~0.0018）高一个量级，
    又远低于说话（0.02+），不会把轻声说话漏掉。
    """
    import numpy as np

    out = {"left": 0.0, "right": 0.0}
    if data is None or data.shape[1] < 2:
        return out
    win = max(1, int(sr * 0.1))          # 100ms 一窗
    gate = 0.004
    total = len(data)
    for start in range(0, total, win):
        block = data[start:start + win]
        if len(block) == 0:
            break
        sec = len(block) / sr
        # 用绝对值均值而不是 RMS：前者对短促讲话更敏感，且计算便宜
        if float(np.abs(block[:, 0]).mean()) > gate:
            out["left"] += sec
        if float(np.abs(block[:, 1]).mean()) > gate:
            out["right"] += sec
    return {k: round(v, 2) for k, v in out.items()}


def _span_sides(data, sr: int, spans) -> list[str | None]:
    """每个声纹窗口属于哪个声道。左=麦克风（本方），右=系统声音（对方）。

    窗口里两声道都没能量时沿用前一个判断（开头就没能量则返回 None）。
    """
    out: list[str | None] = []
    last: str | None = None
    for start, end in spans:
        a = max(0, int(start * sr / 1000))
        b = min(len(data), int(end * sr / 1000))
        if b - a < int(0.05 * sr):
            out.append(last)
            continue
        block = data[a:b]
        left = float((block[:, 0] ** 2).sum())
        right = float((block[:, 1] ** 2).sum())
        if left + right <= 1e-9:
            out.append(last)
            continue
        last = "left" if left >= right else "right"
        out.append(last)
    return out


def _cluster_stereo(vectors, sides, min_sim, max_speakers, merge_sim):
    """左右声道各自聚类，返回带侧别偏移的标签（左 0-999，右 1000-1999）。

    分开聚的好处：同一侧坐着两个人（免提外放时麦克风也会收到好几个人）时，
    不会因为另一侧能量更强而被整片吞掉。
    """
    offsets = {"left": 0, "right": 1000}
    labels = [0] * len(vectors)
    used = {"left": 0, "right": 0}
    for side in ("left", "right"):
        idx = [i for i, s in enumerate(sides) if s == side]
        if not idx:
            continue
        if len(idx) == 1:
            local = [0]
        else:
            local = _cluster(
                [vectors[i] for i in idx], min_sim, max_speakers, merge_sim
            )
        for j, i in enumerate(idx):
            labels[i] = offsets[side] + used[side] + int(local[j])
        used[side] += max(local) + 1
    return labels


def diarize(
    wav_path,
    duration: float,
    engine: FunASREngine,
    settings: Settings,
    store=None,
    channel: str | None = None,
    stereo_path=None,
):
    """执行「识别 + 说话人分离 + 声纹识别」，返回 (segments, notes, speaker_info)。

    segments 元素形如 (start_s, end_s, speaker, text)，由 transcribe.py 转成 Segment。
    speaker_info 形如 {"channel": "...", "names": {"0": "张三"}, "assignments": [...]}，
    其中 assignments 带每个簇的 embedding，供人工确认后入库。
    """
    import numpy as np
    import soundfile as sf

    notes: list[str] = []
    speaker_info: dict = {
        "channel": (channel or settings.speaker_channel or "unknown"),
        "names": {},
        "assignments": [],
    }
    assign_map: dict[int, object] = {}
    cluster_sides: dict[int, str] = {}   # 簇编号 → left / right（双声道时才有）
    tokens = engine.tokens(wav_path)
    total_ms = int(max(duration, tokens[-1].end / 1000.0) * 1000)

    # 1) 声纹窗口
    if settings.diar_enabled:
        try:
            regions = engine.speech_regions(wav_path)
            spans = _build_windows(regions, total_ms, settings.diar_window_ms, settings.diar_hop_ms)
            if not spans:
                raise PipelineError("没有可用的语音区间")
            pcm, sr = sf.read(str(wav_path), dtype="float32")
            if pcm.ndim > 1:
                pcm = pcm.mean(axis=1)
            if sr != 16000:
                raise PipelineError(f"声纹输入要求 16k，实际 {sr}")

            # 双声道时：先按窗口判左右，声纹向量也改用**该声道自己**的音频。
            # 用左右混音去算向量会把两侧声音搅在一起，同一侧的两个人都聚不开。
            span_sides: list[str | None] = [None] * len(spans)
            stereo_pcm = None
            if stereo_path is not None:
                try:
                    st, st_sr = sf.read(str(stereo_path), dtype="float32", always_2d=True)
                    if st.shape[1] >= 2 and st_sr == sr:
                        stereo_pcm = st
                        span_sides = _span_sides(st, sr, spans)
                        # 光知道「哪边大」不够：电话外放时对方的声音从房间进麦克风，
                        # 右声道全程近乎静音，但每个窗口仍是 left>right，
                        # 于是两个人都被判成 left。所以额外量一次「每边到底有多久在响」，
                        # 供后续命名与实时归属判断「这一路是否真的存在」。
                        activity = _channel_activity(st, sr)
                        speaker_info["channel_activity"] = activity
                        if activity["right"] < CHANNEL_ACTIVE_MIN_SEC:
                            notes.append(
                                f"右声道只有 {activity['right']:.1f}s 有声"
                                f"（< {CHANNEL_ACTIVE_MIN_SEC:.0f}s），判定该路基本静音："
                                f"对方的声音很可能也从麦克风进来了，不按声道分人"
                            )
                    else:
                        notes.append("双声道副本规格不符，声纹仍用混音")
                except Exception as exc:
                    notes.append(f"双声道副本读取失败，声纹仍用混音：{exc}")

            def _slice(index: int, start_ms: int, end_ms: int):
                a = int(start_ms * sr / 1000)
                b = int(end_ms * sr / 1000)
                side = span_sides[index]
                if stereo_pcm is not None and side:
                    return stereo_pcm[a:b, 0 if side == "left" else 1]
                return pcm[a:b]

            # 能量闸门：静音/极弱窗口不进聚类，否则会被当成"另一个人"
            # （实测真实电话录音末尾 2.2s 近乎无声，被单独聚成 Speaker 4）
            rms = []
            for start, end in spans:
                seg = pcm[int(start * sr / 1000) : int(end * sr / 1000)]
                rms.append(float(np.sqrt(np.mean(seg**2))) if len(seg) else 0.0)
            positive = [r for r in rms if r > 0]
            median = float(np.median(positive)) if positive else 0.0
            peak = max(positive) if positive else 0.0
            base_gate = max(0.002, median * settings.diar_min_rms_ratio)
            # 2026-10-19 修：只看中位数时，若整段大部分是静音（实测约 1 分钟录音中位仅 0.0014），
            # 门限会退化成 0.002 等于不筛，弱窗全被平均进质心，同人声纹被拉到 0.2~0.5。
            # 这里再加一道「相对最强窗」的门限，只留真正的语音窗。
            gate = max(base_gate, peak * 0.25)
            if sum(1 for r in rms if r >= gate) < 2:
                gate = base_gate  # 整段都很弱时退回原门限，避免把所有窗口筛光
            dropped = sum(1 for r in rms if r < gate)

            t0 = time.time()
            vectors = []
            for i, ((start, end), energy) in enumerate(zip(spans, rms)):
                if energy < gate:
                    vectors.append(None)  # 静音/弱音窗口，跳过
                    continue
                seg = _slice(i, start, end)
                if len(seg) < int(0.2 * sr):
                    vectors.append(None)  # 太短，跳过
                    continue
                vectors.append(engine.embed(seg))
            logger.info(
                "声纹提取完成：%d 个窗口（跳过静音/弱音 %d 个，能量阈值 %.4f），耗时 %.1fs",
                len(spans), dropped, gate, time.time() - t0,
            )

            keep = [i for i, v in enumerate(vectors) if v is not None]
            if len(keep) >= 2:
                win_sides = [span_sides[i] for i in keep]

                if any(win_sides):
                    labels = _cluster_stereo(
                        [vectors[i] for i in keep], win_sides,
                        settings.diar_min_sim,
                        settings.diar_max_speakers,
                        settings.diar_merge_sim,
                    )
                    for lab, side in zip(labels, win_sides):
                        if side:
                            cluster_sides[int(lab)] = side
                    left_n = len({int(lb) for lb, s in zip(labels, win_sides) if s == "left"})
                    right_n = len({int(lb) for lb, s in zip(labels, win_sides) if s == "right"})
                    note = (
                        f"双声道分轨聚类：麦克风侧 {left_n} 人、对方侧 {right_n} 人"
                        f"（窗口 {len(win_sides)} 个，跳过静音 {dropped} 个）"
                    )
                else:
                    labels = _cluster(
                        [vectors[i] for i in keep],
                        settings.diar_min_sim,
                        settings.diar_max_speakers,
                        settings.diar_merge_sim,
                    )
                    note = (
                        f"声纹窗口 {len(keep)} 个（跳过静音 {dropped} 个），"
                        f"聚类出 {len(set(labels))} 位说话人"
                    )

                windows = [Window(start=spans[i][0], end=spans[i][1], speaker=labels[j]) for j, i in enumerate(keep)]
                _assign_speakers(tokens, windows)
                n_spk = len(set(labels))
                counts = {s: labels.count(s) for s in set(labels)}
                if n_spk > 1 and max(counts.values()) / len(labels) > 0.8:
                    note += "；说话人占比高度不均，窄带/远场录音的分离可信度偏低"
                notes.append(note)
                logger.info(note)

                # 3) 声纹识别：把簇质心拿去和声纹库比对（库为空时全部返回 unknown）
                if store is not None and settings.speaker_enabled:
                    try:
                        labels_arr = np.asarray(labels)
                        cents = _centroids(np.vstack([vectors[i] for i in keep]), labels_arr)
                        stats: dict[int, dict] = {}
                        for w in windows:
                            st = stats.setdefault(w.speaker, {"ms": 0, "n": 0})
                            st["ms"] += w.end - w.start
                            st["n"] += 1
                        items = [
                            (k, cents[k], stats[k]["ms"] / 1000.0, stats[k]["n"])
                            for k in sorted(stats)
                        ]
                        found = store.identify_many(items, speaker_info["channel"])
                        # 双声道里「右声道」是对方那一路，但串音/外放会让它长得像自己
                        # （2026-10-19 实测：对方簇 vs 本人模板 0.564，已过 auto 线 0.50）。
                        # 这一路只给候选、不自动命名，避免把对方直接认成你本人。
                        if cluster_sides:
                            for a in found:
                                if cluster_sides.get(int(a.cluster)) == "right" and a.status == "auto":
                                    logger.info(
                                        "Speaker %d 属对方声道，%.2f 虽过线仍降级为候选（防串音误认）",
                                        a.cluster + 1, a.score,
                                    )
                                    a.status, a.name = "candidate", None
                        assign_map = {a.cluster: a for a in found}
                        hit = [a for a in found if a.name]
                        pending = [a for a in found if a.status == "candidate"]
                        if hit:
                            notes.append(
                                "声纹库匹配：" + "、".join(f"{a.name}({a.score:.2f})" for a in hit)
                            )
                        if pending:
                            top = pending[0]
                            hint = "、".join(f"{c.name}({c.score:.2f})" for c in top.candidates[:2])
                            notes.append(f"Speaker {top.cluster + 1} 待确认命中：{hint}")
                        logger.info(
                            "声纹识别：%d 簇，自动命名 %d，候选 %d，未知 %d",
                            len(found), len(hit), len(pending),
                            len([a for a in found if a.status == "unknown"]),
                        )
                    except Exception as exc:
                        notes.append(f"声纹库比对失败（不影响转写）：{exc}")
                        logger.warning(notes[-1])
            else:
                notes.append("语音片段过少，未能区分说话人")
        except Exception as exc:
            notes.append(f"说话人分离失败，退化为单人：{exc}")
            logger.warning(notes[-1])

    # 2) 按说话人切 turn 并加标点
    turns = _group_turns(tokens, settings.diar_min_turn_ms, settings.diar_max_gap_ms)
    # 按首次出现顺序重编号，保证第一句说话人固定是 Speaker 1；
    # 声纹命中同一个人的两个簇要并到同一编号（否则"张三"会分身成两个 Speaker）
    order: dict[object, int] = {}
    cluster_order: dict[int, int] = {}
    names: dict[int, str] = {}
    segments = []
    for turn in turns:
        text = "".join(t.text for t in turn).strip()
        if not text:
            continue
        try:
            text = engine.punctuate(text)
        except Exception as exc:
            notes.append(f"标点模型失败，保留无标点文本：{exc}")
        orig = turn[0].speaker
        guess = assign_map.get(orig)
        key = ("name", guess.name) if (guess is not None and guess.name) else ("cluster", orig)
        spk = order.setdefault(key, len(order))
        cluster_order.setdefault(orig, spk)
        side = cluster_sides.get(int(orig))
        if side:
            speaker_info.setdefault("channel_map", {}).setdefault(str(spk), side)
        if guess is not None and guess.name:
            names[spk] = guess.name
        segments.append((turn[0].start / 1000.0, turn[-1].end / 1000.0, spk, text))

    if assign_map:
        # 用最终 segments 的真实时长替换「窗口时长累加」（窗口有 50% 重叠，累加会翻倍）
        dur_by_spk: dict[int, float] = {}
        for start, end, spk, _text in segments:
            dur_by_spk[spk] = dur_by_spk.get(spk, 0.0) + max(0.0, end - start)
        per_speaker: dict[int, dict] = {}
        for orig, guess in assign_map.items():
            data = guess.to_dict()
            new_id = cluster_order.get(orig, orig)
            data["cluster"] = new_id
            data["label"] = data.get("name") or f"Speaker {new_id + 1}"
            data["duration"] = round(dur_by_spk.get(new_id, data.get("duration", 0.0)), 1)
            prev = per_speaker.get(new_id)
            if prev is None or data["score"] > prev["score"]:
                per_speaker[new_id] = data
        speaker_info["assignments"] = [per_speaker[k] for k in sorted(per_speaker)]
    speaker_info["names"] = {str(k): v for k, v in sorted(names.items())}
    if cluster_sides:
        speaker_info["mode"] = "dual"
    return segments, notes, speaker_info
