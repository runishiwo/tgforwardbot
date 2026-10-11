/* ================= 基础 ================= */
const $ = id => document.getElementById(id);
const GROUP_LABEL = { monitor: '实时监控', manual: '手动转发' };
const STATE_LABEL = { running: '运行中', paused: '已停用', done: '已完成', cancelled: '已取消', failed: '失败', interrupted: '已中断' };
const PHASE_HINT = { scanning: '扫描中…', forwarding: '转发中…' };
const TITLES = {
  dashboard: ['总览', '所有任务的整体运行情况'],
  'dashboard-monitor': ['实时监控概览', '实时监控任务的运行与统计数据'],
  'dashboard-manual': ['手动转发概览', '手动转发任务的运行与统计数据'],
  tasks: ['任务中心', '实时监控任务与手动转发任务，点进任意一条看它自己的日志'],
  taskdetail: ['任务详情', '该任务的配置、统计与独立日志'],
  monitor: ['实时任务', '创建多条实时监控任务，各自独立运行'],
  manual: ['发起转发', '按频道与时间范围手动补转历史消息'],
  words: ['词表管理', '关键词与屏蔽词，实时监控 / 手动转发各用一份'],
};
let CURRENT_GROUP = 'monitor';
let WORD_TYPE = 'keyword';
let groups = { monitor: { use_keyword: true, use_blocked: true }, manual: { use_keyword: true, use_blocked: true } };
let groupCounts = {};
let tasksCache = [];
let detailId = null;
let detailMode = 'view';
let detailLines = [];
let logFilter = '';        // 日志类别筛选：'' = 全部；其余为 直转/下载转/屏蔽/未命中/失败
let detailLogDate = '';    // 当前查看的日志日期 YYYY-MM-DD，'' = 今天
let detailLogDates = [];   // 该任务已有的日志日期列表（新 → 旧）
let dialogsAll = [];
let dialogsPostable = [];

function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m])); }
function toast(msg, kind) {
  const d = document.createElement('div');
  d.className = 'toast ' + (kind || '');
  d.textContent = msg;
  $('toasts').appendChild(d);
  setTimeout(() => d.remove(), 3600);
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { data = {}; }
  if (!r.ok) throw new Error((data && (data.error || data.message)) || ('HTTP ' + r.status));
  return data;
}
function fmtUptime(sec) {
  sec = sec || 0;
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  return (d ? d + '天' : '') + (h || d ? h + '小时' : '') + m + '分';
}

