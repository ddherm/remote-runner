(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const designPreview = new URLSearchParams(location.search).get('preview') === 'design';
  const state = {
    hosts: [], sessions: [], defaults: {}, session: null, selectedHost: null,
    hostId: '', archived: false, openHosts: null, lastWorkspaces: new Map(), draftInputs: new Map(),
    navigationRevision: 0, navigating: false, workspaceAction: false, deleteTarget: null,
    refreshing: false, polling: null, probing: false, deploying: false, applyingCapacity: false,
    tasksSignature: '', workersSignature: '', gpuSignature: '', computerNavSignature: '', sessionNavSignature: '', configurationExpanded: new Map(),
    browse: null, browseSerial: 0, log: null, logLoading: false,
    connectionRevision: 0, hostChecks: new Map(), hostCheckQueue: [], activeHostChecks: 0,
  };
  const hash = new URLSearchParams(location.hash.slice(1));
  let token = hash.get('token') || sessionStorage.getItem('runnerToken') || '';
  if (token) sessionStorage.setItem('runnerToken', token);
  if (hash.has('token')) history.replaceState(null, '', location.pathname + location.search);
  const number = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
  const clamp = (v, max = 100) => Math.max(0, Math.min(max, Number(v) || 0));
  const formatNumber = (v) => number(v) ? Number(v).toLocaleString('zh-CN', {maximumFractionDigits: 1}) : '—';
  const percent = (v) => number(v) ? `${Math.round(clamp(v))}%` : '—';
  const shortTime = (v) => {
    const d = typeof v === 'number' ? new Date(v * 1000) : new Date(v);
    return Number.isNaN(d.valueOf()) ? '' : d.toLocaleTimeString('zh-CN', {hour12: false});
  };
  const duration = (seconds) => {
    if (!number(seconds)) return '估算中';
    const n = Math.max(0, Math.round(Number(seconds)));
    if (n < 60) return n ? '不足 1 分钟' : '已完成';
    const minutes = Math.ceil(n / 60);
    if (minutes < 60) return `约 ${minutes} 分钟`;
    const hours = Math.floor(minutes / 60), rest = minutes % 60;
    return hours >= 24 ? `约 ${Math.floor(hours / 24)} 天 ${hours % 24} 小时` : `约 ${hours} 小时${rest ? ` ${rest} 分钟` : ''}`;
  };
  const escape = (value) => String(value === null || value === undefined ? '' : value)
    .replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const statusLabels = {
    draft: '等待设置', created: '等待设置', ready: '准备就绪', configured: '等待部署',
    awaiting_configuration: '等待设置', waiting_configuration: '等待设置',
    preparing: '准备部署', deploying: '正在部署', uploading: '正在上传', verifying: '正在核验',
    launching: '正在启动', running: '运行中', monitoring: '运行中', computing: '计算中',
    complete: '已完成', completed: '已完成', verified_resume: '已复用', success: '已完成',
    failed: '失败', error: '出现错误', timeout: '超时', disconnected: '连接中断',
    pending: '排队中', queued: '排队中', waiting: '等待中', waiting_slot: '等待名额',
    waiting_quota: '等待批次名额', waiting_global: '等待机器名额', idle: '等待中',
    waiting_batch_slot: '等待批次名额', waiting_machine_slot: '等待机器名额', interrupted: '部署中断',
    cancelled: '已取消', validation: '验证中', validating: '验证中', testing: '测试中',
  };
  const phaseLabels = {training: '训练', train: '训练', collecting: '采集', updating: '优化更新', optimizing: '优化更新', validation: '验证', validate: '验证', validating: '验证', testing: '测试', test: '测试', preparing: '准备', prepare: '准备', complete: '已完成', completed: '已完成', waiting_slot: '等待计算名额'};
  const completeStatuses = new Set(['complete', 'completed', 'verified_resume', 'success']);
  const initialStatuses = new Set(['draft', 'created', 'ready', 'configured', 'awaiting_configuration', 'waiting_configuration']);
  const deployingStatuses = new Set(['deploying', 'preparing', 'uploading', 'verifying', 'launching']);
  const statusLabel = (v) => statusLabels[v] || (v ? String(v) : '等待中');
  const phaseLabel = (v) => phaseLabels[v] || (v ? String(v).split('\n')[0] : '');
  const statusClass = (v) => completeStatuses.has(v) ? 'complete' : /fail|error|timeout|disconnect|interrupt/.test(v || '') ? 'failed' : /running|monitor|comput|validat|test|deploy|upload|verify|launch|prepar/.test(v || '') ? 'running' : 'waiting';

  async function api(path, options = {}) {
    if (designPreview) throw new Error('这是界面设计预览，示例数据不会连接或改动远端电脑。');
    const headers = {'X-Runner-Token': token, ...options.headers};
    if (options.body && typeof options.body !== 'string') {
      options.body = JSON.stringify(options.body);
      headers['Content-Type'] = 'application/json';
    }
    const response = await fetch(path, {...options, headers, cache: 'no-store'});
    let data;
    try { data = await response.json(); } catch (_) { throw new Error(`服务返回了无法读取的数据（${response.status}）`); }
    if (!response.ok || data.error && data.ok === false) {
      const error = new Error(typeof data.error === 'string' ? data.error : `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function notice(id, message) {
    const el = $(id);
    el.hidden = !message;
    el.textContent = message || '';
  }
  let toastTimer;
  function toast(message) {
    $('toast').textContent = message;
    $('toast').hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3500);
  }
  function setConnection(kind, message) {
    $('connectionDot').className = 'connection-dot ' + kind;
    $('connectionHint').textContent = message;
    $('connectionHint').classList.toggle('error-text', kind === 'failed');
  }
  function connected(host) { return host && ['connected', 'online', 'ok', 'ready', 'success'].includes(host.status); }
  function archived(session) { return !!(session && (session.archived || session.archived_unix)); }
  function sessionTitle(session) { return session.request && session.request.title || session.title || session.id; }
  function connectionClass(host) {
    if (host._checking) return 'checking';
    if (connected(host)) return 'connected';
    return ['failed', 'offline'].includes(host.status) ? 'failed' : '';
  }
  function pageUrl(hostId, id, showArchived = false) {
    const args = new URLSearchParams();
    if (designPreview) args.set('preview', 'design');
    if (hostId) args.set('host_id', hostId);
    if (id) args.set('session', id);
    if (showArchived) args.set('archived', '1');
    return args.size ? `/?${args}` : '/';
  }
  function pageFromLocation() {
    const args = new URLSearchParams(location.search);
    return {hostId: args.get('host_id') || '', id: args.get('session'), showArchived: args.get('archived') === '1'};
  }
  function rememberOpenHosts() {
    if (designPreview) return;
    try { localStorage.setItem('runnerOpenHosts', JSON.stringify([...state.openHosts])); }
    catch (_) { /* Selection remains available until this page closes. */ }
  }
  function readOpenHosts() {
    try {
      const saved = JSON.parse(localStorage.getItem('runnerOpenHosts'));
      if (Array.isArray(saved)) return new Set(saved.filter((value) => typeof value === 'string'));
    } catch (_) { /* Show saved connections when browser storage is unavailable. */ }
    return null;
  }
  function showComputerChoices() {
    const chosen = state.openHosts || new Set();
    $('computerChoices').innerHTML = state.hosts.length ? state.hosts.map((host) =>
      `<label class="computer-choice"><input type="checkbox" value="${escape(host.id)}" ${chosen.has(host.id) ? 'checked' : ''}><span><strong>${escape(host.label || host.address)}</strong>${host.label && host.label !== host.address ? `<small>${escape(host.address)}</small>` : ''}</span><span class="connection-dot ${connectionClass(host)}" aria-hidden="true"></span></label>`
    ).join('') : '<p class="directory-message">连接成功的电脑会出现在这里。</p>';
    $('computerDialog').showModal();
  }
  function renderComputerNavigation() {
    const scopeHost = state.hosts.find((host) => host.id === state.hostId);
    $('computerPageTitle').textContent = scopeHost ? scopeHost.label || scopeHost.address : '待分配';
    $('activeWorkspaces').setAttribute('aria-pressed', String(!state.archived));
    $('archivedWorkspaces').setAttribute('aria-pressed', String(state.archived));
    const ids = state.openHosts || new Set();
    const pages = [{id: '', label: '待分配'}, ...state.hosts.filter((host) => ids.has(host.id))];
    const nav = $('computerNavigation');
    const html = pages.map((host) => {
      const name = host.label || host.address;
      const tooltip = host.address && host.address !== name ? `${name} (${host.address})` : name;
      const active = state.hostId === host.id;
      return `<a class="computer-page" href="${escape(pageUrl(host.id))}" data-computer-id="${escape(host.id)}" title="${escape(tooltip)}" ${active ? 'aria-current="page"' : ''}><span class="${host.id ? 'pixel-device' : 'pixel-window'}" aria-hidden="true"></span><span>${escape(name)}</span>${host.id ? `<span class="connection-dot ${connectionClass(host)}" aria-hidden="true"></span>` : ''}</a>`;
    }).join('');
    if (state.computerNavSignature === html) return;
    state.computerNavSignature = html;
    const focused = document.activeElement?.closest('[data-computer-id]')?.dataset.computerId;
    nav.innerHTML = html;
    [...nav.querySelectorAll('[data-computer-id]')].find((item) => item.dataset.computerId === focused)?.focus({preventScroll: true});
  }
  function updateHostHistory() {
    const focused = document.activeElement && document.activeElement.closest('[data-host-id]');
    const focusId = focused && focused.dataset.hostId;
    const html = state.hosts.length ? state.hosts.map((host) => `<button type="button" class="host-option" data-host-id="${escape(host.id)}" aria-selected="${state.selectedHost && state.selectedHost.id === host.id ? 'true' : 'false'}"><span class="connection-dot ${host._checking ? 'checking' : connected(host) ? 'connected' : host.status === 'failed' || host.status === 'offline' ? 'failed' : ''}" aria-hidden="true"></span><span class="host-option-label"><strong>${escape(host.label || host.address)}</strong><small>${escape(host.address)}</small></span><span class="host-option-state">${host._checking ? '检查中' : connected(host) ? '已连接' : host.status === 'failed' || host.status === 'offline' ? '连接失败' : '待检查'}${host.checked_unix ? `<small>${escape(shortTime(host.checked_unix))} 检查</small>` : ''}</span></button>`).join('') : '<p class="history-empty">连接成功的电脑会保存在这里。</p>';
    if ($('hostHistory').innerHTML !== html) {
      $('hostHistory').innerHTML = html;
      if (focusId) [...$('hostHistory').querySelectorAll('[data-host-id]')].find((button) => button.dataset.hostId === focusId)?.focus({preventScroll: true});
    }
  }
  function refreshConnectionHint() {
    const host = state.selectedHost;
    if (!host) return;
    if (designPreview) { setConnection('connected', '示例节点 · 仅用于界面预览'); return; }
    const checked = host.checked_unix ? `，检查于 ${shortTime(host.checked_unix)}` : '';
    setConnection(host._checking ? 'checking' : connected(host) ? 'connected' : host.status === 'offline' || host.status === 'failed' ? 'failed' : '', host._checking ? '正在检查 SSH 连接和目标环境…' : connected(host) ? `${host.label || host.address} 已连接${checked}` : host.error || '已选择历史电脑，等待检查连接。');
  }
  function storeHost(host, preserveLabel = false) {
    const index = state.hosts.findIndex((h) => h.id === host.id);
    const existing = index >= 0 ? state.hosts[index] : null;
    const merged = {...existing, ...host};
    if (preserveLabel && existing && existing.label) merged.label = existing.label;
    if (index >= 0) state.hosts[index] = merged; else state.hosts.unshift(merged);
    renderComputerNavigation();
    return merged;
  }
  function hostConnectionKey(host) {
    return JSON.stringify(['address', 'user', 'port', 'identity_file', 'python', 'runtime_root', 'slot_root'].map((key) => host[key] || (key === 'port' ? 22 : '')));
  }
  function requestHostProbe(payload) {
    const key = hostConnectionKey(payload);
    if (state.hostChecks.has(key)) return state.hostChecks.get(key);
    let resolve, reject;
    const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
    state.hostChecks.set(key, promise);
    state.hostCheckQueue.push({payload, key, resolve, reject});
    drainHostChecks();
    return promise;
  }
  function drainHostChecks() {
    while (state.activeHostChecks < 2 && state.hostCheckQueue.length) {
      const item = state.hostCheckQueue.shift();
      state.activeHostChecks++;
      api('/api/hosts/probe', {method: 'POST', body: item.payload}).then(item.resolve, item.reject).finally(() => {
        state.activeHostChecks--;
        state.hostChecks.delete(item.key);
        drainHostChecks();
      });
    }
  }
  async function silentProbe(host) {
    if (!host || state.applyingCapacity) return;
    const revision = state.connectionRevision;
    const key = hostConnectionKey(host);
    const payload = {};
    for (const field of ['address', 'user', 'port', 'identity_file', 'python', 'runtime_root', 'slot_root', 'principal']) if (host[field] !== undefined) payload[field] = host[field];
    const cached = state.hosts.find((h) => h.id === host.id);
    if (cached) cached._checking = true;
    if (state.selectedHost && state.selectedHost.id === host.id) { state.selectedHost._checking = true; refreshConnectionHint(); }
    updateHostHistory();
    renderComputerNavigation();
    let result;
    try {
      const data = await requestHostProbe(payload);
      result = storeHost({...data.host || data, _checking: false}, true);
    } catch (error) {
      result = storeHost({...host, status: 'offline', error: error.message, checked_unix: Date.now() / 1000, _checking: false}, true);
    }
    if (revision === state.connectionRevision && state.selectedHost && state.selectedHost.id === host.id && hostConnectionKey(state.selectedHost) === key) {
      state.selectedHost = result;
      refreshConnectionHint(); updateLimits(); updateActions(); renderSidebarHost();
    }
    updateHostHistory();
  }
  function syncConnectionFromSnapshot(session, snapshot) {
    if (!state.selectedHost || initialStatuses.has(session.status)) return;
    let change;
    if (session.status === 'disconnected') change = {status: 'offline', error: session.error || '监控连接中断', _checking: false, checked_unix: Date.now() / 1000};
    else if (snapshot.updated_unix && !snapshot.stale && Date.now() / 1000 - snapshot.updated_unix < 20) change = {status: 'online', error: null, _checking: false, checked_unix: snapshot.updated_unix};
    if (change) { state.selectedHost = storeHost({...state.selectedHost, ...change}); refreshConnectionHint(); updateHostHistory(); }
  }
  function applyHost(host) {
    state.connectionRevision++;
    state.selectedHost = host || null;
    const profile = host || {};
    $('hostAddress').value = profile.address || state.defaults.host || '';
    $('capacityInput').value = profile.global_limit || state.defaults.global_limit || 18;
    notice('capacityMessage', '');
    const fields = {sshUser: 'user', sshPort: 'port', identityFile: 'identity_file', pythonPath: 'python', runtimeRoot: 'runtime_root', slotRoot: 'slot_root'};
    for (const [id, key] of Object.entries(fields)) $(id).value = profile[key] ?? state.defaults[key] ?? (key === 'port' ? 22 : '');
    if (host) refreshConnectionHint();
    else setConnection('', '连接成功后自动保存，可以为电脑重命名。');
    updateHostHistory();
    updateLimits();
    updateActions();
    renderSidebarHost();
  }
  function hostPayload() {
    return {
      address: $('hostAddress').value.trim(),
      label: state.selectedHost && state.selectedHost.address === $('hostAddress').value.trim() ? state.selectedHost.label : '',
      user: $('sshUser').value.trim(), port: Number($('sshPort').value || 22),
      identity_file: $('identityFile').value.trim(), python: $('pythonPath').value.trim(),
      runtime_root: $('runtimeRoot').value.trim(), slot_root: $('slotRoot').value.trim(),
    };
  }
  function updateLimits() {
    const machine = state.session && state.session.snapshot && state.session.snapshot.global_limit || state.selectedHost && state.selectedHost.global_limit || null;
    $('machineLimit').textContent = formatNumber(machine);
    const parallel = Number($('parallel').value);
    $('effectiveLimit').classList.toggle('limited', number(machine) && parallel > machine);
    $('effectiveLimit').textContent = number(machine) ? parallel > machine ? `本次有效上限 ${machine}，受全机并行上限约束。` : `本次有效上限 ${parallel}，按独立任务数量使用。` : '连接后读取目标电脑的设置。';
  }
  function updateActions() {
    const session = state.session;
    const initial = !!(session && initialStatuses.has(session.status) && !archived(session));
    const busy = state.navigating || state.workspaceAction || state.deploying;
    const locked = !!session && !initial;
    const machineFixed = !!state.hostId;
    $('configLock').hidden = !locked;
    $('configLock').textContent = archived(session) ? '工作区已归档' : '本次部署设置已固定';
    for (const id of ['remoteRoot', 'parallel', 'pythonPath', 'runtimeRoot', 'slotRoot']) $(id).disabled = locked || busy;
    for (const id of ['hostAddress', 'sshUser', 'sshPort', 'identityFile']) $(id).disabled = locked || machineFixed || busy;
    $('connectButton').disabled = state.probing || locked || busy || !$('hostAddress').value.trim();
    $('browseButton').disabled = locked || busy || !state.selectedHost;
    $('historyButton').disabled = locked || machineFixed || busy;
    $('renameButton').disabled = !state.selectedHost || locked || busy;
    $('capacityInput').disabled = !connected(state.selectedHost) || state.applyingCapacity || locked || busy;
    $('applyCapacity').disabled = $('capacityInput').disabled;
    $('decreaseParallel').disabled = locked || busy || Number($('parallel').value) <= 1;
    $('increaseParallel').disabled = locked || busy || Number($('parallel').value) >= 36;
    $('deployButton').hidden = !session || archived(session);
    $('deployButton').disabled = !initial || !connected(state.selectedHost) || state.probing || busy || !$('remoteRoot').value.trim();
    $('assignWorkspace').hidden = !initial || machineFixed || designPreview;
    $('assignWorkspace').disabled = !state.selectedHost || busy || state.probing;
    if (state.selectedHost) $('assignWorkspace').title = `保存到 ${state.selectedHost.label || state.selectedHost.address}`;
    $('archiveWorkspace').hidden = designPreview || !session || archived(session);
    $('restoreWorkspace').hidden = designPreview || !archived(session);
    $('deleteWorkspace').hidden = designPreview || !session;
    $('archiveWorkspace').disabled = $('restoreWorkspace').disabled = busy;
    $('deleteWorkspace').disabled = busy || !session?.can_delete;
    $('deleteWorkspace').title = session && !session.can_delete ? '可在任务结束、状态确认后删除记录' : '';
    const monitor = session && session.request && session.request.mode === 'monitor';
    $('deployButton').querySelector('span').textContent = state.deploying || session && deployingStatuses.has(session.status) ? (monitor ? '正在接入…' : '正在部署…') : monitor ? '接入监控' : '部署并运行';
    $('folderHint').textContent = monitor ? '选择已有任务的工作目录，接入过程只读取运行状态。' : '代码与结果保存在此目录；目录不存在时，部署会自动创建。';
    $('deploymentHint').textContent = !session ? '等待 Codex 准备实验任务。' : locked ? `部署设置已固定。${session.status === 'disconnected' ? '重新连接只恢复监控。' : '可以在下方查看进度及日志。'}` : monitor ? '接入已有任务，读取进度和日志。' : '确认工作区的目录与并行上限后开始部署。';
    if (designPreview) {
      for (const id of ['deployButton','connectButton','applyCapacity','renameButton','historyButton','browseButton']) $(id).disabled = true;
      $('deploymentHint').textContent = '设计预览使用示例数据，不会连接或部署任务。';
      $('deployButton').querySelector('span').textContent = '设计预览';
    }
    updateConfigurationView();
  }
  function updateConfigurationView() {
    const session = state.session;
    const collapsible = !!session && (archived(session) || !initialStatuses.has(session.status) && !deployingStatuses.has(session.status));
    const expanded = !collapsible || state.configurationExpanded.get(session.id) === true;
    $('configContent').hidden = !expanded;
    $('configSummary').hidden = expanded;
    $('toggleConfiguration').hidden = !collapsible;
    $('toggleConfiguration').textContent = expanded ? '收起运行设置' : '查看运行设置';
    $('toggleConfiguration').setAttribute('aria-expanded', String(expanded));
    $('configHeading').closest('.configuration').classList.toggle('is-collapsed', !expanded);
    if (session) {
      const host = state.selectedHost;
      $('summaryComputer').textContent = host ? `${host.label || host.address}${host.label && host.label !== host.address ? ` (${host.address})` : ''}` : $('hostAddress').value || '尚未选择';
      $('summaryDirectory').textContent = session.remote_root || $('remoteRoot').value || '尚未设置';
      $('summaryParallel').textContent = formatNumber(session.parallel || $('parallel').value);
      $('summaryMachineLimit').textContent = formatNumber(session.snapshot && session.snapshot.global_limit || host && host.global_limit);
    }
  }
  function renderSessionOptions() {
    const selected = state.session?.id || '';
    $('sessionSelect').innerHTML = state.sessions.length ? state.sessions.map((session) => `<option value="${escape(session.id)}">${escape(sessionTitle(session))}</option>`).join('') : '<option value="">暂无工作区</option>';
    if (selected) $('sessionSelect').value = selected;
    $('sessionSelect').disabled = !state.sessions.length;
    renderSessionNavigation();
  }
  function renderSessionNavigation() {
    const nav = $('sessionNavigation');
    const html = state.sessions.map((session) => {
      const selected = state.session?.id === session.id;
      const status = (selected ? state.session.status : session.status) || 'draft';
      return `<button class="session-nav-item ${selected ? 'is-active' : ''}" type="button" data-session-id="${escape(session.id)}" ${selected ? 'aria-current="page"' : ''}><span class="session-pixel-icon" aria-hidden="true"><i></i><i></i><i></i><i></i></span><span class="session-nav-copy"><strong>${escape(sessionTitle(session))}</strong><small>${escape(statusLabel(status))}</small></span><span class="session-nav-dot ${statusClass(status)}" aria-hidden="true"></span></button>`;
    }).join('') || `<p class="sidebar-empty">${state.archived ? '暂无已归档的工作区' : '暂无工作区'}</p>`;
    if (state.sessionNavSignature === html) return;
    state.sessionNavSignature = html;
    const focused = document.activeElement?.closest('[data-session-id]')?.dataset.sessionId;
    nav.innerHTML = html;
    [...nav.querySelectorAll('[data-session-id]')].find((item) => item.dataset.sessionId === focused)?.focus({preventScroll: true});
  }
  function requestSummary(request) {
    if (typeof request.summary === 'string') return request.summary;
    if (Array.isArray(request.summary)) return request.summary.map((r) => typeof r === 'string' ? r : r.label || JSON.stringify(r)).join('；');
    if (request.summary && typeof request.summary === 'object') return Object.entries(request.summary).map(([key, value]) => `${key}：${typeof value === 'object' ? JSON.stringify(value) : value}`).join('；');
    const plan = request.work_plan;
    if (Array.isArray(plan) && plan.length) return plan.map((p) => `${p.label || p.id || '任务'}${number(p.total) ? ` ${formatNumber(p.total)} ${p.unit || ''}` : ''}`).join('；');
    return '实验任务由 Codex 准备。选择目标电脑和工作目录后开始运行。';
  }
  function flattenTasks(tasks, parent = null) {
    const result = [];
    for (const task of tasks || []) {
      result.push({...task, parent_id: task.parent_id || parent});
      if (Array.isArray(task.children)) result.push(...flattenTasks(task.children, task.id));
    }
    return result;
  }
  function metricsOf(task) {
    if (Array.isArray(task.metrics)) return task.metrics;
    if (Array.isArray(task.phases)) return task.phases;
    return [];
  }
  let dialSequence = 0;
  const pixelGlyphs = {
    '0': ['01110','11011','11011','11011','11011','11011','01110'],
    '1': ['00100','01100','00100','00100','00100','00100','01110'],
    '2': ['01110','11011','00011','00110','01100','11000','11111'],
    '3': ['11110','00011','00011','01110','00011','00011','11110'],
    '4': ['00011','00111','01111','11011','11111','00011','00011'],
    '5': ['11111','11000','11000','11110','00011','00011','11110'],
    '6': ['01110','11000','11000','11110','11011','11011','01110'],
    '7': ['11111','00011','00110','00110','01100','01100','01100'],
    '8': ['01110','11011','11011','01110','11011','11011','01110'],
    '9': ['01110','11011','11011','01111','00011','00011','01110'],
  };
  function pixelNumberHTML(value) {
    if (!number(value)) return '<span class="dial-empty">—</span>';
    const digits = String(Math.round(clamp(value)));
    let cells = '';
    [...digits].forEach((digit, index) => pixelGlyphs[digit].forEach((row, y) => [...row].forEach((cell, x) => {
      if (cell === '1') cells += `<rect x="${index * 6 + x}" y="${y}" width=".87" height=".87"/>`;
    })));
    return `<svg class="pixel-number" viewBox="0 0 ${digits.length * 6 - 1} 7" aria-hidden="true">${cells}</svg><span class="pixel-suffix">%</span><span class="visually-hidden">${digits}%</span>`;
  }
  function dialSVG(value, indeterminate = false) {
    const known = number(value);
    const p = known ? clamp(value) : indeterminate ? 22 : 0;
    const id = `dial-shade-${++dialSequence}`;
    const circumference = 2 * Math.PI * 76;
    let ticks = '';
    for (let index = 0; index < 48; index++) {
      const angle = (index / 48 * 360 - 90) * Math.PI / 180;
      ticks += `<rect class="dial-tick ${known && index / 48 * 100 < p ? 'is-passed' : ''}" x="${(100 + Math.cos(angle) * 95 - 1.5).toFixed(2)}" y="${(100 + Math.sin(angle) * 95 - 1.5).toFixed(2)}" width="3" height="3"/>`;
    }
    const cursorAngle = (p / 100 * 360 - 90) * Math.PI / 180;
    const cursor = known && p > 0 && p < 100 ? `<rect class="dial-cursor" x="${(100 + Math.cos(cursorAngle) * 76 - 3.5).toFixed(2)}" y="${(100 + Math.sin(cursorAngle) * 76 - 3.5).toFixed(2)}" width="7" height="7"/>` : '';
    return `<svg class="dial-svg ${indeterminate && !known ? 'dial-spinning' : ''}" viewBox="0 0 200 200" fill="none" aria-hidden="true"><defs><linearGradient id="${id}" x1="20" y1="20" x2="180" y2="175" gradientUnits="userSpaceOnUse"><stop stop-color="currentColor" stop-opacity=".28"/><stop offset=".38" stop-color="currentColor" stop-opacity=".82"/><stop offset="1" stop-color="currentColor"/></linearGradient></defs>${ticks}<circle class="dial-track" cx="100" cy="100" r="76" stroke-width="18"/><circle class="dial-arc" cx="100" cy="100" r="76" stroke-width="18" stroke="url(#${id})" stroke-dasharray="${(circumference * p / 100).toFixed(3)} ${circumference.toFixed(3)}" transform="rotate(-90 100 100)"/>${cursor}</svg>`;
  }
  function metricHTML(metric) {
    const known = !metric.indeterminate && number(metric.current) && number(metric.total) && Number(metric.total) > 0;
    const p = known ? clamp(Number(metric.current) / Number(metric.total) * 100) : null;
    const label = metric.label || phaseLabel(metric.id) || '进度';
    const detail = number(metric.current) && number(metric.total) ? `${formatNumber(metric.current)} / ${formatNumber(metric.total)}${metric.unit ? ` ${escape(metric.unit)}` : ''}` : number(metric.current) ? `${formatNumber(metric.current)}${metric.unit ? ` ${escape(metric.unit)}` : ''}` : '等待进度更新';
    return `<div class="metric metric-dial ${escape(metric.id || '')}"><div class="dial-graphic ${known ? '' : 'indeterminate'}" role="progressbar" aria-label="${escape(label)}" ${known ? `aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(p)}"` : 'aria-valuetext="运行中，暂无可量化进度"'}>${dialSVG(p, !known)}<div class="dial-center"><strong class="dial-value">${known ? Math.round(p) + '<span>%</span>' : '<span class="dial-running">运行中</span>'}</strong></div></div><div class="metric-copy"><span class="metric-label">${escape(label)}</span><span class="metric-value">${detail}</span></div></div>`;
  }
  function applyProgressWidths(container) {
    for (const fill of container.querySelectorAll('[data-progress]')) fill.style.width = clamp(fill.dataset.progress) + '%';
  }
  function taskHTML(task, tasks, child = false, ancestors = new Set()) {
    const key = String(task.id || '');
    if (ancestors.has(key)) return '';
    const next = new Set(ancestors); next.add(key);
    const children = tasks.filter((t) => String(t.parent_id) === key && String(t.id) !== key);
    const allMetrics = metricsOf(task);
    const counted = allMetrics.filter((metric) => metric.contributes_to_overall !== false);
    const metrics = counted.length ? counted : allMetrics;
    const attempt = task.attempt ? String(task.attempt).split(/[\\/]/).filter(Boolean).pop() : '';
    const phase = [phaseLabel(task.phase), ...allMetrics.filter((metric) => !metrics.includes(metric)).map((metric) => `${metric.label || metric.id} ${formatNumber(metric.current)} / ${formatNumber(metric.total)}`)].filter(Boolean).join('，');
    return `<article class="${child ? 'child-task' : 'task-row'}" data-task-key="${escape(key)}"><div class="task-heading"><div><div class="task-title"><h3>${escape(task.label || task.group || task.id || '任务')}</h3><span class="status-badge ${statusClass(task.status)}">${escape(statusLabel(task.status))}</span></div>${phase || attempt ? `<p class="task-phase">${phase ? `<span>${escape(phase)}</span>` : ''}${attempt ? `<span>${escape(attempt)}</span>` : ''}</p>` : ''}</div><button class="text-button" type="button" data-log-task="${escape(key)}">查看日志</button></div>${metrics.length ? `<div class="metric-list ${metrics.length === 1 ? 'single-metric' : ''}">${metrics.map(metricHTML).join('')}</div>` : `<p class="task-note">${escape(completeStatuses.has(task.status) ? '任务已完成' : /fail|error/.test(task.status || '') ? task.error || '任务失败，请查看日志。' : '此任务未提供可量化的内部进度。')}</p>`}${children.length ? `<details class="task-children" data-detail-key="${escape(key)}"><summary>子任务 <span>(${children.length})</span></summary><div class="child-list">${children.map((t) => taskHTML(t, tasks, true, next)).join('')}</div></details>` : ''}</article>`;
  }
  function renderTasks(snapshot) {
    const tasks = flattenTasks(snapshot.tasks || []);
    const signature = JSON.stringify(tasks);
    $('tasksEmpty').hidden = tasks.length > 0;
    if (signature !== state.tasksSignature) {
      const open = new Set([...$('taskList').querySelectorAll('details[open]')].map((d) => d.dataset.detailKey));
      const focused = document.activeElement && document.activeElement.closest('[data-log-task]');
      const focusKey = focused && focused.dataset.logTask;
      const ids = new Set(tasks.map((t) => String(t.id)));
      const roots = tasks.filter((t) => !t.parent_id || !ids.has(String(t.parent_id)));
      $('taskList').innerHTML = roots.map((t) => taskHTML(t, tasks)).join('');
      applyProgressWidths($('taskList'));
      for (const detail of $('taskList').querySelectorAll('details')) detail.open = open.has(detail.dataset.detailKey);
      if (focusKey) [...$('taskList').querySelectorAll('[data-log-task]')].find((b) => b.dataset.logTask === focusKey)?.focus({preventScroll: true});
      state.tasksSignature = signature;
    }
    return tasks;
  }
  function renderWorkers(snapshot, tasks) {
    const workers = snapshot.active_workers || [];
    $('workersSection').hidden = !workers.length;
    $('workerCount').textContent = `${workers.length} 个计算任务`;
    const signature = JSON.stringify([workers, tasks]);
    if (signature === state.workersSignature) return;
    $('workerList').innerHTML = workers.map((worker) => {
      const task = tasks.find((t) => t.id === worker.task_id && (!worker.attempt || !t.attempt || t.attempt === worker.attempt)) || {};
      const metrics = metricsOf(task);
      const phase = worker.phase || task.phase || '';
      const desired = /validat/.test(phase) ? 'validation' : /test/.test(phase) ? 'testing' : 'training';
      const metric = metrics.find((m) => m.id === desired) || metrics.find((m) => number(m.total) && m.total > 0 && !/update|optim/.test(m.id || ''));
      const p = metric && !metric.indeterminate && number(metric.current) && number(metric.total) && metric.total > 0 ? clamp(Number(metric.current) / Number(metric.total) * 100) : null;
      const label = worker.label || task.label || worker.task_id || `计算任务 ${number(worker.slot) ? worker.slot + 1 : ''}`;
      return `<div class="worker-item"><div class="worker-dial ${p === null ? 'indeterminate' : ''}" role="progressbar" aria-label="${escape(label)}" ${p !== null ? `aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(p)}"` : 'aria-valuetext="运行中"'}>${dialSVG(p, p === null)}<div class="dial-center"><strong class="dial-value">${p !== null ? Math.round(p) + '<span>%</span>' : '<span>···</span>'}</strong></div></div><div class="worker-copy"><div class="worker-heading"><div class="worker-title"><span class="connection-dot connected" aria-hidden="true"></span><span title="${escape(label)}">${escape(label)}</span></div><span class="worker-phase">${escape(phaseLabel(phase) || '计算中')}</span></div><p class="worker-detail">${metric && !metric.indeterminate ? `${escape(metric.label || '进度')} ${formatNumber(metric.current)} / ${formatNumber(metric.total)}` : '运行中，等待进度'}${number(worker.slot) ? `　槽位 ${Number(worker.slot) + 1}` : ''}</p></div></div>`;
    }).join('');
    applyProgressWidths($('workerList'));
    state.workersSignature = signature;
  }
  function renderResources(snapshot) {
    const resource = snapshot.resources || {};
    $('cpuValue').textContent = percent(resource.cpu_percent);
    $('cpuFill').style.width = clamp(resource.cpu_percent) + '%';
    if ($('cpuDial')) $('cpuDial').innerHTML = dialSVG(resource.cpu_percent);
    $('cpuDetail').textContent = number(resource.cpu_percent) ? '整台目标电脑的 CPU 利用率' : resource.cpu_error || '等待连续采样';
    $('memoryValue').textContent = percent(resource.memory_percent);
    $('memoryFill').style.width = clamp(resource.memory_percent) + '%';
    if ($('memoryDial')) $('memoryDial').innerHTML = dialSVG(resource.memory_percent);
    $('memoryDetail').textContent = number(resource.memory_used_gib) && number(resource.memory_total_gib) ? `${formatNumber(resource.memory_used_gib)} / ${formatNumber(resource.memory_total_gib)} GB` : resource.memory_error || '等待采样';
    const gpus = resource.gpus || [];
    const signature = JSON.stringify([gpus, resource.gpu_error, resource.gpu_status]);
    if (signature !== state.gpuSignature) {
      $('gpuResources').innerHTML = gpus.length ? gpus.map((gpu, i) => `<div class="resource-item"><div class="resource-visual"><div class="resource-dial-graphic">${dialSVG(gpu.utilization_percent)}</div><strong class="resource-dial-value">${percent(gpu.utilization_percent)}</strong></div><div class="resource-copy"><div class="resource-title"><span>${escape(gpu.name && /^GPU \d+$/.test(gpu.name) ? gpu.name : gpus.length > 1 ? 'GPU ' + i : 'GPU')}</span></div><p>${escape(gpu.name && !/^GPU \d+$/.test(gpu.name) ? gpu.name : '图形处理器')}${!number(gpu.utilization_percent) ? `：${escape(gpu.error || '利用率不可用')}` : ''}</p></div></div>`).join('') : `<div class="resource-item"><div class="resource-visual"><div class="resource-dial-graphic">${dialSVG(null)}</div><strong class="resource-dial-value">—</strong></div><div class="resource-copy"><div class="resource-title"><span>GPU</span></div><p>${escape(resource.gpu_error || resource.gpu_status || (snapshot.updated_unix ? '未提供 GPU 数据' : '等待设备信息'))}</p></div></div>`;
      applyProgressWidths($('gpuResources'));
      state.gpuSignature = signature;
    }
    const globalLimit = number(snapshot.global_limit) ? snapshot.global_limit : state.selectedHost && state.selectedHost.global_limit;
    $('globalUsage').textContent = `全机正在计算 ${formatNumber(snapshot.global_used)} / ${formatNumber(globalLimit)}`;
    $('resourceHost').textContent = state.selectedHost ? `${state.selectedHost.label || state.selectedHost.address} (${state.selectedHost.address})` : '尚未连接目标电脑';
    $('updatedAt').textContent = snapshot.updated_unix ? `更新于 ${shortTime(snapshot.updated_unix)}${snapshot.stale ? '，数据已过期' : '，每 5 秒刷新'}` : '尚无采样数据';
    if (designPreview) $('updatedAt').textContent = '示例数据 · 实际运行每 5 秒更新';
  }
  function renderEvents(session) {
    const events = (session.events || []).slice(-4);
    $('deploymentEvents').hidden = !events.length;
    const rows = events.map((event) => `<div class="deployment-event"><time>${escape(shortTime(event.updated_unix || event.timestamp || event.time || event.at))}</time><span>${escape(typeof event === 'string' ? event : event.message || event.label || event.status || '')}</span></div>`).join('');
    $('deploymentEvents').innerHTML = `<details class="event-disclosure" ${deployingStatuses.has(session.status) ? 'open' : ''}><summary><span class="event-status-pixel" aria-hidden="true"></span>${escape(session.status === 'draft' ? '等待配置' : deployingStatuses.has(session.status) ? '部署进展' : '运行记录')}<span>${events.length} 条</span></summary><div class="event-list">${rows}</div></details>`;
  }
  function renderSidebarHost(snapshot = state.session?.snapshot || {}) {
    const host = state.selectedHost;
    $('sidebarHost').textContent = host ? host.label || host.address : '尚未选择电脑';
    $('sidebarHostState').textContent = host ? `${host.address} · ${snapshot.global_limit || host.global_limit || 18} 个名额` : '连接后自动保存';
    $('sidebarConnectionDot').className = 'connection-dot ' + (host ? connectionClass(host) : '');
  }
  function renderSession() {
    const session = state.session;
    const request = session && session.request || {};
    $('experimentTitle').textContent = request.title || (session ? '远程实验' : state.archived ? '暂无已归档的工作区' : state.hostId ? '此电脑暂无工作区' : '准备一次远程运行');
    $('experimentSummary').textContent = session ? requestSummary(request) : state.archived ? '' : state.hostId ? '从待分配页面选择工作区，并保存到此电脑。' : '在 Codex 对话中发起实验，新的工作区会出现在这里。';
    $('archivedBadge').hidden = !archived(session);
    $('sessionStatus').hidden = !session;
    $('phaseRail').hidden = !session;
    $('progressHeading').closest('.progress-section').hidden = !session;
    $('resourceHeading').closest('.resource-panel').hidden = !session;
    $('configHeading').closest('.configuration').hidden = !session && state.archived;
    $('sessionStatus').textContent = session ? statusLabel(session.status) : '等待实验';
    $('sessionStatus').className = 'status-badge ' + (session ? statusClass(session.status) : '');
    const snapshot = session && session.snapshot || {};
    if (session) syncConnectionFromSnapshot(session, snapshot);
    const overall = snapshot.overall || {};
    const known = number(overall.percent);
    const overallP = known ? clamp(overall.percent) : null;
    $('overallPercent').innerHTML = pixelNumberHTML(overallP);
    if ($('overallDialFace')) $('overallDialFace').innerHTML = dialSVG(overallP, !known && session && !initialStatuses.has(session.status));
    $('overallFill').style.width = (overallP || 0) + '%';
    $('overallTrack').classList.toggle('indeterminate', !known && session && !initialStatuses.has(session.status) && !completeStatuses.has(session.status));
    $('overallTrack').classList.toggle('complete', !!session && completeStatuses.has(session.status));
    if (known) {
      $('overallTrack').setAttribute('aria-valuemin', '0');
      $('overallTrack').setAttribute('aria-valuemax', '100');
      $('overallTrack').setAttribute('aria-valuenow', String(Math.round(overallP)));
    } else $('overallTrack').removeAttribute('aria-valuenow');
    $('overallDetail').textContent = number(overall.total) && overall.total > 0 ? `已完成 ${formatNumber(overall.current)} / ${formatNumber(overall.total)} 个计划工作项${overallP === 100 && session && !completeStatuses.has(session.status) ? '，验收中' : ''}` : session && !initialStatuses.has(session.status) ? '当前任务尚未提供可量化的总工作量' : '部署后显示完成工作量';
    const workers = snapshot.active_workers || [];
    const counts = {training: 0, validation: 0, testing: 0, other: 0};
    workers.forEach((w) => {
      const phase = w.phase || '';
      if (/validat/.test(phase)) counts.validation++;
      else if (/test/.test(phase)) counts.testing++;
      else if (/train|collect|updat|optim/.test(phase)) counts.training++;
      else counts.other++;
    });
    const active = number(snapshot.batch_used) ? Number(snapshot.batch_used) : workers.length;
    const limit = Math.min(Number(session && session.parallel || $('parallel').value || 18), Number(snapshot.global_limit || state.selectedHost && state.selectedHost.global_limit || 36));
    $('parallelSummary').innerHTML = snapshot.updated_unix ? `正在计算 <strong class="actual-count">${formatNumber(active)}</strong>　${[['training', '训练'], ['validation', '验证'], ['testing', '测试'], ['other', '其他']].filter(([key]) => counts[key]).map(([key, label]) => `${label} ${counts[key]}`).join('，') || '暂无计算任务'}　<span>本次有效上限 ${limit}</span>` : '等待开始运行';
    $('etaSummary').innerHTML = `预计剩余 <strong>${escape(snapshot.stale ? '连接恢复后更新' : completeStatuses.has(session && session.status) ? '已完成' : duration(snapshot.eta_seconds))}</strong>`;
    if ($('overviewStatus')) $('overviewStatus').textContent = session ? statusLabel(session.status) : '等待实验';
    if ($('overviewCompleted')) $('overviewCompleted').textContent = number(overall.current) ? formatNumber(overall.current) : '—';
    if ($('slotBoard')) {
      $('slotBoard').hidden = !snapshot.updated_unix;
      $('slotBoard').setAttribute('aria-label', `本批次正在使用 ${active} / ${limit} 个计算名额`);
      $('slotBoard').innerHTML = Array.from({length: Math.min(36, limit)}, (_, index) => `<span class="slot-pixel ${index < active ? 'is-active' : ''}" aria-hidden="true"></span>`).join('');
    }
    renderSidebarHost(snapshot);
    if ($('phaseRail')) {
      const stage = !session || initialStatuses.has(session.status) ? 0 : deployingStatuses.has(session.status) ? 1 : completeStatuses.has(session.status) ? 3 : 2;
      $('phaseRail').innerHTML = ['连接目标', '部署任务', '并行运行', '完成验收'].map((label, index) => `<div class="phase-step ${index < stage ? 'is-done' : index === stage ? 'is-current' : ''}"><span class="phase-node" aria-hidden="true">${index < stage ? '✓' : '<i></i>'}</span><span>${label}</span></div>`).join('');
    }
    const stale = snapshot.stale || session && session.status === 'disconnected';
    const disconnected = session && session.status === 'disconnected';
    notice('staleNotice', stale ? `${disconnected ? '监控连接中断' : '状态暂未更新'}，显示最后一次有效数据${snapshot.updated_unix ? `（${shortTime(snapshot.updated_unix)}）` : ''}。${disconnected ? 'Windows 上的任务继续运行。' : '正在等待新的状态，监控不会重新启动任务。'}` : '');
    const warnings = snapshot.warnings || [];
    notice('snapshotWarnings', Array.isArray(warnings) ? warnings.map((w) => typeof w === 'string' ? w : w.message || w.error || '').filter(Boolean).join('\n') : typeof warnings === 'string' ? warnings : '');
    $('reconnectButton').hidden = !session || initialStatuses.has(session.status) || deployingStatuses.has(session.status);
    $('refreshLabel').hidden = !$('reconnectButton').hidden;
    const tasks = renderTasks(snapshot);
    renderWorkers(snapshot, tasks);
    renderResources(snapshot);
    renderEvents(session || {});
    renderSessionNavigation();
    updateActions();
    updateLimits();
    if (session && session.error) notice('pageError', session.error);
    else notice('pageError', '');
  }
  function pageKey(hostId = state.hostId, showArchived = state.archived) {
    return `${hostId}:${showArchived}`;
  }
  function applySession(session) {
    state.session = session;
    const previous = session && state.draftInputs.get(session.id);
    const draft = previous?.pageHost === state.hostId && initialStatuses.has(session.status) && !archived(session) ? previous : null;
    const host = state.hosts.find((row) => row.id === (state.hostId || draft?.preferredHost));
    if (session) state.lastWorkspaces.set(pageKey(), session.id);
    applyHost(host);
    $('remoteRoot').value = draft ? draft.remoteRoot : session?.remote_root || session?.request?.remote_root || state.defaults.remote_root || '';
    $('parallel').value = draft ? draft.parallel : session?.parallel || host?.global_limit || state.defaults.global_limit || 18;
    state.tasksSignature = state.workersSignature = state.gpuSignature = '';
    renderComputerNavigation();
    renderSessionOptions();
    renderSession();
    if (!designPreview && host && session && initialStatuses.has(session.status) && !archived(session)) silentProbe(host);
  }
  function schedulePoll() {
    clearTimeout(state.polling);
    if (!state.session || state.navigating || designPreview) return;
    state.polling = setTimeout(refreshSession, deployingStatuses.has(state.session.status) ? 1000 : 5000);
  }
  async function refreshSession() {
    if (!state.session || state.refreshing || state.navigating || state.workspaceAction) {
      schedulePoll();
      return;
    }
    state.refreshing = true;
    const id = state.session.id;
    const revision = state.navigationRevision;
    const current = () => revision === state.navigationRevision && state.session?.id === id && !state.workspaceAction;
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(id)}`);
      if (!current()) return;
      const session = data.session || data;
      if ((session.host_id || '') !== state.hostId || archived(session) !== state.archived) {
        await loadPage({id}, 'replaceState');
        return;
      }
      state.session = session;
      renderSession();
      if ($('logDialog').open) await loadLog();
    } catch (error) {
      if (!current()) return;
      if (error.status === 404) {
        await loadPage({hostId: state.hostId, showArchived: state.archived}, 'replaceState');
        toast('此工作区已删除');
        return;
      }
      notice('pageError', `无法读取本地后台状态：${error.message}`);
      if (state.session.snapshot) state.session.snapshot.stale = true;
      notice('staleNotice', '本地后台暂时不可达，已保留最后一次数据。Windows 上的任务继续运行。');
    } finally { state.refreshing = false; schedulePoll(); }
  }
  async function loadPage({hostId = '', id = null, showArchived = false}, historyMethod = 'pushState') {
    if (state.session && !state.navigating && !state.workspaceAction && !state.deploying && initialStatuses.has(state.session.status) && !archived(state.session)) {
      state.draftInputs.set(state.session.id, {pageHost: state.hostId, preferredHost: state.selectedHost?.id, remoteRoot: $('remoteRoot').value, parallel: $('parallel').value});
    }
    const revision = ++state.navigationRevision;
    state.connectionRevision++;
    state.browseSerial++;
    clearTimeout(state.polling);
    for (const name of ['folderDialog', 'logDialog', 'renameDialog', 'deleteDialog']) if ($(name).open) $(name).close();
    closeHistory();
    state.navigating = true;
    updateActions();
    document.querySelector('.workspace').setAttribute('aria-busy', 'true');
    let removed = false;
    try {
      let page, session;
      if (designPreview) {
        const sessions = state.previewSessions.filter((row) => (row.host_id || '') === hostId && archived(row) === showArchived);
        page = {hosts: state.hosts, defaults: state.defaults, sessions, session: sessions[0] || null};
        session = sessions.find((row) => row.id === id);
      } else {
        if (id) {
          try { session = await api(`/api/sessions/${encodeURIComponent(id)}`); }
          catch (error) {
            if (error.status !== 404) throw error;
            removed = true;
          }
        }
        if (session) {
          hostId = session.host_id || '';
          showArchived = archived(session);
        }
        const args = new URLSearchParams({host_id: hostId, archived: String(showArchived)});
        page = await api(`/api/bootstrap?${args}`);
      }
      if (revision !== state.navigationRevision) return false;
      const sessions = page.sessions || [];
      if (!session) {
        const previous = state.lastWorkspaces.get(pageKey(hostId, showArchived));
        if (previous && page.session?.id !== previous && sessions.some((item) => item.id === previous)) {
          try { session = designPreview ? sessions.find((item) => item.id === previous) : await api(`/api/sessions/${encodeURIComponent(previous)}`); }
          catch (error) { if (error.status !== 404) throw error; }
          if (session && ((session.host_id || '') !== hostId || archived(session) !== showArchived)) session = null;
        }
      }
      if (revision !== state.navigationRevision) return false;
      state.defaults = page.defaults || {};
      state.hosts = page.hosts || [];
      if (!state.openHosts) state.openHosts = (designPreview ? null : readOpenHosts()) || new Set(state.hosts.map((host) => host.id));
      if (hostId && !state.openHosts.has(hostId)) {
        state.openHosts.add(hostId);
        rememberOpenHosts();
      }
      state.hostId = hostId;
      state.archived = showArchived;
      state.sessions = sessions;
      session = session || page.session || null;
      const url = pageUrl(hostId, session?.id, showArchived);
      if (url !== location.pathname + location.search) history[historyMethod](null, '', url);
      applySession(session);
      if (removed) toast('此工作区已删除或不可用');
      return true;
    } catch (error) {
      if (revision === state.navigationRevision) notice('pageError', `${error.message}${!token ? '。请从启动器打开此界面。' : ''}`);
      return false;
    } finally {
      if (revision === state.navigationRevision) {
        state.navigating = false;
        document.querySelector('.workspace').removeAttribute('aria-busy');
        updateActions();
        schedulePoll();
      }
    }
  }
  function selectSession(id) {
    if (id && id !== state.session?.id) loadPage({id, hostId: state.hostId, showArchived: state.archived});
  }
  async function changeWorkspace(action, id, body = {}) {
    if (state.workspaceAction || state.navigating || state.deploying) return;
    const hostId = state.hostId;
    const showArchived = state.archived;
    const revision = state.navigationRevision;
    state.workspaceAction = true;
    updateActions();
    notice('pageError', '');
    try {
      await api(`/api/sessions/${encodeURIComponent(id)}/${action}`, {method: 'POST', body});
      state.draftInputs.delete(id);
      if (action === 'delete' && $('deleteDialog').open) $('deleteDialog').close();
      if (revision !== state.navigationRevision) return;
      const loaded = action === 'assign' ? await loadPage({id}) : await loadPage({hostId, showArchived}, 'replaceState');
      if (loaded) {
        const selected = $('sessionNavigation').querySelector('[aria-current="page"]');
        (selected || $(showArchived ? 'archivedWorkspaces' : 'activeWorkspaces')).focus({preventScroll: true});
      }
      const labels = {assign: '工作区已保存到电脑', archive: '工作区已归档', restore: '工作区已恢复', delete: '工作区记录已删除'};
      toast(labels[action]);
    } catch (error) {
      if (revision === state.navigationRevision) notice(action === 'delete' ? 'deleteError' : 'pageError', error.message);
    } finally {
      state.workspaceAction = false;
      updateActions();
      schedulePoll();
    }
  }
  async function probe() {
    if (state.probing) return;
    const payload = hostPayload();
    const revision = state.connectionRevision;
    const originalParallel = $('parallel').value;
    if (!payload.address) { $('hostAddress').focus(); return; }
    state.probing = true;
    setConnection('checking', '正在检查 SSH 连接和目标环境…');
    $('connectButton').textContent = '连接中…';
    updateActions();
    try {
      const data = await requestHostProbe(payload);
      const host = storeHost({...data.host || data, _checking: false});
      if (connected(host) && !state.openHosts.has(host.id)) {
        state.openHosts.add(host.id);
        rememberOpenHosts();
        renderComputerNavigation();
      }
      if (revision === state.connectionRevision && hostConnectionKey(hostPayload()) === hostConnectionKey(payload)) {
        applyHost(host);
        if (connected(host)) {
          if ((!state.session || !state.session.parallel) && $('parallel').value === originalParallel && Number(originalParallel) === Number(state.defaults.global_limit || 18)) $('parallel').value = host.global_limit || state.defaults.global_limit || 18;
          toast('已连接，电脑信息已保存');
        }
      }
    } catch (error) {
      if (revision === state.connectionRevision) {
        setConnection('failed', error.message);
        if (state.selectedHost && state.selectedHost.address === payload.address) state.selectedHost.status = 'offline';
      }
    } finally {
      state.probing = false;
      $('connectButton').textContent = '连接';
      updateHostHistory(); updateLimits(); updateActions(); renderComputerNavigation(); renderSidebarHost();
    }
  }
  async function deploy(event) {
    event.preventDefault();
    if (state.deploying || state.navigating || state.workspaceAction || !state.session || !state.selectedHost || !$('configForm').reportValidity()) return;
    const id = state.session.id;
    const revision = state.navigationRevision;
    const body = {host_id: state.selectedHost.id, remote_root: $('remoteRoot').value.trim(), parallel: Number($('parallel').value)};
    state.deploying = true;
    updateActions();
    notice('pageError', '');
    try {
      const result = await api(`/api/sessions/${encodeURIComponent(id)}/deploy`, {method: 'POST', body});
      state.draftInputs.delete(id);
      if (revision !== state.navigationRevision) return;
      const session = result.session || result;
      if ((session.host_id || '') !== state.hostId) {
        await loadPage({id}, 'replaceState');
      } else {
        state.session = session;
        renderSession();
        await refreshSession();
      }
    } catch (error) {
      if (revision === state.navigationRevision) notice('pageError', error.message);
    } finally {
      state.deploying = false;
      updateActions();
      schedulePoll();
    }
  }
  async function reconnect() {
    if (!state.session) return;
    if (designPreview) { toast('设计预览使用静态示例，实际任务会自动刷新。'); return; }
    const revision = state.navigationRevision;
    $('reconnectButton').disabled = true;
    try {
      await api(`/api/sessions/${encodeURIComponent(state.session.id)}/reconnect`, {method: 'POST', body: {}});
      if (revision === state.navigationRevision) { toast('正在恢复监控连接'); await refreshSession(); }
    } catch (error) { if (revision === state.navigationRevision) notice('pageError', error.message); }
    finally { $('reconnectButton').disabled = false; }
  }
  async function applyCapacity() {
    if (!state.selectedHost || state.applyingCapacity) return;
    const input = $('capacityInput');
    if (!input.reportValidity()) return;
    const previousHost = state.selectedHost;
    const revision = state.navigationRevision;
    state.applyingCapacity = true;
    $('applyCapacity').textContent = '检查中…';
    notice('capacityMessage', '正在检查计算任务和派发器是否已全部结束…');
    $('capacityMessage').classList.remove('error-text');
    updateActions();
    try {
      const capacity = Number(input.value);
      const result = await api('/api/hosts/capacity', {method: 'POST', body: {id: previousHost.id, capacity, execute: true}});
      const host = storeHost(result.host || {...previousHost, global_limit: capacity});
      if (revision !== state.navigationRevision || state.selectedHost?.id !== host.id) return;
      state.selectedHost = host;
      if (state.session && state.session.snapshot) state.session.snapshot.global_limit = host.global_limit;
      notice('capacityMessage', `全机并行上限已更新为 ${host.global_limit}。本批次并行设置保持不变。`);
      toast('全机并行上限已更新');
      updateHostHistory(); updateLimits();
    } catch (error) { if (revision === state.navigationRevision) { notice('capacityMessage', error.message); $('capacityMessage').classList.add('error-text'); } }
    finally { state.applyingCapacity = false; $('applyCapacity').textContent = '应用'; updateActions(); }
  }
  function closeHistory() { $('hostHistory').hidden = true; $('historyButton').setAttribute('aria-expanded', 'false'); }
  function folderIcon(drive = false) { return drive ? '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><rect x="2" y="4" width="16" height="12" rx="2" stroke="currentColor" stroke-width="1.4"/><path d="M2 12h16" stroke="currentColor" stroke-width="1.4"/><circle cx="14" cy="14" r=".8" fill="currentColor"/></svg>' : '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="M2 6a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6Z" stroke="currentColor" stroke-width="1.4"/></svg>'; }
  function renderBreadcrumbs(path) {
    const crumbs = [{name: '此电脑', path: ''}];
    if (path) {
      const normalized = path.replaceAll('/', '\\');
      const unc = /^\\\\[^\\]+\\[^\\]+/.exec(normalized);
      let acc = unc ? unc[0] : normalized.slice(0, 2);
      crumbs.push({name: acc, path: acc + '\\'});
      const rest = normalized.slice(acc.length).split('\\').filter(Boolean);
      for (const name of rest) { acc += '\\' + name; crumbs.push({name, path: acc}); }
    }
    $('breadcrumbs').innerHTML = crumbs.map((c, i) => `${i ? '<span aria-hidden="true">/</span>' : ''}<button type="button" data-browse-path="${escape(c.path)}">${escape(c.name)}</button>`).join('');
  }
  async function browse(path) {
    if (!state.selectedHost) return;
    const serial = ++state.browseSerial;
    notice('browseError', '');
    $('directoryList').innerHTML = '<p class="directory-message">正在读取远端文件夹…</p>';
    $('useFolderButton').disabled = true;
    $('browsePath').value = path || '';
    try {
      const data = await api(`/api/browse?host_id=${encodeURIComponent(state.selectedHost.id)}&path=${encodeURIComponent(path || '')}`);
      if (serial !== state.browseSerial) return;
      state.browse = data;
      $('browsePath').value = data.path || '';
      renderBreadcrumbs(data.path || '');
      const items = [];
      if (data.parent !== undefined && data.parent !== null && data.path) items.push(`<button class="folder-row" type="button" data-browse-path="${escape(data.parent)}">${folderIcon()}<span>上一级</span></button>`);
      if (!data.path) for (const drive of data.drives || []) {
        const value = typeof drive === 'string' ? drive : drive.path || drive.name;
        items.push(`<button class="folder-row" type="button" data-browse-path="${escape(value)}">${folderIcon(true)}<span>${escape(typeof drive === 'string' ? drive : drive.label || drive.name || drive.path)}</span></button>`);
      }
      for (const folder of data.directories || []) items.push(`<button class="folder-row" type="button" data-browse-path="${escape(folder.path)}">${folderIcon()}<span>${escape(folder.name)}</span></button>`);
      $('directoryList').innerHTML = data.exists === false && data.path ? '<p class="directory-message">此文件夹尚不存在。<br>可以选择此路径，部署时会自动创建。</p>' : items.length ? items.join('') : `<p class="directory-message">${data.path ? '此文件夹下没有子文件夹。' : '未找到可浏览的盘符，请输入完整路径。'}</p>`;
      $('useFolderButton').disabled = !data.path;
      $('useFolderButton').textContent = data.exists === false ? '使用此路径' : '选择此文件夹';
    } catch (error) {
      if (serial !== state.browseSerial) return;
      state.browse = null;
      notice('browseError', error.message);
      $('directoryList').innerHTML = '<p class="directory-message">读取失败，请检查路径或目标电脑的访问权限。</p>';
    }
  }
  async function loadLog() {
    if (!state.log || state.logLoading || !state.session) return;
    if (designPreview) {
      $('logContent').textContent = '[设计预览 · 示例日志]\n[训练] 回合 160 / 200，状态已保存\n[验证] 检查点 EP160，5 个环境正在计算\n[队列] 训练与验证共享同一批次的计算名额\n\n这里会实时同步 Windows 上的任务输出。';
      return;
    }
    const log = state.log;
    state.logLoading = true;
    try {
      const data = await api(`/api/sessions/${encodeURIComponent(state.session.id)}/logs?task_id=${encodeURIComponent(log.task)}&stream=${log.stream}&cursor=${log.cursor}`);
      if (state.log !== log) return;
      if (data.reset || log.cursor === 0) $('logContent').textContent = '';
      if (data.text) $('logContent').textContent += data.text;
      if (!$('logContent').textContent) $('logContent').textContent = data.text === '' ? '暂无日志输出。' : '';
      if (data.text && $('logContent').textContent.startsWith('暂无日志输出。')) $('logContent').textContent = $('logContent').textContent.slice('暂无日志输出。'.length);
      log.cursor = data.cursor || 0;
      if ($('logContent').textContent.length > 500000) $('logContent').textContent = '（已省略较早日志，完整日志保留在目标电脑）\n' + $('logContent').textContent.slice(-400000);
      if ($('followLog').checked) $('logContent').scrollTop = $('logContent').scrollHeight;
      notice('logError', '');
    } catch (error) { if (state.log === log) notice('logError', error.message); }
    finally { state.logLoading = false; }
  }
  async function openLog(taskId) {
    const task = flattenTasks(state.session && state.session.snapshot && state.session.snapshot.tasks || []).find((t) => String(t.id) === taskId) || {};
    $('logTitle').textContent = `${task.label || task.id || taskId} 的日志`;
    $('logAttempt').textContent = task.attempt || '';
    $('logContent').textContent = '正在读取日志…';
    notice('logError', '');
    state.log = {task: taskId, stream: 'stdout', cursor: 0};
    for (const button of document.querySelectorAll('[data-log-stream]')) button.setAttribute('aria-pressed', String(button.dataset.logStream === 'stdout'));
    $('logDialog').showModal();
    await loadLog();
  }
  function setupEvents() {
    window.addEventListener('popstate', () => loadPage(pageFromLocation(), 'replaceState'));
    $('computerNavigation').addEventListener('click', (event) => {
      const link = event.target.closest('a[data-computer-id]');
      if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (link.dataset.computerId !== state.hostId) loadPage({hostId: link.dataset.computerId, showArchived: false});
    });
    $('chooseComputers').addEventListener('click', showComputerChoices);
    $('addComputer').disabled = designPreview;
    $('addComputer').addEventListener('click', async () => {
      $('computerDialog').close();
      if (state.hostId || state.archived) await loadPage({hostId: ''});
      $('hostAddress').focus();
    });
    $('computerForm').addEventListener('submit', (event) => {
      event.preventDefault();
      const selected = [...$('computerChoices').querySelectorAll('input:checked')].map((input) => input.value);
      state.openHosts = new Set(selected);
      rememberOpenHosts();
      $('computerDialog').close();
      if ((state.hostId && !state.openHosts.has(state.hostId)) || (!state.hostId && !state.session && selected.length)) {
        loadPage({hostId: selected[0] || ''});
      } else {
        renderComputerNavigation();
      }
    });
    $('activeWorkspaces').addEventListener('click', () => {
      if (state.archived) loadPage({hostId: state.hostId});
    });
    $('archivedWorkspaces').addEventListener('click', () => {
      if (!state.archived) loadPage({hostId: state.hostId, showArchived: true});
    });
    $('assignWorkspace').addEventListener('click', () => {
      if (!state.session || !state.selectedHost || !$('configForm').reportValidity()) return;
      changeWorkspace('assign', state.session.id, {host_id: state.selectedHost.id, remote_root: $('remoteRoot').value.trim(), parallel: Number($('parallel').value)});
    });
    $('archiveWorkspace').addEventListener('click', () => { if (state.session) changeWorkspace('archive', state.session.id); });
    $('restoreWorkspace').addEventListener('click', () => { if (state.session) changeWorkspace('restore', state.session.id); });
    $('deleteWorkspace').addEventListener('click', () => {
      if (!state.session?.can_delete) return;
      state.deleteTarget = state.session.id;
      $('deleteDescription').textContent = `移除「${sessionTitle(state.session)}」在运行器里的记录，保留远端目录和计算结果。删除后无法恢复这条记录。`;
      notice('deleteError', '');
      $('deleteDialog').showModal();
    });
    $('deleteForm').addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!state.deleteTarget || state.workspaceAction) return;
      $('confirmDelete').disabled = true;
      try { await changeWorkspace('delete', state.deleteTarget); }
      finally { $('confirmDelete').disabled = false; }
    });
    if ($('sessionNavigation')) $('sessionNavigation').addEventListener('click', (event) => {
      const button = event.target.closest('[data-session-id]');
      if (button) selectSession(button.dataset.sessionId);
    });
    if ($('sidebarSettings')) $('sidebarSettings').addEventListener('click', () => {
      if (state.session) state.configurationExpanded.set(state.session.id, true);
      updateConfigurationView();
      $('configHeading').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start'});
    });
    $('toggleConfiguration').addEventListener('click', () => {
      if (!state.session) return;
      state.configurationExpanded.set(state.session.id, state.configurationExpanded.get(state.session.id) !== true);
      updateConfigurationView();
    });
    $('sessionSelect').addEventListener('change', (e) => selectSession(e.target.value));
    $('hostAddress').addEventListener('input', () => {
      state.connectionRevision++;
      if (state.selectedHost && state.selectedHost.address !== $('hostAddress').value.trim()) { state.selectedHost = null; setConnection('', '连接成功后自动保存，可以为电脑重命名。'); }
      updateHostHistory(); updateLimits(); updateActions();
    });
    for (const id of ['sshUser', 'sshPort', 'identityFile', 'pythonPath', 'runtimeRoot', 'slotRoot']) $(id).addEventListener('input', () => { state.connectionRevision++; if (state.selectedHost) { state.selectedHost = null; setConnection('', '连接设置已更改，请重新连接。'); } updateHostHistory(); updateLimits(); updateActions(); });
    $('hostAddress').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); probe(); } });
    $('connectButton').addEventListener('click', probe);
    $('historyButton').addEventListener('click', () => { const open = $('hostHistory').hidden; $('hostHistory').hidden = !open; $('historyButton').setAttribute('aria-expanded', String(open)); if (open) { for (const host of [...state.hosts]) silentProbe(host); $('hostHistory').querySelector('button')?.focus(); } });
    $('hostHistory').addEventListener('click', (event) => { const target = event.target.closest('[data-host-id]'); if (!target) return; const host = state.hosts.find((h) => h.id === target.dataset.hostId); if (host) { applyHost(host); closeHistory(); $('hostAddress').focus(); silentProbe(host); } });
    document.addEventListener('click', (e) => { if (!$('hostHistory').contains(e.target) && !$('historyButton').contains(e.target)) closeHistory(); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeHistory(); });
    $('renameButton').addEventListener('click', () => { if (!state.selectedHost) return; $('hostLabel').value = state.selectedHost.label || state.selectedHost.address; notice('renameError', ''); $('renameDialog').showModal(); $('hostLabel').select(); });
    $('renameForm').addEventListener('submit', async (e) => {
      e.preventDefault(); if (!state.selectedHost || !$('hostLabel').value.trim()) return;
      const id = state.selectedHost.id;
      const label = $('hostLabel').value.trim();
      try {
        const data = await api('/api/hosts/rename', {method: 'POST', body: {id, label}});
        const host = storeHost(data.host || {id, label});
        if (state.selectedHost?.id === id) state.selectedHost = host;
        updateHostHistory();
        if ($('renameDialog').open) $('renameDialog').close();
        renderSession();
        toast('名称已保存');
      } catch (error) { notice('renameError', error.message); }
    });
    $('parallel').addEventListener('input', () => { updateLimits(); updateActions(); });
    $('remoteRoot').addEventListener('input', updateActions);
    for (const [id, step] of [['decreaseParallel', -1], ['increaseParallel', 1]]) $(id).addEventListener('click', () => { $('parallel').value = clamp(Number($('parallel').value || 1) + step, 36) || 1; updateLimits(); updateActions(); });
    $('configForm').addEventListener('submit', deploy);
    $('reconnectButton').addEventListener('click', reconnect);
    $('applyCapacity').addEventListener('click', applyCapacity);
    $('browseButton').addEventListener('click', () => { $('folderDialog').showModal(); browse($('remoteRoot').value.trim()); });
    $('browseGo').addEventListener('click', () => browse($('browsePath').value.trim()));
    $('browsePath').addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); browse(e.target.value.trim()); } });
    for (const id of ['breadcrumbs', 'directoryList']) $(id).addEventListener('click', (e) => { const button = e.target.closest('[data-browse-path]'); if (button) browse(button.dataset.browsePath); });
    $('useFolderButton').addEventListener('click', () => { if (!state.browse || !state.browse.path) return; $('remoteRoot').value = state.browse.path; $('folderDialog').close(); updateActions(); });
    for (const button of document.querySelectorAll('[data-close]')) button.addEventListener('click', () => $(button.dataset.close).close());
    for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('click', (e) => { if (e.target !== dialog) return; const rect = dialog.getBoundingClientRect(); if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) dialog.close(); });
    $('taskList').addEventListener('click', (e) => { const button = e.target.closest('[data-log-task]'); if (button) openLog(button.dataset.logTask); });
    $('refreshLog').addEventListener('click', loadLog);
    $('logDialog').addEventListener('close', () => { state.log = null; });
    for (const button of document.querySelectorAll('[data-log-stream]')) button.addEventListener('click', () => { if (!state.log) return; state.log = {task: state.log.task, stream: button.dataset.logStream, cursor: 0}; $('logContent').textContent = '正在读取日志…'; for (const b of document.querySelectorAll('[data-log-stream]')) b.setAttribute('aria-pressed', String(b === button)); loadLog(); });
  }
  function init() {
    setupEvents();
    if (designPreview) initDesignPreview();
    else loadPage(pageFromLocation(), 'replaceState');
  }
  function initDesignPreview() {
    document.body.classList.add('design-preview');
    const home = document.querySelector('.brand');
    if (home) home.href = '/?preview=design';
    if ($('previewBadge')) $('previewBadge').hidden = false;
    const now = Date.now() / 1000;
    const host = {id:'design-node',label:'Windows 工作站',address:'192.0.2.10',status:'online',global_limit:18,
      python:'C:\\Python\\python.exe',runtime_root:'C:\\RemoteRunner\\runtime',slot_root:'C:\\RemoteRunner\\slots',port:22,user:'runner',checked_unix:now};
    const metric = (id,label,current,total,unit) => ({id,label,current,total,unit,contributes_to_overall:true});
    const groupA = {id:'full',label:'模型 A',parent_id:null,status:'running',phase:'validation',attempt:'a0001',
      metrics:[metric('training','训练',160,200,'回合'),metric('validation','验证',18,40,'场')],metadata:{group:'A'}};
    const groupB = {id:'without-trajectory',label:'模型 B',parent_id:null,status:'running',phase:'training',attempt:'a0001',
      metrics:[metric('training','训练',124,200,'回合'),metric('validation','验证',12,40,'场')],metadata:{group:'B'}};
    const children = Array.from({length:5},(_,index)=>({id:`full/env-${index+1}`,label:`验证环境 ${String(index+1).padStart(2,'0')}`,parent_id:'full',status:'running',phase:'validation',attempt:'a0001',
      metrics:[{...metric('simulation','仿真',index+3,10,'秒'),contributes_to_overall:false}]}));
    const workers = [{task_id:groupB.id,label:'模型 B',phase:'training',slot:0,attempt:'a0001'},
      ...children.map((child,index)=>({task_id:child.id,label:child.label,phase:'validation',slot:index+1,attempt:'a0001'}))];
    const base = {status:'running',updated_unix:now,stale:false,tasks:[groupA,groupB,...children],active_workers:workers,
      resources:{cpu_percent:68,memory_percent:46,memory_used_gib:14.7,memory_total_gib:32,gpus:[{name:'GPU 0',utilization_percent:34}]},
      global_used:6,global_limit:18,overall:{current:314,total:480,percent:314/480*100},eta_seconds:2260};
    const session = (id,title,status,snapshot,summary) => ({id,status,host_id:host.id,parallel:18,remote_root:'C:\\RemoteRunner\\workspaces\\sample',
      request:{title,summary,mode:'deploy'},snapshot,events:[{time:now-2260,message:'任务包已部署，输入校验通过'},{time:now-2255,message:'训练与验证正在同一队列内运行'}]});
    const main = session('design-ablation','模型对照实验','running',base,'对照两组示例模型，训练与验证共享计算名额。');
    const finished = session('design-scale','参数对照测试','completed',{...base,status:'complete',tasks:[{id:'scale',label:'独立测试',status:'complete',phase:'testing',metrics:[metric('testing','测试',50,50,'场')]}],active_workers:[],global_used:6,overall:{current:50,total:50,percent:100},eta_seconds:0},'比较不同参数设置的独立测试。');
    const validationChildren = children.slice(0,3).map((child,index)=>({...child,id:`validation/env-${index+1}`,parent_id:'validation'}));
    const validating = session('design-validation','模型检查点验证','running',{...base,tasks:[{id:'validation',label:'检查点 EP160',status:'running',phase:'validation',metrics:[metric('validation','验证',27,40,'场')]},...validationChildren],active_workers:validationChildren.map((child,index)=>({task_id:child.id,label:child.label,phase:'validation',slot:index,attempt:'a0001'})),overall:{current:27,total:40,percent:67.5}},'使用固定检查点在独立环境中验证模型。');
    const waiting = session('design-new','新建远程实验','draft',null,'设置目标电脑、工作目录和并行上限，准备下一次运行。');
    state.defaults={host:host.address,python:host.python,runtime_root:host.runtime_root,slot_root:host.slot_root,remote_root:main.remote_root,global_limit:18};
    const secondHost = {...host, id: 'design-second', address: '192.0.2.11', label: '备用工作站', status: 'offline'};
    state.hosts = [host, secondHost];
    state.previewSessions = [main, validating, finished, {...waiting, host_id: secondHost.id}];
    const route = pageFromLocation();
    if (!route.hostId && !route.id && !route.showArchived) route.hostId = host.id;
    const selected = state.previewSessions.find((row) => row.id === route.id);
    if (selected) route.hostId = selected.host_id;
    loadPage(route, 'replaceState');
  }
  init();
})();
