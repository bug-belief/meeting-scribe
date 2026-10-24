/* 语音分身：合成、排队、播放、打断（O-12 D-5）。
 *
 * 这是全应用最绕的一块，因为它同时踩在三条链路上：
 *   ① 合成   POST /api/tts 拿音频（整句 or 按标点切段并行）
 *   ② 播放   <audio> → MediaElementSource → 混音总线 → 虚拟声卡 / 耳机
 *   ③ 记账   tts_begin / tts_end 上报服务端，让转写稿知道「助理说过什么」
 *
 * 三个必须理解的机制（否则改一处就出回声或错字）：
 *   · **切段并行合成**：长句按标点切成 ≤MAX_UNIT 字的段并行合成，
 *     首段先播、其余边播边等 —— 首字延迟从数秒降到 1 秒内。
 *   · **duck（压低真麦）**：分身说话时真麦压到 -18dB，
 *     否则对方会从自己的麦克风里听到回声。
 *   · **打断即作废**：用户插话时 sayAbort=true，已排队的段全部标 cancelled，
 *     并且**只把真正播出去的字数**写进上下文（spoken_chars），
 *     不然模型会以为它说过没说出口的话。
 *
 * 依赖：window.MeetApp（toast / wsSend）、window.MeetAudio（duck / applySink / setMixHint）、
 *       window.MeetRender（liveSay* / scrollBottom / recSec）。
 *       全部运行时解析，避免加载顺序死结。
 */
