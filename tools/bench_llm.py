#!/usr/bin/env python3
"""给这个应用挑一个合适的 LLM 模型。

**为什么需要这个**：不同模型对 thinking 的处理差别很大，
有的模型**强制思考**，一次提词 reasoning 就吃掉 190~256 tokens，导致两个后果：
  ① 慢：1.9~2.4s（用户的体感是"说完三秒才出提示"）
  ② 空内容：max_tokens 给小了，正文直接被吃光
三种关 thinking 的写法全被后端忽略，应用侧无法规避 —— 只能换模型。
但平台上有哪些模型、哪个真的快、哪个也偷偷思考，光看名字判断不了，必须实测。

**怎么用**：
    # 用应用当前的 .env（默认）
    python tools/bench_llm.py

    # 指定别的 key / 地址（比如平台新开的虚拟 key）
    LLM_API_KEY=sk-xxx LLM_BASE_URL=https://api.example.com/v1 python tools/bench_llm.py

    # 只测某几个模型
    python tools/bench_llm.py --models model-a,model-b

**判定标准**（按重要性排序）：
    1. 空内容率必须为 0 —— 空了等于功能不可用
    2. reasoning_tokens 越低越好 —— 这是慢和贵的直接来源
    3. 延迟越低越好
    4. 答案要能用 —— 用真实提词任务打分，不是"说个字"
"""
from __future__ import annotations

import argparse
import os
import re as _re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_env() -> tuple[str, str]:
    """优先用环境变量，其次读项目 .env。"""
    key = os.environ.get("LLM_API_KEY", "").strip()
    base = os.environ.get("LLM_BASE_URL", "").strip()
    env_file = ROOT / ".env"
    if (not key or not base) and env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("LLM_API_KEY=") and not key:
                key = line.split("=", 1)[1].strip()
            if line.startswith("LLM_BASE_URL=") and not base:
                base = line.split("=", 1)[1].strip()
    # 没配时给一个明显是占位的地址，而不是静默指向某个真实服务：
    # 直接报「连不上」比悄悄打到别人机器上更好排查。
    return key, base or "http://127.0.0.1:1234/v1"


def call(key: str, base: str, model: str, messages: list[dict],
         max_tokens: int, temperature: float = 0.0,
         extra: dict | None = None) -> dict:
    """发一次请求，返回统一结构（含 reasoning tokens 与耗时）。"""
    from openai import OpenAI

    client = OpenAI(base_url=base, api_key=key, timeout=120)
    t0 = time.perf_counter()
    try:
        kw = {"model": model, "messages": messages,
              "max_tokens": max_tokens, "temperature": temperature}
        if extra:
            kw.update(extra)
        r = client.chat.completions.create(**kw)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:160], "ms": int((time.perf_counter() - t0) * 1000)}

    ms = int((time.perf_counter() - t0) * 1000)
    ch = r.choices[0]
    content = (ch.message.content or "").strip()
    reasoning = 0
    if r.usage and getattr(r.usage, "completion_tokens_details", None):
        reasoning = int(getattr(r.usage.completion_tokens_details, "reasoning_tokens", 0) or 0)
    return {
        "ok": True,
        "content": content,
        "reasoning_tokens": reasoning,
        "completion_tokens": (r.usage.completion_tokens if r.usage else 0),
        "finish": ch.finish_reason,
        "ms": ms,
    }


