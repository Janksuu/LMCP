
(function () {
'use strict';

function nc(v, fb) { return (v === null || v === undefined) ? fb : v; }
function get(obj, path, fb) {
  if (!obj) return fb;
  const parts = path.split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur === null || cur === undefined) return fb;
    cur = cur[p];
  }
  return (cur === null || cur === undefined) ? fb : cur;
}


// ─── XSS-safe escape (required by handoff) ───────────────
function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function el(id) { return document.getElementById(id); }
function q(sel, root) { return (root || document).querySelector(sel); }
function qa(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

// ─── State ───────────────────────────────────────────────
const state = {
  status: null,       // from /status
  registry: null,     // from /registry/view (mgmt only)
  pending: {          // staged edits
    clients: {},      // client_id -> { allow_servers?: [...], rate_limit_rpm?: n, _action?: 'add'|'remove' }
    servers: {},      // server_id -> { tool_policy_mode?, timeouts?, _action?: 'remove' }
  },
  events: [],         // tail of recent SSE events
  eventFilter: '',
  sse: null,
  mgmtToken: sessionStorage.getItem('lmcp_mgmt_token') || null,
  mode: 'readonly',   // 'readonly' | 'management'
  activeNav: 'matrix',
  expandedClient: null,
  expandedServer: null,
  applyError: null,
  applyWarning: null,
};

// ─── Reference/demo data (used when endpoints unreachable) ─
const DEMO = {
  status: {
    status_version: 3,
    service: 'lmcp-v3',
    host: '127.0.0.1',
    port: 7345,
    loopback_only: true,
    uptime_s: 14532.8,
    clients: [
      { client_id: 'vscode', token_status: 'set', allow_servers: ['ollama-mcp', 'comfyui-mcp', 'figshare'], rate_limit_rpm: 120 },
      { client_id: 'claude-desktop', token_status: 'set', allow_servers: ['ollama-mcp', 'figshare'], rate_limit_rpm: null },
      { client_id: 'codex', token_status: 'placeholder', allow_servers: [], rate_limit_rpm: null },
    ],
    servers: [
      { server_id: 'ollama-mcp',   transport: 'stdio', available_hint: true,  tool_policy_mode: 'allow_all',  timeouts: { initialize_s: 30, tools_list_s: 30, tools_call_s: 300, retry_on_timeout: 1, retry_backoff_s: 1.5 } },
      { server_id: 'comfyui-mcp',  transport: 'http',  available_hint: true,  tool_policy_mode: 'allow_all',  timeouts: { initialize_s: 60, tools_list_s: 60, tools_call_s: 600, retry_on_timeout: 1, retry_backoff_s: 2 } },
      { server_id: 'figshare',     transport: 'stdio', available_hint: true,  tool_policy_mode: 'allow_all',  timeouts: { initialize_s: 30, tools_list_s: 30, tools_call_s: 300, retry_on_timeout: 1, retry_backoff_s: 1.5 } },
      { server_id: 'playwright-docker', transport: 'stdio', available_hint: false, tool_policy_mode: 'deny_all', timeouts: { initialize_s: 45, tools_list_s: 45, tools_call_s: 600, retry_on_timeout: 0, retry_backoff_s: 1 } },
    ],
    recent_audit_entries: [
      { event: 'client_auth',   client_id: 'vscode',         server_id: null,          tool_name: null, allowed: true,  reason: 'token_match',        detail: null, ts: nowIso(-40) },
      { event: 'server_auth',   client_id: 'vscode',         server_id: 'ollama-mcp',  tool_name: null, allowed: true,  reason: 'on_list',            detail: null, ts: nowIso(-39) },
      { event: 'server_auth',   client_id: 'claude-desktop', server_id: 'ollama-mcp',  tool_name: null, allowed: true,  reason: 'on_list',            detail: null, ts: nowIso(-28) },
      { event: 'rate_limited',  client_id: 'codex',          server_id: null,          tool_name: null, allowed: false, reason: 'rate_limit_exceeded',detail: null, ts: nowIso(-22) },
      { event: 'server_auth',   client_id: 'codex',          server_id: 'ollama-mcp',  tool_name: null, allowed: false, reason: 'not_on_list',        detail: null, ts: nowIso(-18) },
      { event: 'config_change', client_id: null,             server_id: null,          tool_name: null, allowed: true,  reason: 'management_apply',   detail: { changes: '+1 client mod' }, ts: nowIso(-12) },
    ],
  },
  registry: {
    lmcp: { host: '127.0.0.1', port: 7345, audit_log: '/var/log/lmcp/audit.jsonl', loopback_only: true, rate_limit_rpm: null },
    clients: {
      'vscode':         { token_status: 'set',         allow_servers: ['ollama-mcp','comfyui-mcp','figshare'], rate_limit_rpm: 120 },
      'claude-desktop': { token_status: 'set',         allow_servers: ['ollama-mcp','figshare'],                rate_limit_rpm: null },
      'codex':          { token_status: 'placeholder', allow_servers: [],                                        rate_limit_rpm: null },
    },
    servers: {
      'ollama-mcp':        { transport: 'stdio', command: 'npx', args: ['-y','ollama-mcp-server'], env: {}, cwd: null, stdio_mode: 'newline', tool_policy: { mode: 'allow_all', allow_tools: [], deny_tools: [] }, timeouts: { initialize_s: 30, tools_list_s: 30, tools_call_s: 300, retry_on_timeout: 1, retry_backoff_s: 1.5 } },
      'comfyui-mcp':       { transport: 'http',  url: 'http://127.0.0.1:9000/mcp', headers: { 'X-Api-Key': '***redacted***' }, tool_policy: { mode: 'allow_all', allow_tools: [], deny_tools: [] }, timeouts: { initialize_s: 60, tools_list_s: 60, tools_call_s: 600, retry_on_timeout: 1, retry_backoff_s: 2 } },
      'figshare':          { transport: 'stdio', command: 'python', args: ['-m','figshare_mcp'], env: { FIGSHARE_TOKEN: '***redacted***' }, cwd: null, stdio_mode: 'newline', tool_policy: { mode: 'allow_all', allow_tools: [], deny_tools: [] }, timeouts: { initialize_s: 30, tools_list_s: 30, tools_call_s: 300, retry_on_timeout: 1, retry_backoff_s: 1.5 } },
      'playwright-docker': { transport: 'stdio', command: 'docker', args: ['run','-i','--rm','mcp/playwright'], env: {}, cwd: null, stdio_mode: 'newline', tool_policy: { mode: 'deny_all', allow_tools: [], deny_tools: [] }, timeouts: { initialize_s: 45, tools_list_s: 45, tools_call_s: 600, retry_on_timeout: 0, retry_backoff_s: 1 } },
    },
  },
};

function nowIso(offsetSeconds) {
  const t = Date.now() + (offsetSeconds || 0) * 1000;
  return new Date(t).toISOString();
}

// ─── Fetch wrappers with graceful demo fallback ───────────
async function apiGet(path, needsMgmt) {
  const headers = {};
  if (needsMgmt && state.mgmtToken) headers['X-Lmcp-Management-Token'] = state.mgmtToken;
  try {
    const r = await fetch(path, { headers });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return await r.json();
  } catch (e) {
    // demo fallback
    if (path === '/status') return DEMO.status;
    if (path === '/registry/view') return DEMO.registry;
    throw e;
  }
}

async function apiPost(path, body) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.mgmtToken) headers['X-Lmcp-Management-Token'] = state.mgmtToken;
  try {
    const r = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      return { _httpError: true, status: r.status, body: data };
    }
    return data;
  } catch (e) {
    // offline simulation: pretend validation passed
    return simulateApply(path, body);
  }
}

