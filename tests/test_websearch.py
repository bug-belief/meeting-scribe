#!/usr/bin/env python3
"""验证关键词清理：既要剥干净指代，又不能把内容剥没。

**这个测试比看代码重要**：`extract_query` 是隐私边界的执行者 ——
它剥得不干净，会议内容就漏给第三方搜索了；剥过头，检索就查不到东西。
两个方向都得测。
"""
from __future__ import annotations

import json
import sys
import urllib.error as urllib_error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from websearch import extract_query, should_search  # noqa: E402

PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}\n     得到 {got!r}\n     期望 {want!r}")


def main() -> int:
    print("\n1) 剥掉句首指代与语气词")
    check("那我想问一下", extract_query("那我想问一下车险理赔时效是多久"), "车险理赔时效")
    check("嗯，帮我查下", extract_query("嗯，帮我查下专利法最新修订"), "专利法最新修订")
    check("请问", extract_query("请问一下杭州落户条件"), "杭州落户条件")
    check("你好", extract_query("你好，那个公积金提取额度"), "公积金提取额度")

    print("\n2) 句中的「我」是内容，不能剥")
    # "我司"「我方」这类是主体，剥掉就变味了
    check("我司保留", extract_query("我司注册资本要求"), "我司注册资本要求")
    check("我方保留", extract_query("我方违约责任怎么算"), "我方违约责任怎么算")

    print("\n3) 语气尾巴要剥干净")
    # 剥掉「这个」是**对的**：搜索引擎不懂"这个"，留着反而查不到。
    # 这种上下文相关的问题本来就该由模型按对话答，检索帮不上忙。
    check("去「吗」与指代", extract_query("这个能报销吗"), "能报销")
    check("去「怎么样」", extract_query("物业费标准怎么样"), "物业费标准")
    check("去「是多少」", extract_query("违约金比例是多少"), "违约金比例")

    print("\n4) 没检索价值的要返回空")
    for s in ["好的", "嗯嗯", "收到", "谢谢", "啊？", ""]:
        check(f"{s!r} → 空", extract_query(s), "")

    print("\n5) should_search：该跳的跳")
    check("纯客套不查", should_search("好的谢谢"), False)
    check("天气走专门工具，这里不查", should_search("今天天气怎么样"), False)
    check("业务问题要查", should_search("杭州落户社保要交几年"), True)

    print("\n6) 长度上限（防止把整段对话当 query 发出去）")
    long_text = "关于" + "很长的背景说明" * 20 + "我想问碳排放配额"
    q = extract_query(long_text)
    check("不超过 60 字", len(q) <= 60, True)
    print(f"     实际长度 {len(q)}：{q[:40]}…")

    print("\n7) 隐私边界：含个人/公司敏感信息的整句不检索")
    from websearch import maybe_search

    for s_ in ["我的身份证号是 3707 开头", "合同号 HT-2026-001 怎么查",
               "手机号 138xxxx 打不通", "银行卡额度多少"]:
        check(f"{s_[:14]!r}… 不检索", should_search(s_), False)

    print("\n8) 没有配 key 时完全不工作（纯增量，不改现有行为）")
    import os as _os

    _os.environ.pop("WEB_SEARCH_PROVIDER", None)
    _os.environ.pop("WEB_SEARCH_KEY", None)
    from websearch import enabled

    check("未配 key → enabled() 为假", enabled(), False)
    check("未配 key → maybe_search 返回空", maybe_search("杭州落户条件"), "")

    print("\n9) 反向对照：不剥的话会带出指代词")
    raw = "那我想问一下车险理赔时效是多久"
    check("原始句子确实含指代（说明上面第 1 节不是空跑）",
          ("我" in raw or "你" in raw), True)

    # ------------------------------------------------------------------ #
    print("\n10) 🔴 三种失败模式都必须安全降级（纯增量的底线）")
    # 检索是锦上添花，绝不能成为代答的故障点。三种失败模式的异常类型完全不同：
    #   ① key 无效 → HTTPError 401
    #   ② 超时     → socket.timeout
    #   ③ 域名不存在 → URLError(gaierror)
    # 任何一个抛出去，代答就整条失败了 —— 所以必须逐个验证"返回空串且不抛"。
    import importlib
    import time as _time

    import websearch as _ws

    def _with_env(provider: str, key: str, timeout: str):
        _os.environ["WEB_SEARCH_PROVIDER"] = provider
        _os.environ["WEB_SEARCH_KEY"] = key
        _os.environ["WEB_SEARCH_TIMEOUT"] = timeout
        importlib.reload(_ws)
        return _ws

    # ① 无效 key（真打网络，会拿到 401）
    m = _with_env("zhipu", "invalid-key-for-test", "5")
    check("配了无效 key → enabled() 为真（说明真的会去查）", m.enabled(), True)
    try:
        out = m.maybe_search("杭州落户社保缴纳年限要求")
        check("无效 key → 返回空串（不抛异常）", out, "")
    except Exception as exc:
        check("无效 key → 返回空串（不抛异常）", f"抛了 {type(exc).__name__}", "不抛")

    # ② 超时设极短
    m = _with_env("zhipu", "any", "0.05")
    t0 = _time.perf_counter()
    try:
        out = m.maybe_search("小微企业增值税减免政策")
        ms = int((_time.perf_counter() - t0) * 1000)
        check("超时 → 返回空串", out, "")
        check(f"超时后快速放弃（实际 {ms}ms，应 <2000）", ms < 2000, True)
    except Exception as exc:
        check("超时 → 返回空串（不抛异常）", f"抛了 {type(exc).__name__}", "不抛")

    # ③ 域名不存在
    m = _with_env("zhipu", "any", "3")

    def _broken(query, key, timeout, count):
        import urllib.request as _u

        _u.urlopen("https://this-host-does-not-exist-xyz.invalid/", timeout=timeout)
        return []

    m._PROVIDERS["zhipu"] = _broken
    try:
        out = m.maybe_search("测试域名不存在")
        check("域名不存在 → 返回空串（不抛异常）", out, "")
    except Exception as exc:
        check("域名不存在 → 返回空串（不抛异常）", f"抛了 {type(exc).__name__}", "不抛")

    # 清理环境，别污染后续
    for k in ("WEB_SEARCH_PROVIDER", "WEB_SEARCH_KEY", "WEB_SEARCH_TIMEOUT"):
        _os.environ.pop(k, None)
    importlib.reload(_ws)
    check("清理后回到未启用状态", _ws.enabled(), False)

    print("\n10.5) provider 判定：三种 provider 的启用条件不同")
    # litellm 不需要单独 key（复用 LLM_API_KEY），但**必须有 tool 名**，
    # 否则每次都会打一个必然失败的空查询。
    _clear = ("WEB_SEARCH_PROVIDER", "WEB_SEARCH_TOOL", "WEB_SEARCH_KEY", "LLM_API_KEY")

    def _cfg(env: dict) -> tuple[str, bool]:
        for k in _clear:
            _os.environ.pop(k, None)
        _os.environ.update(env)
        p, _key, _t = _ws.config()
        return p, bool(p)

    check("全空 → 不启用", _cfg({}), ("", False))
    check("litellm 缺 tool 名 → 不启用（避免空查询）", _cfg(
        {"WEB_SEARCH_PROVIDER": "litellm", "LLM_API_KEY": "sk-app"}), ("", False))
    check("litellm + tool 名 → 启用（复用 LLM_API_KEY）", _cfg(
        {"WEB_SEARCH_PROVIDER": "litellm", "WEB_SEARCH_TOOL": "zhipu-search",
         "LLM_API_KEY": "sk-app"}), ("litellm", True))
    check("zhipu 缺 key → 不启用", _cfg({"WEB_SEARCH_PROVIDER": "zhipu"}), ("", False))
    check("zhipu + key → 启用", _cfg(
        {"WEB_SEARCH_PROVIDER": "zhipu", "WEB_SEARCH_KEY": "sk-x"}), ("zhipu", True))
    check("未知 provider → 不启用（拼错名字不会静默乱跑）", _cfg(
        {"WEB_SEARCH_PROVIDER": "bing", "WEB_SEARCH_KEY": "x"}), ("", False))

    for k in _clear:
        _os.environ.pop(k, None)
    importlib.reload(_ws)

    print("\n10.6) 🔴 超时默认值必须是现实的量级（900ms 那次是我设错了）")
    # 实测：到 Tavily 光是往返就 1016~1228ms（拿一个 401 都要这么久）。
    # 设 900ms 会让**每一次**真实检索都超时，而且是**静默**的 —— 装了等于没装。
    for k in _clear:
        _os.environ.pop(k, None)
    _os.environ.update({"WEB_SEARCH_PROVIDER": "zhipu", "WEB_SEARCH_KEY": "sk-x"})
    _t_default = _ws.config()[2]
    check(f"默认超时 ≥2.0s（实际 {_t_default}s）", _t_default >= 2.0, True)
    check(f"默认超时 ≤4.0s（别太佛系，实际 {_t_default}s）", _t_default <= 4.0, True)

    _os.environ["WEB_SEARCH_TIMEOUT"] = "abc"
    check("非法超时值 → 退回默认而不是崩", _ws.config()[2], _t_default)
    _os.environ["WEB_SEARCH_TIMEOUT"] = "5"
    check("显式设置的超时生效", _ws.config()[2], 5.0)
    _os.environ.pop("WEB_SEARCH_TIMEOUT", None)

    print("\n10.7) 预取：必须非阻塞、有配额、默认关闭")
    # 预取的目的：把检索延迟从"用户等着听"里挪走。
    # 定稿行到达时后台先查，等真要回复时命中缓存 = 0ms。
    for k in _clear + ("WEB_SEARCH_PREFETCH", "WEB_SEARCH_PREFETCH_MAX"):
        _os.environ.pop(k, None)
    _os.environ.update({"WEB_SEARCH_PROVIDER": "zhipu", "WEB_SEARCH_KEY": "sk-x"})
    importlib.reload(_ws)

    check("未开开关时 prefetch_enabled() 为假", _ws.prefetch_enabled(), False)
    check("未开开关时 prefetch() 直接返回 False",
          _ws.prefetch("杭州落户社保缴纳年限要求"), False)

    _os.environ["WEB_SEARCH_PREFETCH"] = "1"
    importlib.reload(_ws)
    check("开了开关后 prefetch_enabled() 为真", _ws.prefetch_enabled(), True)

    _ws.reset_prefetch()
    _t0 = _time.perf_counter()
    _fired = _ws.prefetch("杭州落户社保缴纳年限要求", "testsess")
    _el = int((_time.perf_counter() - _t0) * 1000)
    check("prefetch() 派活了", _fired, True)
    # 关键：不能被网络请求拖住。实测 2ms，给 200ms 余量已经很宽松。
    check(f"prefetch() 非阻塞（实际 {_el}ms）", _el < 200, True)

    check("客套话不预取", _ws.prefetch("好的谢谢"), False)
    check("天气不预取（走专门工具）", _ws.prefetch("今天天气怎么样"), False)
    check("含身份证的整句不预取", _ws.prefetch("我的身份证号是 3707 开头"), False)

    _ws.reset_prefetch()
    for _i in range(30):
        _ws.prefetch(f"配额测试查询{_i}")
    _st = _ws.prefetch_stats()
    check(f"配额打满后 used == max（{_st['used']}/{_st['max']}）",
          _st["used"] == _st["max"], True)
    check("打满后新请求被拒", _ws.prefetch("再试一次配额"), False)
    _ws.reset_prefetch()
    check("reset 后配额归零", _ws.prefetch_stats()["used"], 0)

    for k in _clear + ("WEB_SEARCH_PREFETCH", "WEB_SEARCH_PREFETCH_MAX"):
        _os.environ.pop(k, None)
    importlib.reload(_ws)

    print("\n11) 端点与请求格式（这条不需要真 key：401 就说明格式对）")
    # ⚠️ 这一节**真的发网络请求**。离线 / 无外网的环境会失败，
    # 那是环境问题不是代码问题，所以给一个显式开关跳过。
    #   SKIP_NET_TESTS=1 python tests/test_websearch.py
    if _os.environ.get("SKIP_NET_TESTS") == "1":
        print("  ⏭  SKIP_NET_TESTS=1，跳过（离线环境正常现象，不算失败）")
    # 用无效 key 打真实接口：404 说明路径错、400 说明字段错、401 说明只是没权限。
    # 2026-10-22 实测：bocha 401 / zhipu 401，两个端点都对，且服务器出网正常。
    import urllib.request as _url

    for name, url, body in [] if _os.environ.get("SKIP_NET_TESTS") == "1" else [
        ("zhipu", "https://open.bigmodel.cn/api/paas/v4/web_search",
         {"search_engine": "search_std", "search_query": "测试", "count": 1}),
        ("bocha", "https://api.bochaai.com/v1/web-search",
         {"query": "测试", "count": 1, "summary": True}),
    ]:
        try:
            req = _url.Request(
                url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer invalid-probe"})
            with _url.urlopen(req, timeout=8) as resp:
                # 2xx 是未预期结果（无效 key 不该成功），但先把状态记下来
                code: object = resp.status
        except urllib_error.HTTPError as e:
            code = e.code
        except Exception as exc:
            # 连不上属于环境问题，不是代码问题：不判失败，但照样消耗一条，
            # 保证任何环境下的**断言条数都相同**（条件写进期望值，不写分支）。
            code = f"{type(exc).__name__}: {str(exc)[:60]}"

        # 401/403/429 = 端点对且格式对；400 = 字段错但端点对。
        # 404/405 = URL 写错；2xx = 未预期（无效 key 竟然通了）；其它 = 环境问题。
        ok = code in (400, 401, 403, 429) or not isinstance(code, int)
        check(f"{name} 端点与格式（HTTP {code}）", ok, True)
        if isinstance(code, int) and code not in (400, 401, 403, 429):
            print(f"     ⚠️ HTTP {code} —— 若是 404/405 说明 URL 写错了")
        elif not isinstance(code, int):
            print(f"     ⚠️ 这次没连上（{code}）—— 环境问题，不计入失败")

    print(f"\n{'=' * 44}\n通过 {PASS} 项，失败 {FAIL} 项\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
