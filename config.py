"""集中配置：所有可调项都从 .env / 环境变量读取，代码里不写死路径与密钥。

优先级：真实环境变量 > .env 文件 > 本文件默认值
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # 允许在未装依赖时做纯配置检查
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent
_TRUE = {"1", "true", "yes", "y", "on"}


def _load_env() -> None:
    if load_dotenv is None:
        return
    env_file = Path(os.getenv("MEETING_ENV_FILE") or (BASE_DIR / ".env"))
    if env_file.is_file():
        load_dotenv(env_file, override=False)


_load_env()


def _str(key: str, default: str = "") -> str:
    value = os.getenv(key)
    return default if value is None or value.strip() == "" else value.strip()


def _int(key: str, default: int) -> int:
    try:
        return int(_str(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_str(key, str(default)))
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    value = _str(key, "")
    return default if value == "" else value.lower() in _TRUE


def _path(key: str, default: str) -> Path:
    raw = Path(_str(key, default)).expanduser()
    return raw if raw.is_absolute() else (BASE_DIR / raw).resolve()


def _path_opt(key: str) -> Path | None:
    raw = _str(key, "")
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else (BASE_DIR / p).resolve()


def _token_map(key: str) -> dict[str, str]:
    """`TENANT_TOKENS="me:abc,zhangsan:def"` → {token: owner}。

    逗号分隔，每项 `owner:token`。留空则只认单口令 WEB_TOKEN。
    """
    out: dict[str, str] = {}
    for item in _str(key, "").split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        owner, token = item.split(":", 1)
        owner, token = owner.strip(), token.strip()
        if owner and token:
            out[token] = owner
    return out


def _csv(key: str, default: str) -> tuple[str, ...]:
    return tuple(x.strip().lower() for x in _str(key, default).split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # ---------- 路径 ----------
    base_dir: Path
    inbox_dir: Path
    processed_dir: Path
    work_dir: Path
    logs_dir: Path
    failed_dir: Path

    # ---------- 输入 ----------
    audio_exts: tuple[str, ...]
    min_file_size_kb: int
    min_duration_sec: float
    stability_seconds: float
    stability_timeout: float
    poll_interval_sec: float

    # ---------- 外部程序 ----------
    ffmpeg_bin: str
    ffprobe_bin: str
    keep_wav: bool
    voice_corpus_auto: bool
    assistant_city: str
    assistant_hometown: str

    # ---------- ASR 后端选择 ----------
    # funasr / whisperx 走本地模型；cloud 走云端 API（只替换识别这一步，
    # VAD 与声纹仍用本地 —— 见 cloud_asr.py 顶部说明）
    asr_backend: str
    asr_device: str
    asr_language: str

    # ---------- 云端 ASR（asr_backend=cloud 时生效） ----------
    asr_cloud_api_key: str
    asr_cloud_base_url: str
    asr_cloud_model: str
    asr_cloud_timeout: float
    # 时间戳单位 auto/s/ms。auto 时按整批判（有小数→秒；最大值>3600→毫秒；否则秒）
    asr_cloud_time_unit: str
    speaker_label_prefix: str
    asr_split_minutes: int
    funasr_model: str
    funasr_vad: str
    funasr_punc: str
    funasr_spk: str
    funasr_batch_size_s: int
    funasr_hub: str
    # 说话人分离（自研，见 diarize.py）
    diar_enabled: bool
    diar_window_ms: int
    diar_hop_ms: int
    diar_min_sim: float
    diar_max_speakers: int
    diar_min_turn_ms: int
    # 停顿超过该毫秒数即切段（电话录音里两人可能被判成同一人，靠停顿保证可读性）
    diar_max_gap_ms: int
    # 静音/噪声窗过滤：窗能量低于「中位数 × 该比例」直接丢弃（避免把静音聚成一个人）
    diar_min_rms_ratio: float
    # 质心相似度高于该值的两簇视为同一人，合并（对抗窄带电话音导致的过分割）
    diar_merge_sim: float
    # 空闲多久后卸载模型、把显存还给同机其他程序（0 = 不卸载，常驻最快）
    asr_idle_unload_min: int

    # ---------- 声纹库（把 Speaker N 变成真实人名，见 speakers.py） ----------
    speaker_store: Path
    speaker_enabled: bool
    # close（耳机/近场）| far（免提/会议室）| phone（电话窄带）| unknown
    speaker_channel: str
    speaker_max_templates: int

    whisperx_model: str
    whisperx_batch_size: int
    whisperx_diarize: bool
    whisperx_min_speakers: int
    whisperx_max_speakers: int
    hf_token: str

    # ---------- 云端 LLM ----------
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_thinking: str
    llm_allow_non_flash: bool
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout: float
    llm_retries: int
    llm_proxy: str

    # ---------- 报告分块 ----------
    summary_direct_chars: int
    summary_chunk_chars: int

    # ---------- Web 服务 ----------
    # 访问口令：公网暴露时必填，页面链接带 ?k=xxx 即可，之后靠 cookie
    web_token: str
    # 监听地址与端口（O-14：原来写死在 server.py 的 argparse 默认值与 .service 里）
    # 默认值与收敛前完全一致，不改变任何现有部署行为
    web_host: str
    web_port: int
    # 双路分轨录音里两个声道的名字：左=麦克风（你），右=系统声音（对方）
    dual_self_name: str
    dual_peer_name: str
    # CosyVoice2 合成服务地址（独立进程、独立 venv，只监听本机）
    tts_base_url: str
    # 合成服务监听端口；只用于 tts_server.py 的 argparse 默认值
    tts_port: int

    # ---------- 多租户 ----------
    # 每人的归档/工作目录/声纹库互不可见；模型、日志共用。
    tenant_root: Path
    owner: str
    default_owner: str
    # {token: owner}，来自 TENANT_TOKENS；为空时退化为单口令 WEB_TOKEN
    tenant_token_map: dict[str, str]

    # ---------- 运行控制 ----------
    max_attempts: int
    retry_cooldown_sec: int
    log_level: str


def load_settings() -> Settings:
    processed = _path("PROCESSED_DIR", "processed")
    return Settings(
        base_dir=BASE_DIR,
        inbox_dir=_path("INBOX_DIR", "audios_inbox"),
        processed_dir=processed,
        work_dir=_path("WORK_DIR", "work"),
        logs_dir=_path("LOGS_DIR", "logs"),
        failed_dir=processed / "_failed",
        audio_exts=_csv("AUDIO_EXTS", ".m4a,.mp3,.wav,.aac,.flac,.ogg,.opus,.amr,.wma,.mp4"),
        min_file_size_kb=_int("MIN_FILE_SIZE_KB", 8),
        min_duration_sec=_float("MIN_DURATION_SEC", 3.0),
        stability_seconds=_float("STABILITY_SECONDS", 8.0),
        stability_timeout=_float("STABILITY_TIMEOUT", 600.0),
        poll_interval_sec=_float("POLL_INTERVAL_SEC", 60.0),
        ffmpeg_bin=_str("FFMPEG_BIN", "ffmpeg"),
        ffprobe_bin=_str("FFPROBE_BIN", "ffprobe"),
        keep_wav=_bool("KEEP_WAV", False),
        voice_corpus_auto=_bool("VOICE_CORPUS_AUTO", True),
        assistant_city=_str("ASSISTANT_CITY", ""),
        # 默认空：方言提示只有用户自己配了家乡才出现。
        # 绝不在代码里写死某地 —— 那是把一个人的位置发给每一条请求。
        assistant_hometown=_str("ASSISTANT_HOMETOWN", ""),
        asr_backend=_str("ASR_BACKEND", "funasr").lower(),
        asr_device=_str("ASR_DEVICE", "auto").lower(),
        asr_language=_str("ASR_LANGUAGE", "zh"),
        asr_cloud_api_key=_str("ASR_CLOUD_API_KEY", ""),
        asr_cloud_base_url=_str("ASR_CLOUD_BASE_URL", "https://api.openai.com/v1"),
        asr_cloud_model=_str("ASR_CLOUD_MODEL", "whisper-1"),
        asr_cloud_timeout=_float("ASR_CLOUD_TIMEOUT", 120.0),
        asr_cloud_time_unit=_str("ASR_CLOUD_TIME_UNIT", "auto").lower(),
        speaker_label_prefix=_str("SPEAKER_LABEL_PREFIX", "Speaker"),
        asr_split_minutes=_int("ASR_SPLIT_MINUTES", 0),
        funasr_model=_str("FUNASR_MODEL", "paraformer-zh"),
        funasr_vad=_str("FUNASR_VAD", "fsmn-vad"),
        funasr_punc=_str("FUNASR_PUNC", "ct-punc"),
        funasr_spk=_str("FUNASR_SPK", "cam++"),
        funasr_batch_size_s=_int("FUNASR_BATCH_SIZE_S", 300),
        funasr_hub=_str("FUNASR_HUB", "ms"),
        diar_enabled=_bool("DIAR_ENABLED", True),
        diar_window_ms=_int("DIAR_WINDOW_MS", 3000),
        diar_hop_ms=_int("DIAR_HOP_MS", 1500),
        diar_min_sim=_float("DIAR_MIN_SIM", 0.5),
        diar_max_speakers=_int("DIAR_MAX_SPEAKERS", 6),
        diar_min_turn_ms=_int("DIAR_MIN_TURN_MS", 400),
        diar_max_gap_ms=_int("DIAR_MAX_GAP_MS", 700),
        diar_min_rms_ratio=_float("DIAR_MIN_RMS_RATIO", 0.2),
        diar_merge_sim=_float("DIAR_MERGE_SIM", 0.55),
        asr_idle_unload_min=_int("ASR_IDLE_UNLOAD_MIN", 15),
        speaker_store=_path("SPEAKER_STORE", str(processed.parent / "speakers.json")),
        speaker_enabled=_bool("SPEAKER_ENABLED", True),
        speaker_channel=_str("SPEAKER_CHANNEL", "unknown").lower(),
        speaker_max_templates=_int("SPEAKER_MAX_TEMPLATES", 5),
        whisperx_model=_str("WHISPERX_MODEL", "large-v3"),
        whisperx_batch_size=_int("WHISPERX_BATCH_SIZE", 8),
        whisperx_diarize=_bool("WHISPERX_DIARIZE", True),
        whisperx_min_speakers=_int("WHISPERX_MIN_SPEAKERS", 0),
        whisperx_max_speakers=_int("WHISPERX_MAX_SPEAKERS", 0),
        hf_token=_str("HF_TOKEN", ""),
        llm_base_url=_str("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        llm_api_key=_str("LLM_API_KEY", ""),
        llm_model=_str("LLM_MODEL", "deepseek-flash"),
        llm_thinking=_str("LLM_THINKING", "off").lower(),
        llm_allow_non_flash=_bool("LLM_ALLOW_NON_FLASH", False),
        llm_temperature=_float("LLM_TEMPERATURE", 0.3),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 2048),
        llm_timeout=_float("LLM_TIMEOUT", 180.0),
        llm_retries=_int("LLM_RETRIES", 3),
        llm_proxy=_str("LLM_PROXY", ""),
        summary_direct_chars=_int("SUMMARY_DIRECT_CHARS", 12000),
        summary_chunk_chars=_int("SUMMARY_CHUNK_CHARS", 6000),
        web_token=_str("WEB_TOKEN", ""),
        web_host=_str("WEB_HOST", "0.0.0.0"),
        web_port=_int("WEB_PORT", 8848),
        dual_self_name=_str("DUAL_SELF_NAME", "我"),
        dual_peer_name=_str("DUAL_PEER_NAME", "对方"),
        tts_base_url=_str("TTS_BASE_URL", "http://127.0.0.1:9880").rstrip("/"),
        tts_port=_int("TTS_PORT", 9880),
        tenant_root=_path_opt("TENANT_ROOT") or (processed.parent / "tenants"),
        owner=_str("DEFAULT_OWNER", "me"),
        default_owner=_str("DEFAULT_OWNER", "me"),
        tenant_token_map=_token_map("TENANT_TOKENS"),
        max_attempts=_int("MAX_ATTEMPTS", 3),
        retry_cooldown_sec=_int("RETRY_COOLDOWN_SEC", 300),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )


SETTINGS = load_settings()


def owner_names(settings: Settings = SETTINGS) -> list[str]:
    """已知租户：默认 owner + TENANT_TOKENS 里出现过的。"""
    names = {settings.default_owner}
    names.update(settings.tenant_token_map.values())
    return sorted(n for n in names if n)


def for_owner(owner: str, base: Settings = SETTINGS) -> Settings:
    """派生某个租户的配置：只换目录与声纹库，模型 / LLM / 口令沿用全局。

    这样管线的其余代码完全不用改——它们本来就只认 `settings.processed_dir`。
    """
    from dataclasses import replace

    name = (owner or base.default_owner).strip() or base.default_owner
    root = base.tenant_root / name
    return replace(
        base,
        owner=name,
        inbox_dir=root / "inbox",
        processed_dir=root / "processed",
        work_dir=root / "work",
        failed_dir=root / "processed" / "_failed",
        speaker_store=root / "speakers.json",
    )


def cli_settings(owner: str | None = None) -> Settings:
    """命令行工具的配置：按 OWNER 环境变量（缺省 DEFAULT_OWNER）取租户配置。

    脚本的 `__main__` 一律用它，别直接 `load_settings()`——那会指回旧的单租户目录。
    """
    name = (owner or os.environ.get("OWNER") or SETTINGS.default_owner or "").strip()
    return for_owner(name or SETTINGS.default_owner)


def resolve_owner(token: str, base: Settings = SETTINGS) -> str | None:
    """口令 → 租户名。返回 None 表示拒绝。

    - 配了 TENANT_TOKENS：按表查；WEB_TOKEN 仍然可进默认租户
    - 只配了 WEB_TOKEN：只认它
    - 什么都没配：本地调试，直接给默认租户
    """
    token = (token or "").strip()
    if token and token in base.tenant_token_map:
        return base.tenant_token_map[token]
    if base.web_token and token == base.web_token:
        return base.default_owner
    if not base.web_token and not base.tenant_token_map:
        return base.default_owner
    return None


def ensure_dirs(settings: Settings = SETTINGS) -> None:
    for path in (
        settings.inbox_dir,
        settings.processed_dir,
        settings.work_dir,
        settings.logs_dir,
        settings.failed_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


# ---------- 版本号（唯一真源 = 仓库根的 VERSION 文件） ----------
#
# 为什么版本号不放 Settings、也不能被环境变量覆盖：
#   版本是「这份代码是什么」，不是「这台机器怎么配」。放进 Settings 就会被
#   .env 覆盖，两个工作区（服务器 ~/app 与本地）各写各的，又回到漂移老路。
#
# 谁读它：
#   - server.py：/api/health 的 version（免鉴权，只给 semver）
#                /api/me 的 build（需鉴权，含 git 提交短 sha 与是否脏）
#   - deploy/build_apk.sh：打 APK 时默认用它当 versionName
#   - tests/test_version.py：校验各处是否与它一致
#
# 读不到返回占位符而不是抛异常：探活接口不能因为少一个文件就 500。

VERSION_FALLBACK = "0.0.0-unknown"


def read_version(base_dir: "Path | None" = None) -> str:
    """读仓库根的 VERSION。参数只为测试注入，生产不传。"""
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent
    try:
        raw = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return VERSION_FALLBACK
    return raw or VERSION_FALLBACK


def read_build(base_dir: "Path | None" = None) -> dict:
    """版本 + git 提交信息。给鉴权后的接口用，不进 /api/health。

    这个函数就是为回答这个问题写的。git 不可用时字段留空，不影响服务。
    """
    root = Path(base_dir) if base_dir is not None else Path(__file__).resolve().parent
    info: dict = {"version": read_version(root), "commit": "", "dirty": False}
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            info["commit"] = out.stdout.strip()
        st = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        info["dirty"] = bool(st.stdout.strip()) if st.returncode == 0 else False
    except (OSError, subprocess.SubprocessError):
        pass
    return info


if __name__ == "__main__":
    from dataclasses import asdict

    for key, value in asdict(SETTINGS).items():
        if key in {"llm_api_key", "hf_token", "web_token", "tenant_token_map"}:
            # 口令类一律打码：这个命令的输出经常被贴进 issue 或聊天窗口
            value = "***" if value else "(未设置)"
        print(f"{key:24s} = {value}")
    print(f"{'version':24s} = {read_version()}")
