"""成本模块自检：算法口径必须逐条对得上，否则「省了多少钱」全是假的。

跑法（仓库根目录）：
    python tests/test_costs.py
不依赖 pytest，也不碰真实 usage.jsonl（用临时目录）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS = FAIL = 0


def check(name: str, got, want, tol: float = 1e-9) -> None:
    global PASS, FAIL
    ok = (abs(float(got) - float(want)) <= tol) if isinstance(want, (int, float)) else (got == want)
    if ok:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}：得到 {got!r}，期望 {want!r}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cost-test-"))
    os.environ["USAGE_LOG"] = str(tmp / "usage.jsonl")
    os.environ["PRICING_FILE"] = str(tmp / "missing.yaml")  # 故意不存在 → 用内置默认价

    import costs

    print("\n1) TTS 计费字符：一个汉字算 2 个字符")
    check("纯中文 3 字", costs.tts_billable_chars("你好啊"), 6)
    check("中文+标点+英文", costs.tts_billable_chars("你好，world"), 10)
    check("纯英文 5 字", costs.tts_billable_chars("hello"), 5)
    check("数字与百分号", costs.tts_billable_chars("69.5%"), 5)
    check("空串", costs.tts_billable_chars(""), 0)

    print("\n2) LLM 计价：峰谷 × 缓存命中")
    # 高峰：命中 0.1 / 未命中 3 / 输出 9（元每百万 token）
    peak = costs.llm_cost("deepseek-flash", 1_000_000, 1_000_000,
                          cache_hit_tokens=0, when=datetime(2026, 10, 19, 14, 0))
    check("高峰·全未命中 1M+1M", peak["cost_yuan"], 12.0, 1e-6)
    check("高峰档位", peak["band"], "peak")

    hit = costs.llm_cost("deepseek-flash", 1_000_000, 0,
                         cache_hit_tokens=1_000_000, when=datetime(2026, 10, 19, 14, 0))
    check("高峰·全命中 1M", hit["cost_yuan"], 0.1, 1e-6)

    off = costs.llm_cost("deepseek-flash", 1_000_000, 1_000_000,
                         cache_hit_tokens=0, when=datetime(2026, 10, 19, 3, 0))
    check("谷时（03:00）1M+1M", off["cost_yuan"], 6.0, 1e-6)
    check("谷时档位", off["band"], "offpeak")

    weekend = costs.llm_cost("deepseek-flash", 1_000_000, 0,
                             cache_hit_tokens=0, when=datetime(2026, 10, 18, 14, 0))
    check("周日 14:00 算谷时", weekend["band"], "offpeak")

    # 拿不到缓存信息时必须按全未命中算（宁可高估）
    unknown = costs.llm_cost("deepseek-flash", 1_000_000, 0, cache_hit_tokens=None,
                             when=datetime(2026, 10, 19, 14, 0))
    check("无缓存信息按未命中", unknown["cost_yuan"], 3.0, 1e-6)
    check("无缓存信息记 hit=0", unknown["cache_hit_tokens"], 0)

    pro = costs.llm_cost("deepseek-pro", 1_000_000, 0, cache_hit_tokens=0,
                         when=datetime(2026, 10, 19, 14, 0))
    check("pro 高峰未命中", pro["cost_yuan"], 9.0, 1e-6)

    print("\n3) ASR：本地只算电费，折算按云端小时价")
    asr = costs.asr_cost(3600, "paraformer-zh", compute_sec=1800)
    check("1 小时音频 fair", asr["fair_yuan"], 0.288, 1e-6)
    check("本地实付为 0", asr["cost_yuan"], 0)
    # 默认功耗（costs.py 的 POWER_W）× 0.6 元/度 × 0.5h
    check("半小时电费", asr["electricity_yuan"], 0.075, 1e-6)

    print("\n4) TTS：缓存命中不重复计费")
    tts = costs.tts_cost("你好啊")
    check("3 汉字 = 6 字符", tts["chars"], 6)
    check("6 字符 × 2 元/万", tts["fair_yuan"], 0.0012, 1e-8)
    cached = costs.tts_cost("你好啊", cached=True)
    check("缓存命中 chars 归零", cached["chars"], 0)
    check("缓存命中不计费", cached["cost_yuan"], 0)

    print("\n5) 落盘 / 归集 / 改挂")
    costs.log("llm", ref="M20991231-TEST", scene="summary", owner="me",
              **costs.llm_cost("deepseek-flash", 1000, 500))
    costs.log("asr", ref="M20991231-TEST", scene="archive", owner="me",
              **costs.asr_cost(600, "paraformer-zh", compute_sec=120))
    costs.log("tts", ref="sbx-1", scene="say", owner="me", **costs.tts_cost("这是一句测试台词"))
    check("读回 3 条", len(costs.read_records()), 3)
    check("按 ref 过滤", len(costs.read_records(ref="M20991231-TEST")), 2)

    moved = costs.relabel("sbx-1", "M20991231-TEST")
    check("改挂 1 条", moved, 1)
    check("改挂后共 3 条", len(costs.read_records(ref="M20991231-TEST")), 3)
    check("旧 ref 已空", len(costs.read_records(ref="sbx-1")), 0)

    summary = costs.meeting_summary("M20991231-TEST")
    check("汇总记录数", summary["records"], 3)
    check("本地部分实付 0", summary["local"]["cost"], 0)
    check("省下的钱 = 本地 fair", round(summary["total"]["saved"], 8),
          round(summary["local"]["fair"], 8))
    check("云部分实付 = fair", summary["cloud"]["cost"], summary["cloud"]["fair"])

    print("\n6) 纪要成本块：可重复写、不套娃")
    report = tmp / "report.md"
    report.write_text("# 会议纪要\n\n正文。\n", encoding="utf-8")
    costs.append_to_report(report, "M20991231-TEST")
    costs.append_to_report(report, "M20991231-TEST")
    text = report.read_text(encoding="utf-8")
    check("只有一块 cost:begin", text.count("<!-- cost:begin -->"), 1)
    check("正文没被吃掉", "正文。" in text, True)
    check("含省下多少", "本地部署省下" in text, True)

    print("\n7) 账本文件是合法 JSONL")
    lines = [x for x in Path(os.environ["USAGE_LOG"]).read_text(encoding="utf-8").splitlines() if x.strip()]
    parsed = all(isinstance(json.loads(x), dict) for x in lines)
    check("逐行可解析", parsed, True)
    check("行数", len(lines), 3)

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {FAIL} 项\n临时目录：{tmp}\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
