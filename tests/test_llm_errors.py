"""llm_errors 护栏：每条分类规则都要有正例 + 反例。

**为什么反例比正例重要**：分类表的失效方式不是「认不出来」（那种会落到 unknown，
一眼能看见），而是**认错**——把网络超时判成"服务超时"会让人去重试，
而实际该去看进程有没有起来。错误的分诊比不诊更糟。

所以每条规则都配一个「长得像、但不该归到这里」的反例，
并额外锁死几个顺序敏感的用例（unreachable 必须排在 timeout 前面）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import llm_errors  # noqa: E402

passed = 0
failed: list[str] = []


def check(desc: str, got, want=True) -> None:
    global passed
    # 类型守卫：三参误用（check(desc, got, want) 传非布尔）会让断言永远为真
    if not isinstance(want, (bool, str, int, list, dict, tuple, type(None))):
        failed.append(f"{desc}：want 类型异常 {type(want)}")
        return
    if got == want:
        passed += 1
        return
    failed.append(f"{desc}\n      期望 {want!r}\n      实际 {got!r}")


def kind_of(text: str) -> str:
    return llm_errors.classify(text)["kind"]


def main() -> int:
    print("1) 真实样本：2026-10-22 生产上抓到的 402 原文")
    REAL_402 = (
        "API 调用最终失败：Error code: 402 - {'error': {'message': "
        "\"litellm.BadRequestError: OpenAIException - Insufficient Balance "
        "(request_id: 18438f13-e1e0-45cd-a9f5-36fd450363d6)\\n\\nLiteLLM: model group "
        "'<你的模型组>' failed with the error above and no fallback model group was "
        "found for it, so the request was not retried on another model. Fallbacks are "
        "configured for: <备选模型组>.'}}"
    )
    check("真实 402 → no_balance", kind_of(REAL_402), "no_balance")
    check("人话里不出现 litellm", "litellm" in llm_errors.message(REAL_402).lower(), False)
    check("人话可读", llm_errors.message(REAL_402), "AI 服务余额不足")

    print("\n2) 每类正例")
    positives = {
        "no_balance": [
            "Insufficient Balance",
            "Error code: 402 - Payment Required",
            "账户余额不足，请充值",
        ],
        "bad_key": [
            "Error code: 401 - Incorrect API key provided",
            "AuthenticationError: invalid api key",
            "Unauthorized: api key invalid",
            "密钥已过期",
        ],
        "rate_limit": [
            "Error code: 429 - Rate limit reached",
            "Too many requests, please slow down",
            "请求过于频繁，已限流",
        ],
        "quota": [
            "You exceeded your current quota, please check your plan",
            "quota exceeded for this billing period",
        ],
        "unreachable": [
            "Connection refused: 127.0.0.1:1234",
            "ECONNREFUSED 127.0.0.1:1234",
            "Failed to establish a new connection: [Errno 111]",
            "HTTPSConnectionPool(host='x'): Max retries exceeded with url",
            "Name or service not known",
        ],
        "timeout": [
            "Request timed out after 60s",
            "ReadTimeout: The read operation timed out",
            "deadline exceeded while waiting",
            "调用超时",
        ],
        "model_missing": [
            "The requested model '<你的模型>' is not available for this API key",
            "model group '<你的模型组>' ... no fallback model group",
            "model_not_found: does not exist",
        ],
        "empty": [
            "API 返回空内容：1024 个 token 全花在思考上",
            "empty response from upstream",
        ],
    }
    for want, samples in positives.items():
        for s in samples:
            check(f"{want} ← {s[:42]}", kind_of(s), want)

    print("\n3) 反例：长得像但不该归到那里（顺序敏感）")
    # "connection timeout" 同时含 connection 与 timeout → 必须先判 unreachable，
    # 因为处置动作是「看网关进程」，不是「重试一次」。
    check("connection timeout → unreachable 而非 timeout",
          kind_of("HTTPSConnectionPool: connection timeout to 127.0.0.1"),
          "unreachable")
    check("read timed out（无 connection 字样）→ timeout",
          kind_of("Read timed out"), "timeout")
    # 401/402 同时出现时，余额优先（402 是更具体的原因）
    check("402 与 401 同时出现 → no_balance",
          kind_of("Error code: 402 Insufficient Balance (upstream said 401 earlier)"),
          "no_balance")
    # 模型思考超时不是网关不可达
    check("模型思考超时 → timeout", kind_of("upstream timed out after 1024 tokens"),
          "timeout")
    # 普通中文不该被塞进任何具体类
    check("无关文本 → unknown", kind_of("磁盘写满了"), "unknown")
    check("空串 → unknown", kind_of(""), "unknown")
    check("None → unknown", llm_errors.classify(None)["kind"], "unknown")

    print("\n4) 结构契约：classify 永远给全 4 个键，且不抛异常")
    for s in ["", "x", REAL_402, None, 12345, b"bytes"]:
        try:
            r = llm_errors.classify(s)
            check(f"classify({type(s).__name__}) 键齐全",
                  sorted(r.keys()), ["detail", "hint", "kind", "label"])
            check(f"classify({type(s).__name__}) kind 在 CATALOG 里",
                  r["kind"] in llm_errors.CATALOG, True)
            check(f"classify({type(s).__name__}) label 非空", bool(r["label"]), True)
        except Exception as exc:  # 分类器自己抛错会把原始问题彻底埋掉
            failed.append(f"classify({type(s).__name__}) 抛异常：{exc}")

    print("\n5) 原文截断：不能把 16KB 的响应体拽进界面")
    huge = "错误" * 5000
    r = llm_errors.classify(huge)
    check("detail 有上限", len(r["detail"]) <= llm_errors.MAX_DETAIL, True)
    check("超长时带省略号", r["detail"].endswith("…"), True)
    check("短文本不加省略号", llm_errors.classify("磁盘写满了")["detail"], "磁盘写满了")

    print("\n6) CATALOG 自洽：每个 kind 都有 label 与 hint")
    for k, meta in llm_errors.CATALOG.items():
        check(f"{k} 有 label", bool(meta.get("label")), True)
        check(f"{k} 有 hint", bool(meta.get("hint")), True)
        check(f"{k} 的 label 不含英文异常名", "exception" not in meta["label"].lower(), True)

    print("\n7) PipelineError 携带分类（界面用的 kind/hint 不能丢）")
    from utils import PipelineError

    e = PipelineError("API 调用最终失败：Insufficient Balance",
                      kind="no_balance", hint="充值后立即恢复")
    check("kind 保留", e.kind, "no_balance")
    check("hint 保留", e.hint, "充值后立即恢复")
    check("str 仍是原文（日志要靠它）", str(e), "API 调用最终失败：Insufficient Balance")
    old = PipelineError("老写法不带 kind")
    check("老写法 kind 为 None", old.kind, None)
    check("老写法 hint 为 None", old.hint, None)
    check("老写法 str 正常", str(old), "老写法不带 kind")

    print("\n" + "=" * 40)
    print(f"通过 {passed} 项，失败 {len(failed)} 项")
    for f in failed:
        print("  ✗ " + f)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