function simulateApply(path, body) {
  const summary = buildChangesSummary();
  if (path === '/registry/validate') {
    return { valid: true, errors: [], changes_summary: summary };
  }
  if (path === '/registry/apply') {
    applyPendingToDemo();
    return {
      applied: true, errors: [], changes_summary: summary,
      backup_path: '/var/lib/lmcp/registry.yaml.bak.20260419_143215',
    };
  }
  return {};
}

function applyPendingToDemo() {
  // mutate DEMO.registry + DEMO.status to match staged edits
  Object.entries(state.pending.clients).forEach(([cid, patch]) => {
    if (patch._action === 'remove') {
      delete DEMO.registry.clients[cid];
      DEMO.status.clients = DEMO.status.clients.filter(c => c.client_id !== cid);
      return;
    }
    if (!DEMO.registry.clients[cid]) {
      DEMO.registry.clients[cid] = { token_status: 'placeholder', allow_servers: [], rate_limit_rpm: null };
      DEMO.status.clients.push({ client_id: cid, token_status: 'placeholder', allow_servers: [], rate_limit_rpm: null });
    }
    if (patch.allow_servers !== undefined) {
      DEMO.registry.clients[cid].allow_servers = [...patch.allow_servers];
      const s = DEMO.status.clients.find(c => c.client_id === cid);
      if (s) s.allow_servers = [...patch.allow_servers];
    }
    if (patch.rate_limit_rpm !== undefined) {
      DEMO.registry.clients[cid].rate_limit_rpm = patch.rate_limit_rpm;
      const s = DEMO.status.clients.find(c => c.client_id === cid);
      if (s) s.rate_limit_rpm = patch.rate_limit_rpm;
    }
    if (patch.token_status !== undefined) {
      DEMO.registry.clients[cid].token_status = patch.token_status;
      const s = DEMO.status.clients.find(c => c.client_id === cid);
      if (s) s.token_status = patch.token_status;
    }
  });
  Object.entries(state.pending.servers).forEach(([sid, patch]) => {
    if (patch._action === 'remove') {
      delete DEMO.registry.servers[sid];
      DEMO.status.servers = DEMO.status.servers.filter(s => s.server_id !== sid);
      // cascade: remove from client allowlists
      Object.values(DEMO.registry.clients).forEach(c => {
        c.allow_servers = c.allow_servers.filter(x => x !== sid);
      });
      DEMO.status.clients.forEach(c => {
        c.allow_servers = c.allow_servers.filter(x => x !== sid);
      });
      return;
    }
    if (patch.tool_policy_mode !== undefined) {
      if (DEMO.registry.servers[sid]) {
        DEMO.registry.servers[sid].tool_policy.mode = patch.tool_policy_mode;
      }
      const s = DEMO.status.servers.find(x => x.server_id === sid);
      if (s) s.tool_policy_mode = patch.tool_policy_mode;
    }
  });
  // emit a synthetic config_change event
  pushEvent({
    event_type: 'config_change',
    event_version: 1,
    timestamp: nowIso(0),
    payload: { allowed: true, reason: 'management_apply' },
  });
}

// ─── Pending / diff helpers ──────────────────────────────
function hasPending() {
  return Object.keys(state.pending.clients).length > 0 ||
         Object.keys(state.pending.servers).length > 0;
}

function buildChangesSummary() {
  let clients_added = 0, clients_modified = 0, clients_removed = 0;
  let servers_added = 0, servers_modified = 0, servers_removed = 0;
  Object.values(state.pending.clients).forEach(p => {
    if (p._action === 'add') clients_added++;
    else if (p._action === 'remove') clients_removed++;
    else clients_modified++;
  });
  Object.values(state.pending.servers).forEach(p => {
    if (p._action === 'add') servers_added++;
    else if (p._action === 'remove') servers_removed++;
    else servers_modified++;
  });
  return { clients_added, clients_modified, clients_removed, servers_added, servers_modified, servers_removed };
}

function buildHumanDiff() {
  // Array of { glyph, kind, text }
  const out = [];
  Object.entries(state.pending.clients).forEach(([cid, patch]) => {
    if (patch._action === 'remove') {
      out.push({ glyph: '−', kind: 'rem', text: 'remove client ' + cid });
      return;
    }
    if (patch._action === 'add') {
      out.push({ glyph: '+', kind: 'add', text: 'add client ' + cid });
    }
    if (patch.allow_servers !== undefined) {
      const cur = ((state.registry.clients[cid] && state.registry.clients[cid].allow_servers)) || [];
      const next = patch.allow_servers;
      const added = next.filter(x => !cur.includes(x));
      const removed = cur.filter(x => !next.includes(x));
      added.forEach(s => out.push({ glyph: '+', kind: 'add', text: 'client ' + cid + ' gains access to ' + s }));
      removed.forEach(s => out.push({ glyph: '−', kind: 'rem', text: 'client ' + cid + ' loses access to ' + s }));
    }
    if (patch.rate_limit_rpm !== undefined) {
      const cur = (state.registry.clients[cid] && state.registry.clients[cid].rate_limit_rpm);
      out.push({ glyph: '~', kind: 'mod', text: 'client ' + cid + ' rate_limit_rpm: ' + nc(cur, 'unlimited') + ' → ' + nc(patch.rate_limit_rpm, 'unlimited') });
    }
    if (patch.token_status !== undefined) {
      out.push({ glyph: '~', kind: 'mod', text: 'client ' + cid + ' token rotated' });
    }
  });
  Object.entries(state.pending.servers).forEach(([sid, patch]) => {
    if (patch._action === 'remove') {
      out.push({ glyph: '−', kind: 'rem', text: 'remove server ' + sid + ' (cascades to client allowlists)' });
      return;
    }
    if (patch.tool_policy_mode !== undefined) {
      const cur = (state.registry.servers[sid] && state.registry.servers[sid].tool_policy && state.registry.servers[sid].tool_policy.mode) || '?';
      out.push({ glyph: '~', kind: 'mod', text: 'server ' + sid + ' tool_policy.mode: ' + cur + ' → ' + patch.tool_policy_mode });
    }
  });
  return out;
}

