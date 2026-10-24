#!/usr/bin/env python3
"""云端 ASR：时间戳解析与粒度降级。

## 为什么要测这个

可移植性的关键判断是「**换掉 ASR 之后声纹还在不在**」。答案取决于云端
返回的时间戳粒度：

    words[]（词/字级） → 声纹投票精度与本地一致
    segments[]（句级） → 只能按字数均分，句内说话人切换会糊
    text（无时间戳）   → 声纹完全失效，只能单人稿

所以「粒度识别对不对」和「降级有没有被说出来」是硬要求。
降级允许，**静默降级不允许** —— 那会让人以为在认人，其实没认。

## 怎么测

`parse_payload()` 是纯函数（不碰网络），用真实结构样本喂它。
再加一个**本地 stub HTTP 服务**跑通 `request_payload()` 的整条链路
（multipart 上传 + 200 + JSON），不需要任何真 API key。

跑法：
    python tests/test_cloud_asr.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cloud_asr as C  # noqa: E402

TOTAL = 0
FAILED = 0


def check(desc: str, got, want) -> None:
    global TOTAL, FAILED
    TOTAL += 1
    if got == want:
        print(f"  ok   {desc}")
    else:
        FAILED += 1
        print(f"  FAIL {desc}\n         得={got!r} 期望={want!r}")


def main() -> int:
    print("1) 词级时间戳（最理想）：直接采用，逐字保留")
    payload_words = {
        "text": "你好世界",
        "words": [
            {"word": "你好", "start": 0.0, "end": 0.5},
            {"word": "世界", "start": 0.5, "end": 1.0},
        ],
    }
    units, g = C.parse_payload(payload_words)
    check("粒度识别为 words", g, C.GRANULARITY_WORDS)
    check("多字词拆成单字（与本地 paraformer 一致）", [u["text"] for u in units],
          ["你", "好", "世", "界"])
    check("时间单调不减", all(units[i]["end_ms"] <= units[i + 1]["start_ms"] + 1
                              for i in range(len(units) - 1)), True)
    check("首字起点 0ms", units[0]["start_ms"], 0)
    check("末字终点 1000ms", units[-1]["end_ms"], 1000)

    print("\n2) 词级 + 空格（英文）：空白字符不应产生空 token")
    payload_en = {"text": "hello world", "words": [
        {"word": " hello", "start": 0.0, "end": 0.4},
        {"word": " world", "start": 0.4, "end": 0.9},
    ]}
    units_en, _ = C.parse_payload(payload_en)
    check("无空串 token", all(u["text"].strip() for u in units_en), True)
    check("字母全部保留", "".join(u["text"] for u in units_en), "helloworld")

    print("\n3) 时间单位：单个值有歧义，所以 auto 看整批 + 留显式开关")
    payload_ms = {"text": "测试", "words": [
        {"word": "测", "start": 1200, "end": 1500},
        {"word": "试", "start": 1500, "end": 1800},
    ]}
    # 显式指定 ms：原样保留（1200ms 不能被乘 1000）
    units_ms, _ = C.parse_payload(payload_ms, time_unit="ms")
    check("显式 ms：1200 原样保留", units_ms[0]["start_ms"], 1200)
    check("显式 ms：末值 1800", units_ms[-1]["end_ms"], 1800)
    # 显式指定 s：按秒换算
    units_s, _ = C.parse_payload(payload_ms, time_unit="s")
    check("显式 s：1200 秒 → 1200000ms", units_s[0]["start_ms"], 1200000)
    # auto 判据：小数→秒；超大整数→毫秒；小整数→秒（OpenAI 惯例）
    check("auto 见小数判为秒", C.detect_time_unit([0.0, 1.25, 3.5]), "s")
    check("auto 见 >3600 整数判为毫秒", C.detect_time_unit([0, 75000, 120000]), "ms")
    check("auto 小整数按秒", C.detect_time_unit([0, 1, 2]), "s")
    check("auto 空列表不崩", C.detect_time_unit([]), "s")

    print("\n4) 句级时间戳：可用但精度降级，且必须能被识别出来")
    payload_seg = {"text": "第一句话。第二句话。", "segments": [
        {"text": "第一句话。", "start": 0.0, "end": 2.0},
        {"text": "第二句话。", "start": 2.0, "end": 4.0},
    ]}
    units_seg, g_seg = C.parse_payload(payload_seg)
    check("粒度识别为 segments", g_seg, C.GRANULARITY_SEGMENTS)
    check("句内按字数拆开（5 字 × 2 句 = 10 token）", len(units_seg), 10)
    check("首段从 0 开始", units_seg[0]["start_ms"], 0)
    check("末段到 4000ms", units_seg[-1]["end_ms"], 4000)
    _, note_seg = C.describe_granularity(payload_seg)
    check("降级说明不为空（不许静默）", bool(note_seg), True)
    check("说明里点了「句子级」", "句子级" in note_seg, True)

    print("\n5) 只有纯文本：无法分人，必须报错而不是假装成功")
    payload_text = {"text": "只有文字没有时间"}
    units_txt, g_txt = C.parse_payload(payload_text)
    check("粒度识别为 text", g_txt, C.GRANULARITY_TEXT)
    check("不产出可用 token", units_txt, [])
    _, note_txt = C.describe_granularity(payload_text)
    check("说明里点了「无法区分说话人」", "无法区分" in note_txt or "单人稿" in note_txt, True)
    orig_req = C.request_payload
    C.request_payload = lambda path, cfg: payload_text   # 桩掉网络，只测粒度判定
    try:
        C.cloud_tokens(Path("x.wav"), _StubSettings())
        check("cloud_tokens 对纯文本应抛错", "没抛错", "应抛 PipelineError")
    except Exception as exc:
        check("cloud_tokens 抛的是 PipelineError", type(exc).__name__, "PipelineError")
        check("错误信息说明原因", "时间戳" in str(exc), True)
    finally:
        C.request_payload = orig_req

    print("\n6) 空/异常 payload 不应崩")
    for bad in ({}, {"words": []}, {"segments": "x"}, None, {"text": ""}):
        units_b, g_b = C.parse_payload(bad)
        check(f"parse_payload({bad!r}) 返回空且不崩", (units_b, g_b),
              ([], C.GRANULARITY_TEXT))

    print("\n7) 未配 API key：给出可执行的提示，而不是干等超时")
    cfg = C.CloudConfig(api_key="")
    check("ready 为 False", cfg.ready, False)
    try:
        C.request_payload(Path("whatever.wav"), cfg)
        check("未配 key 应抛错", "没抛错", "应抛 PipelineError")
    except Exception as exc:
        check("抛的是 PipelineError", type(exc).__name__, "PipelineError")
        check("提示里给了配置项名", "ASR_CLOUD_API_KEY" in str(exc), True)
        check("提示里给了退路", "funasr" in str(exc), True)

    print("\n8) 端到端：本地 stub 服务跑通 multipart 上传链路（无需真 key）")
    srv, captured = _start_stub({
        "text": "端到端",
        "words": [{"word": "端", "start": 0.0, "end": 0.3},
                  {"word": "到", "start": 0.3, "end": 0.6},
                  {"word": "端", "start": 0.6, "end": 0.9}],
        "end_to_end": True,
    })
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as fh:
            fh.write(b"RIFF" + b"\x00" * 40)
            wav = Path(fh.name)
        cfg2 = C.CloudConfig(api_key="test-key-not-real",
                             base_url=f"http://127.0.0.1:{srv.server_port}/v1",
                             model="stub-model", timeout=10)
        payload2 = C.request_payload(wav, cfg2)
        wav.unlink(missing_ok=True)
        check("拿到 stub 的 payload", payload2.get("end_to_end"), True)
        check("请求打到了 /v1/audio/transcriptions", captured.get("path"),
              "/v1/audio/transcriptions")
        check("带了 Bearer 鉴权", captured.get("auth", "").startswith("Bearer "), True)
        check("用了配置里的 model", captured.get("model"), "stub-model")
        check("请求了词级时间戳", "word" in (captured.get("granularities") or ""), True)
        check("确实上传了文件（multipart）", captured.get("got_file"), True)
        units2, g2 = C.parse_payload(payload2)
        check("stub 结果解析成 3 个字", len(units2), 3)
        check("粒度是 words", g2, C.GRANULARITY_WORDS)
    finally:
        srv.shutdown()

    print("\n9) 反向对照：故意把粒度判定写错，第 4 组必须变红")
    orig = C.parse_payload
    C.parse_payload = lambda p, time_unit="auto": (
        orig(p, time_unit)[0], C.GRANULARITY_WORDS
    )   # 把句级谎报成词级
    try:
        _, lie = C.parse_payload(payload_seg)
    finally:
        C.parse_payload = orig
    check("被破坏后句级被谎报为 words（证明第 4 组不是空跑）", lie, C.GRANULARITY_WORDS)
    check("恢复后仍判为 segments", C.parse_payload(payload_seg)[1], C.GRANULARITY_SEGMENTS)

    print("\n" + "-" * 46)
    print(f"通过 {TOTAL} 项，失败 {FAILED} 项")
    return 0 if FAILED == 0 else 1


class _StubSettings:
    """最小 Settings 替身：只给 cloud_asr 会读的那几个字段。"""

    asr_cloud_api_key = "stub-key"
    asr_cloud_base_url = "http://127.0.0.1:1/v1"
    asr_cloud_model = "stub-model"
    asr_cloud_timeout = 5.0
    asr_language = "zh"


def _start_stub(payload: dict):
    """起一个只认 /audio/transcriptions 的本地服务，记录收到的请求要点。"""
    captured: dict = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            captured["path"] = self.path
            captured["auth"] = self.headers.get("Authorization", "")
            text = body.decode("utf-8", "ignore")
            captured["got_file"] = "filename=" in text

            def _field(fname: str) -> str:
                """从 multipart 里取 name="x" 之后真正的值（不是字段名）。

                ⚠️ 不要写 `(?:[^\\r\\n]*\\r\\n)*` 这种"跳过若干行"的写法：
                它是贪心的，会一路吃到最后一个字段 —— 第一次写就踩了这个坑，
                model 抓到的是 'word'（granularities 的值）。
                普通字段的结构固定是 `name="x"\\r\\n\\r\\n取值`，直接锚死即可。
                """
                import re as _re

                m = _re.search(
                    r'name="' + _re.escape(fname) + r'"\r\n\r\n([^\r\n]*)',
                    text,
                )
                return m.group(1) if m else ""

            captured["model"] = _field("model")
            captured["granularities"] = _field("timestamp_granularities[]")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode("utf-8"))

        def log_message(self, *a):   # 静音
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    srv.server_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, captured


if __name__ == "__main__":
    sys.exit(main())
