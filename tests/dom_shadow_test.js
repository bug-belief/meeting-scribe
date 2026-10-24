#!/usr/bin/env node
/* DOM id 撞车检测（2026-10-22 实测事故后新增）。
 *
 * **为什么必须有这个测试**：
 * 浏览器有一条老规矩 —— 带 id 的元素会自动成为 window 上的同名全局变量。
 * 于是 `kbScenario()` 这种「忘了跨模块导入」的调用**不会报未定义**，
 * 而是拿到 <select id="kbScenario">，报错信息是：
 *
 *     没接上：kbScenario is not a function
 *
 * 这条错误极其误导：
 *   ① 看起来像"某个函数写错了"，实际是**漏了导入**；
 *   ② 唤醒本身是成功的（日志里明明有"助理已唤醒"），
 *      所以会被误判成"唤醒没生效"——这次就白查了半天。
 *
 * 模块化拆分（O-12）之后这类风险变高了：函数搬到别的文件，
 * 调用点靠解构引入，**漏一个就静默变成 DOM 元素**。
 * 所以把它做成常规检测：任何模块里「调用了本模块没定义、但和某个 DOM id 同名」
 * 的函数，一律报错。
 */
const fs = require('fs');
const path = require('path');

const WEB = path.join(__dirname, '..', 'web');
const FILES = ['app.js', 'audio.js', 'render.js', 'archive.js', 'tts.js', 'prompter.js',
  'lib/format.js', 'lib/wake.js'];

// 语言内置 + 浏览器 API：这些不是"漏导入"
const BUILTIN = new Set(('if for while switch catch return function typeof Number String ' +
  'Math JSON parseInt parseFloat setTimeout setInterval clearTimeout clearInterval fetch ' +
  'requestAnimationFrame cancelAnimationFrame isNaN isFinite encodeURIComponent ' +
  'decodeURIComponent Date Array Object Promise confirm alert Boolean RegExp Error Audio Blob ' +
  'URL localStorage Uint8Array Float32Array Int16Array Map Set WeakMap Symbol Object ' +
  'structuredClone queueMicrotask performance console document navigator WebSocket AudioContext ' +
  'AudioWorkletNode MediaStream Audio ResizeObserver IntersectionObserver CustomEvent Event ' +
  'getComputedStyle requestIdleCallback Notification ClipboardItem Response Request Headers ' +
  'FormData TextDecoder TextEncoder AbortController URLSearchParams Intl Reflect Proxy').split(' '));

let pass = 0;
const fails = [];
function checkTrue(desc, cond, detail) {
  if (cond) { pass++; return; }
  fails.push(`${desc}${detail ? '\n      ' + detail : ''}`);
}

/* ---------- 收集 Dom id ---------- */
const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
const domIds = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));

console.log('\n1) index.html 的 id 清单');
checkTrue('index.html 里能读到 id（至少 30 个）', domIds.size >= 30, `实际 ${domIds.size}`);
checkTrue('含有已知的 kbScenario（否则本测试的靶子不成立）', domIds.has('kbScenario'));

/* ---------- 提取每个模块的声明 ---------- */
function declaredNames(src) {
  const d = new Set();
  for (const m of src.matchAll(/\bfunction\s+([A-Za-z_$][\w$]*)\s*\(/g)) d.add(m[1]);
  for (const m of src.matchAll(/\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[=;,)]/g)) d.add(m[1]);
  // 解构：const { a, b: c } = ...
  for (const m of src.matchAll(/\b(?:const|let|var)\s*\{([^}]*)\}\s*=/g)) {
    for (const part of m[1].split(',')) {
      const name = part.split(':').pop().trim().replace(/=.*$/, '').trim();
      if (/^[A-Za-z_$][\w$]*$/.test(name)) d.add(name);
    }
  }
  // 形参
  for (const m of src.matchAll(/\(([^)]*)\)\s*\{/g)) {
    for (const part of m[1].split(',')) {
      const name = part.trim().replace(/=.*$/, '').trim();
      if (/^[A-Za-z_$][\w$]*$/.test(name)) d.add(name);
    }
  }
  return d;
}

/* ---------- 扫描 ---------- */
console.log('\n2) 每个模块调用的函数，不得是「本模块未定义的同名 DOM id」');
const problems = [];
for (const rel of FILES) {
  const p = path.join(WEB, rel);
  if (!fs.existsSync(p)) { fails.push(`${rel} 不存在`); continue; }
  const src = fs.readFileSync(p, 'utf8');
  const declared = declaredNames(src);

  for (const m of src.matchAll(/(^|[^A-Za-z0-9_$.])([A-Za-z_$][\w$]*)\s*\(/g)) {
    const name = m[2];
    if (declared.has(name) || BUILTIN.has(name)) continue;
    if (!domIds.has(name)) continue;
    const lineNo = src.slice(0, m.index).split('\n').length;
    problems.push(`${rel}:${lineNo}  ${name}()  ← 本模块未定义，但存在同 id 元素`);
  }
}
checkTrue('没有「调用名与 DOM id 撞车」', problems.length === 0,
  problems.join('\n      '));
if (problems.length) problems.forEach((x) => console.log('     ⚠️ ' + x));

/* ---------- 反向对照：判据本身必须能命中 ---------- */
console.log('\n3) 反向对照：造一个撞车的样本，判据必须报出来');
{
  const SAMPLE = 'const x = 1;\nfunction real() {}\nconst y = kbScenario();\n';
  const declared = declaredNames(SAMPLE);
  checkTrue('样本里 kbScenario 未被声明', !declared.has('kbScenario'));
  checkTrue('样本里 real 被声明（正例）', declared.has('real'));
  let hit = 0;
  for (const m of SAMPLE.matchAll(/(^|[^A-Za-z0-9_$.])([A-Za-z_$][\w$]*)\s*\(/g)) {
    const name = m[2];
    if (declared.has(name) || BUILTIN.has(name)) continue;
    if (domIds.has(name)) hit++;
  }
  checkTrue('反向对照：样本能被判为撞车（命中 1 处）', hit === 1, `命中 ${hit}`);
}

/* ---------- tts.js 必须显式代理 kbScenario ---------- */
console.log('\n4) tts.js 里 kbScenario 走的是 prompter 代理');
{
  const tts = fs.readFileSync(path.join(WEB, 'tts.js'), 'utf8');
  checkTrue('tts.js 有 kbScenario 的代理定义',
    /function kbScenario\(\)\s*\{\s*return window\.MeetPrompter\.kbScenario\(\)/.test(tts));
  // 反向对照：把代理删掉，第 2 节的判据就该命中
  const stripped = tts.replace(/function kbScenario\(\)[^\n]*\n/, '');
  const declared = declaredNames(stripped);
  checkTrue('反向对照：删掉代理后 kbScenario 变回未声明', !declared.has('kbScenario'));
}

console.log('\n' + '='.repeat(40));
if (fails.length) {
  console.log(`通过 ${pass} 项，失败 ${fails.length} 项`);
  for (const f of fails) console.log('  FAIL ' + f);
  process.exit(1);
}
console.log(`通过 ${pass} 项，失败 0 项（共 ${pass} 项）`);