# 真实提词任务：判断"这个模型能不能干这活"，比"说一个字"有信息量得多。
#
# ⚠️ 光看延迟会选错模型（2026-10-22 实测教训）：
# 当时有个「快」的模型中位 500ms，比在用的 869ms 快一倍，
# 但它**只给断言、不给依据**，而且对同一个问题给出的答案可能是错的。
# 所以这里必须有**有标准答案**的任务，否则"快"会掩盖"不准"。
TASKS = [
    {
        "name": "提词（技术取舍）",
        "system": "你是用户的随身提词器。给他一句极短的提示，最多 2 行，每条不超过 16 字。"
                  "先给定论，再给判断依据，不要模棱两可。只输出提示本身。",
        "user": "最近的对话：\n"
                "Token 成本怎么控？最贵的不是读了多少，是缓存被击穿和对话历史积累。\n"
                "贵的模型只做编排，便宜的模型做批量。\n\n"
                "他现在想问的是：\n回滚和压缩哪个便宜？\n\n"
                "请只输出给他的话术提示，最多 2 行。",
        "min_chars": 8,
    },
    {
        "name": "提词（要数字）",
        "system": "你是用户的随身提词器。给他一句极短的提示，最多 2 行，每条不超过 16 字。"
                  "对话里没出现过的数字一律提示「先确认」。",
        "user": "最近的对话：\n对方问：这个月发票什么时候能开出来？\n"
                "他现在想问的是：\n发票什么时候能开\n\n"
                "请只输出给他的话术提示，最多 2 行。",
        "min_chars": 6,
    },
    {
        # 这一条有**客观正确答案**：15 日发货 + 物流 3 天 = **18 日到**。
        # 用它区分「会照着算」和「只会复述」——延迟再快，答非所问也没用。
        #
        # ⚠️ 判据必须写准（第一版写错了，结论完全反过来）：
        #    错误写法 must_not="3月(1[0-46-9]|2[0-9]|3[01])日"
        #    本意是"排除 15 日以外"，但 [0-46-9] 把 **18 也包含了**，
        #    于是把"算对了 18 日"判成错、把"没算"判成对 —— 假绿。
        # 正确做法：must_match 要求必须出现 18，must_not 只排除明显错的日期。
        "name": "提词（有标准答案）",
        "system": "你是用户的随身提词器。给他一句极短的提示，最多 2 行。"
                  "只能使用对话里出现过的事实，不许添加新数字。",
        "user": "最近的对话：\n"
                "对方：这批货什么时候能到？\n"
                "我：合同上写的是 3 月 15 日发货，物流大概 3 天。\n\n"
                "他现在想问的是：\n对方追问具体哪天能到手上\n\n"
                "请只输出给他的话术提示，最多 2 行。",
        "min_chars": 4,
        # 必须算出到货日 18 号（换个说法：十八）
        "must_match": r"(3\s*月\s*)?18\s*[日号]|十八[日号]",
        # 明显错的日期（4 月、3 月 20 号以后、1~14 号）一律算错
        "must_not": r"4\s*月|3\s*月\s*(?:[1-9]|1[0-4])\s*[日号]|3\s*月\s*(?:2[0-9]|3[01])\s*[日号]",
    },
]