function buildPatch() {
  // Return a patch object matching /registry/validate and /registry/apply contract.
  const patch = { clients: {}, servers: {} };
  Object.entries(state.pending.clients).forEach(([cid, p]) => {
    if (p._action === 'remove') { patch.clients[cid] = null; return; }
    const entry = {};
    if (p.allow_servers !== undefined) entry.allow_servers = p.allow_servers;
    if (p.rate_limit_rpm !== undefined) entry.rate_limit_rpm = p.rate_limit_rpm;
    if (p.token !== undefined) entry.token = p.token;
    patch.clients[cid] = entry;
  });
  Object.entries(state.pending.servers).forEach(([sid, p]) => {
    if (p._action === 'remove') { patch.servers[sid] = null; return; }
    const entry = {};
    if (p.tool_policy_mode !== undefined) entry.tool_policy = { mode: p.tool_policy_mode };
    patch.servers[sid] = entry;
  });
  return patch;
}

function discardPending() {
  state.pending = { clients: {}, servers: {} };
  state.applyError = null;
  state.applyWarning = null;
  render();
}

// ─── Matrix edits ────────────────────────────────────────
function toggleMatrixCell(clientId, serverId) {
  if (state.mode !== 'management') return;
  // effective current state = pending override or base
  const pendingC = state.pending.clients[clientId];
  const base = ((state.registry.clients[clientId] && state.registry.clients[clientId].allow_servers) || []);
  const current = (pendingC && pendingC.allow_servers) || base;
  const isOn = current.includes(serverId);
  const next = isOn ? current.filter(x => x !== serverId) : [...current, serverId];
  // determine dirty vs reverted
  const baseSorted = [...base].sort().join(',');
  const nextSorted = [...next].sort().join(',');
  if (baseSorted === nextSorted) {
    // reverted to base: clear allow_servers from pending
    if (pendingC) {
      delete pendingC.allow_servers;
      if (Object.keys(pendingC).length === 0) delete state.pending.clients[clientId];
    }
  } else {
    if (!state.pending.clients[clientId]) state.pending.clients[clientId] = {};
    state.pending.clients[clientId].allow_servers = next;
  }
  render();
}

function effectiveAllow(clientId) {
  const pendingC = state.pending.clients[clientId];
  if (pendingC && pendingC.allow_servers) return pendingC.allow_servers;
  return ((state.registry && state.registry.clients && state.registry.clients[clientId] && state.registry.clients[clientId].allow_servers) || []);
}

function isDirtyCell(clientId, serverId) {
  const pendingC = state.pending.clients[clientId];
  if (!pendingC || !pendingC.allow_servers) return false;
  const base = ((state.registry && state.registry.clients && state.registry.clients[clientId] && state.registry.clients[clientId].allow_servers) || []);
  const next = pendingC.allow_servers;
  return base.includes(serverId) !== next.includes(serverId);
}

// ─── Event stream ────────────────────────────────────────
function pushEvent(ev) {
  state.events.unshift(ev);
  if (state.events.length > 200) state.events.length = 200;
  renderEvents();
}

function connectSSE() {
  if (state.sse) state.sse.close();
  el('sse-state').textContent = 'connecting…';
  el('sb-events-state').textContent = 'connecting';
  try {
    const src = new EventSource('/events');
    state.sse = src;
    src.onopen = () => {
      el('sse-state').textContent = 'connected';
      el('sb-events-state').textContent = 'connected';
      el('sb-events-state').style.color = 'var(--green)';
    };
    src.onerror = () => {
      el('sse-state').textContent = 'reconnecting…';
      el('sb-events-state').textContent = 'reconnecting';
      el('sb-events-state').style.color = 'var(--amber)';
    };
    src.onmessage = (e) => {
      try { pushEvent(JSON.parse(e.data)); } catch (_) {}
    };
    // handle named event types too
    ['client_auth','server_auth','rate_limited','config_change','server_error','tool_call','tool_result'].forEach(t => {
      src.addEventListener(t, (e) => {
        try { pushEvent(JSON.parse(e.data)); } catch (_) {}
      });
    });
  } catch (e) {
    // fallback: seed from status, then simulate a tick
    el('sse-state').textContent = 'offline · demo';
    el('sb-events-state').textContent = 'demo';
    el('sb-events-state').style.color = 'var(--amber)';
    startDemoEventLoop();
  }
}

function startDemoEventLoop() {
  // Seed from recent_audit_entries
  (DEMO.status.recent_audit_entries || []).slice().reverse().forEach(a => {
    pushEvent({
      event_type: a.event,
      event_version: 1,
      timestamp: a.ts,
      payload: { client_id: a.client_id, server_id: a.server_id, allowed: a.allowed, reason: a.reason, tool_name: a.tool_name },
    });
  });
  // Gently emit synthetic events
  const demoSources = [
    { event_type: 'client_auth', payload: { client_id: 'vscode', allowed: true, reason: 'token_match' } },
    { event_type: 'server_auth', payload: { client_id: 'vscode', server_id: 'ollama-mcp', allowed: true, reason: 'on_list' } },
    { event_type: 'server_auth', payload: { client_id: 'claude-desktop', server_id: 'figshare', allowed: true, reason: 'on_list' } },
    { event_type: 'rate_limited', payload: { client_id: 'codex', allowed: false, reason: 'rate_limit_exceeded' } },
    { event_type: 'server_auth', payload: { client_id: 'codex', server_id: 'ollama-mcp', allowed: false, reason: 'not_on_list' } },
    { event_type: 'tool_call', payload: { client_id: 'vscode', server_id: 'ollama-mcp', tool_name: 'generate', allowed: true } },
  ];
  let i = 0;
  setInterval(() => {
    const src = demoSources[i % demoSources.length];
    pushEvent({ event_type: src.event_type, event_version: 1, timestamp: nowIso(0), payload: src.payload });
    i++;
  }, 4200);
}

// ─── Rendering ───────────────────────────────────────────
function render() {
  renderTopBar();
  renderNav();
  renderMain();
  renderPendingBar();
}

