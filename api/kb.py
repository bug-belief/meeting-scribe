"""知识库检索、索引概况、场景清单（O-11 C-4）。

3 条路由：`/api/kb/search` `/api/kb/stats` `/api/kb/scenarios`

⚠️ 本模块只读 KB 的**索引包**（`~/data/kb/*.json`）与场景配置，
   不碰 personal-kb 仓库本身——那个库正在整体重构。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from utils import get_logger

logger = get_logger("api.kb")


def register(app: FastAPI) -> None:
    @app.get("/api/kb/search")
    async def kb_search_api(q: str = "", top: int = 5, scenario: str = ""):
        """查个人知识库。给前端「我刚才说的这事，我以前是怎么处理的」用。

        返回的是**已经过滤过**的结果：secret 条目在构建索引包时就没进来，
        这里再按可见性挡一道；`scenario` 再叠一层领域/听众闸门，双保险。
        """
        query = (q or "").strip()
        if not query:
            return JSONResponse({"ok": False, "error": "缺少查询词 q"}, status_code=400)
        try:
            from kb_bridge import hits

            rows = await asyncio.to_thread(hits, None, "", query,
                                           min(max(top, 1), 20), scenario)
        except Exception as exc:
            logger.warning("知识检索失败：%s", exc)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
        return {"ok": True, "query": query, "count": len(rows), "hits": rows}

    @app.get("/api/kb/stats")
    async def kb_stats_api():
        """索引包概况：用什么语料、多少条、什么时候建的。看「知识库没同步」时用。"""
        try:
            from kb_bridge import legal_pack_path, load, load_legal, pack_path

            out = {"ok": True, "packs": []}
            for name, pack, path in (
                ("kb", load(), pack_path()),
                ("legal", load_legal(), legal_pack_path()),
            ):
                if not pack:
                    # ⚠️ B-5 同族第 5 处：原来是 str(path) 绝对路径。
                    # 这个接口纯诊断用（前端没调），只留文件名就够定位「用的哪个包」。
                    out["packs"].append({"corpus": name, "loaded": False,
                                         "path": Path(path).name})
                    continue
                out["packs"].append({
                    "corpus": name,
                    "loaded": True,
                    "path": Path(path).name,
                    "label": pack.get("corpus"),
                    "generated_at": pack.get("generated_at"),
                    "entries": pack.get("count"),
                    "chunks": len(pack.get("chunks") or []),
                })
            out["loaded"] = any(p["loaded"] for p in out["packs"])
            return out
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.get("/api/kb/scenarios")
    async def kb_scenarios_api():
        """前端下拉框要的场景清单 + 当前默认档。文件改了下一次请求就生效。"""
        try:
            from kb_bridge import (
                DEFAULT_SCENARIOS,
                load_scenarios,
                resolve_scenario,
                scenario_names,
            )

            data = load_scenarios()
            cur = resolve_scenario("")
            return {
                "ok": True,
                "default": data.get("default") or DEFAULT_SCENARIOS["default"],
                "current": cur["name"],
                "scenarios": scenario_names(),
            }
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
