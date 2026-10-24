/* 归档、回放、声纹认领、收藏、成本（O-12 D-4）。
 *
 * 这块对应「一次录音结束之后」的所有操作：
 *   · 回放      setPlayback / hidePlayback / setMeetingId（音频播放器 + 会议号）
 *   · 收藏      paintStar / refreshStar / toggleStar（星标免于音频定期清理）
 *   · 声纹认领  speakerRow / loadSpeakers（把「Speaker 2」认成真人，写回声纹库）
 *   · 成本      refreshCost / startCostTimer / stopCostTimer
 *   · 打开最近  openLatest
 *
 * 两个概念容易混，注释里点明：
 *   收藏（.favorite 标记）只影响**音频**是否被定期清理，不影响文本 —— 文本永久留。
 *   声纹认领会写进 speakers.json，**下一次**识别才生效，不用重新转写。
 */
(() => {
  const S = window.MeetState;
  const { fmtMoney, spkClass } = window.MeetFormat;
  const byId = (id) => document.getElementById(id);

  const playerEl = byId('player');
  const playbackEl = byId('playback');
  const pbtipEl = byId('pbtip');
  const meetingIdEl = byId('meetingId');
  const starBtnEl = byId('starBtn');
  const speakersEl = byId('speakers');
  const costBadgeEl = byId('costBadge');

  /* ---- 跨模块回调（运行时解析） ---- */
  function toast(msg, ms) { return window.MeetApp.toast(msg, ms); }
  function setBadge(text, cls) { return window.MeetApp.setBadge(text, cls); }
  function render() { return window.MeetRender.render(); }

  function setPlayback(archive, channels) {
    if (!archive) { hidePlayback(); return; }
    const name = String(archive).replace(/[\\/]+$/, '').split(/[\\/]/).pop();
    if (!name) { hidePlayback(); return; }
    playerEl.src = '/api/archive/audio?dir=' + encodeURIComponent(name);
    playerEl.load();
    playbackEl.hidden = false;
    if (pbtipEl) {
      pbtipEl.textContent = channels > 1
        ? '回放：左右两路已混成一条，等同于旁听者听到的全部声音。'
        : '回放：整段混音，等同于旁听者听到的全部声音。';
    }
  }

  function hidePlayback() {
    try { playerEl.pause(); } catch (e) {}
    playerEl.removeAttribute('src');
    try { playerEl.load(); } catch (e) {}
    if (playbackEl) playbackEl.hidden = true;
    setMeetingId('');   // 同时清掉收藏按钮的状态，否则上一场的星标会残留
    if (speakersEl) { speakersEl.hidden = true; speakersEl.innerHTML = ''; }
  }

  function setMeetingId(mid) {
    if (!meetingIdEl) return;
    const id = String(mid || '').trim();
    if (!id) {
      meetingIdEl.hidden = true;
      meetingIdEl.textContent = '';
      S.currentMid = '';
      S.currentStarred = false;
      paintStar();
      return;
    }
    meetingIdEl.hidden = false;
    meetingIdEl.textContent = 'ID  ' + id;
    meetingIdEl.title = '点一下复制这个 ID';
    meetingIdEl.onclick = async () => {
      try {
        await navigator.clipboard.writeText(id);
        toast('已复制 ' + id);
      } catch (e) {
        toast('复制失败，长按选中手动复制');
      }
    };
    S.currentMid = id;
    S.currentStarred = false;
    paintStar();
    refreshStar();
    // 归档后账也改挂到 meeting_id 上了，这里刷一次才看得到这一场的成本
    refreshCost();
  }

  function paintStar() {
    if (!starBtnEl) return;
    if (!S.currentMid) { starBtnEl.hidden = true; return; }
    starBtnEl.hidden = false;
    starBtnEl.textContent = S.currentStarred ? '★ 已收藏' : '☆ 收藏';
    starBtnEl.classList.toggle('on', S.currentStarred);
  }

  async function refreshStar() {
    if (!S.currentMid) return;
    try {
      const res = await fetch('/api/meetings?limit=100', { credentials: 'same-origin' });
      const data = await res.json();
      const hit = (data.items || []).find((it) => it.meeting_id === S.currentMid);
      if (hit && S.currentMid) { S.currentStarred = !!hit.starred; paintStar(); }
    } catch (e) { /* 拿不到状态就保持未收藏，不影响主流程 */ }
  }

  async function toggleStar() {
    if (!S.currentMid) return;
    const next = !S.currentStarred;
    if (starBtnEl) starBtnEl.disabled = true;
    try {
      const res = await fetch('/api/meeting/star', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: S.currentMid, star: next }),
      });
      let data = null;
      try { data = await res.json(); } catch (e) { /* 下面统一报 */ }
      if (!data || !data.ok) throw new Error((data && data.error) || ('HTTP ' + res.status));
      S.currentStarred = !!data.starred;
      paintStar();
      toast(S.currentStarred
        ? '已收藏：这场录音的音频不会被定期清理'
        : '已取消收藏（音频将按 60 天规则清理）');
    } catch (e) {
      toast('收藏失败：' + (e.message || e));
    } finally {
      if (starBtnEl) starBtnEl.disabled = false;
    }
  }

  function speakerRow(item, archiveName) {
    const row = document.createElement('div');
    row.className = 'spkrow';

    const name = document.createElement('div');
    name.className = 'spkname';
    const pfTag = (item.name && item.profile && item.profile !== '默认') ? `（${item.profile}）` : '';
    name.textContent = (item.name || item.label) + pfTag;
    name.classList.add(spkClass(item.name || item.label));
    row.appendChild(name);

    const pill = document.createElement('span');
    const known = !!item.name;
    const cand = (item.candidates && item.candidates[0]) || null;
    // 只有 status=candidate 才叫「候选」；unknown 时后端的 candidates 只是
    // 排序后的最相近项（可能只有 0.02），标成候选会误导人
    const isCand = !known && item.status === 'candidate' && !!cand;
    pill.className = 'spkpill' + (known ? ' ok' : (isCand ? ' warn' : ''));
    pill.textContent = known ? '已认领'
      : isCand ? `候选 ${cand.name} ${Number(cand.score).toFixed(2)}`
      : '未知';
    row.appendChild(pill);

    const meta = document.createElement('span');
    meta.className = 'spkmeta';
    meta.textContent = `${item.duration.toFixed(1)}s / ${item.windows} 段`;
    row.appendChild(meta);

    const hear = document.createElement('button');
    hear.textContent = '听一下';
    hear.className = 'iconbtn';
    hear.addEventListener('click', () => {
      const a = new Audio('/api/archive/cluster_audio?dir=' + encodeURIComponent(archiveName)
        + '&cluster=' + item.cluster);
      a.play().catch(() => toast('取样失败'));
    });
    row.appendChild(hear);

    if (!known) {
      const input = document.createElement('input');
      input.placeholder = '这是谁？';
      input.setAttribute('list', 'knownNames');
      row.appendChild(input);

      // 场景：同一个人闲聊 / 开会念稿 / 说方言，声纹不一样。
      // 系统猜不出「这是方言」，只能由你在这里标。
      const prof = document.createElement('select');
      prof.className = 'spkprof';
      prof.title = '这个声音属于哪种场景？';
      ['默认', '日常', '开会', '方言', '电话'].forEach((p) => {
        const o = document.createElement('option');
        o.value = p;
        o.textContent = p;
        prof.appendChild(o);
      });
      if (cand && cand.profile) prof.value = cand.profile;
      row.appendChild(prof);

      const ok = document.createElement('button');
      ok.textContent = '认领';
      ok.className = 'iconbtn';
      ok.addEventListener('click', async () => {
        const who = input.value.trim();
        if (!who) { input.focus(); return; }
        ok.disabled = true;
        ok.textContent = '登记中';
        try {
          const res = await fetch('/api/archive/claim', {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dir: archiveName, cluster: item.cluster, name: who,
                                   profile: prof.value }),
          });
          const data = await res.json();
          if (!data.ok) throw new Error(data.error || '认领失败');
          const pf = data.profile && data.profile !== '默认' ? `·${data.profile}` : '';
          toast(`已认领为「${who}${pf}」，下次会自动认出来`);
          await loadSpeakers(archiveName);
          // 转写稿标签已改写，重新拉一次
          const latest = await fetch('/api/latest', { credentials: 'same-origin' });
          if (latest.ok) {
            const d = await latest.json();
            S.resultData.transcript = d.transcript || S.resultData.transcript;
            render();
          }
        } catch (e) {
          toast(String(e.message || e));
          ok.disabled = false;
          ok.textContent = '认领';
        }
      });
      row.appendChild(ok);
    }
    return row;
  }

  async function loadSpeakers(archiveName) {
    if (!speakersEl) return;
    if (!archiveName) { speakersEl.hidden = true; return; }
    try {
      const res = await fetch('/api/archive/speakers?dir=' + encodeURIComponent(archiveName),
        { credentials: 'same-origin' });
      const data = await res.json();
      const items = (data && data.assignments) || [];
      speakersEl.innerHTML = '';
      if (!items.length) { speakersEl.hidden = true; return; }

      const head = document.createElement('div');
      head.className = 'sptip';
      head.textContent = `说话人（信道 ${data.channel || '未知'}）— 认领后写进声纹库，下次自动认出来`;
      speakersEl.appendChild(head);

      items.forEach((it) => speakersEl.appendChild(speakerRow(it, archiveName)));

      // 已知人名做自动补全
      const dl = document.createElement('datalist');
      dl.id = 'knownNames';
      speakersEl.appendChild(dl);
      fetch('/api/speakers', { credentials: 'same-origin' })
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          ((d && d.names) || []).forEach((n) => {
            const o = document.createElement('option');
            o.value = n;
            dl.appendChild(o);
          });
        })
        .catch(() => {});
      speakersEl.hidden = false;
    } catch (e) {
      speakersEl.hidden = true;
    }
  }

  async function refreshCost() {
    if (!costBadgeEl) return;
    const ref = S.liveSessionId || S.currentMid;
    if (!ref) { costBadgeEl.hidden = true; return; }
    try {
      const res = await fetch('/api/cost?ref=' + encodeURIComponent(ref),
        { credentials: 'same-origin' });
      const data = await res.json().catch(() => null);
      const s = data && data.summary;
      if (!s || !s.records) { costBadgeEl.hidden = true; return; }
      const fair = Number(s.total.fair) || 0;
      const saved = Number(s.total.saved) || 0;
      const paid = (Number(s.total.cost) || 0) + (Number(s.total.electricity) || 0);
      costBadgeEl.textContent = '';
      const main = document.createElement('span');
      main.innerHTML = '¥<b>' + fmtMoney(fair) + '</b>';
      costBadgeEl.appendChild(main);
      if (saved > 0) {
        const save = document.createElement('span');
        save.textContent = ' · 省 ' + fmtMoney(saved);
        save.style.color = 'var(--ok)';
        costBadgeEl.appendChild(save);
      }
      costBadgeEl.hidden = false;
      costBadgeEl.title = `折算 ¥${fmtMoney(fair)} ｜ 实付 ¥${fmtMoney(paid)} ｜ 本地省下 ¥${fmtMoney(saved)}`;
    } catch (e) {
      costBadgeEl.hidden = true;
    }
  }

  function startCostTimer() {
    clearInterval(S.costTimer);
    refreshCost();
    S.costTimer = setInterval(refreshCost, 12000);
  }

  function stopCostTimer() {
    clearInterval(S.costTimer);
    S.costTimer = 0;
  }

  async function openLatest() {
    if (S.state !== 'idle' && S.state !== 'done') return;
    try {
      const res = await fetch('/api/latest');
      if (!res.ok) { toast('还没有可回放的录音'); return; }
      const data = await res.json();
      S.resultData = { transcript: data.transcript || '', report: data.report || '' };
      S.currentTab = 'transcript';
      document.querySelectorAll('.tab').forEach((t) =>
        t.classList.toggle('on', t.dataset.tab === 'transcript'));
      setPlayback(data.archive, null);
      setMeetingId(data.meeting_id);
      const archName = data.archive ? String(data.archive).replace(/[\\/]+$/, '').split(/[\\/]/).pop() : '';
      loadSpeakers(archName);
      render();
      result.hidden = false;
      setBadge('上次录音', '');
    } catch (e) {
      toast('读取上次录音失败');
    }
  }
  window.MeetArchive = {
    setPlayback,
    hidePlayback,
    setMeetingId,
    paintStar,
    refreshStar,
    toggleStar,
    speakerRow,
    loadSpeakers,
    refreshCost,
    startCostTimer,
    stopCostTimer,
    openLatest,
  };
})();