function renderTopBar() {
  const s = state.status;
  if (!s) return;
  el('tb-version').textContent = 'v' + (nc(s.status_version, '?'));
  el('tb-dot').className = 'tb-dot ' + (s.service ? 'live' : 'down');
  el('tb-host').textContent = s.host + ':' + s.port + (s.loopback_only ? '' : ' · remote');
  el('tb-uptime').textContent = fmtUptime(s.uptime_s);
  el('tb-clients').textContent = String((s.clients ? s.clients.length : 0));
  el('tb-servers').textContent = String((s.servers ? s.servers.length : 0));
  const pill = el('mode-pill');
  if (state.mode === 'management') {
    pill.textContent = '● management';
    pill.className = 'mode-pill unlocked';
    el('btn-unlock').textContent = 'Lock';
    el('btn-unlock').classList.remove('amber');
  } else {
    pill.textContent = '● read-only';
    pill.className = 'mode-pill';
    el('btn-unlock').textContent = 'Unlock management';
    el('btn-unlock').classList.add('amber');
  }
  el('sb-statusver').textContent = String(s.status_version);
  el('sb-registry').textContent = (state.registry && state.registry.lmcp && state.registry.lmcp.audit_log) ? 'loaded' : (state.mode === 'management' ? 'loading' : 'gated');
  el('sb-status-state').textContent = 'ok';
  el('sb-status-state').style.color = 'var(--green)';
}

function renderNav() {
  qa('.nav-item').forEach(n => {
    n.classList.toggle('active', n.dataset.nav === state.activeNav);
  });
}

function renderMain() {
  const root = el('main-content');
  if (state.activeNav === 'matrix') root.innerHTML = renderMatrixHTML();
  else if (state.activeNav === 'clients') root.innerHTML = renderClientsHTML();
  else if (state.activeNav === 'servers') root.innerHTML = renderServersHTML();
  else if (state.activeNav === 'settings') root.innerHTML = renderSettingsHTML();
  bindMainEvents();
}

function renderMatrixHTML() {
  const s = state.status;
  if (!s) return '<div class="empty-state">loading…</div>';
  const registry = state.registry;
  const clients = s.clients || [];
  const servers = s.servers || [];
  const locked = state.mode !== 'management';
  const lockedNote = locked
    ? '<span class="section-id">unlock management to edit</span>'
    : '<span class="section-id">toggle cells to stage changes</span>';

  let head = '<tr><th></th>';
  servers.forEach(sv => {
    const avail = sv.available_hint ? '● available' : '○ unreachable';
    head += '<th class="server-head">'
      + '<div class="sid">' + esc(sv.server_id) + '</div>'
      + '<div class="smeta"><span class="stransport">' + esc(sv.transport) + '</span> · ' + esc(sv.tool_policy_mode) + ' · ' + esc(avail) + '</div>'
      + '</th>';
  });
  head += '</tr>';

  let body = '';
  clients.forEach(c => {
    const allow = locked
      ? (c.allow_servers || [])
      : effectiveAllow(c.client_id);
    const rpm = c.rate_limit_rpm == null ? 'unlimited' : c.rate_limit_rpm + ' rpm';
    let row = '<tr>';
    row += '<td class="client-cell">'
      + '<div class="cid">' + esc(c.client_id) + '</div>'
      + '<div class="cmeta">'
      +   '<span class="token-pill ' + esc(c.token_status) + '">' + esc(c.token_status) + '</span>'
      +   '<span>' + esc(rpm) + '</span>'
      + '</div>'
      + '</td>';
    servers.forEach(sv => {
      const on = allow.includes(sv.server_id);
      const dirty = !locked && isDirtyCell(c.client_id, sv.server_id);
      const cls = [
        'cell-btn',
        on ? 'on' : 'off',
        dirty ? 'dirty' : '',
        locked ? 'locked' : '',
      ].filter(Boolean).join(' ');
      const glyph = on ? '●' : (dirty ? '◌' : '');
      row += '<td><span class="' + cls + '" data-act="toggle-cell" '
        + 'data-client="' + esc(c.client_id) + '" data-server="' + esc(sv.server_id) + '" '
        + 'style="position:relative;">' + glyph + '</span></td>';
    });
    row += '</tr>';
    body += row;
  });

  return ''
    + '<div class="section-head">'
    +   '<span class="section-title">Permission matrix · clients × servers</span>'
    +   lockedNote
    + '</div>'
    + '<div class="matrix-wrap scroll">'
    +   '<table class="matrix"><thead>' + head + '</thead><tbody>' + body + '</tbody></table>'
    + '</div>'
    + renderLegend();
}

function renderLegend() {
  return ''
    + '<div style="display:flex;gap:20px;margin-top:14px;font-family:var(--mono);font-size:10px;color:var(--fg-2);text-transform:uppercase;letter-spacing:0.12em;">'
    +   '<span><span style="color:var(--amber)">●</span> allowed</span>'
    +   '<span><span style="color:var(--amber)">◌</span> staged change</span>'
    +   '<span>token · <span style="color:var(--green)">set</span> / <span style="color:var(--amber)">placeholder</span> / <span style="color:var(--red)">empty</span></span>'
    + '</div>';
}

function renderClientsHTML() {
  const s = state.status;
  if (!s) return '<div class="empty-state">loading…</div>';
  const locked = state.mode !== 'management';
  let rows = '';
  (s.clients || []).forEach(c => {
    const expanded = state.expandedClient === c.client_id;
    const pendingC = state.pending.clients[c.client_id];
    const dirty = !!pendingC;
    const rpmDisplay = c.rate_limit_rpm == null ? 'unlimited' : c.rate_limit_rpm + ' rpm';
    rows += '<div class="list-row ' + (expanded ? 'expanded' : '') + '" data-act="toggle-client" data-client="' + esc(c.client_id) + '">'
      + '<div class="row-head">'
      +   '<span class="row-id">' + esc(c.client_id) + '</span>'
      +   '<span class="token-pill ' + esc(c.token_status) + '" style="font-family:var(--mono);font-size:9px;letter-spacing:0.1em;padding:1px 5px;border:1px solid var(--line);">' + esc(c.token_status) + '</span>'
      +   (dirty ? '<span class="mono" style="font-size:10px;color:var(--amber);letter-spacing:0.12em;">· edited</span>' : '')
      +   '<div class="row-meta">'
      +     '<span>' + ((c.allow_servers ? c.allow_servers.length : 0)) + ' servers</span>'
      +     '<span>' + esc(rpmDisplay) + '</span>'
      +   '</div>'
      + '</div>';
    if (expanded) {
      rows += '<div class="row-detail" data-noclick="1">'
        + '<dl>'
        +   '<dt>client_id</dt><dd>' + esc(c.client_id) + '</dd>'
        +   '<dt>token</dt><dd>'
        +     esc(c.token_status)
        +     (!locked ? ' &nbsp; <button class="btn" data-act="rotate-token" data-client="' + esc(c.client_id) + '">Set new token</button>' : '')
        +   '</dd>'
        +   '<dt>allow_servers</dt><dd class="mono">' + esc((c.allow_servers || []).join(', ') || '—') + '</dd>'
        +   '<dt>rate_limit_rpm</dt><dd>'
        +     (locked
            ? esc(String(nc(c.rate_limit_rpm, 'unlimited')))
            : '<input class="inline-input" type="text" data-act="edit-rpm" data-client="' + esc(c.client_id) + '" value="' + esc(String(nc(c.rate_limit_rpm, ''))) + '" placeholder="unlimited" style="width:100px;">')
        +   '</dd>'
        + '</dl>';
      if (!locked) {
        rows += '<div class="actions">'
          + '<button class="btn danger" data-act="remove-client" data-client="' + esc(c.client_id) + '">Remove client</button>'
          + '</div>';
      }
      rows += '</div>';
    }
    rows += '</div>';
  });
  const addBtn = !locked
    ? '<button class="btn amber" data-act="add-client" style="margin-left:auto;">+ Add client</button>'
    : '';
  return ''
    + '<div class="section-head">'
    +   '<span class="section-title">Clients · ' + ((s.clients ? s.clients.length : 0)) + '</span>'
    +   addBtn
    + '</div>'
    + '<div class="panel" style="border:1px solid var(--line);margin-top:0;border-top:none;">'
    +   '<div class="panel-body">' + (rows || '<div class="empty-state">no clients registered</div>') + '</div>'
    + '</div>';
}

