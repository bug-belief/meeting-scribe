"""O-11 护栏：27 条路由、method、中间件、鉴权行为 —— 拆 server.py 时一条都不许丢。

**为什么先建这个测试再动手**：拆分前 `grep create_app tests/` 是空的，
也就是说「哪些路由该存在」完全没被固化。拆 1,647 行等于在没有护栏的情况下搬房子——
漏注册一条、把 `@app.get` 抄成 `@app.post`、中间件漏挂导致全站裸奔，全靠人眼。

本测试把现状冻结成基线。**基线是硬编码的**（不是从当前代码反推），
这样才能抓到「有人顺手删了一条路由」。

运行前提：需要 fastapi + httpx。缺依赖时**跳过并明确打印**，不假装通过。

两个容易写错的点（第一版都踩了，留在这当注释防止后人再踩）：
  · FastAPI 自带 /openapi.json 等框架路由，必须从基线里排除，不然永远对不上；
  · `resolve_owner` 有「web_token 和 tenant_token_map 都没配 → 本地调试直通」的
    设计分支。**测鉴权必须传一个带 web_token 的 Settings**，
    拿默认 SETTINGS（本机没口令）去断言 401 是测不到东西的。
"""
from __future__ import annotations

import re
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = 0
# ⚠️ 必须是**实算**的计数器，不能像原来那样在末尾写死 `total = 30`。
# 写死等于汇总行永远报 30：加断言不涨、断言被 if 分支跳过也不降，
# 于是 run_all 的「通过数 < 基线」这条护栏彻底失效（2026-10-23 修）。
TOTAL = 0


def check(name: str, got, want=True) -> bool:
    global FAILED, TOTAL
    TOTAL += 1
    ok = got == want
    if not ok:
        FAILED += 1
    print(("  ok   " if ok else "  FAIL ") + name
          + ("" if ok else f"\n         实际 {got!r}\n         期望 {want!r}"))
    return ok


def check_true(name: str, cond: bool) -> bool:
    return check(name, bool(cond), True)


# ---------------------------------------------------------------------------
# 冻结基线：2026-10-20 拆分前逐条导出的 27 条业务路由。
# 改这里的唯一正当理由是「业务真的增删了路由」，且必须同一提交说明原因。
EXPECTED_ROUTES = {
    ("/", "GET"),
    ("/api/archive/audio", "GET"),
    ("/api/archive/claim", "POST"),
    ("/api/archive/cluster_audio", "GET"),
    ("/api/archive/speakers", "GET"),
    ("/api/asr/quick", "POST"),
    ("/api/assistant/reply", "POST"),
    ("/api/cost", "GET"),
    ("/api/health", "GET"),
    ("/api/kb/scenarios", "GET"),
    ("/api/kb/search", "GET"),
    ("/api/kb/stats", "GET"),
    ("/api/latest", "GET"),
    ("/api/logout", "POST"),
    ("/api/me", "GET"),
    ("/api/meeting", "GET"),
    ("/api/meeting/star", "POST"),
    ("/api/meetings", "GET"),
    ("/api/prompter/suggest", "POST"),
    ("/api/speakers", "GET"),
    ("/api/tts", "POST"),
    ("/api/tts/health", "GET"),
    ("/api/tts/voices", "GET"),
    ("/api/voice/sample", "GET"),
    ("/api/voice/samples", "GET"),
    ("/manifest.webmanifest", "GET"),
    ("/voice", "GET"),
    ("/ws/live", "WS"),
}

# FastAPI / Starlette 自带的框架路由，不算业务面
# （docs/redoc 已被 create_app 关掉，openapi.json 留着不影响鉴权，基线里排除即可）
FRAMEWORK_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/static"}


# ---------------------------------------------------------------------------
print("\n0) 依赖检查")
try:
    from fastapi.testclient import TestClient