/* ================= 视图切换 ================= */
function showView(name, group) {
  if (name === 'dashboard-monitor') group = 'monitor';
  else if (name === 'dashboard-manual') group = 'manual';
  group = group || CURRENT_GROUP;
  CURRENT_GROUP = group;
  document.querySelectorAll('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + name));
  document.querySelectorAll('#nav button').forEach(b => {
    const g = b.closest('.nav-group');
    b.classList.toggle('active', b.dataset.view === name && (!g || g.dataset.group === group));
  });
  document.querySelectorAll('.cat-card').forEach(c => c.classList.toggle('on', c.dataset.group === group));
  const t = TITLES[name] || ['', ''];
  $('page-title').textContent = t[0];
  $('page-sub').textContent = t[1];
  $('crumb').innerHTML = (name === 'dashboard' || name === 'tasks' || name === 'taskdetail')
    ? '<b>总览</b>' : '<b>' + GROUP_LABEL[group] + '</b> / ' + t[0];
  document.title = t[0] + ' · ' + (GROUP_LABEL[group] || '') + ' · TG 转发机器人';
  window.scrollTo({ top: 0 });
  if (name === 'dashboard') loadStats();
  if (name === 'dashboard-monitor') renderOverview('monitor');
  if (name === 'dashboard-manual') renderOverview('manual');
  if (name === 'tasks') loadTasks();
  if (name === 'monitor') { ensureDialogs(); loadMonitorTasks(); }
  if (name === 'manual') { ensureDialogs(); renderManualRule(); }
  if (name === 'words') initWords();
  renderHeadActions(name);
}
function switchView(name) { showView(name, CURRENT_GROUP); }
function enterCategory(g) { showView(g === 'manual' ? 'manual' : 'monitor', g); }
function renderHeadActions(name) {
  const box = $('head-actions');
  box.innerHTML = '';
  const mk = (text, cls, fn) => { const b = document.createElement('button'); b.className = 'btn ' + cls; b.textContent = text; b.onclick = fn; return b; };
  if (name === 'tasks') box.appendChild(mk('新建实时任务', 'ghost sm', () => showView('monitor', 'monitor')));
  if (name === 'monitor') box.appendChild(mk('去任务中心', 'ghost sm', () => showView('tasks')));
  if (name === 'manual') box.appendChild(mk('手动任务历史', 'ghost sm', () => { showView('tasks'); setTaskKind(null, 'manual'); }));
  if (name === 'words') box.appendChild(mk(WORD_TYPE === 'keyword' ? '看屏蔽词' : '看关键词', 'ghost sm', () => switchWordTab(WORD_TYPE === 'keyword' ? 'blocked' : 'keyword')));
}
document.querySelectorAll('#nav button').forEach(b => b.addEventListener('click', () => {
  const g = b.closest('.nav-group');
  showView(b.dataset.view, g ? g.dataset.group : 'monitor');
}));

/* ================= 概览 ================= */
async function loadStats() {
  try {
    const d = await api('/api/stats');
    const t = d.today || {};
    const c = d.counts || {};
    setConn(d.connection, d.telegram_authorized);
    $('cat-monitor-cnt').textContent = '实时任务 ' + (c.monitor || 0) + ' 条 · 运行中 ' + (c.monitor_running || 0) + ' 条';
    $('cat-manual-cnt').textContent = '手动任务 ' + (c.manual || 0) + ' 条 · 运行中 ' + (c.manual_running || 0) + ' 条';
    const cards = [
      ['转发成功', t.forwarded || 0, 'ok', '今日累计 ' + ((d.totals || {}).forwarded || 0)],
      ['转发失败', t.failed || 0, 'err', '今日累计 ' + ((d.totals || {}).failed || 0)],
      ['跳过', t.skipped || 0, 'skip', '命中屏蔽词 / 重复'],
      ['命中关键词', t.matched || 0, '', '进入转发判断'],
      ['队列', d.queue || 0, 'warn', '待转发消息'],
      ['运行中任务', c.tasks_running || 0, 'ok', '实时 ' + (c.monitor_running || 0) + ' · 手动 ' + (c.manual_running || 0)],
    ];
    $('stat-cards').innerHTML = cards.map(x =>
      `<div class="stat ${x[2]}"><div class="k">${x[0]}</div><div class="v">${x[1]}</div><div class="t">${esc(x[3])}</div></div>`).join('')
      + `<div class="stat"><div class="k">连续运行</div><div class="v" style="font-size:18px">${fmtUptime(d.uptime_seconds)}</div><div class="t">自 ${esc(d.started_at || '')}</div></div>`;
    renderChart('chart', d.days || []);
    const list = (d.recent_tasks || []).filter(x => x.state === 'running'); 
    $('run-count').textContent = list.length ? ('共 ' + list.length + ' 条') : '';
    $('dash-tasks').innerHTML = list.length ? list.map(taskCard).join('')
      : '<div class="empty">当前没有运行中的任务，去「实时任务」建一条吧</div>';
    bindTaskActions($('dash-tasks'), list);
  } catch (e) { $('stat-cards').innerHTML = '<div class="empty">概览加载失败：' + esc(e.message) + '</div>'; }
}
function renderChart(target, days) {
  const el = (typeof target === 'string' && $(target)) ? $(target) : $('chart');
  days = days || [];
  if (!days.length) { el.innerHTML = '<div class="empty">暂无数据</div>'; return; }
  const W = 720, H = 190, PL = 34, PR = 12, PT = 14, PB = 26;
  const iw = W - PL - PR, ih = H - PT - PB;
  const rawMax = Math.max(1, ...days.map(d => Math.max(d.forwarded || 0, d.failed || 0)));
  const top = rawMax <= 5 ? 5 : rawMax <= 10 ? 10 : rawMax <= 20 ? 20 : rawMax <= 50 ? 50 : rawMax <= 100 ? 100 : Math.ceil(rawMax / 100) * 100;
  const X = i => (days.length === 1 ? PL + iw / 2 : PL + iw * i / (days.length - 1));
  const Y = v => PT + ih - ih * v / top;
  const line = key => days.map((d, i) => `${X(i).toFixed(1)},${Y(d[key] || 0).toFixed(1)}`).join(' ');
  const grid = [0, top / 2, top].map(v =>
    `<line x1="${PL}" y1="${Y(v)}" x2="${W - PR}" y2="${Y(v)}" class="gl"/><text x="${PL - 6}" y="${Y(v) + 3.5}" class="gt" text-anchor="end">${top >= 100 ? Math.round(v) : v}</text>`
  ).join('');
  const dots = days.map((d, i) => {
    const lbl = esc(d.label || d.date || '');
    return `<circle cx="${X(i).toFixed(1)}" cy="${Y(d.forwarded || 0).toFixed(1)}" r="3.2" class="dot ok"><title>${lbl} 转发成功 ${d.forwarded || 0}</title></circle>` +
      `<circle cx="${X(i).toFixed(1)}" cy="${Y(d.failed || 0).toFixed(1)}" r="3.2" class="dot err"><title>${lbl} 转发失败 ${d.failed || 0}</title></circle>`;
  }).join('');
  const xl = days.map((d, i) =>
    `<text x="${X(i).toFixed(1)}" y="${H - 8}" class="gt" text-anchor="middle">${esc(d.label || d.date || '')}</text>`).join('');
  el.innerHTML = `<svg class="lchart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">${grid}` +
    `<polyline class="ln ok" points="${line('forwarded')}"/><polyline class="ln err" points="${line('failed')}"/>` +
    `${dots}${xl}</svg>`;
}
async function renderOverview(kind) {
  const pre = kind === 'monitor' ? 'ov-mon' : 'ov-man';
  const label = GROUP_LABEL[kind];
  try {
    const d = await api('/api/stats');
    const sc = (d.scopes && d.scopes[kind]) || {};
    const days = sc.days || [];
    const counts = sc.counts || {};
    const td = await api('/api/tasks?kind=' + kind);
    const tasks = td.tasks || [];
    // 概览的数字 = 当前这一类「所有任务各自统计」的合计。
    // 所以任务「重新开始」（统计清零）或删除任务后，这里的数字会同步减少 —— 不再是一个只增不减的独立计数器。
    const agg = { checked: 0, matched: 0, sent: 0, failed: 0, skipped: 0, queued: 0 };
    tasks.forEach(t => { const s = t.stats || {}; Object.keys(agg).forEach(k => { agg[k] += (s[k] || 0); }); });
    $('' + pre + '-stat').innerHTML = [
      ['转发成功', agg.sent, 'ok', '全部任务合计'],
      ['转发失败', agg.failed, 'err', '全部任务合计'],
      ['跳过', agg.skipped, 'skip', '屏蔽词 / 重复 / 来源即目标'],
      ['命中关键词', agg.matched, '', '全部任务合计'],
      ['运行中任务', counts.running || 0, 'ok', '已停用 ' + (counts.paused || 0)],
    ].map(x => `<div class="stat ${x[2]}"><div class="k">${x[0]}</div><div class="v">${x[1]}</div><div class="t">${esc(x[3])}</div></div>`).join('')
      + `<div class="stat"><div class="k">${label}任务总数</div><div class="v" style="font-size:18px">${counts.total || 0}</div><div class="t">运行中 ${counts.running || 0} · 已停用 ${counts.paused || 0}</div></div>`;
    renderChart(pre + '-chart', days);
    const states = ['running', 'paused', 'done', 'cancelled', 'failed', 'interrupted'];
    $('' + pre + '-states').innerHTML = states.map(s =>
      `<div class="it"><div class="k">${STATE_LABEL[s] || s}</div><div class="v">${counts[s] || 0}</div></div>`).join('');
    $('' + pre + '-state').textContent = '共 ' + (counts.total || 0) + ' 条';
    $('' + pre + '-count').textContent = tasks.length ? ('共 ' + tasks.length + ' 条') : '';
    $('' + pre + '-tasks').innerHTML = tasks.length ? tasks.map(taskCard).join('')
      : '<div class="empty">还没有' + label + '任务，去对应的建任务页创建一条</div>';
    bindTaskActions($('' + pre + '-tasks'), tasks);
  } catch (e) {
    $('' + pre + '-stat').innerHTML = '<div class="empty">概览加载失败：' + esc(e.message) + '</div>';
  }
}
function setConn(conn, authorized) {
  const dot = $('conn-dot'), txt = $('conn-text');
  if (!authorized) {
    dot.className = 'dot err';
    txt.innerHTML = '<a href="/telegram-login" style="color:#fca5a5;text-decoration:underline">Telegram 未登录 · 点此连接</a>';
    return;
  }
  const st = (conn || {}).state;
  if (st === 'connected') { dot.className = 'dot ok'; txt.textContent = '已连接 · 跟踪 ' + ((conn || {}).channels_tracked || 0) + ' 频道'; }
  else { dot.className = 'dot warn'; txt.textContent = '连接状态：' + (st || '未知'); }
}

/* ================= 任务中心 ================= */
let taskKind = '', taskState = '';
function setTaskKind(btn, kind) {
  taskKind = kind;
  document.querySelectorAll('.filters [data-kind]').forEach(b => b.classList.toggle('on', b === btn || (btn == null && b.dataset.kind === kind)));
  if (btn) btn.classList.add('on');
  renderTasks();
}
function setTaskState(btn, state) {
  taskState = state;
  document.querySelectorAll('.filters [data-state]').forEach(b => b.classList.toggle('on', b === btn || (btn == null && b.dataset.state === state)));
  if (btn) btn.classList.add('on');
  renderTasks();
}
async function loadTasks() {
  try {
    const d = await api('/api/tasks');
    tasksCache = d.tasks || [];
    renderTasks();
    renderMonitorList();
    if (document.getElementById('view-dashboard-monitor').classList.contains('active')) renderOverview('monitor');
    if (document.getElementById('view-dashboard-manual').classList.contains('active')) renderOverview('manual');
  } catch (e) { $('task-list').innerHTML = '<div class="empty">加载失败：' + esc(e.message) + '</div>'; }
}
function renderTasks() {
  const q = ($('task-q').value || '').trim().toLowerCase();
  let items = tasksCache;
  if (taskKind) items = items.filter(x => x.kind === taskKind);
  if (taskState) items = items.filter(x => x.state === taskState);
  if (q) items = items.filter(x => x.name.toLowerCase().includes(q) || x.id.toLowerCase().includes(q));
  $('tasks-count').textContent = '显示 ' + items.length + ' / ' + tasksCache.length + ' 条';
  $('task-list').innerHTML = items.length ? items.map(taskCard).join('') : '<div class="empty">没有符合条件的任务</div>';
  bindTaskActions($('task-list'), items);
}
function taskCard(t) {
  const s = t.stats || {};
  return `<div class="tk" data-id="${esc(t.id)}">
    <div class="main">
      <div class="nm">${esc(t.name)}
        <span class="pill k-${t.kind}">${t.kind === 'monitor' ? '实时监控' : '手动转发'}</span>
        <span class="pill s-${t.state}">${STATE_LABEL[t.state] || t.state}</span>
      </div>
      <div class="meta">${esc(t.summary)}</div>
      <div class="meta">ID ${esc(t.id)} · 最近更新 ${esc(t.updated_text || '—')}${PHASE_HINT[t.phase] ? ' · ' + PHASE_HINT[t.phase] : ''}${t.error ? ' · ' + esc(t.error) : ''}</div>
      <div class="nums">
        <div>扫描<b>${s.checked || 0}</b></div>
        <div>命中<b>${s.matched || 0}</b></div>
        <div>成功<b>${s.sent || 0}</b></div>
        <div>失败<b>${s.failed || 0}</b></div>
        <div>跳过<b>${s.skipped || 0}</b></div>
        ${t.kind === 'manual' ? `<div>待转发<b>${s.queued || 0}</b></div>` : ''}
      </div>
    </div>
    <div class="acts" data-acts="${esc(t.id)}"></div>
  </div>`;
}
function bindTaskActions(root, items) {
  const pool = items || tasksCache;
  root.querySelectorAll('.acts[data-acts]').forEach(box => {
    const id = box.dataset.acts;
    const t = pool.find(x => x.id === id);
    if (!t) return;
    box.innerHTML = '';
    const mk = (text, cls, fn) => { const b = document.createElement('button'); b.className = 'btn ' + cls + ' sm'; b.textContent = text; b.onclick = fn; return b; };
    box.appendChild(mk('详情 / 日志', 'ghost', () => openTask(id)));
    if (t.kind === 'monitor') {
      if (t.state === 'running') box.appendChild(mk('停用', 'warn', () => monitorState(id, 'pause')));
      else if (t.state === 'paused') box.appendChild(mk('恢复运行', 'ok', () => monitorState(id, 'resume')));
    } else {
      if (t.state === 'running') box.appendChild(mk('暂停', 'warn', () => manualState(id, 'pause')));
      else if (t.state === 'paused' || t.state === 'interrupted') box.appendChild(mk(t.resumable ? '恢复（断点续跑）' : '恢复', 'ok', () => manualState(id, 'resume')));
      box.appendChild(mk('重新开始', 'ghost', () => restartManual(id, t.name)));
    }
    box.appendChild(mk('编辑', 'ghost', () => openEdit(id)));
    box.appendChild(mk('删除', 'danger', () => dropTask(id, t.name)));
  });
}
async function monitorState(id, action) {
  try {
    await api('/api/tasks/monitor/state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, action }) });
    toast(action === 'pause' ? '任务已停用' : '任务已恢复运行', 'ok');
    loadTasks();
  } catch (e) { toast('操作失败：' + e.message, 'err'); }
}
async function cancelManual(id) {
  try { await api('/api/tasks/manual/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }) }); toast('已请求取消', 'ok'); loadTasks(); }
  catch (e) { toast('取消失败：' + e.message, 'err'); }
}
async function manualState(id, action) {
  try {
    await api('/api/tasks/manual/state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, action }) });
    toast(action === 'pause' ? '任务已暂停' : '任务已恢复运行', 'ok');
    loadTasks();
  } catch (e) { toast('操作失败：' + e.message, 'err'); }
}
async function restartManual(id, name) {
  if (!confirm('「重新开始」会清空该任务的统计与转发记录，时间范围内命中的消息会从头重新转发一遍（之前已转过的也会再发一次，可能重复）。确定对「' + (name || id) + '」重新开始？')) return;
  try {
    await api('/api/tasks/manual/state', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id, action: 'restart' }) });
    toast('任务已重新开始', 'ok');
    loadTasks();
  } catch (e) { toast('重新开始失败：' + e.message, 'err'); }
}

/* ================= 编辑任务 ================= */
let editKind = null;
function setDetailMode(mode) {
  // view = 只看统计/日志/锚点；edit = 只显示编辑表单（隐藏统计/日志/锚点）
  detailMode = mode;
  const view = mode === 'view';
  $('dt-stats-card').style.display = view ? '' : 'none';
  $('dt-log-card').style.display = view ? '' : 'none';
  $('dt-anchor-card').style.display = view ? '' : 'none';
  $('dt-edit-card').style.display = view ? 'none' : '';
  if (!view) clearTimeout(detailTimer);
}
async function openEdit(id) {
  try {
    const d = await api('/api/tasks/detail?id=' + encodeURIComponent(id));
    const t = d.task, cfg = t.config || {};
    editKind = t.kind;
    const all = await api('/api/dialogs');
    const post = await api('/api/dialogs?postable=1');
    const srcItems = all.items || [];
    // 目的频道只列「可发言」的频道（与新建任务一致）；已选但当前不可发言的仍保留，避免保存时被悄悄清掉
    const postItems = post.items || [];
    const postIds = new Set(postItems.map(x => x.id));
    const keptTargets = (cfg.targets || []).filter(x => !postIds.has(x.id))
      .map(x => ({ id: x.id, title: x.title, username: x.username || '' }));
    const dstItems = postItems.concat(keptTargets);
    const srcSel = (cfg.sources || []).map(x => x.id);
    const dstSel = (cfg.targets || []).map(x => x.id);
    let html = `<div class="field"><label>任务名称</label><input type="text" id="ed-name" value="${esc(t.name)}" style="max-width:420px"></div>
      <div class="grid2">
        <div class="field"><label>来源频道</label>
          <div class="logbar"><input type="text" class="grow" id="ed-sources-filter" placeholder="搜索频道…" oninput="filterChannels('ed-sources')"><span class="counter" id="ed-sources-status"></span><button class="btn ghost sm" onclick="channelSelAll('ed-sources', this)">全选</button></div>
          <div class="chlist" id="ed-sources"></div>
        </div>
        <div class="field"><label>目的频道</label>
          <div class="logbar"><input type="text" class="grow" id="ed-targets-filter" placeholder="搜索频道…" oninput="filterChannels('ed-targets')"><span class="counter" id="ed-targets-status"></span><button class="btn ghost sm" onclick="channelSelAll('ed-targets', this)">全选</button></div>
          <div class="chlist" id="ed-targets"></div>
        </div>
      </div>`;
    if (t.kind === 'monitor') {
      html += `<div class="grid2">
        <div class="field"><label>关键词（每行一个，或用 ; 分隔，留空 = 用大类词表）</label><textarea id="ed-keywords" rows="3" placeholder="每行一个，或用 ; 分隔（留空则使用大类词表）">${esc((cfg.keywords || []).join('\n'))}</textarea></div>
        <div class="field"><label>屏蔽词（每行一个，或用 ; 分隔，留空 = 用大类词表）</label><textarea id="ed-blocked" rows="3" placeholder="每行一个，或用 ; 分隔（命中即不转发，优先于关键词）">${esc((cfg.blocked || []).join('\n'))}</textarea></div>
      </div>
      <div class="row">
        <label class="sw"><input type="checkbox" id="ed-uk" ${cfg.use_keyword !== false ? 'checked' : ''}><span>启用关键词过滤</span></label>
        <label class="sw"><input type="checkbox" id="ed-ub" ${cfg.use_blocked !== false ? 'checked' : ''}><span>启用屏蔽词过滤（优先）</span></label>
      </div>`;
    } else {
      const r = cfg.range || {};
      html += `<div class="grid2">
        <div class="field"><label>起始时间</label><input type="hidden" id="ed-start" value="${esc(r.start ? r.start.replace(' ', 'T') : '')}"><button type="button" class="dtbox" data-dt="ed-start" onclick="openPicker('ed-start')">${ r.start ? '<span class="val">' + esc(r.start) + '</span>' : '<span class="ph">点击选择起始时间</span>' }</button></div>
        <div class="field"><label>结束时间</label><input type="hidden" id="ed-end" value="${esc(r.end ? r.end.replace(' ', 'T') : '')}"><button type="button" class="dtbox" data-dt="ed-end" onclick="openPicker('ed-end')">${ r.end ? '<span class="val">' + esc(r.end) + '</span>' : '<span class="ph">点击选择结束时间</span>' }</button></div>
      </div>
      <div class="grid2">
        <div class="field"><label>临时关键词（每行一个，或用 ; 分隔，留空 = 按大类词表，运行中改词表即时生效）</label><textarea id="ed-keywords" rows="3" placeholder="每行一个，或用 ; 分隔（留空 = 用「手动转发」大类关键词表，运行中改动即时生效）">${esc((cfg.keywords || []).join('\n'))}</textarea></div>
        <div class="field"><label>临时屏蔽词（每行一个，或用 ; 分隔，留空 = 按大类词表，运行中改词表即时生效）</label><textarea id="ed-blocked" rows="3" placeholder="每行一个，或用 ; 分隔（命中即不转发，优先于关键词）">${esc((cfg.blocked || []).join('\n'))}</textarea></div>
      </div>
      <div class="row">
        <label class="sw"><input type="checkbox" id="ed-fk" ${((cfg.filters || {}).keyword !== false) ? 'checked' : ''}><span>关键词过滤</span></label>
        <label class="sw"><input type="checkbox" id="ed-fb" ${((cfg.filters || {}).blocked !== false) ? 'checked' : ''}><span>屏蔽词过滤</span></label>
        <div class="field" style="margin:0"><label>扫描上限</label><input type="number" id="ed-limit" min="1" max="10000" value="${cfg.limit || 2000}" style="width:140px"><div class="hint">从起始时间往新方向数（未填起始时间则从频道最旧一条开始）</div></div>
      </div>`;
    }
    html += `<div class="grid3" style="align-items:flex-end">
        <div class="field" style="margin:0"><label>日志保留天数</label><input type="number" id="ed-keep" min="0" max="3650" value="${t.keep_days == null ? 30 : t.keep_days}" style="max-width:160px"><div class="hint">0 = 永久保留</div></div>
        <label class="sw"><input type="checkbox" id="ed-anchor" ${t.anchor !== false ? 'checked' : ''}><span>转发锚点去重</span></label>
        <label class="sw"><input type="checkbox" id="ed-gdedup" ${t.global_dedup !== false ? 'checked' : ''}><span>跨任务去重</span></label>
      </div>
      <div class="row" style="margin-top:14px">
        <button class="btn" id="ed-save" onclick="saveEdit('${esc(t.id)}')">保存修改</button>
        <button class="btn ghost" onclick="closeEdit()">取消</button>
      </div>`;
    $('dt-edit').innerHTML = html;
    setupChannelList('ed-sources', srcItems, srcSel);
    setupChannelList('ed-targets', dstItems, dstSel);
    detailId = id;
    showView('taskdetail');
    setDetailMode('edit');
  } catch (e) { toast('打开编辑失败：' + e.message, 'err'); }
}
function closeEdit() { $('dt-edit-card').style.display = 'none'; $('dt-edit').innerHTML = ''; editKind = null; setDetailMode('view'); if (detailId) loadTaskDetail(true); }

/* ================= 自定义日期时间选择器 ================= */
let phTarget = null, phView = null, phSel = null;
function pad2(n) { return (n < 10 ? '0' : '') + n; }
function fmtDisp(v) { return v ? v.replace('T', ' ') : ''; }
function phRender() {
  const yr = phView.y, mo = phView.m;
  $('ph-title').textContent = yr + '年 ' + (mo + 1) + '月';
  const startDow = new Date(yr, mo, 1).getDay();
  const daysInMo = new Date(yr, mo + 1, 0).getDate();
  const prevDays = new Date(yr, mo, 0).getDate();
  const dows = ['日', '一', '二', '三', '四', '五', '六'];
  let cells = dows.map(d => '<div class="ph-dow">' + d + '</div>').join('');
  for (let i = 0; i < startDow; i++) {
    const d = prevDays - startDow + 1 + i;
    cells += '<div class="ph-day mut">' + pad2(d) + '</div>';
  }
  for (let d = 1; d <= daysInMo; d++) {
    const key = yr + '-' + pad2(mo + 1) + '-' + pad2(d);
    cells += '<div class="ph-day' + (phSel === key ? ' sel' : '') + '" onclick="phPickDay(\'' + key + '\', this)">' + pad2(d) + '</div>';
  }
  const total = Math.ceil((startDow + daysInMo) / 7) * 7;
  let tail = 0;
  for (let i = startDow + daysInMo; i < total; i++) { tail++; cells += '<div class="ph-day mut">' + pad2(tail) + '</div>'; }
  $('ph-grid').innerHTML = cells;
}
function phPickDay(key, el) {
  // 只更新选中样式，**不**重建 DOM：否则被点的节点会与文档脱钩，
  // 导致下方 document 的「点击外部关闭」误判为点到外部而把选择框关掉。
  phSel = key;
  const grid = $('ph-grid');
  if (grid) Array.from(grid.children).forEach(c => c.classList.remove('sel'));
  if (el) el.classList.add('sel'); else phRender();
}
function phShift(d) { phView.m += d; if (phView.m < 0) { phView.m = 11; phView.y--; } else if (phView.m > 11) { phView.m = 0; phView.y++; } phRender(); }
function phNow() { const n = new Date(); phView = { y: n.getFullYear(), m: n.getMonth() }; phSel = n.getFullYear() + '-' + pad2(n.getMonth() + 1) + '-' + pad2(n.getDate()); $('ph-hh').value = n.getHours(); $('ph-mm').value = n.getMinutes(); phRender(); }
function phClear() { phSel = null; phRender(); }
function openPicker(targetId) {
  phTarget = targetId;
  const cur = $(targetId).value; // 'YYYY-MM-DDTHH:MM' 或 ''
  let y, mo, d, h = 0, mi = 0;
  if (cur) {
    const p = cur.split('T'); const dp = p[0].split('-'); y = +dp[0]; mo = +dp[1] - 1; d = +dp[2];
    if (p[1]) { const t = p[1].split(':'); h = +t[0] || 0; mi = +t[1] || 0; }
  } else { const n = new Date(); y = n.getFullYear(); mo = n.getMonth(); d = n.getDate(); }
  phView = { y, m: mo };
  phSel = cur ? (y + '-' + pad2(mo + 1) + '-' + pad2(d)) : null;
  $('ph-hh').value = h; $('ph-mm').value = mi;
  phRender();
  const box = document.querySelector('[data-dt="' + targetId + '"]');
  const r = (box || $(targetId)).getBoundingClientRect();
  const pk = $('dt-picker'); pk.style.display = 'block';
  let top = r.bottom + 6, left = r.left;
  if (top + pk.offsetHeight > window.innerHeight) top = r.top - pk.offsetHeight - 6;
  if (left + pk.offsetWidth > window.innerWidth) left = window.innerWidth - pk.offsetWidth - 8;
  pk.style.top = Math.max(8, top) + 'px'; pk.style.left = Math.max(8, left) + 'px';
}
function phApply() {
  if (!phTarget) return;
  const box = document.querySelector('[data-dt="' + phTarget + '"]');
  if (phSel) {
    let h = parseInt($('ph-hh').value || '0', 10), mi = parseInt($('ph-mm').value || '0', 10);
    if (h < 0) h = 0; if (h > 23) h = 23; if (mi < 0) mi = 0; if (mi > 59) mi = 59;
    const v = phSel + 'T' + pad2(h) + ':' + pad2(mi);
    $(phTarget).value = v;
    if (box) box.innerHTML = '<span class="val">' + fmtDisp(v) + '</span>';
  } else {
    $(phTarget).value = '';
    if (box) box.innerHTML = '<span class="ph">点击选择时间</span>';
  }
  closePicker();
}
function closePicker() { const pk = $('dt-picker'); if (pk) pk.style.display = 'none'; phTarget = null; }
document.addEventListener('click', function (e) {
  const pk = $('dt-picker');
  if (!pk || pk.style.display !== 'block') return;
  // composedPath 取的是派发时的路径快照，即使内部节点被重绘也不会误判
  const path = (e.composedPath && e.composedPath()) || [];
  if (path.indexOf(pk) !== -1) return;
  if (pk.contains(e.target)) return;
  if (e.target.closest && e.target.closest('.dtbox')) return;
  closePicker();
});
async function saveEdit(id) {
  const getSel = sid => channelSelToArr(sid);
  const base = {
    name: $('ed-name').value.trim(),
    sources: getSel('ed-sources'),
    targets: getSel('ed-targets'),
    keep_days: parseInt($('ed-keep').value || '30', 10) || 0,
    anchor: $('ed-anchor').checked,
    global_dedup: $('ed-gdedup').checked,
  };
  if (editKind === 'monitor') {
    base.keywords = $('ed-keywords').value;
    base.blocked = $('ed-blocked').value;
    base.use_keyword = $('ed-uk').checked;
    base.use_blocked = $('ed-ub').checked;
  } else {
    base.start_datetime = $('ed-start').value || null;
    base.end_datetime = $('ed-end').value || null;
    base.keywords = $('ed-keywords').value;
    base.blocked = $('ed-blocked').value;
    base.limit = parseInt($('ed-limit').value || '2000', 10);
    base.filter_keyword = $('ed-fk').checked;
    base.filter_blocked = $('ed-fb').checked;
  }
  const path = editKind === 'monitor' ? '/api/tasks/monitor/update' : '/api/tasks/manual/update';
  const btn = $('ed-save'); btn.disabled = true;
  try {
    await api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ id }, base)) });
    toast('已保存修改', 'ok');
    closeEdit();
    loadTasks();
    if (detailId === id) loadTaskDetail(true);
  } catch (e) { toast('保存失败：' + e.message, 'err'); }
  finally { btn.disabled = false; }
}
async function dropTask(id, name) {
  if (!confirm('确定删除任务「' + name + '」？该任务的日志与记录会一并删除。')) return;
  try { await api('/api/tasks/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }) }); toast('已删除', 'ok'); loadTasks(); if (detailId === id) showView('tasks'); }
  catch (e) { toast('删除失败：' + e.message, 'err'); }
}

/* ================= 任务详情 ================= */
let detailTimer = null;
async function openTask(id) {
  detailId = id;
  logFilter = '';                    // 换任务时回到「全部」
  detailLogDate = '';                // 换任务时回到「今天」
  detailLogDates = [];
  setDetailMode('view');
  showView('taskdetail');
  await loadTaskDetail(true);
}
async function loadTaskDetail(scroll) {
  if (!detailId) return;
  if (detailMode === 'edit') return;  // 编辑模式下不刷新/重置视图内容
  try {
    const d = await api('/api/tasks/detail?id=' + encodeURIComponent(detailId)
      + (detailLogDate ? ('&date=' + encodeURIComponent(detailLogDate)) : ''));
    const t = d.task, cfg = t.config || {}, s = t.stats || {};
    $('dt-name').innerHTML = esc(t.name) + ' <span class="pill k-' + t.kind + '">' + (t.kind === 'monitor' ? '实时监控' : '手动转发') + '</span> <span class="pill s-' + t.state + '">' + (STATE_LABEL[t.state] || t.state) + '</span>';
    $('dt-summary').textContent = t.summary + ' · ID ' + t.id;
    const acts = $('dt-acts'); acts.innerHTML = '';
    const mk = (text, cls, fn) => { const b = document.createElement('button'); b.className = 'btn ' + cls + ' sm'; b.textContent = text; b.onclick = fn; return b; };
    acts.appendChild(mk('← 返回任务中心', 'ghost', () => showView('tasks')));
    if (t.kind === 'monitor') {
      if (t.state === 'running') acts.appendChild(mk('停用任务', 'warn', async () => { await monitorState(t.id, 'pause'); loadTaskDetail(); }));
      else if (t.state === 'paused') acts.appendChild(mk('恢复运行', 'ok', async () => { await monitorState(t.id, 'resume'); loadTaskDetail(); }));
    } else {
      if (t.state === 'running') {
        acts.appendChild(mk('暂停', 'warn', async () => { await manualState(t.id, 'pause'); loadTaskDetail(); }));
        acts.appendChild(mk('取消任务', 'danger', async () => { await cancelManual(t.id); loadTaskDetail(); }));
      } else if (t.state === 'paused' || t.state === 'interrupted') {
        const hasP = !!((cfg.progress || {}).stage);
        acts.appendChild(mk(hasP ? '恢复（从断点继续）' : '恢复', 'ok', async () => { await manualState(t.id, 'resume'); loadTaskDetail(); }));
      }
      acts.appendChild(mk('重新开始', 'ghost', async () => { await restartManual(t.id, t.name); loadTaskDetail(); }));
    }
    acts.appendChild(mk('编辑任务', 'ghost', async () => openEdit(t.id)));
    acts.appendChild(mk('删除任务', 'danger', async () => { await dropTask(t.id, t.name); }));
    const items = [
      ['已扫描', s.checked || 0], ['命中', s.matched || 0], ['转发成功', s.sent || 0],
      ['转发失败', s.failed || 0], ['跳过', s.skipped || 0],
      ['创建时间', t.created_text || '—'], ['最近更新', t.updated_text || '—'],
      ['来源频道', (cfg.sources || []).length ? (cfg.sources || []).map(x => x.title).join('、') : (t.kind === 'monitor' ? '全部频道' : '—')],
      ['目的频道', (cfg.targets || []).map(x => x.title).join('、') || '—'],
    ];
    if (t.kind === 'manual') {
      items.push(['时间范围', ((cfg.range || {}).start || '最早') + ' ~ ' + ((cfg.range || {}).end || '现在')]);
    } else {
      items.push(['自带关键词', (cfg.keywords || []).length ? (cfg.keywords || []).join('、') : '用大类词表']);
      items.push(['自带屏蔽词', (cfg.blocked || []).length ? (cfg.blocked || []).join('、') : '用大类词表']);
    }
    if (t.kind === 'manual') {
      items.splice(5, 0, ['当前阶段', PHASE_HINT[t.phase] || '—'], ['待转发', s.queued || 0]);
      const pg = cfg.progress || {};
      if (pg.stage) {
        items.push(['进度断点', pg.stage === 'forward'
          ? ('已记住转发到第 ' + (pg.pos || 0) + ' 条，点「恢复」接着发（不重扫）')
          : ('已记住扫描到第 ' + ((pg.src || 0) + 1) + ' 个来源，点「恢复」接着扫（不重扫）')]);
      }
    }
    $('dt-stats').innerHTML = items.map(x => `<div class="it"><div class="k">${esc(x[0])}</div><div class="v" style="font-size:15.5px">${esc(x[1])}</div></div>`).join('');
    const an = t.anchors || {};
    const aitems = an.items || [];
    $('dt-anchors').innerHTML = [
      ['去重频道数', an.channels || 0],
      ['已记录消息', an.messages || 0],
    ].map(x => `<div class="it"><div class="k">${x[0]}</div><div class="v">${x[1]}</div></div>`).join('')
      + (aitems.length ? '<div style="grid-column:1/-1;font-size:14px;color:var(--muted)">按源频道：' + aitems.slice(0, 12).map(x => '频道 ' + x.chat_id + ' · ' + x.count + ' 条').join('；') + (aitems.length > 12 ? ' …' : '') + '</div>' : '');
    const aacts = $('dt-anchor-acts'); aacts.innerHTML = '';
    const mka = (text, cls, fn) => { const b = document.createElement('button'); b.className = 'btn ' + cls + ' sm'; b.textContent = text; b.onclick = fn; return b; };
    aacts.appendChild(mka('重置该任务锚点（强制重发）', 'warn', async () => {
      if (!confirm('重置后该任务之前转发过的消息都会当作「未转发」重新处理，确定？')) return;
      try { await api('/api/anchors/reset', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ scope: 'task', id: t.id }) }); toast('已重置锚点', 'ok'); loadTaskDetail(); }
      catch (e) { toast('重置失败：' + e.message, 'err'); }
    }));
    detailLogDates = (d.log && d.log.dates) || [];
    detailLogDate = (d.log && d.log.date) || detailLogDate || todayStr();
    renderLogDates();
    detailLines = (d.log && d.log.lines) || [];
    renderDetailLog();
    if (t.state === 'running') {
      clearTimeout(detailTimer);
      detailTimer = setTimeout(() => loadTaskDetail(), 2500);
    }
  } catch (e) { toast('详情加载失败：' + e.message, 'err'); }
}
/* 日志行格式：`[时间] [类别] 内容`（类别可缺省，旧日志就没有） */
/* 类别：直转 / 下载转 / 屏蔽 / 未命中 / 失败；`跳过`、`成功` 是旧名，解析时归一化后兼容 */
const LOG_LINE_RE = /^\[([^\]]*)\]\s*(?:\[(直转|下载转|成功|失败|跳过|未命中|屏蔽)\]\s*)?([\s\S]*)$/;
const LOG_TAGS = [['', '全部'], ['直转', '直转'], ['下载转', '下载转'],
  ['屏蔽', '屏蔽'], ['未命中', '未命中'], ['失败', '失败']];
