#!/usr/bin/env node
/* 唤醒词识别（web/lib/wake.js）。
 *
 * **为什么单独测这个**：2026-10-21 用户实测「语音助理唤不醒」，
 * 查了半天代码逻辑（模式、闸门、状态全正常），最后发现是**唤醒词表太窄**——
 * 实时流式 ASR 把「语音助理」识别成了「语音助厉」，精确匹配失败。
 *
 * 这类 bug 有三个特点，所以必须用测试锁住：
 *   ① 不报错，只是"没反应"，看起来像功能没做；
 *   ② 实时文本和最终稿不一致，事后看稿子会以为识别是对的，越查越糊涂；
 *   ③ 换个说法（同音字）就又坏一次，靠人工测永远测不全。
 *
 * 边界同样重要：放开容错不能把「语音识别」「语音助听器」这类正常词也当唤醒，
 * 否则开会提到这些词助理就自己跳出来。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const WEB = path.join(__dirname, '..', 'web');
let pass = 0;
const fails = [];
function check(desc, got, want) {
  const a = JSON.stringify(got), b = JSON.stringify(want);
  if (a === b) { pass++; return; }
  fails.push(`${desc}\n      期望 ${b}\n      实际 ${a}`);
}
function checkTrue(desc, cond, detail) {
  if (cond) { pass++; return; }
  fails.push(`${desc}${detail ? '\n      ' + detail : ''}`);
}

function loadWake() {
  const sb = { window: {}, RegExp, Object };
  sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(fs.readFileSync(path.join(WEB, 'lib', 'wake.js'), 'utf8'), sb,
    { filename: 'lib/wake.js', timeout: 5000 });
  return sb.window.MeetWake;
}

/* ---------- 1) 模块形状 ---------- */
console.log('\n1) 模块导出');
{
  const W = loadWake();
  checkTrue('window.MeetWake 存在', !!W);
  checkTrue('wakeDecision 是函数', typeof W.wakeDecision === 'function');
  // 注意：不能用 `instanceof RegExp` —— vm 沙箱里的 RegExp 与外层不是同一个构造器，
  // 这条断言会假红（第一版就这么错过了）。用 toString 判类型。
  check('WAKE_RE 是正则', Object.prototype.toString.call(W.WAKE_RE), '[object RegExp]');
  check('WAKE_ONLY_RE 是正则',
    Object.prototype.toString.call(W.WAKE_ONLY_RE), '[object RegExp]');
}

/* ---------- 2) 唤醒词容错（这次事故的核心） ---------- */
console.log('\n2) 唤醒词：同音近形字必须都认');
{
  const { WAKE_RE } = loadWake();
  // 每一组都是实测/可预期的识别偏差
  const SHOULD_WAKE = [
    ['语音助理', '标准说法'],
    ['语音助厉', '实测出现过的错字（就是这次没唤醒的那句）'],
    ['语音助力', 'lì → 力'],
    ['语音助里', 'lǐ → 里'],
    ['语音助李', 'lǐ → 李'],
    ['语音著理', 'zhù → 著'],
    ['语音著力', 'zhù→著 + lì→力'],
    ['语音助手', '手'],
    ['语音著手', 'zhù→著 + 手'],
    ['助理', '简称'],
    ['语音助理。', '带句号'],
    ['嗯，语音助理。', '带语气词与逗号'],
    ['那语音助理还介绍呢', '句中包含'],
  ];
  for (const [s, why] of SHOULD_WAKE) {
    checkTrue(`认得出「${s}」（${why}）`, WAKE_RE.test(s));
  }

  // 反向对照：名字类称呼**故意不认**。唤醒词只该是产品通用词，
  // 不该包含任何个人称呼 —— 否则每个人的昵称都要改一份源码才能用。
  // （这里用一个明显虚构的称呼验证「任意名字都不匹配」，不举任何真实昵称。）
  const NOT_WAKE = [['小王', '个人称呼不是唤醒词']];
  for (const [s, why] of NOT_WAKE) {
    checkTrue(`不认称呼「${s}」（${why}）`, !WAKE_RE.test(s), '实际匹配了');
  }
}