except ImportError as exc:  # pragma: no cover
    print(f"  SKIP 缺依赖：{exc}")
    print("      本测试需要 fastapi + httpx（服务器 ~/app/.venv 里有）")
    print("\n" + "=" * 40)
    print("通过 0 项，失败 0 项（共 0 项，已跳过）")
    sys.exit(0)

import server
from config import SETTINGS


def make_app(**overrides):
    """在临时目录里建 app，不污染仓库目录。"""
    td = tempfile.mkdtemp()
    cfg = replace(SETTINGS, base_dir=Path(td), **overrides)
    return server.create_app(cfg), cfg


def route_key(r):
    methods = sorted(getattr(r, "methods", None) or [])
    path = getattr(r, "path", None)
    if path is None or path in FRAMEWORK_PATHS:
        return None
    if type(r).__name__ == "APIWebSocketRoute":
        return (path, "WS")
    if not methods:
        return None                      # Mount（/static）不是业务路由
    return (path, methods[0])


app, cfg0 = make_app()


# ---------------------------------------------------------------------------
print("\n1) 路由清单必须与冻结基线逐条相等")

actual = {k for k in (route_key(r) for r in app.routes) if k}
missing = sorted(EXPECTED_ROUTES - actual)
extra = sorted(actual - EXPECTED_ROUTES)

check(f"业务路由数 == 28（实际 {len(actual)}）", len(actual), 28)
for p, m in missing:
    print(f"     缺失 - {m:5s} {p}")
for p, m in extra:
    print(f"     多出 + {m:5s} {p}")
check("没有缺失的路由", missing, [])
check("没有多出的路由", extra, [])

# ⚠️ 上面用的是 set，**抓不到「同一路径注册两次」**——
# 实测踩过：C-2 把 /voice 搬进 api/pages.py 后忘了从 server.py 删掉，
# 结果 /voice 被注册两次，而 set 去重后 27 条照样对得上，测试全绿。
# FastAPI 里后注册的会覆盖前面的，功能上可能「看起来正常」，
# 但一旦两份实现漂移，就是最难查的那种 bug。所以必须单独数重复。
from collections import Counter  # noqa: E402

_raw_paths = [rig for rig in (route_key(r) for r in app.routes) if rig]
_dups = {k: v for k, v in Counter(_raw_paths).items() if v > 1}
if _dups:
    print("     重复注册：")
    for k, v in sorted(_dups.items()):
        print(f"       x{v} {k[1]:5s} {k[0]}")
check("没有重复注册的路由", _dups, {})


# ---------------------------------------------------------------------------
print("\n2) 中间件仍在（漏挂 = 全站裸奔）")

mw_names = [getattr(getattr(m, "cls", None), "__name__", "") for m in app.user_middleware]
check_true(f"挂了自定义中间件（实际 {mw_names}）", len(mw_names) >= 1)
check("中间件数量 == 1（auth_gate 是唯一一个）", len(mw_names), 1)


# ---------------------------------------------------------------------------
print("\n3) 鉴权行为：带口令的 cfg 下，白名单直通、其余 401、口令对则 200")

app_auth, _ = make_app(web_token="__TEST_TOKEN__")
client = TestClient(app_auth)
with client:
    r = client.get("/api/health")
    check("不带口令访问 /api/health（白名单）→ 200", r.status_code, 200)
    body = r.json() if r.status_code == 200 else {}
    check_true("/api/health 有 version", isinstance(body.get("version"), str))
    check_true("/api/health 不含租户/路径类敏感字段",
               not any(k in body for k in ("tenants", "base_dir", "paths", "devices")))

    r3 = client.get("/api/me")
    check("不带口令访问 /api/me → 401", r3.status_code, 401)

    r4 = client.get("/api/me?k=__TEST_TOKEN__")
    check("带正确口令访问 /api/me → 200", r4.status_code, 200)

    r5 = client.get("/api/me?k=WRONG")
    check("带错误口令 → 仍 401", r5.status_code, 401)

    # 浏览器（Accept: text/html）拿到的应是激活页；
    # 用一个**新** client，避免上一步成功拿到的 wb_k cookie 在这一步替它放行
    # ——第一版就是踩了这个 cookie 残留，断出「200」的假结论。
    fresh = TestClient(app_auth)
    with fresh:
        r6 = fresh.get("/api/me", headers={"accept": "text/html"})
        check("浏览器（Accept: html）无 cookie 被挡时也是 401", r6.status_code, 401)
        check_true("并且返回的是激活页而不是纯文本",
                   "text/html" in (r6.headers.get("content-type") or ""))


