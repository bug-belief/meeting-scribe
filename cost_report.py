"""成本报告：把 usage.jsonl 汇总成人能看的一张表。

    python cost_report.py                  # 最近 7 天
    python cost_report.py --days 30        # 最近 30 天
    python cost_report.py --ref M20260101-100000-bbbb
    python cost_report.py --since 2026-10  # 整个 10 月
    python cost_report.py --top 10         # 花钱最多的 10 场
    python cost_report.py --json           # 喂给别的程序

「本地 vs 外部」是这份报告的重点：本地模型的 fair 价是「同样的活儿上云要付多少」，
不是真花掉的钱。真花掉的只有外部 API + 电费。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta

from costs import (
    aggregate,
    load_pricing,
    read_records,
    recent_refs,
    render_markdown,
    usage_log,
)


def _yuan(value: float) -> str:
    value = float(value or 0)
    if value == 0:
        return "0"
    if value < 0.01:
        return f"{value:.4f}"
    return f"{value:.3f}"


def _filter_days(records: list[dict], days: int) -> list[dict]:
    if days <= 0:
        return records
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    return [r for r in records if str(r.get("ts") or "") >= cutoff]


def _print_overview(records: list[dict], label: str) -> None:
    summary = aggregate(records)
    kinds = summary["kinds"]
    if not kinds:
        print(f"{label}：没有记账记录（{usage_log()}）")
        return

    print(f"\n{label}　共 {summary['records']} 条记录\n" + "─" * 62)
    print(f"{'项目':<10}{'次数':>6}{'用量':>22}{'折算价':>12}{'实付':>12}")
    print("─" * 62)

    for kind in ("llm", "asr", "tts"):
        bucket = kinds.get(kind)
        if not bucket:
            continue
        if kind == "llm":
            usage = (f"{summary['tokens']['prompt']:,}入/"
                     f"{summary['tokens']['completion']:,}出")
            name = "大模型"
        elif kind == "asr":
            usage = f"{summary['seconds']['audio'] / 60:.1f} 分钟音频"
            name = "语音转写"
        else:
            usage = f"{summary['chars']['tts']:,} 字符"
            name = "语音合成"
        print(f"{name:<10}{bucket['calls']:>6}{usage:>22}"
              f"{'¥' + _yuan(bucket['fair']):>12}{'¥' + _yuan(bucket['cost']):>12}")

    elec = summary["total"]["electricity"]
    print(f"{'电费':<10}{'':>6}{'本机推理':>22}{'—':>12}{'¥' + _yuan(elec):>12}")
    print("─" * 62)
    print(f"折算合计 ¥{_yuan(summary['total']['fair'])}　"
          f"实付合计 ¥{_yuan(summary['total']['cost'] + elec)}　"
          f"本地省下 ¥{_yuan(summary['total']['saved'])}")

    if summary["tokens"]["cache_hit"]:
        print(f"输入缓存命中 {summary['tokens']['cache_hit']:,} tokens"
              f"（已按命中价计费）")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="成本报告")
    parser.add_argument("--ref", default="", help="meeting_id 或会话 id")
    parser.add_argument("--since", default="", help="时间前缀，如 2026-10")
    parser.add_argument("--days", type=int, default=7, help="最近 N 天（默认 7，0=全部）")
    parser.add_argument("--top", type=int, default=0, help="按场次列出花钱最多的 N 场")
    parser.add_argument("--markdown", action="store_true", help="输出可粘进纪要的 Markdown")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = parser.parse_args(argv)

    if args.ref or args.since:
        records = read_records(ref=args.ref, since=args.since)
        label = args.ref or f"{args.since} 起"
        if not records:
            print(f"没有记录：{label}（{usage_log()}）")
            return 0
    else:
        records = _filter_days(read_records(), args.days)
        label = f"最近 {args.days} 天" if args.days > 0 else "全部"

    if args.json:
        print(json.dumps({"label": label, "summary": aggregate(records)},
                         ensure_ascii=False, indent=2))
        return 0

    if args.markdown:
        print(render_markdown(aggregate(records), title=f"成本 · {label}"))
        return 0

    if args.top:
        rows = recent_refs(limit=100000)
        rows.sort(key=lambda x: x["fair"], reverse=True)
        print(f"\n花钱最多的 {args.top} 场（按折算价）\n" + "─" * 62)
        print(f"{'场次':<30}{'记录':>6}{'折算':>10}{'实付':>10}")
        for row in rows[: args.top]:
            print(f"{row['ref']:<30}{row['records']:>6}"
                  f"{'¥' + _yuan(row['fair']):>10}{'¥' + _yuan(row['cost']):>10}")
        print()

    _print_overview(records, label)

    cfg = load_pricing()
    print(f"价格表：{cfg.get('currency', 'CNY')} ｜ "
          f"TTS {cfg['tts']['models'].get('cosyvoice2', {}).get('cloud_list')} 元/万字符 ｜ "
          f"ASR {cfg['asr']['models'].get('paraformer-zh', {}).get('cloud_list')} 元/小时 ｜ "
          f"电费 {cfg['compute']['watts']}W × ¥{cfg['compute']['yuan_per_kwh']}/度\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
