#!/usr/bin/env python3
"""CosyVoice2 语音合成服务。

为什么单独一个进程
------------------
CosyVoice2 钉 `transformers==4.51.3` / `omegaconf==2.3.0`，而转写服务用的是
`transformers 5.x` / `numpy 2.5` —— 塞进同一个 venv 会互相踩。
所以合成跑在 `~/tts/venv`（复制自转写环境再降级），只监听 127.0.0.1，
由转写服务反代出去。好处还多一条：重启合成不会打断正在进行的转写。

接口
----
    GET  /health            模型状态、显存、音色清单
    GET  /voices            [{name, text, seconds, mtime}]
    POST /tts               {"text": "...", "voice": "zhangsan", "speed": 1.0}
                            → audio/wav（24k 单声道）
    POST /tts/stream        同上，但返回 chunked PCM16（网页低延迟播放用）
    POST /reload            重新扫描 voices/ 目录（新增音色后调用）

音色目录结构（两套布局都认）
--------------------------
    ~/tts/voices/<name>/prompt.wav                    参考音（16k 单声道，3~30s）
    ~/tts/voices/<name>/prompt.txt                    参考音的逐字稿（必须和音频对得上）
    ~/tts/voices/<name>/meta.json                     来源信息（可选）

    ~/tts/voices/<speaker>/<profile>/prompt.wav      同一人按场景分音色
    ~/tts/voices/<speaker>/<profile>/prompt.txt      例：张三/开会、张三/老家方言
    ~/tts/voices/<speaker>/<profile>/meta.json

voice id：profile=「默认」用裸名（`张三`），其余用 `张三@开会`。
一个 id 只能绑一个 prompt —— 想换场景必须换 id，同一 id 重复注册是覆盖不是混合。

启动时用 `add_zero_shot_spk` 把每个音色的 token/feat/embedding 预计算进内存，
之后每句合成只走 LLM，不再重复跑 speech_tokenizer 那个 ONNX。
"""

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

LOG_PREFIX = "[tts]"
DEFAULT_PROFILE = "默认"

# 电脑默认音色（不是克隆）：用 Edge 的在线神经语音。
# 目的不是「好听」，而是**一听就知道是机器**——真实通话里给对方一个提醒。
# voice id 统一加 sys: 前缀，和克隆音色分得开，前端按前缀/kind 分组。
# (显示名, 说明, Edge 音色名)
SYSTEM_VOICES: dict[str, tuple[str, str, str]] = {
    "sys:xiaoxiao": ("晓晓", "女声 · 自然", "zh-CN-XiaoxiaoNeural"),
    "sys:yunxi": ("云希", "男声 · 年轻", "zh-CN-YunxiNeural"),
    "sys:yunjian": ("云健", "男声 · 沉稳", "zh-CN-YunjianNeural"),
    "sys:xiaoyi": ("晓伊", "女声 · 活泼", "zh-CN-XiaoyiNeural"),
    "sys:xiaobei": ("晓北", "女声 · 东北话", "zh-CN-liaoning-XiaobeiNeural"),
}


def _hdr(text: str) -> str:
    """HTTP 头只能放 latin-1。音色名可能是中文（张三@开会），必须转义，
    否则 Response 构造时会抛 UnicodeEncodeError（整个请求 500）。"""
    from urllib.parse import quote

    return quote(str(text or ""), safe="")


def log(msg: str, *args) -> None:
    text = msg % args if args else msg
    print(f"{time.strftime('%H:%M:%S')} {LOG_PREFIX} {text}", flush=True)


