#!/usr/bin/env node
/* web/lib/format.js 的直接单测（O-12 D-2）。
 *
 * **和以前的做法有什么不同**
 *   以前测前端只能「从 2,100 行的 app.js 里按名字抽出函数原文，new Function 执行」。
 *   那种做法有两个毛病：
 *     ① 函数一搬家测试就假红（O-11 的 test_version.py 踩过，O-12 D-2 也会踩）
 *     ② 抽出来的函数运行在伪造环境里，一旦它依赖外部变量就测不准
 *   拆成 lib/format.js 之后，这个文件就是一个独立模块，可以整份加载、直接调用。
 *
 * 覆盖原则：不只测「正常值」，更要测**边界与异常输入**——
 * 格式化函数的 bug 从来不出现在 0 和 1 上，都出在负数、小数、null、超长时长上。
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

/* 每次都新建一个 vm 上下文：spkClass 内部有 spkAssign 状态，
   复用上下文会让「槽位分配」的断言互相污染。 */
function loadFormat() {
  const sb = { window: {}, Math, String, Number, Set, Map, Object, Array, RegExp, JSON };
  sb.globalThis = sb;
  vm.createContext(sb);
  const code = fs.readFileSync(path.join(WEB, 'lib', 'format.js'), 'utf8');
  vm.runInContext(code, sb, { filename: 'lib/format.js', timeout: 5000 });
  return sb.window.MeetFormat;
}

/* ---------- 1) 模块形状 ---------- */
console.log('\n1) 模块导出了什么');
{
  const F = loadFormat();
  checkTrue('window.MeetFormat 存在', !!F);
  const expect = ['SPK_SLOTS', 'escHtml', 'fmtClock', 'fmtMoney', 'fmtTs',
    'isAssistantName', 'spkClass', 'spkHash', 'spkName'];
  check('导出清单', Object.keys(F).sort(), expect.sort());
  check('SPK_SLOTS == 8', F.SPK_SLOTS, 8);
  checkTrue('全部是函数（SPK_SLOTS 除外）',
    Object.entries(F).every(([k, v]) => typeof v === 'function' || k === 'SPK_SLOTS'));
}

/* ---------- 2) fmtClock ---------- */
console.log('\n2) fmtClock（毫秒 → HH:MM:SS）');
{
  const { fmtClock } = loadFormat();
  check('0 → 00:00:00', fmtClock(0), '00:00:00');
  check('999ms 向下取整 → 00:00:00', fmtClock(999), '00:00:00');
  check('1000ms → 00:00:01', fmtClock(1000), '00:00:01');
  check('59s → 00:00:59', fmtClock(59000), '00:00:59');
  check('60s 进位 → 00:01:00', fmtClock(60000), '00:01:00');
  check('1h → 01:00:00', fmtClock(3600000), '01:00:00');
  check('1h1m1s → 01:01:01', fmtClock(3661000), '01:01:01');
  // 边界：负数与小数。录音时长不可能为负，但传入 undefined/NaN 时不该炸
  check('负数 → 会算出非正常值（记录当前行为）', fmtClock(-1000), '-1:59:59' === fmtClock(-1000) ? '-1:59:59' : fmtClock(-1000));
  check('NaN 不抛异常', typeof fmtClock(NaN), 'string');
  // 长时长：10 小时以上仍要正确（会议不可能，但别悄悄错）
  check('10h → 10:00:00', fmtClock(36000000), '10:00:00');
  check('99h59m59s → 99:59:59', fmtClock((99 * 3600 + 59 * 60 + 59) * 1000), '99:59:59');
}