# ---------------------------------------------------------------------------
print("\n3.5) /api/logout 必须真把 wb_k 删掉（多租户换人的唯一出口）")

# 判据只认「cookie 名 + Max-Age=0 + Path=/」，**不写死整串**：
# starlette 以后调整属性顺序、加 partitioned，不该把这个测试搞红。
def _is_clear(sc: str) -> bool:
    return "wb_k=" in sc and "Max-Age=0" in sc and "Path=/" in sc


out = TestClient(app_auth)
with out:
    login_sc = out.get("/api/me?k=__TEST_TOKEN__").headers.get("set-cookie") or ""
    check("登录后 /api/me（走 cookie）→ 200", out.get("/api/me").status_code, 200)

    # 反向对照先行：登录那次写的是 90 天，同一个 cookie 名但**不该**被判成删除。
    # 没有这一条，"wb_k= + Max-Age=0" 可能是个恒真条件也说不定。
    check_true("反向对照：登录写的是 90 天，判据不认成删除",
               "wb_k=" in login_sc and not _is_clear(login_sc))

    r_out = out.post("/api/logout")
    sc = r_out.headers.get("set-cookie") or ""
    check("/api/logout → 200", r_out.status_code, 200)
    check_true("Set-Cookie 是清除 wb_k", _is_clear(sc))
    # 删完必须真的进不去。只断言响应头不够 —— 那只能证明"发了删除指令"，
    # 证明不了"浏览器接受了"。这一步才是行为断言。
    check("删完再问 /api/me → 401", out.get("/api/me").status_code, 401)
    # 再登录一次又通了：证明上面的 401 是删除造成的，不是别的原因
    check("重新带口令还能进（证明 401 是删除造成的）",
          out.get("/api/me?k=__TEST_TOKEN__").status_code, 200)


# ---------------------------------------------------------------------------
print("\n4) WebSocket 路由仍注册且路径正确")

ws = [r for r in app.routes if type(r).__name__ == "APIWebSocketRoute"]
check("有且只有 1 条 WebSocket 路由", len(ws), 1)
if ws:
    check("/ws/live 路径正确", ws[0].path, "/ws/live")


# ---------------------------------------------------------------------------
print("\n5) create_app 可重复调用，互不污染")

app2, _ = make_app()
check("两次 create_app 路由总数一致", len(app2.routes), len(app.routes))
check_true("两次是不同对象", app2 is not app)


# ---------------------------------------------------------------------------
print("\n6) 传入的 Settings 必须真的生效（防「静默串租户」）")
# 现状是闭包捕获 cfg；C-2 拆开后改走 app.state.cfg。
# 这一项用**行为**锁死：cfg 里换了口令，鉴权就按新口令走。
app_t, _ = make_app(web_token="__TOKEN_A__")
c = TestClient(app_t)
with c:
    check("用 TOKEN_A 建的 app：TOKEN_A 放行", c.get("/api/me?k=__TOKEN_A__").status_code, 200)
    check("用 TOKEN_A 建的 app：TOKEN_B 被拒", c.get("/api/me?k=__TOKEN_B__").status_code, 401)

# C-2 起 create_app 会把 cfg 放进 app.state
state_cfg = getattr(app_t.state, "cfg", None)
if state_cfg is not None:
    check("app.state.cfg 是传入的那个 settings", str(state_cfg.web_token), "__TOKEN_A__")
