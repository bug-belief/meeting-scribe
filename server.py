"""实时录音转写 Web 服务（阶段 2A）。

链路
----
浏览器麦克风 → AudioWorklet 取 PCM16/16k → WebSocket → paraformer 流式识别
    → 增量字幕（每 600 ms 一次） → 静音断句
停止后：整段音频落盘 → 走一遍**完整的离线管线**（说话人分离 + 声纹库 + 标点 + DeepSeek 纪要）
    → 返回带人名的转写稿与纪要。

为什么实时和离线跑两套
----------------------
流式模型只做"立刻看到字"：它不带标点、不做说话人分离。真正交付的稿子必须用离线管线
（paraformer 整段 + cam++ 声纹 + ct-punc），准确率和可读性都高一个档。所以实时是体验，
离线是结果，两者分工，不要在浏览器里硬做聚类。

启动
----
    .venv/bin/python server.py --host 0.0.0.0 --port 8848
    # 或者 deploy/meeting-web.service（systemd user 单元）
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import live_engine

# 流式引擎 / 会话 / 归档收尾都在 live_engine.py（O-11 C-1 搬出）。
# 单例不导入名字，只导入模块——因为本文件要**赋值**它们（global 跨模块无效）。
from api import (
    assistant as api_assistant,
    health as api_health,
    kb as api_kb,
    live as api_live,
    meetings as api_meetings,
    pages as api_pages,
    speakers as api_speakers,
    voice as api_voice,
)
from config import (
    SETTINGS,
    Settings,
    ensure_dirs,
    for_owner,
    load_settings,
    owner_names,
    read_version,
    resolve_owner,
)
from live_engine import (
    StreamEngine,
)
from utils import get_logger, setup_logging

logger = get_logger("server")

WEB_DIR = Path(__file__).resolve().parent / "web"

# 启动时读一次。版本号是「这份代码是什么」，运行期不会变，不必每请求读文件。
# 真源是仓库根的 VERSION，见 config.read_version 的说明。
_APP_VERSION = read_version()
# 收藏标记文件名。用独立的空文件而不是往 meta.json 里塞字段，
# 是因为 meta.json 会被 refresh_archive 之类的重建流程整份覆写。
FAVORITE_NAME = ".favorite"

# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
# 音色归属表的短缓存：{"at": 取表时刻, "map": {voice_id: owner}}
# ⚠️ 这个缓存原本紧跟在 live_engine 那几个单例后面，C-1 搬家时被连带删掉过一次
#    （ruff 的 F821 抓出来的）——它属于 server.py，不属于 live_engine。
_VOICE_MAP: dict = {"at": 0.0, "map": {}}

def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or SETTINGS
    # 只建租户目录；旧的单租户路径不再创建，避免和 tenants/<owner> 混淆
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    for _name in owner_names(cfg):
        ensure_dirs(for_owner(_name, cfg))
    # 进程级单例：赋值要写在 live_engine 模块上（`global` 跨模块无效）
    live_engine.ENGINE = StreamEngine(cfg)

    app = FastAPI(title="会议录音实时转写", docs_url=None, redoc_url=None)
    # deps.py 的唯一 cfg 来源（别再让各模块读模块级 SETTINGS，见 api/deps.py 抬头）
    app.state.cfg = cfg
    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.middleware("http")
    async def auth_gate(request, call_next):
        """公网暴露时用口令挡一下：链接带 ?k=xxx，验证通过后写 cookie。

        口令同时决定「你是谁」：每个租户一个口令，归档 / 工作目录 / 声纹库
        全部按租户隔离。`TENANT_TOKENS="me:abc,friend:def"` 即可开多租户。
        """
        if request.url.path == "/api/health":
            request.state.owner = cfg.default_owner
            return await call_next(request)
        # PWA 的清单和图标必须免口令：安卓「添加到主屏幕」和 APK 壳都会在
        # 没有 cookie 的情况下抓这几样，卡在 401 就装不上。
        # 这里**只放行图标和 manifest**，不放 /static/app.js 之类的前端逻辑。
        if request.url.path == "/manifest.webmanifest" or request.url.path.startswith(
            ("/static/icon-", "/static/apple-touch-icon", "/static/favicon-")
        ):
            request.state.owner = cfg.default_owner
            return await call_next(request)
        supplied = request.query_params.get("k") or request.cookies.get("wb_k") or ""
        owner = resolve_owner(supplied, cfg)
        if owner is None:
            # 浏览器打开时给一个能输口令的页面（PWA / APK 首次激活都走这里）；
            # fetch/脚本调用仍然返回纯文本，方便它们自己判断状态码。
            wants_html = "text/html" in (request.headers.get("accept") or "")
            page = WEB_DIR / "activate.html"
            if wants_html and page.is_file():
                return FileResponse(page, status_code=401,
                                    media_type="text/html; charset=utf-8")
            return PlainTextResponse(
                "需要访问口令：请在链接后加 ?k=<口令>\n如果忘了口令，看服务器上的 WEB_TOKEN。",
                status_code=401,
            )
        request.state.owner = owner
        response = await call_next(request)
        if request.query_params.get("k"):
            # 只有走 HTTPS 时才加 secure。内网直连是 http://<你的内网IP>:8848，
            # 加了 secure 浏览器就不回传 cookie，等于每次都得重新带 ?k=。
            # Funnel 在 Tailscale 边缘做 TLS 终结，转发进来是 http，
            # 所以要看 X-Forwarded-Proto，不能只看 request.url.scheme。
            proto = (request.headers.get("x-forwarded-proto")
                     or request.url.scheme or "").lower()
            response.set_cookie("wb_k", supplied, max_age=60 * 60 * 24 * 90,
                                httponly=True, samesite="lax", secure=(proto == "https"))
        return response

    def _tenant(obj) -> Settings:
        """按当前请求的 owner 派生目录配置。"""
        return for_owner(getattr(obj.state, "owner", cfg.default_owner), cfg)

    # ---------------- 音色归属 ----------------
    # tts_server 是单进程、不知道谁在请求，所以隔离判定只能放在这里。
    # 缓存 10 秒：合成是一句一次，每句都多打一次本机往返不划算。
    def _voice_owners(tenant: Settings) -> dict[str, str]:
        """voice id → owner。拿不到就沿用上次结果，返回空表表示「不知道归属」。"""
        now = time.time()
        if now - _VOICE_MAP["at"] < 10.0:
            return _VOICE_MAP["map"]
        try:
            import urllib.request

            with urllib.request.urlopen(f"{tenant.tts_base_url}/voices", timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.debug("音色归属表取不到：%s", exc)
            return _VOICE_MAP["map"]
        table: dict[str, str] = {}
        for v in data.get("voices") or []:
            if v.get("kind") == "system":
                continue
            table[str(v.get("name") or "")] = str(v.get("owner") or "")
        _VOICE_MAP.update({"at": now, "map": table})
        return table

    def _voice_visible(voice: dict, owner: str, table: dict) -> bool:
        """系统音色人人可用；克隆音色只归本人。

        owner 为空 = 老音色还没打标，放行。理由：升级瞬间现有音色全消失比
        这个风险更糟，而且 deploy/tag_voice_owner.py 会立刻补上。
        """
        if voice.get("kind") == "system":
            return True
        mine = table.get(str(voice.get("name") or ""), "")
        return (not mine) or mine == owner

    def _voice_allowed(voice_id: str, owner: str, table: dict) -> bool:
        """合成前的闸门。表里没这个名字就当音色不存在，交给 /tts 自己报错。"""
        if not voice_id or voice_id.startswith("sys:"):
            return True
        if not table:  # 归属表没拿到，不拦——真不可用 /tts 会报错
            return True
        mine = table.get(voice_id)
        if mine is None:
            return True
        return (not mine) or mine == owner

    def _log_llm(tenant: Settings, result: dict, ref: str, scene: str) -> None:
        """把一次 LLM 调用的 usage 换算成钱记一笔。记账失败绝不影响回复。"""
        try:
            from costs import llm_cost, log as log_cost

            usage = result.get("usage") or {}
            if not usage.get("prompt_tokens") and not usage.get("completion_tokens"):
                return
            billed = llm_cost(
                str(result.get("model") or tenant.llm_model),
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("cache_hit_tokens") if usage.get("cache_hit_known") else None,
            )
            log_cost("llm", ref=ref, scene=scene, owner=tenant.owner, **billed)
        except Exception as exc:  # pragma: no cover - 记账永远是可选的
            logger.debug("LLM 成本记账跳过：%s", exc)

    api_pages.register(app)
    api_health.register(app)
    api_meetings.register(app)
    api_speakers.register(app)
    api_assistant.register(app)
    api_kb.register(app)
    api_voice.register(app)
    api_live.register(app)
    return app

app = create_app()

def _env_file_path() -> Path:
    """当前生效的 .env 路径（与 config.py 的解析规则一致，含 MEETING_ENV_FILE 覆盖）。

    自检要把自动生成的口令写回**真正被读的那份** .env，写错地方等于没写。
    """
    import os

    raw = (os.getenv("MEETING_ENV_FILE") or "").strip()
    return Path(raw) if raw else (Path(__file__).resolve().parent / ".env")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="实时录音转写 Web 服务")
    parser.add_argument("--host", default=SETTINGS.web_host)
    parser.add_argument("--port", type=int, default=SETTINGS.web_port)
    parser.add_argument("--warmup", action="store_true", help="启动时预加载流式模型")
    args = parser.parse_args(argv)

    settings = load_settings()
    setup_logging(settings.logs_dir, settings.log_level, "server")

    # 首次启动自检（bootstrap.py）：数据目录不能落在仓库里、没口令不许对外监听。
    #
    # 为什么放在这里而不是模块顶部：`app` 是模块级建好的，这里改 settings 只能
    # 影响**运行时读取**它的地方（auth_gate、for_owner、各 API），不能改路由注册。
    # 而自检要改的正是 `web_token` 这种运行时值，所以位置正确。
    #
    # bootstrap 在 try **外面**导入：放在里面的话，导入失败时 except 分支
    # 引用 `bootstrap.BootstrapError` 会抛 NameError，把「导入失败」变成「崩在异常处理里」。
    import bootstrap

    try:
        report = bootstrap.run(settings, args.host, env_file=_env_file_path())
        if report.summary():
            print("\n" + "=" * 60)
            print(report.summary())
            print("=" * 60 + "\n")
            sys.stdout.flush()
    except bootstrap.BootstrapError as exc:
        print("\n启动中止：\n" + str(exc) + "\n", file=sys.stderr)
        sys.stderr.flush()
        return 2
    except Exception:
        # 自检本身出错不能拦住服务：它不是业务逻辑
        logger.exception("启动自检异常（已跳过，服务继续）")

    if args.warmup:
        def _warmup():
            try:
                live_engine.ENGINE.ensure_loaded()
            except Exception:
                # 预加载失败不拦启动（文件上传照样能用）。
                # ⚠️ 但也**不会**在点开始时「再试一次」——那样用户要白等 20 秒。
                #    api/live.py 会在建 WS 时再调一次 ensure_loaded，失败就发 fatal
                #    并明确告知缺什么，见 tests/test_asr_missing_hint.py。
                logger.exception("流式模型预加载失败（实时字幕将不可用，上传文件不受影响）")

        threading.Thread(target=_warmup, daemon=True).start()

    # 缺 uvicorn 时给可操作的提示，而不是裸 traceback。
    # 前面一堆自检都跑完了才崩在最后一行，前面的「一切正常」会让人以为是别的毛病。
    try:
        import uvicorn
    except ModuleNotFoundError:
        logger.error(
            "缺少 uvicorn，服务无法监听端口。装它：pip install uvicorn"
            "（已在 requirements.txt 里；若用 venv，确认已 activate）")
        return 2

    logger.info("服务启动：http://%s:%d（工作目录 %s）", args.host, args.port, settings.base_dir)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", ws_ping_interval=25,
                ws_ping_timeout=60)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
