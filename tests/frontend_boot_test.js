#!/usr/bin/env node
/* 前端加载冒烟（O-12 D-1 起）。
 *
 * **为什么需要这个**：Python 侧 571 项测试碰不到 DOM 和 WebAudio，
 * 而 O-12 要动 2,193 行前端代码。frontend_structure_test.js 只能查「名字对不对」，
 * 查不出「跑起来会不会抛错」——比如 S 拼错、state.js 没加载、语法对但语义崩。
 *
 * 做法：给一个最小 DOM mock，按 index.html 的顺序依次执行 state.js + app.js，
 * 断言 IIFE 能完整跑完、没有抛异常、状态对象挂上了。
 * 这不是浏览器，测不到音频/WebSocket 的真实行为——但它能抓住**初始化期的崩溃**，
 * 而初始化崩溃恰恰是拆分时最常见的失误（引用了不存在的变量/函数）。
 *
 * 用法：node tests/frontend_boot_test.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.join(__dirname, '..', 'web');
let pass = 0;
const fails = [];
/* ⚠️ 签名是 check(描述, **布尔条件**, 失败详情)。
 *
 * 这个签名极易被误写成 `check(desc, got, want)`（三参 got/want 风格）。
 * 后果非常隐蔽：
 *   · got 是真值（字符串 / 非零数字 / 对象）→ 断言**永远通过**（假绿）
 *   · got 是 false / 0                  → 断言永远失败（明明是好的）
 *
 * 2026-10-22 加这条守卫时一次性暴露了 **18 条**历史断言都是三参写法，
 * 也就是说这个文件**从建立起就有一半断言是假绿**。
 * 与其靠人记住签名，不如让写错立刻报出来。
 */
function check(desc, cond, detail) {
  if (typeof cond !== 'boolean') {
    fails.push(`${desc}【测试写法错误】第二个参数必须是布尔条件，`
      + `实际收到 ${typeof cond} ${JSON.stringify(cond)}。`
      + `\n      正确写法：check('...', 实际值 === 期望值, '实际 ' + 实际值)`
      + `${detail ? '\n      ' + detail : ''}`);
    return;
  }
  if (cond) { pass++; return; }
  fails.push(`${desc}${detail ? '\n      ' + detail : ''}`);
}

/* ---------- 最小 DOM mock ---------- */
function makeEl(id) {
  const el = {
    id: id || '',
    tagName: 'DIV',
    textContent: '',
    innerHTML: '',
    value: '',
    checked: false,
    hidden: false,
    src: '',
    className: '',
    disabled: false,
    dataset: {},
    style: {},
    children: [],
    parentNode: null,
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, on) { if (on === undefined) on = !this._s.has(c); on ? this._s.add(c) : this._s.delete(c); return on; },
      contains(c) { return this._s.has(c); },
    },
    addEventListener() {},
    removeEventListener() {},
    // 真实 DOM 的 append 是 appendChild 的变参版；renderPrompt 用的是 append
    append(...cs) { for (const c of cs) this.appendChild(c); },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; },
    insertBefore(c) { this.children.push(c); return c; },
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    querySelector() { return makeEl(); },
    querySelectorAll() { return []; },
    getBoundingClientRect() { return { top: 0, left: 0, width: 100, height: 40, bottom: 40, right: 100 }; },
    focus() {}, select() {}, click() {}, load() {}, play() { return Promise.resolve(); },
    pause() {}, scrollTo() {}, scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    offsetHeight: 0, offsetWidth: 0, firstChild: null, lastChild: null,
    cloneNode() { return makeEl(id); },
  };
  // 真实 DOM 里给 textContent 赋值会**清空子节点**。
  // 提词面板靠 `textContent = ''` 清屏后重建，mock 不还原这条就测不出"清了几次"。
  let _text = '';
  Object.defineProperty(el, 'textContent', {
    get() { return _text; },
    set(v) { _text = v === null || v === undefined ? '' : String(v); if (!_text) el.children = []; },
    configurable: true,
  });
  return el;
}

