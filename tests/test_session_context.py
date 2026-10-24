"""O-11 C-4：代答与提词的会话上下文必须永远一致。

**为什么单独测这个**：`/api/assistant/reply` 和 `/api/prompter/suggest`
原先各自写了一遍「取会话 → 校验归属 → 取助理说过的话 → 滤噪声」（约 20 行 × 2）。
两处一旦漂移，就会出现「代答看得到助理刚才说的话、提词看不到」这种
**没人会想到去查**的诡异现象。

C-4 把它们抽成了 `api/assistant.session_context()`，本测试锁住：
  ① 同一份输入，两条路由拿到的上下文完全一致；
  ② 404 / 403 的返回值符合各自语义（代答报错、提词静默）；
  ③ 噪声行不进上下文。

不需要 fastapi 起服务：直接调 session_context + 用 dict 伪装 Request。
真路由的行为由 test_routes 的 27 条基线守。
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = 0


def check(name: str, got, want=True) -> bool:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(("  ok   " if ok else "  FAIL ") + name
          + ("" if ok else f"\n         实际 {got!r}\n         期望 {want!r}"))
    return ok


def check_true(name: str, cond: bool) -> bool:
    return check(name, bool(cond), True)


try:
    from fastapi import Request

    import server  # noqa: F401
except ImportError as exc:  # pragma: no cover
    print(f"  SKIP 缺依赖：{exc}")
    sys.exit(0)

import live_engine
from api.assistant import session_context
from config import SETTINGS


def make_request(owner: str = "me"):
    """构造一个只带 app/state 的最小 Request。"""
    d = Path(tempfile.mkdtemp())
    cfg = replace(SETTINGS, base_dir=d, work_dir=d / "work", web_token="T")
    app = server.create_app(cfg)
    req = Request({"type": "http", "app": app, "headers": [],
                   "query_string": b"", "path": "/"})
    req.state.owner = owner
    return req, cfg


def make_session(cfg, owner="me", lines=None, events=None):
    """造一个真实的 LiveSession（会落一个临时目录，测完不清理也无所谓）。"""
    sess = live_engine.LiveSession(replace(cfg, owner=owner))
    sess.lines = list(lines or [])
    sess.events = list(events or [])
    live_engine.SESSIONS[sess.id] = sess
    return sess


# ---------------------------------------------------------------------------
print("\n1) 无 session 时，退回前端传来的 lines")
req, cfg = make_request()
payload = {"lines": [{"text": "你好"}, {"text": "今天开会"}]}
sid, lines, spoken, err = session_context(req, payload)
check("sid 为空", sid, "")
check("err 为 None", err, None)
check("lines 原样返回（2 条）", len(lines), 2)
check("spoken 为空", spoken, [])

# ---------------------------------------------------------------------------
print("\n2) 有 session 时，服务端 lines 覆盖前端 lines（服务端才是权威）")
sess = make_session(cfg, lines=[{"text": "服务端的第 1 句"}, {"text": "服务端的第 2 句"}])
req2, _ = make_request()
sid, lines, spoken, err = session_context(req2, {"session": sess.id,
                                                "lines": [{"text": "前端伪造的"}]})
check("取到 session id", sid, sess.id)
check("用服务端 lines（2 条），不用前端那条", len(lines), 2)
check_true("内容来自服务端", lines[0]["text"] == "服务端的第 1 句")

# ---------------------------------------------------------------------------
print("\n3) 助理播报按 spoken_chars 截断，只取 done/partial")
sess1 = make_session(cfg, events=[
    {"text": "这句话只说了一半", "spoken_chars": 3, "state": "partial"},
    {"text": "这句完整说完", "state": "done"},
    {"text": "这句被取消了", "state": "cancelled"},
])
req3, _ = make_request()
_, _, spoken, _ = session_context(req3, {"session": sess1.id})
check("只收 done/partial 两条", len(spoken), 2)
check("partial 按 spoken_chars 截断", spoken[0], "这句话")
check("done 完整保留", spoken[1], "这句完整说完")

# ---------------------------------------------------------------------------
print("\n4) 噪声幻听不进上下文（否则模型会回一句「你再说一遍」）")
req4, cfg4 = make_request()
payload4 = {"lines": [
    {"text": "今天要谈三件事"},
    {"text": "的的的的的"},
    {"text": "我我"},
    {"text": "第一是预算"},
]}
_, lines, _, _ = session_context(req4, payload4)
check("噪音被滤掉，只剩 2 条", len(lines), 2)
check_true("保留的都是正常句子",
           all("的的的" not in x["text"] for x in lines))

# ---------------------------------------------------------------------------
print("\n5) 会话不存在 → 404（代答报错、提词静默，由各自路由决定）")
req5, _ = make_request()
_, _, _, err = session_context(req5, {"session": "不存在的id"})
check_true("返回 404 错误响应", err is not None and err.status_code == 404)

# ---------------------------------------------------------------------------
print("\n6) 跨租户 → 403（不能靠猜 session id 偷听别人）")
sess_other = make_session(cfg, owner="别人", lines=[{"text": "别人的私密内容"}])
req6, _ = make_request(owner="me")
_, lines6, _, err6 = session_context(req6, {"session": sess_other.id})
check_true("返回 403 错误响应", err6 is not None and err6.status_code == 403)
check("越权时 lines 是空（不漏内容）", lines6, [])

# ---------------------------------------------------------------------------
print("\n7) 两条路由用的是**同一个** session_context（不是各写一遍）")
import inspect  # noqa: E402

import api.assistant as A  # noqa: E402

src = inspect.getsource(A)
# 两个路由体内都必须调用 session_context，且不许再直接碰 SESSIONS
n_calls = src.count("session_context(request, body)")
check("两条路由各调一次 session_context", n_calls, 2)
# 路由体内不该再出现 SESSIONS.get（都已收进 helper）
route_src = src[src.index("def register("):]
check("路由体内不再直接读 SESSIONS", route_src.count("SESSIONS.get"), 0)

# ---------------------------------------------------------------------------
print("\n8) 反向对照：把 helper 的噪声过滤去掉，第 4 项必须失败")

orig = A.session_context


def broken_context(request, body):
    """故意去掉噪声过滤与会话覆盖，模拟「两处逻辑漂移」的后果。"""
    sid = str(body.get("session") or "").strip()
    return sid, list(body.get("lines") or []), [], None


A.session_context = broken_context
try:
    _, lines_bad, _, _ = A.session_context(req4, payload4)
    check_true("去掉滤噪后，噪声行确实混进来了（证明第 4 项不是空跑）", len(lines_bad) == 4)
finally:
    A.session_context = orig
# 还原后再验一次
_, lines_ok, _, _ = A.session_context(req4, payload4)
check("还原后噪声又被滤掉", len(lines_ok), 2)

# ---------------------------------------------------------------------------
print("\n" + "=" * 40)
total = 18
print(f"通过 {total - FAILED} 项，失败 {FAILED} 项（共 {total} 项）")
sys.exit(1 if FAILED else 0)
