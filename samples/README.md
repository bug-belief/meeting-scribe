# samples/ —— 演示音频

这里**不放音频文件**（只有脚本和一个合成 WAV）。原因在下面「为什么不在仓库里放人声」。

## 快速开始

```bash
python3 samples/make_samples.py            # 生成 demo-dual.wav + demo-mono.wav（离线、零依赖）
python3 samples/make_samples.py --speech    # 额外生成人声样例（需联网 + pip install edge-tts）
```

## 两个产物，用途别混

| 文件 | 是什么 | 能验证什么 | **不能**验证什么 |
|---|---|---|---|
| `demo-dual.wav` | 13 秒合成音，16kHz/16bit/**立体声**。左路 220/247/262 Hz 正弦 ×3 段，右路 330/294/350 Hz 三角波 ×3 段，各 1.5 秒交替 | 双声道分轨、声纹聚类、整条管线不崩 | 🔴 **转写准不准** —— 它压根不是人声，转不出字 |
| `demo-mono.wav` | 4 秒单声道 440 Hz 纯音 | 单路上传链路 | 同上 |
| `demo-speech-a.wav` / `-b.wav` | 两个不同音色念的中文（`--speech` 现生成） | **识别质量 + 说话人是否分对** | — |

⚠️ `demo-speech-*.mp3/wav` 是云端 TTS 合成的，**授权不明确，不要提交**（`.gitignore` 已挡）。

## 为什么不在仓库里放人声

真正能验证转写的样例必须是**人声**，而人声两条路都不适合入库：

- **真人录音** → 隐私问题，还可能带上真实对话内容
- **云端 TTS** → 微软官方口径是「**付费档**才明确允许再分发合成音频」，
  `edge-tts` 走的是免费接口，没有明确授权。开源仓库不该赌这个。

所以：脚本入库，音频现生成。

## 为什么合成音的长度是 12–13 秒

不是随手定的，两个数字都有硬约束：

**① 每路各 3 段 × 1.5 秒 = 4.5 秒**
`live_engine.PEER_PROBE_MIN_SAMPLES` 要求观察满 **8 秒**才肯判定「右声道是否真的存在」。
太短的话 `peer_channel_alive()` 一直返回 `None`，右路会被退化成 Speaker 2/3 ——
看起来像「分轨失败」，其实是素材不够长。

**② 段间必须有 0.5 秒静音**
分轨判定依据「这一段有没有能量」，两路一直响就分不出轮次，测不出切换。

## 怎么用它验证分轨

不用起服务、不用模型，直接量能量：

```bash
python3 - <<'PY'
import wave, numpy as np, diarize
with wave.open("samples/demo-dual.wav", "rb") as fh:
    a = np.frombuffer(fh.readframes(fh.getnframes()), dtype="<i2")
a = a.reshape(-1, 2).astype(np.float32) / 32768.0
print(diarize._channel_activity(a, 16000))
PY
```

期望输出：

```
{'left': 4.5, 'right': 4.5}
```

左 4.5 = 3 段×1.5s，右 4.5 同。**两边相等是设计使然**（轮流说话，各占一半），
不是 bug。如果看到 `right: 0.0`，说明 WAV 被转成了单声道（看 `getnchannels()` 是否为 2）。

## 走完整上传管线

```bash
curl -F file=@samples/demo-dual.wav 'http://127.0.0.1:8848/api/upload?k=<你的 WEB_TOKEN>'
```

- 只装了基础依赖（`requirements.txt`）→ 会出**单人稿**，`notes` 里写明「未装 funasr」
- 装了 `requirements-asr-local.txt` → 会尝试声纹聚类，分成两簇
- 但**两种都转不出文字**（素材不是语音），这是预期结果

## 亲自动手做真人样例

想测识别质量，用自己的录音最实在：

1. 手机录音机录 10 秒以上，两人各说一两句
2. 拷到本机，上传或丢进 `INBOX_DIR`
3. `samples/` 目录下的真实录音**不会**被 git 跟踪（整目录已忽略）

## 音色

见 [`demo-voice.md`](demo-voice.md) —— 仓里不提供任何音色文件，说明怎么克隆自己的。
