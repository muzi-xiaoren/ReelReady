/* Personal ratings are local. External publishing always follows an explicit preview. */
(() => {
  'use strict';
  const editor = document.getElementById('evaluation-dialog');
  const sync = document.getElementById('rating-sync-dialog');
  if (!editor || !sync) return;
  const form = document.getElementById('evaluation-form');
  const $ = s => document.querySelector(s);
  let current, score = null, scope = null, planId = null, planState = '', pollTimer, platform = 'tmdb';
  let editorBusy = false, previewBusy = false, openingSync = false;
  async function api(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 35000);
    try {
      const response = await fetch('/ratings' + path, {method: body === undefined ? 'GET' : 'POST',
        headers: {'Content-Type': 'application/json', 'HX-Request': 'true'},
        body: body === undefined ? undefined : JSON.stringify(body), signal: controller.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '内容不正确，请检查后重试');
      return data;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('请求超时，请稍后重新读取状态');
      throw error;
    } finally { clearTimeout(timer); }
  }
  function notice(message, level = 'ok') {
    document.body.dispatchEvent(new CustomEvent('toast', {detail: {message, level}}));
  }
  function paintStars(container, value) {
    container.querySelectorAll('[data-star], [data-editor-star]').forEach(button => {
      const n = Number(button.dataset.star || button.dataset.editorStar);
      button.classList.toggle('selected', n <= (value || 0));
      button.setAttribute('aria-pressed', String(n === value));
    });
  }
  function updateCards(data) {
    document.querySelectorAll(`[data-evaluation="${data.id}"]`).forEach(row => {
      row.dataset.rating = data.rating || 0;
      paintStars(row, data.rating);
      row.querySelector('[data-review]').classList.toggle('has-review', Boolean(data.review));
    });
  }
  async function openEditor(id) {
    if (editorBusy) return;
    editorBusy = true;
    try {
      current = await api('/movies/' + id);
      score = current.rating;
      $('#evaluation-title').textContent = current.title;
      $('#personal-review').value = current.review;
      $('#rating-mark-watched').checked = false;
      $('#evaluation-error').textContent = '';
      paintStars(form, score);
      editor.showModal();
    } catch (error) { notice(error.message, 'error'); }
    finally { editorBusy = false; }
  }
  async function saveEditor() {
    if (editorBusy) return false;
    editorBusy = true;
    form.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      const data = await api('/movies/' + current.id, {rating: score,
        review: $('#personal-review').value, mark_watched: $('#rating-mark-watched').checked});
      current = data;
      updateCards(data);
      return true;
    } catch (error) { $('#evaluation-error').textContent = error.message; return false; }
    finally { editorBusy = false; form.querySelectorAll('button').forEach(b => b.disabled = false); }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (await saveEditor()) { editor.close(); notice('评价已保存到本地'); }
  });
  form.querySelector('[data-sync-current]').addEventListener('click', async () => {
    if (await saveEditor()) { const id = current.id; editor.close(); openSync(id); }
  });
  function selectPlatform(next) {
    platform = next;
    planId = null;
    clearTimeout(pollTimer);
    $('#douban-rating-panel').hidden = platform !== 'douban';
    $('#rating-platform-help').textContent = platform === 'tmdb' ?
      '5 星对应 TMDB 10 分。仅同步评分；TMDB 暂未提供评论写入接口。清空本地评价不会删除平台内容。' :
      '豆瓣通过 CookieCloud 登录态直连，提交评分或短评会在豆瓣标记「看过」。保留隐私与标签，不发布广播。清空本地内容不会删除豆瓣内容。';
    $('#rating-sync-rows').replaceChildren();
    $('#rating-sync-progress').textContent = '';
    $('[data-sync-confirm]').disabled = true;
    sync.querySelectorAll('[data-conflicts]').forEach(b => b.disabled = true);
    sync.querySelectorAll('[data-rating-platform]').forEach(b => {
      b.classList.toggle('active', b.dataset.ratingPlatform === platform);
      b.setAttribute('aria-pressed', String(b.dataset.ratingPlatform === platform));
    });
    if (platform === 'douban') loadManual();
    accountState().catch(error => { $('#rating-account-state').textContent = error.message; });
  }
  async function accountState() {
    const data = await api('/accounts');
    $('#rating-account-state').textContent = platform === 'tmdb' ? (data.tmdb_connected ? '已连接 · ' + data.tmdb_name :
      (data.tmdb_configured ? '尚未连接 TMDB 账户' : '请先在设置中填写 TMDB API Key')) :
      (data.douban_ready ? '已同步豆瓣登录 Cookie · 预览时验证有效性' : '请先在浏览器登录豆瓣，并同步 CookieCloud');
    $('[data-tmdb-connect]').hidden = platform !== 'tmdb' || data.tmdb_connected;
    $('[data-tmdb-disconnect]').hidden = platform !== 'tmdb' || !data.tmdb_connected;
    $('[data-sync-preview]').disabled = platform === 'tmdb' ? !data.tmdb_connected : !data.douban_ready;
  }
  async function openSync(movieId = null) {
    if (sync.open || openingSync) return;
    openingSync = true;
    let active = null;
    try { active = await api('/active'); } catch (_) { /* The normal account read below reports connection errors. */ }
    scope = active ? active.movie_id : movieId;
    clearTimeout(pollTimer);
    planId = null;
    planState = '';
    $('#rating-sync-title').textContent = scope ? '同步这部影片的评价' : '全局同步我的评价';
    $('#rating-sync-scope').textContent = scope ? '仅同步这部影片已保存的个人评分和评论。' : '包含所有分类中已填写的个人评分和评论，不受当前筛选影响。';
    $('#rating-sync-rows').replaceChildren();
    $('#rating-sync-progress').textContent = '';
    $('[data-sync-confirm]').disabled = true;
    sync.querySelectorAll('[data-conflicts]').forEach(b => b.disabled = true);
    sync.querySelectorAll('[data-rating-platform]').forEach(b => b.disabled = false);
    selectPlatform(active ? active.platform : 'tmdb');
    sync.showModal();
    openingSync = false;
    try { await accountState(); } catch (error) { $('#rating-account-state').textContent = error.message; }
    if (active) { planId = active.id; await pollPlan(); }
  }
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function drawPlan(plan) {
    const list = $('#rating-sync-rows');
    list.replaceChildren();
    const labels = {new:'可新增', conflict:'已有不同内容', same:'内容相同', error:'未能读取', unsupported:'暂不支持'};
    for (const row of plan.rows) {
      const card = node('article', undefined, 'rating-sync-row');
      const header = node('div', undefined, 'rating-row-head');
      header.append(node('strong', row.title), node('span', labels[row.status], 'rating-row-status ' + row.status));
      card.append(header);
      card.append(node('div', row.field === 'rating' ? (platform === 'tmdb' ? `我的评分 ${row.local / 2} 星 → TMDB ${row.local} 分` : `我的评分：${row.local} 星`) : '我的评论：' + row.local, 'rating-row-copy'));
      if (row.status === 'conflict' || row.status === 'same') card.append(node('div', row.field === 'review' ? '平台短评：' + row.remote : `平台已有：${row.remote} ${platform === 'tmdb' ? '分' : '星'}`, 'rating-row-copy'));
      if (plan.state === 'ready' && ['new', 'conflict'].includes(row.status)) {
        const label = node('label', row.field === 'rating' ? '评分的处理方式' : '评论的处理方式');
        const select = document.createElement('select');
        select.dataset.syncChoice = row.key;
        for (const [value, text] of row.status === 'new' ? [['add','新增'], ['skip','跳过']] : [['skip','保留平台内容（跳过）'], ['overwrite','用我的内容覆盖']]) {
          const option = node('option', text); option.value = value; select.append(option);
        }
        if (row.status === 'conflict') select.dataset.conflict = 'true';
        label.append(select); card.append(label);
      }
      if (row.result) card.append(node('p', row.result, 'help'));
      list.append(card);
    }
    const conflicts = plan.rows.some(row => row.status === 'conflict');
    sync.querySelectorAll('[data-conflicts]').forEach(b => b.disabled = plan.state !== 'ready' || !conflicts);
    $('[data-sync-confirm]').disabled = plan.state !== 'ready' || !plan.rows.some(r => ['new','conflict'].includes(r.status));
  }
  async function pollPlan() {
    clearTimeout(pollTimer);
    if (!planId || !sync.open) return;
    try {
      const plan = await api('/plans/' + planId);
      planState = plan.state;
      const busy = ['reading','writing'].includes(plan.state);
      $('[data-sync-preview]').disabled = busy;
      sync.querySelectorAll('[data-rating-platform]').forEach(b => b.disabled = busy);
      $('[data-tmdb-disconnect]').disabled = busy;
      $('#rating-sync-progress').textContent = plan.error || (busy ?
        (plan.state === 'reading' ? `正在逐部读取平台评价 · 共 ${plan.count} 部，仅此操作会访问平台` : '正在同步，请勿重复提交；关闭窗口后会继续，可重新打开查看。') :
        plan.state === 'done' ? '同步已结束，请查看每条结果。失败项可重新预览再试；已相同的评分会跳过。' : `已对比 ${plan.count} 部影片 · 尚未发布`);
      if (!busy) drawPlan(plan);
      else pollTimer = setTimeout(pollPlan, 1200);
    } catch (error) { $('#rating-sync-progress').textContent = error.message; $('[data-sync-preview]').disabled = false; }
  }
  async function loadManual() {
    const list = $('#douban-manual-rows'); list.replaceChildren();
    try {
      const items = await api('/local' + (scope ? '?movie_id=' + scope : ''));
      for (const item of items) {
        const card = node('article', undefined, 'rating-sync-row');
        card.append(node('strong', item.title));
        card.append(node('p', item.personal_rating ? '我的评分：' + item.personal_rating + ' 星' : '未评分', 'help'));
        if (item.personal_review) card.append(node('p', item.personal_review, 'rating-row-copy'));
        const actions = node('div', undefined, 'rating-manual-actions');
        if (item.douban_id && /^\d+$/.test(item.douban_id)) {
          const link = node('a', '到豆瓣核对并提交', 'btn btn-sm');
          link.href = 'https://movie.douban.com/subject/' + item.douban_id + '/'; link.target = '_blank'; link.rel = 'noreferrer'; actions.append(link);
        } else actions.append(node('span', '缺少豆瓣影片编号', 'help'));
        if (item.personal_review) {
          const copy = node('button', '复制我的评论', 'btn btn-sm'); copy.type = 'button';
          copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(item.personal_review); notice('评论已复制'); } catch (_) { notice('无法复制，请手动选取评论', 'error'); } });
          actions.append(copy);
        }
        card.append(actions); list.append(card);
      }
      if (!items.length) list.append(node('p', '还没有填写个人评分或评论。', 'help'));
    } catch (error) { list.append(node('p', error.message, 'help')); }
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('button'); if (!button) return;
    if (button.matches('[data-close-rating]')) { if (!editorBusy) button.closest('dialog').close(); return; }
    if (button.matches('[data-review]')) { openEditor(Number(button.dataset.review)); return; }
    if (button.matches('[data-rating-sync]')) { openSync(); return; }
    if (button.matches('[data-editor-star]')) { score = Number(button.dataset.editorStar); paintStars(form, score); return; }
    if (button.matches('[data-clear-rating]')) { score = null; paintStars(form, score); return; }
    if (button.matches('[data-rating-platform]')) { selectPlatform(button.dataset.ratingPlatform); return; }
    if (button.matches('[data-star]')) {
      const row = button.closest('[data-evaluation]');
      row.querySelectorAll('button').forEach(b => b.disabled = true);
      try { updateCards(await api('/movies/' + row.dataset.evaluation, {rating: Number(button.dataset.star)})); notice('评分已保存在本地'); }
      catch (error) { notice(error.message, 'error'); }
      finally { row.querySelectorAll('button').forEach(b => b.disabled = false); }
      return;
    }
    if (button.matches('[data-conflicts]')) { sync.querySelectorAll('select[data-conflict]').forEach(s => s.value = button.dataset.conflicts); return; }
    if (button.matches('[data-tmdb-connect]')) {
      button.disabled = true;
      try { const data = await api('/tmdb/connect', {}); window.location.assign(data.url); }
      catch (error) { $('#rating-account-state').textContent = error.message; }
      finally { button.disabled = false; } return;
    }
    if (button.matches('[data-tmdb-disconnect]')) {
      button.disabled = true;
      try { await api('/tmdb/disconnect', {}); planId = null; $('[data-sync-confirm]').disabled = true; $('#rating-sync-rows').replaceChildren(); await accountState(); }
      catch (error) { $('#rating-account-state').textContent = error.message; }
      finally { button.disabled = false; } return;
    }
    if (button.matches('[data-sync-preview]')) {
      if (previewBusy) return; previewBusy = true; button.disabled = true;
      $('[data-sync-confirm]').disabled = true;
      try { const data = await api('/preview', {movie_id: scope, platform}); planId = data.id; planState = 'reading'; $('#rating-sync-rows').replaceChildren(); await pollPlan(); }
      catch (error) { $('#rating-sync-progress').textContent = error.message; button.disabled = false; }
      finally { previewBusy = false; } return;
    }
    if (button.matches('[data-sync-confirm]')) {
      button.disabled = true;
      const choices = {};
      sync.querySelectorAll('[data-sync-choice]').forEach(s => { choices[s.dataset.syncChoice] = s.value; s.disabled = true; });
      sync.querySelectorAll('[data-conflicts]').forEach(b => b.disabled = true);
      try { await api('/plans/' + planId + '/confirm', {choices}); planState = 'writing'; await pollPlan(); }
      catch (error) { $('#rating-sync-progress').textContent = error.message + '；可重新预览确认最新状态。'; }
    }
  });
  editor.addEventListener('cancel', event => { if (editorBusy) event.preventDefault(); });
  sync.addEventListener('close', () => clearTimeout(pollTimer));
  const auth = new URLSearchParams(location.search).get('rating_auth');
  if (auth) notice(auth === 'connected' ? 'TMDB 账户已连接，可以预览并同步评分' : auth === 'cancelled' ? '已取消 TMDB 授权' : 'TMDB 授权未完成，请检查网络后重试', auth === 'failed' ? 'error' : 'ok');
})();
