/* 音频采集与混音总线（O-12 D-3）。
 *
 * 这块管三件事，全部围绕 WebAudio 的节点图：
 *   ① 采集      麦克风 / 系统声音 / 双路分轨（acquireMic · acquireStreams）
 *   ② 混音总线  真麦 + 分身合成音 → 虚拟声卡，并单独给耳机一路监听
 *               （setupMix · applyMix · applySink · duck）
 *   ③ 电平显示  analyser → 顶部那排跳动的小柱子（drawLevel）
 *
 * 三处容易踩的坑，代码里都留着注释：
 *   · Windows 把默认录音设备设成 CABLE Output 时，浏览器会抓到我们自己的输出
 *     → 必须主动绕开虚拟声卡（isVirtualMic）
 *   · 分身说话的瞬间真麦要压低（duck），否则对方会听到自己的回声
 *   · 混音链必须在会话结束时彻底拆掉，否则下一次 new MediaElementSource 会抛
 *
 * 依赖约定：本模块只认 window.MeetState（共享状态）与两个跨模块回调
 * （toast 提示、stop 收尾）。回调通过 window.MeetApp 在**运行时**取，
 * 这样 app.js 与本文件不会有加载顺序上的循环依赖。
 */
(() => {
  const S = window.MeetState;
  const byId = (id) => document.getElementById(id);

  const bars = byId('bars');
  const mixEnableEl = byId('mixEnable');
  const mixMonitorEl = byId('mixMonitor');
  const mixSinkEl = byId('mixSink');
  const mixHintEl = byId('mixHint');
  const mixStateEl = byId('mixState');

  const DUCK_LEVEL = 0.12; // 分身说话时真麦压到 -18dB 左右

  /* ---- 跨模块回调：app.js 启动后挂到 window.MeetApp ---- */
  // 用「调用时才解析」而不是启动时取值：audio.js 先加载，那时 MeetApp 还不存在。
  function toast(msg, ms) { return window.MeetApp.toast(msg, ms); }
  function stop() { return window.MeetApp.stop(); }

  function isVirtualMic(label) {
    return /CABLE Output|VoiceMeeter Output|VoiceMeeter VAIO|VB-Audio|虚拟声卡/i.test(label || '');
  }

  async function acquireMic() {
    const base = {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    };
    let stream = await navigator.mediaDevices.getUserMedia({ audio: base });
    S.micName = '';
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      const inputs = devices.filter((d) => d.kind === 'audioinput');
      const settings = stream.getAudioTracks()[0].getSettings();
      const cur = inputs.find((d) => d.deviceId === settings.deviceId);
      if (cur && isVirtualMic(cur.label)) {
        const real = inputs.find((d) =>
          !isVirtualMic(d.label) &&
          d.deviceId !== 'default' && d.deviceId !== 'communications' && d.deviceId !== '');
        if (real) {
          stream.getTracks().forEach((t) => { try { t.stop(); } catch (e) {} });
          stream = await navigator.mediaDevices.getUserMedia({
            audio: { ...base, deviceId: { exact: real.deviceId } },
          });
          S.micName = real.label || '真实麦克风';
        } else {
          toast('系统默认麦克风是虚拟声卡，且没找到真实麦克风——录不到你的声音', 5000);
        }
      } else if (cur) {
        S.micName = cur.label || '';
      }
    } catch (e) { /* 枚举失败就用默认，不阻断录音 */ }
    return stream;
  }

  async function acquireStreams() {
    const got = { mic: null, system: null };

    if (S.source === 'mic' || S.source === 'dual') {
      got.mic = await acquireMic();
    }

    if (S.source === 'system' || S.source === 'dual') {
      // 系统声音只能跟着屏幕共享一起要；拿到后把画面轨停掉，只留音频
      const display = await navigator.mediaDevices.getDisplayMedia({
        video: { frameRate: 1 },
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
      if (!display.getAudioTracks().length) {
        display.getTracks().forEach((t) => t.stop());
        throw new Error('没拿到系统声音：请在弹窗里选「整个屏幕」，并勾选「同时分享系统音频」');
      }
      display.getVideoTracks().forEach((t) => t.stop());
      got.system = display;
    }

    if (!got.mic && !got.system) throw new Error('没有可用的音频输入');
    return got;
  }

  function setMixHint(text) {
    if (mixHintEl) mixHintEl.textContent = text;
  }

  function setMixState(text) {
    if (mixStateEl) mixStateEl.textContent = text;
  }

  async function setupMix() {
    if (!S.audioCtx) return;
    // 总线只负责「送进麦克风」，绝不接回 worklet——
    // 否则分身说的话会被自己的转写链路再听一遍，形成回环。
    S.mixBus = S.audioCtx.createGain();
    S.mixBus.gain.value = 1;

    S.sinkGain = S.audioCtx.createGain();
    S.sinkGain.gain.value = 0;          // 默认不往虚拟声卡送
    S.mixBus.connect(S.sinkGain);

    S.msDest = S.audioCtx.createMediaStreamDestination();
    S.sinkGain.connect(S.msDest);

    // 合成音：一份进总线（给对方），一份进监听（给自己）
    S.ttsGain = S.audioCtx.createGain();
    S.ttsGain.gain.value = 1;
    S.ttsAudio = new Audio();
    S.ttsAudio.preload = 'auto';
    S.ttsSrc = S.audioCtx.createMediaElementSource(S.ttsAudio);
    S.ttsSrc.connect(S.ttsGain);
    S.ttsGain.connect(S.mixBus);

    S.monitorGain = S.audioCtx.createGain();
    S.monitorGain.gain.value = mixMonitorEl.checked ? 0.9 : 0;
    S.ttsGain.connect(S.monitorGain);
    S.monitorGain.connect(S.audioCtx.destination);

    // 真麦：进混音的支路可单独压低（ducking）
    if (S.micNode) {
      S.micMixGain = S.audioCtx.createGain();
      S.micMixGain.gain.value = 1;
      S.micNode.connect(S.micMixGain);
      S.micMixGain.connect(S.mixBus);
    }

    // 隐藏的 <audio> 承载混音流，再用 setSinkId 定向到虚拟声卡
    S.sinkAudio = new Audio();
    S.sinkAudio.srcObject = S.msDest.stream;
    S.sinkAudio.autoplay = true;
    S.sinkAudio.style.display = 'none';
    document.body.appendChild(S.sinkAudio);
    S.sinkAudio.play().catch(() => { /* 需要手势，试播时再补 */ });

    await refreshSinks();
    if (S.micNode && S.micName) {
      setMixHint(mixHintEl.textContent + ` 采集麦克风：${S.micName}`);
    }
    if (!S.micNode) {
      setMixHint(mixHintEl.textContent +
        ' 注意：当前来源没有麦克风，你真人的声音不会进微信（要混真声请选「麦克风」或「双路分轨」）。');
    }
    applyMix();
  }

  async function refreshSinks() {
    if (!mixSinkEl) return;
    let outs = [];
    try {
      const all = await navigator.mediaDevices.enumerateDevices();
      outs = all.filter((d) => d.kind === 'audiooutput');
    } catch (e) { /* 下面统一提示 */ }

    mixSinkEl.innerHTML = '';
    outs.forEach((d, i) => {
      const opt = document.createElement('option');
      opt.value = d.deviceId;
      opt.textContent = d.label || `输出设备 ${i + 1}`;
      mixSinkEl.appendChild(opt);
    });

    // 注意：VB-CABLE 会同时注册 "CABLE Input" 和 "CABLE In 16ch"，两个都能用
    const cable = outs.find((d) => /CABLE In|VoiceMeeter/i.test(d.label || ''));
    if (cable) {
      mixSinkEl.value = cable.deviceId;
      setMixHint(`已找到虚拟声卡：${cable.label}。微信麦克风要选 CABLE Output。`);
    } else if (outs.length && !outs.some((d) => d.label)) {
      setMixHint('读不到设备名：先用「麦克风」或「双路分轨」点一次开始，授权后才有名字。');
    } else {
      setMixHint('没找到 CABLE Input：确认 VB-CABLE 已安装并重启过电脑。');
    }
    if (mixEnableEl.checked && !cable) mixEnableEl.checked = false;
  }

  async function applySink() {
    if (!S.sinkAudio || !mixSinkEl.value) return false;
    if (typeof S.sinkAudio.setSinkId !== 'function') {
      setMixHint('这个浏览器不支持指定输出设备，请用 Chrome 或 Edge。');
      return false;
    }
    try {
      await S.sinkAudio.setSinkId(mixSinkEl.value);
      return true;
    } catch (err) {
      setMixHint(`切换输出设备失败：${err.name}（设备被独占或采样率不匹配）`);
      return false;
    }
  }

  function applyMix() {
    if (!S.audioCtx) return;
    const on = !!mixEnableEl.checked;
    if (S.sinkGain) S.sinkGain.gain.setTargetAtTime(on ? 1 : 0, S.audioCtx.currentTime, 0.02);
    if (S.monitorGain) {
      S.monitorGain.gain.setTargetAtTime(mixMonitorEl.checked ? 0.9 : 0, S.audioCtx.currentTime, 0.02);
    }
    setMixState(on ? '已接入' : '未启用');
    if (on) applySink();
  }

  function duck(on) {
    if (!S.micMixGain || !S.audioCtx) return;
    const now = S.audioCtx.currentTime;
    S.micMixGain.gain.cancelScheduledValues(now);
    S.micMixGain.gain.setTargetAtTime(on ? DUCK_LEVEL : 1, now, 0.06);
  }

  function drawLevel() {
    if (!S.analyser) return;
    const data = new Uint8Array(S.analyser.fftSize);
    S.analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (let i = 0; i < data.length; i++) {
      const v = (data[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / data.length);
    const level = Math.min(1, rms * 7);
    const items = bars.children;
    for (let i = 0; i < items.length; i++) {
      // 中间高两边低，看起来像电平条
      const shape = 1 - Math.abs(i - (items.length - 1) / 2) / (items.length / 2);
      const h = 4 + level * shape * 22 * (0.6 + Math.random() * 0.4);
      items[i].style.height = `${h}px`;
    }
    S.rafId = requestAnimationFrame(drawLevel);
  }
  window.MeetAudio = {
    isVirtualMic,
    acquireMic,
    acquireStreams,
    setMixHint,
    setMixState,
    setupMix,
    refreshSinks,
    applySink,
    applyMix,
    duck,
    drawLevel,
  };
})();