function renderServersHTML() {
  const s = state.status;
  if (!s) return '<div class="empty-state">loading…</div>';
  const locked = state.mode !== 'management';
  const reg = state.registry;
  let rows = '';
  (s.servers || []).forEach(sv => {
    const expanded = state.expandedServer === sv.server_id;
    const pendingS = state.pending.servers[sv.server_id];
    const dirty = !!pendingS;
    const rsv = (reg && reg.servers ? reg.servers[sv.server_id] : undefined);
    rows += '<div class="list-row ' + (expanded ? 'expanded' : '') + '" data-act="toggle-server" data-server="' + esc(sv.server_id) + '">'
      + '<div class="row-head">'
      +   '<span class="row-id">' + esc(sv.server_id) + '</span>'
      +   '<span class="event-type ' + (sv.available_hint ? 'allowed' : 'denied') + '" style="font-size:9px;">'
      +     (sv.available_hint ? '● reachable' : '○ unreachable')
      +   '</span>'
      +   (dirty ? '<span class="mono" style="font-size:10px;color:var(--amber);letter-spacing:0.12em;">· edited</span>' : '')
      +   '<div class="row-meta">'
      +     '<span>' + esc(sv.transport) + '</span>'
      +     '<span>' + esc(sv.tool_policy_mode) + '</span>'
      +   '</div>'
      + '</div>';
    if (expanded) {
      const curMode = (pendingS && pendingS.tool_policy_mode !== undefined) ? pendingS.tool_policy_mode : sv.tool_policy_mode;
      const t = sv.timeouts || {};
      let targetRow = '';
      if (rsv) {
        if (rsv.transport === 'stdio') {
          const args = (rsv.args || []).map(a => '"' + a + '"').join(' ');
          targetRow = '<dt>command</dt><dd class="mono locked-field">' + esc(rsv.command || '—') + ' ' + esc(args) + '</dd>';
        } else {
          targetRow = '<dt>url</dt><dd class="mono locked-field">' + esc(rsv.url || '—') + '</dd>';
        }
      }
      rows += '<div class="row-detail" data-noclick="1">'
        + '<dl>'
        +   '<dt>server_id</dt><dd>' + esc(sv.server_id) + '</dd>'
        +   '<dt>transport</dt><dd>' + esc(sv.transport) + '</dd>'
        +   targetRow
        +   '<dt>tool_policy.mode</dt><dd>'
        +     (locked
                ? esc(curMode)
                : '<select class="inline-select" data-act="edit-policy" data-server="' + esc(sv.server_id) + '">'
        +         ['allow_all','deny_all','allow_list'].map(m => '<option value="' + m + '"' + (curMode === m ? ' selected' : '') + '>' + m + '</option>').join('')
        +         + '</select>')
        +   '</dd>'
        +   '<dt>timeouts</dt><dd class="mono">init ' + (nc(t.initialize_s, '—')) + 's · list ' + (nc(t.tools_list_s, '—')) + 's · call ' + (nc(t.tools_call_s, '—')) + 's · retry ' + (nc(t.retry_on_timeout, '—')) + '× ' + (nc(t.retry_backoff_s, '—')) + 's</dd>';
      if (rsv && rsv.transport === 'stdio') {
        rows += '<dt>stdio_mode</dt><dd class="locked-field">' + esc(rsv.stdio_mode || 'newline') + '</dd>';
        rows += '<dt>env</dt><dd class="locked-field">' + esc(Object.keys(rsv.env || {}).join(', ') || '—') + '</dd>';
      }
      if (rsv && rsv.transport === 'http') {
        rows += '<dt>headers</dt><dd class="locked-field">' + esc(Object.keys(rsv.headers || {}).join(', ') || '—') + '</dd>';
      }
      rows += '</dl>';
      rows += '<div class="k" style="font-size:9px;margin-top:12px;">fields marked <em style="font-style:italic;">italic</em> are YAML-only (edit registry.yaml to change)</div>';
      if (!locked) {
        rows += '<div class="actions">'
          + '<button class="btn danger" data-act="remove-server" data-server="' + esc(sv.server_id) + '">Remove server (cascades)</button>'
          + '</div>';
      }
      rows += '</div>';
    }
    rows += '</div>';
  });
  return ''
    + '<div class="section-head">'
    +   '<span class="section-title">Servers · ' + ((s.servers ? s.servers.length : 0)) + '</span>'
    +   '<span class="section-id">complex fields are read-only · edit registry.yaml</span>'
    + '</div>'
    + '<div class="panel" style="border:1px solid var(--line);margin-top:0;border-top:none;">'
    +   '<div class="panel-body">' + (rows || '<div class="empty-state">no servers registered</div>') + '</div>'
    + '</div>';
}