# --------------------------------------------------------------------------- #
# 合成引擎
# --------------------------------------------------------------------------- #
class TtsEngine:
    def __init__(self, model_dir: Path, voices_dir: Path, cache_dir: Path,
                 repo_dir: Path, fp16: bool = False):
        self.model_dir = model_dir
        self.voices_dir = voices_dir
        self.cache_dir = cache_dir
        self.repo_dir = repo_dir
        self.fp16 = fp16
        self.model = None
        self.sample_rate = 24000
        self.load_seconds = 0.0
        self._load_lock = threading.Lock()
        self._gen_lock = threading.Lock()
        self.voices: dict[str, dict] = {}

    # ---------------- 加载 ----------------
    @property
    def loaded(self) -> bool:
        return self.model is not None

    def ensure_loaded(self) -> None:
        if self.model is not None:
            return
        with self._load_lock:
            if self.model is not None:
                return
            for extra in (self.repo_dir, self.repo_dir / "third_party" / "Matcha-TTS"):
                if str(extra) not in sys.path:
                    sys.path.insert(0, str(extra))
            t0 = time.time()
            log("加载 CosyVoice2：%s（fp16=%s）", self.model_dir, self.fp16)
            from cosyvoice.cli.cosyvoice import CosyVoice2

            self.model = CosyVoice2(
                str(self.model_dir),
                load_jit=False,
                load_trt=False,
                load_vllm=False,
                fp16=self.fp16,
            )
            self.sample_rate = int(self.model.sample_rate)
            self.load_seconds = time.time() - t0
            log("模型就绪，%.1fs，采样率 %d", self.load_seconds, self.sample_rate)
        self.reload_voices()

    # ---------------- 音色 ----------------
    # 音色目录两套布局都认：
    #   voices/<name>/prompt.wav                    ← 一个人一个音色（老布局）
    #   voices/<speaker>/<profile>/prompt.wav       ← 同一人按场景分多个音色（新布局）
    # 新布局的 voice id：profile=「默认」时用裸名（兼容老调用），否则用 `名字@场景`。
    def _iter_voice_dirs(self):
        """按目录布局找出所有音色，两层优先、一层兜底，voice id 去重。"""
        if not self.voices_dir.is_dir():
            return []
        entries: list[tuple[str, Path, str, str]] = []
        seen: set[str] = set()
        for top in sorted(self.voices_dir.iterdir()):
            if not top.is_dir():
                continue
            for sub in sorted(top.iterdir()):
                if not sub.is_dir():
                    continue
                if (sub / "prompt.wav").is_file() and (sub / "prompt.txt").is_file():
                    vid = top.name if sub.name == DEFAULT_PROFILE else f"{top.name}@{sub.name}"
                    if vid not in seen:
                        entries.append((vid, sub, top.name, sub.name))
                        seen.add(vid)
            # 老布局：voices/<name>/prompt.wav（同 id 已被两层目录占用时跳过）
            if (top / "prompt.wav").is_file() and (top / "prompt.txt").is_file():
                if top.name not in seen:
                    entries.append((top.name, top, top.name, DEFAULT_PROFILE))
                    seen.add(top.name)
        return entries

    def scan_voices(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for vid, folder, speaker, profile in self._iter_voice_dirs():
            wav = folder / "prompt.wav"
            txt = folder / "prompt.txt"
            text = txt.read_text(encoding="utf-8").strip()
            if not text:
                log("跳过音色 %s：prompt.txt 为空", vid)
                continue
            meta = {}
            meta_path = folder / "meta.json"
            if meta_path.is_file():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            found[vid] = {
                "name": vid,
                "speaker": meta.get("speaker") or speaker,
                "profile": meta.get("profile") or profile,
                "text": text,
                "path": str(wav),
                "seconds": float(meta.get("seconds") or 0.0),
                "source": meta.get("source", ""),
                # 归属人。空 = 老音色还没打标，由 deploy/tag_voice_owner.py 补齐；
                # 隔离判定放在 server.py（那里才知道当前是谁在请求），这里只如实上报。
                "owner": str(meta.get("owner") or ""),
            }
        return found

    @staticmethod
    def public_voice(info: dict) -> dict:
        """对外只给前端用得上的字段：绝对路径不出这个进程。

        `path` 是 add_zero_shot_spk 的入参，`source` 是截取参考音的那场录音——
        两者都是 `<家目录>/...` 绝对路径，既泄露目录结构，`source` 还连带
        暴露「这条音色是从哪场会议剪的」。统一裁成文件名。
        """
        out = {k: v for k, v in info.items() if k != "path"}
        src = str(out.get("source") or "")
        if src:
            out["source"] = os.path.basename(src.replace("\\", "/"))
        return out

    def reload_voices(self) -> list[str]:
        self.ensure_loaded()
        from cosyvoice.utils.file_utils import load_wav

        voices = self.scan_voices()
        ids: list[str] = []
        for name, info in voices.items():
            try:
                # 注意：add_zero_shot_spk 内部自己调 load_wav，必须传**路径**；
                # 传已加载的张量会走到 torchaudio.load(tensor) 报 "Invalid file"
                seconds = 0.0
                try:
                    seconds = load_wav(info["path"], 16000).shape[1] / 16000
                except Exception:
                    pass
                with self._gen_lock:
                    self.model.add_zero_shot_spk(info["text"], info["path"], name)
                info["seconds"] = round(seconds, 2)
                self.voices[name] = info
                ids.append(name)
                log("音色就绪：%s（参考音 %.1fs）", name, seconds)
            except Exception as exc:  # 单个音色坏了不该拖垮整个服务
                log("音色 %s 注册失败：%s", name, exc)
        # 目录里已删除的音色，内存里也要清掉
        for stale in [k for k in self.voices if k not in voices]:
            self.voices.pop(stale, None)
            try:
                self.model.frontend.spk2info.pop(stale, None)
            except Exception:
                pass
        return ids

    # ---------------- 电脑默认音色 ----------------
    # 和克隆音色放在同一个 voice 命名空间里（sys: 前缀），这样前端只需要
    # 一个下拉、一条 /tts 通路，缓存、流式、反代全都不用改。
    def system_voices(self) -> list[dict]:
        out = []
        for vid, (name, tag, edge_name) in SYSTEM_VOICES.items():
            out.append({
                "name": vid,
                "speaker": name,
                "profile": tag,
                "kind": "system",
                "engine": "edge",
                "edge_voice": edge_name,
                "text": "",
                "seconds": 0.0,
                "source": "系统音色（Edge 神经语音）",
            })
        return out

    def _synth_system(self, text: str, voice: str, speed: float):
        """系统音色：Edge 神经语音 → ffmpeg 解成 24k 单声道 float32。

        句子短（<=30 字一级），整段收完再解码，几秒就回来，换来的是实现简单、
        不会因为半路断流产生半截音频。Edge 那条连接要外网，不通就明确报错，
        不要静默回退成我的声音——那样反而把「这是 AI」这个提醒抹掉了。
        """
        import asyncio
        import subprocess

        try:
            import edge_tts
        except ImportError:
            raise RuntimeError("系统音色没装：在 ~/tts/venv 里 pip install edge-tts")

        edge_name = SYSTEM_VOICES[voice][2]
        rate = f"{int(round((float(speed) - 1.0) * 100)):+d}%"

        async def _fetch() -> bytes:
            buf = bytearray()
            com = edge_tts.Communicate(text, edge_name, rate=rate)
            async for chunk in com.stream():
                if chunk.get("type") == "audio":
                    buf.extend(chunk["data"])
            return bytes(buf)

        # 这里可能被 async handler 直接调用（事件循环已经在跑），
        # 所以不能让 asyncio.run 跑在当前线程里，扔到独立线程自己建 loop。
        box: dict[str, object] = {}

        def _worker() -> None:
            try:
                box["mp3"] = asyncio.run(_fetch())
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc

        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        th.join()
        if "err" in box:
            raise RuntimeError(f"系统音色合成失败（需要外网）：{box['err']}")
        mp3 = box.get("mp3") or b""
        if not mp3:
            raise RuntimeError("系统音色没有返回音频")

        ff = os.environ.get("FFMPEG_BIN", "ffmpeg")
        proc = subprocess.run(
            [ff, "-hide_banner", "-nostdin", "-v", "error",
             "-i", "pipe:0", "-f", "s16le", "-acodec", "pcm_s16le",
             "-ac", "1", "-ar", str(self.sample_rate), "pipe:1"],
            input=mp3, capture_output=True,
        )
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError("系统音色解码失败：" + proc.stderr.decode("utf-8", "ignore")[-200:])
        pcm = np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
        step = max(1, int(self.sample_rate * 0.2))
        for i in range(0, len(pcm), step):
            yield pcm[i:i + step]

    # ---------------- 合成 ----------------
    def _cache_path(self, text: str, voice: str, speed: float) -> Path:
        key = hashlib.sha1(f"{voice}|{speed:.2f}|{text}".encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.wav"

    def synth_chunks(self, text: str, voice: str, speed: float = 1.0):
        """逐块产出 float32 单声道数组。流式与非流式共用。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("文本为空")
        # 系统音色不走 GPU，也不该为了它把 CosyVoice 加载进来
        if voice in SYSTEM_VOICES:
            yield from self._synth_system(text, voice, speed)
            return
        self.ensure_loaded()
        if voice not in self.voices:
            raise ValueError(f"没有这个音色：{voice}（可用：{', '.join(self.voices) or '无'}）")

        with self._gen_lock:  # GPU 串行，避免多人同时点把显存打爆
            for out in self.model.inference_zero_shot(
                text, "", "", zero_shot_spk_id=voice, stream=True, speed=float(speed)
            ):
                speech = out["tts_speech"]
                arr = speech.detach().float().cpu().numpy().reshape(-1)
                yield arr


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #
def build_app(engine: TtsEngine):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse, Response, StreamingResponse

    app = FastAPI(title="CosyVoice2 TTS", docs_url=None, redoc_url=None)

    # 注意：本模块开了 `from __future__ import annotations`，FastAPI 解析不到
    # 函数内定义的 Pydantic 模型（会退化成查询参数），所以这里手动解析 JSON。
    async def _body(request: Request) -> dict:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise HTTPException(400, "请求体必须是 JSON 对象")
        return data

    def _pick_voice(name: str) -> str:
        if name and (name in engine.voices or name in SYSTEM_VOICES):
            return name
        if engine.voices:
            return next(iter(engine.voices))
        if SYSTEM_VOICES:
            return next(iter(SYSTEM_VOICES))
        raise HTTPException(503, "还没有任何音色，先用 voice_register.py 录一条")

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "model_loaded": engine.loaded,
            "model_dir": str(engine.model_dir),
            "sample_rate": engine.sample_rate,
            "load_seconds": round(engine.load_seconds, 1),
            "voices": list(engine.voices),
            "system_voices": list(SYSTEM_VOICES),
        }

    @app.get("/voices")
    async def voices():
        # kind=clone 是我的声纹，kind=system 是电脑默认音色：前端据此分组，
        # 也据此决定助理标签叫「我的声音」还是「电脑声音」。
        # 归属过滤不在这里做——本服务不知道谁在请求，由 server.py 按租户筛。
        out = [{**engine.public_voice(info), "kind": "clone"} for info in engine.voices.values()]
        out.extend(engine.system_voices())
        return {"ok": True, "voices": out}

    @app.post("/reload")
    async def reload():
        try:
            ids = engine.reload_voices()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return {"ok": True, "voices": ids}

    @app.post("/tts")
    async def tts(request: Request):
        data = await _body(request)
        voice = _pick_voice(str(data.get("voice") or ""))
        speed = float(data.get("speed") or 1.0)
        use_cache = bool(data.get("use_cache", True))
        text = str(data.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "文本为空")
        if len(text) > 2000:
            raise HTTPException(400, "单次最多 2000 字，长文本请分段")

        out = engine._cache_path(text, voice, speed)
        t0 = time.time()
        if use_cache and out.is_file():
            log("命中缓存 %.1fs 文本：%s", time.time() - t0, text[:24])
            return Response(content=out.read_bytes(), media_type="audio/wav",
                            headers={"X-Cache": "hit", "X-Voice": _hdr(voice)})

        import soundfile as sf

        parts = list(engine.synth_chunks(text, voice, speed))
        if not parts:
            raise HTTPException(500, "合成没有产出")
        audio = np.concatenate(parts)
        engine.cache_dir.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), audio, engine.sample_rate, subtype="PCM_16")
        audio_sec = len(audio) / engine.sample_rate
        cost = time.time() - t0
        log("合成 %.1fs 音频用 %.2fs（RTF %.2f）：%s", audio_sec, cost,
            cost / max(audio_sec, 0.01), text[:24])
        return Response(content=out.read_bytes(), media_type="audio/wav",
                        headers={"X-Cache": "miss", "X-Voice": _hdr(voice),
                                 "X-Audio-Seconds": f"{audio_sec:.2f}",
                                 "X-Rtf": f"{cost / max(audio_sec, 0.01):.2f}"})

    @app.post("/tts/stream")
    async def tts_stream(request: Request):
        """chunked 裸 PCM16 little-endian，采样率见 X-Sample-Rate。"""
        data = await _body(request)
        voice = _pick_voice(str(data.get("voice") or ""))
        speed = float(data.get("speed") or 1.0)
        text = str(data.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "文本为空")

        def gen():
            for arr in engine.synth_chunks(text, voice, speed):
                yield np.clip(arr, -1.0, 1.0).astype("<i2").tobytes()

        return StreamingResponse(
            gen(),
            media_type="audio/L16",
            headers={"X-Sample-Rate": str(engine.sample_rate), "X-Channels": "1",
                     "X-Voice": _hdr(voice)},
        )

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CosyVoice2 合成服务")
    ap.add_argument("--model-dir", default=os.environ.get(
        "TTS_MODEL_DIR", str(Path.home() / "data/models/CosyVoice2-0.5B")))
    ap.add_argument("--repo-dir", default=os.environ.get(
        "TTS_REPO_DIR", str(Path.home() / "tts/CosyVoice")))
    ap.add_argument("--voices", default=os.environ.get(
        "TTS_VOICES_DIR", str(Path.home() / "tts/voices")))
    ap.add_argument("--cache", default=os.environ.get(
        "TTS_CACHE_DIR", str(Path.home() / "data/work/tts_cache")))
    ap.add_argument("--host", default="127.0.0.1")
    # O-14：端口从 Settings 读，与 TTS_PORT 环境变量一致。
    # 延迟导入：TTS 服务跑在独立 venv 里，万一没装 dotenv 也不能在启动阶段炸掉。
    try:
        from config import SETTINGS as _S
        _port_default = _S.tts_port
    except Exception:  # noqa: BLE001 - 独立 venv 缺依赖时退回原默认值
        _port_default = 9880
    ap.add_argument("--port", type=int, default=_port_default)
    ap.add_argument("--fp16", action="store_true", help="半精度（显存省一半，音质略降）")
    ap.add_argument("--warm", action="store_true", help="启动即加载模型（默认懒加载）")
    args = ap.parse_args(argv)

    engine = TtsEngine(
        model_dir=Path(args.model_dir).expanduser(),
        voices_dir=Path(args.voices).expanduser(),
        cache_dir=Path(args.cache).expanduser(),
        repo_dir=Path(args.repo_dir).expanduser(),
        fp16=args.fp16,
    )
    engine.cache_dir.mkdir(parents=True, exist_ok=True)

    if args.warm:
        threading.Thread(target=engine.ensure_loaded, daemon=True).start()

    import uvicorn

    log("监听 http://%s:%d", args.host, args.port)
    uvicorn.run(build_app(engine), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