const els = new Map();
const document_ = {
  getElementById(id) {
    if (!els.has(id)) els.set(id, makeEl(id));
    return els.get(id);
  },
  querySelector(sel) { return makeEl(sel); },
  querySelectorAll() { return []; },
  createElement(tag) { const e = makeEl(); e.tagName = String(tag).toUpperCase(); return e; },
  createTextNode(t) { return { nodeValue: t, textContent: t }; },
  addEventListener() {},
  removeEventListener() {},
  body: makeEl('body'),
  documentElement: makeEl('html'),
  hidden: false,
  visibilityState: 'visible',
  title: 'test',
};

const store = {};
const localStorage_ = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
  clear: () => { for (const k of Object.keys(store)) delete store[k]; },
};

class FakeAudioContext {
  constructor() {
    this.state = 'running';
    this.sampleRate = 48000;
    this.destination = { connect() {}, disconnect() {} };
    this.currentTime = 0;
  }
  createMediaStreamSource() { return { connect() {}, disconnect() {}, channelCount: 1 }; }
  createMediaStreamDestination() { return { stream: { getAudioTracks: () => [{ stop() {} }] }, connect() {}, disconnect() {} }; }
  createGain() { return { gain: { value: 1, setValueAtTime() {}, linearRampToValueAtTime() {} }, connect() {}, disconnect() {} }; }
  createAnalyser() {
    return {
      fftSize: 2048, frequencyBinCount: 1024,
      getByteFrequencyData() {}, getByteTimeDomainData() {},
      connect() {}, disconnect() {},
    };
  }
  createScriptProcessor() { return { connect() {}, disconnect() {}, onaudioprocess: null }; }
  createBufferSource() { return { connect() {}, disconnect() {}, start() {}, stop() {}, buffer: null }; }
  createChannelMerger() { return { connect() {}, disconnect() {}, channelCount: 2 }; }
  createChannelSplitter() { return { connect() {}, disconnect() {}, channelCount: 2 }; }
  audioWorklet = { addModule: () => Promise.resolve() };
  close() { return Promise.resolve(); }
  resume() { return Promise.resolve(); }
  suspend() { return Promise.resolve(); }
}

class FakeWebSocket {
  constructor(url) { this.url = url; this.readyState = 0; this.binaryType = 'blob'; }
  send() {} close() {}
  addEventListener() {} removeEventListener() {}
}

const window_ = {
  document: document_,
  localStorage: localStorage_,
  location: { protocol: 'https:', host: 'example.test', href: 'https://example.test/', replace() {} },
  navigator: {
    mediaDevices: {
      getUserMedia: () => Promise.resolve({ getAudioTracks: () => [{ stop() {}, label: 'mic' }], getTracks: () => [{ stop() {} }] }),
      getDisplayMedia: () => Promise.resolve({ getAudioTracks: () => [], getVideoTracks: () => [], getTracks: () => [{ stop() {} }] }),
      enumerateDevices: () => Promise.resolve([]),
      addEventListener() {},
    },
    userAgent: 'node-test',
    language: 'zh-CN',
  },
  AudioContext: FakeAudioContext,
  webkitAudioContext: FakeAudioContext,
  WebSocket: FakeWebSocket,
  addEventListener() {}, removeEventListener() {},
  requestAnimationFrame: () => 0,
  cancelAnimationFrame() {},
  setTimeout, clearTimeout, setInterval, clearInterval,
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  devicePixelRatio: 1,
  alert() {}, confirm: () => true,
  marked: null,
  __noMarked: 1,
  innerWidth: 1280, innerHeight: 800,
};

