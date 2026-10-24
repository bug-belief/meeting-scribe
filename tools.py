"""语音分身用的轻量工具：目前只有天气。

原则：工具只负责「取数」，不负责「怎么说」。拿到结果塞进提示词，由模型组织语言。
任何工具都必须有超时 + 失败返回空串——外部服务挂了不能把对话卡住，
更不能让模型拿一个假数据去编。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request

# 命中这些词才去查天气，避免每句话都发一次网络请求
WEATHER_RE = re.compile(r"天气|气温|多少度|下雨|下雪|冷不冷|热不热|穿什么|带伞")

_UA = "curl/8.0"

# wttr.in 的 lang=zh 实际没翻译（lang_zh 返回的还是英文），自己映射。
# 顺序有讲究：先长后短，否则 "Partly cloudy" 会被 "cloudy" 抢走。
_ZH_DESC = (
    ("thunder", "雷阵雨"),
    ("heavy rain", "大雨"),
    ("moderate rain", "中雨"),
    ("light rain", "小雨"),
    ("patchy rain", "零星小雨"),
    ("rain", "有雨"),
    ("heavy snow", "大雪"),
    ("light snow", "小雪"),
    ("snow", "有雪"),
    ("sleet", "雨夹雪"),
    ("freezing", "冻雨"),
    ("fog", "雾"),
    ("mist", "薄雾"),
    ("haze", "霾"),
    ("partly", "晴间多云"),
    ("overcast", "阴"),
    ("cloudy", "多云"),
    ("sunny", "晴"),
    ("clear", "晴"),
)


def _zh_desc(desc: str) -> str:
    s = str(desc or "").strip().lower()
    for key, val in _ZH_DESC:
        if key in s:
            return val
    return str(desc or "").strip() or "未知"


def _get(url: str, timeout: float = 6.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def weather(city: str = "", days: int = 3, timeout: float = 6.0) -> str:
    """查未来几天天气。city 为空时按服务器出口 IP 定位。

    返回一句话，形如：
        今天（2026-10-19）多云，14~22℃，降雨概率 10%；明天…
    失败返回空串（调用方当作「没有实时信息」处理）。
    """
    q = urllib.parse.quote(city.strip()) if city.strip() else ""
    url = f"https://wttr.in/{q}?format=j1&lang=zh"
    try:
        data = json.loads(_get(url, timeout))
    except Exception:
        return ""

    labels = ("今天", "明天", "后天", "第四天", "第五天")
    out = []
    for i, day in enumerate((data.get("weather") or [])[:max(1, days)]):
        hourly = day.get("hourly") or []
        desc = ""
        # 取中午那一档，比日出/深夜更能代表「白天什么样」
        if hourly:
            mid = hourly[len(hourly) // 2]
            zh = mid.get("lang_zh") or []
            if zh:
                desc = str(zh[0].get("value") or "")
            if not desc:
                desc = str((mid.get("weatherDesc") or [{}])[0].get("value") or "")
        desc = _zh_desc(desc)
        rain = 0
        for h in hourly:
            try:
                rain = max(rain, int(h.get("chanceofrain") or 0))
            except (TypeError, ValueError):
                pass
        lo = day.get("mintempC") or "?"
        hi = day.get("maxtempC") or "?"
        out.append(
            f"{labels[i] if i < len(labels) else day.get('date', '')}"
            f"（{day.get('date', '')}）{desc}，{lo}~{hi}℃，降雨概率 {rain}%"
        )
    return "；".join(x for x in out if x.strip())


_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 600.0   # 10 分钟。两次追问之间数据必须一致，否则模型会"改数字"


def maybe_weather(text: str, city: str = "") -> str:
    """文本里提到天气才查，其余一律返回空串（零成本）。"""
    if not text or not WEATHER_RE.search(text):
        return ""
    key = city.strip() or "__ip__"
    hit = _CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    out = weather(city)
    if out:
        _CACHE[key] = (now, out)
    return out