const TAG_CLS = { '直转': 'ok', '下载转': 'dl', '成功': 'ok', '失败': 'err',
  '未命中': 'skip', '跳过': 'skip', '屏蔽': 'block' };
function parseLogLine(l) {
  const m = LOG_LINE_RE.exec(String(l == null ? '' : l));
  if (!m) return { time: '', tag: '', text: l || '' };
  let tag = m[2] || '';
  if (tag === '跳过') tag = '未命中';      // 兼容改名前的旧日志
  // 旧日志只有一个笼统的「成功」，分不出直转/下载转：保留原样，
  // 这样它仍能在「全部」里看到（不会被误统计进「直转」）。
  return { time: m[1] || '', tag: tag, text: m[3] || '' };
}
function setLogFilter(tag) { logFilter = tag || ''; renderDetailLog(); }
function renderDetailLog() {
  const q = ($('dt-q').value || '').trim();
  const rows = detailLines.map(parseLogLine);
  const counts = { '': rows.length };
  rows.forEach(r => { if (r.tag) counts[r.tag] = (counts[r.tag] || 0) + 1; });
  const tabs = $('dt-logtabs');
  if (tabs) {
    tabs.innerHTML = LOG_TAGS.map(([tag, label]) => {
      const on = (logFilter === tag) ? ' on' : '';
      const cls = TAG_CLS[tag] ? (' t-' + TAG_CLS[tag]) : '';
      return '<button class="ltab' + on + cls + '" onclick="setLogFilter(\'' + tag + '\')">' +
        label + '<b>' + (counts[tag] || 0) + '</b></button>';
    }).join('');
  }
  let shown = logFilter ? rows.filter(r => r.tag === logFilter) : rows;
  if (q) shown = shown.filter(r => (r.text + ' ' + r.tag + ' ' + r.time).indexOf(q) >= 0);
  const filtering = !!logFilter || !!q;
  $('dt-logcount').textContent = filtering
    ? ('匹配 ' + shown.length + ' / ' + rows.length + ' 行')
    : (rows.length + ' 行');
  const pre = $('dt-log');
  if (!shown.length) {
    pre.innerHTML = filtering ? '（没有匹配的日志）' : '（暂无日志）';
  } else {
    pre.innerHTML = shown.map(r => {
      // 显示「MM-DD HH:MM:SS」，从完整时间戳里去年份；悬停仍是完整时间
      const short = r.time ? r.time.replace(/^\d{4}-/, '') : '';
      const head = r.time ? '<span class="lt" title="' + esc(r.time) + '">' + esc(short) + '</span> ' : '';
      const badge = r.tag ? '<span class="ltag t-' + TAG_CLS[r.tag] + '">' + esc(r.tag) + '</span> ' : '';
      let body = esc(r.text);
      if (q) body = body.split(esc(q)).join('<span class="hl">' + esc(q) + '</span>');
      return head + badge + body;
    }).join('\n');
  }
  if ($('dt-follow').checked) pre.scrollTop = pre.scrollHeight;
}

