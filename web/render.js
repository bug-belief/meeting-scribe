/* 字幕与结果渲染（O-12 D-4）。
 *
 * 本模块只做一件事：**把数据变成屏幕上的字**。
 *   · 实时字幕：addLine / showPartial / clearPartial / resetLines
 *   · 分身播报行：liveSay*（thinking → draft → progress → end/clear）
 *   · 结果稿：markdown / tintSpeakers / render / showResult
 *   · 计时与滚动：tickClock / recSec / scrollBottom / nearBottom
 *
 * 两条一直影响体验的规则，都写在这里而不是散在各处：
 *   ① 用户手动往上翻时不能强行拉回底部（linesUserScrollAt + PROMPT_FOLLOW_PAUSE_MS）
 *   ② 底噪编出来的字不能上面板（由上层判 quiet，这里只管不显示）
 *
 * 跨模块依赖只有两类，都用「调用时才解析」的代理：
 *   · window.MeetApp    —— toast / setBadge / setIdle（应用级动作）
 *   · window.MeetArchive —— 出稿后要更新的收藏、回放、声纹面板
 * 这样 render.js 可以先加载，不会和 app.js / archive.js 形成加载顺序死结。
 */
(() => {
  const S = window.MeetState;
  const { fmtClock, fmtTs, spkClass, isAssistantName } = window.MeetFormat;
  const byId = (id) => document.getElementById(id);

  const clock = byId('clock');
  const linesEl = byId('lines');
  const hint = byId('hint');
  const doc = byId('doc');
  const result = byId('result');
  const sayVoiceEl = byId('sayVoice');

  // 用户手动滚动后，暂停自动跟底多久（毫秒）。放在这里是因为它只影响渲染行为。
  const PROMPT_FOLLOW_PAUSE_MS = 3000;

  /* ---- 跨模块回调（运行时解析，避免加载顺序死结） ---- */
  function toast(msg, ms) { return window.MeetApp.toast(msg, ms); }
  function setBadge(text, cls) { return window.MeetApp.setBadge(text, cls); }
  function setIdle() { return window.MeetApp.setIdle(); }
  function setPlayback(a, c) { return window.MeetArchive.setPlayback(a, c); }
  function setMeetingId(m) { return window.MeetArchive.setMeetingId(m); }
  function loadSpeakers(n) { return window.MeetArchive.loadSpeakers(n); }

  function resetLines() {
    linesEl.innerHTML = '';
    hint.style.display = 'none';
    S.liveSay = null;
    S.pendingRow = null;
  }

  function assistantLabel() {
    const v = sayVoiceEl ? sayVoiceEl.value : '';
    return String(v).indexOf('sys:') === 0 ? '助理（电脑声音）' : '助理（我的声音）';
  }

  function addLine(text, start, who) {
    const row = document.createElement('div');
    row.className = 'line';
    row.innerHTML = `<div class="ts">${fmtTs(start)}</div><div class="txt"></div>`;
    const box = row.querySelector('.txt');
    if (who) {
      const tag = document.createElement('span');
      tag.className = 'who ' + spkClass(who);
      tag.textContent = who + '：';
      box.appendChild(tag);
    }
    box.appendChild(document.createTextNode(text));
    linesEl.appendChild(row);
    scrollBottom();
    return row;
  }

  function showPartial(text) {
    if (!text) return;
    if (!S.pendingRow) {
      S.pendingRow = document.createElement('div');
      S.pendingRow.className = 'line pending';
      S.pendingRow.innerHTML = '<div class="ts">··</div><div class="txt typing"></div>';
      linesEl.appendChild(S.pendingRow);
    }
    S.pendingRow.querySelector('.txt').textContent = text;
    scrollBottom();
  }

  function clearPartial() {
    if (S.pendingRow) {
      S.pendingRow.remove();
      S.pendingRow = null;
    }
  }

  function scrollBottom() {
    // 往上翻看历史时不抢滚动条：3 秒内不自动跟到底，否则正读一半就被拽回去。
    // 但**遮罩仍要刷新** —— 用户正在看历史时又来了新台词，内容变多、更该提示「下面还有」。
    // 所以这里的 return 只管滚动，不管遮罩。
    const hold = S.linesUserScrollAt && Date.now() - S.linesUserScrollAt < PROMPT_FOLLOW_PAUSE_MS;
    if (!hold) linesEl.scrollTop = linesEl.scrollHeight;
    syncLinesMore();
  }

  function nearBottom(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  /* 转写区末尾渐隐（2026-10-25，与提词面板同一套做法）。
   * 转写区高度是 flex 算出来的，很容易正好切在一条台词中间——
   * 那半行看着像渲染坏了，而不像「下面还有」。所以「还有内容且未到底」时淡出 14px。
   * ⚠️ 只在未到底时加：否则最后一行永远半透明，读不清。
   * ⚠️ 判据用实测几何（scrollHeight/clientHeight），不用「条数」——字号和换行都会变。 */
  function syncLinesMore() {
    if (!linesEl) return;
    const total = linesEl.scrollHeight;
    const view = linesEl.clientHeight;
    const atBottom = linesEl.scrollTop + view >= total - 2;
    linesEl.classList.toggle('more', total - view > 2 && view > 0 && !atBottom);
  }
  if (linesEl) {
    linesEl.addEventListener('scroll', syncLinesMore, { passive: true });
  }

  function liveSayEls() {
    if (!S.liveSay) return null;
    return { said: S.liveSay.row.querySelector('.said'),
             ahead: S.liveSay.row.querySelector('.ahead'),
             tag: S.liveSay.row.querySelector('.atag') };
  }

  function liveSayNew() {
    // 不删上一条：对话历史要留着，能往上翻看助理刚才说过什么
    const row = document.createElement('div');
    row.className = 'line assistant';
    row.innerHTML = '<div class="ts">' + fmtTs(S.sentSamples / 16000) + '</div>'
      + '<div class="txt"><span class="aibadge">AI</span>'
      + '<span class="who asst spk-asst">' + assistantLabel() + '：</span>'
      + '<span class="said"></span><span class="ahead"></span>'
      + '<span class="atag"></span></div>';
    linesEl.appendChild(row);
    S.liveSay = { row, text: '', spoken: 0, base: 0, state: 'draft' };
    scrollBottom();
    return S.liveSay;
  }

  function liveSayThinking() {
    const s = liveSayNew();
    s.state = 'thinking';
    s.row.classList.add('thinking');
    s.row.querySelector('.ahead').textContent = '正在想…';
    scrollBottom();
  }

  function liveSayDraft(text) {
    const s = S.liveSay && S.liveSay.row && S.liveSay.row.parentNode ? S.liveSay : liveSayNew();
    s.row.classList.remove('thinking');
    s.state = 'speaking';
    s.text = String(text || '');
    s.spoken = 0;
    s.base = 0;
    const e = liveSayEls();
    if (!e) return;
    e.said.textContent = '';
    e.ahead.textContent = s.text;
    e.tag.textContent = '';
    scrollBottom();
  }

  function liveSayProgress(n) {
    if (!S.liveSay || !S.liveSay.text) return;
    const s = S.liveSay;
    s.spoken = Math.max(0, Math.min(s.text.length, Math.floor(n || 0)));
    const e = liveSayEls();
    if (!e) return;
    e.said.textContent = s.text.slice(0, s.spoken);
    e.ahead.textContent = s.text.slice(s.spoken);
  }

  function liveSayEnd(kind, spoken) {
    if (!S.liveSay || !S.liveSay.text) return;
    const s = S.liveSay;
    if (typeof spoken === 'number') liveSayProgress(spoken);
    const e = liveSayEls();
    if (!e) return;
    s.state = kind;
    if (kind === 'done') {
      e.tag.textContent = '说完';
      s.row.classList.add('done');
    } else if (kind === 'partial') {
      e.tag.textContent = '说到一半被打断';
      s.row.classList.add('cut');
      e.ahead.classList.add('dropped');
    } else if (kind === 'cancelled') {
      e.tag.textContent = '未播出';
      s.row.classList.add('cut');
      e.ahead.classList.add('dropped');
    }
    scrollBottom();
  }

  function liveSayClear() {
    if (S.liveSay && S.liveSay.row && S.liveSay.row.parentNode) S.liveSay.row.remove();
    S.liveSay = null;
  }

  /* 助理没接上：把「一句人话 + 一条建议」画进那条红色行。
   *
   * 为什么不直接写原始报错：上游给的是一大段英文 JSON
   * （`litellm.BadRequestError: OpenAIException - Insufficient Balance ...`），
   * 塞进界面等于什么都没说——用户不知道发生了什么，也不知道该做什么。
   * 分类与措辞由后端 llm_errors.py 决定（单一真源），这里只负责显示。
   *
   * `err` 形如 { message, hint, detail }，三者都可缺省。
   */
  function liveSayFail(err) {
    const e = liveSayEls();
    const msg = String((err && err.message) || '未知错误');
    const hint = String((err && err.hint) || '').trim();
    if (!S.liveSay || !e) { toast('接话失败：' + msg); return; }
    S.liveSay.row.classList.remove('thinking');
    S.liveSay.row.classList.add('failed');
    e.ahead.textContent = '没接上：' + msg;
    // 建议单独一行、字号更小：与主文案一起读是「怎么了 + 现在能做什么」，
    // 但视觉上不能抢主文案。同一行已有 hint 就先删掉旧的，避免叠两条。
    const old = S.liveSay.row.querySelector('.lhint');
    if (old) old.remove();
    if (hint) {
      const div = document.createElement('div');
      div.className = 'lhint';
      div.textContent = hint;
      e.ahead.parentNode.appendChild(div);
    }
    scrollBottom();
  }

  function markdown(text) {
    if (window.marked && !window.__noMarked) {
      try { return marked.parse(text); } catch (e) { /* 落到下面 */ }
    }
    // 极简兜底：标题/粗体/列表/段落
    const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const html = esc(text)
      .replace(/^### (.*)$/gm, '<h3>$1</h3>')
      .replace(/^## (.*)$/gm, '<h2>$1</h2>')
      .replace(/^# (.*)$/gm, '<h1>$1</h1>')
      .replace(/^&gt; (.*)$/gm, '<blockquote>$1</blockquote>')
      .replace(/^- (.*)$/gm, '<li>$1</li>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    return html.split(/\n{2,}/).map((p) => (p.trim().startsWith('<') ? p : `<p>${p}</p>`)).join('\n');
  }

  function tintSpeakers(html) {
    return String(html).replace(
      /<strong>\[(\d{1,2}:\d{2}:\d{2})\]\s*([^<]+?)\s*<\/strong>/g,
      (m, ts, name) => {
        const cls = spkClass(name);
        const ai = isAssistantName(name) ? '<span class="aibadge">AI</span>' : '';
        return `<strong class="spkline ${cls}"><span class="spts">[${ts}]</span>${name}${ai}</strong>`;
      },
    );
  }

  function render() {
    const text = S.currentTab === 'report' ? S.resultData.report : S.resultData.transcript;
    doc.innerHTML = text ? tintSpeakers(markdown(text)) : '<p class="hint">这一项没有内容。</p>';
    doc.scrollTop = 0;
  }

  function showResult(msg) {
    setIdle();
    S.resultData = { transcript: msg.transcript || '', report: msg.report || '' };
    if (!msg.ok && !S.resultData.transcript) {
      toast(msg.error || '整理失败，看看服务日志');
      return;
    }
    setPlayback(msg.archive, msg.channels);
    setMeetingId(msg.meeting_id);
    const archName = msg.archive ? String(msg.archive).replace(/[\\/]+$/, '').split(/[\\/]/).pop() : '';
    loadSpeakers(archName);
    result.hidden = false;
    S.currentTab = 'transcript';
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t.dataset.tab === 'transcript'));
    render();
    const extra = msg.elapsed ? `｜${Math.round(msg.elapsed)}s` : '';
    setBadge(`完成${extra}`, '');
  }

  function tickClock() {
    clock.textContent = fmtClock(Date.now() - S.startedAt);
  }

  function recSec() {
    return Math.max(0, S.sentSamples / 16000);
  }
  window.MeetRender = {
    resetLines,
    assistantLabel,
    addLine,
    showPartial,
    clearPartial,
    scrollBottom,
    nearBottom,
    liveSayEls,
    liveSayNew,
    liveSayThinking,
    liveSayDraft,
    liveSayProgress,
    liveSayEnd,
    liveSayClear,
    liveSayFail,
    markdown,
    tintSpeakers,
    render,
    showResult,
    tickClock,
    recSec,
    PROMPT_FOLLOW_PAUSE_MS,
  };
})();