function renderSettingsHTML() {
  const s = state.status;
  const r = state.registry;
  if (!s) return '<div class="empty-state">loading…</div>';
  return ''
    + '<div class="section-head">'
    +   '<span class="section-title">Info · read-only reference</span>'
    +   '<span class="section-id">lmcp settings require restart · edit registry.yaml</span>'
    + '</div>'
    + '<div class="panel" style="border:1px solid var(--line);margin-top:0;border-top:none;">'
    +   '<div class="row-detail" style="padding:18px 20px;border-top:none;margin-top:0;">'
    +     '<dl>'
    +       '<dt>service</dt><dd>' + esc(s.service) + '</dd>'
    +       '<dt>status_version</dt><dd>' + esc(String(s.status_version)) + '</dd>'
    +       '<dt>host</dt><dd class="mono">' + esc(s.host) + '</dd>'
    +       '<dt>port</dt><dd class="mono">' + esc(String(s.port)) + '</dd>'
    +       '<dt>loopback_only</dt><dd>' + (s.loopback_only ? '<span style="color:var(--green)">true</span>' : '<span style="color:var(--amber)">false · NETWORK EXPOSED</span>') + '</dd>'
    +       '<dt>uptime</dt><dd class="mono">' + fmtUptime(s.uptime_s) + '</dd>'
    +       ((r && r.lmcp && r.lmcp.audit_log) ? '<dt>audit_log</dt><dd class="mono">' + esc(r.lmcp.audit_log) + '</dd>' : '')
    +       ((r && r.lmcp ? r.lmcp.rate_limit_rpm : undefined) != null ? '<dt>default rate_limit_rpm</dt><dd class="mono">' + esc(String(r.lmcp.rate_limit_rpm)) + '</dd>' : '')
    +       '<dt>management api</dt><dd>' + (state.mode === 'management' ? '<span style="color:var(--amber)">unlocked</span>' : 'locked') + '</dd>'
    +     '</dl>'
    +   '</div>'
    + '</div>';
}

// ─── Events rendering ────────────────────────────────────
function renderEvents() {
  const list = el('events-list');
  if (!state.events.length) {
    list.innerHTML = '<div class="events-empty">waiting for events…</div>';
    return;
  }
  const filt = state.eventFilter;
  const shown = filt ? state.events.filter(e => e.event_type === filt) : state.events;
  if (!shown.length) {
    list.innerHTML = '<div class="events-empty">no events match filter</div>';
    return;
  }
  let html = '';
  shown.slice(0, 100).forEach(e => {
    const p = e.payload || {};
    const allowed = p.allowed;
    const ts = fmtTime(e.timestamp);
    const typeClass = e.event_type === 'config_change' ? 'config_change'
                    : e.event_type === 'rate_limited' ? 'rate_limited'
                    : e.event_type === 'server_error' ? 'server_error'
                    : (allowed === true ? 'allowed' : allowed === false ? 'denied' : '');
    const own = e.event_type === 'config_change';
    let detail = '';
    if (p.client_id || p.server_id || p.tool_name) {
      const parts = [];
      if (p.client_id) parts.push('<span class="eid">' + esc(p.client_id) + '</span>');
      if (p.server_id) parts.push('→ <span class="eid">' + esc(p.server_id) + '</span>');
      if (p.tool_name) parts.push('· ' + esc(p.tool_name));
      detail = parts.join(' ');
    } else {
      detail = '<span class="muted">(no subject)</span>';
    }
    if (p.reason) detail += '<br><span class="reason">' + esc(p.reason) + '</span>';
    html += '<div class="event-row' + (own ? ' own' : '') + '">'
      + '<div class="event-ts">' + esc(ts) + '</div>'
      + '<div>'
      +   '<span class="event-type ' + typeClass + '">' + esc(e.event_type) + '</span>'
      +   '<div class="event-detail">' + detail + '</div>'
      + '</div>'
      + '</div>';
  });
  list.innerHTML = html;
}

function fmtTime(iso) {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour12: false });
  } catch(e) { return iso; }
}
function fmtUptime(s) {
  if (s == null) return '—';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h + 'h ' + m + 'm';
}

// ─── Pending bar ─────────────────────────────────────────
function renderPendingBar() {
  const bar = el('pending-bar');
  if (!hasPending() && !state.applyError && !state.applyWarning) {
    bar.classList.remove('visible', 'error', 'warning');
    bar.innerHTML = '';
    return;
  }
  bar.classList.add('visible');
  bar.classList.remove('error','warning');
  if (state.applyError) bar.classList.add('error');
  else if (state.applyWarning) bar.classList.add('warning');

  const s = buildChangesSummary();
  const parts = [];
  const push = (n, label, cls) => { if (n) parts.push('<span class="' + (cls||'amber') + '">' + n + '</span> <span class="tag">' + label + '</span>'); };
  push(s.clients_added, 'clients added');
  push(s.clients_modified, 'clients modified');
  push(s.clients_removed, 'clients removed', 'red');
  push(s.servers_added, 'servers added');
  push(s.servers_modified, 'servers modified');
  push(s.servers_removed, 'servers removed', 'red');

  const label = state.applyError ? 'Apply failed'
              : state.applyWarning ? 'Applied · warning'
              : 'Pending changes';
  const labelCls = state.applyError ? 'error' : '';

  let html = ''
    + '<div class="pending-label ' + labelCls + '">' + esc(label) + '</div>'
    + '<div class="pending-summary">' + (parts.join(' · ') || '<span class="muted">no diff</span>') + '</div>'
    + '<div class="pending-actions">';

  if (!state.applyError) {
    html += '<button class="btn" data-act="validate">Validate</button>';
    html += '<button class="btn amber-solid" data-act="apply">Apply…</button>';
  } else {
    html += '<button class="btn amber" data-act="apply">Retry apply</button>';
  }
  html += '<button class="btn" data-act="discard">Discard</button>';
  html += '</div>';

  if (state.applyError) {
    html += '<div class="pending-errors"><strong>' + esc(state.applyError.headline) + '</strong>';
    if (state.applyError.errors && state.applyError.errors.length) {
      html += '<ul>' + state.applyError.errors.map(e => '<li>' + esc(e) + '</li>').join('') + '</ul>';
    }
    html += '</div>';
  } else if (state.applyWarning) {
    html += '<div class="pending-errors" style="color:var(--amber);background:rgba(227,164,74,0.06);border-color:var(--amber-dim);"><strong>' + esc(state.applyWarning) + '</strong></div>';
  }

  bar.innerHTML = html;
}

