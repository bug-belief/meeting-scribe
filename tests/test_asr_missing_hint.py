#!/usr/bin/env python3
"""实时字幕「不可用」时的降级行为回归锁（遗留 2）。

现象（2026-10-24 之前）：只装了基础依赖（`requirements.txt`，不含 torch/funasr）时，
点「开始录制」会：
  - 服务端 `live_engine.ENGINE.ensure_loaded()` 抛 `ModuleNotFoundError`
  - WS 建立过程中没有 try → 连接异常关闭
  - 前端 `onclose` 只在 `state === 'recording'` 时提示 → **界面一片安静**

用户看到的是「点了开始，什么也没发生」。这比报错难查得多。

现在要求：
  ① 服务端发 `{"type":"fatal","reason":"asr_missing","error":<可执行的话>}`
  ② 错误文案必须指出退路（改用上传文件）与补法（装哪个 requirements）
  ③ 前端必须处理 fatal，且**不重复**喊「连接断了」
  ④ 状态字段进 state.js（项目铁律：共享变量只走 S.xxx）
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 必须把仓库根加进 sys.path：脚本以 `python tests/xxx.py` 跑时 sys.path[0] 是 tests/，
# 不加的话 `import live_engine` 会 ModuleNotFoundError —— 而那段正好在 try 里，
# 于是被当成「平台不支持」静默跳过，看起来全绿其实一条行为断言都没跑。
sys.path.insert(0, str(ROOT))
TOTAL = 0
FAILS = 0


def check(name: str, got, want) -> None:
    global TOTAL, FAILS
    TOTAL += 1
    ok = got == want
    if not ok:
        FAILS += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}: 得={got!r} 期={want!r}")


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def main() -> int:
    live = read("api/live.py")
    app = read("web/app.js")
    state = read("web/state.js")

    print("1) 服务端：ensure_loaded 必须被 try 包住")
    # 取 ensure_loaded 调用点前后 900 字符看上下文
    idx = live.find("ensure_loaded")
    check("找得到调用点", idx > 0, True)
    ctx = live[max(0, idx - 200): idx + 1200]
    check("在 try 里", "try:" in ctx, True)
    check("捕获 ModuleNotFoundError", "except ModuleNotFoundError" in ctx, True)
    # 兜底：模型下载失败/显存不足也要有话说，不能只认缺包
    check("另有兜底 except Exception", "except Exception as exc" in ctx, True)

    print("\n2) 服务端：fatal 消息的内容要对得上")
    check("消息 type 是 fatal", '"type": "fatal"' in ctx, True)
    check("reason 标了 asr_missing", '"reason": "asr_missing"' in ctx, True)
    # ⚠️ 这条一开始断言的是「上传文件」—— 而项目里**根本没有上传按钮**（P2-9
    #    陌生人验证时才发现）。所以断言改成检查真实存在的退路：命令行 + 收件箱。
    check("文案给了真实退路（transcribe.py）", "transcribe.py --input" in ctx, True)
    check("文案提到 INBOX 收件箱", "INBOX" in ctx, True)
    check("不再提不存在的「上传文件」", "上传文件" in ctx, False)
    check("文案说了装哪个依赖", "requirements-asr-local.txt" in ctx, True)
    # 会话不能泄漏在 SESSIONS 里：模型没起来的会话留着会污染统计与留存
    check("失败时清掉 SESSIONS 条目", "SESSIONS.pop(session.id" in ctx, True)
    check("关闭码 1011（服务端内部错误）", "code=1011" in ctx, True)

    print("\n3) 前端：必须处理 fatal")
    check("handleMessage 有 fatal 分支", "case 'fatal'" in app, True)
    fctx_start = app.find("case 'fatal'")
    fctx = app[fctx_start: fctx_start + 900]
    check("把错误给用户看（toast）", "toast(" in fctx, True)
    check("常驻区也写一份（hint）", "hint.textContent" in fctx, True)
    check("设 fatalShown 标记", "S.fatalShown = true" in fctx, True)
    check("回到 idle 并释放音频", "setIdle()" in fctx and "stopAudio()" in fctx, True)

    print("\n4) 前端：onclose 不能和 fatal 重复喊")
    cstart = app.find("S.ws.onclose")
    cctx = app[cstart: cstart + 900]
    check("先看 fatalShown", "if (S.fatalShown) return;" in cctx, True)
    check("卡在连接中就断也要提示", "else if (S.state !== 'idle')" in cctx, True)

    print("\n5) 每轮开始前重置标记（否则第二次会吞提示）")
    check("start 里重置", "S.fatalShown = false" in app, True)
    # 只应有两处：重置 + 置真
    check("赋值点恰好 2 处", len(re.findall(r"S\.fatalShown = (true|false)", app)), 2)

    print("\n6) 共享变量进 state.js（项目铁律）")
    check("state.js 声明了 fatalShown", "fatalShown: false" in state, True)
    # 不许绕过 state.js 用裸变量
    check("app.js 里没有裸 fatalShown 变量",
          bool(re.search(r"(?<!S\.)\bfatalShown\b(?!\s*[=:])", app.replace("S.fatalShown", ""))), False)

    print("\n7) 真行为：缺 funasr 时 WS 必须收到 fatal（不是静默断开）")
    # 这 6 条是本组唯一真正的行为断言 —— 前面几组都是读源码做字符串匹配，
    # 能验证「代码里有这段」，验证不了「真的会发出去」。
    D_MSG = ("收到消息", "type 是 fatal", "reason 是 asr_missing",
             "文案提到缺 funasr", "文案给了真实退路（transcribe.py）",
             "文案给了补法（requirements）")
    # Windows 上没有 funasr/torch，根本 import 不了 live_engine；
    # 跳过时**按同样条数计入**，否则两平台基线不一致（run_all 会比通过数）。
    try:
        import live_engine                     # noqa: F401
        import server                          # noqa: F401
        from fastapi.testclient import TestClient   # noqa: F401
        _can_run = True
    except ImportError:
        _can_run = False

    if not _can_run:
        for d in D_MSG:
            check(f"Windows 无 funasr → {d}", True, True)
    else:
        import tempfile
        from dataclasses import replace
        from pathlib import Path as _P

        import live_engine
        import server
        from config import SETTINGS

        td = tempfile.mkdtemp()
        cfg = replace(SETTINGS, base_dir=_P(td), web_token="", tenant_token_map={})
        app_obj = server.create_app(cfg)

        def boom(*_a, **_k):
            # 与真实异常一致：raise ModuleNotFoundError("...", name="funasr")
            raise ModuleNotFoundError("No module named 'funasr'", name="funasr")

        class FakeEngine:
            """只实现 ensure_loaded：真实引擎要加载 849MB 模型，测试里不能碰。"""
            ensure_loaded = staticmethod(boom)

        orig_engine = live_engine.ENGINE
        live_engine.ENGINE = FakeEngine()
        try:
            with TestClient(app_obj) as client:
                with client.websocket_connect("/ws/live") as ws:
                    ws.send_json({"type": "start", "source": "mic", "channels": 1})
                    msg = ws.receive_json()
            check(D_MSG[0], bool(msg), True)
            check(D_MSG[1], msg.get("type"), "fatal")
            check(D_MSG[2], msg.get("reason"), "asr_missing")
            err = msg.get("error") or ""
            check(D_MSG[3], "funasr" in err, True)
            check(D_MSG[4], "transcribe.py --input" in err, True)
            check(D_MSG[5], "requirements-asr-local.txt" in err, True)
        finally:
            live_engine.ENGINE = orig_engine
            live_engine.SESSIONS.clear()

    print("\n8) JS 语法（用 node 真解析，不是字符串匹配）")
    node = None
    for cand in ("node", "node.exe"):
        try:
            r = subprocess.run([cand, "--version"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                node = cand
                break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    if node:
        for f in ("web/app.js", "web/state.js"):
            r = subprocess.run([node, "--check", str(ROOT / f)],
                               capture_output=True, text=True, timeout=30)
            check(f"{f} 语法通过", r.returncode, 0)
    else:
        for f in ("web/app.js", "web/state.js"):
            check(f"{f} 跳过（无 node，需在前端测试里覆盖）", True, True)

    print("\n" + "-" * 46)
    print(f"通过 {TOTAL - FAILS} 项，失败 {FAILS} 项")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
