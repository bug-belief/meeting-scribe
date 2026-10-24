"""实时转写 WebSocket：`/ws/live`（O-11 C-5，最后一批）。

这是全仓最大的单体（297 行），也是唯一一条 WS 路由。协议要点：

  浏览器 → 服务端
    二进制  原始 PCM16 小块（双声道时左右交织：左=麦克风、右=系统声音）
    json    {type: start|meta|tts_begin|tts_end|stop|ping}

  服务端 → 浏览器
    ready / source / partial / line / drop / finalizing / done / snapshot / error

**为什么这段最难拆**：它是唯一持有可变状态的路由（session、buffer、finalized），
且 `process_chunk` 是嵌套函数，靠 `nonlocal` 读写 `finalized`。
搬到 `register()` 里后嵌套层级不变，行为等价。
"""

from __future__ import annotations

import asyncio
import json
import uuid

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import live_engine
from api.deps import cfg_of, tenant
from config import resolve_owner
from live_engine import (
    BYTES_PER_SAMPLE,
    CHUNK_SAMPLES,
    MAX_LINE_CHARS,
    SAMPLE_RATE,
    SILENCE_FINALIZE_SEC,
    LiveSession,
    chunk_rms,
    finalize,
    log_live_asr,
    looks_like_noise,
)
from utils import get_logger

logger = get_logger("api.live")

# ⚠️ 这两条标签在 `transcribe.py` 里还有**一份同名副本**（apply_assistant_timeline 用）。
# 两边必须一致，否则会出现「实时字幕标『助理（我的声音）』、归档稿标另一个名字」。
# tests/test_routes.py 里有一条断言锁住它们相等——不要只改一处。
ASSISTANT_LABEL = "助理（我的声音）"
ASSISTANT_LABEL_SYS = "助理（电脑声音）"  # 选了 sys: 开头的电脑音色时用这个


def assistant_label(voice: str) -> str:
    """按音色决定标签：克隆音色是我的声音，sys: 开头的是电脑声音。"""
    return ASSISTANT_LABEL_SYS if str(voice or "").startswith("sys:") else ASSISTANT_LABEL


def warm_weather(settings) -> None:
    """开局预热天气缓存。失败就当没发生（外网不通时不该影响录音）。"""
    try:
        from tools import weather

        weather(settings.assistant_city)
        logger.info("天气缓存预热完成（%s）", settings.assistant_city or "按出口 IP 定位")
    except Exception as exc:      # noqa: BLE001 - 预热是纯优化，任何异常都吞掉
        logger.debug("天气预热跳过：%s", exc)