// ─── Modal: apply confirmation ───────────────────────────
function openApplyModal() {
  const diff = buildHumanDiff();
  const patch = buildPatch();
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = ''
    + '<div class="modal">'
    +   '<div class="modal-head">'
    +     '<span class="modal-title">Apply changes to registry.yaml</span>'
    +     '<span class="modal-close" data-act="modal-close">✕</span>'
    +   '</div>'
    +   '<div class="modal-body">'
    +     '<div class="k" style="margin-bottom:8px;">Will write ' + diff.length + ' change' + (diff.length===1?'':'s') + ' · creates .bak first</div>'
    +     '<div class="diff-list">'
    +       (diff.length
              ? diff.map(d => '<div class="diff-item ' + d.kind + '"><span class="glyph">' + d.glyph + '</span><span>' + esc(d.text) + '</span></div>').join('')
              : '<div class="dim" style="padding:8px 0;">no changes</div>')
    +     '</div>'
    +     '<details><summary>Show raw patch JSON</summary>'
    +     '<pre class="raw-patch">' + esc(JSON.stringify(patch, null, 2)) + '</pre></details>'
    +   '</div>'
    +   '<div class="modal-foot">'
    +     '<button class="btn" data-act="modal-close">Cancel</button>'
    +     '<button class="btn amber-solid" data-act="modal-apply">Confirm apply</button>'
    +   '</div>'
    + '</div>';
  document.body.appendChild(backdrop);
  backdrop.addEventListener('click', (e) => {
    const act = (e.target.closest('[data-act]') ? e.target.closest('[data-act]').dataset.act : undefined);
    if (!act) { if (e.target === backdrop) backdrop.remove(); return; }
    if (act === 'modal-close') backdrop.remove();
    if (act === 'modal-apply') { backdrop.remove(); doApply(); }
  });
}

function openUnlockModal() {
  const backdrop = document.createElement('div');
  backdrop.className = 'modal-backdrop';
  backdrop.innerHTML = ''
    + '<div class="modal" style="min-width:400px;">'
    +   '<div class="modal-head">'
    +     '<span class="modal-title">Unlock management</span>'
    +     '<span class="modal-close" data-act="modal-close">✕</span>'
    +   '</div>'
    +   '<div class="modal-body">'
    +     '<div class="k" style="margin-bottom:10px;">Management token</div>'
    +     '<input type="password" id="mgmt-token-input" class="inline-input" style="width:100%;padding:8px;font-size:13px;" placeholder="X-Lmcp-Management-Token">'
    +     '<div class="k" style="margin-top:10px;color:var(--fg-3);letter-spacing:0.1em;">Stored in sessionStorage · cleared when tab closes</div>'
    +   '</div>'
    +   '<div class="modal-foot">'
    +     '<button class="btn" data-act="modal-close">Cancel</button>'
    +     '<button class="btn amber-solid" data-act="modal-unlock">Unlock</button>'
    +   '</div>'
    + '</div>';
  document.body.appendChild(backdrop);
  setTimeout(() => backdrop.querySelector('#mgmt-token-input').focus(), 50);
  backdrop.addEventListener('click', (e) => {
    const act = (e.target.closest('[data-act]') ? e.target.closest('[data-act]').dataset.act : undefined);
    if (!act) { if (e.target === backdrop) backdrop.remove(); return; }
    if (act === 'modal-close') backdrop.remove();
    if (act === 'modal-unlock') {
      const t = backdrop.querySelector('#mgmt-token-input').value.trim();
      if (!t) return;
      sessionStorage.setItem('lmcp_mgmt_token', t);
      state.mgmtToken = t;
      backdrop.remove();
      unlockMgmt();
    }
  });
  backdrop.querySelector('#mgmt-token-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') backdrop.querySelector('[data-act=modal-unlock]').click();
  });
}

async function unlockMgmt() {
  try {
    const reg = await apiGet('/registry/view', true);
    state.registry = reg;
    state.mode = 'management';
    toast('Management unlocked', 'ok');
    render();
  } catch (e) {
    toast('Auth failed · check token', 'err');
  }
}

function lockMgmt() {
  sessionStorage.removeItem('lmcp_mgmt_token');
  state.mgmtToken = null;
  state.mode = 'readonly';
  state.registry = null;
  state.pending = { clients: {}, servers: {} };
  state.applyError = null;
  state.applyWarning = null;
  toast('Locked', 'ok');
  render();
}

// ─── Validate/Apply flow ─────────────────────────────────
async function doValidate() {
  const patch = buildPatch();
  const res = await apiPost('/registry/validate', { patch });
  if (res._httpError) {
    state.applyError = { headline: 'Validation failed (HTTP ' + res.status + ')', errors: (res.body && res.body.errors) || [(res.body && res.body.error) || 'Unknown error'] };
  } else if (!res.valid) {
    state.applyError = { headline: 'Validation errors', errors: res.errors || [] };
  } else {
    state.applyError = null;
    toast('Valid · ' + summaryOneLine(res.changes_summary), 'ok');
  }
  render();
}

async function doApply() {
  const patch = buildPatch();
  const res = await apiPost('/registry/apply', { patch });
  if (res._httpError) {
    state.applyError = {
      headline: 'Apply failed · HTTP ' + res.status + ((res.body && res.body.error) ? ' · ' + res.body.error : ''),
      errors: (res.body && res.body.errors) || [],
    };
    render();
    return;
  }
  if (!res.applied) {
    state.applyError = { headline: 'Apply rejected', errors: res.errors || [] };
    render();
    return;
  }
  // Success or partial success
  state.applyError = null;
  if (res.reload_failed) {
    state.applyWarning = 'Written to disk, but in-memory reload failed. Restart daemon required. ' + (res.reload_error || '');
    toast('Applied · restart required', 'warn');
    // keep pending since runtime didn't pick up
  } else {
    state.pending = { clients: {}, servers: {} };
    state.applyWarning = res.warning || (res.restart_required ? 'Applied · non-reloadable settings changed · restart required' : null);
    toast('Applied · backup ' + (res.backup_path || ''), 'ok');
    // refetch
    await refetchAll();
  }
  render();
}

function summaryOneLine(s) {
  if (!s) return '';
  const parts = [];
  if (s.clients_added) parts.push(s.clients_added + ' +client');
  if (s.clients_modified) parts.push(s.clients_modified + ' ~client');
  if (s.clients_removed) parts.push(s.clients_removed + ' -client');
  if (s.servers_added) parts.push(s.servers_added + ' +server');
  if (s.servers_modified) parts.push(s.servers_modified + ' ~server');
  if (s.servers_removed) parts.push(s.servers_removed + ' -server');
  return parts.join(' · ') || 'no changes';
}

async function refetchAll() {
  const st = await apiGet('/status');
  state.status = st;
  if (state.mode === 'management') {
    try {
      const r = await apiGet('/registry/view', true);
      state.registry = r;
    } catch(e) {}
  }
}

// ─── Toast ──────────────────────────────────────────────
let toastTimer;
function toast(msg, kind) {
  const t = el('toast');
  t.textContent = msg;
  t.className = 'visible ' + (kind || 'ok');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('visible'), 3200);
}

