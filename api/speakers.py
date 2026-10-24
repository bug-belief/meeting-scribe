"""说话人认领：把「未知 Speaker N」变成真名（O-11 C-3）。

4 条路由：`/api/speakers` `/api/archive/speakers`
          `/api/archive/cluster_audio` `/api/archive/claim`

`cluster_audio` 会给某个簇剪 3~6 秒代表音频，让人**先听一耳朵再认领**——
只听声纹相似度数字下判断，很容易把「像」当成「是」。
"""

from __future__ import annotations

import asyncio
import json
import subprocess

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from api.deps import archive_dir, archive_source, cfg_of, tenant
from utils import get_logger

logger = get_logger("api.speakers")


def register(app: FastAPI) -> None:
    @app.get("/api/speakers")
    async def speakers_list(request: Request):
        """当前租户的声纹库（人名 + 模板数 + 信道），用于认领时自动补全。"""
        ts = tenant(request)
        try:
            from speakers import SpeakerStore

            store = SpeakerStore(ts.speaker_store, ts.speaker_max_templates)
            return {"ok": True, "names": store.names(), "people": store.summary()}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.get("/api/archive/speakers")
    async def archive_speakers(request: Request, dir: str = ""):
        folder = archive_dir(request, dir)
        if folder is None:
            return JSONResponse({"ok": False, "error": "目录不存在"}, status_code=404)
        path = folder / "speakers.json"
        if not path.is_file():
            return {"ok": True, "channel": "", "assignments": [],
                    "note": "这次录音没有声纹结果（可能是单人/纯系统声音）"}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return JSONResponse({"ok": False, "error": f"speakers.json 坏了：{exc}"},
                                status_code=500)
        items = []
        for a in data.get("assignments") or []:
            cluster = int(a.get("cluster", 0))
            items.append({
                "cluster": cluster,
                "label": a.get("label") or f"Speaker {cluster + 1}",
                "name": a.get("name"),
                "status": a.get("status") or "unknown",
                "score": round(float(a.get("score") or 0), 3),
                "duration": round(float(a.get("duration") or 0), 1),
                "windows": int(a.get("windows") or 0),
                "candidates": a.get("candidates") or [],
            })
        return {"ok": True, "channel": data.get("channel") or "unknown",
                "mode": data.get("mode") or "", "assignments": items}

    @app.get("/api/archive/cluster_audio")
    async def cluster_audio(request: Request, dir: str = "", cluster: int = -1):
        """给某个簇剪 3~6 秒代表音频，认领前先听一耳朵。"""
        folder = archive_dir(request, dir)
        if folder is None:
            return PlainTextResponse("目录不存在", status_code=404)
        src = archive_source(folder)
        if src is None:
            return PlainTextResponse("这次录音没有音频", status_code=404)

        start, dur = 0.0, 4.0
        seg_file = folder / "transcript.json"
        if seg_file.is_file():
            try:
                segs = json.loads(seg_file.read_text(encoding="utf-8")).get("segments") or []
                mine = [s for s in segs if int(s.get("speaker", -1)) == int(cluster)]
                if mine:
                    best = max(mine, key=lambda s: float(s.get("end", 0)) - float(s.get("start", 0)))
                    start = max(0.0, float(best.get("start", 0)) - 0.25)
                    dur = min(6.0, float(best.get("end", 0)) - start + 0.25)
            except Exception:
                pass

        ts = tenant(request)
        cache = ts.work_dir / "_cluster"
        cache.mkdir(parents=True, exist_ok=True)
        out = cache / f"{folder.name}-c{int(cluster)}-{int(start)}-{int(dur)}.mp3"
        if not out.is_file():
            cmd = [
                cfg_of(request).ffmpeg_bin, "-hide_banner", "-nostdin", "-y",
                "-ss", f"{start:.2f}", "-t", f"{dur:.2f}", "-i", str(src),
                "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", str(out),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0 or not out.is_file():
                logger.warning("切片失败：%s", (proc.stderr or "")[-300:])
                return PlainTextResponse("切片失败", status_code=500)
        return FileResponse(out, media_type="audio/mpeg")

    @app.post("/api/archive/claim")
    async def archive_claim(request: Request):
        """{"dir": "...", "cluster": 0, "name": "张三"} → 质心入库 + 改写本次稿子。"""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "请求体不是 JSON"}, status_code=400)
        folder = archive_dir(request, str(body.get("dir") or ""))
        if folder is None:
            return JSONResponse({"ok": False, "error": "目录不存在"}, status_code=404)
        name = str(body.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "名字不能为空"}, status_code=400)
        try:
            cluster = int(body.get("cluster"))
        except Exception:
            return JSONResponse({"ok": False, "error": "cluster 必须是整数"}, status_code=400)

        ts = tenant(request)
        profile = body.get("profile")
        try:
            from speakers import SpeakerStore, claim_cluster

            store = SpeakerStore(ts.speaker_store, ts.speaker_max_templates)
            result = await asyncio.to_thread(
                claim_cluster, folder, store, cluster, name, body.get("channel"),
                True, profile,
            )
        except Exception as exc:
            logger.warning("认领失败：%s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        logger.info("认领：%s 的 cluster=%s → %s［%s］（%s）", folder.name, cluster, name,
                    result.get("profile"), result.get("reason"))
        return {"ok": True, **result}