def pct(values: list[float], p: float) -> float:
    """分位数（线性插值）。样本少时也不至于取不到。"""
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * p
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return float(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


def bench(key: str, base: str, models: list[str], max_tokens: int, repeat: int,
          tag: str = "", extra: dict | None = None) -> list[dict]:
    rows = []
    for model in models:
        print(f"\n=== {model}{('（' + tag + '）') if tag else ''} ===")
        per_task = []
        for task in TASKS:
            msgs = [
                {"role": "system", "content": task["system"]},
                {"role": "user", "content": task["user"]},
            ]
            samples = []
            for i in range(repeat):
                r = call(key, base, model, msgs, max_tokens, extra=extra)
                if not r["ok"]:
                    print(f"  [{task['name']}] 第{i + 1}次失败：{r['error']}")
                    samples.append({**r, "usable": False})
                    continue
                usable = (len(r["content"]) >= task["min_chars"]
                          and r["finish"] != "length")
                # 有标准答案的任务：必须命中要点、且不许编造
                if usable and task.get("must_match"):
                    if not _re.search(task["must_match"], r["content"]):
                        usable = False
                if usable and task.get("must_not"):
                    if _re.search(task["must_not"], r["content"]):
                        usable = False
                samples.append({**r, "usable": usable})
                flag = "✓" if usable else "✗"
                print(f"  [{task['name']}] {flag} {r['ms']}ms "
                      f"reason={r['reasoning_tokens']} finish={r['finish']} "
                      f"| {r['content'][:46]!r}")
            ok = [s for s in samples if s.get("ok")]
            per_task.append({
                "name": task["name"],
                "empty": sum(1 for s in samples if s.get("ok") and not s["content"]),
                "usable": sum(1 for s in samples if s.get("usable")),
                "total": len(samples),
                # 中位数说明"通常多快"，**p95/max 说明"最差多慢"** ——
                # 对实时提词来说后者更要紧：用户卡住时等 1.6 秒就是"它没反应"。
                "ms": statistics.median([s["ms"] for s in ok]) if ok else 0,
                "p95": pct([s["ms"] for s in ok], 0.95),
                "max": max([s["ms"] for s in ok]) if ok else 0,
                "reasoning": statistics.median([s["reasoning_tokens"] for s in ok]) if ok else 0,
            })
        rows.append({"model": model, "tag": tag, "tasks": per_task})

    return rows


def report(rows: list[dict]) -> None:
    print("\n" + "=" * 92)
    print(f"{'模型':<22}{'空内容':<8}{'可用':<9}{'中位':<9}{'p95':<9}{'最差':<9}{'思考tok'}")
    print("-" * 92)
    best = None
    for row in rows:
        empty = sum(t["empty"] for t in row["tasks"])
        usable = sum(t["usable"] for t in row["tasks"])
        total = sum(t["total"] for t in row["tasks"])
        ms = statistics.median([t["ms"] for t in row["tasks"] if t["ms"]]) if any(t["ms"] for t in row["tasks"]) else 0
        p95 = max([t.get("p95", 0) for t in row["tasks"]] or [0])
        worst = max([t.get("max", 0) for t in row["tasks"]] or [0])
        reason = statistics.median([t["reasoning"] for t in row["tasks"] if t["reasoning"]]) if any(t["reasoning"] for t in row["tasks"]) else 0
        label = row["model"] + (("/" + row["tag"]) if row.get("tag") else "")
        print(f"{label:<22}{empty:<8}{usable}/{total:<6}{int(ms):<9}{int(p95):<9}{int(worst):<9}{int(reason)}")
        # 排序依据（对实时场景而言，优先级从上到下）：
        #   ① 空内容必须 0（空了 = 功能不可用）
        #   ② 不可用数越少越好
        #   ③ **最差延迟**优先于中位 —— 用户卡住那次恰好撞上长尾就毁了体验
        score = (empty, total - usable, worst, ms)
        if best is None or score < best[0]:
            best = (score, label, ms, worst, reason)

    print("=" * 92)
    if best:
        _, name, ms, worst, reason = best
        print(f"建议：{name}   （中位 {int(ms)}ms / 最差 {int(worst)}ms / 思考 {int(reason)} tok）")
        if worst > ms * 2 and worst > 1200:
            print("⚠️ 长尾明显：最差是中位的 2 倍以上，且超过 1.2 秒。")
            print("   实时场景里用户会感到「这次它没反应」—— 交付时要考虑超时兜底。")
        if reason > 50:
            print("⚠️ 这个模型也在思考 —— 即使它最快，token 仍会被 reasoning 吃掉一部分。")
    else:
        print("没有任何模型可用 —— 先确认 key 的 models 授权。")
    print()
    print("注：排序按「空内容 → 不可用 → **最差延迟** → 中位延迟」。")
    print("    实时提词怕的是长尾，不是平均；中位再快，偶尔卡 1.6 秒也会让人以为坏了。")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="", help="逗号分隔；留空则用 /v1/models 全量")
    ap.add_argument("--max-tokens", type=int, default=1024,
                    help="默认 1024（思考型模型低于这个数必然截断，见 prompter.py 注释）")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--compare", default="",
                    help="额外比一组：'标签=key:model'，可重复用 | 分隔。"
                         "例：--compare 'fast=sk-xxx:model-a|main=sk-yyy:model-b'")
    ap.add_argument("--reasoning", default="none",
                    help="传给 reasoning_effort 的值；real 传 'default' 表示不传")
    args = ap.parse_args()

    key, base = load_env()
    if not key:
        print("找不到 LLM_API_KEY（环境变量或 .env）")
        return 1

    extra = {"reasoning_effort": "none"} if args.reasoning != "default" else {}
    if args.reasoning != "default":
        print(f"统一附加参数：reasoning_effort={args.reasoning}")

    rows: list[dict] = []
    if args.compare:
        # 多组对比：每组用各自的 key 和模型（key 的授权是分开的，不能混用）
        for part in args.compare.split("|"):
            part = part.strip()
            if not part:
                continue
            label, _, spec = part.partition("=")
            k, _, model = spec.partition(":")
            k, model, label = k.strip(), model.strip(), label.strip() or model
            if not k or not model:
                print(f"跳过无法解析的一段：{part!r}（要形如 标签=key:model）")
                continue
            print(f"\n>>> {label}：{model}（key {k[:8]}****{k[-4:]}）")
            rows.extend(bench(k, base, [model], args.max_tokens, args.repeat,
                              tag=label, extra=extra))
    else:
        if args.models:
            models = [m.strip() for m in args.models.split(",") if m.strip()]
        else:
            from openai import OpenAI
            try:
                client = OpenAI(base_url=base, api_key=key, timeout=30)
                models = [m.id for m in client.models.list().data]
            except Exception as exc:
                print(f"列模型失败：{exc}")
                return 1
        print(f"地址：{base}")
        print(f"key ：{key[:8]}****{key[-4:]}")
        print(f"模型：{', '.join(models) or '(空)'}")
        rows = bench(key, base, models, args.max_tokens, args.repeat, extra=extra)

    print(f"参数：max_tokens={args.max_tokens}，每任务 {args.repeat} 次")
    if not rows:
        print("\n没有任何可用模型 —— 需要平台侧授权。")
        return 1

    report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
