#!/usr/bin/env node
/* 唤起判定单测。
 *
 * 直接从 web/app.js 里把 wakeDecision 抽出来执行——测的是线上真代码，
 * 不是在这里重抄一份规则。抄一份的话，改了 app.js 而忘了改测试，
 * 两边都绿，等于没测（这个项目已经踩过 recv_nowait 那次假绿）。
 *
 * 用法：node tests/wake_decision_test.js
 */
const fs = require('fs');
const path = require('path');

// O-12 会把 wakeDecision 从 app.js 搬到 web/lib/wake.js。
// 原来这里写死只读 app.js——一搬家就抛「app.js 里找不到 wakeDecision」，
// 那是**断实现位置**的典型症状（和 O-11 里 test_version.py 的源码切片一个毛病）。
// 改成按顺序在几个候选文件里找，文件怎么搬都不影响本测试。
const CANDIDATES = ['app.js', 'lib/wake.js'];
const WEB = path.join(__dirname, '..', 'web');
let src = '';
let APP = '';
for (const rel of CANDIDATES) {
  const p = path.join(WEB, rel);
  if (fs.existsSync(p)) {
    const t = fs.readFileSync(p, 'utf8');
    if (t.includes('function wakeDecision(')) { src = t; APP = p; break; }
  }
}
if (!src) {
  console.error('在 ' + CANDIDATES.join(' / ') + ' 里都找不到 wakeDecision，是不是被改名了？');
  process.exit(1);
}

// 按大括号配对取函数体，别用正则——函数里还有嵌套的 {} 和注释里的括号
function extractFn(text, name) {
  const start = text.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(`${APP} 里找不到 ${name}，是不是被改名了？`);
  // 先跳过参数列表：参数是解构 { mode, byMe, ... }，直接找第一个 { 会截在参数上
  let i = text.indexOf('(', start);
  let pDepth = 0;
  for (; i < text.length; i++) {
    if (text[i] === '(') pDepth++;
    else if (text[i] === ')') { pDepth--; if (pDepth === 0) break; }
  }
  let body = text.indexOf('{', i);
  if (body < 0) throw new Error(`${name} 后面没有函数体`);
  let depth = 0;
  for (let j = body; j < text.length; j++) {
    const c = text[j];
    if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) return text.slice(start, j + 1);
    }
  }
  throw new Error(`${name} 的大括号没有闭合`);
}

const fnText = extractFn(src, 'wakeDecision');
const wakeDecision = new Function(`${fnText}; return wakeDecision;`)();

let pass = 0;
const fails = [];
function check(desc, got, want) {
  if (got === want) { pass++; return; }
  fails.push(`${desc}\n      期望 ${JSON.stringify(want)}，实际 ${JSON.stringify(got)}`);
}

const MODES = ['strict', 'guest', 'open', 'proxy'];

// ---------- 1. strict：只有我说的才算 ----------
check('strict｜我喊了 → 唤醒', wakeDecision({ mode: 'strict', byMe: true, called: true, inWindow: false, micMuted: false }), 'summon');
check('strict｜对方喊了 → 不唤醒（本次修复的核心）', wakeDecision({ mode: 'strict', byMe: false, called: true, inWindow: false, micMuted: false }), null);
check('strict｜我窗口内续聊 → 续聊', wakeDecision({ mode: 'strict', byMe: true, called: false, inWindow: true, micMuted: false }), 'followup');
check('strict｜对方窗口内说话 → 不接（旧版会误接）', wakeDecision({ mode: 'strict', byMe: false, called: false, inWindow: true, micMuted: false }), null);
check('strict｜对方说话 + 我闭麦 → 不代班（那是 proxy 的事）', wakeDecision({ mode: 'strict', byMe: false, called: false, inWindow: false, micMuted: true }), null);

// ---------- 2. guest：我或对方，谁喊都认 ----------
check('guest｜我喊了 → 唤醒', wakeDecision({ mode: 'guest', byMe: true, called: true, inWindow: false, micMuted: false }), 'summon');
check('guest｜对方喊了 → 也唤醒', wakeDecision({ mode: 'guest', byMe: false, called: true, inWindow: false, micMuted: false }), 'summon');
check('guest｜对方窗口内说话 → 续聊', wakeDecision({ mode: 'guest', byMe: false, called: false, inWindow: true, micMuted: false }), 'followup');

// ---------- 3. open：不分谁，窗口内一直接 ----------
check('open｜没喊、不在窗口 → 不接', wakeDecision({ mode: 'open', byMe: false, called: false, inWindow: false, micMuted: false }), null);
check('open｜对方窗口内说话 → 续聊', wakeDecision({ mode: 'open', byMe: false, called: false, inWindow: true, micMuted: false }), 'followup');

// ---------- 4. proxy：我闭麦时对方问就代班 ----------
check('proxy｜我方闭麦 + 对方说话 → 代班', wakeDecision({ mode: 'proxy', byMe: false, called: false, inWindow: false, micMuted: true }), 'proxy');
check('proxy｜我方有声 + 对方说话 → 不代班（我在场，别抢话）', wakeDecision({ mode: 'proxy', byMe: false, called: false, inWindow: false, micMuted: false }), null);
check('proxy｜我闭麦时对方喊助理 → 按被点名处理', wakeDecision({ mode: 'proxy', byMe: false, called: true, inWindow: false, micMuted: true }), 'summon');

// ---------- 5. 单声道：who 缺失按「我」处理 ----------
check('单声道｜who 为空、喊了 → 唤醒（否则单声道永远叫不醒）', wakeDecision({ mode: 'strict', byMe: true, called: true, inWindow: false, micMuted: false }), 'summon');

// ---------- 6. 交叉组合：所有档位 × 所有状态都不该崩 ----------
let crossOk = true;
for (const mode of MODES) {
  for (const byMe of [true, false]) {
    for (const called of [true, false]) {
      for (const inWindow of [true, false]) {
        for (const micMuted of [true, false]) {
          const r = wakeDecision({ mode, byMe, called, inWindow, micMuted });
          if (![null, 'summon', 'followup', 'proxy'].includes(r)) crossOk = false;
        }
      }
    }
  }
}
check('交叉组合 64 种返回值都在枚举内', crossOk, true);

// ---------- 7. 反向对照：故意改错规则，测试必须报错 ----------
// 证明这套断言真的会拦人，而不是永远绿。
const broken = new Function(`${fnText.replace('mode === \'strict\' ? byMe : true', 'true')}; return wakeDecision;`)();
const falseGreen = broken({ mode: 'strict', byMe: false, called: true, inWindow: false, micMuted: false }) === 'summon';
check('反向对照：去掉 strict 限制后，测试确实能发现问题', falseGreen, true);

if (fails.length) {
  fails.forEach((f) => console.log('  ✗ ' + f));
}
console.log(`\n通过 ${pass} 项，失败 ${fails.length} 项`);
process.exit(fails.length ? 1 : 0);