/* ---------- 3) 不能误唤醒 ---------- */
console.log('\n3) 正常词不能被误唤醒（开会说到这些不该跳出来）');
{
  const { WAKE_RE } = loadWake();
  const SHOULD_NOT = [
    '语音识别', '语音转文字', '语音助听器', '语音输入法', '语音合成',
    '语音播报', '这个语音功能不错', '我们谈谈语音这块',
  ];
  for (const s of SHOULD_NOT) {
    checkTrue(`「${s}」不唤醒`, !WAKE_RE.test(s), '实际匹配了');
  }

  // 已知取舍（不是漏测）：中文没有词边界，「语音助理」出现在长句里会连带唤醒，
  // 例如「语音助理们」这类词。这是**正则匹配的固有代价**，不是本次改造引入的；
  // 真要解决得加停顿检测，但那会漏掉「语音助理你说」这类自然说法，代价更大，暂不动。
  checkTrue('已知取舍：「语音助理们」会误唤醒（记录在案，非常见说法）',
    WAKE_RE.test('语音助理们'));
}

/* ---------- 4) 纯唤醒词判定（决定要不要提词） ---------- */
console.log('\n4) WAKE_ONLY_RE：整句只是唤醒词 → 不提词');
{
  const { WAKE_ONLY_RE } = loadWake();
  const ONLY = ['语音助理', '语音助理。', '嗯，语音助理。', '嗯 语音助理',
    '好的，助理！', '语音助厉。', '喂，语音助理'];
  for (const s of ONLY) {
    checkTrue(`「${s}」算纯唤醒`, WAKE_ONLY_RE.test(s));
  }

  const NOT_ONLY = [
    ['语音助理，帮我算下首付', '唤醒词后面还有实际内容'],
    ['这个提词器好像不管', '完全没有唤醒词'],
    ['语音助厉还介绍', '唤醒词后面还有内容'],
    ['你刚才说语音助理了吗', '把它当话题在讨论'],
  ];
  for (const [s, why] of NOT_ONLY) {
    checkTrue(`「${s}」不算纯唤醒（${why}）`, !WAKE_ONLY_RE.test(s));
  }
}

/* ---------- 5) wakeDecision 仍然按原规则（回归保护） ---------- */
console.log('\n5) wakeDecision 回归保护');
{
  const { wakeDecision } = loadWake();
  const base = { mode: 'strict', byMe: true, called: true, inWindow: false, micMuted: false };
  check('strict + 我喊 + 叫了 → summon', wakeDecision(base), 'summon');
  check('strict + 对方喊 + 叫了 → 不触发',
    wakeDecision({ ...base, byMe: false }), null);
  check('guest + 对方喊 + 叫了 → summon',
    wakeDecision({ ...base, mode: 'guest', byMe: false }), 'summon');
  check('窗口内 + 我 → followup',
    wakeDecision({ ...base, called: false, inWindow: true }), 'followup');
  check('proxy + 对方 + 我闭麦 → proxy',
    wakeDecision({ mode: 'proxy', byMe: false, called: false, inWindow: false, micMuted: true }),
    'proxy');
  check('什么都没发生 → null',
    wakeDecision({ mode: 'strict', byMe: true, called: false, inWindow: false, micMuted: false }),
    null);
}

/* ---------- 6) 反向对照 ---------- */
console.log('\n6) 反向对照：把容错改回精确匹配，第 2 节必须失败');
{
  const OLD_RE = /(语音助理|语音助手|助理)/;   // 修复前的写法
  check('旧写法认不出「语音助厉」', OLD_RE.test('语音助厉'), false);
  check('旧写法认得出「语音助理」', OLD_RE.test('语音助理'), true);
  // 证明第 2 节里确实有旧写法认不出的样本（否则那条测试是空跑）
  const missed = ['语音助厉', '语音助力', '语音著理', '语音著手']
    .filter((s) => !OLD_RE.test(s));
  checkTrue('第 2 节包含旧写法认不出的样本', missed.length >= 3,
    `只找到 ${missed.length} 个：${missed.join(',')}`);
}

/* ---------- 7) app.js 里的用法一致性 ---------- */
console.log('\n7) app.js 确实用的是模块版（不是自己另写一份）');
{
  const app = fs.readFileSync(path.join(WEB, 'app.js'), 'utf8');
  checkTrue('app.js 从 MeetWake 解构 WAKE_RE',
    /const\s*\{[^}]*WAKE_RE[^}]*\}\s*=\s*window\.MeetWake/.test(app));
  checkTrue('app.js 从 MeetWake 解构 WAKE_ONLY_RE',
    /const\s*\{[^}]*WAKE_ONLY_RE[^}]*\}\s*=\s*window\.MeetWake/.test(app));
  checkTrue('app.js 里没有另写一份 WAKE_RE 定义',
    !/const WAKE_RE = /.test(app), '发现局部定义，会和模块版脱钩');
  checkTrue('提词触发处用了 WAKE_ONLY_RE 过滤',
    app.includes('!WAKE_ONLY_RE.test(msg.text)'));
}

