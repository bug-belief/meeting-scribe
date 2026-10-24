"""归档浏览：按 ID 定位、列表、收藏、回放（O-11 C-3）。

4 条路由：`/api/meeting` `/api/meetings` `/api/meeting/star` `/api/archive/audio`

回放走 ffmpeg 转 mp3 并缓存到 `<work>/_playback/<owner>/`；原始 wav 一概不动，
缓存可以随时清空、按需重建。
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from api.deps import (
    FAVORITE_NAME,
    archive_dir,
    archive_source,
    cfg_of,
    find_meeting,
    meeting_id,
    tenant,
)
from utils import get_logger

logger = get_logger("api.meetings")


def register(app: FastAPI) -> None:
    @app.get("/api/meeting")
    async def meeting(request: Request, id: str = ""):
        """按 meeting_id 定位一次录音。

        `GET /api/meeting?id=M20260101-090000-aaaa`
        返回音频地址、转写稿、纪要、归属人和归档目录——用户只报一个 ID，
        音频 / 稿子 / 纪要 / 用户就对上了，不用再猜是哪一段。
        """
        mid = (id or "").strip()
        if not mid:
            return JSONResponse({"ok": False, "error": "缺少 id"}, status_code=400)
        ts = tenant(request)
        if not ts.processed_dir.is_dir():
            return JSONResponse({"ok": False, "error": "还没有归档"}, status_code=404)

        hit = find_meeting(ts, mid)
        if hit is None:
            return JSONResponse({"ok": False, "error": f"没有找到 ID 为 {mid} 的录音"},
                                status_code=404)

        out: dict = {
            "ok": True, "meeting_id": meeting_id(hit), "owner": ts.owner,
            # ⚠️ B-5 同族：原来返回 str(hit) 绝对路径。前端只用最后一段目录名
            # （setPlayback 与 loadSpeakers 都先 split(/[\\/]/).pop()），改 basename 无副作用。
            "dir": hit.name, "archive": hit.name,
            "audio_url": "/api/archive/audio?dir=" + quote(hit.name),
            "created": datetime.fromtimestamp(hit.stat().st_mtime).isoformat(timespec="seconds"),
            # 收藏 = 免清理。见 deploy/retention.py：标了这个标记的目录，音频不会被删
            "starred": (hit / FAVORITE_NAME).is_file(),
        }
        for name, key in (("transcript.md", "transcript"), ("report.md", "report")):
            path = hit / name
            if path.is_file():
                out[key] = path.read_text(encoding="utf-8")
        meta = hit / "meta.json"
        if meta.is_file():
            try:
                info = json.loads(meta.read_text(encoding="utf-8"))
                out["meta"] = {k: info.get(k) for k in
                               ("audio", "duration", "archived_at", "dual", "mic",
                                "elapsed_sec", "owner", "sha256", "size") if k in info}
            except Exception:
                pass
        return out

    @app.get("/api/meetings")
    async def meetings(request: Request, limit: int = 20):
        """最近的归档列表（带 meeting_id），页面和排查时都用得到。"""
        ts = tenant(request)
        if not ts.processed_dir.is_dir():
            return {"ok": True, "items": []}
        dirs = sorted((p for p in ts.processed_dir.iterdir()
                       if p.is_dir() and not p.name.startswith("_")),
                      key=lambda p: p.stat().st_mtime, reverse=True)[: max(1, min(limit, 100))]
        items = []
        for folder in dirs:
            mid = meeting_id(folder)
            items.append({"meeting_id": mid, "dir": folder.name,
                          "starred": (folder / FAVORITE_NAME).is_file(),
                          "created": datetime.fromtimestamp(folder.stat().st_mtime)
                          .isoformat(timespec="seconds")})
        return {"ok": True, "owner": ts.owner, "items": items}

    @app.post("/api/meeting/star")
    async def meeting_star(request: Request):
        """收藏 / 取消收藏一次录音。

        收藏的唯一作用是**免于按天数清理音频**：转写稿和纪要本来就不清，
        但原始录音默认保留 60 天（见 deploy/retention.py），标了收藏就整个目录跳过。

        body: {"id": "M20260101-090000-aaaa" 或归档目录名, "star": true|false}
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        mid = str((body or {}).get("id") or "").strip()
        star = bool((body or {}).get("star", True))
        if not mid:
            return JSONResponse({"ok": False, "error": "缺少 id"}, status_code=400)

        ts = tenant(request)
        hit = find_meeting(ts, mid)
        if hit is None:
            return JSONResponse({"ok": False, "error": f"没有找到 ID 为 {mid} 的录音"},
                                status_code=404)

        fav = hit / FAVORITE_NAME
        if star:
            # 标记文件而不是改 meta.json：meta 会被重建流程覆写，标记文件不会
            fav.write_text(datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")
        elif fav.is_file():
            fav.unlink()
        return {"ok": True, "meeting_id": meeting_id(hit), "dir": hit.name,
                "starred": fav.is_file()}

    @app.get("/api/archive/audio")
    async def archive_audio(request: Request, dir: str = ""):
        """回放某次录音。双路分轨会混成单声道——等同于一个旁听者听到的全部声音。"""
        folder = archive_dir(request, dir)
        if folder is None:
            return PlainTextResponse("目录名不合法或不存在", status_code=400)
        ts = tenant(request)
        name = folder.name
        src = archive_source(folder)
        if src is None:
            return PlainTextResponse("这次录音没有音频", status_code=404)

        playback = cfg_of(request).work_dir / "_playback"
        playback.mkdir(parents=True, exist_ok=True)
        owner_dir = playback / ts.owner
        owner_dir.mkdir(parents=True, exist_ok=True)
        out = owner_dir / f"{name}.mp3"
        if not out.is_file() or out.stat().st_mtime < src.stat().st_mtime:
            cmd = [
                cfg_of(request).ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-i", str(src),
                "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-b:a", "64k", str(out),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0 or not out.is_file():
                logger.warning("回放转码失败，直接返回原始文件：%s", (proc.stderr or "")[-300:])
                return FileResponse(src)
        return FileResponse(out, media_type="audio/mpeg")