(() => {
  const S = window.MeetState;
  const { escHtml } = window.MeetFormat;
  const byId = (id) => document.getElementById(id);

  const sayTextEl = byId('sayText');
  const sayGoEl = byId('sayGo');
  const sayStopEl = byId('sayStop');
  const sayDraftEl = byId('sayDraft');
  const sayVoiceEl = byId('sayVoice');
  const sayVoiceHintEl = byId('sayVoiceHint');
  const sayStateEl = byId('sayState');
  const sayQueueEl = byId('sayQueue');
  const saySpeakEl = byId('saySpeak');
  const mixSampleEl = byId('mixSample');

  // 一段最长多少字。经验值：CosyVoice2 的 RTF ≈ 0.45（合成 1 秒音频要 0.45 秒），
  // 切成 16 字一段能在「首字延迟」和「请求数」之间取平衡。
  const MAX_UNIT = 16;

  // 音色目录名 → 人话。目录名是拼音/英文（voices/<name>/默认），
  // 直接显示会变成「音色：zhangsan」，用户看着莫名其妙。
  // 服务端 meta.json 补了 speaker 之后本来就走中文了，这里只是兜底：
  // 老音色没补 speaker 字段、或别的租户用拼音命名，都还能显示中文。
  // 表故意留空：真实音色名属于个人信息，不该写进前端代码。
  // 正确做法是服务端在 meta.json 里补 `speaker` 字段，页面直接用；
  // 表为空时 `cnSpeaker` 会原样显示音色 id —— 难看但不会说谎。
  // 自用想加中文名，请改这里并**只在自己机器上提交**。
  const SPEAKER_CN = {};

  function cnSpeaker(v) {
    const raw = String(v.speaker || v.name || '');
    const person = raw.split('@')[0];
    return SPEAKER_CN[person] || person || raw;
  }

  /* ---- 跨模块回调（运行时解析） ---- */
  function toast(msg, ms) { return window.MeetApp.toast(msg, ms); }
  function wsSend(obj) { return window.MeetApp.wsSend(obj); }
  function duck(on) { return window.MeetAudio.duck(on); }
  function applySink() { return window.MeetAudio.applySink(); }
  function setMixHint(t) { return window.MeetAudio.setMixHint(t); }
  function liveSayEls() { return window.MeetRender.liveSayEls(); }
  function liveSayThinking() { return window.MeetRender.liveSayThinking(); }
  function liveSayDraft(t) { return window.MeetRender.liveSayDraft(t); }
  function liveSayProgress(n) { return window.MeetRender.liveSayProgress(n); }
  function liveSayEnd(k, s) { return window.MeetRender.liveSayEnd(k, s); }
  function scrollBottom() { return window.MeetRender.scrollBottom(); }
  function recSec() { return window.MeetRender.recSec(); }

  /* ⚠️ kbScenario 必须有这一行（实测事故，踩过一次）。
   *
   * 少了它，`kbScenario()` 照样"能跑"，但拿到的是 **DOM 元素**：
   * 浏览器有一条老规矩——带 id 的元素会自动成为 window 上的同名全局变量，
   * 而 index.html 里恰好有 `<select id="kbScenario">`。
   * 于是报错是「kbScenario is not a function」而不是「未定义」，
   * 一眼看过去像"某个函数写错了"，实际是**漏了跨模块导入**。
   *
   * 后果：接话请求在 JSON.stringify 里就抛错，用户看到
   * 「没接上：kbScenario is not a function」，而唤醒本身是成功的
   * （日志里明明有"助理已唤醒"），所以特别容易被误判成"唤醒没生效"。
   *
   * tests/dom_shadow_test.js 会守住这一类问题：任何模块里出现
   * 「调用了本模块没定义、但和某个 DOM id 同名」的函数都会被报出来。
   */
  function kbScenario() { return window.MeetPrompter.kbScenario(); }

  async function playTts() {
    const name = mixSampleEl.value;
    if (!name) { toast('没有可播放的样本'); return; }
    if (!S.ttsAudio || !S.audioCtx) { toast('先点「开始」再试播'); return; }
    if (S.sinkAudio && S.sinkAudio.paused) S.sinkAudio.play().catch(() => {});
    await applySink();

    S.ttsAudio.src = '/api/voice/sample?name=' + encodeURIComponent(name);
    S.ttsAudio.onended = () => { duck(false); setMixHint('播放完毕'); };
    S.ttsAudio.onerror = () => { duck(false); toast('样本加载失败，检查口令或文件是否存在'); };

    try {
      await S.ttsAudio.play();
      duck(true);
      setMixHint(`正在播放：${name}`);
    } catch (err) {
      duck(false);
      toast('播放失败：' + (err.message || err));
    }
  }

  function splitScript(text) {
    const raw = String(text).replace(/\s+/g, ' ').trim();
    if (!raw) return [];
    const parts = raw.match(/[^。！？!?；;\n]+[。！？!?；;]?/g) || [raw];
    const out = [];
    parts.forEach((p) => {
      const s = p.trim();
      if (!s) return;
      if (s.length <= MAX_UNIT) { out.push(s); return; }
      (s.match(/[^，,、]+[，,、]?/g) || [s]).forEach((q) => {
        const t = q.trim();
        if (t) out.push(t);
      });
    });
    // 太碎的句子合并，避免一句两三个字反复起停
    const merged = [];
    out.forEach((s) => {
      if (merged.length && (merged[merged.length - 1] + s).length <= MAX_UNIT) {
        merged[merged.length - 1] += s;
      } else {
        merged.push(s);
      }
    });
    // 首句再切一刀：等待感全在"第一声什么时候出"，先念半句（4~10 字）把声音顶出来，
    // 剩下半句交给预取。两个前提：切点必须在标点上（不然断得难听）、
    // 剩下的部分本身不长（否则切完也只是把等待挪到后半句）。
    if (merged.length && merged[0].length > 10) {
      const head = merged[0].match(/^[\s\S]{4,10}[，,、]/);
      if (head) {
        const rest = merged[0].slice(head[0].length);
        if (rest && rest.length <= MAX_UNIT) merged.splice(0, 1, head[0], rest);
      }
    }
    return merged;
  }

  function setSayState(text) {
    if (sayStateEl) sayStateEl.textContent = text || '';
  }

  function renderQueue() {
    if (!sayQueueEl) return;
    sayQueueEl.innerHTML = '';
    S.sayQueue.forEach((u) => {
      const row = document.createElement('div');
      row.className = 'qline ' + u.state;

      const dot = document.createElement('span');
      dot.className = 'qdot';
      row.appendChild(dot);

      const txt = document.createElement('span');
      txt.className = 'qtext';
      if (u.state === 'speaking' && u.progress > 0.01) {
        const n = Math.floor(u.text.length * u.progress);
        txt.innerHTML = '<b>' + escHtml(u.text.slice(0, n)) + '</b>' + escHtml(u.text.slice(n));
      } else {
        txt.textContent = u.text;
      }
      row.appendChild(txt);

      let tag = '';
      if (u.state === 'pending') tag = '待说';
      else if (u.state === 'synth') tag = '合成中';
      else if (u.state === 'partial') tag = '被打断';
      else if (u.state === 'cancelled') tag = '未播出';
      if (tag) {
        const s = document.createElement('span');
        s.className = 'qtag';
        s.textContent = tag;
        row.appendChild(s);
      }
      sayQueueEl.appendChild(row);
    });
    sayQueueEl.hidden = !S.sayQueue.length;
  }

  function sayVoiceKind(v) {
    return String(v || '').indexOf('sys:') === 0 ? 'system' : 'clone';
  }

  function cnSpeakerName(v) {
    // 入参是音色 id 字符串（如 "zhangsan" / "zhangsan@方言"），
    // 只取 @ 前的说话人，再查中文表；表里没有就原样显示。
    const person = String(v || '').split('@')[0];
    return SPEAKER_CN[person] || person;
  }

  function updateSayVoiceHint() {
    if (!sayVoiceHintEl || !sayVoiceEl) return;
    // 直接读 select 的 value（原始 id），不从 textContent 反解 ——
    // 显示文案以后怎么改都不会把这里带偏。
    const id = sayVoiceEl.value || '';
    const label = cnSpeakerName(id);
    if (!id || !label) {
      sayVoiceHintEl.textContent = '还没有可用音色';
      return;
    }
    const profile = String(id).split('@')[1] || '';
    const pf = profile && profile !== '默认' ? ' · ' + profile : '';
    if (sayVoiceKind(id) === 'system') {
      sayVoiceHintEl.textContent = '电脑声音「' + label + pf + '」：一听就是 AI，适合先给对方打个招呼';
    } else {
      sayVoiceHintEl.textContent = '像你本人「' + label + pf + '」：用你登记过的声纹说话';
    }
  }

  async function loadSayVoices() {
    if (!sayVoiceEl) return;
    try {
      const res = await fetch('/api/tts/voices', { credentials: 'same-origin' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      const list = (data && data.voices) || [];
      if (!list.length) {
        setSayState('还没有登记音色（服务器上跑 voice_register.py）');
        return;
      }
      let saved = '';
      try { saved = localStorage.getItem('wb.sayVoice') || ''; } catch (e) {}
      const keep = sayVoiceEl.value || saved;

      const clones = list.filter((v) => (v.kind || 'clone') !== 'system');
      const systems = list.filter((v) => v.kind === 'system');
      sayVoiceEl.innerHTML = '';

      const addGroup = (title, arr) => {
        if (!arr.length) return;
        const g = document.createElement('optgroup');
        g.label = title;
        arr.forEach((v) => {
          const o = document.createElement('option');
          o.value = v.name;
          if (v.kind === 'system') {
            o.textContent = '电脑 · ' + (v.speaker || v.name)
              + (v.profile ? '（' + v.profile + '）' : '');
          } else {
            const who = cnSpeaker(v);
            const pf = v.profile && v.profile !== '默认' ? ' · ' + v.profile : '';
            o.textContent = '音色：' + who + pf;
          }
          g.appendChild(o);
        });
        sayVoiceEl.appendChild(g);
      };
      addGroup('我的声纹（像你本人）', clones);
      addGroup('电脑声音（一听就是 AI）', systems);

      const has = (v) => Array.prototype.some.call(sayVoiceEl.options, (o) => o.value === v);
      if (keep && has(keep)) sayVoiceEl.value = keep;
      else if (clones.length) sayVoiceEl.value = clones[0].name;
      else if (systems.length) sayVoiceEl.value = systems[0].name;

      if (!sayVoiceEl.dataset.bound) {
        sayVoiceEl.dataset.bound = '1';
        sayVoiceEl.addEventListener('change', () => {
          try { localStorage.setItem('wb.sayVoice', sayVoiceEl.value); } catch (e) {}
          updateSayVoiceHint();
        });
      }
      updateSayVoiceHint();
      setSayState('');
    } catch (e) {
      setSayState('合成服务不可用');
    }
  }

  async function synthUnit(unit, voice, speed) {
    const res = await fetch('/api/tts', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: unit.text,
        voice: voice,
        speed: speed,
        // 带上会话 id，服务端才能把这次合成的字符数记到这一场账上
        session: S.liveSessionId || '',
        scene: 'say',
      }),
    });
    if (!res.ok) {
      let detail = 'HTTP ' + res.status;
      try { detail = (await res.json()).error || detail; } catch (e) {}
      throw new Error(detail);
    }
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  }

  function playUnit(unit) {
    return new Promise((resolve, reject) => {
      unit._resolve = resolve;
      S.ttsAudio.onended = () => { unit._resolve = null; resolve('ended'); };
      S.ttsAudio.onerror = () => { unit._resolve = null; reject(new Error('播放失败')); };
      S.ttsAudio.ontimeupdate = () => {
        if (!S.ttsAudio.duration) return;
        unit.progress = Math.min(1, S.ttsAudio.currentTime / S.ttsAudio.duration);
        renderQueue();
        if (S.liveSay && S.liveSay.text) {
          liveSayProgress((S.liveSay.base || 0) + Math.floor(unit.text.length * unit.progress));
        }
      };
      S.ttsAudio.src = unit.url;
      S.ttsAudio.play().then(() => duck(true)).catch(reject);
    });
  }

  function announceBegin(id, text, voice) {
    clearPartial();   // 开始播报时，把上一句还没定稿的残字清掉，免得看起来像助理说了两遍
    wsSend({ type: 'tts_begin', id, text, voice, at: recSec() });
  }

  function announceEnd(id, state, spokenChars) {
    wsSend({ type: 'tts_end', id, at: recSec(), spoken_chars: spokenChars, state });
  }

  async function sayTest() {
    if (!S.ttsAudio || !S.audioCtx) { toast('先点「开始」，进入录音状态后可用'); return; }
    if (S.sayRunning) { toast('正在说话，先点「打断」'); return; }
    const voice = sayVoiceEl ? sayVoiceEl.value : '';
    if (!voice) { toast('先选一个音色'); return; }
    // 试听的话跟着音色变：克隆音色强调"这是你的分身"，电脑音色强调"这是机器"
    const text = sayVoiceKind(voice) === 'system'
      ? '喂，能听到吗？我是语音助理，现在用电脑的声音跟你说话。'
      : '喂，能听到吗？我是你的声音分身在说话。';
    const tid = 'test' + Date.now().toString(36);
    setSayState('正在合成试听…');
    try {
      if (S.sinkAudio && S.sinkAudio.paused) S.sinkAudio.play().catch(() => {});
      await applySink();
      S.ttsAudio.onended = null;
      S.ttsAudio.onerror = null;
      S.ttsAudio.src = await synthUnit({ text }, voice, 1.0);
      S.ttsAudio.onended = () => {
        announceEnd(tid, 'done', text.length);
        S.lastTtsEndAt = Date.now();
        setSayState('试听完毕');
      };
      S.ttsAudio.onerror = () => {
        announceEnd(tid, 'cancelled', 0);
        setSayState('');
        toast('试听播放失败');
      };
      announceBegin(tid, text, voice);
      await S.ttsAudio.play();
      setSayState('正在试听：耳机和 CABLE 那头应该都能听到');
    } catch (e) {
      announceEnd(tid, 'cancelled', 0);
      setSayState('');
      toast('试听失败：' + (e.message || e));
    }
  }

  function speakOn() {
    return !saySpeakEl || saySpeakEl.checked;
  }

  function liveSayMuted() {
    if (!S.liveSay || !S.liveSay.text) return;
    const e = liveSayEls();
    if (!e) return;
    S.liveSay.state = 'done';
    S.liveSay.row.classList.remove('thinking');
    S.liveSay.row.classList.add('done');
    e.said.textContent = S.liveSay.text;
    e.ahead.textContent = '';
    e.tag.textContent = '只听不说';
    scrollBottom();
  }

  async function sayDraft(auto, called) {
    if (!S.ttsAudio || !S.audioCtx) { toast('先点「开始」，进入录音状态后可用'); return; }
    if (S.wakeBusy) return;
    // 对方（或你）在助理说话时又开口了：先停下，再按新的上下文重新想。
    // 不续说——被打断说明前面的话已经不作数了。
    if (S.sayRunning) sayInterrupt();
    S.wakeBusy = true;
    // ⚠️ **try 必须从置位之后立刻开始**（2026-10-22 修）。
    // 原来是：置位 → setSayState → liveSayThinking → 然后才 try。
    // 那两句里的任何一次抛错都会跳过 finally，`wakeBusy` 就**永久停在 true**，
    // 于是这一次录音（乃至整个页面）再也不会被唤醒 ——
    // 现象正是「喊了完全没反应，服务端一条日志都没有」，极难反查。
    // 现在把置位之后的所有动作都放进 try，finally 里一定复位。
    try {
      if (sayDraftEl) sayDraftEl.disabled = true;
      setSayState(auto ? (called ? '听到叫我了，正在想怎么回…' : '助理正在接话…') : '正在组织语言…');
      // 面板上立刻给一个「正在想」的占位：模型那 1~3 秒里页面不是死的
      liveSayThinking();
    } catch (e) {
      console.error('[唤醒] 占位渲染失败，已复位 wakeBusy', e);
      S.wakeBusy = false;
      if (sayDraftEl) sayDraftEl.disabled = false;
      toast('接话初始化失败：' + (e.message || e));
      return;
    }
    const t0 = performance.now();
    try {
      const res = await fetch('/api/assistant/reply', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session: S.liveSessionId, scenario: kbScenario() }),
      });
      let data = null;
      try { data = await res.json(); } catch (e) { /* 下面统一报 */ }
      if (!data || !data.ok) {
        // 后端已把错误翻成「一句人话 + 一条建议」（llm_errors.py）。
        // 这里把 hint 挂在 Error 上带出去，别在 throw 的地方丢掉——
        // 只留 message 的话，到 catch 里就只剩一句「余额不足」，没法告诉人怎么办。
        const err = new Error((data && data.error) || ('HTTP ' + res.status));
        if (data) { err.kind = data.kind; err.hint = data.hint; err.detail = data.detail; }
        throw err;
      }
      const think = data.elapsed_ms ? (data.elapsed_ms / 1000).toFixed(1)
        : ((performance.now() - t0) / 1000).toFixed(1);
      sayTextEl.value = data.text;
      // 草稿原文直接进面板：后面播到哪、哪句没播，都在这条上跟着更新
      liveSayDraft(data.text);
      if (auto && !speakOn()) {
        // 「出声」关着：文本照样取回来、照样进面板，只是不合成不出声。
        // 这条路径就是以后提词器要接的地方——先不做 UI，接口先通。
        liveSayMuted();
        setSayState('想了 ' + think + 's（只听不说）');
        return;
      }
      setSayState('想了 ' + think + 's，开始合成');
      await sayRun();
    } catch (e) {
      setSayState('');
      if (S.liveSay && S.liveSay.state === 'thinking') {
        // 红色主文案 + 一行建议，都由 render 统一画（见 liveSayFail 的注释）
        window.MeetRender.liveSayFail({ message: e.message || String(e), hint: e.hint });
      }
      toast('接话失败：' + (e.message || e));
    } finally {
      S.wakeBusy = false;
      if (sayDraftEl) sayDraftEl.disabled = false;
    }
  }

  async function sayRun() {
    if (S.sayRunning) return;
    if (!S.ttsAudio || !S.audioCtx) { toast('先点「开始」，进入录音状态后可用'); return; }
    const text = (sayTextEl && sayTextEl.value || '').trim();
    if (!text) { toast('先输入要它说的话'); if (sayTextEl) sayTextEl.focus(); return; }

    const voice = sayVoiceEl ? sayVoiceEl.value : '';
    const sentences = splitScript(text);
    if (!sentences.length) return;

    const stamp = Date.now().toString(36);
    S.sayQueue = sentences.map((t, i) => ({
      id: 'u' + stamp + i, text: t, state: 'pending', progress: 0, url: null, spokenChars: 0,
    }));
    // 手动输入的台词也要进面板；自动接话时草稿已经建好这条，不重复建
    if (!S.liveSay || S.liveSay.text !== text) liveSayDraft(text);
    S.sayRunning = true;
    S.sayAbort = false;
    sayGoEl.disabled = true;
    sayStopEl.disabled = false;
    if (S.sinkAudio && S.sinkAudio.paused) S.sinkAudio.play().catch(() => {});
    await applySink();
    renderQueue();

    let spokenBase = 0;
    try {
      for (let i = 0; i < S.sayQueue.length; i++) {
        if (S.sayAbort) break;
        const unit = S.sayQueue[i];

        if (!unit.url) {
          unit.state = 'synth';
          renderQueue();
          const tSynth = performance.now();
          unit.url = await synthUnit(unit, voice, 1.0);
          if (i === 0) {
            setSayState('首句合成 ' + ((performance.now() - tSynth) / 1000).toFixed(1) + 's');
          }
        }
        if (S.sayAbort) { unit.state = 'cancelled'; break; }

        // 预取后面三句：只生成文件、不出声，不污染录音。
        // 合成比播放快一倍（RTF≈0.45），提前排进队列就不会在换句处卡住；
        // 请求按顺序排队，先发的先合成完，不会插队。
        for (let k = i + 1; k <= i + 3 && k < S.sayQueue.length; k++) {
          const next = S.sayQueue[k];
          if (next && !next.url && !next._fetching) {
            next._fetching = true;
            synthUnit(next, voice, 1.0)
              .then((u) => { next.url = u; })
              .catch(() => {})
              .finally(() => { next._fetching = false; });
          }
        }

        unit.state = 'speaking';
        unit.progress = 0;
        if (S.liveSay) S.liveSay.base = spokenBase;
        const at0 = recSec();
        wsSend({ type: 'tts_begin', id: unit.id, text: unit.text, voice: voice, at: at0 });
        renderQueue();
        setMixHint(`正在说：${unit.text.slice(0, 16)}…`);
        setSayState(`第 ${i + 1}/${S.sayQueue.length} 句`);

        await playUnit(unit);

        if (unit.state === 'partial' || S.sayAbort) break;

        unit.state = 'done';
        unit.spokenChars = unit.text.length;
        spokenBase += unit.text.length;
        liveSayProgress(spokenBase);
        wsSend({
          type: 'tts_end', id: unit.id, at: recSec(),
          spoken_chars: unit.spokenChars, state: 'done',
        });
        renderQueue();
      }
    } catch (err) {
      toast('合成失败：' + (err.message || err));
      setSayState('合成失败');
    } finally {
      S.lastTtsEndAt = Date.now();
      duck(false);
      S.sayQueue.forEach((u) => {
        if (u.state === 'pending' || u.state === 'synth') u.state = 'cancelled';
      });
      renderQueue();
      S.sayRunning = false;
      sayGoEl.disabled = false;
      sayStopEl.disabled = true;
      S.ttsAudio.ontimeupdate = null;
      const done = S.sayQueue.filter((u) => u.state === 'done').length;
      const cut = S.sayQueue.filter((u) => u.state === 'cancelled').length;
      if (S.liveSay && S.liveSay.state === 'speaking') {
        liveSayEnd(cut ? 'partial' : 'done', cut ? S.liveSay.spoken : S.liveSay.text.length);
      }
      setMixHint(cut ? `已说 ${done} 句，${cut} 句未播出（不计入录音与稿子）` : `已说 ${done} 句`);
    }
  }

  function sayInterrupt() {
    if (!S.sayRunning) return;
    S.sayAbort = true;
    const unit = S.sayQueue.find((u) => u.state === 'speaking');
    if (unit) {
      const dur = S.ttsAudio.duration || 0;
      const ratio = dur ? Math.min(1, S.ttsAudio.currentTime / dur) : 0;
      unit.spokenChars = Math.floor(unit.text.length * ratio);
      unit.state = 'partial';
      const finish = unit._resolve;
      unit._resolve = null;
      try { S.ttsAudio.pause(); } catch (e) {}
      wsSend({
        type: 'tts_end', id: unit.id, at: recSec(),
        spoken_chars: unit.spokenChars, state: 'partial',
      });
      if (S.liveSay && S.liveSay.text) {
        liveSayEnd('partial', (S.liveSay.base || 0) + unit.spokenChars);
      }
      if (finish) finish('stopped');
    }
    S.sayQueue.forEach((u) => {
      if (u.state === 'pending' || u.state === 'synth') u.state = 'cancelled';
    });
    duck(false);
    renderQueue();
    setMixHint('已打断：未播出的句子作废，不进录音也不进稿子');
    setSayState('已打断');
  }

  async function loadSamples() {
    if (!mixSampleEl) return;
    try {
      const res = await fetch('/api/voice/samples');
      const data = await res.json();
      const items = (data && data.samples) || [];
      mixSampleEl.innerHTML = '';
      items.forEach((it) => {
        const opt = document.createElement('option');
        opt.value = it.name;
        opt.textContent = it.name;
        mixSampleEl.appendChild(opt);
      });
      if (!items.length) {
        const opt = document.createElement('option');
        opt.textContent = '（没有样本）';
        mixSampleEl.appendChild(opt);
      }
    } catch (e) { /* 口令不对时静默 */ }
  }
  window.MeetTts = {
    playTts,
    splitScript,
    setSayState,
    renderQueue,
    sayVoiceKind,
    updateSayVoiceHint,
    loadSayVoices,
    synthUnit,
    playUnit,
    announceBegin,
    announceEnd,
    sayTest,
    speakOn,
    liveSayMuted,
    sayDraft,
    sayRun,
    sayInterrupt,
    loadSamples,
    MAX_UNIT,
  };
})();