/* ---------- 8) 可观测性：没触发也必须留下原因 ---------- */
console.log('\n8) 唤醒探测必须覆盖所有提前返回的分支');
{
  const app = fs.readFileSync(path.join(WEB, 'app.js'), 'utf8');
  const start = app.indexOf('function maybeWake(');
  const end = app.indexOf('\n  function handleMessage(');
  const body = start >= 0 && end > start ? app.slice(start, end) : '';
  checkTrue('找到 maybeWake 函数体', body.length > 200);

  // 2026-10-22 教训：上一版把「复选框没勾」的判断放在 report() 之前，
  // 于是这种情况一条日志都没有 → 看起来像"功能完全没反应"，最难查。
  const checkedIdx = body.indexOf('if (!checked)');
  const reportIdx = body.indexOf('const report =');
  checkTrue('report() 定义在 checked 判断之前（否则没勾时无任何上报）',
    reportIdx >= 0 && reportIdx < checkedIdx,
    `report@${reportIdx} checked@${checkedIdx}`);

  // 每个 return 之前都该有 report(...)
  const returns = body.split('\n').filter((l) => /^\s*return;\s*$/.test(l)).length;
  const reports = (body.match(/report\(/g) || []).length - 1; // 减去定义那一处
  checkTrue(`每个 return 前都有 report（return ${returns} 处，report 调用 ${reports} 次）`,
    reports >= returns, 'report 调用次数不足');

  checkTrue('没勾模式时会提示用户（不能静默）', body.includes('语音助理模式没打开'));
  checkTrue('会话开始就上报一次初始状态', app.includes("text: '（会话开始）'"));
}

/* ---------- 9) wakeBusy 不能泄漏 ---------- */
console.log('\n9) wakeBusy 必须在 finally 里复位（否则一次异常永久失效）');
{
  const tts = fs.readFileSync(path.join(WEB, 'tts.js'), 'utf8');
  const s = tts.indexOf('function sayDraft(');
  const body = s >= 0 ? tts.slice(s, s + 2600) : '';
  checkTrue('找到 sayDraft', body.length > 200);

  // 关键：置位后必须**立刻**进入 try，中间不能有别的话。
  // 原写法是 置位 → setSayState → liveSayThinking → try，
  // 那两句抛错就跳过 finally，wakeBusy 永久为 true → 此后永不唤醒。
  const setIdx = body.indexOf('S.wakeBusy = true;');
  const tryIdx = body.indexOf('try {', setIdx);
  checkTrue('置位后有 try', setIdx >= 0 && tryIdx > setIdx);
  const between = body.slice(setIdx + 'S.wakeBusy = true;'.length, tryIdx);
  const realCode = between.split('\n')
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('//') && !l.startsWith('*') && !l.startsWith('/*'))
    .join('');
  check('置位与 try 之间没有可抛错的语句', realCode, '');

  checkTrue('wakeBusy 在 finally 里复位', /finally\s*\{[^}]*S\.wakeBusy = false/.test(tts));
  // 反向对照：旧写法（置位后夹着 setSayState）必须被判为「中间有代码」
  const OLD = 'S.wakeBusy = true;\n    setSayState(x);\n    liveSayThinking();\n    try {';
  const oldBetween = OLD.slice('S.wakeBusy = true;'.length, OLD.indexOf('try {'));
  checkTrue('反向对照：旧写法能被判出中间有代码',
    oldBetween.split('\n').map((l) => l.trim()).filter(Boolean).join('') !== '');
}

/* ---------- 汇总 ---------- */
console.log('\n' + '='.repeat(40));
if (fails.length) {
  console.log(`通过 ${pass} 项，失败 ${fails.length} 项`);
  for (const f of fails) console.log('  FAIL ' + f);
  process.exit(1);
}
console.log(`通过 ${pass} 项，失败 0 项（共 ${pass} 项）`);
