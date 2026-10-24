"""云端 ASR：把「整段识别」这一步交给 API，其余（VAD / 声纹 / 聚类）仍留本地。

## 为什么这么切

`diarize.py` 的六步里只有第 4 步（整段跑 paraformer 拿字符级时间戳）需要 ASR 模型；
第 1 步 VAD、第 2 步 cam++ 取声纹、第 3 步聚类、第 5 步投票、第 6 步加标点都不需要。

所以只要**替换 `engine.tokens()`** 这一个方法，「认人」能力就能完整保留：

    CloudEngine(FunASREngine)  →  tokens() 改成调云端，其余继承

而 `cam++`(28MB) 与 `paraformer`(848MB) 本来就是两个独立模型先后串联，
换掉后者不影响前者 —— 这是整个可移植方案成立的技术前提。

## 时间戳粒度是硬要求

`_assign_speakers()` 按「每个 token 与哪些声纹窗口重叠」投票。
云端只返回整段文本（无时间戳）时，声纹完全没法工作，只能退化为单人稿。
所以按精度降级并**在 notes 里说清楚**，绝不静默：

    words[]（词/字级）  → 精度与本地一致
    segments[]（句级）  → 句内字符按字数均分（说话人切换处会糊）
    text（无时间戳）    → 整段单人

## 兼容性

走 OpenAI 兼容的 `POST {base}/audio/transcriptions`（multipart 上传本地文件，
`response_format=verbose_json` + `timestamp_granularities[]=word`）。
这样同一份代码可以对接 OpenAI、硅基流动、Groq、以及本地 faster-whisper 服务，
只需改 `ASR_CLOUD_BASE_URL`。

阿里 DashScope 的 paraformer 文件识别是**异步 + 需要公网可访问的 URL**，
本地文件得先上传到某处，不适合「跑在自己机器上」的场景，暂不内置。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from utils import PipelineError, get_logger

logger = get_logger("cloud_asr")

# 这些是"整段识别"结果的承载方式，按精度从高到低
GRANULARITY_WORDS = "words"
GRANULARITY_SEGMENTS = "segments"
GRANULARITY_TEXT = "text"


@dataclass
class CloudConfig:
    """云端 ASR 的连接参数。全部来自 env，缺 key 时由调用方决定怎么降级。"""

    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "whisper-1"
    timeout: float = 120.0
    language: str = "zh"
    # 时间戳单位：auto / s / ms
    #   auto：有小数即判为秒；最大值 > 3600 判为毫秒；否则按秒（OpenAI 惯例）
    #   歧义确实存在（1200 可能是 1.2s 也可能是 1200s），所以留了显式开关。
    time_unit: str = "auto"

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/audio/transcriptions"

    @property
    def ready(self) -> bool:
        return bool(self.api_key.strip())


def parse_payload(payload: dict, time_unit: str = "auto") -> tuple[list[dict], str]:
    """从 API 返回里抽出带时间戳的单元，返回 (单元列表, 精度)。

    单元形如 {"text": "你", "start_ms": 120, "end_ms": 260}，
    多字符的词会被拆成单字（保持与本地 paraformer 一致的字符级粒度）。

    纯函数，不碰网络 —— 这是能被单测覆盖的关键。
    """
    if not isinstance(payload, dict):
        return [], GRANULARITY_TEXT

    # ① 词/字级：最理想
    words = payload.get("words")
    if isinstance(words, list) and words:
        units = _from_words(words, time_unit)
        if units:
            return units, GRANULARITY_WORDS

    # ② 句级：句内按字数均分
    segments = payload.get("segments")
    if isinstance(segments, list) and segments:
        units = _from_segments(segments, time_unit)
        if units:
            return units, GRANULARITY_SEGMENTS

    # ③ 只有文本：没有时间轴，调用方只能当单人稿
    text = (payload.get("text") or "").strip()
    if text:
        return [], GRANULARITY_TEXT
    return [], GRANULARITY_TEXT


def detect_time_unit(values: list) -> str:
    """判定这批时间戳是秒还是毫秒。返回 "s" 或 "ms"。

    为什么不能逐个值判：**单个值本身有歧义** —— 1200 既可能是 1.2 秒（毫秒），
    也可能是 20 分钟（秒）。所以必须看整批：

      ① 出现小数 → 秒（OpenAI verbose_json 给的是 float 秒）
      ② 最大值 > 3600 → 毫秒（秒制下 3600 已是 1 小时，而毫秒制很常见）
      ③ 其余 → 秒（跟着 OpenAI 惯例）

    实在判不出来时用 `ASR_CLOUD_TIME_UNIT=s|ms` 显式指定，别赌。
    """
    nums = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        nums.append(f)
    if not nums:
        return "s"
    if any(abs(f - round(f)) > 1e-9 for f in nums):
        return "s"
    if max(nums) > 3600:
        return "ms"
    return "s"


def _to_ms(value, unit: str, default: int = 0) -> int:
    if value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return int(round(v)) if unit == "ms" else int(round(v * 1000))


def _split_chars(text: str, start_ms: int, end_ms: int) -> list[dict]:
    """把一个多字单元按字数均分时间。

    为什么要拆：本地 paraformer 给的是单字时间戳，`_group_turns()` 靠
    「连续同说话人的 token」切 turn。保留多字会让 turn 边界变粗，
    说话人快速交替时糊成一片。拆开后行为与本地一致。
    """
    chars = [c for c in str(text) if not c.isspace()]
    if not chars:
        return []
    if len(chars) == 1:
        return [{"text": chars[0], "start_ms": start_ms, "end_ms": max(end_ms, start_ms)}]
    span = max(end_ms - start_ms, len(chars))
    out = []
    for i, ch in enumerate(chars):
        s = start_ms + span * i // len(chars)
        e = start_ms + span * (i + 1) // len(chars)
        out.append({"text": ch, "start_ms": s, "end_ms": max(e, s)})
    return out


def _time_unit_for(items: list, time_unit: str) -> str:
    """按配置决定单位；auto 时看整批数据判。"""
    if time_unit in ("s", "ms"):
        return time_unit
    flat = []
    for it in items:
        if isinstance(it, dict):
            flat.extend([it.get("start"), it.get("end")])
    return detect_time_unit(flat)


def _from_words(words: list, time_unit: str = "auto") -> list[dict]:
    unit = _time_unit_for(words, time_unit)
    out: list[dict] = []
    for w in words:
        if not isinstance(w, dict):
            continue
        text = w.get("word") or w.get("text") or ""
        s = _to_ms(w.get("start"), unit, 0)
        e = _to_ms(w.get("end"), unit, s)
        out.extend(_split_chars(text, s, max(e, s)))
    return out


def _from_segments(segments: list, time_unit: str = "auto") -> list[dict]:
    unit = _time_unit_for(segments, time_unit)
    out: list[dict] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = seg.get("text") or ""
        s = _to_ms(seg.get("start"), unit, 0)
        e = _to_ms(seg.get("end"), unit, s)
        out.extend(_split_chars(text, s, max(e, s)))
    return out


def request_payload(wav_path: Path, cfg: CloudConfig) -> dict:
    """真发一次 HTTP 请求，返回解析后的 JSON。

    与 `parse_payload` 分开：这个函数需要网络/密钥，不适合放进单测主路径；
    单测用 `parse_payload` + 本地 stub 服务覆盖。
    """
    if not cfg.ready:
        raise PipelineError(
            "云端 ASR 未配置 API key。请在 .env 里设置 ASR_CLOUD_API_KEY，"
            "或把 ASR_BACKEND 改回 funasr 用本地模型。"
        )
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover
        raise PipelineError(
            "缺少 httpx（云端 ASR 依赖）。执行：pip install -r requirements.txt"
        ) from exc

    wav_path = Path(wav_path)
    if not wav_path.is_file():
        raise PipelineError(f"音频文件不存在：{wav_path}")

    data = {
        "model": cfg.model,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "word",
    }
    if cfg.language:
        data["language"] = cfg.language

    t0 = time.time()
    try:
        with httpx.Client(timeout=cfg.timeout) as client:
            with wav_path.open("rb") as fh:
                resp = client.post(
                    cfg.endpoint,
                    headers={"Authorization": f"Bearer {cfg.api_key}"},
                    files={"file": (wav_path.name, fh, "audio/wav")},
                    data=data,
                )
    except httpx.HTTPError as exc:
        raise PipelineError(f"云端 ASR 请求失败（{cfg.endpoint}）：{exc}") from exc

    if resp.status_code != 200:
        # 只留前 300 字，避免把整页 HTML 或超长报错灌进日志
        raise PipelineError(
            f"云端 ASR 返回 {resp.status_code}：{resp.text[:300]}"
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise PipelineError(f"云端 ASR 返回的不是 JSON：{resp.text[:200]}") from exc

    logger.info("云端 ASR 完成：%.1fs（%s / %s）", time.time() - t0, cfg.model, wav_path.name)
    return payload


def cloud_tokens(wav_path: Path, settings) -> list:
    """云端整段识别 → `diarize.Token` 列表（毫秒时间戳）。

    `Token` 是 diarize 的识别单元，中文基本是单字。这里延迟 import，
    避免 diarize（会拉 funasr 相关模块）在纯云端模式下成为硬依赖。
    """
    from diarize import Token

    cfg = CloudConfig(
        api_key=getattr(settings, "asr_cloud_api_key", "") or "",
        base_url=getattr(settings, "asr_cloud_base_url", "") or "https://api.openai.com/v1",
        model=getattr(settings, "asr_cloud_model", "") or "whisper-1",
        timeout=float(getattr(settings, "asr_cloud_timeout", 120) or 120),
        language=getattr(settings, "asr_language", "zh") or "zh",
        time_unit=(getattr(settings, "asr_cloud_time_unit", "") or "auto").lower(),
    )
    payload = request_payload(wav_path, cfg)
    units, granularity = parse_payload(payload, cfg.time_unit)

    if granularity == GRANULARITY_TEXT:
        text = (payload.get("text") or "").strip() if isinstance(payload, dict) else ""
        raise PipelineError(
            "云端 ASR 只返回了纯文本、没有时间戳，说话人分离无法工作。"
            f"请换支持词级时间戳的模型（当前 {cfg.model}）。原始文本前 80 字：{text[:80]}"
        )
    if granularity == GRANULARITY_SEGMENTS:
        logger.warning(
            "云端 ASR 只给了句子级时间戳：说话人归属精度会下降"
            "（句内说话人切换处会糊）。要精确请换支持 word timestamps 的模型。"
        )

    tokens = [Token(text=u["text"], start=u["start_ms"], end=u["end_ms"]) for u in units]
    if not tokens:
        raise PipelineError("云端 ASR 没有识别出任何文本（可能是纯静音或音频损坏）")
    return tokens


def describe_granularity(payload: dict, time_unit: str = "auto") -> tuple[str, str]:
    """给调用方一句人话，用于写进 notes（降级必须可见，不能静默）。"""
    _, g = parse_payload(payload, time_unit)
    if g == GRANULARITY_WORDS:
        return g, ""
    if g == GRANULARITY_SEGMENTS:
        return g, "云端只提供句子级时间戳：说话人归属精度下降（句内切换会糊）"
    return g, "云端未提供时间戳：无法区分说话人，本次为单人稿"