/* ================= 日志日期选择 / 清理 ================= */
function todayStr() {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0')
    + '-' + String(d.getDate()).padStart(2, '0');
}
function logDateLabel(d) {
  if (!d) return '';
  if (d === todayStr()) return '今天 · ' + d;
  return d;
}
function renderLogDates() {
  const sel = $('dt-logdate');
  if (!sel) return;
  const day = detailLogDate || todayStr();
  let opts = detailLogDates.slice();
  if (opts.indexOf(day) < 0) opts = [day].concat(opts);
  sel.innerHTML = opts.map(d =>
    '<option value="' + d + '"' + (d === day ? ' selected' : '') + '>' + logDateLabel(d) + '</option>'
  ).join('');
  sel.value = day;
}
function setLogDate(d) {
  detailLogDate = d || todayStr();
  loadTaskDetail(true);
}
/* 清空全部后服务端会补一行说明，重新拉一次能看到 */
async function reloadLogDays() {
  detailLogDate = '';                 // 回到今天（今天一定存在）
  await loadTaskDetail(true);
  renderLogDates();
}
async function clearTaskLog(all) {
  if (!detailId) return;
  const day = detailLogDate || todayStr();
  const msg = all
    ? '确定清空该任务的【全部日期】日志？此操作不可恢复。'
    : ('确定清空该任务【' + day + '】的日志？此操作不可恢复。');
  if (!confirm(msg)) return;
  try {
    await api('/api/tasks/log/clear', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(all ? { id: detailId } : { id: detailId, date: day })
    });
    toast(all ? '该任务日志已清空' : (day + ' 的日志已清空'), 'ok');
    await reloadLogDays();
  } catch (e) { toast('清理失败：' + e.message, 'err'); }
}
async function clearAllLogs() {
  if (!confirm('确定清空【所有任务】的全部日志？任务本身、统计与锚点不受影响。此操作不可恢复。')) return;
  try {
    const r = await api('/api/tasks/log/clear', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: '*' })
    });
    toast('已清空全部任务日志（共 ' + ((r && r.cleared) || 0) + ' 行）', 'ok');
    loadTasks();
  } catch (e) { toast('清理失败：' + e.message, 'err'); }
}

