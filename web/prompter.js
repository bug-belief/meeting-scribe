/* 提词器：会中实时给出「下一句该说什么」（O-12 D-5）。
 *
 * 它不是「把整段话写给你」，而是**跟着对话往下走**：
 *   每收到一句新台词，等用户停顿 PROMPT_DEBOUNCE_MS（1.2s）再请求，
 *   避免说话中途疯狂打接口；服务端拿最近几轮上下文生成 1~3 条提示。
 *
 * 三条体验规则，全部写在这里：
 *   ① **用户手动往上翻就别拉回**（promptUserScrollAt + PROMPT_FOLLOW_PAUSE_MS）
 *   ② **请求防重**（promptBusy）：上一轮没回来不发新的，否则会看到旧提示闪回
 *   ③ **按 kind 分色**（接 / 问 / 核）：一眼能看出这条是「该说出去的话」「该问对方」
 *      「先自己核实」。kind 由服务端给（模型按 [接]/[问]/[核] 自打标签），
 *      前端不猜——猜错标签比不标更误导（2026-10-28：答案被顶成「核」）。
 *
 * 依赖：window.MeetApp（toast）、window.MeetRender（nearBottom、PROMPT_FOLLOW_PAUSE_MS）。
 */
(() => {
  const S = window.MeetState;
  const { nearBottom, PROMPT_FOLLOW_PAUSE_MS } = window.MeetRender;
  const byId = (id) => document.getElementById(id);

  const promptEl = byId('prompt');
  const promptBodyEl = byId('promptBody');
  const promptMetaEl = byId('promptMeta');
  const promptMeterEl = byId('promptMeter');
  const promptMeterBarEl = byId('promptMeterBar');
  const promptOnEl = byId('promptOn');
  const promptAllEl = byId('promptAll');
  const promptPhoneticEl = byId('promptPhonetic');
  const promptSayEl = byId('promptSay');
  const promptAgainEl = byId('promptAgain');
  const kbScenarioEl = byId('kbScenario');

  // 用户停顿多久才去要提示。太短会在说话中途反复打接口，太长又跟不上节奏。
  //
  // 2026-10-22 从 1200ms 降到 450ms：原来其实是「每句话说完后再等 1.2 秒」，
  // 而这句台词在服务端**已经定稿了**（VAD 判过静音才会出 line），
  // 再等 1.2 秒是纯延迟。实测端到端 提词 ≈ 1.2s(等) + 1.0s(模型) = 2.2s，
  // 用户感受就是"说完两秒才出提示"。现在只留一个很小的合并窗口，
  // 用来把「一句话被切成的两行」并成一次请求，不是等满一秒。
  const PROMPT_DEBOUNCE_MS = 450;

  // 两次请求的最小间隔：防止对方连着说时把接口打爆（rpm 有限）。
  // 取 700ms 是因为模型本身要 ~1s，请求还没回来时新来的会被合并，不会真的排队。
  const PROMPT_MIN_INTERVAL_MS = 700;

  // 历史保留多少轮。太少看不到前文，太多会把面板撑爆。
  const PROMPT_KEEP_ROUNDS = 12;

  // 提示类型 → 面板上的单字标记与配色含义
  const PROMPT_KIND_LABEL = { say: '接', ask: '问', check: '核' };

  /* ---- 轮次历史（模块私有） ----
   * 为什么必须有：原来 renderPrompt 每次 `textContent=''` 整块替换，
   * 于是**只能看到最新一轮**，前面的问答全丢 —— 用户在会中想回看
   * 「刚才他问的是什么、我当时该怎么说」时无从查起。
   * 现在每轮记成 { q: 触发它的那句话, items: [...], at: 秒数 }，
   * 面板按时间顺序往下排，可滚动回看。
   */
  let rounds = [];
  let lastFetchAt = 0;
  let lastTrigger = '';

  /* ---- 跨模块回调（运行时解析） ---- */
  function toast(msg, ms) { return window.MeetApp.toast(msg, ms); }

  function promptOn() {
    return !!promptOnEl && promptOnEl.checked;
  }

  function promptVisible() {
    return !!promptEl && !promptEl.hidden;
  }

  function setPromptVisible(on) {
    if (!promptEl) return;
    promptEl.hidden = !on;
    if (on) {
      renderPrompt(S.promptItems, true);
      // 刚显示时 clientHeight 才是真值（hidden 时所有尺寸都是 0）
      requestAnimationFrame(syncPromptMeter);
    }
  }

  /* 提词区滚动进度条（2026-10-23）。
   *
   * 为什么需要它：桌面 Chrome / Linux 默认是 overlay 滚动条，鼠标不悬停时宽度为 0，
   * 再叠上 scrollbar-width:thin 基本等于没有。用户看到的是「提示多了但完全没有滚动条」
   * —— 其实是**能滚的**，只是没人告诉他。所以改成自绘一条 3px 进度条：
   *   条高 = 可见比例，起点位置 = 当前滚动位置。
   * 判据一律用实测尺寸（scrollHeight vs clientHeight），不用「超过几条」这种近似：
   * 字号、英文注音、长句换行都会改变实际高度。
   */
  function syncPromptMeter() {
    if (!promptMeterEl || !promptMeterBarEl || !promptBodyEl) return;
    const total = promptBodyEl.scrollHeight;
    const view = promptBodyEl.clientHeight;
    const over = total - view;

    // 末尾遮罩（2026-10-25）：只在「下面还有、且还没滚到底」时淡出，
    // 滚到底就撤掉 —— 否则最后一行永远是半透明的，读不清。
    // 用 class 而不是内联 mask：CSS 里已有默认值，JS 只负责开关。
    const atBottom = promptBodyEl.scrollTop + view >= total - 2;
    promptBodyEl.classList.toggle('more', over > 2 && view > 0 && !atBottom);

    if (over <= 2 || view <= 0) { promptMeterEl.hidden = true; return; }
    promptMeterEl.hidden = false;
    const ratio = Math.min(1, view / total);
    const top = Math.max(0, Math.min(1 - ratio, promptBodyEl.scrollTop / total));
    promptMeterBarEl.style.height = (ratio * 100).toFixed(1) + '%';
    promptMeterBarEl.style.marginTop = (top * 100).toFixed(1) + '%';
  }

  function promptText(item) {
    const wantPhonetic = !!(promptPhoneticEl && promptPhoneticEl.checked);
    return (wantPhonetic && item.phonetic) ? item.phonetic : item.text;
  }

  function kbScenario() {
    return (kbScenarioEl && kbScenarioEl.value) || '';
  }

  async function loadKbScenarios() {
    if (!kbScenarioEl) return;
    try {
      const res = await fetch('/api/kb/scenarios', { credentials: 'same-origin' });
      const data = await res.json();
      if (!data || !data.ok || !Array.isArray(data.scenarios)) return;
      const saved = localStorage.getItem('wb.kbScenario') || '';
      kbScenarioEl.textContent = '';
      for (const s of data.scenarios) {
        const opt = document.createElement('option');
        opt.value = s.name;
        opt.textContent = s.label || s.name;
        if (s.desc) opt.title = s.desc;
        kbScenarioEl.appendChild(opt);
      }
      const want = saved && Array.from(kbScenarioEl.options).some((o) => o.value === saved)
        ? saved : (data.current || data.default || '');
      kbScenarioEl.value = want;
    } catch (e) {
      // 场景清单拿不到不影响用：留空＝服务端按默认档处理
    }
  }

  function renderPrompt(items, keepScroll) {
    if (!promptBodyEl) return;
    const follow = nearBottom(promptBodyEl)
      && Date.now() - S.promptUserScrollAt > PROMPT_FOLLOW_PAUSE_MS;
    promptBodyEl.textContent = '';

    if (!rounds.length) {
      const empty = document.createElement('div');
      empty.className = 'pempty';
      // 空状态必须说清「为什么空」。原来一句「等这句说完，就给提示」
      // 在"永远不会有提示"的场景下是**骗人**的：
      // 用户会一直等，还以为它在憋着。2026-10-22 就因为这个多绕了一圈。
      empty.textContent = S.promptWhy || '等对方说完一句，就给提示。';
      promptBodyEl.appendChild(empty);
      if (promptSayEl) promptSayEl.disabled = true;
      syncPromptMeter();
      return;
    }

    for (const round of rounds) {
      const box = document.createElement('div');
      box.className = 'pround';

      // 「他在问什么」：没有这一行，回看时只有孤零零的几句提示，看不懂上下文
      if (round.q) {
        const q = document.createElement('div');
        q.className = 'pq';
        q.textContent = (round.at ? round.at + ' · ' : '') + round.q;
        q.title = round.q;   // 长句被截断时鼠标悬停看全文
        box.appendChild(q);
      }

      for (const item of round.items) {
        const row = document.createElement('div');
        row.className = 'pitem ' + (item.kind || 'say');
        const tag = document.createElement('span');
        tag.className = 'pk';
        tag.textContent = PROMPT_KIND_LABEL[item.kind] || '接';
        const text = document.createElement('span');
        text.className = 'pt';
        text.textContent = promptText(item);
        row.append(tag, text);
        box.appendChild(row);
      }
      promptBodyEl.appendChild(box);
    }

    if (promptSayEl) promptSayEl.disabled = false;
    if (!keepScroll && follow) promptBodyEl.scrollTop = promptBodyEl.scrollHeight;
    syncPromptMeter();
  }

  async function promptFetch(manual) {
    if (!promptOn() || !S.liveSessionId) return;
    if (S.promptBusy) {
      // 上一轮还没回来：记一笔，等它结束后立刻补一次。
      // 原来直接 return，导致「说话快的时候连续几句都拿不到提示」。
      S.promptQueued = true;
      return;
    }
    S.promptBusy = true;
    // 请求发出时的触发句与时刻，绑在这一轮上（不是响应回来时才取，
    // 否则会被后一句覆盖，历史里就串行了）
    const trigger = manual ? '（手动再想一次）' : lastTrigger;
    const atSec = S.sentSamples ? Math.floor(S.sentSamples / 16000) : 0;
    if (promptEl) promptEl.classList.add('busy');
    if (promptAgainEl) promptAgainEl.disabled = true;
    try {
      const res = await fetch('/api/prompter/suggest', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: S.liveSessionId, scenario: kbScenario() || 'meeting' }),
      });
      const data = await res.json().catch(() => null);
      if (data && data.ok) {
        const next = Array.isArray(data.items) ? data.items : [];
        if (next.length || manual) {
          S.promptItems = next;
          if (next.length) {
            setPromptVisible(true);
            // 追加一轮（同一条触发句不重复追加：手动"再想一次"时更新末尾那轮）
            const sameAsLast = rounds.length
              && rounds[rounds.length - 1].q === trigger && trigger;
            if (sameAsLast) {
              rounds[rounds.length - 1].items = next;
            } else {
              rounds.push({ q: trigger, items: next, at: fmtSec(atSec) });
              if (rounds.length > PROMPT_KEEP_ROUNDS) {
                rounds = rounds.slice(-PROMPT_KEEP_ROUNDS);
              }
            }
            renderPrompt(S.promptItems);
          }
        }
        if (promptMetaEl) {
          promptMetaEl.textContent = data.elapsed_ms
            ? (data.elapsed_ms / 1000).toFixed(1) + 's' : '';
        }
      }
    } catch (e) {
      // 提词是辅助：失败就静默，不弹 toast 打断对话
    } finally {
      S.promptBusy = false;
      lastFetchAt = Date.now();
      if (promptEl) promptEl.classList.remove('busy');
      if (promptAgainEl) promptAgainEl.disabled = false;
      // 期间有新台词：立刻补一次（不排队、不叠加，最多一次）
      if (S.promptQueued) {
        S.promptQueued = false;
        promptFetch(false);
      }
    }
  }

  // 秒数 → mm:ss（面板上标这一轮是什么时候的，方便回看对应录音位置）
  function fmtSec(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
  }

  /* 什么时候去要提示。
   *
   * 2026-10-22 改造（原来是一条 1.2s 的尾部防抖，等于白等一秒多）：
   *   · 台词在服务端**已经定稿**了，不需要再等它"说完"
   *   · 只保留 PROMPT_DEBOUNCE_MS(450ms) 用来合并"一句话被切成的两行"
   *   · 再加 PROMPT_MIN_INTERVAL_MS(700ms) 防止连着说时打爆接口
   *   · 正在请求中来的新台词 → 排队，回来后立刻补一次
   * 效果：端到端从 ~2.2s 降到 ~1.4s（模型 1s + 合并窗口 0.45s）。
   */
  function promptSchedule(trigger) {
    if (!promptOn() || S.state !== 'recording') return;
    if (trigger) lastTrigger = String(trigger).trim();
    const since = Date.now() - lastFetchAt;
    clearTimeout(S.promptTimer);
    if (S.promptBusy) {
      S.promptQueued = true;
      return;
    }
    if (since >= PROMPT_MIN_INTERVAL_MS) {
      promptFetch(false);
      return;
    }
    S.promptTimer = setTimeout(
      () => promptFetch(false),
      Math.max(PROMPT_DEBOUNCE_MS, PROMPT_MIN_INTERVAL_MS - since),
    );
  }

  function promptReset(hide) {
    clearTimeout(S.promptTimer);
    S.promptTimer = 0;
    S.promptItems = [];
    S.promptBusy = false;
    S.promptQueued = false;
    // 每场录音重新判断「分轨到底有没有分出对方」，不能沿用上一场的结论
    S.peerSpoke = false;
    S.soloLines = 0;
    S.promptWhy = '';
    // ⚠️ 注意**不清** S.promptAll：那是用户按场景定的偏好（并已持久化），
    // 不属于这一场录音的状态。清掉的话每开一次录音都要重新勾，很烦。
    rounds = [];
    lastFetchAt = 0;
    lastTrigger = '';
    if (promptBodyEl) promptBodyEl.textContent = '';
    if (promptMetaEl) promptMetaEl.textContent = '';
    if (promptSayEl) promptSayEl.disabled = true;
    if (promptAgainEl) promptAgainEl.disabled = true;
    if (promptEl) promptEl.classList.remove('busy');
    if (hide) setPromptVisible(false);
  }

  /* 这条定稿行要不要拿去提词。返回 peer / solo / skip。
   *
   * 三个决定因素（按优先级）：
   *   ① **用户开了「全听」** → 一律 peer。这是显式意图，压过其它判断。
   *   ② 说话人是对方（含未标注）→ peer。
   *   ③ 分轨没分出对方（本场从未出现「对方」，且我已经连着说了 2 句）
   *      → solo，退化成"所有台词都提词"。
   *
   * 为什么要有 ③：实测踩过 —— `peer_channel_alive()` 阈值只有 0.003，
   * 开「双路分轨」时右声道有底噪就判 alive=True，于是我说的话全被标成「我」，
   * 提词条件恒假、一行都不触发，而且**完全静默**。
   * 保底降级能救这种情况，但不该替代 ①（用户主动开全听才是正解）。
   */
  function promptSourceNote(who, isSelf) {
    if (S.promptAll) return 'peer';
    if (!who || !isSelf(who)) {
      S.peerSpoke = true;
      S.soloLines = 0;
      return 'peer';
    }
    S.soloLines += 1;
    if (!S.peerSpoke && S.soloLines >= 2) {
      if (!S.promptWhy) {
        S.promptWhy = '分轨没分出「对方」，先按你的话给提示';
        console.info('[提词] 已降级：本会话未出现「对方」，改为所有台词都提词');
      }
      return 'solo';
    }
    return 'skip';
  }

  /* 「全听」开关：改变的是监听范围，不是提词器总开关。
   * 总开关是设置区里的 promptOn（关掉就完全不请求）。 */
  function promptAllOn() {
    return !!S.promptAll;
  }

  function setPromptAll(on, persist) {
    S.promptAll = !!on;
    if (promptAllEl) promptAllEl.checked = S.promptAll;
    if (promptEl) promptEl.classList.toggle('all', S.promptAll);
    // 切换时要重新判断空状态：从"只听对方"切到"全听"，
    // 原来那句「等对方说完」就不成立了，得换成能反映现状的说明。
    if (S.promptAll) {
      S.promptWhy = '';
    } else if (!S.peerSpoke) {
      S.promptWhy = '';
    }
    if (!rounds.length) renderPrompt([], true);
    if (persist) {
      try { localStorage.setItem('wb.promptAll', S.promptAll ? '1' : '0'); } catch (e) {}
    }
  }
  /* 滚动时刷新进度条与末尾遮罩（2026-10-25 补）。
   *
   * 原来只有 render / 显示 / 收到新提示时才调 syncPromptMeter，
   * **滚动时完全不调用** —— 于是：进度条停在原位不动，用户滚到底了遮罩还在。
   * 这是个一直存在但没人报的 bug：进度条本来就细，不动也容易被当成「就长这样」。
   * 绑定放在模块加载时（DOM 已就绪，本文件在 body 末尾引入）。
   * passive:true —— 滚动监听不该阻塞主线程合成。 */
  if (promptBodyEl) {
    promptBodyEl.addEventListener('scroll', syncPromptMeter, { passive: true });
  }

  window.MeetPrompter = {
    promptOn,
    promptVisible,
    setPromptVisible,
    promptText,
    kbScenario,
    loadKbScenarios,
    renderPrompt,
    promptFetch,
    promptSchedule,
    promptReset,
    promptSourceNote,
    promptAllOn,
    setPromptAll,
    syncPromptMeter,
    PROMPT_DEBOUNCE_MS,
    PROMPT_KIND_LABEL,
  };
})();
