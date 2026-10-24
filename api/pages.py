"""页面与 PWA 清单 + 启动预热（O-11 C-2）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from utils import get_logger

logger = get_logger("api.pages")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def register(app: FastAPI) -> None:
    @app.on_event("startup")
    async def _warm_kb() -> None:
        """后台预热知识库索引，别让 4.8 万条法条的建索引成本砸在第一次代答上。"""
        try:
            import kb_bridge

            asyncio.create_task(asyncio.to_thread(kb_bridge.warm))
        except Exception:
            logger.debug("知识库预热未启动", exc_info=True)

    @app.get("/")
    async def index():
        page = WEB_DIR / "index.html"
        if not page.is_file():
            return PlainTextResponse("web/index.html 不存在", status_code=500)
        return FileResponse(page, media_type="text/html; charset=utf-8")

    @app.get("/manifest.webmanifest")
    async def manifest():
        """PWA 清单。放在根路径是为了 scope 覆盖 `/`，不用额外配根。"""
        p = WEB_DIR / "manifest.webmanifest"
        if not p.is_file():
            return PlainTextResponse("manifest 不存在", status_code=404)
        return FileResponse(p, media_type="application/manifest+json")

    @app.get("/voice")
    async def voice_page():
        page = WEB_DIR / "voice.html"
        if not page.is_file():
            return PlainTextResponse("web/voice.html 不存在", status_code=500)
        return FileResponse(page, media_type="text/html; charset=utf-8")

    @app.post("/api/logout")
    async def logout(request: Request):
        """退出当前口令，回到输入口令页（多租户换人用的唯一出口）。

        多租户是靠「口令 → owner」解析出来的（见 server.auth_gate），
        所以换个人用本机只能换口令。此前没有出口：得手动去开发者工具清 cookie，
        手机上根本做不到，只能卸载重装。这个接口就是补这个洞。

        ⚠️ 三个必须对齐的点，少一个就"看着成功了其实没退"：
          ① **path 必须与 set_cookie 时一致**。cookie 的删除条件是
             「name + domain + path」三者相同；path 对不上就是删另一个 cookie，
             浏览器里那份原封不动，下次访问照样放行。
          ② **secure 必须跟着本次请求的协议走**，不能写死 true/false。
             内网直连是 http（cookie 当初按非 secure 写入），Funnel 在 Tailscale
             边缘 TLS 终结、转发进来是 http，真相在 x-forwarded-proto。
             写死 secure=True 会让 http 下删不掉非 secure 的 cookie。
          ③ **只清 cookie，不停 WS**。正在录音时切账号不该在这里被"顺手停掉"：
             停止要做整理归档，中途跳走等于丢一场录音。前端会先拦住录音态。

        故意不返回 200 以外的东西：前端只需要知道"删了没"，
        至于删完后能不能进，由 `/` 的 401 + 激活页决定。
        """
        proto = (
            request.headers.get("x-forwarded-proto") or request.url.scheme or ""
        ).lower()
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(
            "wb_k",
            path="/",
            httponly=True,
            samesite="lax",
            secure=(proto == "https"),
        )
        return resp