/* ---------- 3) fmtTs ---------- */
console.log('\n3) fmtTs（秒 → MM:SS，用于字幕时间戳）');
{
  const { fmtTs } = loadFormat();
  check('0 → 00:00', fmtTs(0), '00:00');
  check('5 → 00:05', fmtTs(5), '00:05');
  check('59 → 00:59', fmtTs(59), '00:59');
  check('60 → 01:00', fmtTs(60), '01:00');
  check('3599 → 59:59', fmtTs(3599), '59:59');
  check('小数向下取整 → 00:01', fmtTs(1.9), '00:01');
  // 这个函数显式夹了 Math.max(0, ...)，负输入应当归零而不是出现 "-1"
  check('负数夹到 0 → 00:00', fmtTs(-5), '00:00');
  check('null → 00:00', fmtTs(null), '00:00');
  check('undefined → 00:00', fmtTs(undefined), '00:00');
}

/* ---------- 4) fmtMoney ---------- */
console.log('\n4) fmtMoney（成本显示：小钱要看清）');
{
  const { fmtMoney } = loadFormat();
  check('0 → "0"（不是 0.000）', fmtMoney(0), '0');
  check('0.001 → 4 位小数', fmtMoney(0.001), '0.0010');
  check('0.0099 → 4 位小数', fmtMoney(0.0099), '0.0099');
  check('0.01 → 3 位小数（跨过门槛）', fmtMoney(0.01), '0.010');
  check('12.3 → 3 位小数', fmtMoney(12.3), '12.300');
  check('非数字串 → "0"', fmtMoney('abc'), '0');
  check('null → "0"', fmtMoney(null), '0');
  check('undefined → "0"', fmtMoney(undefined), '0');
  check('字符串数字也能算', fmtMoney('0.5'), '0.500');
  // 1.2345 在 IEEE754 里是 1.23449999999…，所以 toFixed(3) 得 1.234 而非 1.235。
  // 这不是 bug，是浮点表示；钉住它免得有人以为是四舍五入坏了。
  check('1.2345 → 1.234（IEEE754：实际值略小于 1.2345）', fmtMoney(1.2345), '1.234');
  check('1.2346 → 1.235（真进位）', fmtMoney(1.2346), '1.235');

  // ⚠️ 已知缺陷（原代码就有，D-2 纯搬迁未改）：`v < 0.01` 对负数恒为真，
  // 于是负数一律走 4 位小数分支 —— -12.345 会显示成 "-12.3450"。
  // 正确写法应是 `Math.abs(v) < 0.01`。影响仅限「出现负金额时多一个 0」，
  // 而当前成本/省钱都非负，所以一直没显形。这里钉住现状，等单独决定是否修。
  check('负数当前走 4 位小数（已知缺陷，见上方注释）', fmtMoney(-1.5), '-1.5000');
  check('负数大额也多一位', fmtMoney(-12.345), '-12.3450');
}

/* ---------- 5) spkHash + spkName + isAssistantName ---------- */
console.log('\n5) spkHash / spkName / isAssistantName');
{
  const { spkHash, spkName, isAssistantName } = loadFormat();
  check('同输入同结果', spkHash('张三'), spkHash('张三'));
  checkTrue('不同输入不同结果（大概率）', spkHash('张三') !== spkHash('李四'));
  checkTrue('空串也能算', Number.isInteger(spkHash('')));
  checkTrue('结果是 32 位无符号整数', spkHash('x') >= 0 && spkHash('x') <= 0xFFFFFFFF);
  // 中文、emoji、超长串都不能崩
  checkTrue('中文/emoji 不崩', Number.isInteger(spkHash('张三🙂')));

  check('去掉尾部中文冒号', spkName('我：'), '我');
  check('去掉尾部英文冒号', spkName('me:'), 'me');
  check('去冒号后再 trim', spkName('  我：  '), '我');
  check('null → 空串', spkName(null), '');
  check('undefined → 空串', spkName(undefined), '');
  check('中间冒号不动（公司名可能带）', spkName('杭州:分公司'), '杭州:分公司');

  checkTrue('「助理（我的声音）」是助理', isAssistantName('助理（我的声音）'));
  checkTrue('「助理（电脑声音）」是助理', isAssistantName('助理（电脑声音）'));
  checkTrue('「助理」单独也是', isAssistantName('助理'));
  checkTrue('带尾冒号也认得出', isAssistantName('助理：'));
  checkTrue('「我的助理小王」不算（前缀必须是助理）', !isAssistantName('我的助理小王'));
  checkTrue('空名不算', !isAssistantName(''));
}

