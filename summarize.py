"""用兼容 OpenAI SDK 的云端 API（默认 DeepSeek）生成结构化会议纪要。

用法：
    python summarize.py --transcript work/会议/transcript.md
    python summarize.py --transcript work/会议/transcript.md --dry-run   # 只看分块方案，不花钱
    python summarize.py --all                                            # 批量处理 work 下所有转写稿

省 token 策略：
    短稿（≤ SUMMARY_DIRECT_CHARS 字符）→ 单次调用；
    长稿 → map（每块压成 ≤900 字要点）→ reduce（合并出终稿），块数多时自动做二级归并。
    报告头（时间/时长/说话人数）由本地拼接，模型只产出正文。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import llm_errors
import prompts
from config import SETTINGS, Settings, cli_settings, ensure_dirs
from utils import (
    PipelineError,
    estimate_tokens,
    get_logger,
    setup_logging,
)

logger = get_logger("summarize")

MAP_MAX_TOKENS = 700
MAX_TOKENS_CAP = 4096
REDUCE_INPUT_CHARS = 12000  # 二级归并阈值


# --------------------------------------------------------------------------- #
# 转写稿解析与分块
# --------------------------------------------------------------------------- #
@dataclass
class TranscriptDoc:
    path: Path
    meta: dict[str, str]
    body: str

    @property
    def title(self) -> str:
        return self.meta.get("title") or self.path.parent.name


def load_transcript(path: Path) -> TranscriptDoc:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip()
            body = parts[2]
    body = body.strip()
    if not body:
        raise PipelineError(f"转写稿为空：{path}")
    return TranscriptDoc(path=path, meta=meta, body=body)


def split_chunks(body: str, limit: int) -> list[str]:
    """按说话人轮次分块，尽量不把同一段话切断。"""
    if len(body) <= limit:
        return [body]

    chunks: list[str] = []
    buffer: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        if buffer:
            chunks.append("\n".join(buffer).strip())
            buffer, size = [], 0

    for line in body.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if len(line) > limit:  # 单段超长（无标点的长句）→ 按标点硬切
            flush()
            chunks.extend(_hard_split(line, limit))
            continue
        if size + len(line) > limit:
            flush()
        buffer.append(line)
        size += len(line) + 1
    flush()
    return [c for c in chunks if c.strip()]


def _hard_split(text: str, limit: int) -> list[str]:
    sentences = re.split(r"(?<=[。！？!?；;])", text)
    out: list[str] = []
    buf = ""
    for sentence in sentences:
        if len(buf) + len(sentence) > limit and buf:
            out.append(buf)
            buf = ""
        while len(sentence) > limit:  # 极端情况：单句仍超长
            out.append(sentence[:limit])
            sentence = sentence[limit:]
        buf += sentence
    if buf:
        out.append(buf)
    return out


# --------------------------------------------------------------------------- #
# LLM 客户端
# --------------------------------------------------------------------------- #
class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        # 关思考的降级链（见 _reasoning_effort）：none → minimal → off（不再传）
        # 记录哪一档被服务端接受了，后续请求直接用它，不重复试错。
        self._reasoning_mode: str | None = "none"
        self.usage = {
            "prompt_tokens": 0, "completion_tokens": 0, "calls": 0,
            # 思考型模型的 reasoning token（含在 completion_tokens 里，单独记一份便于看成本构成）。
            # 2026-10-22 实测：这个后端模型强制思考，一次提词能吃掉 190~250 tokens。
            "reasoning_tokens": 0,
            # DeepSeek 这类服务会把「命中前缀缓存」的输入单独计价，便宜 30~50 倍。
            # 拿不到就保持 0 且 known=False，成本模块会按全部未命中算（宁高估不低估）。
            "cache_hit_tokens": 0, "cache_hit_known": False,
        }

    def _ensure(self):
        if self._client is not None:
            return self._client
        if not self.settings.llm_api_key:
            raise PipelineError(
                "未配置 LLM_API_KEY。请在 .env 里填写（参考 .env.example），或先跑 --dry-run 验证分块。"
            )
        # 有些 key 只授权了 flash：直连时把这条约束写进代码，避免误改成 pro 烧钱
        if (
            "deepseek.com" in self.settings.llm_base_url
            and "flash" not in self.settings.llm_model.lower()
            and not self.settings.llm_allow_non_flash
        ):
            raise PipelineError(
                f"当前模型 {self.settings.llm_model} 不是 flash；如确需使用请设 LLM_ALLOW_NON_FLASH=true"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise PipelineError("未安装 openai SDK：pip install openai") from exc

        kwargs: dict = {
            "api_key": self.settings.llm_api_key,
            "base_url": self.settings.llm_base_url,
            "timeout": self.settings.llm_timeout,
            "max_retries": 0,  # 自己控制重试节奏与日志
        }
        if self.settings.llm_proxy:
            try:
                import httpx

                kwargs["http_client"] = httpx.Client(
                    proxy=self.settings.llm_proxy, timeout=self.settings.llm_timeout
                )
                logger.info("使用代理：%s", self.settings.llm_proxy)
            except Exception as exc:
                logger.warning("代理配置失败，忽略：%s", exc)
        self._client = OpenAI(**kwargs)
        return self._client

    def _reasoning_effort(self, want: bool | None = None) -> str | None:
        """决定这次请求往 `reasoning_effort` 里放什么。返回 None 表示不传该字段。

        **为什么不再用 `thinking` / `enable_thinking`**（2026-10-22 网关侧核实）：
        网关开了 `drop_params: true`，它只保留 LiteLLM **认识**的参数。
        `thinking` / `enable_thinking` / `chat_template_kwargs` 都不是标准字段，
        进网关就被**静默丢弃**，从来没到达后端 —— 等于没传。

        所以那几种写法不是"效果不好"，而是**完全无效**。
        （我原先以为 `thinking={"type":"disabled"}` 会"反而让思考更长"，
          那是采样噪声；网关侧六组对照证明它们与"什么都不传"表现一致。）
        现在统一用 OpenAI 标准字段 `reasoning_effort`，这才真正生效：
            "none"    → reasoning 0，比默认快约 30~40%
            "medium"  → 显式要它思考
            不传      → 用后端默认

        want：True 要求思考 / False 要求不思考 / None 跟随 LLM_THINKING
        """
        if want is None:
            mode = (self.settings.llm_thinking or "").strip().lower()
            if mode in {"off", "disable", "disabled", "none", "0", "false"}:
                want = False
            elif mode in {"on", "enable", "enabled", "1", "true"}:
                want = True
            else:
                return None      # 没配就不干预，用后端默认
        if not want:
            # 降级链：none → minimal → None（见 chat 的 except 分支）
            return None if self._reasoning_mode == "off" else self._reasoning_mode
        # 要思考：用标准字段显式表达，而不是发一个会被丢掉的 thinking 字段
        return "medium"

    def chat(self, messages: list[dict], max_tokens: int | None = None,
             temperature: float | None = None,
             reasoning: bool | None = None) -> str:
        """temperature 不传就沿用全局设置；提词这类「要稳定答案」的场景显式传 0。

        为什么要单独给口子：2026-10-21 实测同一个问题（回滚和压缩哪个便宜）
        在 temperature=0.3 下连问四次得到四个不同结论（压缩更便宜 / 要续就压缩 /
        能丢就回滚 / 回滚更便宜）。提词是给人照着说的，答案摇摆比答案简单更糟。

        reasoning：是否允许模型「思考」。
            None（默认）跟随 LLM_THINKING 设置
            False        强制关（实时提词/代答用，用户在等着开口）
            True         强制开（纪要总结用，慢点无所谓，质量优先）
        2026-10-22 实测（同一提词任务各 4 次）：
            不传           中位 1372ms，思考 97 tok
            none           中位  958ms，思考  0 tok   ← 提速 30%
            minimal        中位 1258ms，思考 63 tok
        """
        client = self._ensure()
        attempts = max(1, self.settings.llm_retries)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            kwargs: dict = {
                "model": self.settings.llm_model,
                "messages": messages,
                "max_tokens": min(max_tokens or self.settings.llm_max_tokens, MAX_TOKENS_CAP),
            }
            # 思考与否**只走 reasoning_effort 这一条路**。
            # 原来发 `extra_body={"thinking": ...}` 是纯粹的装饰 ——
            # 网关 `drop_params: true` 会把它丢掉，等于没传（2026-10-22 已核实）。
            effort = self._reasoning_effort(reasoning)
            if effort:
                kwargs["reasoning_effort"] = effort
            if effort and effort != "none":
                pass  # 思考模式下 temperature 无效，干脆不传
            else:
                kwargs["temperature"] = (
                    temperature if temperature is not None else self.settings.llm_temperature
                )
            try:
                response = client.chat.completions.create(**kwargs)
                reasoning_tokens = 0
                if response.usage:
                    self.usage["prompt_tokens"] += response.usage.prompt_tokens or 0
                    self.usage["completion_tokens"] += response.usage.completion_tokens or 0
                    # 思考型模型会把大部分 completion_tokens 花在 reasoning 上。
                    # 单独记一份，否则算成本时看到的"输出很多"其实是它在自言自语。
                    details = getattr(response.usage, "completion_tokens_details", None)
                    reasoning_tokens = int(getattr(details, "reasoning_tokens", 0) or 0)
                    if reasoning_tokens:
                        self.usage["reasoning_tokens"] = (
                            self.usage.get("reasoning_tokens", 0) + reasoning_tokens
                        )
                    hit = getattr(response.usage, "prompt_cache_hit_tokens", None)
                    if hit is None:
                        # OpenAI SDK 会把厂商私有字段收进 model_extra
                        extra_usage = getattr(response.usage, "model_extra", None) or {}
                        hit = extra_usage.get("prompt_cache_hit_tokens")
                    if hit is not None:
                        self.usage["cache_hit_tokens"] += int(hit or 0)
                        self.usage["cache_hit_known"] = True
                self.usage["calls"] += 1
                content = (response.choices[0].message.content or "").strip()
                if not content:
                    # 报错要说清是哪一种，别让人去猜"内容过滤"。
                    # 2026-10-22 实测：这个后端模型**强制思考**，
                    # max_tokens=256 时 reasoning 就吃掉 248，content 直接空。
                    # 所以真正常见的原因是 token 预算被思考耗尽，而不是合规过滤。
                    finish = getattr(response.choices[0], "finish_reason", "?")
                    if reasoning_tokens:
                        raise PipelineError(
                            f"API 返回空内容：{reasoning_tokens} 个 token 全花在思考上"
                            f"（finish_reason={finish}，max_tokens={kwargs.get('max_tokens')}）"
                            f" —— 加大 max_tokens，不是内容过滤"
                        )
                    raise PipelineError(
                        f"API 返回空内容（finish_reason={finish}，"
                        f"max_tokens={kwargs.get('max_tokens')}，无思考 token）"
                    )
                return content
            except PipelineError:
                raise
            except Exception as exc:
                message = str(exc).lower()
                # reasoning_effort 的取值不被支持（有的厂商只认 minimal/low/medium/high，
                # 不认 none）：沿降级链往下走，消耗重试次数之外单独重发。
                #   none → minimal → 不传（宁可慢一点，也不要因为参数不被接受而整条失败）
                if "reasoning_effort" in kwargs and any(
                    w in message for w in ("reasoning", "effort", "unsupported", "invalid", "not support")
                ):
                    prev = self._reasoning_mode
                    self._reasoning_mode = "minimal" if prev == "none" else "off"
                    logger.warning(
                        "服务端不接受 reasoning_effort=%s，降级为 %s：%s",
                        prev, self._reasoning_mode, exc,
                    )
                    continue
                last_error = exc
                wait = min(60, 5 * 2 ** (attempt - 1))
                logger.warning("API 调用失败（第 %d/%d 次）：%s；%ds 后重试", attempt, attempts, exc, wait)
                if attempt < attempts:
                    time.sleep(wait)

        # 分类在**源头**做一次（而不是让每个调用方各自再判一遍）：
        # message 保留原始英文给日志/ report.md 用，kind / hint 是给界面的一句话。
        # 见 llm_errors.py 抬头：不同错误的处置动作完全不同，不能一律「失败了」。
        info = llm_errors.classify(last_error)
        raise PipelineError(
            f"API 调用最终失败：{last_error}",
            kind=info["kind"],
            hint=info["hint"],
        ) from last_error


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _meta_line(doc: TranscriptDoc) -> str:
    fields = []
    for key, label in (("duration", "时长"), ("speakers", "说话人"), ("backend", "转写引擎")):
        if doc.meta.get(key):
            fields.append(f"{label} {doc.meta[key]}")
    return " ｜ ".join(fields)


def _clean_report(text: str) -> str:
    """去掉模型偶尔多嘴的包裹：代码围栏、重复一级标题。"""
    text = text.strip()
    text = re.sub(r"^```(?:markdown|md)?\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    text = re.sub(r"^#\s+.*\n+", "", text)
    return text.strip() + "\n"


def build_report(doc: TranscriptDoc, client: LLMClient, settings: Settings) -> tuple[str, str]:
    meta = _meta_line(doc)
    total_chars = len(doc.body)
    est = estimate_tokens(doc.body)
    logger.info("转写稿 %d 字，约 %d tokens", total_chars, est)

    if total_chars <= settings.summary_direct_chars:
        logger.info("单次总结（≤%d 字）", settings.summary_direct_chars)
        # 纪要**不用** reasoning=False：它是后台任务，慢十几秒没人等，
        # 质量比速度重要。跟随全局 LLM_THINKING 即可（当前 off）。
        # 提词/代答才需要强制关思考 —— 那里用户在等着开口。
        content = client.chat(
            [
                {"role": "system", "content": prompts.SYSTEM_PROMPT},
                {"role": "user", "content": prompts.user_direct(doc.body, meta)},
            ]
        )
        return _clean_report(content), "direct"

    chunks = split_chunks(doc.body, settings.summary_chunk_chars)
    logger.info("长稿分块：%d 块（每块约 %d 字）→ map-reduce", len(chunks), settings.summary_chunk_chars)
    notes: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        logger.info("  片段 %d/%d（%d 字）", index, len(chunks), len(chunk))
        note = client.chat(
            [
                {"role": "system", "content": prompts.MAP_PROMPT},
                {"role": "user", "content": prompts.user_map(chunk, index, len(chunks))},
            ],
            max_tokens=MAP_MAX_TOKENS,
        )
        notes.append(f"【片段{index}】\n{note}")

    merged = "\n\n".join(notes)
    rounds = 0
    while len(merged) > REDUCE_INPUT_CHARS and rounds < 2:
        rounds += 1
        logger.info("抽取结果 %d 字仍偏长，做第 %d 轮归并", len(merged), rounds)
        groups = split_chunks(merged, REDUCE_INPUT_CHARS // 2)
        merged = "\n\n".join(
            client.chat(
                [
                    {"role": "system", "content": prompts.MAP_PROMPT},
                    {"role": "user", "content": f"【中间归并 {i}/{len(groups)}】\n{g}"},
                ],
                max_tokens=MAP_MAX_TOKENS,
            )
            for i, g in enumerate(groups, start=1)
        )

    content = client.chat(
        [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": prompts.user_reduce(f"{prompts.REDUCE_PROMPT}\n\n{merged}", meta)},
        ]
    )
    return _clean_report(content), f"map-reduce({len(chunks)}块)"


def assemble(doc: TranscriptDoc, body: str, mode: str, client: LLMClient, settings: Settings) -> str:
    usage = client.usage
    header = [
        "---",
        f"title: {doc.title}",
        f"source: {doc.meta.get('source', '')}",
        f"duration: {doc.meta.get('duration', '')}",
        f"speakers: {doc.meta.get('speakers', '')}",
        f"asr: {doc.meta.get('backend', '')}",
        f"llm: {settings.llm_model}",
        f"mode: {mode}",
        f"tokens: prompt={usage['prompt_tokens']} completion={usage['completion_tokens']}",
        f"created: {datetime.now().isoformat(timespec='seconds')}",
        "---",
        "",
        f"# 会议纪要 · {doc.title}",
        "",
        f"> {_meta_line(doc)} ｜ 生成 {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        body.strip(),
        "",
    ]
    return "\n".join(header)


def summarize_transcript(
    transcript_md: Path,
    out_path: Path | None = None,
    settings: Settings = SETTINGS,
    dry_run: bool = False,
) -> Path:
    doc = load_transcript(Path(transcript_md))
    out_path = Path(out_path) if out_path else Path(transcript_md).parent / "report.md"

    if dry_run:
        chars = len(doc.body)
        chunks = split_chunks(doc.body, settings.summary_chunk_chars) if chars > settings.summary_direct_chars else [doc.body]
        print(f"源文件    : {doc.path}")
        print(f"正文字符  : {chars}  估算 tokens: {estimate_tokens(doc.body)}")
        print(f"模式      : {'direct' if len(chunks) == 1 else f'map-reduce({len(chunks)} 块)'}")
        print(f"模型      : {settings.llm_model} @ {settings.llm_base_url}")
        print(f"预计输入  : 约 {estimate_tokens(doc.body) + 250 * len(chunks)} tokens（含提示词）")
        for i, chunk in enumerate(chunks, start=1):
            print(f"  - 块{i}: {len(chunk)} 字 / 约 {estimate_tokens(chunk)} tokens ｜ {chunk[:40]}…")
        return out_path

    client = LLMClient(settings)
    t0 = time.time()
    body, mode = build_report(doc, client, settings)
    report = assemble(doc, body, mode, client, settings)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    logger.info(
        "纪要生成完成：%s（%.1fs，%d 次调用，prompt %d / completion %d tokens）",
        out_path.name, time.time() - t0, client.usage["calls"],
        client.usage["prompt_tokens"], client.usage["completion_tokens"],
    )
    _log_cost(settings, client, out_path.parent.name)
    return out_path


def _log_cost(settings: Settings, client: LLMClient, ref: str) -> None:
    """把这次纪要的 token 换算成钱记一笔。

    ref 先用工作目录名，归档时 pipeline 会改写成 meeting_id。
    记账失败绝不能影响纪要——整块吞异常。
    """
    try:
        from costs import llm_cost, log as log_cost

        billed = llm_cost(
            settings.llm_model,
            client.usage["prompt_tokens"],
            client.usage["completion_tokens"],
            client.usage["cache_hit_tokens"] if client.usage.get("cache_hit_known") else None,
        )
        log_cost("llm", ref=ref, scene="summary", owner=settings.owner, **billed)
    except Exception as exc:  # pragma: no cover - 记账永远是可选的
        logger.debug("成本记账跳过：%s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="云端 LLM 生成 Markdown 会议纪要")
    parser.add_argument("--transcript", help="transcript.md 路径")
    parser.add_argument("--all", action="store_true", help="处理 work 目录下所有 transcript.md")
    parser.add_argument("--output", help="report.md 输出路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印分块与 token 估算，不调用 API")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 report.md")
    args = parser.parse_args(argv)

    settings = cli_settings()
    ensure_dirs(settings)
    setup_logging(settings.logs_dir, settings.log_level, "summarize")

    if args.all:
        targets = sorted(settings.work_dir.glob("*/transcript.md"))
    elif args.transcript:
        targets = [Path(args.transcript).expanduser()]
    else:
        parser.error("需要 --transcript 或 --all")

    if not targets:
        logger.warning("没有找到转写稿")
        return 0

    failures = 0
    for target in targets:
        out_path = Path(args.output) if args.output else target.parent / "report.md"
        if out_path.exists() and not args.force and not args.dry_run:
            logger.info("已存在纪要，跳过（--force 覆盖）：%s", out_path)
            continue
        try:
            result = summarize_transcript(target, out_path, settings, args.dry_run)
            if not args.dry_run:
                logger.info("→ %s", result)
        except PipelineError as exc:
            failures += 1
            logger.error("[%s] %s", target, exc)
        except Exception:
            failures += 1
            logger.exception("[%s] 未预期异常", target)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