/* ================= 频道选择（可搜索 + 勾选，与词表体验一致） ================= */
let dialogsLoaded = false;
let channelItems = {};   // id -> [{id,title,username}]
let channelSel = {};      // id -> Set(频道id)
function setupChannelList(id, items, selected) {
  channelItems[id] = items || [];
  channelSel[id] = new Set(selected || []);
  renderChannelList(id);
}
function refreshChannelItems(id, items) {
  // 更新可选列表但保留已勾选（点「重新加载」时不清空已选）
  channelItems[id] = items || [];
  if (!channelSel[id]) channelSel[id] = new Set();
  renderChannelList(id);
}
function renderChannelList(id) {
  const box = $(id);
  if (!box) return;
  const items = channelItems[id] || [];
  const sel = channelSel[id] || (channelSel[id] = new Set());
  const f = $(id + '-filter');
  const q = (f ? f.value : '').trim().toLowerCase();
  const shown = q ? items.filter(x => (x.title + ' ' + (x.username || '')).toLowerCase().indexOf(q) >= 0) : items;
  if (!shown.length) {
    box.innerHTML = '<div class="empty" style="width:100%">' + (q ? '没有匹配的频道' : '暂无可用频道') + '</div>';
  } else {
    box.innerHTML = shown.map(x => {
      const on = sel.has(x.id);
      const un = x.username ? ' <span class="mu">@' + esc(x.username) + '</span>' : '';
      return '<label class="witem ' + (on ? 'sel' : '') + '"><input type="checkbox" ' + (on ? 'checked' : '') +
        ' onchange="channelToggle(\'' + id + '\',' + x.id + ',this)">' + esc(x.title) + un + '</label>';
    }).join('');
  }
  const st = $(id + '-status');
  if (st) st.textContent = (q ? ('匹配 ' + shown.length + ' / 共 ') : '共 ') + items.length + ' 个' + (sel.size ? (' · 已选 ' + sel.size) : '');
}
function channelToggle(id, cid, cb) {
  const on = cb.checked;
  const sel = channelSel[id] || (channelSel[id] = new Set());
  if (on) sel.add(cid); else sel.delete(cid);
  if (cb.parentNode) cb.parentNode.classList.toggle('sel', on);
  const st = $(id + '-status');
  if (st) {
    const items = channelItems[id] || [];
    const f = $(id + '-filter');
    const q = (f ? f.value : '').trim().toLowerCase();
    const shown = q ? items.filter(x => (x.title + ' ' + (x.username || '')).toLowerCase().indexOf(q) >= 0).length : items.length;
    st.textContent = (q ? ('匹配 ' + shown + ' / 共 ') : '共 ') + items.length + ' 个' + (sel.size ? (' · 已选 ' + sel.size) : '');
  }
}
function channelSelAll(id, btn) {
  const items = channelItems[id] || [];
  const f = $(id + '-filter');
  const q = (f ? f.value : '').trim().toLowerCase();
  const shown = q ? items.filter(x => (x.title + ' ' + (x.username || '')).toLowerCase().indexOf(q) >= 0) : items;
  const sel = channelSel[id] || (channelSel[id] = new Set());
  const allSel = shown.length && shown.every(x => sel.has(x.id));
  shown.forEach(x => allSel ? sel.delete(x.id) : sel.add(x.id));
  if (btn) btn.textContent = allSel ? '全选' : '取消全选';
  renderChannelList(id);
}
function channelSelToArr(id) {
  const items = channelItems[id] || [];
  const sel = channelSel[id] || new Set();
  return items.filter(x => sel.has(x.id)).map(x => ({ id: x.id, title: x.title }));
}
function filterChannels(id) { renderChannelList(id); }
async function loadDialogs() {
  try {
    const a = await api('/api/dialogs');
    dialogsAll = a.items || [];
    const p = await api('/api/dialogs?postable=1');
    dialogsPostable = p.items || [];
    if (a.error) toast(a.error, 'err');
    dialogsLoaded = true;
    refreshChannelItems('mt-sources', dialogsAll);
    refreshChannelItems('mt-targets', dialogsPostable);
    refreshChannelItems('mf-sources', dialogsAll);
    refreshChannelItems('mf-targets', dialogsPostable);
  } catch (e) { toast('频道列表加载失败：' + e.message, 'err'); }
}
function ensureDialogs() { if (!dialogsLoaded) loadDialogs(); }