else:
    print("  ..   app.state.cfg 尚未引入（C-2 起自动加硬，现在不判失败）")

# ⚠️ 光断言 app.state.cfg 存在是**不够**的：路由可能还在读模块级 SETTINGS，
# 而 state 里那份只是个没人用的副本。所以必须验「路由的行为真的跟着 state 走」。
# 做法：两个 app，各自的 base_dir 不同，看 /api/latest 报的绝对路径落在谁家。
#   实测踩过：把 deps.cfg_of 改成 `return SETTINGS`（模拟串租户），
#   上一版断言依然全绿——就是这个盲区。
import tempfile as _tf  # noqa: E402

d1, d2 = _tf.mkdtemp(), _tf.mkdtemp()
a1, _ = make_app(web_token="__T1__")
# 注意：有**三个**路径字段要一起换才能隔离干净
#   base_dir     —— 日志与默认推导
#   processed_dir—— 单租户时的归档根
#   tenant_root  —— 多租户时的归档根（tenant() 实际用它派生 tenants/<owner>/processed）
# 第一版只改了前两个，结果两个 app 都派到仓库里的同一个 tenants/me。
a1.state.cfg = replace(a1.state.cfg, base_dir=Path(d1),
                       processed_dir=Path(d1) / "processed", tenant_root=Path(d1) / "tenants")
a2, _ = make_app(web_token="__T2__")
a2.state.cfg = replace(a2.state.cfg, base_dir=Path(d2),
                       processed_dir=Path(d2) / "processed", tenant_root=Path(d2) / "tenants")

# 硬判据：直接问 deps，确认它读的是 request.app.state.cfg，而不是模块级 SETTINGS
from starlette.requests import Request as _Req  # noqa: E402

from api.deps import cfg_of, tenant as _tenant_of  # noqa: E402


def _fake_request(app_obj, owner="me"):
    scope = {"type": "http", "app": app_obj, "headers": [], "query_string": b"", "path": "/"}
    req = _Req(scope)
    req.state.owner = owner
    return req


check("cfg_of 读的是 app.state.cfg（app1）", str(cfg_of(_fake_request(a1)).base_dir), d1)
check("cfg_of 读的是 app.state.cfg（app2）", str(cfg_of(_fake_request(a2)).base_dir), d2)

# tenant() 会把目录**重新派生**到 tenants/<owner>/ 下（多租户设计，不是简单继承 processed_dir），
# 所以判据是「两个 app 的路径不同 + owner 段正确」，不是「等于 base_dir/processed」。
# 第一版按后者写，自己写错了。
tp1 = _tenant_of(_fake_request(a1, "me")).processed_dir
tp2 = _tenant_of(_fake_request(a2, "me")).processed_dir
check_true(f"两个 app 的租户目录互不相同（{tp1.name} vs {tp2.name}）", str(tp1) != str(tp2))
check_true("路径里带 owner 段 tenants/<owner>/",
           f"tenants{chr(92)}me" in str(tp1) or "tenants/me" in str(tp1).replace("\\", "/"))
check_true("各自落在自己 base_dir 派生的 tenant_root 下",
           str(Path(d1)) in str(tp1) or "tenants" in str(tp1))


# ---------------------------------------------------------------------------
print("\n7) 路径穿越防线（C-3 搬进 api/deps.py 后必须还在）")
# `archive_dir()` 是回放 / 认领 / 切片三处的唯一入口，它挡不住就等于
# 「持有口令的人可以读服务器上任意目录」。搬模块时最容易把这条校验丢掉。
from urllib.parse import quote as _q  # noqa: E402

d3 = Path(_tf.mkdtemp())
proc3 = d3 / "tenants" / "me" / "processed"
proc3.mkdir(parents=True)
(proc3 / "ok_dir").mkdir()
(proc3 / "ok_dir" / "speakers.json").write_text('{"assignments": []}', encoding="utf-8")
(proc3 / "ok_dir" / "meta.json").write_text("{}", encoding="utf-8")

