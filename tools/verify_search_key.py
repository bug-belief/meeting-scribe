#!/usr/bin/env python3
"""智谱 / 博查 key 一次性验证：判断「能不能用」以及「够不够快」。

**怎么用**（在服务器上跑，key 只在本机内存里，不落盘、不出网给你我之外的人）：

    cd ~/app
    .venv/bin/python /tmp/verify_search_key.py

脚本会提示你粘贴 key（输入时不回显）。也可以直接用环境变量：

    SEARCH_KEY=sk-xxx .venv/bin/python /tmp/verify_search_key.py --provider zhipu

**它测三件事**：
  ① key 有效吗（能不能拿到结果）
  ② 延迟多少（决定能不能用在代答；代答预算 1~3 秒）
  ③ 返回结构对不对（我的解析代码能不能读出 title/snippet/url）

最后给出可以直接抄进 .env 的两行。
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

ENDPOINTS = {
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4/web_search",
              lambda q, n: {"search_engine": "search_std", "search_query": q, "count": n}),
    "bocha": ("https://api.bochaai.com/v1/web-search",
              lambda q, n: {"query": q, "count": n, "summary": True}),
}

# 用真实会问的问题测，不用"测试"——那样测不出结果里的信息量
QUERIES = [
    "小微企业增值税减免政策",
    "杭州落户社保缴纳年限要求",
    "劳动合同试用期最长期限",
]


def parse(provider: str, raw: dict) -> list[dict]:
    """按我的 websearch.py 里的解析逻辑提取，验证结构是否匹配。"""
    rows = []
    if provider == "zhipu":
        for p in (raw.get("search_result") or []):
            rows.append({"title": p.get("title"), "snippet": p.get("content"),
                         "url": p.get("link")})
    else:
        pages = (((raw.get("data") or {}).get("webPages") or {}).get("value") or [])
        for p in pages:
            rows.append({"title": p.get("name"),
                         "snippet": p.get("summary") or p.get("snippet"),
                         "url": p.get("url")})
    return rows


def call(provider: str, key: str, query: str, count: int = 3,
         timeout: float = 10.0) -> tuple[list[dict], int, str]:
    url, builder = ENDPOINTS[provider]
    data = json.dumps(builder(query, count)).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": "meeting-scribe/1.1",
    })
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        ms = int((time.perf_counter() - t0) * 1000)
        return parse(provider, json.loads(body)), ms, ""
    except urllib.error.HTTPError as e:
        ms = int((time.perf_counter() - t0) * 1000)
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return [], ms, f"HTTP {e.code} {detail}"
    except Exception as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        return [], ms, f"{type(exc).__name__}: {str(exc)[:120]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="zhipu", choices=list(ENDPOINTS))
    args = ap.parse_args()

    key = os.environ.get("SEARCH_KEY", "").strip()
    if not key:
        print(f"请粘贴 {args.provider} 的 API key（输入不回显，只在本机内存里）：")
        key = getpass.getpass("key> ").strip()
    if not key:
        print("没拿到 key，退出。")
        return 1

    print(f"\nprovider = {args.provider}")
    print(f"key      = {key[:6]}****{key[-4:]}（长度 {len(key)}）")
    print()

    latencies: list[int] = []
    ok = 0
    for i, q in enumerate(QUERIES, 1):
        rows, ms, err = call(args.provider, key, q)
        if err:
            print(f"[{i}] {q}")
            print(f"    ❌ {err}")
            continue
        ok += 1
        latencies.append(ms)
        print(f"[{i}] {q}  →  {ms}ms，{len(rows)} 条")
        for r in rows[:2]:
            title = (r.get("title") or "")[:34]
            snip = (r.get("snippet") or "")[:60]
            print(f"      · {title}")
            print(f"        {snip}")
        # 结构校验：这三个字段缺一个，我的解析就拿不到东西
        bad = [k for k in ("title", "snippet", "url") if not rows[0].get(k)]
        if bad:
            print(f"      ⚠️ 字段缺失：{bad}（解析可能取不到内容）")
        print()

    print("=" * 56)
    if not ok:
        print("全部失败 —— key 无效，或这家服务在这台机器上不通。")
        print("（端点本身已验证过：401 = 格式对、只是 key 不对）")
        return 1

    print(f"成功 {ok}/{len(QUERIES)} 次")
    if latencies:
        med = int(statistics.median(latencies))
        worst = max(latencies)
        print(f"延迟：中位 {med}ms / 最差 {worst}ms")
        print()
        # 判断能不能用在代答（预算 1~3s，检索只该占一小部分）
        if med <= 500:
            print("✅ 很快，接在代答里完全没压力")
        elif med <= 1000:
            print("✅ 可用（我的超时设的是 900ms，偶尔会超时降级——可以接受）")
        elif med <= 2000:
            print("⚠️ 偏慢。建议把 WEB_SEARCH_TIMEOUT 调到 1.5，或只在高价值问题上查")
        else:
            print("❌ 太慢，不建议接在代答里（用户会明显感到卡）")

    print()
    print("要启用的话，把这两行加进 ~/app/.env：")
    print(f"  WEB_SEARCH_PROVIDER={args.provider}")
    print(f"  WEB_SEARCH_KEY={key}")
    print()
    print("然后重启：systemctl --user restart meeting-web")
    return 0


if __name__ == "__main__":
    sys.exit(main())