/* ================= 实时任务 ================= */
async function loadMonitorTasks() {
  if (!tasksCache.length) { try { const d = await api('/api/tasks?kind=monitor'); tasksCache = tasksCache.concat(d.tasks || []); } catch (e) {} }
  renderMonitorList();
}
function renderMonitorList() {
  const items = tasksCache.filter(x => x.kind === 'monitor');
  $('mt-count').textContent = items.length ? ('共 ' + items.length + ' 条') : '';
  $('mt-list').innerHTML = items.length ? items.map(taskCard).join('') : '<div class="empty">还没有实时任务，用上面的表单建一条</div>';
  bindTaskActions($('mt-list'), items);
}
async function createMonitor(asPaused) {
  const targets = channelSelToArr('mt-targets');
  if (!targets.length) { toast('请至少选择一个目的频道', 'err'); return; }
  const btn = $('mt-create'); btn.disabled = true;
  try {
    const d = await api('/api/tasks/monitor', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: $('mt-name').value.trim(),
        sources: channelSelToArr('mt-sources'),
        targets: targets,
        keywords: $('mt-keywords').value,
        blocked: $('mt-blocked').value,
        state: asPaused ? 'paused' : 'running',
        keep_days: parseInt($('mt-keep').value || '30', 10) || 0,
        anchor: $('mt-anchor').checked,
        global_dedup: $('mt-gdedup').checked,
      })
    });
    toast(asPaused ? '已创建（当前为停用状态）' : '任务已创建并开始监控', 'ok');
    $('mt-name').value = ''; $('mt-keywords').value = ''; $('mt-blocked').value = '';
    channelSel['mt-sources'] = new Set(); channelSel['mt-targets'] = new Set();
    renderChannelList('mt-sources'); renderChannelList('mt-targets');
    tasksCache = (tasksCache || []).filter(x => x.id !== d.task.id).concat([d.task]);
    renderMonitorList();
    loadTasks();
  } catch (e) { toast('创建失败：' + e.message, 'err'); }
  finally { btn.disabled = false; }
}

