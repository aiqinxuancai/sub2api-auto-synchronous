'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
// crypto.randomUUID 仅在安全上下文（HTTPS / localhost）可用，HTTP 访问时需回退
const requestKey = () => {
  if (window.crypto?.randomUUID) return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (window.crypto?.getRandomValues) crypto.getRandomValues(bytes);
  else for (let i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
  return [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
};
const state = { csrf: '', setup: false, ready: false, page: 'overview', overview: null,
  groups: [], users: [], jobs: [], logs: [], quota: null, receipts: [], summaries: [], selected: new Set(),
  groupFilter: 'all', groupPage: 0, userPage: 0, settingsDirty: false, quotaDirty: false,
  watched: new Map(), knownJobs: new Map(), batchMode: 'paste', currentGroup: null,
  currentJob: null, batchKey: null, resetRule: null, polling: false, offline: false,
  jobHistory: new Map(), jobsExhausted: false, loadingJobs: false, openCandidates: new Set(),
  openRules: new Set(), stableNameEdited: false, jobLog: { id: null, items: [] } };
const JOB_PAGE = 100;
const pages = { overview: '同步概览', settings: '连接配置', members: '成员与额度', logs: '运行日志' };
const kinds = { sync: '同步可用分组', preview: '同步预览', connect: '连接测试', refresh_users: '刷新成员',
  batch_users: '批量创建成员', quota_reset: '余额重置', low_cost: '最低价渠道检测', best_source: '最优源站检测' };
const statuses = { active: '运行中', inactive: '已停用', pending: '待同步', excluded: '已排除',
  success: '已完成', failed: '失败', partial: '部分完成', running: '执行中', queued: '排队中',
  interrupted: '已中断', uncertain: '待核对', paused: '管理已暂停' };
const reasons = { no_healthy_models: '当前没有通过健康检测的模型', no_longer_accessible: '上游账号已无法访问此分组',
  upstream_inactive: '上游分组已停用', not_in_provider_hall: '分组未出现在上游监测大厅',
  platform_mismatch: '分组平台与监测数据不一致', monitoring_disabled_or_unknown: '上游检测已关闭或状态未确认',
  availability_unknown: '上游可用性尚未确认',
  platform_not_selected: '未选择同步此平台', outside_sync_top_n: '超出设定的前 N 个分组',
  group_not_selected: '不在限定的分组 ID 范围内', not_visible_in_hall: '未在供应商大厅展示',
  model_detection_not_passed: '模型检测未通过', selector_disabled: '自动管理已暂停，现有分组保持原状态',
  no_healthy_candidates: '候选范围内暂无可用渠道，保留现有路由并重试', pending_upstream_key: '等待上游 Key，保留现有路由并重试',
  no_bound_account: '未发现可用的绑定账号，保留分组状态并等待修复',
  sync_update_failed: '本轮更新失败，保留现有状态并等待后续重试',
  rank_dropped: '当前渠道综合评分排名下降超过允许范围',
  source_unavailable: '当前渠道已不可用', updating: '正在切换渠道',
  outside_top_ten: '当前渠道已离开设定的候选排名范围', keyword_excluded: '名称命中排除关键字',
  group_inactive: '上游分组已停用', probe_disabled: '上游未启用有效检测', unsupported_platform: '暂不支持此平台',
  no_provider: '没有匹配的监测数据', no_provider_data: '没有匹配的监测数据',
  no_enabled_probe: '当前没有启用中的有效探测', probe_expired: '上游探测结果已过期' };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function icon(name) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  node.classList.add('icon'); node.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(node.namespaceURI, 'use');
  use.setAttribute('href', '#i-' + name); node.append(use); return node;
}
function badge(status) { return el('span', 'pill status-' + status, statuses[status] || status); }
function number(value, digits = 4) {
  return value == null ? '—' : Number(value).toLocaleString('en-US', { maximumFractionDigits: digits });
}
function date(value, zone, full = false) {
  if (!value) return '—';
  const instant = new Date(typeof value === 'number' ? value * 1000 : value);
  if (Number.isNaN(instant.getTime())) return '—';
  try { return instant.toLocaleString('zh-CN', { timeZone: zone || undefined,
    year: full ? 'numeric' : undefined, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }); }
  catch { return instant.toLocaleString('zh-CN'); }
}
function timeAgo(value) {
  const seconds = Math.max(0, Date.now() / 1000 - value);
  return seconds < 60 ? '刚刚' : seconds < 3600 ? `${Math.floor(seconds / 60)} 分钟前` :
    seconds < 86400 ? `${Math.floor(seconds / 3600)} 小时前` : date(value);
}
function host(value) { try { return new URL(value).host; } catch { return '尚未配置'; } }
function warningText(value) {
  if (typeof value === 'string') return value;
  const text = value?.warning || value?.error || '请查看运行日志';
  if (text.includes('API_KEY_COUNT_EXCEEDED')) return '上游 Key 数量已达上限；已有 Key 和路由保留，需要新 Key 的分组等待上游额度调整后再同步。';
  if (text.includes('API_KEY_CREATE_RATE_LIMITED')) return '上游暂时限制新增 Key，待同步分组将在后续周期继续处理。';
  const label = subject(value); return (label ? label + '：' : '') + text;
}
// Who a job warning or failure is about: member, upstream group (with name when known) or account.
function subject(value) {
  if (value?.email) return value.email;
  if (value?.user_id) return '成员 #' + value.user_id;
  if (value?.upstream_id) return value.name ? `「${value.name}」#${value.upstream_id}` : `分组 #${value.upstream_id}`;
  if (value?.account_id) return '账号 #' + value.account_id;
  return '';
}
function empty(message, hint, name = 'grid') {
  const node = el('div', 'empty-state'); node.append(icon(name), el('strong', '', message));
  if (hint) node.append(el('p', '', hint)); return node;
}
function toast(message, error = false) {
  const node = el('div', 'toast' + (error ? ' error' : ''));
  node.append(icon(error ? 'close' : 'check'), el('span', 'toast-message', message));
  const close = el('button', 'icon-button', '×'); close.setAttribute('aria-label', '关闭提示');
  close.addEventListener('click', () => node.remove()); node.append(close); $('#toasts').append(node);
  setTimeout(() => node.remove(), error ? 12000 : 7000);
}
async function api(path, options = {}) {
  const request = { credentials: 'same-origin', ...options };
  if (options.method && options.method !== 'GET') {
    request.headers = { 'Content-Type': 'application/json', 'X-CSRF-Token': state.csrf };
    request.body = JSON.stringify(options.body || {});
  }
  let response;
  try { response = await fetch('/api' + path, request); }
  catch { throw new Error('无法连接工作台，请检查服务是否正在运行。'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && state.ready) { state.ready = false; showAuth(false); }
    throw new Error(data.error || '请求未完成，请稍后重试');
  }
  return data;
}
async function action(work, button) {
  if (button) button.disabled = true;
  try { return await work(); } catch (error) { toast(error.message, true); }
  finally { if (button) button.disabled = false; }
}
function showAuth(setup) {
  state.setup = setup;
  $('#auth-screen').hidden = false; $('#workspace').hidden = true;
  $('#auth-title').textContent = setup ? '从这里，开始流畅' : '欢迎回来';
  $('#auth-description').textContent = setup ? '先为工作台设置一个独立密码，已有站点配置会自动保留。' : '输入工作台密码，继续管理你的连接。';
  $('#auth-confirm-wrap').hidden = !setup; $('#auth-confirm').required = setup;
  $('#auth-password').minLength = setup ? 10 : 1;
  $('#auth-password').autocomplete = setup ? 'new-password' : 'current-password';
  $('#auth-password').placeholder = setup ? '至少 10 个字符' : '请输入工作台密码';
  $('#auth-action').textContent = setup ? '创建工作台密码' : '进入工作台';
}
$('#auth-form').addEventListener('submit', async event => {
  event.preventDefault(); $('#auth-error').textContent = '';
  const password = $('#auth-password').value;
  if (state.setup && password !== $('#auth-confirm').value) { $('#auth-error').textContent = '两次输入的密码不一致'; return; }
  const button = $('.auth-submit'); button.disabled = true;
  try {
    const result = await api(state.setup ? '/auth/setup' : '/auth/login', { method: 'POST', body: { password } });
    state.csrf = result.csrf; $('#auth-form').reset(); await initialize();
  } catch (error) { $('#auth-error').textContent = error.message; }
  finally { button.disabled = false; }
});
$('#logout').addEventListener('click', () => action(async () => {
  await api('/auth/logout', { method: 'POST' }); location.reload();
}));

async function initialize() {
  const [overview, groups, users, quota, jobs] = await Promise.all([
    api('/overview'), api('/groups'), api('/users'), api('/quota'), api('/jobs')]);
  Object.assign(state, { overview, groups: groups.items, users: users.items, quota: quota.rule,
    receipts: quota.receipts, summaries: quota.summaries || [], jobs: jobs.items, ready: true });
  for (const job of state.jobs) state.knownJobs.set(job.id, job.status);
  state.jobHistory = new Map(); state.jobsExhausted = jobs.items.length < JOB_PAGE; rememberJobs(jobs.items);
  state.selected = new Set(state.quota.user_ids);
  $('#auth-screen').hidden = true; $('#workspace').hidden = false;
  $('#today').textContent = new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' });
  renderOverview(); renderSettings(); renderGroups(); renderUsers(users.updated); renderQuota(); renderJobs();
  navigate(); await loadLogs();
}
function navigate() {
  const key = location.hash.slice(1);
  state.page = pages[key] ? key : 'overview';
  for (const [name, title] of Object.entries(pages)) {
    $('#page-' + name).hidden = state.page !== name;
    $(`[data-page="${name}"]`).classList.toggle('active', state.page === name);
    if (state.page === name) $('#page-crumb').textContent = title;
  }
  document.title = pages[state.page] + ' · Flow';
  window.scrollTo(0, 0);
  if (state.ready && state.page === 'logs') action(loadLogs);
}
window.addEventListener('hashchange', navigate);

function renderOverview() {
  const { config, stats, connections, report } = state.overview;
  for (const name of ['active', 'models', 'inactive', 'keys']) $('#stat-' + name).textContent = number(stats[name], 0);
  $('#stat-visible').textContent = `上游可见 ${number(stats.visible, 0)} 个分组`;
  for (const side of ['upstream', 'target']) {
    $('#' + side + '-host').textContent = host(config[side + '_base']);
    const status = $('#' + side + '-status'), connection = connections[side];
    status.textContent = connection ? connection.ok ? '已连接' : '连接失败' : '未测试';
    status.className = 'connection-status' + (connection ? connection.ok ? ' ok' : ' error' : '');
  }
  $('#sync-cadence').textContent = `每 ${config.interval_minutes} 分钟`;
  $('#next-sync-time').textContent = config.sync_enabled ? date(state.overview.next_sync) : '前往连接配置开启';
  $('#schedule-status').textContent = config.sync_enabled ? '自动运行' : '已暂停';
  const messages = [];
  const keyLimited = (report.warnings || []).some(warning => String(warning?.warning || warning).includes('API_KEY_CREATE_RATE_LIMITED'));
  const keyCountLimited = (report.warnings || []).some(warning => String(warning?.warning || warning).includes('API_KEY_COUNT_EXCEEDED'));
  if (report.pending) messages.push(keyCountLimited ? `${report.pending} 个可用分组等待上游 Key，Key 数量已达上限；已有 Key 和路由保留，额度调整后自动继续同步。` : `${report.pending} 个可用分组等待上游 Key${keyLimited ? '，受上游创建频率限制' : ''}，后续周期会自动继续同步。`);
  if (report.errors?.length) messages.push(`${report.errors.length} 项同步异常，请查看任务详情与日志。`);
  if (report.warnings?.length) messages.push(...report.warnings.filter(warning => !report.pending || !/API_KEY_(CREATE_RATE_LIMITED|COUNT_EXCEEDED)/.test(String(warning?.warning || warning))).map(warningText));
  $('#sync-notice').hidden = messages.length === 0;
  $('#sync-notice').textContent = [...new Set(messages)].join(' ');
  renderStable(); renderRunning(); renderActivity(); renderCountdown();
}
const scoreMetrics = { price: '价格', rating: '评分', popularity: '人气', cache: '缓存', latency: '延迟', success: '成功率' };
const platformNames = { openai: 'OpenAI', deepseek: 'DeepSeek', zhipu: '智谱', kimi: 'Kimi', anthropic: 'Anthropic', grok: 'Grok', gemini: 'Gemini' };
const strategyNames = { low_cost: '最低价', best_source: '最优源站' };
const stableJobs = { low_cost: '/jobs/low-cost', best_source: '/jobs/best-source' };
function lowCostDecision(data) {
  if (data.decision === 'retained') return data.current_score_rank ? `当前渠道评分第 ${data.current_score_rank} 名，在允许范围内，继续保留。` : '当前渠道保持可用，继续保留。';
  if (data.switch_reason && reasons[data.switch_reason]) return `${reasons[data.switch_reason]}，已更换为评分最高的渠道。`;
  if (data.decision === 'selected') return '已选用综合评分最高的渠道。';
  return '';
}
function metricsText(metrics) {
  if (!metrics) return '';
  const parts = [`倍率 ${number(metrics.rate)}`];
  if (metrics.stars != null) parts.push(`${number(metrics.stars, 1)} 星`);
  if (metrics.popularity != null) parts.push(`人气 ${number(metrics.popularity * 100, 2)}%`);
  if (metrics.cache_hit_rate != null) parts.push(`缓存 ${number(metrics.cache_hit_rate * 100, 1)}%`);
  if (metrics.latency_ms != null) parts.push(`首字 ${number(metrics.latency_ms / 1000, 1)}s`);
  if (metrics.success_rate != null) parts.push(`成功率 ${number(metrics.success_rate * 100, 1)}%`);
  return parts.join(' · ');
}
function scoreBar(candidate, weights) {
  const bar = el('div', 'score-bar'), total = Object.values(weights || {}).reduce((sum, value) => sum + value, 0) || 1;
  for (const key of Object.keys(scoreMetrics)) {
    const segment = el('span', 'seg-' + key);
    segment.style.width = `${(weights?.[key] || 0) * (candidate.parts?.[key] || 0) / total * 100}%`;
    bar.append(segment);
  }
  bar.title = Object.entries(scoreMetrics).map(([key, label]) => `${label} ${number((candidate.parts?.[key] || 0) * 100, 0)}`).join(' · ');
  return bar;
}
function summaryCell(label, value, note, small = false) {
  const cell = el('div'); cell.append(el('span', 'small-label', label), el(small ? 'small' : 'strong', '', value));
  if (note !== undefined) cell.append(el('small', '', note));
  return cell;
}
// One card per preferred-group rule; cards are rebuilt on every poll, so remember which candidate lists are open.
function renderStable() {
  const root = $('#stable-panels'); root.replaceChildren();
  const entries = state.overview.stable_groups || [];
  if (!entries.length) {
    const panel = el('div', 'panel stable-empty'), text = el('div');
    const link = el('a', 'text-button', '前往连接配置添加'); link.href = '#settings';
    text.append(el('strong', '', '还没有优选分组'), el('p', '', '为任意平台添加「最低价」或「最优源站」分组，固定分组 ID，渠道自动切换。'));
    panel.append(icon('tag'), text, link); root.append(panel); return;
  }
  for (const entry of entries) root.append(stableCard(entry));
}
function stableCard({ key, rule, status: data = {}, schedule = {} }) {
  const lowCost = rule.strategy === 'low_cost', enabled = rule.enabled && state.overview.config.sync_accounts;
  const active = data.status === 'active';
  const panel = el('div', 'panel low-cost-panel' + (lowCost ? '' : ' best-source-panel'));
  panel.dataset.key = key;
  const heading = el('div', 'panel-heading'), copy = el('div'), title = el('h2');
  title.append(icon(lowCost ? 'tag' : 'shield'), el('span', '', rule.name), el('span', 'platform-tag', `${strategyNames[rule.strategy]} · ${platformNames[rule.platform] || rule.platform}`),
    badge(enabled ? (data.status || 'pending') : 'paused'));
  title.firstChild.classList.add('accent');
  const check = el('button', 'button'); check.type = 'button'; check.append(icon('refresh'), document.createTextNode('立即检测'));
  check.disabled = state.jobs.some(job => ['queued', 'running'].includes(job.status) && job.kind === rule.strategy);
  check.onclick = () => action(() => runJob(stableJobs[rule.strategy], { mode: 'ranking', platform: rule.platform }, job => openJob(job)));
  const summary = el('div', 'low-cost-summary'), rate = data.source_rate == null ? '—' : `${number(data.source_rate)} → ${number(data.local_rate)}`;
  const enableHint = data.local_group_id ? '自动管理已暂停，现有分组保持原状态' : '在连接配置中启用自动管理，并开启同步转发账号';
  const details = el('details', 'candidate-details'), list = el('div');
  details.open = state.openCandidates.has(key);
  details.ontoggle = () => details.open ? state.openCandidates.add(key) : state.openCandidates.delete(key);
  let note, label;
  if (lowCost) {
    const topN = rule.top_n, pool = (data.candidates || []).filter(row => row.eligible).length;
    summary.append(
      summaryCell('当前上游渠道', data.source_name || '等待首次检测', data.provider_rank ? `大厅 #${data.provider_rank} · 本地 #${data.local_group_id || '待创建'} · ${data.models?.length || 0} 个模型` : `从默认排序前 ${topN} 名中评分择优`),
      summaryCell('综合评分', active && data.score != null ? `${number(data.score, 1)} 分` : '—', active && data.score_rank ? `第 ${data.score_rank} / ${pool} 名 · 跌出前 ${1 + rule.rank_tolerance} 名时更换` : ''),
      summaryCell('适用倍率 · 上游 → 本地', rate),
      summaryCell('下一次检测', enabled ? `评分 ${date(schedule.next_rank)} · 可用性 ${date(schedule.next_health)}` : enableHint, undefined, true));
    note = reasons[data.reason] || lowCostDecision(data) || `对前 ${topN} 名综合评分，选用得分最高的渠道。`;
    label = `查看前 ${topN} 名候选评分`;
    renderScoredCandidates(list, data);
  } else {
    const keepTop = rule.keep_top;
    summary.append(
      summaryCell('当前上游渠道', data.source_name || '等待首次检测', data.local_group_id ? `本地 #${data.local_group_id} · ${data.models?.length || 0} 个模型` : 'providers 默认排序'),
      summaryCell('默认排序', active && data.provider_rank ? `#${data.provider_rank}` : '—', active ? `跌出前 ${keepTop} 名时更换` : ''),
      summaryCell('适用倍率 · 上游 → 本地', rate),
      summaryCell('下一次检测', enabled ? `检测 ${date(schedule.next_rank)} · 每 ${rule.check_minutes} 分钟` : enableHint, undefined, true));
    note = reasons[data.reason] || (data.switch_reason && reasons[data.switch_reason] ? `${reasons[data.switch_reason]}，已更换为默认排序第一个可用渠道。`
      : data.decision === 'retained' ? `当前渠道仍在默认排序前 ${keepTop} 名内，继续保留。` : `跟随 providers 默认排序第 1 名，不跌出前 ${keepTop} 名就保持。`);
    label = `查看默认排序前 ${keepTop} 名`;
    renderRankedCandidates(list, data);
  }
  if (!enabled) note = enableHint;
  copy.append(title, el('p', '', note)); heading.append(copy, check);
  const toggle = el('summary'); toggle.append(el('span', '', label), icon('chevron')); details.append(toggle, list);
  panel.append(heading, summary, details);
  return panel;
}
function renderScoredCandidates(root, data) {
  if ((data.candidates || []).some(row => row.parts)) {
    const legend = el('div', 'score-legend');
    for (const [key, label] of Object.entries(scoreMetrics)) {
      const item = el('span'); item.append(el('i', 'seg-' + key), document.createTextNode(`${label} ${data.weights?.[key] ?? 0}`)); legend.append(item);
    }
    root.append(legend);
  }
  for (const candidate of data.candidates || []) {
    const row = el('div', 'candidate-row' + (candidate.eligible ? '' : ' ineligible'));
    const name = el('span'); name.append(document.createTextNode(candidate.name || `分组 #${candidate.upstream_id}`),
      el('span', 'candidate-meta', candidate.eligible ? `大厅 #${candidate.provider_rank} · ${metricsText(candidate.metrics)}` : `大厅 #${candidate.provider_rank}`));
    if (candidate.eligible && candidate.parts) name.append(scoreBar(candidate, data.weights));
    const value = el('strong', 'candidate-score');
    if (candidate.eligible && candidate.score != null) value.append(document.createTextNode(`${number(candidate.score, 1)} 分`), el('small', '', `${number(candidate.source_rate)} 倍`));
    else value.textContent = candidate.eligible ? `${number(candidate.source_rate)} 倍` : reasons[candidate.reason] || '不可用';
    row.append(el('span', 'candidate-rank', candidate.score_rank ? `#${candidate.score_rank}` : '—'), name, value);
    if (candidate.upstream_id === data.upstream_id) row.classList.add('current');
    root.append(row);
  }
  if (!root.childNodes.length) root.append(el('p', 'field-help', '完成检测后显示候选渠道与综合评分。'));
}
function renderRankedCandidates(root, data) {
  for (const candidate of data.candidates || []) {
    const row = el('div', 'candidate-row' + (candidate.eligible ? '' : ' ineligible'));
    row.append(el('span', 'candidate-rank', `#${candidate.provider_rank}`), el('span', '', candidate.name || `分组 #${candidate.upstream_id}`),
      el('strong', 'candidate-score', candidate.eligible ? `${number(candidate.source_rate)} 倍` : reasons[candidate.reason] || '不可用'));
    if (candidate.upstream_id === data.upstream_id) row.classList.add('current');
    root.append(row);
  }
  if (!root.childNodes.length) root.append(el('p', 'field-help', '完成检测后显示默认排序的候选渠道。'));
}
function jobTitle(job) {
  const platform = job.result?.platform;
  return platform && stableJobs[job.kind] ? `${kinds[job.kind]} · ${platformNames[platform] || platform}` : kinds[job.kind];
}
function renderCountdown() {
  if (!state.overview) return;
  let text = '已暂停';
  if (state.overview.config.sync_enabled) {
    const seconds = Math.max(0, Math.floor(state.overview.next_sync - Date.now() / 1000));
    text = seconds ? `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}` : '即将同步';
  }
  $('#sync-countdown').textContent = text;
}
function renderRunning() {
  const active = state.jobs.filter(job => ['queued', 'running'].includes(job.status));
  $('#active-job').hidden = !active.length;
  if (active.length) {
    const progress = active[0].progress;
    const step = progress?.step ? ` · ${progress.step}${progress.total ? `（${progress.done}/${progress.total}）` : ''}` : '';
    $('#active-job-label').textContent = `${jobTitle(active[0])}${active[0].status === 'queued' ? '已排队' : '正在执行'}${step}${active.length > 1 ? ` · 另有 ${active.length - 1} 项任务` : ''}`;
    $('#active-job-detail').onclick = () => openJob(active[0]);
  }
  const syncing = active.some(job => ['sync', 'preview'].includes(job.kind));
  $('#sync-now').disabled = syncing; $('#preview-sync').disabled = syncing;
  $('#refresh-users').disabled = active.some(job => job.kind === 'refresh_users');
}
function renderActivity() {
  const root = $('#activity-list'); root.replaceChildren();
  if (!state.jobs.length) { root.append(empty('一切准备就绪', '开始首次同步，动态会出现在这里。', 'clock')); return; }
  for (const job of state.jobs.slice(0, 5)) {
    const row = el('div', 'activity-item'), circle = el('span', 'activity-icon');
    circle.append(icon(job.kind === 'connect' ? 'link' : job.kind.includes('users') ? 'users' : 'refresh'));
    const text = el('div', 'activity-text');
    const button = el('button', 'text-button', jobTitle(job)); button.onclick = () => openJob(job);
    text.append(button, el('p', '', summary(job)), el('small', '', timeAgo(job.finished || job.created)));
    row.append(circle, text); root.append(row);
  }
}
function visibleGroups() {
  const search = $('#group-search').value.trim().toLowerCase();
  const rank = { active: 0, pending: 1, inactive: 2, excluded: 3 };
  return state.groups.filter(group => (state.groupFilter === 'all' || group.status === state.groupFilter) &&
    (!search || [group.name, group.platform, group.upstream_id, ...(group.models || [])].join(' ').toLowerCase().includes(search)))
    .sort((a, b) => (rank[a.status] ?? 4) - (rank[b.status] ?? 4) || (a.sync_order ?? 100000) - (b.sync_order ?? 100000) || a.upstream_id - b.upstream_id);
}
function renderGroups() {
  const rows = visibleGroups(), size = 10, pages = Math.max(1, Math.ceil(rows.length / size));
  state.groupPage = Math.min(state.groupPage, pages - 1);
  $('#group-total').textContent = state.groups.length;
  const root = $('#group-rows'); root.replaceChildren();
  if (!rows.length) {
    const row = el('tr'), td = el('td'); td.colSpan = 5;
    td.append(empty(state.groups.length ? '没有匹配的分组' : '让连接开始流动', state.groups.length ? '试试其他关键词或状态。' : '保存站点配置，然后点击「立即同步」。'));
    row.append(td); root.append(row);
  }
  for (const group of rows.slice(state.groupPage * size, (state.groupPage + 1) * size)) {
    const row = el('tr'), identity = el('td'), name = el('div', 'name-cell');
    const platform = (group.platform || 'api').toLowerCase();
    name.append(el('span', 'platform-icon ' + (['anthropic', 'gemini'].includes(platform) ? platform : ''),
      platform === 'anthropic' ? 'A' : platform === 'gemini' ? '✧' : '◎'));
    const copy = el('div'); const title = el('span', 'group-name', group.name || `分组 #${group.upstream_id}`);
    title.title = group.name || ''; copy.append(title, el('small', 'group-source', `${platform.toUpperCase()} · 上游 #${group.upstream_id}${group.local_group_id ? ` / 本地 #${group.local_group_id}` : ''}`));
    name.append(copy); identity.append(name); const status = el('td'); status.append(badge(group.status));
    const rate = el('td', 'rate'); rate.append(el('span', '', number(group.source_rate)), el('span', 'arrow', '→'), el('strong', '', number(group.local_rate)));
    const models = el('td'), model = el('button', 'model-button', `${group.models?.length || 0} 个模型`);
    model.onclick = () => openGroup(group); models.append(model);
    const more = el('td'), detail = el('button', 'icon-button'); detail.append(icon('chevron'));
    detail.setAttribute('aria-label', `查看 ${group.name || group.upstream_id} 详情`); detail.onclick = () => openGroup(group); more.append(detail);
    row.append(identity, status, rate, models, more); root.append(row);
  }
  $('#group-pagination-info').textContent = rows.length ? `显示 ${state.groupPage * size + 1}–${Math.min(rows.length, (state.groupPage + 1) * size)} / ${rows.length} 个分组` : '暂无匹配分组';
  $('#groups-prev').disabled = state.groupPage === 0; $('#groups-next').disabled = state.groupPage >= pages - 1;
}
$('#group-search').addEventListener('input', () => { state.groupPage = 0; renderGroups(); });
$('#group-filters').addEventListener('click', event => {
  const button = event.target.closest('[data-filter]'); if (!button) return;
  state.groupFilter = button.dataset.filter; state.groupPage = 0;
  $$('#group-filters button').forEach(item => item.classList.toggle('active', item === button)); renderGroups();
});
$('#groups-prev').onclick = () => { state.groupPage--; renderGroups(); };
$('#groups-next').onclick = () => { state.groupPage++; renderGroups(); };
function addMeta(root, title, value, wide = false) {
  const entry = el('div', wide ? 'wide' : ''); entry.append(el('dt', '', title));
  const text = el('dd'); text.append(value instanceof Node ? value : document.createTextNode(String(value ?? '—')));
  entry.append(text); root.append(entry);
}
function openGroup(group) {
  state.currentGroup = group; $('#group-detail-name').textContent = group.name || `分组 #${group.upstream_id}`;
  const meta = $('#group-detail-meta'); meta.replaceChildren();
  addMeta(meta, '当前状态', badge(group.status)); addMeta(meta, '平台', (group.platform || '—').toUpperCase());
  if (group.provider_rank) addMeta(meta, '大厅默认排名', `#${group.provider_rank} · 综合评分 ${number(group.provider_score, 2)}`);
  if (group.stats) addMeta(meta, '大厅近 1 小时指标', group.stats, true);
  addMeta(meta, '上游 / 本地分组', `#${group.upstream_id} / ${group.local_group_id ? '#' + group.local_group_id : '尚未创建'}`);
  addMeta(meta, '倍率（上游 → 本地）', `${number(group.source_rate)} → ${number(group.local_rate)}`);
  addMeta(meta, '上游 Key', group.key_id ? `#${group.key_id} · 保留并复用` : '尚未创建');
  if (group.marker) addMeta(meta, '来源标记', group.marker, true);
  $('#group-detail-reason').hidden = !group.reason;
  $('#group-detail-reason').textContent = reasons[group.reason] || group.reason || '';
  $('#model-search').value = ''; renderModels(); $('#group-dialog').showModal();
}
function renderModels() {
  const models = state.currentGroup?.models || [], search = $('#model-search').value.trim().toLowerCase();
  $('#model-count').textContent = models.length;
  const root = $('#model-list'); root.replaceChildren();
  const filtered = models.filter(model => model.toLowerCase().includes(search));
  for (const model of filtered) root.append(el('span', 'model-tag', model));
  if (!filtered.length) root.append(el('p', 'empty-inline', models.length ? '没有匹配模型' : '暂无模型记录'));
}
$('#model-search').oninput = renderModels;

const numericSettings = new Set(['interval_minutes', 'rate_factor', 'account_concurrency', 'priority', 'timeout', 'max_new_keys', 'request_interval']);
// Editable fields of a preferred-group rule: [field, label, help, options].
const ruleFields = {
  low_cost: [
    ['name', '本地分组名称', '实际名称会自动追加当前上游，如「名称（A031-Pro）」', { type: 'text', maxlength: 100 }],
    ['top_n', '候选范围', '只对该平台默认排序前 N 名评分，1–100', { min: 1, max: 100, prefix: '前', unit: '名' }],
    ['score_minutes', '评分检测间隔', '重新评分并按排名决定是否更换', { min: 1, max: 1440, unit: '分钟' }],
    ['rank_tolerance', '允许排名下降', '当前渠道跌出前「1 + 此值」名才更换；0 表示始终用第 1 名', { min: 0, max: 50, unit: '名' }],
    ['health_minutes', '可用性检测间隔', '渠道关闭、无法访问或不健康时立即更换', { min: 1, max: 1440, unit: '分钟' }],
    ['retry_minutes', '失败重试间隔', '连接失败或等待上游 Key 时重试', { min: 1, max: 1440, unit: '分钟' }],
    ['exclude_keywords', '排除关键字', '上游分组名称包含任一关键字（不区分大小写）的渠道不参与评分；多个关键字用逗号分隔，最多 50 个。',
      { type: 'text', maxlength: 2600, placeholder: '留空不排除，如 测试, 公益, Azure', wide: true }],
  ],
  best_source: [
    ['name', '本地分组名称', '实际名称会自动追加当前上游', { type: 'text', maxlength: 100 }],
    ['check_minutes', '检测间隔', '每次检测同时检查排名与可用性', { min: 1, max: 1440, unit: '分钟' }],
    ['keep_top', '保持范围', '当前渠道跌出前 N 名才更换，1–100', { min: 1, max: 100, prefix: '前', unit: '名' }],
    ['retry_minutes', '失败重试间隔', '连接失败或等待上游 Key 时重试', { min: 1, max: 1440, unit: '分钟' }],
  ],
};
const weightFields = [['price', '价格倍率', '适用倍率越低越好，以候选中最便宜者为满分'], ['rating', '用户评分', '大厅星级，5 星为满分'],
  ['popularity', '评价数 / 人气', '近 1 小时使用占比，按对数与候选中最高者比较'], ['cache', '缓存命中率', '50% 为 0 分，100% 为满分'],
  ['latency', '首字延迟', '以候选中最快者为满分'], ['success', '成功率', '近 1 小时，80% 为 0 分，100% 为满分']];
function renderSettings() {
  const form = $('#settings-form'), config = state.overview.config;
  // A page newer than the running backend shows blank fields; say so instead of letting a save fail later.
  const missing = $$('[name]', form).filter(control => control.type !== 'password' && config[control.name] === undefined);
  $('#settings-version-notice').hidden = !missing.length && ['stable_groups', 'sync_platform_limits', 'price_sync_platforms'].every(key => config[key] !== undefined);
  for (const control of $$('[name]', form)) {
    if (control.type === 'checkbox') control.checked = config[control.name];
    else if (control.type === 'password') {
      control.value = ''; control.placeholder = config.secrets_set[control.name] ? '已保存 · 留空保持原值' : '请输入';
    } else control.value = Array.isArray(config[control.name]) ? config[control.name].join(', ') : config[control.name] ?? '';
  }
  renderPlatformLimits(config.sync_platform_limits || {});
  renderPricePlatforms(config.price_sync_platforms || []);
  $('#stable-rules').replaceChildren(...(config.stable_groups || []).map(rule => ruleCard(rule)));
  setSettingsDirty(false); renderAuthModes(); renderRuleState();
  for (const side of ['upstream', 'target']) {
    const result = state.overview.connections[side];
    $('#test-' + side + '-result').textContent = result ? result.message + (result.latency_ms ? ` · ${number(result.latency_ms / 1000, 2)} s` : '') : '保存的凭据不会回显';
  }
}
function renderPlatformLimits(limits) {
  const root = $('#platform-limits'); root.replaceChildren();
  for (const [platform, label] of Object.entries(platformNames)) {
    const row = el('div', 'platform-limit'), choice = el('label', 'provider-choice'), check = el('input');
    check.type = 'checkbox'; check.dataset.platform = platform; check.checked = platform in limits;
    choice.append(check, el('span', '', label));
    const count = el('span', 'input-with-unit'), input = el('input');
    input.type = 'number'; input.min = 0; input.max = 10000; input.required = true; input.dataset.platformLimit = platform;
    input.value = limits[platform] ?? 0; input.setAttribute('aria-label', `${label} 同步前 N 个分组，0 表示不限制`);
    count.append(el('span', 'unit-prefix', '前'), input, el('span', '', '个'));
    row.append(choice, count); root.append(row);
  }
}
function renderPricePlatforms(selected) {
  $('#price-platforms').replaceChildren(...Object.entries(platformNames).map(([platform, label]) => {
    const choice = el('label', 'provider-choice'), check = el('input');
    check.type = 'checkbox'; check.dataset.pricePlatform = platform; check.checked = selected.includes(platform);
    choice.append(check, el('span', '', label)); return choice;
  }));
}
function platformLimitsDraft() {
  return Object.fromEntries($$('[data-platform]', $('#platform-limits')).filter(check => check.checked)
    .map(check => [check.dataset.platform, Number($(`[data-platform-limit="${check.dataset.platform}"]`).value)]));
}
const ruleKey = rule => `${rule.strategy}:${rule.platform}`;
const ruleCards = () => $$('.stable-rule', $('#stable-rules'));
function ruleInput(field, value, options) {
  const input = el('input'); input.dataset.field = field;
  input.type = options.type || 'number'; input.required = field !== 'exclude_keywords';
  for (const key of ['min', 'max', 'maxlength', 'placeholder']) if (options[key] !== undefined) input.setAttribute(key, options[key]);
  input.value = Array.isArray(value) ? value.join(', ') : value ?? '';
  return input;
}
// A rule is a compact row (name, strategy · platform and its key numbers) that expands to show every parameter.
function ruleCard(rule, open = state.openRules.has(ruleKey(rule))) {
  const lowCost = rule.strategy === 'low_cost';
  const card = el('section', 'stable-rule ' + rule.strategy); card.dataset.strategy = rule.strategy; card.dataset.platform = rule.platform;
  const head = el('div', 'stable-rule-head'), toggle = el('button', 'stable-rule-toggle'); toggle.type = 'button';
  const mark = el('span', 'stable-rule-mark'), copy = el('span', 'stable-rule-copy'), chevron = icon('chevron');
  chevron.classList.add('stable-rule-chevron'); mark.append(icon(lowCost ? 'tag' : 'shield'));
  copy.append(el('strong', 'stable-rule-name', rule.name), el('small', 'stable-rule-meta'));
  toggle.append(chevron, mark, copy);
  toggle.onclick = () => setRuleOpen(card, !card.classList.contains('open'));
  const switchLabel = el('label', 'switch-label'), enabled = el('input');
  enabled.type = 'checkbox'; enabled.setAttribute('role', 'switch'); enabled.dataset.field = 'enabled'; enabled.checked = rule.enabled;
  switchLabel.append(el('span', '', '自动管理'), enabled, el('span', 'switch-track'));
  const remove = el('button', 'icon-button stable-rule-remove'); remove.type = 'button'; remove.title = '删除';
  remove.setAttribute('aria-label', `删除 ${rule.name}`); remove.append(icon('close'));
  remove.onclick = () => {
    card.remove(); state.openRules.delete(ruleKey(rule)); setSettingsDirty(true); renderRuleState();
    toast(`已移除「${$('[data-field="name"]', card).value || rule.name}」，保存配置后生效`);
  };
  const actions = el('div', 'stable-rule-actions'); actions.append(switchLabel, remove);
  head.append(toggle, actions);
  const body = el('div', 'stable-rule-body'), grid = el('div', 'form-grid four');
  body.append(el('p', 'stable-rule-intro', lowCost ? '对默认排序前 N 名做综合评分（价格为主），自动选用得分最高的渠道。' : '跟随 providers 默认排序的第 1 名，只要不跌出前 N 名就保持不换。'));
  for (const [field, label, help, options] of ruleFields[rule.strategy]) {
    const wrapper = el('label', 'field' + (options.wide ? ' span-2' : '')), input = ruleInput(field, rule[field], options);
    wrapper.append(document.createTextNode(label));
    if (options.unit) {
      const unit = el('span', 'input-with-unit');
      if (options.prefix) unit.append(el('span', 'unit-prefix', options.prefix));
      unit.append(input, el('span', '', options.unit)); wrapper.append(unit);
    } else wrapper.append(input);
    wrapper.append(el('small', '', help)); grid.append(wrapper);
  }
  body.append(grid);
  if (lowCost) {
    const detection = el('label', 'check-label'), check = el('input');
    check.type = 'checkbox'; check.dataset.field = 'require_detection'; check.checked = rule.require_detection;
    detection.append(check, document.createTextNode('仅选择模型检测通过的渠道'));
    const section = el('div', 'sub-section'), heading = el('div', 'sub-section-heading'), text = el('div');
    text.append(el('h3', '', '评分权重'), el('p', '', '每项先在候选中归一化为 0–1，再按权重合成 0–100 分；缺少数据的项按中间值 0.5 计算。'));
    const reset = el('button', 'button ghost small'); reset.type = 'button'; reset.append(icon('undo'), document.createTextNode('恢复默认'));
    reset.onclick = () => {
      for (const [key, value] of Object.entries(state.overview.low_cost_weight_defaults || {})) $(`[data-weight="${key}"]`, card).value = value;
      setSettingsDirty(true); renderRuleState();
    };
    heading.append(text, reset);
    const weights = el('div', 'form-grid three');
    for (const [key, label, help] of weightFields) {
      const wrapper = el('label', 'field weight-field'), unit = el('span', 'input-with-unit'), input = el('input');
      input.type = 'number'; input.min = 0; input.max = 100; input.step = 1; input.required = true; input.dataset.weight = key;
      input.value = rule.weights?.[key] ?? 0;
      unit.append(input, el('span', 'weight-share', '—')); unit.lastChild.dataset.weightShare = key;
      wrapper.append(document.createTextNode(label), unit, el('small', '', help)); weights.append(wrapper);
    }
    section.append(heading, weights); body.append(detection, section);
  }
  card.append(head, body); setRuleOpen(card, open);
  return card;
}
function setRuleOpen(card, open) {
  const key = ruleKey(card.dataset);
  card.classList.toggle('open', open); $('.stable-rule-body', card).hidden = !open;
  $('.stable-rule-toggle', card).setAttribute('aria-expanded', String(open));
  if (open) state.openRules.add(key); else state.openRules.delete(key);
}
function ruleSummary(card) {
  const value = field => $(`[data-field="${field}"]`, card)?.value ?? '';
  const parts = [`${strategyNames[card.dataset.strategy]} · ${platformNames[card.dataset.platform] || card.dataset.platform}`];
  if (card.dataset.strategy === 'low_cost') {
    parts.push(`前 ${value('top_n')} 名评分`, `允许下降 ${value('rank_tolerance')} 名`, `评分每 ${value('score_minutes')} 分钟`, `可用性每 ${value('health_minutes')} 分钟`);
    const keywords = value('exclude_keywords').split(/[,，、;；\n]+/).filter(item => item.trim()).length;
    if (keywords) parts.push(`排除 ${keywords} 个关键字`);
  } else parts.push(`保持前 ${value('keep_top')} 名`, `每 ${value('check_minutes')} 分钟检测`);
  return parts.join(' · ');
}
function rulesDraft() {
  return ruleCards().map(card => {
    const rule = { strategy: card.dataset.strategy, platform: card.dataset.platform };
    for (const input of $$('[data-field]', card)) {
      const field = input.dataset.field;
      if (input.type === 'checkbox') rule[field] = input.checked;
      else if (field === 'exclude_keywords') rule[field] = input.value.split(/[,，、;；\n]+/).map(item => item.trim()).filter(Boolean);
      else rule[field] = input.type === 'number' ? Number(input.value) : input.value;
    }
    if (rule.strategy === 'low_cost') rule.weights = Object.fromEntries($$('[data-weight]', card).map(input => [input.dataset.weight, Number(input.value)]));
    return rule;
  });
}
// Row titles and summaries, weight shares, the count input next to each platform, and the empty state.
function renderRuleState() {
  const cards = ruleCards();
  for (const card of cards) {
    $('.stable-rule-name', card).textContent = $('[data-field="name"]', card).value.trim() || '未命名分组';
    $('.stable-rule-meta', card).textContent = ruleSummary(card);
    card.classList.toggle('off', !$('[data-field="enabled"]', card).checked);
    const inputs = $$('[data-weight]', card), total = inputs.reduce((sum, input) => sum + Math.max(0, Number(input.value) || 0), 0);
    for (const input of inputs) $(`[data-weight-share="${input.dataset.weight}"]`, card).textContent = total ? `${Math.round(Math.max(0, Number(input.value) || 0) / total * 100)}%` : '—';
  }
  for (const check of $$('[data-platform]', $('#platform-limits'))) $(`[data-platform-limit="${check.dataset.platform}"]`).disabled = !check.checked;
  $('#stable-count').textContent = cards.length;
  if (!cards.length) $('#stable-rules').replaceChildren(el('p', 'empty-inline', '还没有优选分组，点击右上角「添加优选分组」开始。'));
  else $$('.empty-inline', $('#stable-rules')).forEach(node => node.remove());
}
// A collapsed rule with an invalid field would block saving without any visible message.
$('#settings-form').addEventListener('invalid', event => {
  const card = event.target.closest('.stable-rule');
  if (card && !card.classList.contains('open')) setRuleOpen(card, true);
}, true);
function stableDialogChoices() {
  const form = $('#stable-form'), strategy = $('[name="stable-strategy"]:checked', form).value;
  const taken = new Set(ruleCards().map(card => ruleKey(card.dataset)));
  const previous = $('[name="stable-platform"]:checked', form)?.value;
  const root = $('#stable-platforms'); root.replaceChildren();
  for (const [platform, label] of Object.entries(platformNames)) {
    const used = taken.has(`${strategy}:${platform}`), choice = el('label', 'provider-choice'), input = el('input');
    input.type = 'radio'; input.name = 'stable-platform'; input.value = platform; input.disabled = used;
    choice.append(input, el('span', '', used ? `${label} · 已添加` : label)); root.append(choice);
  }
  const available = $$('[name="stable-platform"]:not(:disabled)', root);
  const selected = available.find(input => input.value === previous) || available[0];
  if (selected) selected.checked = true;
  $('#stable-confirm').disabled = !selected;
  $('#stable-dialog-hint').textContent = selected ? '添加后可展开调整参数，保存配置后生效。' : `所有平台都已添加「${strategyNames[strategy]}」分组。`;
  if (selected && !state.stableNameEdited) $('#stable-name').value = `${strategyNames[strategy]} ${platformNames[selected.value]}`;
}
$('#stable-add').onclick = () => {
  state.stableNameEdited = false; $('#stable-name').value = '';
  stableDialogChoices(); $('#stable-dialog').showModal();
};
$('#stable-form').onchange = event => { if (event.target.type === 'radio') stableDialogChoices(); };
$('#stable-name').oninput = () => { state.stableNameEdited = true; };
$('#stable-form').onsubmit = event => {
  event.preventDefault();
  const form = $('#stable-form'), strategy = $('[name="stable-strategy"]:checked', form).value;
  const platform = $('[name="stable-platform"]:checked', form)?.value, name = $('#stable-name').value.trim();
  if (!platform) return;
  if (!name) { toast('请填写本地分组名称', true); return; }
  if (ruleCards().some(card => $('[data-field="name"]', card).value.trim() === name)) { toast('本地分组名称与其他优选分组重复，请换一个名称', true); return; }
  const defaults = structuredClone(state.overview.stable_defaults?.[strategy] || {});
  const card = ruleCard({ strategy, platform, enabled: true, name, ...defaults }, false);
  $('#stable-rules').append(card); setSettingsDirty(true); renderRuleState(); $('#stable-dialog').close();
  card.classList.add('added'); setTimeout(() => card.classList.remove('added'), 1600);
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });
  toast(`已添加「${name}」，保存配置后生效`);
};
function renderAuthModes() {
  $$('[data-auth]').forEach(node => {
    const [side, mode] = node.dataset.auth.split(':');
    node.hidden = $(`[name="${side}_auth_mode"]`).value !== mode;
  });
}
function settingsDraft(side) {
  const result = {};
  for (const control of $$('[name]', $('#settings-form'))) {
    if (side && !control.name.startsWith(side + '_')) continue;
    if (control.type === 'checkbox') result[control.name] = control.checked;
    else if (numericSettings.has(control.name)) result[control.name] = Number(control.value);
    else if (control.name === 'group_ids') {
      result.group_ids = control.value.trim() ? control.value.split(/[,，\s]+/).filter(Boolean).map(Number) : [];
      if (result.group_ids.some(id => !Number.isInteger(id) || id <= 0)) throw new Error('分组 ID 请填写正整数，以逗号分隔。');
    } else result[control.name] = control.value;
  }
  if (!side) {
    result.sync_platform_limits = platformLimitsDraft(); result.stable_groups = rulesDraft();
    result.price_sync_platforms = $$('[data-price-platform]').filter(check => check.checked).map(check => check.dataset.pricePlatform);
  }
  return result;
}
function setSettingsDirty(dirty) {
  state.settingsDirty = dirty;
  $('#settings-dirty').textContent = dirty ? '有未保存的更改' : '已保存';
  $('#settings-save-bar').classList.toggle('dirty', dirty);
  $('#settings-discard').disabled = !dirty;
  $('#nav-settings-dirty').hidden = !dirty;
}
$('#settings-form').addEventListener('input', () => { setSettingsDirty(true); renderAuthModes(); renderRuleState(); });
$('#settings-discard').onclick = () => { renderSettings(); toast('已恢复为上次保存的配置'); };
$$('[data-section]').forEach(link => link.addEventListener('click', event => {
  event.preventDefault(); $(link.getAttribute('href')).scrollIntoView({ behavior: 'smooth', block: 'start' });
}));
$('#settings-form').addEventListener('submit', event => {
  event.preventDefault(); action(async () => {
    const previousTarget = state.overview.config.target_base;
    state.overview.config = await api('/config', { method: 'PUT', body: settingsDraft() });
    state.overview = await api('/overview'); renderSettings(); renderOverview();
    const [groups, users, quota] = await Promise.all([api('/groups'), api('/users'), api('/quota')]);
    state.groups = groups.items; state.users = users.items; state.quota = quota.rule;
    const targetChanged = previousTarget !== state.overview.config.target_base;
    if (targetChanged) state.selected = new Set(state.quota.user_ids);
    renderGroups(); renderUsers(users.updated); if (targetChanged || !state.quotaDirty) renderQuota(); toast('连接与同步设置已保存');
  }, $('button[form="settings-form"]'));
});
$$('[data-test]').forEach(button => button.onclick = () => action(async () => {
  const side = button.dataset.test; $('#test-' + side + '-result').textContent = '正在验证凭据与接口权限…';
  await runJob('/jobs/connect', { side, settings: settingsDraft(side) }, job => {
    $('#test-' + side + '-result').textContent = job.result?.message ? `${job.result.message} · ${number(job.result.latency_ms / 1000, 2)} s` : job.error || '测试未完成';
  });
}, button));

async function runJob(path, body = {}, onDone) {
  const data = await api(path, { method: 'POST', body });
  state.watched.set(data.job_id, onDone || null); toast('任务已提交，可在运行日志中查看进度');
  await refresh(); return data.job_id;
}
$('#sync-now').onclick = () => action(() => runJob('/jobs/sync'));
$('#preview-sync').onclick = () => action(() => runJob('/jobs/sync', { preview: true }, job => openJob(job)));
$('#refresh-users').onclick = () => action(() => runJob('/jobs/refresh-users'));

function filteredUsers() {
  const search = $('#user-search').value.toLowerCase().trim();
  return state.users.filter(user => [user.email, user.username, user.id].join(' ').toLowerCase().includes(search));
}
function renderUsers(updated) {
  const root = $('#user-rows'); root.replaceChildren();
  $('#user-total').textContent = state.users.length;
  if (updated) $('#users-updated').textContent = `最近读取 ${date(updated)} · 余额以刷新时为准`;
  const users = filteredUsers(), size = 12, pages = Math.max(1, Math.ceil(users.length / size));
  state.userPage = Math.min(state.userPage, pages - 1);
  const shown = users.slice(state.userPage * size, (state.userPage + 1) * size);
  if (!shown.length) {
    const row = el('tr'), td = el('td'); td.colSpan = 4;
    td.append(empty(state.users.length ? '没有找到这位成员' : '团队，从一位成员开始', state.users.length ? '试试姓名或邮箱。' : '刷新读取已有成员，或批量创建员工账号。', 'users'));
    row.append(td); root.append(row);
  }
  for (const user of shown) {
    const row = el('tr'), checkCell = el('td', 'checkbox-cell'), check = el('input');
    check.type = 'checkbox'; check.checked = state.selected.has(user.id);
    check.setAttribute('aria-label', '选择 ' + user.email);
    check.onchange = () => { check.checked ? state.selected.add(user.id) : state.selected.delete(user.id); markQuotaDirty(); renderSelection(); };
    checkCell.append(check); const cell = el('td'), person = el('div', 'member-identity');
    const name = user.username || user.email?.split('@')[0] || String(user.id);
    person.append(el('span', 'member-avatar', name[0].toUpperCase()));
    const copy = el('div'); copy.append(el('strong', '', name), el('small', '', user.email)); person.append(copy); cell.append(person);
    const balance = el('td', 'balance-number', '$ ' + number(user.balance, 6));
    const role = el('td'); role.append(badge(user.status === 'active' ? 'active' : 'inactive'), el('span', 'role-label', user.role === 'admin' ? '管理员' : '员工'));
    row.append(checkCell, cell, balance, role); root.append(row);
  }
  $('#user-page-info').textContent = users.length ? `显示 ${state.userPage * size + 1}–${Math.min(users.length, (state.userPage + 1) * size)} / ${users.length} 位成员` : '暂无成员';
  $('#users-prev').disabled = state.userPage === 0; $('#users-next').disabled = state.userPage >= pages - 1;
  renderSelection(); renderBatchHistory();
}
function renderSelection() {
  $('#selection-count').textContent = `已选择 ${state.selected.size} 位成员`;
  const users = filteredUsers().slice(state.userPage * 12, (state.userPage + 1) * 12);
  const count = users.filter(user => state.selected.has(user.id)).length;
  $('#select-visible-users').checked = users.length > 0 && count === users.length;
  $('#select-visible-users').indeterminate = count > 0 && count < users.length;
}
$('#user-search').oninput = () => { state.userPage = 0; renderUsers(); };
$('#users-prev').onclick = () => { state.userPage--; renderUsers(); };
$('#users-next').onclick = () => { state.userPage++; renderUsers(); };
$('#select-visible-users').onchange = event => {
  filteredUsers().slice(state.userPage * 12, (state.userPage + 1) * 12).forEach(user => event.target.checked ? state.selected.add(user.id) : state.selected.delete(user.id));
  markQuotaDirty(); renderUsers();
};
$('#select-employees').onclick = () => { state.users.filter(user => user.role !== 'admin').forEach(user => state.selected.add(user.id)); markQuotaDirty(); renderUsers(); };
$('#clear-selection').onclick = () => { state.selected.clear(); markQuotaDirty(); renderUsers(); };
function renderQuota() {
  const rule = state.quota;
  $('#quota-enabled').checked = rule.enabled; $('#quota-amount').value = rule.amount;
  $('#quota-scope').value = rule.scope; $('#quota-timezone').value = rule.timezone;
  $('#quota-next').textContent = rule.enabled ? `${date(rule.next_run_at, rule.timezone, true)} · ${rule.timezone}` : '尚未启用';
  state.quotaDirty = false; $('#quota-dirty').hidden = true;
}
function markQuotaDirty() { state.quotaDirty = true; $('#quota-dirty').hidden = false; }
$('#quota-form').oninput = markQuotaDirty;
function quotaDraft() {
  return { enabled: $('#quota-enabled').checked, amount: Number($('#quota-amount').value),
    scope: $('#quota-scope').value, timezone: $('#quota-timezone').value.trim(),
    user_ids: [...state.selected] };
}
$('#quota-form').addEventListener('submit', event => {
  event.preventDefault(); action(async () => {
    state.quota = await api('/quota', { method: 'PUT', body: quotaDraft() });
    state.selected = new Set(state.quota.user_ids); renderQuota(); renderSelection();
    toast(state.quota.enabled ? '每月余额重置已启用，下次执行时间已更新' : '重置规则已保存，当前处于关闭状态');
  }, $('button[type="submit"]', $('#quota-form')));
});
$('#reset-now').onclick = () => {
  const rule = quotaDraft();
  if (!Number.isFinite(rule.amount) || rule.amount < 0) { toast('请输入有效的重置金额', true); return; }
  const users = state.users.filter(user => rule.scope === 'all_members' || (rule.scope === 'all_employees' ? user.role !== 'admin' : rule.user_ids.includes(user.id)));
  if (!users.length) { toast('请先刷新成员，并选择需要重置的账号。', true); return; }
  state.resetRule = rule;
  state.resetRequestKey = requestKey();
  const root = $('#reset-review'); root.replaceChildren(el('p', 'muted small', `将${{ all_members: '所有', all_employees: '所有非管理员', selected: '已选择的' }[rule.scope]} ${users.length} 位成员的余额设为`),
    el('div', 'reset-amount', '$ ' + number(rule.amount, 6)), el('p', 'notice', '本次操作直接覆盖当前余额。确认后立即执行，每月自动重置规则另行保存。'));
  for (const user of users.slice(0, 8)) {
    const person = el('p', 'reset-person'); person.append(el('span', '', user.email + (user.role === 'admin' ? ' · 管理员' : '')), el('span', 'balance-number', '当前 $ ' + number(user.balance, 6)));
    root.append(person);
  }
  if (users.length > 8) root.append(el('p', 'reset-person', `另有 ${users.length - 8} 位成员`));
  root.append(el('p', 'field-help reset-note', '重置前会逐一记录成员的剩余余额、本期用量和主要模型，完成后可在「重置用量总结」中查看。'));
  $('#confirm-dialog').showModal();
};
$('#confirm-reset').onclick = () => action(async () => {
  await runJob('/jobs/reset-balances', { confirm: true, rule: state.resetRule, request_key: state.resetRequestKey }); $('#confirm-dialog').close();
}, $('#confirm-reset'));

function parseCsv(text) {
  const delimiter = text.includes('\t') ? '\t' : ',', rows = []; let row = [], field = '', quoted = false;
  text = text.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n');
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '"') { if (quoted && text[i + 1] === '"') { field += '"'; i++; } else quoted = !quoted; }
    else if (c === delimiter && !quoted) { row.push(field.trim()); field = ''; }
    else if (c === '\n' && !quoted) { row.push(field.trim()); if (row.some(Boolean)) rows.push(row); row = []; field = ''; }
    else field += c;
  }
  if (quoted) throw new Error('名单中有未闭合的双引号');
  row.push(field.trim()); if (row.some(Boolean)) rows.push(row);
  if (rows.length && /^(email|邮箱)$/i.test(rows[0][0])) rows.shift(); return rows;
}
function batchRows() {
  let rows;
  const commonPassword = $('#batch-password').value;
  if (state.batchMode === 'paste') rows = parseCsv($('#batch-text').value).map(row => ({ email: (row[0] || '').toLowerCase(), username: row[1] || '', password: row[2] || commonPassword }));
  else {
    const count = Number($('#batch-count').value), start = Number($('#batch-start').value);
    if (!Number.isInteger(count) || count < 1 || count > 200 || !Number.isInteger(start) || start < 0) throw new Error('生成数量为 1–200，起始序号为非负整数');
    const prefix = $('#batch-prefix').value.trim(), domain = $('#batch-domain').value.trim();
    rows = Array.from({ length: count }, (_, i) => ({ email: `${prefix}${start + i}@${domain}`.toLowerCase(), username: `${prefix}${start + i}`, password: commonPassword }));
  }
  if (!rows.length || rows.length > 200) throw new Error('每批请填写 1–200 位成员');
  const seen = new Set();
  for (const row of rows) {
    if (!/^[^\s@,]+@[^\s@,]+\.[^\s@,]+$/.test(row.email)) throw new Error(`邮箱格式无效：${row.email || '空邮箱'}`);
    if (seen.has(row.email)) throw new Error(`名单有重复邮箱：${row.email}`);
    if (row.password && (row.password.length < 8 || row.password.length > 128)) throw new Error(`密码长度需要 8–128 位：${row.email}`);
    seen.add(row.email);
  }
  return rows;
}
function previewBatch() {
  const root = $('#batch-preview-rows'); root.replaceChildren(); $('#batch-error').textContent = '';
  try {
    const rows = batchRows(); $('#batch-preview-count').textContent = rows.length;
    for (const row of rows.slice(0, 30)) {
      const item = el('div', 'batch-preview-row'); item.append(el('span', '', row.email), el('span', '', row.username || '—'), el('span', '', row.password ? '自定义密码' : '随机密码')); root.append(item);
    }
    if (rows.length > 30) root.append(el('p', 'empty-inline', `另有 ${rows.length - 30} 位成员`)); return rows;
  } catch (error) { $('#batch-preview-count').textContent = '0'; $('#batch-error').textContent = error.message; return null; }
}
$('#open-batch').onclick = () => {
  const groups = $('#batch-groups'); groups.replaceChildren();
  for (const group of state.groups.filter(group => group.status === 'active' && group.local_group_id)) {
    const option = el('option', '', `#${group.local_group_id} · ${group.name}`); option.value = group.local_group_id; groups.append(option);
  }
  // Stable groups first: their IDs never change, so they suit long-lived employee access.
  for (const { status: stable } of [...(state.overview.stable_groups || [])].reverse()) {
    if (stable?.status === 'active' && stable.local_group_id) {
      const option = el('option', '', `#${stable.local_group_id} · ${stable.name}`);
      option.value = stable.local_group_id; groups.prepend(option);
    }
  }
  state.batchKey = requestKey(); $('#batch-dialog').showModal();
};
$('#batch-modes').onclick = event => {
  const button = event.target.closest('[data-mode]'); if (!button) return;
  state.batchMode = button.dataset.mode;
  $$('#batch-modes button').forEach(item => item.classList.toggle('active', item === button));
  $('#batch-paste').hidden = state.batchMode !== 'paste'; $('#batch-generate').hidden = state.batchMode !== 'generate';
  $('#batch-error').textContent = ''; $('#batch-preview-rows').replaceChildren(el('p', 'empty-inline', '填写信息后，点击更新预览。'));
};
$('#batch-preview').onclick = previewBatch;
$('#batch-form').oninput = () => { state.batchKey = requestKey(); };
$('#batch-form').addEventListener('submit', event => {
  event.preventDefault(); const users = previewBatch(); if (!users) return;
  action(async () => {
    await runJob('/jobs/batch-users', { users, balance: Number($('#batch-balance').value), concurrency: Number($('#batch-concurrency').value),
      allowed_groups: [...$('#batch-groups').selectedOptions].map(option => Number(option.value)), request_key: state.batchKey }, job => openJob(job));
    $('#batch-dialog').close(); $('#batch-text').value = ''; $('#batch-password').value = '';
  }, $('button[type="submit"]', $('#batch-form')));
});
function credentialLink(job) {
  const link = el('a', 'button', '下载账号凭据'); link.prepend(icon('download'));
  link.href = `/api/jobs/${job.id}/credentials.csv`; link.download = `flow-members-${job.id.slice(0, 8)}.csv`; return link;
}
function renderBatchHistory() {
  const root = $('#batch-history'); root.replaceChildren();
  const jobs = state.jobs.filter(job => job.kind === 'batch_users'); if (!jobs.length) return;
  root.append(el('h3', '', '最近创建批次'));
  for (const job of jobs.slice(0, 4)) {
    const row = el('div', 'batch-record'), text = el('div');
    text.append(el('strong', '', summary(job)), el('small', '', date(job.created))); row.append(text);
    if (job.export_available || job.result?.export_available) row.append(credentialLink(job));
    else { const button = el('button', 'text-button', '查看详情'); button.onclick = () => openJob(job); row.append(button); }
    root.append(row);
  }
}
function summary(job) {
  const result = job.result;
  if (!result) return job.error || statuses[job.status] || job.status;
  if (['low_cost', 'best_source'].includes(job.kind) && (result.pending || result.decision === 'paused')) {
    return reasons[result.reason] || '保留现有分组，等待后续检测';
  }
  if (job.kind === 'connect') return result.message;
  if (job.kind === 'best_source') return result.pending ? '候选渠道等待上游 Key' : result.status === 'active' ? `${result.source_name} · 默认排序 #${result.provider_rank} · ${number(result.source_rate)} 倍 · ${result.decision === 'retained' ? '保持当前渠道' : '已更新渠道'}` : reasons[result.reason] || '检测完成';
  if (job.kind === 'low_cost') return result.pending ? '候选渠道等待上游 Key' : result.status === 'active' ? `${result.source_name} · ${result.score != null ? `${number(result.score, 1)} 分 · ` : ''}${number(result.source_rate)} 倍 · ${result.decision === 'retained' ? '保持当前渠道' : result.switch_reason === 'rank_dropped' ? '评分排名下降，已更换渠道' : '已更新渠道'}` : reasons[result.reason] || '检测完成';
  if (['sync', 'preview'].includes(job.kind)) return `${result.usable_groups} 个可用分组 · ${result.usable_models} 个模型绑定${result.pending ? ` · ${result.pending} 组待同步` : ''}${result.concurrency_fixed ? ` · 修正 ${result.concurrency_fixed} 个账号并发` : ''}${result.pricing_updated ? ` · 更新 ${result.pricing_updated} 个分组价格` : ''}${result.billing_refreshed ? ` · ${result.billing_refreshed} 个保留分组的计费已按上游刷新` : ''}`;
  if (job.kind === 'refresh_users') return `已读取 ${result.count} 位成员`;
  if (job.kind === 'batch_users') return `创建 ${result.created.length} 位 · 跳过 ${result.skipped.length} 位${result.failed ? ` · 失败 ${result.failed} 位` : ''}`;
  if (job.kind === 'quota_reset') return `已重置 ${result.succeeded.length} 位 · $${number(result.amount)}${result.failed ? ` · ${result.failed} 位待处理` : ''}${result.usage && result.usage.missing < result.usage.users ? ` · 本期消费 ${money(result.usage.cost)}` : ''}`;
  return statuses[job.status];
}
// Polling only returns the newest page; history keeps every job seen so older pages stay contiguous.
function rememberJobs(items) { for (const job of items) state.jobHistory.set(job.id, job); }
function jobHistory() { return [...state.jobHistory.values()].sort((a, b) => b.created - a.created); }
async function loadOlderJobs() {
  const oldest = jobHistory().at(-1); if (!oldest || state.loadingJobs) return;
  state.loadingJobs = true; renderJobs();
  try {
    const data = await api(`/jobs?before=${oldest.created}&limit=${JOB_PAGE}`);
    rememberJobs(data.items); if (data.items.length < JOB_PAGE) state.jobsExhausted = true;
  } finally { state.loadingJobs = false; renderJobs(); }
}
function renderJobs() {
  const root = $('#job-list'); root.replaceChildren();
  const history = jobHistory();
  if (!history.length) root.append(empty('还没有执行任务', '首次操作后，执行记录会显示在这里。', 'log'));
  for (const job of history) {
    const row = el('button', 'job-row'); row.type = 'button';
    row.append(icon(job.kind.includes('users') ? 'users' : job.kind === 'connect' ? 'link' : 'refresh'), el('span', 'job-name', jobTitle(job)), el('span', 'job-summary', summary(job)), el('span', 'job-date', date(job.created)), badge(job.status));
    row.onclick = () => openJob(job); root.append(row);
  }
  if (history.length && !state.jobsExhausted) {
    const more = el('button', 'text-button job-more', state.loadingJobs ? '加载中…' : `已显示 ${history.length} 条 · 加载更早的记录`);
    more.type = 'button'; more.disabled = state.loadingJobs; more.onclick = () => action(loadOlderJobs); root.append(more);
  } else if (history.length > JOB_PAGE) root.append(el('p', 'empty-inline', `已显示全部 ${history.length} 条记录`));
  renderActivity(); renderBatchHistory(); renderRunning(); renderReceipts(); renderResetHistory();
}
function openJob(job) {
  if (state.jobLog.id !== job.id) state.jobLog = { id: job.id, items: [], loaded: false, final: false, truncated: false };
  state.currentJob = job.id; renderJobDetail(job);
  if (!$('#job-dialog').open) $('#job-dialog').showModal();
  action(async () => { await loadJobLog(job); if ($('#job-dialog').open && state.currentJob === job.id) renderJobDetail(state.jobHistory.get(job.id) || job); });
}
const JOB_LOG_KEEP = 1000;
// Fetches the job's new log lines; once a finished job's lines are read nothing more can arrive.
async function loadJobLog(job) {
  const log = state.jobLog;
  if (log.id !== job.id || log.final || log.loading) return;
  const finished = !['queued', 'running'].includes(job.status);
  log.loading = true;
  try {
    const data = await api(`/jobs/${job.id}/logs?after=${log.items.at(-1)?.id || 0}`);
    if (!log.loaded && data.items.length >= data.limit) log.truncated = true;
    log.items.push(...data.items);
    if (log.items.length > JOB_LOG_KEEP) { log.items.splice(0, log.items.length - JOB_LOG_KEEP); log.truncated = true; }
    log.loaded = true; log.final = finished;
  } finally { log.loading = false; }
}
function duration(seconds) {
  seconds = Math.max(0, Math.floor(seconds));
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, '0')} 秒`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`;
}
function sinceLabel(start) { const node = el('span', '', duration(Date.now() / 1000 - start)); node.dataset.since = start; return node; }
function renderJobProgress(job) {
  const box = el('div', 'job-progress');
  if (job.status === 'queued') {
    const ahead = state.jobs.filter(other => other.id !== job.id && ['queued', 'running'].includes(other.status) && other.created < job.created).length;
    box.append(el('p', 'job-progress-step', ahead ? `排队中，前面还有 ${ahead} 项任务` : '排队中，即将开始'));
    return box;
  }
  const progress = job.progress || {};
  const step = el('p', 'job-progress-step'); step.append(el('span', 'spinner'), el('span', '', progress.step || '正在执行'));
  box.append(step);
  if (progress.total) {
    const bar = el('div', 'job-progress-bar'), fill = el('span');
    fill.style.width = `${Math.min(100, progress.done / progress.total * 100)}%`; bar.append(fill);
    const row = el('div', 'job-progress-count'); row.append(bar, el('span', '', `${progress.done} / ${progress.total}`)); box.append(row);
  }
  if (progress.request) box.append(el('p', 'job-progress-request', `等待响应 · ${progress.request}${progress.request_seconds >= 2 ? ` · 已等待 ${duration(progress.request_seconds)}` : ''}`));
  return box;
}
function renderJobLog(job) {
  const section = el('div', 'job-log-section'), log = state.jobLog;
  const heading = el('div', 'job-log-heading');
  heading.append(el('h3', '', '执行过程'), el('span', 'muted', log.truncated ? `仅显示最近 ${log.items.length} 条` : log.items.length ? `${log.items.length} 条` : ''));
  section.append(heading);
  const list = el('div', 'job-log');
  if (log.id !== job.id || !log.loaded) list.append(el('p', 'empty-inline', '正在读取执行日志…'));
  else {
    list.dataset.loaded = '1';
    if (!log.items.length) list.append(el('p', 'empty-inline', ['queued', 'running'].includes(job.status) ? '等待第一条日志…' : '没有执行日志，较早任务的日志可能已被清理'));
  }
  for (const line of log.id === job.id ? log.items : []) {
    const row = el('div', 'log-line');
    row.append(el('span', 'log-time', new Date(line.at * 1000).toLocaleTimeString('zh-CN', { hour12: false })), el('span', 'log-level ' + line.level, line.level), el('span', 'log-message', line.message));
    list.append(row);
  }
  section.append(list); return section;
}
function renderJobDetail(job) {
  $('#job-detail-title').textContent = jobTitle(job);
  const root = $('#job-detail-content');
  // Keep the reader's place in the log; follow new lines only when already at the bottom.
  const previous = $('.job-log', root), running = ['queued', 'running'].includes(job.status);
  const follow = previous?.dataset.loaded ? previous.scrollHeight - previous.scrollTop - previous.clientHeight < 24 : running;
  const scrollTop = previous?.scrollTop || 0;
  root.replaceChildren();
  const meta = el('dl', 'detail-meta');
  addMeta(meta, '执行状态', badge(job.status)); addMeta(meta, '提交时间', date(job.created, null, true));
  addMeta(meta, '开始时间', date(job.started));
  if (job.status === 'running' && job.started) addMeta(meta, '已运行', sinceLabel(job.started));
  else addMeta(meta, '完成时间', job.finished && job.started ? `${date(job.finished)} · 用时 ${duration(job.finished - job.started)}` : date(job.finished));
  root.append(meta);
  if (running) root.append(renderJobProgress(job));
  const detail = el('div', 'job-detail-lines'); if (!running) detail.append(el('p', '', summary(job)));
  if (job.error) detail.append(el('p', 'notice error', job.error));
  const result = job.result;
  if (result) {
    if (['low_cost', 'best_source'].includes(job.kind) && result.upstream_id) {
      detail.append(el('p', '', `上游 #${result.upstream_id} → 本地 #${result.local_group_id || '待创建'} · 默认排序 #${result.provider_rank || '—'}`));
      detail.append(el('p', '', `前 ${result.top_n || 10} 名中 ${result.candidates?.filter(row => row.eligible).length || 0} 个候选通过检测 · ${result.retained_keys || 0} 个上游 Key 保留复用`));
      if (result.score != null) detail.append(el('p', '', `综合评分 ${number(result.score, 1)} 分 · 评分第 ${result.score_rank || '—'} 名 · 允许下降 ${result.rank_tolerance ?? '—'} 名`));
      if (result.switch_reason && reasons[result.switch_reason]) detail.append(el('p', 'notice', `更换原因：${reasons[result.switch_reason]}`));
    }
    if (result.preview) detail.append(el('p', 'notice', '本次为预览，未创建或修改站点资源。'));
    if (result.pending) detail.append(el('p', 'notice', ['low_cost', 'best_source'].includes(job.kind)
      ? reasons[result.reason] || '保留现有分组，等待后续检测。'
      : '上游 Key 额度有限，待同步分组会在后续周期继续处理。'));
    for (const warning of result.warnings || []) detail.append(el('p', 'notice', warningText(warning)));
    if (result.period) detail.append(el('p', '', `执行周期：${result.period.startsWith('manual-') ? '手动重置' : result.period} · 跳过 ${result.skipped?.length || 0} 位已有执行记录的成员`));
    const failures = result.failures || result.errors || [];
    if (failures.length) {
      detail.append(el('h3', '', '需要关注的结果'));
      const list = el('ul'); for (const failure of failures) list.append(el('li', '', (subject(failure) ? subject(failure) + '：' : '') + failure.error)); detail.append(list);
    }
    if (job.kind === 'quota_reset' && result.usage) detail.append(summaryButton(job.id, 'button'));
    if (job.kind === 'batch_users') {
      for (const item of result.created || []) detail.append(el('p', '', '已创建 · ' + item.email));
      if (result.skipped?.length) detail.append(el('p', '', '已存在，跳过 · ' + result.skipped.join('、')));
      if (result.export_available) { detail.append(el('p', 'notice', '凭据文件包含初始登录密码，请保存在团队可信的位置。')); detail.append(credentialLink(job)); }
    }
  }
  if (job.kind === 'quota_reset' && job.status === 'running') detail.append(summaryButton(job.id, 'button', '查看已统计的成员'));
  root.append(detail);
  if (job.export_available && !result?.export_available) detail.append(credentialLink(job));
  root.append(renderJobLog(job));
  const list = $('.job-log', root); list.scrollTop = follow ? list.scrollHeight : scrollTop;
}
function renderReceipts() {
  const root = $('#receipt-list'); root.replaceChildren();
  if (!state.receipts.length) { root.append(el('p', 'empty-inline', '尚无余额重置记录')); return; }
  for (const receipt of state.receipts) {
    const row = el('div', 'receipt-row');
    const user = state.users.find(user => user.id === receipt.user_id);
    row.append(el('span', '', receipt.period.startsWith('manual-') ? '手动重置' : receipt.period),
      el('span', '', user?.email || '成员 #' + receipt.user_id), el('span', 'muted', receipt.error || date(receipt.updated)), badge(receipt.state)); root.append(row);
  }
}
// ---------- Reset usage summaries ----------
function money(value) { return value == null ? '—' : '$' + number(value, Math.abs(value) < 1 ? 4 : 2); }
function compact(value) { return value == null ? '—' : Number(value).toLocaleString('en-US', { notation: 'compact', maximumFractionDigits: 1 }); }
function resetLabel(period) { return !period || period.startsWith('manual-') ? '手动重置' : `${period} 每月重置`; }
function summaryButton(jobId, className = 'text-button', text = '查看用量总结') {
  const button = el('button', className, text); button.type = 'button';
  if (className === 'button') button.prepend(icon('users'));
  button.onclick = () => openSummary(jobId); return button;
}
function renderResetHistory() {
  const root = $('#reset-history'); root.replaceChildren();
  if (!state.summaries.length) return;
  root.append(el('h3', '', '重置用量总结'));
  for (const item of state.summaries.slice(0, 6)) {
    const row = el('div', 'batch-record'), text = el('div');
    const spent = item.missing >= item.users ? '用量暂不可用' : `本期消费 ${money(item.cost)}${item.missing ? `（${item.missing} 位未读取）` : ''}`;
    text.append(el('strong', '', `${resetLabel(item.period)} · ${item.users} 位成员`),
      el('small', '', `${date(item.at)} · ${spent} · 重置前剩余 ${money(item.balance)}`));
    row.append(text, summaryButton(item.job_id, 'text-button', '查看总结')); root.append(row);
  }
}
async function openSummary(jobId) {
  await action(async () => {
    const data = await api(`/quota/summaries/${jobId}`);
    state.summaryView = { ...data, search: '' };
    renderSummary(); if (!$('#summary-dialog').open) $('#summary-dialog').showModal();
  });
}
// A member's models with their share of the member's spend (or requests when nothing was charged).
function modelShares(models, total) {
  const byCost = total.actual_cost > 0, base = byCost ? total.actual_cost : total.requests;
  return (models || []).map(model => ({ ...model, share: base ? (byCost ? model.actual_cost : model.requests) / base : 0 }));
}
function shareText(share) { return share >= 0.995 ? '100%' : share < 0.01 ? '<1%' : `${Math.round(share * 100)}%`; }
function teamModels(items) {
  const models = new Map();
  for (const item of items) for (const model of item.models || []) {
    if (model.other) continue;
    const row = models.get(model.model) || { model: model.model, actual_cost: 0, requests: 0, total_tokens: 0, users: 0 };
    row.actual_cost += model.actual_cost; row.requests += model.requests; row.total_tokens += model.total_tokens; row.users++;
    models.set(model.model, row);
  }
  return [...models.values()].sort((a, b) => b.actual_cost - a.actual_cost || b.requests - a.requests);
}
function renderSummary() {
  const view = state.summaryView, total = view.summary, items = view.items;
  $('#summary-title').textContent = resetLabel(total.period);
  $('#summary-subtitle').textContent = `${date(total.at, null, true)} 执行 · 余额设为 ${money(total.amount)} · ${total.succeeded} / ${total.users} 位成员已重置`;
  const root = $('#summary-content'); root.replaceChildren();
  const zone = items.find(item => item.timezone)?.timezone;
  root.append(el('p', 'summary-window', `统计区间 ${total.since} 至 ${total.until}${zone ? `（${zone} 自然日）` : ''}，截至每位成员重置前`));
  const metrics = el('div', 'summary-metrics'), available = total.missing < total.users;
  for (const [label, value, hint] of [
    ['本期消费', available ? money(total.cost) : '—', '按实际扣费'],
    ['请求数', available ? number(total.requests, 0) : '—', `${total.users - total.missing} 位成员有用量记录`],
    ['Tokens', available ? compact(total.tokens) : '—', '输入、输出与缓存合计'],
    ['重置前剩余', money(total.balance), `${total.users} 位成员合计`]]) {
    const metric = el('div', 'summary-metric'); metric.append(el('span', '', label), el('strong', '', value), el('small', '', hint)); metrics.append(metric);
  }
  root.append(metrics);
  if (total.missing) {
    const reason = items.find(item => item.usage_error)?.usage_error;
    root.append(el('p', 'notice', `${total.missing} 位成员的用量未能读取${reason ? `（${reason}）` : ''}，只记录了重置前余额。`));
  }
  const models = teamModels(items).slice(0, 6);
  if (models.length) {
    const section = el('div', 'summary-section'); section.append(el('h3', '', '主要模型'));
    const byCost = models.some(model => model.actual_cost > 0), base = byCost ? total.cost : total.requests;
    for (const model of models) {
      const share = base ? (byCost ? model.actual_cost : model.requests) / base : 0;
      const row = el('div', 'summary-model'), bar = el('div', 'summary-bar'), fill = el('span');
      fill.style.width = `${Math.min(100, share * 100)}%`; bar.append(fill);
      row.append(el('span', 'summary-model-name', model.model), bar, el('span', 'summary-model-share', shareText(share)),
        el('span', 'summary-model-detail', `${money(model.actual_cost)} · ${number(model.requests, 0)} 次 · ${model.users} 位成员`));
      section.append(row);
    }
    root.append(section);
  }
  const heading = el('div', 'summary-table-heading'), search = el('label', 'search-field'), input = el('input');
  input.placeholder = '搜索成员或模型…'; input.setAttribute('aria-label', '搜索成员或模型'); input.value = view.search;
  input.oninput = () => { view.search = input.value; renderSummaryRows(); };
  search.append(icon('search'), input);
  const exportButton = el('button', 'button', '导出 CSV'); exportButton.type = 'button'; exportButton.prepend(icon('download')); exportButton.onclick = exportSummary;
  const tools = el('div', 'summary-tools'); tools.append(search, exportButton);
  heading.append(el('h3', '', '成员明细'), tools);
  const scroll = el('div', 'table-scroll summary-table-scroll'), table = el('table', 'summary-table'), head = el('thead'), headRow = el('tr');
  for (const title of ['成员', '重置前余额', '本期消费', '请求', 'Tokens', '主要模型', '结果']) headRow.append(el('th', '', title));
  head.append(headRow); const body = el('tbody'); body.id = 'summary-rows'; table.append(head, body); scroll.append(table);
  root.append(heading, scroll);
  renderSummaryRows();
}
function renderSummaryRows() {
  const view = state.summaryView, body = $('#summary-rows'); if (!body) return;
  const query = view.search.trim().toLowerCase();
  const items = view.items.filter(item => !query || [item.email, item.username, ...(item.models || []).map(model => model.model)].some(value => value?.toLowerCase().includes(query)));
  body.replaceChildren();
  if (!items.length) { const row = el('tr'), cell = el('td', 'empty-inline', '没有匹配的成员'); cell.colSpan = 7; row.append(cell); body.append(row); }
  for (const item of items) {
    const row = el('tr'), person = el('td'), identity = el('div', 'member-identity'), copy = el('div');
    const name = item.username || item.email?.split('@')[0] || `#${item.user_id}`;
    identity.append(el('span', 'member-avatar', name[0].toUpperCase()));
    copy.append(el('strong', '', name + (item.role === 'admin' ? ' · 管理员' : '')), el('small', '', item.email || `成员 #${item.user_id}`));
    identity.append(copy); person.append(identity);
    const spent = el('td', 'balance-number');
    if (item.usage) {
      spent.append(document.createTextNode(money(item.cost)));
      if (item.since !== view.summary.since) spent.append(el('small', 'summary-since', `自 ${item.since.slice(5)} 起`));
    } else { spent.textContent = '—'; spent.title = item.usage_error || '用量未读取'; }
    const models = el('td', 'summary-models');
    const shares = item.usage ? modelShares(item.models, item.usage) : [];
    if (!item.usage) models.append(el('span', 'muted', '暂不可用'));
    else if (!shares.length) models.append(el('span', 'muted', '本期无调用'));
    for (const model of shares.slice(0, 3)) models.append(el('span', 'model-tag', `${model.model} · ${shareText(model.share)}`));
    if (shares.length > 3) models.append(el('span', 'muted', `共 ${shares.length} 个`));
    models.title = shares.map(model => `${model.model}：${money(model.actual_cost)} · ${number(model.requests, 0)} 次 · ${compact(model.total_tokens)} tokens`).join('\n');
    const result = el('td'); result.append(badge(item.state)); if (item.error) result.title = item.error;
    row.append(person, el('td', 'balance-number', money(item.balance)), spent,
      el('td', 'balance-number', item.usage ? number(item.requests, 0) : '—'), el('td', 'balance-number', item.usage ? compact(item.tokens) : '—'), models, result);
    body.append(row);
  }
}
function csvCell(value) {
  if (value == null) return '';
  let text = String(value);
  // Spreadsheet formula injection protection for names and emails.
  if (typeof value === 'string' && /^[=+\-@\t\r]/.test(text)) text = "'" + text;
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}
function exportSummary() {
  const { summary: total, items } = state.summaryView;
  const rows = [['邮箱', '姓名', '角色', '重置前余额', '本期消费', '标准计费', '请求数', 'Tokens', '输入 Tokens', '输出 Tokens', '缓存读取 Tokens', '缓存写入 Tokens', '主要模型', '统计起始', '统计截止', '重置金额', '重置结果', '说明']];
  for (const item of items) {
    const usage = item.usage, models = usage ? modelShares(item.models, usage) : [];
    rows.push([item.email, item.username, item.role === 'admin' ? '管理员' : '员工', item.balance, usage?.actual_cost, usage?.cost, usage?.requests,
      usage?.total_tokens, usage?.input_tokens, usage?.output_tokens, usage?.cache_read_tokens, usage?.cache_creation_tokens,
      models.slice(0, 5).map(model => `${model.model} ${shareText(model.share)}`).join('; '), item.since, item.until, item.amount,
      statuses[item.state] || item.state, item.error || item.usage_error || '']);
  }
  const text = rows.map(row => row.map(csvCell).join(',')).join('\r\n');
  const url = URL.createObjectURL(new Blob(['\uFEFF' + text], { type: 'text/csv;charset=utf-8' }));
  const stamp = total.period.startsWith('manual-') ? new Date(total.at * 1000).toISOString().slice(0, 10) : total.period;
  const link = el('a'); link.href = url; link.download = `flow-reset-summary-${stamp}.csv`;
  link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function loadLogs() {
  const params = new URLSearchParams({ level: $('#log-level').value, search: $('#log-search').value });
  const result = await api('/logs?' + params); state.logs = result.items;
  const root = $('#log-rows'); root.replaceChildren();
  if (!result.items.length) root.append(el('p', 'empty-inline', '暂无匹配日志'));
  for (const log of [...result.items].reverse()) {
    const row = el('div', 'log-line'); row.append(el('span', 'log-time', date(log.at)), el('span', 'log-level ' + log.level, log.level), el('span', 'log-message', log.message)); root.append(row);
  }
  $('#logs-updated').textContent = '更新于 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
}
let logSearchTimer;
$('#log-search').oninput = () => { clearTimeout(logSearchTimer); logSearchTimer = setTimeout(() => action(loadLogs), 300); };
$('#log-level').onchange = () => action(loadLogs);
$('#download-logs').onclick = () => {
  const text = state.logs.map(log => `[${new Date(log.at * 1000).toISOString()}] ${log.level} ${log.message}`).join('\n');
  const url = URL.createObjectURL(new Blob(['\uFEFF' + text], { type: 'text/plain;charset=utf-8' }));
  const link = el('a'); link.href = url; link.download = 'flow-logs.txt'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
async function refresh() {
  if (!state.ready || state.polling) return; state.polling = true;
  try {
    const [overview, jobs] = await Promise.all([api('/overview'), api('/jobs')]);
    state.overview = overview; state.jobs = jobs.items; rememberJobs(jobs.items);
    let reloadGroups = false, reloadUsers = false, reloadQuota = false;
    for (const job of state.jobs) {
      const terminal = !['queued', 'running'].includes(job.status);
      const previous = state.knownJobs.get(job.id);
      if (terminal && previous !== job.status && (previous || state.watched.has(job.id))) {
        if (job.kind === 'sync') reloadGroups = true;
        if (['refresh_users', 'batch_users', 'quota_reset'].includes(job.kind)) reloadUsers = true;
        if (job.kind === 'quota_reset') reloadQuota = true;
      }
      state.knownJobs.set(job.id, job.status);
      if (terminal && state.watched.has(job.id)) {
        const callback = state.watched.get(job.id); state.watched.delete(job.id);
        toast(`${jobTitle(job)}${job.status === 'success' ? '完成' : job.status === 'partial' ? '部分完成' : '失败'}：${summary(job)}`, job.status !== 'success');
        if (callback) callback(job);
      }
    }
    const shown = $('#job-dialog').open && state.jobHistory.get(state.currentJob);
    if (shown) { await loadJobLog(shown); renderJobDetail(shown); }
    if (reloadGroups) { state.groups = (await api('/groups')).items; renderGroups(); }
    if (reloadUsers) { const data = await api('/users'); state.users = data.items; renderUsers(data.updated); }
    if (reloadQuota || state.page === 'logs') {
      const data = await api('/quota'); state.quota = data.rule; state.receipts = data.receipts; state.summaries = data.summaries || [];
      if (!state.quotaDirty) renderQuota();
    }
    renderOverview(); renderJobs();
    if (state.page === 'logs') await loadLogs();
    if (state.offline) { state.offline = false; toast('已恢复与工作台的连接'); }
  } catch (error) { if (!state.offline) { state.offline = true; toast(error.message, true); } }
  finally { state.polling = false; }
}
$$('[data-close]').forEach(button => button.onclick = () => button.closest('dialog').close());
$$('dialog').forEach(dialog => dialog.addEventListener('click', event => {
  const rect = dialog.getBoundingClientRect();
  if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) dialog.close();
}));
window.addEventListener('beforeunload', event => { if (state.ready && (state.settingsDirty || state.quotaDirty)) { event.preventDefault(); event.returnValue = ''; } });
setInterval(() => { if (!document.hidden) refresh(); }, 4000);
setInterval(() => {
  renderCountdown();
  for (const node of $$('[data-since]')) node.textContent = duration(Date.now() / 1000 - Number(node.dataset.since));
}, 1000);
(async () => {
  try {
    const auth = await api('/auth/status');
    if (auth.authenticated) { state.csrf = auth.csrf; await initialize(); }
    else showAuth(auth.setup_needed);
  } catch (error) { $('#auth-error').textContent = error.message; }
})();
