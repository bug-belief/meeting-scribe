#!/usr/bin/env node
/* 前端结构护栏（O-12 D-0）。
 *
 * 这是 O-11 里 tests/test_routes.py（27 条路由快照）在前端的等价物。
 * 拆 app.js 之前先把它冻住：函数清单、共享状态清单、每个文件的语法。
 *
 * 为什么要这个：O-12 和 O-11 最大的差别是**没有自动化兜底**。
 *   Python 侧能跑 555 项，前端只有 wake_decision_test.js 那 16 项，
 *   而且它还是「从 app.js 源码里抽函数」的写法——一搬家就失效。
 * 所以先把「现在有什么」冻成基线，之后每批拆完都跑一遍：
 *   函数少了 → 搬丢了；函数多了 → 有人顺手加了新逻辑（本次不该有）。
 *
 * 用法：node tests/frontend_structure.js
 */
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const ROOT = path.join(__dirname, '..');
const WEB = path.join(ROOT, 'web');
const APP = path.join(WEB, 'app.js');

let pass = 0;
const fails = [];
function check(desc, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) { pass++; return; }
  fails.push(`${desc}\n      期望 ${JSON.stringify(want)}\n      实际 ${JSON.stringify(got)}`);
}
function checkTrue(desc, cond, detail) {
  if (cond) { pass++; return; }
  fails.push(`${desc}${detail ? '\n      ' + detail : ''}`);
}

const src = fs.readFileSync(APP, 'utf8');

/* ---------- 1) 语法可解析 ---------- */
console.log('\n1) 语法可解析（node --check）');
const JS_FILES = ['state.js', 'lib/format.js', 'lib/wake.js', 'audio.js',
  'render.js', 'archive.js', 'tts.js', 'prompter.js', 'app.js', 'processor.js'];
for (const f of JS_FILES) {
  const p = path.join(WEB, f);
  if (!fs.existsSync(p)) continue;
  try {
    execFileSync(process.execPath, ['--check', p], { stdio: 'pipe' });
    pass++;
  } catch (e) {
    fails.push(`${f} 语法错误：${String(e.stderr || e.message).slice(0, 300)}`);
  }
}
console.log(`  已检查 ${JS_FILES.length} 个文件`);