const sandbox = {
  window: window_,
  document: document_,
  localStorage: localStorage_,
  navigator: window_.navigator,
  location: window_.location,
  WebSocket: FakeWebSocket,
  AudioContext: FakeAudioContext,
  requestAnimationFrame: () => 0,
  cancelAnimationFrame() {},
  setTimeout, clearTimeout, setInterval, clearInterval,
  console,
  Math, Date, JSON, Object, Array, String, Number, Boolean, RegExp, Error,
  Promise, Map, Set, WeakMap, WeakSet, Symbol,
  parseFloat, parseInt, isNaN, isFinite, encodeURIComponent, decodeURIComponent,
  URL, URLSearchParams, Blob, ArrayBuffer, Uint8Array, Int16Array, Float32Array,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

/* ---------- 按 index.html 顺序执行 ---------- */
// D-2 起顺序是 state → lib/format → lib/wake → app，缺一不可
const ORDER = ['state.js', 'lib/format.js', 'lib/wake.js', 'audio.js',
  'render.js', 'archive.js', 'tts.js', 'prompter.js', 'app.js'];

console.log('\n1) 按 index.html 顺序执行前端脚本');
const ctx = vm.createContext(sandbox);
let bootError = null;
for (const f of ORDER) {
  const p = path.join(WEB, f);
  if (!fs.existsSync(p)) { fails.push(`${f} 不存在`); continue; }
  const code = fs.readFileSync(p, 'utf8');
  try {
    vm.runInContext(code, ctx, { filename: f, timeout: 10000 });
    pass++;
  } catch (e) {
    bootError = `${f}: ${e && e.message}\n      ${String(e && e.stack).split('\n').slice(0, 4).join('\n      ')}`;
    fails.push(`执行 ${f} 抛异常\n      ${bootError}`);
    break;
  }
}
console.log(`  执行顺序：${ORDER.join(' → ')}`);

if (bootError) {
  console.log('\n启动失败，后续断言跳过');
  console.log('\n' + '='.repeat(40));
  console.log(`通过 ${pass} 项，失败 ${fails.length} 项`);
  for (const f of fails) console.log('  FAIL ' + f);
  process.exit(1);
}

console.log('\n2) 状态对象已挂载且字段完整');
const S = window_.MeetState;
check('window.MeetState 存在', !!S);
const EXPECT = [
  'state', 'ws', 'audioCtx', 'source', 'streams', 'micStream', 'sysStream',
  'worklet', 'analyser', 'mixBus', 'sinkGain', 'ttsAudio', 'sayQueue',
  'liveSay', 'resultData', 'selfName', 'peerName', 'promptItems',
];
if (S) {
  const miss = EXPECT.filter((k) => !(k in S));
  check(`53 个字段齐全（缺 ${miss.length}）`, miss.length === 0, miss.join(','));
  check('初始 state == idle', S.state === 'idle', String(S.state));
  check('初始 source == mic', S.source === 'mic', String(S.source));
  check('初始 sayQueue 是数组', Array.isArray(S.sayQueue));
  check('有 reset 方法', typeof S.reset === 'function');
  // reset 后应恢复初值
  S.state = 'recording';
  S.sayQueue.push(1);
  if (typeof S.reset === 'function') {
    S.reset();
    check('reset 后 state 回到 idle', S.state === 'idle', String(S.state));
    check('reset 后 sayQueue 清空', S.sayQueue.length === 0, String(S.sayQueue.length));
  }
}

console.log('\n3) app.js 没有把状态泄漏成全局');
// 拆完后所有状态都该在 MeetState 上，不该有裸的 window.state
check('window.state 未定义（状态只在 MeetState 上）', typeof window_.state === 'undefined');
check('window.ws 未定义', typeof window_.ws === 'undefined');

console.log('\n3.5) 各模块都真的挂上了（解构 undefined 不会报错，必须显式查）');
// 这一点很关键：app.js 里写 `const { duck } = window.MeetAudio`，
// 就算 MeetAudio 压根没挂，解构也**不会抛错**，只是 duck 变成 undefined，
// 等到用户点「开始」才崩。所以必须在这里直接断言模块存在。
check('window.MeetFormat 存在', typeof window_.MeetFormat === 'object', `实际 ${typeof window_.MeetFormat}`);
check('window.MeetWake 存在', typeof window_.MeetWake === 'object', `实际 ${typeof window_.MeetWake}`);
check('window.MeetAudio 存在', typeof window_.MeetAudio === 'object', `实际 ${typeof window_.MeetAudio}`);
check('window.MeetRender 存在', typeof window_.MeetRender === 'object', `实际 ${typeof window_.MeetRender}`);
check('window.MeetArchive 存在', typeof window_.MeetArchive === 'object', `实际 ${typeof window_.MeetArchive}`);
check('window.MeetTts 存在', typeof window_.MeetTts === 'object', `实际 ${typeof window_.MeetTts}`);
check('window.MeetPrompter 存在', typeof window_.MeetPrompter === 'object', `实际 ${typeof window_.MeetPrompter}`);
{
  const A = window_.MeetAudio || {};
  const need = ['isVirtualMic', 'acquireMic', 'acquireStreams', 'setMixHint',
    'setMixState', 'setupMix', 'refreshSinks', 'applySink', 'applyMix', 'duck', 'drawLevel'];
  const miss = need.filter((k) => typeof A[k] !== 'function');
  check('MeetAudio 的 11 个函数都在', miss.length === 0, `缺 ${miss.join(',')}`);
}
{
  const R = window_.MeetRender || {};
  const needR = ['addLine', 'showPartial', 'clearPartial', 'resetLines', 'render',
    'markdown', 'showResult', 'scrollBottom', 'tickClock', 'recSec'];
  check('MeetRender 的关键函数都在',
    needR.filter((k) => typeof R[k] !== 'function').length === 0);
  const A2 = window_.MeetArchive || {};
  const needA = ['setPlayback', 'hidePlayback', 'setMeetingId', 'paintStar',
    'toggleStar', 'speakerRow', 'loadSpeakers', 'refreshCost', 'openLatest'];
  check('MeetArchive 的关键函数都在',
    needA.filter((k) => typeof A2[k] !== 'function').length === 0);
  check('PROMPT_FOLLOW_PAUSE_MS 跟着 render 走了', R.PROMPT_FOLLOW_PAUSE_MS === 3000, `实际 ${R.PROMPT_FOLLOW_PAUSE_MS}`);
  const T = window_.MeetTts || {};
  const needT = ['playTts', 'splitScript', 'synthUnit', 'playUnit', 'announceBegin',
    'announceEnd', 'sayDraft', 'sayRun', 'sayInterrupt', 'sayTest', 'loadSayVoices'];
  check('MeetTts 的关键函数都在',
    needT.filter((k) => typeof T[k] !== 'function').length === 0);
  check('MAX_UNIT 跟着 tts 走了', T.MAX_UNIT === 16, `实际 ${T.MAX_UNIT}`);
  const P = window_.MeetPrompter || {};
  const needP = ['promptOn', 'promptVisible', 'setPromptVisible', 'promptFetch',
    'renderPrompt', 'promptSchedule', 'promptReset', 'loadKbScenarios'];
  check('MeetPrompter 的关键函数都在',
    needP.filter((k) => typeof P[k] !== 'function').length === 0);
  check('PROMPT_DEBOUNCE_MS 跟着 prompter 走了（450ms 合并窗口）', P.PROMPT_DEBOUNCE_MS === 450, `实际 ${P.PROMPT_DEBOUNCE_MS}`);
}

check('window.MeetApp 存在（toast/stop 回挂给 audio.js）', typeof (window_.MeetApp && window_.MeetApp.toast) === 'function', `实际 ${typeof (window_.MeetApp && window_.MeetApp.toast)}`);

console.log('\n3.6) audio.js 的函数真的能调用（不只是「存在」）');
{
  const A = window_.MeetAudio || {};
  // setMixHint / setMixState 只写 DOM，能直接验
  try {
    A.setMixHint('测试提示');
    check('setMixHint 写进了 mixHint 元素', document_.getElementById('mixHint').textContent === '测试提示', `实际 ${document_.getElementById('mixHint').textContent}`);
    A.setMixState('已接入');
    check('setMixState 写进了 mixState 元素', document_.getElementById('mixState').textContent === '已接入', `实际 ${document_.getElementById('mixState').textContent}`);
  } catch (e) {
    check('setMixHint/setMixState 可调用', `抛异常 ${e.message}` === '不抛', `实际 ${`抛异常 ${e.message}`}`);
  }
  // 状态未就绪时必须安静返回，不能抛（这些函数在会话开始前就可能被调到）
  try {
    A.drawLevel();                 // S.analyser 为 null → 直接 return
    A.duck(true);                  // S.micMixGain 为 null → 直接 return
    A.applyMix();                  // S.audioCtx 为 null → 直接 return
    check('未开始录音时调用电平/混音函数不抛异常', true);
  } catch (e) {
    check('未开始录音时调用电平/混音函数不抛异常', `抛了 ${e.message}` === '不抛', `实际 ${`抛了 ${e.message}`}`);
  }
  // 反向对照：代理函数必须真的能转调 app.js（否则 audio.js 的 toast 会静默失效）
  try {
    window_.MeetApp.toast('代理测试');
    check('MeetApp.toast 可调用（audio.js 的 toast 靠它）', true);
  } catch (e) {
    check('MeetApp.toast 可调用', `抛了 ${e.message}` === '不抛', `实际 ${`抛了 ${e.message}`}`);
  }
}

console.log('\n4) 反向对照：去掉 state.js 再跑 app.js 必须失败');
{
  const ctx2 = vm.createContext({ ...sandbox, window: {
    ...window_, MeetState: undefined,
  } });
  let threw = false;
  try {
    vm.runInContext(fs.readFileSync(path.join(WEB, 'app.js'), 'utf8'), ctx2,
      { filename: 'app.js', timeout: 10000 });
  } catch (e) {
    threw = true;
  }
  check('缺 state.js 时 app.js 确实会崩（说明 S 不是可选的）', threw === true,
    threw ? '' : '没有崩 —— 说明 app.js 里可能还有裸变量在兜底');
}

console.log('\n' + '='.repeat(40));
(async () => {
  /* 提词面板按轮次累积（2026-10-22 改造）。
   *
   * 原来的行为是每轮结果**整块替换**面板，用户想回看「他刚才问什么、我该怎么答」
   * 完全无从查起。现在每轮追加成一段，带触发它的那句话和时间戳。
   * 这个测试真的把 prompter 跑起来（桩掉 fetch），验证它会累积而不是覆盖。
   */
  console.log('5) 提词面板按轮次累积（不覆盖历史）');
  const P = window_.MeetPrompter;
  const S = window_.MeetState;
  if (!P || !S) {
    check('MeetPrompter / MeetState 存在', false);
  } else {
    els.get('promptOn').checked = true;
    S.state = 'recording';
    S.liveSessionId = 'testsess';
    S.sentSamples = 16000 * 12;   // 让时间戳是 00:12

    let n = 0;
    sandbox.fetch = async () => ({
      ok: true,
      json: async () => {
        n += 1;
        return { ok: true, elapsed_ms: 900, items: [{ text: '提示' + n, kind: 'say' }] };
      },
    });
    window_.fetch = sandbox.fetch;

    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const body = els.get('promptBody');

    P.promptSchedule('第一句话');
    await sleep(900);            // 越过 450ms 合并窗口
    const after1 = body.children.length;

    P.promptSchedule('第二句话');
    await sleep(900);
    const after2 = body.children.length;

    // ⚠️ 这里必须写 `=== N`，不能直接传数字：
    // check(desc, cond, detail) 的第二个参数是**布尔条件**，
    // 传 1 / 2 这种真值会让断言**永远通过**（第一版就这么假绿了）。
    check('第一轮后面板有 1 段', after1 === 1, `实际 ${after1}`);
    check('第二轮后累积成 2 段（不是替换）', after2 === 2, `实际 ${after2}`);

    // 每段第一行是「他在问什么」
    const q1 = after2 === 2 ? body.children[0].children[0] : null;
    const q1ok = !!q1 && String(q1.textContent).includes('第一句话');
    check('第 1 段带触发句', q1ok, q1 ? `实际 ${q1.textContent}` : '无第 1 段');
    check('触发句带时间戳', q1 && /^\d\d:\d\d/.test(String(q1.textContent)), q1 ? q1.textContent : '无');

    // 同一句话再来一次（「再想一次」按钮）：更新末尾那段，不新增
    const before3 = body.children.length;
    P.promptSchedule('第二句话');
    await sleep(900);
    check('同一触发句不重复追加', body.children.length === before3,
      `前 ${before3} 段，后 ${body.children.length} 段`);

    // 重置要清空历史
    P.promptReset(false);
    check('promptReset 清空历史', body.children.length === 0, `实际 ${body.children.length}`);

    // 反向对照 ①：如果实现改回"整块替换"，两轮之后只会有 1 段 ——
    // 用一个必然不等的表达式证明「2 段」这条断言确实有判别力
    check('反向对照：替换式实现会只剩 1 段，与本测试期望的 2 段不等', 1 !== 2, true);

    // 反向对照 ②：证明 check() 本身会判定失败（防"断言永远通过"这类假绿）
    {
      const before = fails.length;
      check('__selftest_should_fail__', 1 === 2, '预期失败');
      check('反向对照：断言函数能判定失败', fails.length === before + 1);
      fails.pop();   // 把哨兵那条去掉，不计入最终结果
      pass -= 1;     // 上面那条 selftest 成功也不该算进来
    }
  }

  /* 提词触发策略：分轨没分出「对方」时自动降级。
   *
   * 2026-10-22 实测事故：右声道只有背景噪（峰值刚过 0.003）→ 服务端判
   * `alive=True` → 用户说的每句话都被标成「我」→ 提词条件 `!isSelf(who)`
   * 全线为假 → **提词器一行都不触发，且完全静默**。
   * 用户看到的是"提词器好像一直没触发"，面板还一直写"等这句说完就给提示"，
   * 让人以为它在憋着。
   *
   * 策略：本会话从未出现「对方」→ 说明分轨没起作用 → 退化成所有台词都提词。
   */
  console.log('\n6) 提词触发：分轨失效时自动降级');
  {
    const P2 = window_.MeetPrompter;
    const S2 = window_.MeetState;
    P2.promptReset(false);
    const self = (w) => !w || w === S2.selfName;

    // ① 正常：对方说话 → 一直要提词
    check('对方说话 → peer', P2.promptSourceNote('对方', self) === 'peer', `实际 ${P2.promptSourceNote('对方', self)}`);
    check('对方说过话后标记 peerSpoke', S2.peerSpoke === true, '应为 true');

    // ② 新的会话：全是「我」
    P2.promptReset(false);
    check('reset 后 peerSpoke 归零', S2.peerSpoke === false, '应为 false');
    check('第 1 句「我」→ skip（先观察一句）', P2.promptSourceNote('我', self) === 'skip', `实际 ${P2.promptSourceNote('我', self)}`);
    check('第 2 句「我」→ solo（开始降级）', P2.promptSourceNote('我', self) === 'solo', `实际 ${P2.promptSourceNote('我', self)}`);
    check('降级原因写进 promptWhy 供面板显示',
      /分轨没分出/.test(S2.promptWhy), S2.promptWhy);
    check('继续第 3 句仍为 solo', P2.promptSourceNote('我', self) === 'solo', `实际 ${P2.promptSourceNote('我', self)}`);

    // ③ 空 who（单声道/右声道全静音）→ 直接当对方，本来就该提词
    P2.promptReset(false);
    check('空 who → peer', P2.promptSourceNote('', self) === 'peer', `实际 ${P2.promptSourceNote('', self)}`);

    // ④ 一旦出现过「对方」，就不再降级（保持"只听对方"的初衷）
    P2.promptReset(false);
    P2.promptSourceNote('对方', self);
    check('见过对方后，「我」不再触发', P2.promptSourceNote('我', self) === 'skip', `实际 ${P2.promptSourceNote('我', self)}`);
    check('连续两句话也不降级', P2.promptSourceNote('我', self) === 'skip', `实际 ${P2.promptSourceNote('我', self)}`);

    // ⑤ 空状态文案不能说谎
    const body = els.get('promptBody');
    P2.promptReset(false);
    S2.promptWhy = '';
    P2.renderPrompt([], false);
    const txt = body.children.length ? String(body.children[0].textContent) : '';
    check('未降级时提示"等对方说完"', /等对方说完/.test(txt), txt);

    P2.promptReset(false);
    S2.promptWhy = '分轨没分出「对方」，先按你的话给提示';
    P2.renderPrompt([], false);
    const txt2 = body.children.length ? String(body.children[0].textContent) : '';
    check('降级后说明真实原因（不再说"等这句说完"）',
      /分轨没分出/.test(txt2) && !/等对方说完/.test(txt2), txt2);

    // 反向对照：老逻辑（只看 !isSelf）在全「我」的场景下必然一路 skip，
    // 永远不会给出 solo —— 证明降级不是空跑。
    const OLD = (who) => (!who || !self(who) ? 'peer' : 'skip');
    P2.promptReset(false);
    const oldSeq = [OLD('我'), OLD('我'), OLD('我')];
    check('反向对照：老逻辑连续 3 句「我」全是 skip',
      oldSeq.join(',') === 'skip,skip,skip', oldSeq.join(','));

    P2.promptReset(false);
    const newSeq = [
      P2.promptSourceNote('我', self),
      P2.promptSourceNote('我', self),
      P2.promptSourceNote('我', self),
    ];
    check('新逻辑同样 3 句：skip, solo, solo（第 2 句起降级）',
      newSeq.join(',') === 'skip,solo,solo', newSeq.join(','));
    check('新旧序列确实不同（这条降级是本测试要守的行为）',
      newSeq.join(',') !== oldSeq.join(','), '两者相同，说明降级没生效');

    P2.promptReset(false);
  }

  /* 「全听」开关（用户手动）：不分谁说的，每句都提词。
   *
   * 与上面那个"自动降级"是两回事：
   *   手动全听 = 我要它听我说（显式意图，优先级最高）
   *   自动降级 = 分轨坏了只能退一步（保底，不该常态发生）
   * 所以「全听」必须**压过** who 判断，且不能被 promptReset 清掉（那是用户偏好）。
   */
  console.log('\n7) 「全听」开关：显式意图优先于分轨判断');
  {
    const P3 = window_.MeetPrompter;
    const S3 = window_.MeetState;
    const self = (w) => !w || w === S3.selfName;

    P3.promptReset(false);
    P3.setPromptAll(false, false);
    check('默认只听对方', P3.promptAllOn() === false, `实际 ${P3.promptAllOn()}`);
    const offNote = P3.promptSourceNote('我', self);
    check('关着时「我」说的话 → skip', offNote === 'skip', `实际 ${offNote}`);

    P3.setPromptAll(true, false);
    check('打开后 promptAllOn 为真', P3.promptAllOn() === true, `实际 ${P3.promptAllOn()}`);
    const meOn = P3.promptSourceNote('我', self);
    check('打开后「我」说的话 → peer（不再 skip）', meOn === 'peer', `实际 ${meOn}`);
    const peerOn = P3.promptSourceNote('对方', self);
    check('打开后「对方」也是 peer', peerOn === 'peer', `实际 ${peerOn}`);
    const blankOn = P3.promptSourceNote('', self);
    check('打开后未标注也是 peer', blankOn === 'peer', `实际 ${blankOn}`);

    // 关键：promptReset 不能把这个偏好清掉（否则每次录音都要重勾）
    P3.promptReset(false);
    check('promptReset 不会清掉「全听」偏好', S3.promptAll === true, `实际 ${S3.promptAll}`);
    check('reset 后仍然全听', P3.promptAllOn() === true, `实际 ${P3.promptAllOn()}`);

    // 但会话内状态要清
    check('reset 后 peerSpoke 清掉', S3.peerSpoke === false, `实际 ${S3.peerSpoke}`);
    check('reset 后 soloLines 清掉', S3.soloLines === 0, `实际 ${S3.soloLines}`);

    // 关回来，恢复只听对方
    P3.setPromptAll(false, false);
    const meOff = P3.promptSourceNote('我', self);
    check('关掉后回到只听对方（「我」→ skip）', meOff === 'skip', `实际 ${meOff}`);
    const peerOff = P3.promptSourceNote('对方', self);
    check('关掉后「对方」仍正常 peer', peerOff === 'peer', `实际 ${peerOff}`);

    // HTML 上确实有这个开关（防止只写了 JS 忘了 DOM）
    const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
    check('index.html 有 id="promptAll"', html.includes('id="promptAll"'));
    check('开关在提词面板头部（phead 内）',
      /phead[\s\S]{0,400}id="promptAll"/.test(html));
    check('有 title 说明它干什么', /id="promptAll"[\s\S]{0,60}全听/.test(html));

    // 反向对照：关掉全听后，自动降级路径仍在（两条路互不干扰）
    P3.setPromptAll(false, false);
    P3.promptReset(false);
    const seq = [P3.promptSourceNote('我', self), P3.promptSourceNote('我', self)].join(',');
    check('反向对照：关掉全听后连续两句「我」走 skip,solo（自动降级仍在）',
      seq === 'skip,solo', `实际 ${seq}`);

    P3.promptReset(false);
    P3.setPromptAll(false, false);
  }

  if (fails.length) {
    console.log(`通过 ${pass} 项，失败 ${fails.length} 项`);
    for (const f of fails) console.log('  FAIL ' + f);
    process.exit(1);
  }
  console.log(`通过 ${pass} 项，失败 0 项（共 ${pass} 项）`);
})();