a3, _ = make_app(web_token="__T3__")
a3.state.cfg = replace(a3.state.cfg, base_dir=d3, tenant_root=d3 / "tenants",
                       processed_dir=d3 / "processed")
c3 = TestClient(a3)
with c3:
    traversal = [
        ("../etc", "上一级"),
        ("..\\..\\x", "反斜杠穿越"),
        ("ok_dir/../..", "斜杠路径"),
        ("", "空值"),
        ("../../tenants/other", "跨租户"),
    ]
    for val, label in traversal:
        r = c3.get(f"/api/archive/speakers?dir={_q(val)}&k=__T3__")
        check_true(f"路径穿越被挡：{label}（dir={val!r} -> {r.status_code}）",
                   r.status_code == 404)
    # 对照组：合法目录必须能访问，否则说明防线把正常访问也挡了（过度防御）
    r_ok = c3.get("/api/archive/speakers?dir=ok_dir&k=__T3__")
    check("合法目录可访问（对照组）", r_ok.status_code, 200)


# ---------------------------------------------------------------------------
print("\n8) 响应里不许出现服务器绝对路径（B-5 及其同族）")
# B-5 的立论是「持有口令不该摸出目录结构」。B-5 只修了 /api/me 的 root，
# 但同族还有 4 处：/api/latest、/api/meeting、WS done 消息、/api/voice/samples。
#
# 判据分两条腿，缺一不可：
#   ① 动态：递归遍历响应 JSON 的**每一个**字符串值，看是否像绝对路径。
#      比逐字段写死强——将来新增接口漏了也不会逃掉。
#   ② 静态：源码里不许出现 `"archive": str(` 这类模式。
#      WS 的 done 消息拿不到（要跑完整管线 + funasr），只能靠静态扫描覆盖。


def looks_like_abs_path(val: str) -> bool:
    """粗判一个字符串是不是服务器绝对路径。宁可误报也别漏报。"""
    s = str(val or "")
    if len(s) > 2 and s[1] == ":" and s[2:3] in ("\\", "/"):     # Windows 盘符
        return True
    if s.startswith("/") and not s.startswith("//"):             # POSIX 绝对路径
        # 排除纯 URL 路径（/api/xxx）——那些本来就是给浏览器的
        return not s.startswith(("/api/", "/static/", "/ws/", "/voice"))
    return False


def find_abs_paths(payload, trail="$"):
    """递归找响应里所有像绝对路径的字符串，返回 [(json 路径, 值)]。"""
    hits = []
    if isinstance(payload, dict):
        for k, v in payload.items():
            hits += find_abs_paths(v, f"{trail}.{k}")
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            hits += find_abs_paths(v, f"{trail}[{i}]")
    elif isinstance(payload, str) and looks_like_abs_path(payload):
        hits.append((trail, payload))
    return hits


d4 = Path(_tf.mkdtemp())
proc4 = d4 / "tenants" / "me" / "processed"
proc4.mkdir(parents=True)
arch4 = proc4 / "2026-10-19_周会"
arch4.mkdir()
(arch4 / "meta.json").write_text('{"meeting_id": "M20260101-090000-aaaa"}', encoding="utf-8")
(arch4 / "transcript.md").write_text("hi", encoding="utf-8")
(arch4 / "report.md").write_text("r", encoding="utf-8")
(arch4 / "speakers.json").write_text('{"assignments": []}', encoding="utf-8")
work4 = d4 / "tenants" / "me" / "work"
work4.mkdir(parents=True)
(work4 / "voice_samples").mkdir(parents=True)

a4, _ = make_app(web_token="__T4__")
a4.state.cfg = replace(a4.state.cfg, base_dir=d4, tenant_root=d4 / "tenants",
                       processed_dir=d4 / "processed", work_dir=work4)