/* ---------- 2) 函数清单冻结 ---------- */
console.log('\n2) 99 个基线函数必须都在（分散在 app.js 与 lib/）');
// 只取 IIFE 内 2 空格缩进的顶层 function（嵌套函数不在此列）
const fnRe = /^ {2}(?:async )?function ([A-Za-z0-9_]+)\s*\(/gm;
const fns = [];
let m;
while ((m = fnRe.exec(src)) !== null) fns.push(m[1]);
const fnSet = new Set(fns);

// 拆分是渐进的：D-2 搬走 9 个纯函数，后续批次还会搬。
// 所以**不能用「app.js 必须刚好 90 个」这种硬数字**——每批都会变，改数字容易改错。
// 真正要守的是：**拆分前的 99 个函数，一个都不能丢、也不能在两个文件里各留一份。**
// 另外允许模块内新增私有助手（比如跨模块调用的代理），那些不在基线里，不算违规。
const MODULES = {
  'lib/format.js': ['escHtml', 'fmtClock', 'fmtMoney', 'fmtTs', 'isAssistantName',
    'spkClass', 'spkHash', 'spkName'],
  'lib/wake.js': ['wakeDecision'],
  'audio.js': ['acquireMic', 'acquireStreams', 'applyMix', 'applySink', 'drawLevel',
    'duck', 'isVirtualMic', 'refreshSinks', 'setMixHint', 'setMixState', 'setupMix'],
  'render.js': ['addLine', 'assistantLabel', 'clearPartial', 'liveSayClear', 'liveSayDraft',
    'liveSayEls', 'liveSayEnd', 'liveSayNew', 'liveSayProgress', 'liveSayThinking',
    'markdown', 'nearBottom', 'recSec', 'render', 'resetLines', 'scrollBottom',
    'showPartial', 'showResult', 'tickClock', 'tintSpeakers'],
  'archive.js': ['hidePlayback', 'loadSpeakers', 'openLatest', 'paintStar', 'refreshCost',
    'refreshStar', 'setMeetingId', 'setPlayback', 'speakerRow', 'startCostTimer',
    'stopCostTimer', 'toggleStar'],
  'tts.js': ['announceBegin', 'announceEnd', 'liveSayMuted', 'loadSamples', 'loadSayVoices',
    'playTts', 'playUnit', 'renderQueue', 'sayDraft', 'sayInterrupt', 'sayRun', 'sayTest',
    'sayVoiceKind', 'setSayState', 'speakOn', 'splitScript', 'synthUnit', 'updateSayVoiceHint'],
  'prompter.js': ['kbScenario', 'loadKbScenarios', 'promptFetch', 'promptOn', 'promptReset',
    'promptSchedule', 'promptText', 'promptVisible', 'renderPrompt', 'setPromptVisible'],
};
const moduleFns = {};      // rel → [names]
const allFns = [...fns];   // app.js 的 + 各模块的
for (const [rel, expect] of Object.entries(MODULES)) {
  const p = path.join(WEB, rel);
  if (!fs.existsSync(p)) { fails.push(`${rel} 不存在`); continue; }
  const t = fs.readFileSync(p, 'utf8');
  const got = [];
  const re = /^ {2}(?:async )?function ([A-Za-z0-9_]+)\s*\(/gm;
  let mm;
  while ((mm = re.exec(t)) !== null) got.push(mm[1]);
  moduleFns[rel] = got;
  // 预期清单是「必须包含」，不是「必须相等」——模块内允许加私有助手
  const miss = expect.filter((x) => !got.includes(x));
  check(`${rel} 包含它该有的函数`, miss, []);
  allFns.push(...got);
}

// 冻结基线（2026-10-21 拆分前实测 99 个；按字母序）
const EXPECTED_FNS = [
  'acquireMic', 'acquireStreams', 'addLine', 'announceBegin', 'announceEnd',
  'applyMix', 'applySink', 'assistantLabel', 'clearPartial', 'drawLevel',
  'duck',     'handleMessage',
  'hidePlayback',  'isSelf', 'isVirtualMic', 'kbScenario',
  'keepAwake', 'liveSayClear', 'liveSayDraft', 'liveSayEls', 'liveSayEnd',
  'liveSayMuted', 'liveSayNew', 'liveSayProgress', 'liveSayThinking',
  'loadKbScenarios', 'loadSamples', 'loadSayVoices', 'loadSpeakers',
  'lockSources', 'markdown', 'maybeWake', 'nearBottom', 'noteSelfLevel',
  'openLatest', 'paintStar', 'playTts', 'playUnit', 'promptFetch',
  'promptOn', 'promptReset', 'promptSchedule', 'promptText', 'promptVisible',
  'recSec', 'refreshCost', 'refreshSinks', 'refreshStar', 'releaseAwake',
  'render', 'renderPrompt', 'renderQueue', 'resetLines', 'sayDraft',
  'sayInterrupt', 'sayRun', 'sayTest', 'sayVoiceKind', 'scrollBottom',
  'selfMicMuted', 'setBadge', 'setIdle', 'setMeetingId', 'setMixHint',
  'setMixState', 'setPlayback', 'setPromptVisible', 'setSayState', 'setSource',
  'setupMix', 'showPartial', 'showResult', 'speakOn', 'speakerRow',
    'splitScript', 'start', 'startAudio', 'startCostTimer',
  'stop', 'stopAudio', 'stopCostTimer', 'synthUnit', 'tickClock',
  'tintSpeakers', 'toast', 'toggleStar', 'updateSayVoiceHint',
  'wakeMode', 'watchPeerLevel', 'wsSend',
];

// 拆分前的完整基线（99 个，含已搬到 lib/ 的 9 个）。
// 断言的是「这 99 个函数都还在某个文件里」，而不是「app.js 有几个」——
// 后者每批都会变，改数字改错了反而会造成假绿。
const BEFORE_SPLIT = [...EXPECTED_FNS,
  'fmtClock', 'fmtTs', 'fmtMoney', 'spkHash', 'spkName',
  'isAssistantName', 'spkClass', 'escHtml', 'wakeDecision'];
check('拆分前基线自身是 99 个', BEFORE_SPLIT.length, 99);

{
  const allSet = new Set(allFns);
  const lost = BEFORE_SPLIT.filter((f) => !allSet.has(f));
  if (lost.length) console.log('     丢失：' + lost.join(', '));
  check('99 个基线函数一个都没丢', lost, []);

  // 同一个函数不许在两个文件里各定义**真实现**（搬走却没删，就是这种症状）。
  // 例外：模块里可以有**代理函数**——单行 `return window.MeetXxx.同名(...)`，
  // 用来在运行时反向调用别的模块（模块间双向依赖时必需）。
  // 判据：**非代理的实现最多一份**。一个真实现 + 若干代理 合法；两份真实现 才是问题。
  //
  // 用「按行找子串」而不是拼正则：转义层数太多，之前写正则在这个文件里踩了两次
  // （`\\.` 被 shell 吃掉、`\\s` 变成字面 s）。字符串判断没有这个风险。
  function isProxyIn(rel, name) {
    const t = fs.readFileSync(path.join(WEB, rel), 'utf8');
    for (const line of t.split('\n')) {
      if (!line.includes('function ' + name + '(')) continue;
      // 代理必须是单行、且转调的是 window.Meet*.<同名>
      if (line.includes('return window.Meet') && line.includes('.' + name + '(')) return true;
    }
    return false;
  }
  const dup = [];
  for (const f of BEFORE_SPLIT) {
    const holders = [];   // {rel, proxy}
    if (fns.includes(f)) holders.push({ rel: 'app.js', proxy: isProxyIn('app.js', f) });
    for (const [rel, arr] of Object.entries(moduleFns)) {
      if (arr.includes(f)) holders.push({ rel, proxy: isProxyIn(rel, f) });
    }
    if (holders.length <= 1) continue;
    const real = holders.filter((h) => !h.proxy).map((h) => h.rel);
    if (real.length <= 1) continue;      // 一份真实现 + 代理 → 合法
    dup.push(`${f} 有 ${real.length} 份真实现：${real.join(' + ')}`);
  }
  check('没有函数被重复实现（代理函数除外）', dup, []);

  // 反向对照 ①：真重复必须被抓到（造一个不是代理的同名函数）
  {
    const fake = '  function duck(on) { return on; }';
    const isProxy = fake.includes('return window.Meet') && fake.includes('.duck(');
    check('反向对照：真重复不会被当成代理放过', isProxy, false);
  }
  // 反向对照 ②：真代理必须被认出来（否则合法情况会被误报）
  {
    const real = '  function setPlayback(a, c) { return window.MeetArchive.setPlayback(a, c); }';
    const isProxy = real.includes('return window.Meet') && real.includes('.setPlayback(');
    check('反向对照：代理写法能被识别', isProxy, true);
  }
}

// app.js 里允许出现基线之外的新函数（跨模块代理、新功能），但要留个记录
const extraFns = [...fnSet].filter((f) => !EXPECTED_FNS.includes(f));
if (extraFns.length) console.log(`     app.js 新增的（非基线）：${extraFns.join(', ')}`);
const missingFns = EXPECTED_FNS.filter((f) => !fnSet.has(f) && !BEFORE_SPLIT.includes(f));
if (missingFns.length) console.log('     app.js 缺失：' + missingFns.join(', '));

/* ---------- 3) 共享状态：D-1 起收口到 state.js ---------- */
console.log('\n3) 共享状态字段（已收口到 web/state.js）');
// D-1 之前这些是 app.js 里的裸 let；收口后必须满足：
//   app.js 里 0 个顶层 let（否则就是有人又加回去了）
//   state.js 里的字段名与基线清单一一对应（数量由清单实算）
const letRe = /^ {2}let ([A-Za-z0-9_]+)\s*=/gm;
const appLets = [];
while ((m = letRe.exec(src)) !== null) appLets.push(m[1]);
check('app.js 里没有顶层 let（状态已全部收口）', appLets, []);

const stateSrc = fs.readFileSync(path.join(WEB, 'state.js'), 'utf8');
const fieldRe = /^ {4}([A-Za-z0-9_]+): /gm;
const lets = [];
while ((m = fieldRe.exec(stateSrc)) !== null) lets.push(m[1]);
const letSet = new Set(lets);
checkTrue('app.js 引用了 window.MeetState',
  /const S = window\.MeetState;/.test(src),
  '找不到 const S = window.MeetState');

const EXPECTED_LETS = [
  'analyser', 'audioCtx', 'costTimer', 'currentMid', 'currentStarred',
  'currentTab', 'ducking', 'lastTtsEndAt', 'linesUserScrollAt', 'liveSay',
  'liveSessionId', 'micMixGain', 'micName', 'micNode', 'micStream', 'mixBus',
  'monitorGain', 'msDest', 'peerMaxRms', 'peerName', 'peerSilentAlerted',
  'pendingRow', 'promptBusy', 'promptQueued', 'promptItems', 'promptWhy', 'peerSpoke', 'soloLines', 'promptAll', 'promptTimer',
  'promptUserScrollAt', 'rafId', 'resultData', 'savedSource', 'sayAbort',
  'sayQueue', 'sayRunning', 'selfName', 'selfSilentSince', 'sentSamples',
  'sessionChannels', 'sinkAudio', 'sinkGain', 'source', 'startedAt', 'state',
  'streams', 'sysStream', 'timerId', 'ttsAudio', 'ttsGain', 'ttsSrc', 'wakeAt',
  'wakeBusy', 'wakeLock', 'wakeOffHinted', 'wakeUntil', 'worklet', 'ws',
  // P2-6 遗留：服务端发过 fatal（如缺本地 ASR）时置真，让 onclose 不重复提示
  'fatalShown',
];

// 数字一律从清单实算：原来这里写死 59，加一个字段就要改 4 处（还容易漏），
// 而且「写死总数」正是本仓记过的假护栏模式（改成清单实算后，漏改清单会由下面的差集抓）。
checkTrue(`状态字段总数 == ${EXPECTED_LETS.length}（实际 ${letSet.size}）`,
  letSet.size === EXPECTED_LETS.length);
const missingLets = EXPECTED_LETS.filter((v) => !letSet.has(v));
const extraLets = [...letSet].filter((v) => !EXPECTED_LETS.includes(v));
if (missingLets.length) console.log('     缺失：' + missingLets.join(', '));
if (extraLets.length) console.log('     多出：' + extraLets.join(', '));
check('没有缺失的状态变量', missingLets, []);
check('没有多出的状态变量', extraLets, []);
check('EXPECTED_LETS 无重复项', new Set(EXPECTED_LETS).size, EXPECTED_LETS.length);

/* ---------- 4) 结构约束 ---------- */
console.log('\n4) 结构约束');
checkTrue('app.js 仍以 IIFE 开头', /^\s*\(\(\)\s*=>\s*\{/m.test(src.slice(0, 200)),
  '（拆到 D-5 之后这条会失效，届时改成检查各文件）');
checkTrue('没有出现 ES module 语法（本项目不用 module）',
  !/^\s*(import|export)\s/m.test(src),
  '发现 import/export —— 本批约定不引入 ES module');
checkTrue('文件里没有顶层函数泄漏到全局（都在 IIFE 内）',
  !/^function [A-Za-z0-9_]+\s*\(/m.test(src));

/* ---------- 4.5) ★ 所有状态引用必须走 S.xxx ---------- */
// 这条是 2026-10-21 真实事故后补的：D-1 收口时用状态机替换 434 处，
// 其中一处 `? liveSay : xxx` 的三元真值分支被误判成「对象键」（因为后面跟着 `:`），
// 没替换 → 运行时报 “liveSay is not defined”，**语音助理整块挂掉**。
// 而当时的 frontend_boot 只跑初始化路径，走不到 TTS 分支，所以没抓到。
// → 静态扫一遍：任何状态名只要不是 S.name / 属性访问 / 对象键，就算违规。
console.log('\n4.5) 状态引用是否都走了 S.xxx');

// 先把注释与字符串内容抹成空格（保留换行，行号不乱）
function blankOut(code) {
  const CODE = 0, LINE = 1, BLOCK = 2, SQ = 3, DQ = 4, TPL = 5, REG = 6;
  let st = CODE, out = '', i = 0, prevSig = '';
  const REG_PREV = new Set(['(', ',', '=', ':', '[', '!', '&', '|', '?', '{', '}', ';', '+', '-', '*', '%', '<', '>', '~', '^']);
  const kw = () => {
    let j = out.length - 1;
    while (j >= 0 && /[A-Za-z0-9_$]/.test(out[j])) j--;
    return out.slice(j + 1);
  };
  const KWS = new Set(['return', 'typeof', 'case', 'in', 'of', 'new', 'delete', 'void', 'instanceof']);
  const nextNS = (k) => { while (k < code.length && /\s/.test(code[k])) k++; return code[k]; };
  while (i < code.length) {
    const c = code[i], c2 = code.slice(i, i + 2);
    if (st === CODE) {
      if (c2 === '//') { st = LINE; i += 2; continue; }
      if (c2 === '/*') { st = BLOCK; i += 2; continue; }
      if (c === "'") { st = SQ; i++; continue; }
      if (c === '"') { st = DQ; i++; continue; }
      if (c === '`') { st = TPL; out += ' '; i++; continue; }
      if (c === '/' && (prevSig === '' || REG_PREV.has(prevSig) || KWS.has(kw()))) { st = REG; i++; continue; }
      out += c;
      if (!/\s/.test(c)) prevSig = c;
      i++; continue;
    }
    if (st === LINE) { if (c === '\n') { st = CODE; out += '\n'; } i++; continue; }
    if (st === BLOCK) { if (c2 === '*/') { st = CODE; i += 2; } else { if (c === '\n') out += '\n'; i++; } continue; }
    if (st === SQ || st === DQ || st === REG) {
      const q = st === SQ ? "'" : (st === DQ ? '"' : '/');
      if (c === '\\') { i += 2; continue; }
      if (c === q || (st === REG && c === '\n')) { st = CODE; if (c === '\n') out += '\n'; }
      i++; continue;
    }
    if (st === TPL) {
      if (c === '\\') { i += 2; continue; }
      if (c === '`') { st = CODE; out += ' '; i++; continue; }
      if (c === '\n') out += '\n';
      i++; continue;
    }
  }
  return out;
}

const stripped = blankOut(src);

// 合法豁免：announceEnd(id, state, spokenChars) 的形参叫 state，遮蔽了全局状态。
// 这是**原代码就有的**命名（搬迁时刻意整段跳过，见 D-1 提交说明），
// 函数体里 `state` 指的是那个形参，不是 S.state。
// ⚠️ 豁免必须写明原因，且只豁免这一个函数区间——
//    绝不能像 D-1 的替换脚本那样「用一条启发式规则静默跳过」，那正是这次事故的成因。
// 扫描范围：**所有会读写共享状态的模块**，不只是 app.js。
// D-3 之前这里只扫 app.js —— 那时 audio.js 还不存在。
// 现在音频逻辑搬出去了，如果只扫 app.js，audio.js 里的裸引用就成了盲区
// （D-1 的 liveSay 事故正是「盲区里的漏网」造成的）。
const STATE_SCAN = ['app.js', 'audio.js', 'render.js', 'archive.js', 'tts.js',
  'prompter.js', 'lib/format.js', 'lib/wake.js'];

let scannable = stripped;
{
  const a = stripped.indexOf('function announceEnd(');
  if (a >= 0) {
    // 找配对的右括号：从函数体开始的 { 起数括号
    const bodyStart = stripped.indexOf('{', stripped.indexOf(')', a));
    let depth = 0, end = -1;
    for (let k = bodyStart; k < stripped.length; k++) {
      if (stripped[k] === '{') depth++;
      else if (stripped[k] === '}') { depth--; if (depth === 0) { end = k; break; } }
    }
    if (end > 0) {
      scannable = stripped.slice(0, a) + ' '.repeat(end - a) + stripped.slice(end);
    }
  }
}

const bareRefs = [];

// 把某个函数整段抹成空格（保留换行与列位置）。
// 用途：合法遮蔽的豁免，必须**按语法结构精确切区间**，
// 不能用「整行跳过」或「按名字跳过」这类启发式——那正是 D-1 事故的成因。
function blankFn(code, fnName) {
  const mark = 'function ' + fnName + '(';
  const a = code.indexOf(mark);
  if (a < 0) return code;
  const bodyStart = code.indexOf('{', code.indexOf(')', a));
  if (bodyStart < 0) return code;
  let depth = 0, end = -1;
  for (let k = bodyStart; k < code.length; k++) {
    if (code[k] === '{') depth++;
    else if (code[k] === '}') { depth--; if (depth === 0) { end = k; break; } }
  }
  if (end < 0) return code;
  return code.slice(0, a) + ' '.repeat(end - a + 1) + code.slice(end + 1);
}

function scanForBareRefs(code, label) {
  // 合法豁免：announceEnd(id, state, spokenChars) 的形参叫 state，遮蔽了全局状态。
  // 这是**原代码就有的**命名（D-1 搬迁时刻意保留），函数体里的 `state` 指形参。
  // 该函数在 D-5 从 app.js 搬到了 tts.js，所以豁免要**按文件各自处理**，不能只对 app.js 生效。
  let c = blankFn(code, 'announceEnd');
  const stripped2 = blankOut(c);
  const lines2 = code.split('\n');
  for (const name of EXPECTED_LETS) {
    const rx = new RegExp('(^|[^A-Za-z0-9_.$])(' + name + ')(?![A-Za-z0-9_$])', 'g');
    let mm;
    while ((mm = rx.exec(stripped2)) !== null) {
      const startAt = mm.index + mm[1].length;
      const before = stripped2.slice(0, startAt).trimEnd();
      const prev = before[before.length - 1] || '';
      const after = (stripped2.slice(startAt + name.length).match(/^\s*(.)/) || [])[1] || '';
      if (prev === '.') continue;                       // obj.name
      if (after === ':' && prev !== '?') continue;      // 对象键（三元真值分支除外！）
      const lineNo = stripped2.slice(0, startAt).split('\n').length;
      bareRefs.push(`${label}:L${lineNo}  ${name}  ← ${(lines2[lineNo - 1] || '').trim().slice(0, 60)}`);
    }
  }
}

// 所有模块用同一套扫法（含 announceEnd 豁免）。app.js 不再单独处理 ——
// 单独处理两次的结果是：函数一搬家，另一处的豁免就失效（D-5 就踩了）。
for (const rel of STATE_SCAN) {
  const p = path.join(WEB, rel);
  if (!fs.existsSync(p)) continue;
  scanForBareRefs(fs.readFileSync(p, 'utf8'), rel);
}
for (const b of bareRefs) console.log('     ' + b);
check(`状态名必须走 S.xxx，不得裸露（发现 ${bareRefs.length} 处）`, bareRefs, []);

// 反向对照：判据本身必须能区分「裸露引用」与「该放过的写法」。
//
// ⚠️ 这里**不依赖真实文件里的具体字符串** —— 第一版写的是
// `src.replace('... ? S.liveSay :', '... ? liveSay :')`，
// 而 liveSay 那批代码在 D-4 搬到了 render.js，于是这条反向对照直接失效（命中 0 处）。
// 教训和 O-11 的源码切片一样：**反向对照要自造样本**，否则它会随代码搬家静默失效。
{
  const SAMPLE = [
    'const a = S.liveSay ? S.liveSay : null;',        // 合法
    'const b = obj.liveSay;',                          // 合法（属性访问）
    'const c = { liveSay: 1 };',                       // 合法（对象键）
    'const d = S.ok ? liveSay : other();',             // ★ 违规（三元真值分支）
    'const e = liveSay;',                              // ★ 违规（裸引用）
  ].join('\n');
  const sb = blankOut(SAMPLE);
  let hit = 0;
  const rx = /(^|[^A-Za-z0-9_.$])(liveSay)(?![A-Za-z0-9_$])/g;
  let q;
  while ((q = rx.exec(sb)) !== null) {
    const st = q.index + q[1].length;
    const bf = sb.slice(0, st).trimEnd();
    const pv = bf[bf.length - 1] || '';
    const af = (sb.slice(st + 7).match(/^\s*(.)/) || [])[1] || '';
    if (pv === '.') continue;
    if (af === ':' && pv !== '?') continue;
    hit++;
  }
  // 期望正好命中 2 处：d 行的三元分支 + e 行的裸引用
  check('反向对照：判据能区分合法写法与裸露引用（应命中 2 处）', hit, 2);
}

// 反向对照：注释与字符串里的同名文字不能被误判
{
  const SAMPLE2 = [
    '// 这里提到 liveSay 只是注释',
    'const s = "liveSay 在字符串里";',
    'const t = `模板里的 liveSay`;',
  ].join('\n');
  const sb2 = blankOut(SAMPLE2);
  const hits2 = (sb2.match(/liveSay/g) || []).length;
  check('反向对照：注释/字符串/模板里的名字被抹掉', hits2, 0);
}

/* ---------- 5) index.html 的引入清单 ---------- */
console.log('\n5) index.html 的脚本引入');
const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
const scripts = [];
const sRe = /<script[^>]*src="([^"]+)"/g;
while ((m = sRe.exec(html)) !== null) scripts.push(m[1]);
checkTrue('index.html 引入了 /static/app.js', scripts.includes('/static/app.js'),
  '实际：' + JSON.stringify(scripts));
checkTrue('index.html 引入了 /static/state.js', scripts.includes('/static/state.js'),
  '实际：' + JSON.stringify(scripts));
checkTrue('index.html 引入了 /static/lib/format.js', scripts.includes('/static/lib/format.js'),
  '实际：' + JSON.stringify(scripts));
checkTrue('index.html 引入了 /static/lib/wake.js', scripts.includes('/static/lib/wake.js'),
  '实际：' + JSON.stringify(scripts));
checkTrue('index.html 引入了 /static/audio.js', scripts.includes('/static/audio.js'),
  '实际：' + JSON.stringify(scripts));
checkTrue('index.html 引入了 /static/render.js', scripts.includes('/static/render.js'),
  '实际：' + JSON.stringify(scripts));
checkTrue('index.html 引入了 /static/archive.js', scripts.includes('/static/archive.js'),
  '实际：' + JSON.stringify(scripts));
checkTrue('index.html 引入了 /static/tts.js', scripts.includes('/static/tts.js'),
  '实际：' + JSON.stringify(scripts));
checkTrue('index.html 引入了 /static/prompter.js', scripts.includes('/static/prompter.js'),
  '实际：' + JSON.stringify(scripts));
// 顺序很重要：app.js 一进来就解构 window.MeetState / MeetFormat / MeetWake
const seq = ['/static/state.js', '/static/lib/format.js', '/static/lib/wake.js',
    '/static/audio.js', '/static/render.js', '/static/archive.js',
    '/static/tts.js', '/static/prompter.js', '/static/app.js'];
{
  const idx = seq.map((x) => scripts.indexOf(x));
  const missing = seq.filter((x, i) => idx[i] < 0);
  const ordered = idx.every((v, i) => i === 0 || (v > idx[i - 1] && v >= 0));
  checkTrue('九个脚本按依赖顺序引入', ordered,
    missing.length ? '缺少：' + missing.join(', ') : `实际位置 ${JSON.stringify(idx)}`);
}
checkTrue('没有 type="module"（本批不用）', !/type="module"/.test(html));

/* ---------- 6) 反向对照：判据本身在做事 ---------- */
console.log('\n6) 反向对照：比对逻辑本身有效');
// 故意从基线里删一条，差集必须非空 —— 用动态长度，免得基线一变这条就假红
const dropOne = EXPECTED_FNS[0];
const broken = EXPECTED_FNS.filter((f) => f !== dropOne);
checkTrue(`人为删掉「${dropOne}」后差集非空（说明比对不是空跑）`,
  broken.length === EXPECTED_FNS.length - 1 && !broken.includes(dropOne),
  `删后 ${broken.length} 条`);

// 再验一次「多出来的能抓到」：假装 app.js 里有个基线没有的函数
{
  const fake = [...new Set([...EXPECTED_FNS, 'someNewFunction'])];
  const extra = fake.filter((f) => !EXPECTED_FNS.includes(f));
  checkTrue('人为加一条后差集非空（多出来的也能抓到）',
    extra.length === 1 && extra[0] === 'someNewFunction');
}

/* ---------- 汇总 ---------- */
console.log('\n' + '='.repeat(40));
if (fails.length) {
  console.log(`通过 ${pass} 项，失败 ${fails.length} 项`);
  for (const f of fails) console.log('  FAIL ' + f);
  process.exit(1);
} else {
  console.log(`通过 ${pass} 项，失败 0 项（共 ${pass} 项）`);
}