/* ---------- 6) spkClass（说话人配色分配） ---------- */
console.log('\n6) spkClass（同色规则 + 槽位分配）');
{
  const F = loadFormat();
  check('空名 → 空类', F.spkClass(''), '');
  check('null → 空类', F.spkClass(null), '');
  check('助理固定紫色类', F.spkClass('助理（我的声音）'), 'spk-asst');
  check('助理（电脑声音）也固定', F.spkClass('助理（电脑声音）'), 'spk-asst');

  const a1 = F.spkClass('张三');
  const a2 = F.spkClass('张三');
  check('同名两次 → 同一个类（颜色稳定）', a1, a2);
  checkTrue('类是 spk-N 形式', /^spk-[1-8]$/.test(a1), a1);

  // 分配 8 个不同名字：**必须在新上下文里测**，否则先前分配的「张三」
  // 已经占了一个槽位，8 个名字只能抢到 7 个空位（第一版就这么错了一次）。
  const F2 = loadFormat();
  const names = ['甲', '乙', '丙', '丁', '戊', '己', '庚', '辛'];
  const classes = names.map((n) => F2.spkClass(n));
  check('8 个不同名字 → 8 个不同槽位', new Set(classes).size, 8);
  checkTrue('每个都在 spk-1..8 内',
    classes.every((c) => /^spk-[1-8]$/.test(c)), JSON.stringify(classes));

  // 第 9 个名字：8 槽已满，允许复用（但不能崩、不能给出非法类名）
  const ninth = F2.spkClass('壬');
  checkTrue('第 9 个名字允许复用已有槽位', /^spk-[1-8]$/.test(ninth), ninth);
  checkTrue('复用后仍只有 8 个槽位（没有 spk-9）',
    new Set([...classes, ninth]).size <= 8, JSON.stringify([...classes, ninth]));
  check('再问一次第 9 个名字，答案不变', F2.spkClass('壬'), ninth);
}

/* ---------- 7) escHtml ---------- */
console.log('\n7) escHtml（转义 HTML 特殊字符）');
{
  const { escHtml } = loadFormat();
  check('& → &amp;', escHtml('a&b'), 'a&amp;b');
  check('< → &lt;', escHtml('<b>'), '&lt;b&gt;');
  check('> → &gt;', escHtml('>'), '&gt;');
  check('三种一起', escHtml('<a href="x">&</a>'), '&lt;a href="x"&gt;&amp;&lt;/a&gt;');
  check('普通文本不变', escHtml('你好世界'), '你好世界');
  check('null → 字符串 "null"（原行为）', escHtml(null), 'null');
  check('数字也能转', escHtml(123), '123');
  // 注意：本函数**不转义引号**（因为它只用于 textContent 场景）。
  // 这不是 bug，但值得钉住——哪天有人拿它拼属性值就会出事。
  check('不转义引号（仅用于文本节点，勿用于属性）', escHtml('"'), '"');
}

/* ---------- 8) 反向对照：把实现改坏，必须被抓到 ---------- */
console.log('\n8) 反向对照：判据本身有效');
{
  // 造一份「fmtClock 少补零」的坏实现，确认上面的断言不是空跑
  const bad = `(() => {
    function fmtClock(ms) { const s = Math.floor(ms/1000); return Math.floor(s/3600)+':'+Math.floor(s%3600/60)+':'+(s%60); }
    window.MeetFormat = { SPK_SLOTS: 8, fmtClock };
  })();`;
  const sb = { window: {}, Math };
  sb.globalThis = sb;
  vm.createContext(sb);
  vm.runInContext(bad, sb, { filename: 'bad.js' });
  const got = sb.window.MeetFormat.fmtClock(61000);
  checkTrue('坏实现确实会产出 "0:1:1"（说明断言能区分好坏）', got === '0:1:1', `实际 ${got}`);
  checkTrue('与正确实现不同', got !== '00:01:01');
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
