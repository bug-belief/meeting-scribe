/* 纯格式化与说话人标签（O-12 D-2）。
 *
 * 为什么单独拆出来：
 *   这些函数不碰共享状态、不碰 DOM、没有副作用 —— 全是「给一个值，返回一个值」。
 *   放在 2,100 行的 app.js 里，既没法单独测，也让人不敢动（改格式化函数怕连带）。
 *   拆出来之后可以用 node 直接 import 断言，不再需要「从源码里抽函数执行」那套。
 *
 * 零构建约定（本项目不引入 ES module）：
 *   每个文件是一个 IIFE，往 window 挂一个对象；index.html 按依赖顺序引入。
 *   app.js 里用 `const { fmtClock } = window.MeetFormat` 取用，
 *   所以**调用点一个都不用改**——这是拆分风险最低的做法。
 */
(() => {
  const SPK_SLOTS = 8;

  // 名字 → 颜色槽位（'spk-3'）。这是**模块私有状态**：全应用只需要一份，
  // 放在这里比挂在 S 上更合适——它不属于「会话状态」，不需要 reset。
  // 规则：同一个名字永远同色；不同名字尽量不撞色（8 槽用满才允许复用）。
  const spkAssign = new Map();

  function fmtClock(ms) {
    const s = Math.floor(ms / 1000);
    const h = String(Math.floor(s / 3600)).padStart(2, '0');
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const ss = String(s % 60).padStart(2, '0');
    return `${h}:${m}:${ss}`;
  }

  function fmtTs(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }

  function fmtMoney(value) {
    const v = Number(value) || 0;
    if (v === 0) return '0';
    if (v < 0.01) return v.toFixed(4);
    return v.toFixed(3);
  }

  // FNV-1a：短字符串散列够散、实现够短。用来给说话人分配颜色，
  // 不要求密码学强度，只要求「同名同色、异名大概率异色」。
  function spkHash(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619) >>> 0;
    }
    return h >>> 0;
  }

  function spkName(who) {
    return String(who || '').replace(/[：:]\s*$/, '').trim();
  }

  function isAssistantName(name) {
    return /^助理/.test(spkName(name));
  }

  function spkClass(who) {
    const name = spkName(who);
    if (!name) return '';
    if (isAssistantName(name)) return 'spk-asst';
    if (spkAssign.has(name)) return spkAssign.get(name);
    const used = new Set(spkAssign.values());
    let slot = (spkHash(name) % SPK_SLOTS) + 1;
    for (let i = 0; i < SPK_SLOTS; i++) {
      const cls = 'spk-' + slot;
      if (!used.has(cls)) { spkAssign.set(name, cls); return cls; }
      slot = (slot % SPK_SLOTS) + 1;
    }
    const cls = 'spk-' + ((spkHash(name) % SPK_SLOTS) + 1);
    spkAssign.set(name, cls);
    return cls;
  }

  function escHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  window.MeetFormat = {
    SPK_SLOTS,
    fmtClock,
    fmtTs,
    fmtMoney,
    spkHash,
    spkName,
    isAssistantName,
    spkClass,
    escHtml,
  };
})();