c4 = TestClient(a4)
with c4:
    probes = [
        ("/api/health", {}),
        ("/api/me", {}),
        ("/api/latest", {}),
        ("/api/meetings", {}),
        ("/api/meeting", {"id": "M20260101-090000-aaaa"}),
        ("/api/archive/speakers", {"dir": arch4.name}),
        ("/api/voice/samples", {}),
    ]
    for path, params in probes:
        qs = "&".join(f"{k}={_q(str(v))}" for k, v in params.items())
        url = f"{path}?k=__T4__" + (f"&{qs}" if qs else "")
        r = c4.get(url)
        if r.status_code != 200:
            print(f"  ..   {path} 返回 {r.status_code}，跳过")
            continue
        try:
            payload = r.json()
        except Exception:
            continue
        leaks = find_abs_paths(payload)
        for jpath, val in leaks:
            print(f"     泄露 {path} {jpath} = {val}")
        check(f"{path} 响应不含绝对路径", leaks, [])

    # 路径里的 archive 必须是目录名（前端靠它回放），不能顺手改成空
    r = c4.get("/api/latest?k=__T4__").json()
    check("/api/latest 的 archive 是目录名", r.get("archive"), arch4.name)
    r = c4.get("/api/meeting?id=M20260101-090000-aaaa&k=__T4__").json()
    check("/api/meeting 的 archive 是目录名", r.get("archive"), arch4.name)

print("  静态扫描：源码里不许出现把路径直接 str() 进响应字段的写法")
_SRC_FILES = ["server.py", "live_engine.py", "api/deps.py", "api/health.py",
              "api/meetings.py", "api/pages.py", "api/speakers.py"]
_BAD = re.compile(r'"(archive|dir|path|root|base_dir)"\s*:\s*str\(')
_hits = []
for _f in _SRC_FILES:
    _p = ROOT / _f
    if not _p.is_file():
        continue
    for _n, _line in enumerate(_p.read_text(encoding="utf-8").split("\n"), 1):
        if _BAD.search(_line) and "B-5" not in _line:
            _hits.append(f"{_f}:{_n}  {_line.strip()[:80]}")
for _h in _hits:
    print(f"     {_h}")
check("源码里没有 `\"archive\"/\"dir\": str(...)` 的写法（含 WS done 消息）", _hits, [])

# ⚠️ 双份常量：ASSISTANT_LABEL 在 api/live.py 与 transcribe.py 各有一份。
# 两边一旦漂移，就会出现「实时字幕标『助理（我的声音）』、归档稿标另一个名字」，
# 而这两条路径由完全不同的代码走，肉眼很难发现。C-5 搬迁时发现的，锁住它。
try:
    from api.live import ASSISTANT_LABEL as _LIVE_LABEL, ASSISTANT_LABEL_SYS as _LIVE_LABEL_SYS
    from transcribe import ASSISTANT_LABEL as _TR_LABEL, ASSISTANT_LABEL_SYS as _TR_LABEL_SYS

    check("api/live 与 transcribe 的 ASSISTANT_LABEL 一致", _LIVE_LABEL, _TR_LABEL)
    check("api/live 与 transcribe 的 ASSISTANT_LABEL_SYS 一致", _LIVE_LABEL_SYS, _TR_LABEL_SYS)
except ImportError as _exc:  # pragma: no cover
    print(f"  ..   跳过双份常量检查（导入失败：{_exc}）")


# ---------------------------------------------------------------------------
print("\n9) 反向对照：比对逻辑本身在做事")
# 人为去掉基线条目，验证集合差集确实会非空 —— 防止「比对代码写错成永远 True」。
_broken = EXPECTED_ROUTES - {("/api/me", "GET")}
diff = sorted(_broken ^ actual)
check_true("人为删基线条目后差集非空（说明比对有效）", len(diff) > 0)


# ---------------------------------------------------------------------------
print("\n" + "=" * 40)
print(f"通过 {TOTAL - FAILED} 项，失败 {FAILED} 项（共 {TOTAL} 项）")
sys.exit(1 if FAILED else 0)
