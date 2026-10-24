"""代答与提词（O-11 C-4）。

2 条路由：`/api/assistant/reply` `/api/prompter/suggest`

两者共用同一份「会话上下文」：优先取服务端 `LiveSession.lines`（权威、且带
「我/对方」归属），会话已收尾或页面刷新过时才退回前端传上来的 lines。
区别只在**输出**：代答给一句能直接念的话（进 TTS），提词给几条极短提示（不进 TTS）。
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import live_engine
import llm_errors
from api.deps import log_llm, tenant
from live_engine import looks_like_noise
from utils import get_logger

logger = get_logger("api.assistant")


def error_body(exc: BaseException) -> dict:
    """把异常翻成前端能直接显示的一份结构。

    `error` 是**一句人话**（不是原始英文），前端拿它当红色提醒的主文案；
    `hint` 是「现在能做什么」；`detail` 留原文供排查（已截断）。
    分类优先用异常自带的 kind（summarize 抛 PipelineError 时已分好），
    没有才现场判——这样非 PipelineError 的意外异常也能兜住。
    """
    kind = getattr(exc, "kind", None)
    if kind in llm_errors.CATALOG:
        meta = llm_errors.CATALOG[kind]
        label, hint = meta["label"], getattr(exc, "hint", None) or meta["hint"]
        detail = llm_errors.classify(exc)["detail"]
    else:
        info = llm_errors.classify(exc)
        kind, label, hint, detail = info["kind"], info["label"], info["hint"], info["detail"]
    return {"ok": False, "error": label, "kind": kind, "hint": hint, "detail": detail}


def session_context(request: Request, body: dict) -> tuple[str, list, list[str], JSONResponse | None]:
    """从请求体 + 服务端会话取出 (sid, lines, spoken, 错误响应)。

    抽出来是因为代答和提词各写了一遍（约 20 行 × 2），
    两处逻辑必须永远一致——否则会出现「代答看得到助理说过的话、提词看不到」。

    返回的 `error` 非 None 时调用方应直接返回它。
    """
    ts = tenant(request)
    sid = str(body.get("session") or "").strip()
    lines = body.get("lines")
    spoken: list[str] = []

    if sid:
        sess = live_engine.SESSIONS.get(sid)
        if sess is None:
            return sid, [], [], JSONResponse(
                {"ok": False, "error": "会话不在了（已收尾？）"}, status_code=404)
        if sess.settings.owner != ts.owner:
            return sid, [], [], JSONResponse(
                {"ok": False, "error": "这个会话不属于当前用户"}, status_code=403)
        lines = sess.lines
        # 助理自己刚才说过的话：连续对话时模型要知道聊到哪儿了，
        # 否则每轮都当第一次见面。只取真的播出去过的（done / partial）。
        spoken = [
            str(e.get("text") or "")[: int(e.get("spoken_chars") or len(e.get("text") or ""))]
            for e in sess.events
            if e.get("state") in {"done", "partial"}
        ]

    if not isinstance(lines, list):
        lines = []
    elif lines:
        # 噪声幻听不进上下文：喂给模型只会换来一句「听着有点乱，你再说一遍」
        lines = [x for x in lines
                 if isinstance(x, dict) and not looks_like_noise(x.get("text"))]

    return sid, lines, spoken, None


def register(app: FastAPI) -> None:
    @app.post("/api/assistant/reply")
    async def assistant_reply(request: Request):
        """{"session": "...", "ask": ""} → 一句可以直接念的稿子。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "请求体不是 JSON"}, status_code=400)

        ts = tenant(request)
        sid, lines, spoken, err = session_context(request, body)
        if err is not None:
            return err

        try:
            from assistant import draft_reply

            result = await asyncio.to_thread(
                draft_reply, lines, ts, str(body.get("ask") or ""), 120, spoken,
                scenario=str(body.get("scenario") or "")
            )
        except Exception as exc:
            body = error_body(exc)
            logger.warning("代答失败（%s）：%s", body["kind"], exc)
            return JSONResponse(body, status_code=502)

        if not result.get("text"):
            # 模型没吐内容：同样走分类，别把 None / 空串直接丢给界面
            return JSONResponse(
                error_body(RuntimeError(result.get("error") or "AI 返回了空内容")),
                status_code=502)
        logger.info("代答（%d 句上下文）：%s", result.get("context_lines", 0), result["text"][:60])
        log_llm(ts, result, ref=sid, scene="assistant")
        return {"ok": True, **result}

    @app.post("/api/prompter/suggest")
    async def prompter_suggest(request: Request):
        """{"session": "...", "ask": ""} → 几条「你可以这么接」的短提示。

        和代答共用同一份会话上下文，但输出更短、不进 TTS。
        前端在「一句定稿」后防抖 1.2 秒才调一次，不会每来半句就烧一次 token。
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "请求体不是 JSON"}, status_code=400)

        ts = tenant(request)
        sid, lines, spoken, err = session_context(request, body)
        if err is not None:
            # 提词是「锦上添花」：会话没了（404）就静默返回空，不要弹错误打扰人。
            # 越权（403）仍然照原样报，不能吞。
            if err.status_code == 404:
                return {"ok": True, "items": [], "stale": True}
            return err

        try:
            from prompter import suggest

            result = await asyncio.to_thread(
                suggest, lines, ts, str(body.get("ask") or ""), spoken,
                scenario=str(body.get("scenario") or "meeting")
            )
        except Exception as exc:
            body = error_body(exc)
            logger.warning("提词失败（%s）：%s", body["kind"], exc)
            return JSONResponse(body, status_code=502)

        log_llm(ts, result, ref=sid, scene="prompter")
        return {"ok": True, **result}