// ─── Event binding ──────────────────────────────────────
function bindMainEvents() {
  qa('[data-act]', el('main-content')).forEach(n => {
    n.addEventListener('click', onMainClick);
    if (n.tagName === 'INPUT' || n.tagName === 'SELECT') {
      n.addEventListener('change', onMainChange);
      if (n.tagName === 'INPUT') n.addEventListener('blur', onMainChange);
    }
  });
}

function onMainClick(e) {
  // Prevent list-row toggle if clicking inside detail area
  const detail = e.target.closest('[data-noclick]');
  const inner = e.target.closest('[data-act]');
  if (!inner) return;
  if (detail && inner.dataset.act !== 'rotate-token' && inner.dataset.act !== 'remove-client' && inner.dataset.act !== 'remove-server') {
    // clicks on inputs/selects inside detail shouldn't bubble to row toggle — already handled by not finding toggle
  }
  const act = inner.dataset.act;
  if (act === 'toggle-cell') {
    if (inner.classList.contains('locked')) return;
    toggleMatrixCell(inner.dataset.client, inner.dataset.server);
    e.stopPropagation();
    return;
  }
  if (act === 'toggle-client') {
    // Only if click didn't land on a control inside detail
    if (e.target.closest('[data-noclick]') && e.target.closest('[data-act]') && e.target.closest('[data-act]').dataset.act !== 'toggle-client') return;
    state.expandedClient = state.expandedClient === inner.dataset.client ? null : inner.dataset.client;
    render();
    return;
  }
  if (act === 'toggle-server') {
    if (e.target.closest('[data-noclick]') && e.target.closest('[data-act]') && e.target.closest('[data-act]').dataset.act !== 'toggle-server') return;
    state.expandedServer = state.expandedServer === inner.dataset.server ? null : inner.dataset.server;
    render();
    return;
  }
  if (act === 'remove-client') {
    e.stopPropagation();
    const cid = inner.dataset.client;
    if (!confirm('Stage removal of client ' + cid + '?')) return;
    state.pending.clients[cid] = { _action: 'remove' };
    render();
    return;
  }
  if (act === 'remove-server') {
    e.stopPropagation();
    const sid = inner.dataset.server;
    if (!confirm('Stage removal of server ' + sid + '? This cascades to every client allowlist.')) return;
    state.pending.servers[sid] = { _action: 'remove' };
    render();
    return;
  }
  if (act === 'add-client') {
    e.stopPropagation();
    const cid = prompt('New client_id:');
    if (!cid) return;
    if (!/^[a-z0-9_-]+$/i.test(cid)) { toast('Invalid client_id', 'err'); return; }
    if (state.status.clients.find(c => c.client_id === cid)) { toast('Already exists', 'err'); return; }
    state.pending.clients[cid] = { _action: 'add', allow_servers: [], rate_limit_rpm: null };
    // also reflect in status so it appears in matrix immediately
    state.status.clients.push({ client_id: cid, token_status: 'empty', allow_servers: [], rate_limit_rpm: null });
    render();
    return;
  }
  if (act === 'rotate-token') {
    e.stopPropagation();
    const cid = inner.dataset.client;
    const tok = prompt('New token for ' + cid + ' (stored in pending patch · not sent until Apply):');
    if (!tok) return;
    if (!state.pending.clients[cid]) state.pending.clients[cid] = {};
    state.pending.clients[cid].token = tok;
    state.pending.clients[cid].token_status = 'set';
    render();
    return;
  }
}

function onMainChange(e) {
  const inner = e.target.closest('[data-act]');
  if (!inner) return;
  const act = inner.dataset.act;
  if (act === 'edit-rpm') {
    const cid = inner.dataset.client;
    const v = inner.value.trim();
    const n = v === '' ? null : Number(v);
    if (v !== '' && (!Number.isFinite(n) || n < 0)) { toast('rpm must be a non-negative number or empty', 'err'); return; }
    const base = nc(state.registry && state.registry.clients && state.registry.clients[cid] && state.registry.clients[cid].rate_limit_rpm, null);
    if (!state.pending.clients[cid]) state.pending.clients[cid] = {};
    if (n === base) {
      delete state.pending.clients[cid].rate_limit_rpm;
      if (Object.keys(state.pending.clients[cid]).length === 0) delete state.pending.clients[cid];
    } else {
      state.pending.clients[cid].rate_limit_rpm = n;
    }
    render();
    return;
  }
  if (act === 'edit-policy') {
    const sid = inner.dataset.server;
    const v = inner.value;
    const base = (state.registry && state.registry.servers && state.registry.servers[sid] && state.registry.servers[sid].tool_policy && state.registry.servers[sid].tool_policy.mode);
    if (!state.pending.servers[sid]) state.pending.servers[sid] = {};
    if (v === base) {
      delete state.pending.servers[sid].tool_policy_mode;
      if (Object.keys(state.pending.servers[sid]).length === 0) delete state.pending.servers[sid];
    } else {
      state.pending.servers[sid].tool_policy_mode = v;
    }
    render();
    return;
  }
}

// Delegated listener on pending bar
el('pending-bar').addEventListener('click', (e) => {
  const act = (e.target.closest('[data-act]') ? e.target.closest('[data-act]').dataset.act : undefined);
  if (!act) return;
  if (act === 'validate') doValidate();
  else if (act === 'apply') openApplyModal();
  else if (act === 'discard') discardPending();
});

// Nav
qa('.nav-item').forEach(n => {
  n.addEventListener('click', () => {
    state.activeNav = n.dataset.nav;
    render();
  });
});

// Unlock button
el('btn-unlock').addEventListener('click', () => {
  if (state.mode === 'management') lockMgmt();
  else openUnlockModal();
});

// Events controls
el('event-filter').addEventListener('change', (e) => {
  state.eventFilter = e.target.value;
  renderEvents();
});
el('btn-clear-events').addEventListener('click', () => {
  state.events = [];
  renderEvents();
});

// ─── Init ───────────────────────────────────────────────
async function init() {
  try {
    const st = await apiGet('/status');
    state.status = st;
  } catch (e) {
    state.status = DEMO.status;
    toast('Offline · showing demo data', 'warn');
  }
  if (state.mgmtToken) {
    try {
      const reg = await apiGet('/registry/view', true);
      state.registry = reg;
      state.mode = 'management';
    } catch (e) {
      // token invalid
      sessionStorage.removeItem('lmcp_mgmt_token');
      state.mgmtToken = null;
    }
  }
  render();
  renderEvents();
  connectSSE();
  // Poll /status every 5s for uptime + client/server drift
  setInterval(async () => {
    try { state.status = await apiGet('/status'); renderTopBar(); } catch(e) {}
  }, 5000);
}

init();
})();