def register(app: FastAPI) -> None:
    @app.websocket("/ws/live")
    async def live(ws: WebSocket):
        cfg = cfg_of(ws)
        supplied = ws.query_params.get("k") or ws.cookies.get("wb_k") or ""
        owner = resolve_owner(supplied, cfg)
        if owner is None:
            await ws.close(code=4401)
            return
        ws.state.owner = owner
        await ws.accept()
        session = LiveSession(tenant(ws))
        live_engine.SESSIONS[session.id] = session
        logger.info("会话开始：%s（%s，owner=%s）", session.id, session.title, owner)

        # 先把模型拉起来再放行，否则用户点开始后的前 20 秒音频全在排队。
        #
        # 🔴 失败必须**说出来**。实时转写走的是 `live_engine` 自己 `from funasr import
        #    AutoModel`（流式模型），**不经过 transcribe.py** —— 所以只装了基础依赖
        #    （requirements.txt）时，这里会抛 ModuleNotFoundError。
        #    原来没有 try：WS 直接异常关闭，前端 onclose 又只在 recording 时提示，
        #    用户看到的是「点了开始，界面没反应」—— 完全无从排查。
        try:
            await asyncio.to_thread(live_engine.ENGINE.ensure_loaded)
        except ModuleNotFoundError as exc:
            missing = getattr(exc, "name", "") or str(exc)
            logger.warning("实时字幕不可用（缺 %s）：会话 %s 拒绝", missing, session.id)
            await ws.send_json({
                "type": "fatal",
                "reason": "asr_missing",
                "error": (
                    f"实时字幕需要本地模型，当前环境缺 `{missing}`。"
                    "改用文件转写：命令行 `python transcribe.py --input <音频>`，"
                    "或把文件放进 INBOX 目录由 watcher 自动处理。"
                    "要开实时字幕请装 requirements-asr-local.txt。"
                ),
            })
            await ws.close(code=1011)
            live_engine.SESSIONS.pop(session.id, None)
            return
        except Exception as exc:            # 模型下载失败 / 显存不足 / 配置错
            logger.exception("实时模型加载失败：%s", exc)
            await ws.send_json({
                "type": "fatal",
                "reason": "asr_load_failed",
                "error": (f"实时模型加载失败：{exc}。"
                          "可以先用文件转写（transcribe.py --input <音频>），或看服务日志。"),
            })
            await ws.close(code=1011)
            live_engine.SESSIONS.pop(session.id, None)
            return
        # 天气第一次查要 3~4 秒（外部接口 + 模型工具调用）。开场就预热一次，
        # 用户真问的时候直接命中 10 分钟缓存——这是「等待很久」里最容易省掉的一段。
        asyncio.create_task(asyncio.to_thread(warm_weather, session.settings))
        # 检索预取配额按场重置（每场最多 N 次，见 websearch._PREFETCH_MAX_PER_SESSION）
        try:
            from websearch import reset_prefetch

            reset_prefetch()
        except Exception:
            pass
        await ws.send_json({
            "type": "ready",
            "session": session.id,
            "sample_rate": SAMPLE_RATE,
            "chunk_ms": int(CHUNK_SAMPLES / SAMPLE_RATE * 1000),
            "sources": ["mic", "system", "dual"],
            # 「谁在说」的标签由后端定（DUAL_SELF_NAME / DUAL_PEER_NAME 可配）。
            # 前端要靠它区分「我说的」和「对方说的」来决定谁能唤醒助理，
            # 硬编码成「我 / 对方」的话，一改配置就判错了。
            "self_name": session.settings.dual_self_name,
            "peer_name": session.settings.dual_peer_name,
        })

        buffer = bytearray()
        finalized = False

        async def process_chunk(chunk_bytes: bytes) -> None:
            nonlocal finalized
            raw = np.frombuffer(chunk_bytes, dtype=np.int16)
            rms_l = rms_r = 0.0
            if session.channels > 1:
                frames = raw.reshape(-1, session.channels).astype(np.float32) / 32768.0
                chunk = frames.mean(axis=1)      # 送 ASR 的一律是单声道
                rms_l = chunk_rms(frames[:, 0])  # 左=麦克风（你）
                rms_r = chunk_rms(frames[:, 1])  # 右=系统声音（对方）
            else:
                chunk = raw.astype(np.float32) / 32768.0
            session.write(chunk_bytes)
            rms = chunk_rms(chunk)
            session.note_rms(rms)
            if session.channels > 1:
                # 累计右声道的存在证据：判断「对方那一路到底有没有录到」。
                # 传的是**帧数**（∦ 交织样本数），才能和 PEER_PROBE_MIN_SAMPLES 直接比。
                session.note_peer_level(rms_r, len(raw) // session.channels)

            text = await asyncio.to_thread(live_engine.ENGINE.feed, chunk, session.cache, False)
            if text:
                # 停顿后重新开口时，这句的起点要跟着往后挪。
                # 以前只在「上一句定稿」时更新 line_start_ms，遇到长静音就会把
                # 后一句的时间戳记成很早（还会让回声区间对不上）。
                if not session.pending:
                    onset = (session.duration
                             - session.silence_samples / SAMPLE_RATE
                             - len(chunk) / SAMPLE_RATE)
                    session.line_start_ms = int(max(0.0, onset) * 1000)
                session.pending += text
                session.silence_samples = 0
                # 这句的能量记账：谁那边响，这句就算谁说的
                session.line_rms_l += rms_l
                session.line_rms_r += rms_r
            else:
                session.silence_samples += len(chunk)

            silent_ms = session.silence_samples / SAMPLE_RATE * 1000
            too_long = len(session.pending) >= MAX_LINE_CHARS
            if session.pending and (silent_ms >= SILENCE_FINALIZE_SEC * 1000 or too_long):
                line = {
                    "start": round(session.line_start_ms / 1000, 2),
                    "end": round(session.duration, 2),
                    "text": session.pending.strip(),
                }
                floor = session.rms_floor()
                peak = session.line_peak
                if peak < floor or looks_like_noise(line["text"]):
                    # 底噪触发的幻听：整句丢掉。既不进面板，也不进助理的上下文——
                    # 之前这些「的的的的」会被当成用户提问，助理只好回「听着有点乱」。
                    logger.info("丢弃噪声行 %.1f→%.1fs 峰值 %.4f（闸门 %.4f）「%s」",
                                line["start"], line["end"], peak, floor, line["text"][:40])
                    session.reset_line()
                    await ws.send_json({"type": "drop", "reason": "noise"})
                    return

                right_dom = session.line_rms_r > session.line_rms_l
                echo = False
                # 双路分轨：按这句累计的左右能量判归属，单声道不发 who。
                #
                # ⚠️ 但「右声道有没有声音」得先确认。电话外放时对方的声音从房间
                # 进麦克风，右声道全程近乎静音（实测约 0.4s / 全程 170s），此时
                # `line_rms_l >= line_rms_r` 恒成立，会把对方的话也标成「我」。
                # 判不准就不标：前端 addLine 见 who 为空即不出标签，
                # isSelf() 对空 who 按「可能是我」放行（有意降级），所以唤醒不受影响。
                if session.channels > 1 and (session.line_rms_l + session.line_rms_r) > 0:
                    alive = session.peer_channel_alive()
                    if alive is not False:          # True=确实有声音，None=还没看够
                        line["who"] = (session.settings.dual_self_name
                                       if session.line_rms_l >= session.line_rms_r
                                       else session.settings.dual_peer_name)
                    elif not session.peer_who_warned:
                        session.peer_who_warned = True
                        logger.warning(
                            "右声道全程静音（峰值 %.4f），不按声道标说话人："
                            "对方的声音是从麦克风进来的，标了会把他说成「%s」",
                            session.peer_max_rms, session.settings.dual_self_name)
                if session.events:
                    echo = session.is_echo(line["start"], line["end"], right_dom)
                session.lines.append(line)
                session.reset_line()
                if echo:
                    # 助理自己的声音被声卡录回来了：ASR 听到的是「克隆音」，
                    # 文本必然是错的（「11到17度」→「一七度」），而且会重复。
                    # 真正的台词已经在 tts_begin 时按原文记进 lines，这里直接丢掉。
                    session.lines.pop()
                    return
                # 实时定稿文本：**必须记**（2026-10-21 教训）。
                # 语音唤醒是在前端按文本匹配的，而实时流式 ASR 和最终稿会有差异
                # （实测最终稿写「语音助理」，实时出的很可能是「语音助厉」）。
                # 以前只在"丢弃噪声行"时打文本，正常的行一律不记 ——
                # 于是"听到了却没唤醒"这类问题只能靠猜，完全没法定位。
                # 含唤醒相关词的打 info（量小、要能看见），其余打 debug 免得刷屏。
                _t = line.get("text") or ""
                if any(w in _t for w in ("语音", "助理", "助手")):
                    logger.info("定稿行（疑似唤醒词）%.1f→%.1fs who=%s「%s」",
                                line.get("start", 0), line.get("end", 0),
                                line.get("who") or "-", _t[:60])
                else:
                    logger.debug("定稿行 %.1f→%.1fs who=%s「%s」",
                                 line.get("start", 0), line.get("end", 0),
                                 line.get("who") or "-", _t[:60])
                await ws.send_json({"type": "line", **line})
                # 联网检索预取（2026-10-22）：听到像问句的话就**后台先查**。
                #
                # 为什么在服务端而不是前端：一条定稿行到这里时，用户可能还要
                # 过几秒才喊唤醒词/点「让它说」；这段空档正好够搜索跑完。
                # 等真要回复时命中缓存 = 0ms 感知不到。
                #
                # 安全点：prefetch() 内部会判断
                #   · 是否启用（默认关，WEB_SEARCH_PREFETCH=1 才开）
                #   · should_search()（客套/天气/含身份证合同号的都不查）
                #   · 本场配额（默认 12 次，防止额度被打爆）
                # 而且它**立即返回**，不阻塞这条连接；异常在后台任务里吞掉。
                try:
                    from websearch import prefetch

                    prefetch(_t, session.id)
                except Exception:
                    pass      # 预取是优化，任何问题都不能影响转写主链路
                return

            # 助理正在说话时，右声道收到的必然是它自己；不要把这段乱码当字幕推给前端
            if session.channels > 1 and rms_r > rms_l and session.tts_window():
                return

            quiet = bool(session.pending) and (
                session.line_peak < session.rms_floor()
                or looks_like_noise(session.pending)
            )
            await ws.send_json({
                "type": "partial",
                # 底噪上编出来的字别往面板上推：面板上那行「的的的的」就是这么来的
                "text": "" if quiet else session.pending,
                "quiet": quiet,
                "silent_ms": int(silent_ms),
                "rms": round(rms, 4),
                "rms_l": round(rms_l, 4),
                "rms_r": round(rms_r, 4),
            })

        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break

                data = message.get("bytes")
                if data:
                    buffer.extend(data)
                    block_bytes = CHUNK_SAMPLES * BYTES_PER_SAMPLE * session.channels
                    while len(buffer) >= block_bytes:
                        block = bytes(buffer[:block_bytes])
                        del buffer[:block_bytes]
                        await process_chunk(block)
                    continue

                text = message.get("text")
                if not text:
                    continue
                try:
                    cmd = json.loads(text)
                except ValueError:
                    continue

                if cmd.get("type") == "start":
                    # 浏览器在推第一块音频之前声明采集方式；决定声道数与切块大小
                    src = str(cmd.get("source") or "mic").strip().lower()
                    if src not in {"mic", "system", "dual"}:
                        src = "mic"
                    try:
                        ch = int(cmd.get("channels") or (2 if src == "dual" else 1))
                    except (TypeError, ValueError):
                        ch = 2 if src == "dual" else 1
                    session.source = src
                    session.channels = 2 if (src == "dual" or ch >= 2) else 1
                    title = str(cmd.get("title") or "").strip()
                    if title:
                        session.title = title
                    logger.info("采集来源：%s（%d 声道）｜%s",
                                session.source, session.channels, session.title)
                    await ws.send_json({
                        "type": "source", "source": session.source,
                        "channels": session.channels, "title": session.title,
                    })
                    continue

                if cmd.get("type") == "meta":
                    # 前端在开始录音后报一次设备信息；只用来给音色语料分设备，不参与识别
                    mic = str(cmd.get("mic") or "").strip()[:80]
                    if mic:
                        session.mic = mic
                        logger.info("会话 %s 采集设备：%s", session.id, mic)
                    continue

                if cmd.get("type") == "wake_probe":
                    # 前端把「这次为什么没唤醒」的判断结果报上来（2026-10-22 新增）。
                    # 为什么需要这条通道：唤醒是**纯前端**按文本匹配的，
                    # 服务端只能看到"说了什么"，看不到"为什么没触发"。
                    # 上次排查「喊了但没反应」时，前端只能 console.warn，
                    # 用户不开 F12 就永远拿不到信息，只能靠猜 —— 白烧了半天。
                    # 现在决策结果直接进服务端日志，一条命令就能看到卡在哪一道。
                    logger.info(
                        "唤醒探测 text=%r who=%r mode=%s byMe=%s called=%s "
                        "inWindow=%s checked=%s decision=%s blocked=%s",
                        str(cmd.get("text") or "")[:40],
                        cmd.get("who"),
                        cmd.get("mode"),
                        cmd.get("byMe"),
                        cmd.get("called"),
                        cmd.get("inWindow"),
                        cmd.get("checked"),
                        cmd.get("decision"),
                        cmd.get("blocked") or "-",
                    )
                    continue

                if cmd.get("type") == "tts_begin":
                    # 页面开播时上报：at = 已推入录音的音频秒数（前端自己算）
                    try:
                        at = float(cmd.get("at"))
                    except (TypeError, ValueError):
                        at = session.duration
                    text = str(cmd.get("text") or "")
                    eid = str(cmd.get("id") or uuid.uuid4().hex[:8])
                    voice = str(cmd.get("voice") or "")
                    label = assistant_label(voice)
                    session.events.append({
                        "id": eid,
                        "kind": "tts",
                        "text": text,
                        "voice": voice,
                        "label": label,
                        "start": round(at, 3),
                        "end": 0.0,
                        "spoken_chars": 0,
                        "state": "speaking",
                    })
                    if text:
                        # 助理的台词以**播报日志原文**进入对话上下文。
                        # 如果不加，模型下一轮看到的「助理说过的话」是声卡录回来的
                        # ASR 错字，越聊越跑偏。
                        session.lines.append({
                            "start": round(at, 2), "end": round(at, 2),
                            "text": text, "who": label,
                            "assistant": True, "_eid": eid,
                        })
                    continue

                if cmd.get("type") == "tts_end":
                    # 说完 / 被打断都走这里；state: done | partial | cancelled
                    eid = str(cmd.get("id") or "")
                    ev = next((e for e in session.events if e["id"] == eid), None)
                    if ev is None:
                        _v = str(cmd.get("voice") or "")
                        ev = {"id": eid, "kind": "tts", "text": str(cmd.get("text") or ""),
                              "voice": _v, "label": assistant_label(_v),
                              "start": session.duration,
                              "end": 0.0, "spoken_chars": 0, "state": "done"}
                        session.events.append(ev)
                    try:
                        ev["end"] = round(float(cmd.get("at")), 3)
                    except (TypeError, ValueError):
                        ev["end"] = round(session.duration, 3)
                    try:
                        ev["spoken_chars"] = max(0, int(cmd.get("spoken_chars") or 0))
                    except (TypeError, ValueError):
                        ev["spoken_chars"] = 0
                    ev["state"] = str(cmd.get("state") or "done")
                    # 被打断：上下文里只保留真正说出去的那一段
                    line = next((x for x in session.lines if x.get("_eid") == eid), None)
                    if line is not None:
                        spoken = int(ev["spoken_chars"] or 0)
                        if ev["state"] in {"partial", "cancelled"} and spoken > 0:
                            line["text"] = line["text"][:spoken]
                        elif ev["state"] == "cancelled":
                            session.lines.remove(line)
                        line.pop("_eid", None)
                        line["end"] = round(session.duration, 2)
                    logger.info("助理播报：%s %.2f→%.2f（%s，%d 字）", ev["id"],
                                ev["start"], ev["end"], ev["state"], ev["spoken_chars"])
                    continue

                if cmd.get("type") == "stop":
                    finalized = True
                    block_bytes = CHUNK_SAMPLES * BYTES_PER_SAMPLE * session.channels
                    # 把缓冲里不足一块的尾巴也送进去，别丢最后半秒
                    if buffer:
                        block = bytes(buffer) + b"\x00" * (block_bytes - len(buffer))
                        buffer.clear()
                        await process_chunk(block)
                    if session.pending.strip():
                        tail_text = session.pending.strip()
                        # 收尾时也过一遍闸门：最后半句常常只是一点底噪，不值得留
                        if not (session.line_peak < session.rms_floor()
                                or looks_like_noise(tail_text)):
                            session.lines.append({
                                "start": round(session.line_start_ms / 1000, 2),
                                "end": round(session.duration, 2),
                                "text": tail_text,
                            })
                        session.pending = ""
                    await ws.send_json({"type": "finalizing", "session": session.id,
                                        "duration": round(session.duration, 1)})
                    try:
                        result = await asyncio.to_thread(finalize, session)
                    except Exception as exc:
                        logger.exception("收尾失败")
                        result = {"type": "done", "ok": False,
                                  "error": f"{type(exc).__name__}: {exc}"}
                    await ws.send_json(result)
                    break

                if cmd.get("type") == "ping":
                    await ws.send_json(session.snapshot())

        except WebSocketDisconnect:
            logger.info("连接断开：%s", session.id)
        except Exception as exc:
            logger.exception("会话异常：%s", session.id)
            try:
                await ws.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            except Exception:
                pass
        finally:
            if not finalized:
                # 没点停止就断开：保留音频，后台补跑一遍，别丢录音
                try:
                    session.close()
                    logger.warning("会话未正常结束，音频已保留：%s", session.dir)
                except Exception:
                    pass
            log_live_asr(session)
            live_engine.SESSIONS.pop(session.id, None)
            try:
                await ws.close()
            except Exception:
                pass