/* ================= 手动转发 ================= */
async function renderManualRule() {
  try {
    const d = await api('/api/words/scopes');
    groups = d.scopes || groups;
    groupCounts = d.counts || {};
  } catch (e) {}
  const c = groups.manual || {};
  const kwOn = c.use_keyword !== false, blOn = c.use_blocked !== false;
  const cn = groupCounts.manual || { keyword: 0, blocked: 0 };
  let html = '当前过滤规则（手动转发）：关键词 '
    + (kwOn ? '<b>启用</b>（' + (cn.keyword || 0) + ' 词）' : '<b>未启用</b>（不限关键词）')
    + '　·　屏蔽词 ' + (blOn ? '<b>启用</b>（' + (cn.blocked || 0) + ' 词，命中即不转发）' : '<b>未启用</b>');
  if (!kwOn) html += '<br><span style="color:var(--err);font-weight:600">⚠ 未启用关键词过滤：时间范围内的所有消息都会进入转发判断，请谨慎使用。</span>';
  html += ' <a class="inline" href="javascript:void(0)" onclick="showView(\'words\',\'manual\')">去调整词表开关 →</a>';
  $('mf-rule').innerHTML = html;
}
async function startManual() {
  const sources = channelSelToArr('mf-sources'), targets = channelSelToArr('mf-targets');
  if (!sources.length || !targets.length) { toast('来源频道和目的频道都要选至少一个', 'err'); return; }
  const btn = $('mf-startbtn'); btn.disabled = true;
  try {
    const d = await api('/api/tasks/manual', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: $('mf-name').value.trim(),
        sources, targets,
        start_datetime: $('mf-start').value || null,
        end_datetime: $('mf-end').value || null,
        keywords: $('mf-keywords').value,
        blocked: $('mf-blocked').value,
        limit: parseInt($('mf-limit').value || '2000', 10),
        keep_days: parseInt($('mf-keep').value || '30', 10) || 0,
        anchor: $('mf-anchor').checked,
        global_dedup: $('mf-gdedup').checked,
      })
    });
    toast('任务已启动：' + d.task.name, 'ok');
    $('mf-name').value = ''; $('mf-keywords').value = ''; $('mf-blocked').value = '';
    channelSel['mf-sources'] = new Set(); channelSel['mf-targets'] = new Set();
    renderChannelList('mf-sources'); renderChannelList('mf-targets');
    tasksCache = (tasksCache || []).filter(x => x.id !== d.task.id).concat([d.task]);
    openTask(d.task.id);
  } catch (e) { toast('启动失败：' + e.message, 'err'); }
  finally { btn.disabled = false; }
}

