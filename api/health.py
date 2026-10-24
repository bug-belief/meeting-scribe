"""探活、身份、成本汇总、最近一场（O-11 C-2）。

**B-5：`/api/me` 不再返回 `root`**（原为 `str(_tenant(request).processed_dir)`）。
它把服务器绝对路径直接吐给任何持有口令的人，等于把目录结构报出去；
`/api/health` 早就刻意不返路径，这个接口是漏网的一处。
前端（`web/app.js` / `web/activate.html`）都只查 `r.ok`，不读 `root`，删掉无影响。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from api.deps import meeting_id, tenant
from config import read_build, read_version

# 启动时读一次。版本号是「这份代码是什么」，运行期不会变，不必每请求读文件。
# 真源是仓库根的 VERSION，见 config.read_version 的说明。
_APP_VERSION = read_version()


def register(app: FastAPI) -> None:
    @app.get("/api/cost")
    async def cost_summary(request: Request, ref: str = "", since: str = "", limit: int = 20):
        """成本查询。

        `?ref=<meeting_id 或 session>` 查一场；`?since=2026-10` 查区间；
        什么都不传则返回「有记账的场次」列表，供前端选。
        """
        from costs import aggregate, read_records, recent_refs, render_markdown, usage_log

        ts = tenant(request)
        include = bool(request.query_params.get("all"))
        if not ref and not since:
            rows = recent_refs(limit=limit)
            if not include:
                rows = [r for r in rows if r["ref"].startswith("M") or r["ref"].startswith("L")]
            return {"ok": True, "file": str(usage_log()), "owner": ts.owner,
                    "recent": rows, "summary": aggregate([])}

        records = read_records(ref=ref, since=since)
        if ts.owner:
            # 账单是租户级隐私：同机另一个口令不该看到别人的场次花销
            records = [x for x in records
                       if not x.get("owner") or x.get("owner") == ts.owner]
        summary = aggregate(records)
        return {"ok": True, "ref": ref, "since": since, "summary": summary,
                "markdown": render_markdown(summary)}

    @app.get("/api/health")
    async def health():
        # 这个接口免鉴权（给探活 / 手机端首屏判断用），所以只回「活着没」，
        # 不回任何路径、设备、租户数——那等于把内部结构报给扫描器。
        # version 只给 semver：它本来就印在 APK 的 versionName 里，不算秘密；
        # 但 git 提交号能推出代码状态，只放在鉴权后的 /api/me 里。
        import live_engine

        return {
            "ok": True,
            "version": _APP_VERSION,
            "model_loaded": bool(live_engine.ENGINE and live_engine.ENGINE.loaded),
            "sessions": len(live_engine.SESSIONS),
        }

    @app.get("/api/me")
    async def me(request: Request):
        """当前口令对应的租户（页面上可以显示「当前身份」）。

        ⚠️ 不返回任何服务器路径。B-5 之前这里有 `root` 字段（processed_dir 绝对路径），
        属于「持有口令就能摸出目录结构」的信息泄露；health 早就守住了这条，这里是漏网。
        """
        return {"ok": True, "owner": request.state.owner,
                "build": read_build()}

    @app.get("/api/latest")
    async def latest(request: Request):
        """最近一次归档（供刷新页面后回看）。"""
        ts = tenant(request)
        dirs = sorted(
            (p for p in ts.processed_dir.iterdir() if p.is_dir() and not p.name.startswith("_")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ) if ts.processed_dir.is_dir() else []
        if not dirs:
            return JSONResponse({"ok": False, "error": "还没有归档"}, status_code=404)
        target = dirs[0]
        # ⚠️ B-5 同族：这里原来返回 `str(target)`（服务器绝对路径）。
        # 前端 4 处用到 archive 的地方全都先 `.split(/[\\/]/).pop()` 取最后一段，
        # 所以直接返回目录名，前端结果完全不变、且不再吐路径。
        out = {"ok": True, "archive": target.name, "meeting_id": meeting_id(target)}
        for name in ("transcript.md", "report.md"):
            path = target / name
            if path.is_file():
                out[name.split(".")[0]] = path.read_text(encoding="utf-8")
        return out
