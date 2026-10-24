/* 实时转写的共享状态（O-12 D-1）。
 *
 * 这些字段原来散在 app.js 各处，是 IIFE 里的裸 let。
 * （不写具体个数：手抄的数字一定会漂，要数就 `grep -c` 一次。）
 * 收口到这里的唯一理由：app.js 有 99 个函数交叉读写它们，
 * 不先集中，就拆不成模块（拆出去的模块会互相 import 对方的变量）。
 *
 * 用法：const S = window.MeetState; 然后 S.state / S.ws / ...
 * 约定：读写一律走 S.xxx，别再往 IIFE 里加新的共享 let。
 * 重置：调用 MeetState.reset() 把全部字段恢复初值（原来没有统一重置，见下方说明）。
 */
(() => {
  const INIT = Object.freeze({
    // ---- 会话与连接 ----
    state: 'idle',  // idle | connecting | recording | processing
    // 服务端发过 fatal（如缺本地 ASR）。用来让 onclose 别再重复喊「连接断了」
    fatalShown: false,
    ws: null,
    liveSessionId: '',  // 服务端会话 id：代答要拿它取最近几轮上下文
    sessionChannels: 1,
    source: 'mic',  // mic | system | dual
    savedSource: 'mic',

    // ---- 计时与调度 ----
    timerId: 0,
    startedAt: 0,
    rafId: 0,
    costTimer: 0,
    promptTimer: 0,

    // ---- 音频采集 ----
    audioCtx: null,
    streams: [],
    micStream: null,
    sysStream: null,
    worklet: null,
    analyser: null,
    micNode: null,  // 真麦在 AudioContext 里的源节点
    micName: '',  // 实际用到的麦克风名字，显示在混音面板里

    // ---- 混音总线 ----
    mixBus: null,  // 混音总线
    micMixGain: null,  // 真麦进混音的支路（分身说话时被压低）
    sinkGain: null,  // 总线 → 虚拟声卡的闸门
    msDest: null,  // 总线 → MediaStream
    sinkAudio: null,  // 把 MediaStream 定向输出到 CABLE Input
    monitorGain: null,  // 只给耳机听的那一路
    ducking: false,

    // ---- 分身播放 ----
    ttsAudio: null,  // 承载合成音的 <audio>
    ttsSrc: null,  // ttsAudio 的 MediaElementSource
    ttsGain: null,  // 合成音总增益
    sayQueue: [],
    sayRunning: false,
    sayAbort: false,
    lastTtsEndAt: 0,  // 助理刚说完的时间，用来防止「自己的声音触发自己」

    // ---- 字幕与结果 ----
    pendingRow: null,
    linesUserScrollAt: 0,
    resultData: { transcript: '', report: '' },
    currentTab: 'transcript',
    currentMid: '',
    currentStarred: false,

    // ---- 唤起与对端 ----
    wakeLock: null,
    wakeBusy: false,  // 防止一句话触发多次代答
    wakeAt: 0,  // 上一次自动接话的时间
    wakeUntil: 0,
    // 「语音助理模式没勾」的提示每次录音只弹一次。
    // 用户喊了唤醒词却没开模式时，原来完全静默 → 像"完全没反应"。
    wakeOffHinted: false,
    liveSay: null,
    selfName: '我',  // 由 ready 消息下发，见 server.py
    peerName: '对方',
    selfSilentSince: 0,  // 左声道连续静音的起点（0 = 当前有声）
    peerMaxRms: 0,  // 右声道（系统声音/对方）历史最大能量，用来发现「根本没录到」
    peerSilentAlerted: false,

    // ---- 提词器 ----
    promptBusy: false,
    // 请求进行中又来了新台词：记一笔，回来立刻补一次（不叠加）
    promptQueued: false,
    promptItems: [],
    promptUserScrollAt: 0,
    // 本会话里「对方」到底有没有开过口。
    // 提词本来只听对方的话（自己说的不用提示），但双路分轨没起作用时
    // （右声道只有背景噪 → 服务端把你说的话也标成「我」），
    // 这个前提就不成立：一行都不触发，提词器成了哑巴，而且毫无提示。
    // 2026-10-22 实测就是这么哑掉的，所以要看这个标志决定要不要降级。
    peerSpoke: false,
    soloLines: 0,      // 连续出现「非对方」定稿行的条数，够了就降级
    promptWhy: '',     // 面板空着的原因（用来显示给人看，别让人以为它在等）
    // 「全听」：不分谁说的，每句话都给提示（用户手动开关，持久化）。
    // 默认关 —— 开会的常态是"对方问、我来答"，只听对方更省 token 也更聚焦。
    // 但单人梳理思路、电话外放把两路混在一起时，就需要全听。
    // 这是**用户主动选择**，与下面那个"分轨失效自动降级"是两回事：
    //   手动全听 = 我要它听我说
    //   自动降级 = 分轨坏了，只能退一步（保底，不该常态发生）
    promptAll: false,

    // ---- 采集计数 ----
    sentSamples: 0,

  });

  window.MeetState = {
    ...Object.fromEntries(Object.entries(INIT).map(([k, v]) => [k, cloneNode(v)])),
  };

  function cloneNode(v) {
    if (Array.isArray(v)) return [];
    if (v && typeof v === 'object') return { ...v };
    return v;
  }

  // 把全部字段恢复初值。原来代码里没有统一重置（各函数自己零散地清几个），
  // 这里只提供入口，D-1 不改任何调用点——先不接线，避免改变现有行为。
  window.MeetState.reset = function reset() {
    for (const [k, v] of Object.entries(INIT)) window.MeetState[k] = cloneNode(v);
  };
})();
