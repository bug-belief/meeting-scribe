/* 录音转写前端：麦克风 → PCM16 → WebSocket；停止后把转写稿/纪要渲染出来 */
(() => {
  /* 共享状态集中在 web/state.js（O-12 D-1 收口）。
   * 读写一律走 S.xxx —— 原来这些是 IIFE 里的裸 let，99 个函数交叉读写，
   * 不集中就拆不成模块。约定：不要再往这里加新的共享变量。 */
  const S = window.MeetState;

  /* 纯函数来自 web/lib/（O-12 D-2）：格式化、说话人配色、唤起判定。
   * 解构成局部常量，**调用点一个都没改** —— 拆分时这是风险最低的做法。 */
  const {
    SPK_SLOTS, fmtClock, fmtTs, fmtMoney, spkHash, spkName, isAssistantName, spkClass, escHtml,
  } = window.MeetFormat;
  const { wakeDecision, WAKE_RE, WAKE_ONLY_RE } = window.MeetWake;

  /* 音频能力来自 web/audio.js（O-12 D-3）：采集、混音总线、电平、虚拟声卡绕行。
   * 同样解构成本地名字，调用点不改。 */
  const {
    isVirtualMic, acquireMic, acquireStreams,
    setMixHint, setMixState, setupMix, refreshSinks, applySink, applyMix, duck, drawLevel,
  } = window.MeetAudio;

  /* 渲染能力来自 web/render.js（O-12 D-4）：字幕、分身播报行、结果稿、计时。
   * 同样解构成本地名字，调用点不改。 */
  const {
    resetLines, assistantLabel, addLine, showPartial, clearPartial,
    scrollBottom, nearBottom,
    liveSayEls, liveSayNew, liveSayThinking, liveSayDraft, liveSayProgress,
    liveSayEnd, liveSayClear,
    markdown, tintSpeakers, render, showResult, tickClock, recSec,
    PROMPT_FOLLOW_PAUSE_MS,
  } = window.MeetRender;

  /* 归档能力来自 web/archive.js（O-12 D-4）：回放、收藏、声纹认领、成本。 */
  const {
    setPlayback, hidePlayback, setMeetingId,
    paintStar, refreshStar, toggleStar,
    speakerRow, loadSpeakers,
    refreshCost, startCostTimer, stopCostTimer,
    openLatest,
  } = window.MeetArchive;

  /* 语音分身来自 web/tts.js（O-12 D-5）：合成、排队、播放、打断、记账。 */
  const {
    playTts, splitScript, setSayState, renderQueue, sayVoiceKind,
    updateSayVoiceHint, loadSayVoices,
    synthUnit, playUnit, announceBegin, announceEnd, sayTest,
    speakOn, liveSayMuted,
    sayDraft, sayRun, sayInterrupt, loadSamples,
  } = window.MeetTts;

  /* 提词器来自 web/prompter.js（O-12 D-5）：会中实时给下一句。 */
  const {
    promptOn, promptVisible, setPromptVisible, promptText, kbScenario,
    loadKbScenarios, renderPrompt, promptFetch, promptSchedule, promptReset,
    promptSourceNote, promptAllOn, setPromptAll,
    PROMPT_DEBOUNCE_MS, PROMPT_KIND_LABEL,
  } = window.MeetPrompter;
  const $ = (id) => document.getElementById(id);
  const btn = $('recbtn');
  const clock = $('clock');
  const bars = $('bars');
  const linesEl = $('lines');
  const hint = $('hint');
  const badge = $('badge');
  const badgeText = $('badgeText');
  const result = $('result');
  const doc = $('doc');
  const toastEl = $('toast');
  const sourcesEl = $('sources');
  const srchintEl = $('srchint');
  const mixPanel = $('mixPanel');
  const mixEnableEl = $('mixEnable');
  const mixMonitorEl = $('mixMonitor');
  const mixSinkEl = $('mixSink');
  const mixSampleEl = $('mixSample');
  const mixPlayEl = $('mixPlay');
  const mixHintEl = $('mixHint');
  const mixStateEl = $('mixState');
  const sayVoiceEl = $('sayVoice');
  const sayVoiceHintEl = $('sayVoiceHint');
  const sayTestEl = $('sayTest');
  const sayTextEl = $('sayText');
  const sayGoEl = $('sayGo');
  const sayDraftEl = $('sayDraft');
  const sayStopEl = $('sayStop');
  const sayWakeEl = $('sayWake');
  const wakeModeEl = $('wakeMode');
  const saySpeakEl = $('saySpeak');
  const promptOnEl = $('promptOn');
  const promptAllEl = $('promptAll');
  const promptPhoneticEl = $('promptPhonetic');
  const kbScenarioEl = $('kbScenario');
  const promptEl = $('prompt');
  const promptBodyEl = $('promptBody');
  const promptMetaEl = $('promptMeta');
  const promptCloseEl = $('promptClose');
  const promptSayEl = $('promptSay');
  const promptAgainEl = $('promptAgain');
  const costBadgeEl = $('costBadge');
  const sayQueueEl = $('sayQueue');
  const sayStateEl = $('sayState');
  const playbackEl = $('playback');
  const playerEl = $('player');
  const pbtipEl = $('pbtip');
  const meetingIdEl = $('meetingId');
  const starBtnEl = $('starBtn');
  const speakersEl = $('speakers');
  const latestEntryEl = $('latestEntry');
  const logoutEntryEl = $('logoutEntry');

  const WS_URL = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/live';
  const BARS = 28;
  // 提词防抖：一句话说完才发请求，别在半句上烧 token
  // 手动滚动后暂停自动跟随的时长（提词区和转写区共用）

  const SOURCE_HINTS = {
    mic: '手机/电脑麦克风。切到后台可能被系统挂起。',
    system: '录电脑正在播放的声音（微信通话、会议、视频）。选择「整个屏幕」并勾选「同时分享系统音频」。',
    dual: '麦克风进左声道、系统声音进右声道，转写稿直接分「我 / 对方」。建议戴耳机，否则两边会串音。',
  };

  // 已送进录音链路的采样数（助理时间线换算成「录音第几秒」用）

  // ---- 分身混音：真麦 + 下行 TTS 混成一路，伪装成麦克风送进微信 ----

  for (let i = 0; i < BARS; i++) bars.appendChild(document.createElement('i'));

  function setSource(name, persist) {
    S.source = SOURCE_HINTS[name] ? name : 'mic';
    if (persist) { try { localStorage.setItem('wb.source', S.source); } catch (e) {} }
    sourcesEl.querySelectorAll('button').forEach((b) => {
      b.classList.toggle('on', b.dataset.src === S.source);
    });
    srchintEl.textContent = SOURCE_HINTS[S.source];
  }

  function lockSources(on) {
    sourcesEl.classList.toggle('locked', !!on);
  }

  function toast(msg, ms = 2200) {
    toastEl.textContent = msg;
    toastEl.classList.add('on');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => toastEl.classList.remove('on'), ms);
  }

  function setBadge(text, cls) {
    badgeText.textContent = text;
    badge.className = 'badge' + (cls ? ' ' + cls : '');
  }

  // ---------- 说话人配色 ----------
  // 同一个名字在任何地方都是同一个颜色：按名字哈希取一个色号（不是每次刷新
  // 随机），只在同屏撞色时才顺延一格。助理固定紫，和任何人都不会撞。

  // ---------- 实时面板里的助理台词 ----------
  // 助理的声音经 CABLE 回到系统声卡，会被 ASR 再听一遍：既重复又错字。
  // 所以助理说的话**不经过 ASR**，直接显示我们让它说的那句原文：
  // 已经说出去的字用正常颜色，还没说的字压灰——一眼能看出说到哪了。

  async function keepAwake() {
    try {
      if ('wakeLock' in navigator) {
        S.wakeLock = await navigator.wakeLock.request('screen');
        S.wakeLock.addEventListener('release', () => { S.wakeLock = null; });
      }
    } catch (e) { /* 不支持就算了，别打扰用户 */ }
  }

  function releaseAwake() {
    if (S.wakeLock) { S.wakeLock.release().catch(() => {}); S.wakeLock = null; }
  }

  // 按采集来源拿流：mic / system / dual（麦克风 + 系统声音）

  async function start() {
    if (S.state !== 'idle' && S.state !== 'done') return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast('这个浏览器不支持录音，换 Chrome 或 Edge 试试');
      return;
    }
    if (!window.isSecureContext) {
      toast('必须用 https 访问（或 localhost），否则浏览器不给麦克风');
      return;
    }
    if (S.source !== 'mic' && !navigator.mediaDevices.getDisplayMedia) {
      toast('当前浏览器不支持抓取系统声音，请在电脑上用 Chrome 或 Edge');
      return;
    }

    S.state = 'connecting';
    btn.disabled = true;
    btn.textContent = '…';
    S.fatalShown = false;      // 新一轮开始，重置「已提示过致命错误」
    setBadge('连接中', 'busy');
    lockSources(true);
    resetLines();
    hidePlayback();

    try {
      const got = await acquireStreams();
      S.micStream = got.mic;
      S.sysStream = got.system;
      S.streams = [got.mic, got.system].filter(Boolean);
    } catch (err) {
      S.state = 'idle';
      btn.disabled = false;
      btn.textContent = '开始';
      setBadge('待机', '');
      lockSources(false);
      const name = err && err.name;
      if (name === 'NotAllowedError') toast('权限被拒绝了');
      else if (name === 'NotSupportedError') toast('这个浏览器不支持抓取系统声音');
      else toast((err && err.message) || ('打不开音频输入：' + err), 4200);
      return;
    }

    S.ws = new WebSocket(WS_URL);
    S.ws.binaryType = 'arraybuffer';

    S.ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      handleMessage(msg);
    };

    S.ws.onerror = () => {
      toast('连接服务失败');
    };

    S.ws.onclose = () => {
      // 注意：缺本地 ASR 时服务端会先发 fatal 再 close。那种情况下已经提示过了，
      // 这里再喊一句「连接断了」只会让人以为还有第二个问题。
      if (S.fatalShown) return;
      if (S.state === 'recording') {
        toast('连接断了，录音已停止');
        stopAudio();
        setIdle();
      } else if (S.state !== 'idle') {
        // 卡在「连接中」就断：以前这里什么都不提示，表现为「点了开始没反应」
        toast('连接没建立成功，看服务日志或稍后重试', 5000);
        S.state = 'idle';
        stopAudio();
        setIdle();
        lockSources(false);
      }
    };

    S.ws.onopen = async () => {
      try {
        // 先声明采集方式，服务端据此决定声道数与切块大小，再开始推流
        S.ws.send(JSON.stringify({
          type: 'start',
          source: S.source,
          channels: S.source === 'dual' ? 2 : 1,
        }));
        await startAudio();
        // 设备名上报：同一段声音用耳机麦、手机外接麦、笔记本内置麦录，声纹和音色都不一样。
        // 攒音色语料时要按设备分桶，不能混在一起平均。
        if (S.micName) S.ws.send(JSON.stringify({ type: 'meta', mic: S.micName }));
      } catch (err) {
        toast('音频初始化失败：' + err);
        S.ws.close();
        stopAudio();
        setIdle();
      }
    };
  }

  async function startAudio() {
    // 优先让浏览器直接给 16k；不支持就按实际采样率在 worklet 里重采样
    try {
      S.audioCtx = new AudioContext({ sampleRate: 16000 });
    } catch (e) {
      S.audioCtx = new AudioContext();
    }
    if (S.audioCtx.state === 'suspended') await S.audioCtx.resume();

    await S.audioCtx.audioWorklet.addModule('/static/processor.js');
    const channels = S.source === 'dual' && S.micStream && S.sysStream ? 2 : 1;
    S.sessionChannels = channels;
    S.worklet = new AudioWorkletNode(S.audioCtx, 'pcm16-worklet', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      channelCount: channels,
      channelCountMode: 'explicit',
      channelInterpretation: 'discrete',
      processorOptions: { targetRate: 16000, packetSamples: 1600, channels },
    });

    // 必须接到 destination 才会被驱动；用 0 增益避免把自己的声音放出来
    const mute = S.audioCtx.createGain();
    mute.gain.value = 0;
    S.analyser = S.audioCtx.createAnalyser();
    S.analyser.fftSize = 1024;
    const level = S.audioCtx.createGain();   // 电平显示：两路都并进来
    level.gain.value = 0.5;
    level.connect(S.analyser);

    if (channels === 2) {
      // 左声道 = 麦克风（你），右声道 = 系统声音（对方）
      const merger = S.audioCtx.createChannelMerger(2);
      S.micNode = S.audioCtx.createMediaStreamSource(S.micStream);
      const sysNode = S.audioCtx.createMediaStreamSource(S.sysStream);
      S.micNode.connect(merger, 0, 0);
      sysNode.connect(merger, 0, 1);
      S.micNode.connect(level);
      sysNode.connect(level);
      merger.connect(S.worklet);
    } else {
      const node = S.audioCtx.createMediaStreamSource(S.streams[0]);
      node.connect(S.worklet);
      node.connect(level);
      // 单声道时 streams[0] 可能就是系统声音，只有确实是麦克风才接进混音
      if (S.micStream && S.streams[0] === S.micStream) S.micNode = node;
    }

    S.worklet.connect(mute);
    mute.connect(S.audioCtx.destination);

    await setupMix();

    S.worklet.port.onmessage = (e) => {
      // 记录已推入录音的采样数：助理时间线要用它换算「录音第几秒」
      if (e.data && e.data.byteLength) {
        S.sentSamples += e.data.byteLength / (2 * (S.sessionChannels || 1));
      }
      if (S.ws && S.ws.readyState === WebSocket.OPEN) S.ws.send(e.data);
    };

    S.state = 'recording';
    S.startedAt = Date.now();
    S.sentSamples = 0;
    btn.disabled = false;
    btn.textContent = '停止';
    btn.classList.add('rec', 'stop');
    setBadge('录音中', 'rec');
    S.timerId = setInterval(tickClock, 500);
    drawLevel();
    keepAwake();
    addLine('（开始录音，说话即可看到文字）', 0).classList.add('pending');
    setTimeout(() => { if (S.state === 'recording') resetLines(); }, 2600);
    if (mixPlayEl) mixPlayEl.disabled = false;
    if (sayGoEl) sayGoEl.disabled = false;
    if (sayDraftEl) sayDraftEl.disabled = false;
    if (sayTestEl) sayTestEl.disabled = false;
    S.peerMaxRms = 0;
    S.peerSilentAlerted = false;
    loadSayVoices();

    // 用户从浏览器工具条上关掉「停止共享」时，体面地结束这次录音
    if (S.sysStream) {
      S.sysStream.getTracks().forEach((t) => t.addEventListener('ended', () => {
        if (S.state === 'recording') {
          toast('系统声音共享已停止，正在收尾');
          stop();
        }
      }));
    }
  }

  // ---------- 台词队列：预览 / 逐句进度 / 打断作废 ----------
  // 关键约定：**录音是唯一真相**。逐句播放，没播出去的句子就没进录音，
  // 因此回放和转写稿天然一致，不需要事后剪音频。

  function wsSend(obj) {
    if (S.ws && S.ws.readyState === WebSocket.OPEN) {
      try { S.ws.send(JSON.stringify(obj)); } catch (e) {}
    }
  }

  // 单句上限 30 字：TTS 是「整句合成完才开始播」，句子越长首字越慢。
  // 30 字大约 6~8 秒音频，冷启动合成 2 秒左右；再长就会明显觉得「它怎么不说话」。
  // 一句话切多长。经验值：CosyVoice2 的 RTF ≈ 0.45（合成 1 秒音频要 0.45 秒），
  // 所以切得越短，第一声出得越快。30 字一句要合成 3~5 秒，用户体感就是"半天不说话"。
  // 16 字大约 3.5 秒音频、1.6 秒合成，后面几句靠预取顶着，衔接听不出断点。

  // ---------- 提词器 ----------
  // 对方说完一句，就给你一句「你可以这么接」。三条硬规矩：
  // 1) 防抖 1.2 秒：还在说话时发请求纯属烧钱，等这句话定稿停下来再发。
  //    实测 TTS 才是成本大头，但提词省的是 LLM 调用次数——一句话调三次和调一次差三倍。
  // 2) 双缓冲：新结果没回来之前，屏幕上保留上一条。否则每次定稿都闪一下空白，
  //    人正读到一半被清空，比不给提示还烦。
  // 3) 手动滚动后 3 秒内不自动滚到底：他在往回看，别把他拽回去。

  // ---------- 成本徽标 ----------
  // 只显示两个数：折算价 + 本地省下多少。想细看就用命令行跑 cost_report.py。

  // 右声道看门狗：连着几秒一点能量都没有，说明「分享系统音频」没勾上 / 对方声音
  // 走了别的设备，这一路是纯静音。以前这种情况只会安静地出空稿子，必须明确报出来。
  function watchPeerLevel(msg) {
    if ((S.sessionChannels || 1) < 2) return;
    const r = Number(msg.rms_r) || 0;
    if (r > S.peerMaxRms) S.peerMaxRms = r;
    if (S.peerSilentAlerted) return;
    const secs = S.sentSamples / 16000;
    if (secs < 8) return;
    if (S.peerMaxRms > 0.003) return;
    S.peerSilentAlerted = true;
    toast('右声道一直是静音：共享屏幕时没勾「同时分享系统音频」，或对方的声音没走这个设备',
      6000);
    setMixHint('右声道 0 能量：这次只录到了你这一侧，转写稿里不会有「对方」的话。');
  }

  // 语音助理模式：喊一声「语音助理」就进入对话窗口，窗口内可以连着说，
  // 不用每句都重新喊——这样才像在跟人讲话，而不是对着指令框下命令。
  // 唤醒词表与「是否纯唤醒词」的判定都在 web/lib/wake.js（可单测）。
  const WAKE_WINDOW_MS = 90000;
  // 「我这边一直没声音」判定的两个门槛：
  // SELF_SILENT_RMS 取在底噪（实测 0.0007~0.0018）和说话（0.02+）之间，
  // 留足余量，避免把「说话声音小」误判成「闭麦」。
  const SELF_SILENT_RMS = 0.005;
  const SELF_MUTED_SEC = 4;

  // 说话人归属：单声道下后端不发 who，分不清是谁——按「可能是我」放行，
  // 否则单声道用户永远唤不醒。这是有意的降级，不是漏判。
  function isSelf(who) {
    return !who || who === S.selfName;
  }

  // 左声道能量记账：判断「我是不是把麦闭了」。拿不到 rms_l 就当作没闭麦。
  function noteSelfLevel(msg) {
    const l = Number(msg.rms_l);
    if (!Number.isFinite(l) || l < 0) { S.selfSilentSince = 0; return; }
    if (l < SELF_SILENT_RMS) {
      if (!S.selfSilentSince) S.selfSilentSince = Date.now();
    } else {
      S.selfSilentSince = 0;
    }
  }

  function selfMicMuted() {
    return S.selfSilentSince > 0 && Date.now() - S.selfSilentSince > SELF_MUTED_SEC * 1000;
  }

  function wakeMode() {
    const v = wakeModeEl ? wakeModeEl.value : 'strict';
    return ['strict', 'guest', 'open', 'proxy'].includes(v) ? v : 'strict';
  }

  function maybeWake(text, who) {
    // 先把「该不该查」这件事本身也报上去。
    // 2026-10-22 教训：上一版只在通过前两个门槛后才打日志，
    // 结果「语音助理模式关掉了」这种情况**一条日志都没有**，看起来像"完全没反应"，
    // 反而比有日志更难查。所以现在只要文本像唤醒词，无论结果如何都上报一次。
    const t = String(text || '').trim();
    const looksLikeWake = /语音|助理|助手/.test(t);
    const mode = wakeMode();
    const byMe = isSelf(who);
    const inWindow = Date.now() < S.wakeUntil;
    const called = WAKE_RE.test(t);
    const checked = !!(sayWakeEl && sayWakeEl.checked);

    // 上报到服务端（走已有的 WS，不额外开请求）。服务端日志一条命令能看到全部。
    const report = (decision, blocked) => {
      if (!looksLikeWake) return;
      try {
        wsSend({
          type: 'wake_probe',
          text: t.slice(0, 40), who: who || '',
          mode, byMe, called, inWindow, checked,
          decision: decision || null, blocked: blocked || '',
        });
      } catch (e) { /* 上报失败不能影响主流程 */ }
      console.warn(`[唤醒] ${decision ? '已触发 ' + decision : '没触发'}${blocked ? '：' + blocked : ''}`,
        { text: t, who, mode, byMe, called, inWindow, checked, selfName: S.selfName });
    };

    if (!checked) {
      report(null, '语音助理模式没勾（sayWake 复选框）');
      // 喊了却没开模式：不能静默什么都不做 —— 用户会以为功能坏了。
      // 每次录音只提示一次，避免反复喊就反复弹。
      if (looksLikeWake && !S.wakeOffHinted) {
        S.wakeOffHinted = true;
        toast('语音助理模式没打开：勾上「语音助理模式」再喊', 4000);
      }
      return;
    }
    if (t.length < 2) { report(null, '文本太短'); return; }

    const decision = wakeDecision({
      mode, byMe, called, inWindow, micMuted: selfMicMuted(),
    });
    if (!decision) {
      report(null, called ? `规则未放行（mode=${mode}, byMe=${byMe}）` : '未匹配唤醒词');
      return;
    }
    const summoned = decision === 'summon';
    const autoProxy = decision === 'proxy';

    if (S.wakeBusy) { report(null, '上一轮还在处理（wakeBusy）'); return; }
    // 喊名字就等于「先别说了，听我说」：当场打断，再基于新输入重新回答
    if (summoned && S.sayRunning) sayInterrupt();
    // 播报刚结束的一两秒内听到的"人声"，很可能就是助理自己被录回来的声音
    if (Date.now() - S.lastTtsEndAt < 1500) {
      report(null, `刚播报完 ${Date.now() - S.lastTtsEndAt}ms（防自激）`);
      return;
    }
    if (Date.now() - S.wakeAt < 2000) {
      report(null, `距上次唤醒 ${Date.now() - S.wakeAt}ms（防连触）`);
      return;
    }
    S.wakeAt = Date.now();
    if (summoned) S.wakeUntil = Date.now() + WAKE_WINDOW_MS;
    setMixHint(
      autoProxy ? '你这边没声音，助理替你接一句'
        : summoned ? '助理已唤醒，90 秒内可以直接接着说话'
          : '助理在听…',
    );
    report(decision, '');
    sayDraft(true, summoned);
  }

  function handleMessage(msg) {
    switch (msg.type) {
      case 'fatal': {
        // 服务端明确说「这条链路起不来」时必须让用户看见。
        // 典型场景：只装了基础依赖、没装本地 ASR → 实时字幕不可用。
        // 这时**不能**静默收摊：用户刚点了开始，界面若无其事故意最糟。
        S.fatalShown = true;                 // 让 onclose 别再喊「连接断了」
        const text = msg.error || '服务端拒绝了这次会话';
        toast(text, 6000);
        // 常驻显示在空状态区：toast 几秒就没了，用户回头看不到原因
        // 用 textContent 而不是 innerHTML：项目里一律走 textContent（天然免转义），
        // 且这里没有需要保留的富文本
        if (hint) hint.textContent = text;
        S.state = 'idle';
        stopAudio();
        setIdle();
        lockSources(false);
        break;
      }
      case 'ready':
        S.liveSessionId = msg.session || '';
        // 说话人标签由后端下发，别在前端硬编码：DUAL_SELF_NAME 改了也要能对上
        if (msg.self_name) S.selfName = msg.self_name;
        if (msg.peer_name) S.peerName = msg.peer_name;
        S.selfSilentSince = 0;
        // 提词条跟着录音一起出现；关掉开关就完全不显示，不占版面
        promptReset(false);
        setPromptVisible(promptOn());
        startCostTimer();
        // 会话一开始就把唤醒相关状态报给服务端。
        // 2026-10-22 教训：「语音助理模式没勾」会让整条链路一条日志都没有，
        // 现象像"完全没反应"。这条让日志里立刻能看出复选框是开是关。
        try {
          wsSend({
            type: 'wake_probe',
            text: '（会话开始）',
            mode: wakeMode(),
            checked: !!(sayWakeEl && sayWakeEl.checked),
            decision: 'ready',
            blocked: '',
            inWindow: false,
            called: false,
            byMe: true,
          });
        } catch (e) { /* 不影响主流程 */ }
        break;
      case 'partial':
        noteSelfLevel(msg);   // 先记账：下面几个分支都会提前 break
        // 助理正在说话时右声道的「字」是它自己：不显示，否则面板上全是错字
        if (Number(msg.rms_r) > Number(msg.rms_l)
            && (S.sayRunning || Date.now() - S.lastTtsEndAt < 1200)) { clearPartial(); break; }
        // 底噪上编出来的碎片（「的的的的」）：服务端已经判掉了，这里连占位行都不留
        if (msg.quiet) { clearPartial(); watchPeerLevel(msg); break; }
        showPartial(msg.text);
        watchPeerLevel(msg);
        break;
      case 'drop':
        // 服务端把这一整行判定为噪声幻听（没人说话）：面板上不该出现
        clearPartial();
        break;
      case 'line':
        clearPartial();
        // 兜底：服务端偶尔漏标（事件丢失）时，右声道在播报期间的定稿行也不进面板。
        // 录音本身还在，最终稿会按播报日志重建，不会丢内容。
        if (msg.who === '对方' && (S.sayRunning || Date.now() - S.lastTtsEndAt < 1500)) break;
        addLine(msg.text, msg.start, msg.who);
        maybeWake(msg.text, msg.who);
        // 提词只跟对方的话走：自己说的不需要提示，未标注（单声道）两边都算。
        //
        // ⚠️ 但「喊助理」那几句必须排除（2026-10-21 实测）：右声道静音时不标 who，
        // 于是自己的话也会走到这里；服务端 SYSTEM_PROMPT 里有一条
        // 「如果对方在叫你（喊助理），提示『应一声，问什么事』」，
        // 模型看到「语音助理」就照那条回了 —— 这正是用户看到的「答案不对」。
        // 唤醒词是控制指令，不是会议议题，不该拿去换话术。
        //
        // 把这句话本身传进去：提词面板要显示「他在问什么」，
        // 否则回看历史时只剩孤零零的几句提示，看不懂上下文。
        //
        // 规则本身（含"分轨没分出对方时自动降级"）写在 prompter.promptSourceNote，
        // 这里只负责问一句「这条要不要提词」，别把策略散成两处。
        const want = promptSourceNote(msg.who, isSelf);
        if (!WAKE_ONLY_RE.test(msg.text) && want !== 'skip') {
          promptSchedule(msg.text);
        }
        break;
      case 'finalizing':
        setBadge('整理中', 'busy');
        break;
      case 'done':
        clearPartial();
        showResult(msg);
        break;
      case 'error':
        toast('出错：' + (msg.message || '未知'));
        setIdle();
        break;
      default:
        break;
    }
  }

  function stopAudio() {
    // 队列还在跑就先做一次「打断」，让时间线知道实际播出到哪
    if (S.sayRunning) sayInterrupt();
    promptReset(true);
    stopCostTimer();
    if (sayGoEl) sayGoEl.disabled = true;
    if (sayDraftEl) sayDraftEl.disabled = true;
    if (sayTestEl) sayTestEl.disabled = true;
    if (sayStopEl) sayStopEl.disabled = true;
    clearInterval(S.timerId);
    cancelAnimationFrame(S.rafId);
    S.rafId = 0;
    if (S.worklet) { try { S.worklet.port.onmessage = null; } catch (e) {} S.worklet = null; }
    if (S.audioCtx) { S.audioCtx.close().catch(() => {}); S.audioCtx = null; }
    S.streams.forEach((s) => s.getTracks().forEach((t) => { try { t.stop(); } catch (e) {} }));
    S.streams = [];
    S.micStream = null;
    S.sysStream = null;
    S.analyser = null;
    releaseAwake();
    for (const el of bars.children) el.style.height = '4px';

    // 混音链跟着会话一起拆掉，避免下一次重复创建 MediaElementSource
    if (S.ttsAudio) { try { S.ttsAudio.pause(); } catch (e) {} S.ttsAudio.src = ''; S.ttsAudio = null; }
    if (S.sinkAudio) {
      try { S.sinkAudio.pause(); } catch (e) {}
      S.sinkAudio.srcObject = null;
      S.sinkAudio.remove();
      S.sinkAudio = null;
    }
    S.ttsSrc = S.ttsGain = S.micMixGain = S.mixBus = S.sinkGain = S.msDest = S.monitorGain = null;
    if (mixPlayEl) mixPlayEl.disabled = true;
    setMixState(mixEnableEl.checked ? '待开始' : '未启用');
  }

  function stop() {
    if (S.state !== 'recording') return;
    S.state = 'processing';
    btn.disabled = true;
    btn.textContent = '…';
    btn.classList.remove('rec');
    setBadge('整理中', 'busy');
    clearInterval(S.timerId);
    stopAudio();
    try {
      S.ws.send(JSON.stringify({ type: 'stop' }));
    } catch (e) {
      toast('发送停止指令失败');
      setIdle();
    }
  }

  function setIdle() {
    S.state = 'idle';
    btn.disabled = false;
    btn.textContent = '开始';
    btn.classList.remove('rec', 'stop');
    setBadge('待机', '');
    lockSources(false);
  }

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
      S.currentTab = tab.dataset.tab;
      document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t === tab));
      render();
    });
  });

  $('closeBtn').addEventListener('click', () => {
    hidePlayback();
    result.hidden = true;
    setBadge('待机', '');
    linesEl.innerHTML = '<div class="hint">点下面的按钮开始新的录音。</div>';
  });

  if (starBtnEl) starBtnEl.addEventListener('click', toggleStar);
  $('copyBtn').addEventListener('click', async () => {
    const text = S.currentTab === 'report' ? S.resultData.report : S.resultData.transcript;
    try {
      await navigator.clipboard.writeText(text || '');
      toast('已复制');
    } catch (e) {
      toast('复制失败，长按选中手动复制');
    }
  });

  $('dlBtn').addEventListener('click', () => {
    const text = S.currentTab === 'report' ? S.resultData.report : S.resultData.transcript;
    const blob = new Blob([text || ''], { type: 'text/markdown;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = S.currentTab === 'report' ? 'report.md' : 'transcript.md';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  });

  /* 供 audio.js 反向调用（toast 提示、stop 收尾）。
   * 为什么需要这层：audio.js 在 app.js 之前加载，拿不到这里的函数；
   * 而 audio.js 又在用户操作时才需要它们，所以运行时有就行。
   * 约定：只暴露这一小撮「单向回调」，不要把整个 app 挂上去。 */
  window.MeetApp = { toast, stop, setBadge, setIdle, wsSend };

  btn.addEventListener('click', () => {
    if (S.state === 'idle') start();
    else if (S.state === 'recording') stop();
  });

  sourcesEl.addEventListener('click', (e) => {
    const target = e.target.closest('button[data-src]');
    if (!target || S.state !== 'idle') return;
    setSource(target.dataset.src, true);
  });

  if (latestEntryEl) {
    latestEntryEl.addEventListener('click', (e) => {
      e.preventDefault();
      openLatest();
    });
  }

  /* 切换账号（2026-10-23）：多租户靠「口令 → owner」解析，换人用只能换口令。
     此前没有出口 —— 手机上除了卸载重装，没别的办法把 wb_k 清掉。 */
  if (logoutEntryEl) {
    logoutEntryEl.addEventListener('click', async () => {
      // 录音中一律拦住：stop() 之后还要等服务端整理归档，
      // 这时候跳走等于把这一场录音丢掉。宁可让用户多点一次。
      if (S.state === 'recording') {
        toast('先停止录音，等它整理完再切换账号', 4500);
        return;
      }
      if (!confirm('切换账号会退出当前口令，回到输入口令页。\n已保存的录音不会删除。继续？')) {
        return;
      }
      logoutEntryEl.disabled = true;
      try {
        const r = await fetch('/api/logout', { method: 'POST', credentials: 'same-origin' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
      } catch (e) {
        logoutEntryEl.disabled = false;
        toast('退出失败：' + ((e && e.message) || '网络错误'));
        return;
      }
      // 这两个键装的是「上一个人的东西」，必须清：
      //   wb.sayVoice  —— 克隆音色按 owner 独占，留着会 403
      //   wb.kbScenario—— 是原租户的知识场景偏好
      // 其余 wb.* 都是本机界面偏好（声源/唤起档/提词开关），换人不该被重置。
      try {
        localStorage.removeItem('wb.sayVoice');
        localStorage.removeItem('wb.kbScenario');
      } catch (e) { /* 隐私模式 */ }
      // cookie 已经删掉，回到 `/` 会拿到 401 + 激活页
      location.replace('/');
    });
  }

  mixEnableEl.addEventListener('change', applyMix);
  mixMonitorEl.addEventListener('change', applyMix);
  mixSinkEl.addEventListener('change', applySink);
  mixPlayEl.addEventListener('click', playTts);
  if (sayGoEl) sayGoEl.addEventListener('click', sayRun);
  if (sayDraftEl) sayDraftEl.addEventListener('click', () => sayDraft(false, false));
  if (sayTestEl) sayTestEl.addEventListener('click', sayTest);
  if (sayStopEl) sayStopEl.addEventListener('click', sayInterrupt);
  loadSamples();
  // 音色列表进来就拉一次，别等开始录音才显示（否则下拉框一直是空的）
  loadSayVoices();

  try {
    if (localStorage.getItem('wb.mix') === '1') mixEnableEl.checked = true;
    if (localStorage.getItem('wb.monitor') === '0') mixMonitorEl.checked = false;
    // 唤起策略和「是否出声」都要记住：每次刷新都回到默认值会让人以为设置没生效
    const wm = localStorage.getItem('wb.wakeMode');
    if (wm && wakeModeEl) wakeModeEl.value = wm;
    if (localStorage.getItem('wb.saySpeak') === '0' && saySpeakEl) saySpeakEl.checked = false;
    if (localStorage.getItem('wb.sayWake') === '0' && sayWakeEl) sayWakeEl.checked = false;
    if (localStorage.getItem('wb.promptOn') === '0' && promptOnEl) promptOnEl.checked = false;
    // 「全听」开关持久化：这是用户按场景定的偏好，重开页面要记住
    if (localStorage.getItem('wb.promptAll') === '1' && promptAllEl) {
      setPromptAll(true, false);
    }
    if (localStorage.getItem('wb.promptPhonetic') === '1' && promptPhoneticEl) {
      promptPhoneticEl.checked = true;
    }
  } catch (e) { /* 隐私模式 */ }
  if (wakeModeEl) {
    wakeModeEl.addEventListener('change', () => {
      try { localStorage.setItem('wb.wakeMode', wakeModeEl.value); } catch (e) {}
      // 换档就清空唤醒窗口：否则切到 strict 之后，之前 guest 档攒下的窗口还在
      S.wakeUntil = 0;
    });
  }
  if (saySpeakEl) {
    saySpeakEl.addEventListener('change', () => {
      try { localStorage.setItem('wb.saySpeak', saySpeakEl.checked ? '1' : '0'); } catch (e) {}
      setMixHint(saySpeakEl.checked ? '助理会自动出声' : '只听不说：它仍会给文字，但不出声');
    });
  }
  if (sayWakeEl) {
    sayWakeEl.addEventListener('change', () => {
      try { localStorage.setItem('wb.sayWake', sayWakeEl.checked ? '1' : '0'); } catch (e) {}
      if (!sayWakeEl.checked) S.wakeUntil = 0;
    });
  }
  if (promptOnEl) {
    promptOnEl.addEventListener('change', () => {
      try { localStorage.setItem('wb.promptOn', promptOnEl.checked ? '1' : '0'); } catch (e) {}
      if (!promptOnEl.checked) {
        promptReset(true);
      } else if (S.state === 'recording') {
        setPromptVisible(true);
        promptFetch(true);
      }
    });
  }
  if (promptAllEl) {
    // 「全听」开关：默认只听对方；勾上后不分谁说的，每句话都提词。
    // 放在提词面板头上（而不是设置区），因为它是**按场景临时切换**的：
    //   开会谈判 → 只听对方（知道他问什么就够了）
    //   自己梳理思路 / 电话外放混音 → 全听
    promptAllEl.addEventListener('change', () => {
      setPromptAll(promptAllEl.checked, true);
      // 刚从"只听对方"切到"全听"时，如果本场还没有任何提词，
      // 立刻按最近内容要一次，别让人勾了之后还得等下一句话。
      if (promptAllEl.checked && !S.promptItems.length && S.state === 'recording') {
        promptFetch(true);
      }
    });
  }
  if (promptPhoneticEl) {
    promptPhoneticEl.addEventListener('change', () => {
      try {
        localStorage.setItem('wb.promptPhonetic', promptPhoneticEl.checked ? '1' : '0');
      } catch (e) {}
      // 注音版本后端已经一起下发了，这里只是换个字段渲染，不再请求
      renderPrompt(S.promptItems, true);
    });
  }
  if (kbScenarioEl) {
    kbScenarioEl.addEventListener('change', () => {
      try { localStorage.setItem('wb.kbScenario', kbScenarioEl.value || ''); } catch (e) {}
      // 已经显示着的提词是按旧场景检索出来的，换档就清掉，不要让人看串味的提示
      S.promptItems = [];
      renderPrompt([], true);
    });
    loadKbScenarios();
  }
  if (promptBodyEl) {
    // 手动往回滚就暂停自动跟随；滚回底部立即恢复
    promptBodyEl.addEventListener('scroll', () => {
      S.promptUserScrollAt = nearBottom(promptBodyEl) ? 0 : Date.now();
      // 进度条要跟着手指走：否则用户往下滚，条还停在上次渲染的位置
      if (window.MeetPrompter) window.MeetPrompter.syncPromptMeter();
    }, { passive: true });
    // 窗口尺寸变化会改 clientHeight（转屏 / 拖窗 / 手机键盘弹出收起），比例得重算
    window.addEventListener('resize', () => {
      if (window.MeetPrompter) window.MeetPrompter.syncPromptMeter();
    }, { passive: true });
  }
  // 转写区同理：正在翻上面的内容时，新句子不把人拽回底部
  linesEl.addEventListener('scroll', () => {
    S.linesUserScrollAt = nearBottom(linesEl) ? 0 : Date.now();
  }, { passive: true });
  if (promptCloseEl) {
    promptCloseEl.addEventListener('click', () => setPromptVisible(false));
  }
  if (promptAgainEl) {
    promptAgainEl.addEventListener('click', () => promptFetch(true));
  }
  if (promptSayEl) {
    // 只填不发：提词是「要点」，直接念出来会很生硬，得让他自己润一句
    promptSayEl.addEventListener('click', () => {
      const text = S.promptItems.map((x) => promptText(x)).join('，');
      if (!text) return;
      if (sayTextEl) {
        sayTextEl.value = text;
        if (mixPanel) mixPanel.open = true;
        sayTextEl.focus();
        sayTextEl.setSelectionRange(text.length, text.length);
      }
      toast('已填进台词框，改完再点「让它说」');
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (String(e.key || '').toLowerCase() === 'p') {
      if (!promptOn()) { toast('先在「分身混音」里勾上提词器'); return; }
      e.preventDefault();
      const show = !promptVisible();
      setPromptVisible(show);
      if (show) promptFetch(!S.promptItems.length);
    } else if (e.key === 'Escape' && promptVisible()) {
      setPromptVisible(false);
    }
  });
  mixEnableEl.addEventListener('change', () => {
    try { localStorage.setItem('wb.mix', mixEnableEl.checked ? '1' : '0'); } catch (e) {}
  });
  mixMonitorEl.addEventListener('change', () => {
    try { localStorage.setItem('wb.monitor', mixMonitorEl.checked ? '1' : '0'); } catch (e) {}
  });
  setMixState(mixEnableEl.checked ? '待开始' : '未启用');

  try { S.savedSource = localStorage.getItem('wb.source') || 'mic'; } catch (e) { /* 隐私模式 */ }
  setSource(S.savedSource, false);

  // 页面切走时不要静默继续（浏览器会挂起 WS），明确停下来并提示。
  // 2026-10-23：提示文案由「可能在几十秒后被挂起」改成说清**挂起后是什么现象**。
  // 原来那句太像免责声明，用户看不懂"挂起"意味着：手机上看到的就是「录音还在跑，
  // 但字幕一停半小时，回来发现中间全丢了」。说清后果，才算真提示。
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && S.state === 'recording') {
      toast(S.source === 'mic'
        ? '切到后台了：系统会在几十秒后停掉麦克风，这段时间的字幕会缺一段'
        : '页面在后台，屏幕共享还在跑，采集会继续', 4500);
    }
    if (!document.hidden && S.state === 'recording') {
      // 屏幕锁／切后台时浏览器会自动释放 wakeLock，回来必须重新要，
      // 否则「一直亮着」的保证只覆盖第一次。这里顺带把断流也提示出来。
      const wasAway = S.wakeLock === null;
      keepAwake();
      if (wasAway) {
        toast('已回到前台。刚才那段如果字幕有缺口，是系统把音频挂起造成的，不是识别坏了', 4000);
      }
    }
  });
})();
