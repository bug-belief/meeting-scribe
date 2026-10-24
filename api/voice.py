"""语音分身：TTS 反代、音色列表、样本、快速转写（O-11 C-4）。

6 条路由：`/api/tts` `/api/tts/health` `/api/tts/voices`
          `/api/voice/samples` `/api/voice/sample` `/api/asr/quick`

`tts_server.py` 只监听 127.0.0.1（独立 venv，避免 transformers 版本打架），
页面走这里转一手，鉴权仍由本服务的 cookie 中间件负责。

⚠️ 原来这几个函数闭包捕获 `cfg`（模块级 SETTINGS）。改成 `cfg_of(request)`
   不只是洁癖：`asset_dir` 与 `tts_base_url` 都从 cfg 派生，
   读错 cfg 就会出现「页面拿到的样本目录和服务实际用目录不是一个」。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

import live_engine
from api.deps import cfg_of, tenant, voice_allowed, voice_owners, voice_visible
from utils import get_logger

logger = get_logger("api.voice")

VOICE_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}


def voice_dir(request: Request) -> Path:
    """测试音频目录（`<work>/voice_samples`）。

    ⚠️ 注意：这里用 `cfg_of(request).work_dir`（**全局** work_dir），
    不是 `tenant(request).work_dir`。也就是说所有租户共用同一个样本目录——
    这是**原来的行为**（原代码是闭包变量 `VOICE_DIR = cfg.work_dir / ...`），
    本批只做搬迁、不改语义。

    但这个目录里的文件是「测试音频样本」，谁都能通过 `/api/voice/sample` 取到。
    多租户真正启用前需要决定：是按租户隔离，还是明确它是公共素材。
    （挂在这里当待办，别等到开了第二个租户才发现。）
    """
    return cfg_of(request).work_dir / "voice_samples"


def register(app: FastAPI) -> None:
    # ---------- 语音分身：把合成服务反代出去 ----------
    @app.get("/api/tts/health")
    async def tts_health(request: Request):
        base = cfg_of(request).tts_base_url

        def _get() -> dict:
            import urllib.request

            with urllib.request.urlopen(f"{base}/health", timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            return {"ok": True, **await asyncio.to_thread(_get)}
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"合成服务没起来：{exc}", "base": base},
                status_code=503,
            )

    @app.get("/api/tts/voices")
    async def tts_voices(request: Request):
        base = cfg_of(request).tts_base_url

        def _get() -> dict:
            import urllib.request

            with urllib.request.urlopen(f"{base}/voices", timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            data = await asyncio.to_thread(_get)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

        # 别人的克隆音色不该出现在下拉里。系统音色全放行。
        ts = tenant(request)
        table = voice_owners(ts)
        data["voices"] = [v for v in (data.get("voices") or [])
                          if voice_visible(v, ts.owner, table)]
        return data

    @app.post("/api/tts")
    async def tts_say(request: Request):
        """{"text": "...", "voice": "...", "speed": 1.0} → audio/wav"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "请求体不是 JSON"}, status_code=400)
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"ok": False, "error": "文本为空"}, status_code=400)

        # 克隆音色只许本人用。拿别人的声纹念任意一句话 = 冒充他本人说过，
        # 这不是数据隔离问题而是身份问题，所以在这里硬拦、不回退。
        ts = tenant(request)
        if not voice_allowed(str(body.get("voice") or ""), ts.owner, voice_owners(ts)):
            logger.warning("拒绝跨租户合成：%s 试图使用音色 %s",
                           ts.owner, body.get("voice"))
            return JSONResponse({"ok": False, "error": "这个音色不属于你"}, status_code=403)

        base = cfg_of(request).tts_base_url

        def _post() -> tuple[bytes, dict]:
            import urllib.request

            payload = json.dumps({
                "text": text,
                "voice": str(body.get("voice") or ""),
                "speed": float(body.get("speed") or 1.0),
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{base}/tts", data=payload, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.read(), dict(resp.headers)

        try:
            import time as _time

            t0 = _time.perf_counter()
            audio, headers = await asyncio.to_thread(_post)
            # 电费按「机器转了多久」算，不是按音频多长——合成 30 秒的话机器要跑十几秒
            compute_sec = _time.perf_counter() - t0
        except Exception as exc:
            logger.warning("合成失败：%s", exc)
            return JSONResponse({"ok": False, "error": f"合成失败：{exc}"}, status_code=502)

        # 记账：缓存命中不重复收费——同一句话念第二遍不该算两次钱。
        try:
            from costs import log as log_cost, tts_cost

            cached = str(headers.get("X-Cache") or "").lower().startswith("hit")
            billed = tts_cost(
                text, model=str(body.get("model") or ""), cached=cached, compute_sec=compute_sec
            )
            log_cost(
                "tts",
                ref=str(body.get("session") or body.get("ref") or ""),
                scene=str(body.get("scene") or "say"),
                owner=ts.owner,
                **billed,
            )
        except Exception as exc:  # pragma: no cover - 记账永远是可选的
            logger.debug("TTS 成本记账跳过：%s", exc)

        return Response(
            content=audio,
            media_type="audio/wav",
            headers={
                "X-Cache": headers.get("X-Cache", ""),
                "X-Audio-Seconds": headers.get("X-Audio-Seconds", ""),
                "X-Rtf": headers.get("X-Rtf", ""),
            },
        )

    # ---------- 测试音频样本 ----------
    @app.get("/api/voice/samples")
    async def voice_samples(request: Request):
        items: list[dict] = []
        vdir = voice_dir(request)
        if vdir.is_dir():
            for p in sorted(vdir.iterdir()):
                if p.is_file() and p.suffix.lower() in VOICE_EXTS:
                    items.append({"name": p.name, "size": p.stat().st_size})
        # ⚠️ B-5 同族：原来返回 str(VOICE_DIR)（服务器绝对路径），
        # 而 voice.html 只把它拼进一句提示文案（"测试音频 N 个，来自 <dir>"）。
        # 改成目录名：信息量不减，路径不外露。
        return {"ok": True, "dir": vdir.name, "samples": items}

    @app.get("/api/voice/sample")
    async def voice_sample(request: Request, name: str = ""):
        """只回 voice_samples 目录里的文件，拒绝任何路径穿越。"""
        vdir = voice_dir(request)
        target = vdir / Path(name).name
        if not name or not target.is_file() or target.parent != vdir:
            return PlainTextResponse("找不到该音频", status_code=404)
        return FileResponse(target)

    # ---------- 快速转写（给「录音色」取逐字稿用） ----------
    # 只跑 ASR + 标点，不做说话人分离、不落归档、不调 DeepSeek。
    # 离线模型首次调用要加载约 1.2 GB，之后常驻，和流式模型互不干扰。
    # 落盘目录按租户派生（tenant(request).work_dir / "quick"），
    # 不要用全局 cfg 预置一个 QUICK_DIR —— 那会串租户目录。
    def _offline(request: Request):
        if live_engine.OFFLINE_ENGINE is None:
            from diarize import FunASREngine

            # 注意：离线引擎是**进程级单例**，只在第一次创建时绑 cfg，
            # 后续请求即便换了租户也复用它（模型 1.2GB，不可能每租户一份）。
            # 这是现状行为，不是 bug——但目录必须按请求派生，见下面 quick_dir。
            live_engine.OFFLINE_ENGINE = FunASREngine(cfg_of(request))
        return live_engine.OFFLINE_ENGINE

    @app.post("/api/asr/quick")
    async def asr_quick(request: Request):
        raw = await request.body()
        if not raw:
            return JSONResponse({"ok": False, "error": "空请求体"}, status_code=400)
        if len(raw) > 32 * 1024 * 1024:
            return JSONResponse({"ok": False, "error": "音频太大"}, status_code=413)
        quick_dir = tenant(request).work_dir / "quick"
        quick_dir.mkdir(parents=True, exist_ok=True)
        tmp = quick_dir / f"{uuid.uuid4().hex}.wav"
        tmp.write_bytes(raw)
        try:
            engine = _offline(request)

            def _run() -> str:
                text = "".join(t.text for t in engine.tokens(tmp))
                return engine.punctuate(text) if text else ""

            text = await asyncio.to_thread(_run)
        except Exception as exc:
            logger.warning("快速转写失败：%s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        finally:
            tmp.unlink(missing_ok=True)
        if not text.strip():
            return JSONResponse({"ok": False, "error": "没有识别到文字"}, status_code=422)
        return {"ok": True, "text": text.strip(), "chars": len(text.strip())}
