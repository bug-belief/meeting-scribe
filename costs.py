"""成本记账：每次 LLM / ASR / TTS 的用量落盘，并按公允价折算成人民币。

**两个口径必须分开**，否则看不出「本地部署到底省了多少」：

- `cost_yuan`：真掏出去的钱。本地模型恒为 0，只另计电费。
- `fair_yuan`：这些活儿若交给云端同款模型要花多少钱。

落盘 `data/usage.jsonl`，一行一次调用。按 `meeting_id`（归档）或
`session`（实时）归集，成本报告与纪要里的成本块都从这一个文件读，
不重复计算、也不各自维护一套统计。

价格全部外置在 `data/pricing.yaml`——厂商调价只改那个文件，不动代码。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("costs")

_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# 兜底价格（pricing.yaml 缺失或字段不全时用）
# 口径源自 2026-10-19 实测，单位见各键注释
# --------------------------------------------------------------------------- #
DEFAULTS: dict = {
    "currency": "CNY",
    "llm": {
        # 元 / 百万 token
        "models": {
            "deepseek-flash": {
                "vendor": "cloud",
                "peak": {"cache_hit": 0.1, "cache_miss": 3.0, "output": 9.0},
                "offpeak": {"cache_hit": 0.05, "cache_miss": 1.5, "output": 4.5},
            },
            "deepseek-pro": {
                "vendor": "cloud",
                "peak": {"cache_hit": 0.3, "cache_miss": 9.0, "output": 27.0},
                "offpeak": {"cache_hit": 0.15, "cache_miss": 4.5, "output": 13.5},
            },
        },
        # 谷时按北京时间：00:30–08:30，周末全天
        "offpeak_windows": [["00:30", "08:30"]],
        "weekend_offpeak": True,
        "fallback": "deepseek-flash",
    },
    "asr": {
        # 元 / 小时，对标阿里云百炼 Paraformer 录音文件识别
        "models": {
            "paraformer-zh": {"vendor": "local", "cloud_list": 0.288},
            "whisperx": {"vendor": "local", "cloud_list": 0.288},
        },
        "fallback": "paraformer-zh",
        # 市场均价（仅供参考，不参与 cost/fair 计算）
        "market_real_time_per_hour": 3.5,
    },
    "tts": {
        # 元 / 万字符，对标阿里云 CosyVoice 大模型；1 汉字 = 2 字符
        "models": {
            "cosyvoice2": {"vendor": "local", "cloud_list": 2.0},
        },
        "fallback": "cosyvoice2",
        "hanzi_counts_as": 2,
        "market_per_10k": 3.0,
    },
    "compute": {
        # 本地推理的电费：整机功耗 × 电价。占比很小，但写出来才诚实。
        # 默认是个中间值；**改成你自己机器的实测功耗**更准（也免得默认值透露我的机型）。
        "watts": 250,
        "yuan_per_kwh": 0.6,
    },
}


# --------------------------------------------------------------------------- #
# 价格表
# --------------------------------------------------------------------------- #
_cache: dict = {"path": None, "mtime": 0.0, "data": None}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _data_root() -> Path:
    """数据根目录（各租户目录的上一级）。

    优先 `TENANT_ROOT`，否则从 `PROCESSED_DIR` 反推——两种布局都要认：
      旧单租户 `/data/processed`           → `/data`
      多租户   `/data/tenants/me/processed` → `/data`
    不这么推的话，换个调用场景就会把账单写到 `tenants/` 里面去。
    """
    raw = (os.getenv("TENANT_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().parent
    raw = (os.getenv("PROCESSED_DIR") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if path.parent.parent.name == "tenants":
            return path.parent.parent.parent
        return path.parent
    return Path.home() / "data"


def pricing_file() -> Path:
    """`PRICING_FILE` 环境变量优先，否则 `<数据根>/pricing.yaml`。"""
    raw = (os.getenv("PRICING_FILE") or "").strip()
    return Path(raw).expanduser() if raw else _data_root() / "pricing.yaml"


def usage_log() -> Path:
    raw = (os.getenv("USAGE_LOG") or "").strip()
    return Path(raw).expanduser() if raw else _data_root() / "usage.jsonl"


def load_pricing(path: Path | None = None) -> dict:
    """读价格表；改文件后自动重载（按 mtime 判断），不用重启服务。"""
    path = Path(path) if path else pricing_file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return DEFAULTS

    global _cache
    if _cache["data"] is not None and _cache["path"] == str(path) and _cache["mtime"] == mtime:
        return _cache["data"]

    data = DEFAULTS
    try:
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            data = _deep_merge(DEFAULTS, loaded)
    except ImportError:
        logger.warning("没装 pyyaml，价格表用内置默认值：pip install pyyaml")
    except Exception as exc:
        logger.warning("价格表读取失败，用内置默认值：%s", exc)

    _cache = {"path": str(path), "mtime": mtime, "data": data}
    return data


def _band(when: datetime, cfg: dict) -> str:
    """峰时还是谷时。默认谷时 = 北京时间 00:30–08:30 + 周末全天。"""
    llm = cfg.get("llm") or {}
    if llm.get("weekend_offpeak", True) and when.weekday() >= 5:
        return "offpeak"
    minute = when.hour * 60 + when.minute
    for start, end in (llm.get("offpeak_windows") or []):
        try:
            sh, sm = (int(x) for x in str(start).split(":"))
            eh, em = (int(x) for x in str(end).split(":"))
        except (ValueError, AttributeError):
            continue
        lo, hi = sh * 60 + sm, eh * 60 + em
        if lo <= hi and lo <= minute < hi:
            return "offpeak"
        if lo > hi and (minute >= lo or minute < hi):  # 跨零点
            return "offpeak"
    return "peak"


def _price_for(model: str, table: dict, fallback: str) -> tuple[str, dict]:
    models = table.get("models") or {}
    if model in models:
        return model, models[model]
    # 前缀匹配：`deepseek-v4-flash` 也认成 `deepseek-flash`
    for key, value in models.items():
        if key in (model or "") or (model or "") in key:
            return key, value
    return fallback, models.get(fallback) or {}


# --------------------------------------------------------------------------- #
# 计费
# --------------------------------------------------------------------------- #
def tts_billable_chars(text: str, hanzi_as: int = 2) -> int:
    """CosyVoice 的计费口径：一个汉字算 2 个字符，标点与英文按 1 算。

    这个换算差一倍，写错整个成本结论就偏了（实测 TTS 占 87%，最容易算错）。
    """
    total = 0
    for ch in str(text or ""):
        if "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf":
            total += hanzi_as
        else:
            total += 1
    return total


def llm_cost(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_hit_tokens: int | None = None,
    when: datetime | None = None,
    cfg: dict | None = None,
) -> dict:
    """一次 LLM 调用的价钱。拿不到缓存命中数就按全部未命中算（宁可高估）。"""
    cfg = cfg or load_pricing()
    when = when or datetime.now()
    name, spec = _price_for(model, cfg.get("llm") or {}, cfg["llm"]["fallback"])
    band = _band(when, cfg)
    rate = spec.get(band) or spec.get("peak") or {}

    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    if cache_hit_tokens is None:
        hit, miss = 0, prompt_tokens
    else:
        hit = max(0, min(int(cache_hit_tokens), prompt_tokens))
        miss = prompt_tokens - hit

    yuan = (
        hit / 1e6 * float(rate.get("cache_hit", 0))
        + miss / 1e6 * float(rate.get("cache_miss", 0))
        + completion_tokens / 1e6 * float(rate.get("output", 0))
    )
    return {
        "vendor": spec.get("vendor", "cloud"),
        "model": name,
        "band": band,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit_tokens": hit,
        "cost_yuan": round(yuan, 6),
        "fair_yuan": round(yuan, 6),  # 外部 API：花的和公允价是同一笔
    }


def asr_cost(
    audio_sec: float,
    model: str = "",
    compute_sec: float = 0.0,
    cfg: dict | None = None,
) -> dict:
    """转写成本。本地跑 → cost 只有电费，fair 按云端同款小时价折算。"""
    cfg = cfg or load_pricing()
    name, spec = _price_for(model or "", cfg.get("asr") or {}, cfg["asr"]["fallback"])
    audio_sec = max(0.0, float(audio_sec or 0))
    hours = audio_sec / 3600.0
    fair = hours * float(spec.get("cloud_list", 0))
    electricity = _electricity(compute_sec or audio_sec, cfg)
    vendor = spec.get("vendor", "local")
    return {
        "vendor": vendor,
        "model": name,
        "audio_sec": round(audio_sec, 1),
        "compute_sec": round(float(compute_sec or 0), 1),
        "cost_yuan": round(0.0 if vendor == "local" else fair, 6),
        "fair_yuan": round(fair, 6),
        "electricity_yuan": round(electricity, 6),
    }


def tts_cost(
    text: str = "",
    chars: int | None = None,
    model: str = "",
    cached: bool = False,
    compute_sec: float = 0.0,
    cfg: dict | None = None,
) -> dict:
    """合成成本。缓存命中不重复计费——同一句话念第二遍不该收两次钱。"""
    cfg = cfg or load_pricing()
    name, spec = _price_for(model or "", cfg.get("tts") or {}, cfg["tts"]["fallback"])
    hanzi_as = int((cfg.get("tts") or {}).get("hanzi_counts_as", 2))
    billable = int(chars) if chars is not None else tts_billable_chars(text, hanzi_as)
    if cached:
        billable = 0
    fair = billable / 10000.0 * float(spec.get("cloud_list", 0))
    electricity = _electricity(compute_sec, cfg)
    vendor = spec.get("vendor", "local")
    return {
        "vendor": vendor,
        "model": name,
        "chars": billable,
        "text_chars": len(str(text or "")),
        "cached": bool(cached),
        "cost_yuan": round(0.0 if vendor == "local" else fair, 6),
        "fair_yuan": round(fair, 6),
        "electricity_yuan": round(electricity, 6),
    }


def _electricity(seconds: float, cfg: dict) -> float:
    comp = cfg.get("compute") or {}
    watts = float(comp.get("watts", 250))
    price = float(comp.get("yuan_per_kwh", 0.6))
    return max(0.0, float(seconds or 0)) / 3600.0 * watts / 1000.0 * price


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def log(kind: str, ref: str = "", scene: str = "", owner: str = "", **fields) -> dict:
    """记一笔用量。写盘失败绝不打断主流程——记账不该弄坏录音。

    kind: llm | asr | tts
    ref:  归档时是 meeting_id，实时时是 session id
    scene: assistant（代答）/ prompter（提词）/ summary（纪要）/ archive（转写）/ say（台词）
    """
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": kind,
        "ref": str(ref or ""),
        "scene": str(scene or ""),
        "owner": str(owner or ""),
        **fields,
    }
    # 结构校验只告警不阻断：账单字段写错是代码 bug，必须留下痕迹；
    # 但「账没记上」比「账记歪了」更难查，所以仍然照原样落盘。
    try:
        from schema_guard import validate

        validate("usage", record)
    except Exception as exc:
        logger.warning("用量记录结构异常（仍然落盘）：%s", exc)
    try:
        path = usage_log()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("用量落盘失败（忽略）：%s", exc)
    return record


def read_records(ref: str = "", since: str = "") -> list[dict]:
    """读回用量记录。`ref` 为空则全读；`since` 是 ISO 前缀（如 `2026-10`）。"""
    path = usage_log()
    if not path.is_file():
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ref and rec.get("ref") != ref:
                    continue
                if since and not str(rec.get("ts", "")).startswith(since):
                    continue
                out.append(rec)
    except OSError as exc:
        logger.warning("用量日志读取失败：%s", exc)
    return out


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def aggregate(records: list[dict]) -> dict:
    """把若干条记录压成一份账单。本地/外部两栏分开是重点。"""
    kinds: dict[str, dict] = {}
    total = {"cost": 0.0, "fair": 0.0, "saved": 0.0, "electricity": 0.0}
    local = {"cost": 0.0, "fair": 0.0}
    cloud = {"cost": 0.0, "fair": 0.0}
    tokens = {"prompt": 0, "completion": 0, "cache_hit": 0, "calls": 0}
    seconds = {"audio": 0.0, "compute": 0.0}
    chars = {"tts": 0, "source": 0}

    for rec in records:
        kind = rec.get("kind") or "other"
        cost = float(rec.get("cost_yuan") or 0)
        fair = float(rec.get("fair_yuan") or 0)
        elec = float(rec.get("electricity_yuan") or 0)
        bucket = kinds.setdefault(kind, {
            "calls": 0, "cost": 0.0, "fair": 0.0, "electricity": 0.0,
            "vendor": rec.get("vendor", ""), "model": rec.get("model", ""),
        })
        bucket["calls"] += 1
        bucket["cost"] += cost
        bucket["fair"] += fair
        bucket["electricity"] += elec

        (local if rec.get("vendor") == "local" else cloud)["cost"] += cost
        (local if rec.get("vendor") == "local" else cloud)["fair"] += fair
        total["cost"] += cost
        total["fair"] += fair
        total["electricity"] += elec

        if kind == "llm":
            tokens["calls"] += 1
            tokens["prompt"] += int(rec.get("prompt_tokens") or 0)
            tokens["completion"] += int(rec.get("completion_tokens") or 0)
            tokens["cache_hit"] += int(rec.get("cache_hit_tokens") or 0)
        elif kind == "asr":
            seconds["audio"] += float(rec.get("audio_sec") or 0)
            seconds["compute"] += float(rec.get("compute_sec") or 0)
        elif kind == "tts":
            chars["tts"] += int(rec.get("chars") or 0)
            chars["source"] += int(rec.get("text_chars") or 0)

    # 「省了多少」= 本地那部分若上云要花的钱
    total["saved"] = max(0.0, local["fair"] - local["cost"])

    for bucket in kinds.values():
        for key in ("cost", "fair", "electricity"):
            bucket[key] = round(bucket[key], 6)

    return {
        "records": len(records),
        "kinds": kinds,
        "local": {k: round(v, 6) for k, v in local.items()},
        "cloud": {k: round(v, 6) for k, v in cloud.items()},
        "total": {k: round(v, 6) for k, v in total.items()},
        "tokens": tokens,
        "seconds": {k: round(v, 1) for k, v in seconds.items()},
        "chars": chars,
    }


def _fmt_yuan(value: float) -> str:
    value = float(value or 0)
    if value == 0:
        return "¥0"
    if value < 0.0001:
        return f"¥{value:.6f}"
    if value < 0.01:
        return f"¥{value:.4f}"
    return f"¥{value:.3f}"


def render_markdown(summary: dict, title: str = "本次成本") -> str:
    """纪要末尾的成本块。用 HTML 注释做边界，重复写入会先擦掉旧的。"""
    kinds = summary.get("kinds") or {}
    if not kinds:
        return ""
    labels = {"llm": "大模型", "asr": "语音转写", "tts": "语音合成"}
    lines = [
        "<!-- cost:begin -->",
        f"## {title}",
        "",
        "> 本地模型按「云端同款托管价」折算，用于对照。真花钱的只有外部 API 与电费。",
        "",
        "| 项目 | 引擎 | 用量 | 折算价 | 实付 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for kind, bucket in kinds.items():
        usage = ""
        if kind == "llm":
            usage = f"{summary['tokens']['prompt']:,} 入 / {summary['tokens']['completion']:,} 出 token"
        elif kind == "asr":
            usage = f"{summary['seconds']['audio'] / 60:.1f} 分钟音频"
        elif kind == "tts":
            usage = f"{summary['chars']['tts']:,} 字符"
        lines.append(
            f"| {labels.get(kind, kind)} | {bucket.get('model') or '-'} | {usage} "
            f"| {_fmt_yuan(bucket['fair'])} | {_fmt_yuan(bucket['cost'])} |"
        )
    elec = summary["total"]["electricity"]
    lines.append(f"| 电费 | 本机推理 | - | - | {_fmt_yuan(elec)} |")
    lines.append("")
    lines.append(
        f"合计：折算 {_fmt_yuan(summary['total']['fair'])}，"
        f"实付 {_fmt_yuan(summary['total']['cost'] + elec)}；"
        f"本地部署省下约 **{_fmt_yuan(summary['total']['saved'])}**。"
    )
    lines.append("<!-- cost:end -->")
    return "\n".join(lines)


def append_to_report(report_path: Path, meeting_id: str, title: str = "本场累计成本") -> bool:
    """把成本块追加进 report.md；旧块先删，重复跑不会套娃。

    标题写「累计」是有意的：重跑一次转写/纪要，这一场的花费就真的又多了一笔，
    账不该抹掉。想看单次多少钱，用 `cost_report.py --top` 按时间对。
    """
    path = Path(report_path)
    if not path.is_file() or not meeting_id:
        return False
    records = read_records(ref=meeting_id)
    block = render_markdown(aggregate(records), title)
    if not block:
        return False
    try:
        text = path.read_text(encoding="utf-8")
        start = text.find("<!-- cost:begin -->")
        end = text.find("<!-- cost:end -->")
        if start >= 0 and end > start:
            text = (text[:start] + text[end + len("<!-- cost:end -->"):]).rstrip() + "\n\n"
        text = text.rstrip() + "\n\n" + block + "\n"
        path.write_text(text, encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("成本块写入失败：%s", exc)
        return False


def session_summary(session_id: str) -> dict:
    return aggregate(read_records(ref=session_id))


def meeting_summary(meeting_id: str) -> dict:
    return aggregate(read_records(ref=meeting_id))


def recent_refs(limit: int = 20, prefix: str = "") -> list[dict]:
    """最近有记账的 ref（默认全部）。用于前端列出「哪些场次有成本数据」。"""
    path = usage_log()
    if not path.is_file():
        return []
    seen: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ref = str(rec.get("ref") or "")
                if not ref or (prefix and not ref.startswith(prefix)):
                    continue
                item = seen.setdefault(ref, {"ref": ref, "records": 0, "cost": 0.0,
                                            "fair": 0.0, "first": rec.get("ts", ""),
                                            "last": rec.get("ts", "")})
                item["records"] += 1
                item["cost"] += float(rec.get("cost_yuan") or 0)
                item["fair"] += float(rec.get("fair_yuan") or 0)
                item["last"] = max(item["last"], str(rec.get("ts") or ""))
    except OSError as exc:
        logger.warning("用量日志读取失败：%s", exc)
        return []

    rows = sorted(seen.values(), key=lambda x: x["last"], reverse=True)[:limit]
    for row in rows:
        row["cost"] = round(row["cost"], 6)
        row["fair"] = round(row["fair"], 6)
    return rows


def relabel(old: str, new: str) -> int:
    """把记账记录里的临时 ref 改成正式名字，返回改了几条。

    转写 / 纪要发生在归档之前，那时 meeting_id 还不存在，只能先用工作目录名
    占位。归档时把这一批一起改写成 meeting_id，之后的成本查询才对得上。
    """
    if not old or not new or old == new:
        return 0
    path = usage_log()
    if not path.is_file():
        return 0
    changed = 0
    try:
        with _LOCK:
            lines = path.read_text(encoding="utf-8").splitlines()
            out = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    out.append(line)
                    continue
                if rec.get("ref") == old:
                    rec["ref"] = new
                    changed += 1
                out.append(json.dumps(rec, ensure_ascii=False))
            if changed:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
                os.replace(tmp, path)
    except OSError as exc:
        logger.warning("改写记账 ref 失败：%s", exc)
        return 0
    return changed


# --------------------------------------------------------------------------- #
# 演示：没有真实记录时也能看出长什么样
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="成本记账自检")
    parser.add_argument("--ref", default="", help="meeting_id 或 session")
    parser.add_argument("--since", default="", help="时间前缀，如 2026-10")
    args = parser.parse_args()

    if args.ref or args.since:
        s = aggregate(read_records(ref=args.ref, since=args.since))
        print(render_markdown(s) or "（没有记录）")
        print(json.dumps({k: s[k] for k in ("total", "local", "cloud", "tokens",
                                            "seconds", "chars")},
                         ensure_ascii=False, indent=2))
    else:
        cfg = load_pricing()
        print(f"价格表：{pricing_file()}（{'已读到' if _cache['data'] else '未读到，用默认'}）")
        print("LLM 高估（全按未命中）：", llm_cost("deepseek-flash", 2000, 100))
        print("TTS 60 字中文：", tts_cost("这是一句用来演示成本折算的中文台词，长度大约三十个字。"))
        print("ASR 100 秒：", asr_cost(100))