/* ================= 词表 ================= */
let wordCache = { keyword: [], blocked: [] };
let wordSelection = { keyword: new Set(), blocked: new Set() };
function currentScope() { return CURRENT_GROUP === 'manual' ? 'manual' : 'monitor'; }
function wKey(t) { return t === 'blocked' ? 'bl' : 'kw'; }
async function initWords() {
  const scope = currentScope();
  closeAllBatch();
  $('words-title-scope').textContent = GROUP_LABEL[scope];
  $('words-file').textContent = '文件：word/keyword.' + scope + '.txt / word/blocked.' + scope + '.txt';
  await loadScopes();
  wordSelection = { keyword: new Set(), blocked: new Set() };
  await Promise.all([wordLoad('keyword'), wordLoad('blocked')]);
}
async function loadScopes() {
  try {
    const d = await api('/api/words/scopes');
    if (d.scopes) groups = d.scopes;
    groupCounts = d.counts || {};
  } catch (e) {}
  applyScopeSwitches();
}
function applyScopeSwitches() {
  const c = groups[currentScope()] || { use_keyword: true, use_blocked: true };
  $('sw-keyword').checked = c.use_keyword !== false;
  $('sw-blocked').checked = c.use_blocked !== false;
  renderScopeHint();
  applyWordTabs();
}
function renderScopeHint() {
  const c = groups[currentScope()] || { use_keyword: true, use_blocked: true };
  const kwOn = c.use_keyword !== false, blOn = c.use_blocked !== false;
  const cn = groupCounts[currentScope()] || { keyword: 0, blocked: 0 };
  let html = '关键词：' + (kwOn
    ? '<span class="on-k">启用</span>（' + (cn.keyword || 0) + ' 词，命中才转发）'
    : '<span class="off">未启用，不做关键词过滤</span>')
    + '　·　屏蔽词：' + (blOn
      ? '<span class="on-b">启用</span>（' + (cn.blocked || 0) + ' 词，命中即不转发，优先于关键词）'
      : '<span class="off">未启用，不做屏蔽词过滤</span>');
  if (!kwOn && !blOn) html += '<br><span class="alert">⚠ 两者都未启用：本类不对消息内容做任何过滤。</span>';
  else if (!kwOn && currentScope() === 'monitor') html += '<br><span class="alert">⚠ 实时监控未启用关键词过滤：被监控频道的所有消息都会进入转发队列。</span>';
  $('scope-hint').innerHTML = html;
}
function applyWordTabs() {
  const c = groups[currentScope()] || {};
  $('tab-kw').textContent = '关键词（命中才转发）' + (c.use_keyword === false ? ' · 已停用' : '');
  $('tab-bl').textContent = '屏蔽词（命中就不转发，优先级更高）' + (c.use_blocked === false ? ' · 已停用' : '');
  $('tab-kw').classList.toggle('on', WORD_TYPE === 'keyword');
  $('tab-bl').classList.toggle('on', WORD_TYPE === 'blocked');
  $('pane-keyword').style.display = WORD_TYPE === 'keyword' ? '' : 'none';
  $('pane-blocked').style.display = WORD_TYPE === 'blocked' ? '' : 'none';
}
async function saveScopes() {
  try {
    const d = await api('/api/words/scopes', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: currentScope(), use_keyword: $('sw-keyword').checked, use_blocked: $('sw-blocked').checked })
    });
    groups = d.scopes || groups;
    await loadScopes();
    toast(GROUP_LABEL[currentScope()] + '：词表开关已保存', 'ok');
  } catch (e) { toast('保存失败：' + e.message, 'err'); applyScopeSwitches(); }
}
function switchWordTab(t) {
  WORD_TYPE = t;
  applyWordTabs();
  renderHeadActions('words');
}
async function wordLoad(t) {
  try {
    const d = await api('/api/words?type=' + t + '&scope=' + currentScope());
    wordCache[t] = d.items || [];
    wordRender(t);
    updatePreview(t);
  } catch (e) { $(wKey(t) + '-status').textContent = '加载失败'; }
}
function wordQuery(t) { return ($(wKey(t) + '-input').value || '').trim().toLowerCase(); }
function wordRender(t) {
  const q = wordQuery(t);
  const all = wordCache[t] || [];
  const items = q ? all.filter(w => w.toLowerCase().includes(q)) : all;
  const sel = wordSelection[t];
  $(wKey(t) + '-list').innerHTML = items.length ? items.map(w => {
    const on = sel.has(w);
    return `<label class="witem ${on ? 'sel' : ''}"><input type="checkbox" ${on ? 'checked' : ''} onchange="wordToggle('${t}', ${JSON.stringify(w).replace(/"/g, '&quot;')}, this.checked)">${esc(w)}</label>`;
  }).join('') : '<div class="empty" style="width:100%">' + (q ? '没有这个词条 —— 回车或点「添加」即可加入' : '词表还是空的，上面添加吧') + '</div>';
  $(wKey(t) + '-status').textContent = (q ? '匹配 ' + items.length + ' / 共 ' + all.length : '共 ' + all.length + ' 个')
    + (sel.size ? ' · 已选 ' + sel.size : '');
  wordHint(t);
}
/* 输入框即「筛选框 + 添加框」：实时告诉用户这个词在不在表里 */
function wordHint(t) {
  const el = $(wKey(t) + '-hint');
  if (!el) return;
  const raw = ($(wKey(t) + '-input').value || '').trim();
  const label = t === 'blocked' ? '屏蔽词' : '关键词';
  if (!raw) {
    el.className = 'whint';
    el.textContent = '输入' + label + '：在表里会立刻筛出来；不在表里，回车就直接加进去。可用 ; ， 、 或换行一次加多个。';
    return;
  }
  const exist = new Set(wordCache[t] || []);
  const words = splitWordInput(raw);
  const dup = words.filter(w => exist.has(w));
  const miss = words.filter(w => !exist.has(w));
  if (words.length === 1 && dup.length) {
    el.className = 'whint exists';
    el.innerHTML = '<b>✓ 已存在</b>：' + esc(dup[0]) + '　<span class="muted">下方列表已筛出该词条，可勾选后「删除所选」</span>';
  } else if (words.length === 1) {
    el.className = 'whint missing';
    el.innerHTML = '<b>＋ 不在词表中</b>：' + esc(miss[0]) + '　<span class="muted">回车或点「添加」即加入</span>';
  } else {
    el.className = 'whint ' + (miss.length ? 'missing' : 'exists');
    el.innerHTML = '<b>' + words.length + ' 个词条</b>：已存在 ' + dup.length + ' 个，待添加 ' + miss.length + ' 个'
      + (miss.length ? '（' + esc(miss.slice(0, 8).join('、')) + (miss.length > 8 ? ' …' : '') + '）' : '');
  }
}
function wordInput(t) { wordRender(t); }
function wordToggle(t, w, on) { on ? wordSelection[t].add(w) : wordSelection[t].delete(w); wordRender(t); }
function wordToggleAll(t, btn) {
  const q = wordQuery(t);
  const items = (wordCache[t] || []).filter(w => !q || w.toLowerCase().includes(q));
  const allSel = items.length && items.every(w => wordSelection[t].has(w));
  items.forEach(w => allSel ? wordSelection[t].delete(w) : wordSelection[t].add(w));
  if (btn) btn.textContent = allSel ? '全选' : '取消全选';
  wordRender(t);
}
async function wordAdd(t) {
  const input = $(wKey(t) + '-input');
  const raw = input.value.trim();
  if (!raw) { toast('先输入要添加的词条', 'err'); return; }
  const label = t === 'blocked' ? '屏蔽词' : '关键词';
  const words = splitWordInput(raw);
  const exist = new Set(wordCache[t] || []);
  const add = words.filter(w => !exist.has(w));
  const dup = words.filter(w => exist.has(w));
  if (!add.length) {                     // 全都已经在表里 —— 只提示，不重复写文件
    toast('已存在，无需重复添加：' + dup.join('、'));
    wordRender(t);
    return;
  }
  try {
    await api('/api/words', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ type: t, scope: currentScope(), action: 'add', words: add }) });
    input.value = '';
    await wordLoad(t);
    toast('已添加 ' + add.length + ' 个' + label + (dup.length ? '（' + dup.length + ' 个已存在，已跳过）' : ''), 'ok');
  } catch (e) { toast('添加失败：' + e.message, 'err'); }
}
async function wordRemoveSel(t) {
  const words = Array.from(wordSelection[t]);
  if (!words.length) { toast('先勾选要删除的词条', 'err'); return; }
  if (!confirm('确定从「' + GROUP_LABEL[currentScope()] + '」的' + (t === 'blocked' ? '屏蔽词' : '关键词') + '表中删除 ' + words.length + ' 个词条？')) return;
  try {
    await api('/api/words', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ type: t, scope: currentScope(), action: 'remove', words }) });
    wordSelection[t] = new Set();
    await wordLoad(t);
    toast('已删除 ' + words.length + ' 个词条', 'ok');
  } catch (e) { toast('删除失败：' + e.message, 'err'); }
}

/* ================= 词表批量添加 / 预览复制 ================= */
const WORD_SPLIT_RE = /[;；,，、\n\r\t　]+/;
const WORD_COMMENT_PREFIX = '##';
function uniqWords(arr) { const s = new Set(), o = []; arr.forEach(w => { if (w && !s.has(w)) { s.add(w); o.push(w); } }); return o; }
function splitWordInput(text) {
  // 与后端一致：以 ## 开头的整行视为注释并忽略，这样可直接粘贴整份词表文件内容
  const lines = String(text || '').split(/\r?\n/).filter(l => !l.trim().startsWith(WORD_COMMENT_PREFIX));
  return uniqWords(lines.join('\n').split(WORD_SPLIT_RE).map(s => s.trim()).filter(Boolean));
}
function updatePreview(t) {
  const all = wordCache[t] || [];
  const box = $(wKey(t) + '-preview');
  if (!box) return;
  box.value = all.join('; ');
  $(wKey(t) + '-preview-count').textContent = '共 ' + all.length + ' 个';
}
async function copyPreview(t) {
  const box = $(wKey(t) + '-preview');
  const all = wordCache[t] || [];
  if (!all.length) { toast((t === 'blocked' ? '屏蔽词' : '关键词') + '表是空的，没有可复制的内容', 'err'); return; }
  let ok = false;
  try { await navigator.clipboard.writeText(box.value); ok = true; } catch (e) { ok = false; }
  if (!ok) {
    try { box.focus(); box.select(); document.execCommand('copy'); ok = true; } catch (e) { ok = false; }
  }
  toast(ok ? ('已复制 ' + all.length + ' 个词条') : '复制失败，请手动全选复制（Ctrl+A / Ctrl+C）', ok ? 'ok' : 'err');
}
function openBatch(t) {
  const box = $(wKey(t) + '-batch'), ta = $(wKey(t) + '-batch-text');
  box.style.display = '';
  ta.value = '';
  ta.placeholder = t === 'blocked' ? '例如：赌博; 代购，加微信、刷单' : '例如：特价; 秒杀，限时、清仓';
  $(wKey(t) + '-batch-tip').innerHTML = '粘贴要添加的词条，支持 <b>;</b> <b>，</b> <b>、</b> <b>换行</b> 分隔，'
    + '一次可以加很多个；已存在的会自动跳过，词条本身可以带空格。';
  try { ta.focus(); } catch (e) {}
  try { box.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); } catch (e) {}
}
function closeBatch(t) {
  const box = $(wKey(t) + '-batch');
  if (box) box.style.display = 'none';
}
function closeAllBatch() { closeBatch('keyword'); closeBatch('blocked'); }
async function batchAction(t) {
  const ta = $(wKey(t) + '-batch-text');
  const label = t === 'blocked' ? '屏蔽词' : '关键词';
  const words = splitWordInput(ta.value);
  if (!words.length) { toast('请先粘贴要添加的词条', 'err'); return; }
  const exist = new Set(wordCache[t] || []);
  const add = words.filter(w => !exist.has(w));
  const skipped = words.length - add.length;
  const btn = $(wKey(t) + '-batch-go');
  btn.disabled = true;
  try {
    if (add.length) {
      await api('/api/words', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: t, scope: currentScope(), action: 'add', words: add })
      });
    }
    closeBatch(t);
    await wordLoad(t);
    if (!add.length) toast('这 ' + words.length + ' 个词条都已经存在，无需添加');
    else toast('已添加 ' + add.length + ' 个' + label + (skipped ? ('（' + skipped + ' 个已存在，已跳过）') : ''), 'ok');
  } catch (e) {
    toast('添加失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

/* ================= 启动 ================= */
showView('dashboard');
setInterval(() => { if ($('view-dashboard').classList.contains('active')) loadStats(); }, 8000);
