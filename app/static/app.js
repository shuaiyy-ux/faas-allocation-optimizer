/* ── FaaS Optimizer — Frontend Logic ───────────────────────────── */

const C = {
  v2: '#00d4ff', greedy: '#ff6b6b', pos: '#00e676', neg: '#ff5252',
  muted: '#6b7b8d', bg: '#06080c', surface: '#0d1117',
  border: 'rgba(255,255,255,0.06)', card: 'rgba(255,255,255,0.03)',
};
const PLOT_LAYOUT = {
  paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
  font: { family: 'Inter, system-ui, sans-serif', size: 11, color: '#e0e4ea' },
  margin: { l: 48, r: 16, t: 8, b: 36 },
  xaxis: { gridcolor: 'rgba(255,255,255,0.06)', zerolinecolor: 'rgba(255,255,255,0.06)' },
  yaxis: { gridcolor: 'rgba(255,255,255,0.06)', zerolinecolor: 'rgba(255,255,255,0.06)' },
};
const PLOT_CONFIG = { displayModeBar: false, responsive: true };

let overview = null;
let weeklyData = null;
let fleetData = null;
let currentPage = 'home';
let homeData = null;
let homeLoaded = false;
let deckInstance = null;
let overviewLoaded = false;
let weeklyLoaded = false;
let allocationConfirmed = false;
let selectedVINs = new Set();
let fleetFilter = 'all';
let weekFilter = null;
let fleetSortCol = 'status';
let fleetSortAsc = true;
let agentFocusTargets = []; // tracks currently focused elements
let weeklyShowFilter = 'assigned'; // 'assigned' or 'all'
let weeklySourceFilter = '';
let weeklyStateFilter = '';
let weeklyWeekFilter = '';
let weeklySelectedVINs = new Set();
let weeklyCollapsedGroups = new Set();
let weeklyGreedyData = null;
// Per-algorithm caches. Allocate Selected fires all three in parallel so
// the Algorithm toggle can switch instantly without re-running the ILP.
// weeklyData is a pointer into one of these three caches based on the
// currently-selected (weeklyMethod, scoringMode) pair.
let weeklyAdditiveData = null;
let weeklyBucketData = null;
let weeklyMethod = 'ilp';
// Remembers the VIN list of the last successful allocate so we can replay
// with the same vehicle set if needed (rare; mainly a debug aid now).
let lastAllocatedVINs = null;
// Util-side scoring strategy. 'additive' = w_util×UTIL + w_rented×RENTED
// (production default, doc/allocation_scoring_explained.md). 'bucket' =
// bucket_mult(tier_of(IN_SERVICE)) per 2026-05-21 teammate spec, see
// docs/bucket_algorithm.md.
let scoringMode = 'additive';
let allocCompareData = null; // {greedy: weeklyGreedyData, ilp: weeklyData} for overview comparison

// Scoring defaults fetched from the server at boot so the frontend never
// hardcodes w_util / w_rented independently of the engine's DEFAULT_*.
// See /api/defaults in server.py. Initial fallback values are only used if
// the fetch fails; the real values overwrite these on init().
let scoringDefaults = {
  w_util: 1.335, w_rented: 0.0574, w_dist: 15.0, w_tax: 1.950, expected_stay_months: 12,
};

// Public demo mode — set from /api/defaults at boot. When true the page shows
// the anonymized-data annotation banner + chart watermarks, and the chat quota
// is owned by the gateway (not this app).
var DEMO_MODE = false;

// Apply the demo annotation banner + chart/map watermarks. Idempotent.
function applyDemoMode() {
  if (!DEMO_MODE) return;
  document.body.classList.add('demo-mode');
  if (!document.getElementById('demo-banner')) {
    var bar = document.createElement('div');
    bar.id = 'demo-banner';
    bar.className = 'demo-banner';
    bar.textContent = 'Anonymized sample data. Values perturbed; not actual HCA figures.';
    document.body.insertBefore(bar, document.body.firstChild);
  }
  var src = document.getElementById('watchlist-source');
  if (src) src.textContent = 'Anonymized sample data';
}

// ── Session ──────────────────────────────────────────
// One stable UUID per browser tab. Sent on EVERY API call via the
// X-Session-Id header so the backend keys per-session state (fleet
// snapshot, allocation cache, constraint plugins). Also reused as
// the Claude CLI session key for multi-turn chat fidelity.
// F5 generates a fresh value — no localStorage — so reload = clean state.
function _newClientId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
    var r = Math.random() * 16 | 0, v = c === 'x' ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });
}
var sessionId = _newClientId();

// ── API ──────────────────────────────────────────────

// One fact per element; spacing comes from .meta-row, not separator characters.
function metaRow(parts) {
  return '<span class="meta-row">' + parts.filter(function(x) { return x !== '' && x != null; })
    .map(function(x) { return '<span>' + x + '</span>'; }).join('') + '</span>';
}

async function api(url, body) {
  var headers = { 'X-Session-Id': sessionId };
  var opts;
  if (body) {
    headers['Content-Type'] = 'application/json';
    opts = { method: 'POST', headers: headers, body: JSON.stringify(body) };
  } else {
    opts = { headers: headers };
  }
  var res = await fetch(url, opts);
  if (!res.ok) throw new Error('API ' + res.status + ': ' + res.statusText);
  return res.json();
}

// ── Init ─────────────────────────────────────────────

async function init() {
  try {
    // Fetch scoring defaults first so every subsequent /api/weekly* body
    // reflects the engine's authoritative DEFAULT_* values.
    try {
      scoringDefaults = await api('/api/defaults');
      DEMO_MODE = !!scoringDefaults.demo_mode;
      applyDemoMode();
    } catch (e) {
      console.warn('Could not fetch /api/defaults, using fallback:', e);
    }
    try {
      await refreshChatStatus();
    } catch (e) {
      console.warn('Could not fetch /api/chat_status, using fallback:', e);
    }
    // Reflect the scoring-mode default on the toggle as soon as the DOM
    // is interactive, so the user sees the active state immediately.
    updateAlgorithmToggle();
    updateAlgoStagingHint();
    // Mobile chat-drawer key handlers + nav-tab label shortening.
    installMobileChrome();
    // Auto-reset on every page load so each session starts clean
    await api('/api/reset', {});
    overview = await api('/api/overview');
    await loadFleet();
    if (DEMO_MODE) await runDemoBatch();
    // Home is the default tab — load its data on boot (async, non-blocking)
    loadHome().catch(function(e) { console.warn('Home load failed:', e); });
  } catch (e) {
    console.error('Init failed:', e);
    document.getElementById('fleet-table-wrap').innerHTML =
      '<div class="loading" style="color:var(--negative)">Failed to load: ' + e.message + '</div>';
  }
}

// ── Page Navigation ──────────────────────────────────

function switchPage(page) {
  currentPage = page;
  document.querySelectorAll('.nav-tab').forEach(function(t) { t.classList.remove('active'); });
  document.querySelectorAll('.page').forEach(function(p) { p.classList.remove('active'); });

  var tabs = document.querySelectorAll('.nav-tab');
  if (page === 'home') tabs[0].classList.add('active');
  else if (page === 'fleet') tabs[1].classList.add('active');
  else if (page === 'weekly') tabs[2].classList.add('active');
  else tabs[3].classList.add('active');

  var el = document.getElementById('page-' + page);
  if (el) el.classList.add('active');

  // Home tab is a fleet-wide brief, not chat-driven — hide the chat panel on
  // desktop. On mobile (≤600px) the chat is a slide-up drawer addressable
  // from the FAB at all times, so we keep it `display: flex` and let the
  // CSS transform keep it off-screen until the user taps the FAB.
  var chatPanel = document.getElementById('chat-panel');
  if (chatPanel) {
    var isMobile = window.matchMedia('(max-width: 600px)').matches;
    if (isMobile) {
      chatPanel.style.display = 'flex';
    } else {
      if (page === 'home') toggleChatFloat(false);
      chatPanel.style.display = (page === 'home') ? 'none' : 'flex';
      // Close any leftover open-drawer state when transitioning to desktop view.
      chatPanel.classList.remove('open');
      var bd = document.getElementById('chat-fab-backdrop');
      if (bd) bd.classList.remove('open');
    }
  }

  if (page === 'home') {
    // Always re-render home with latest weeklyData on visit; refetch /api/home if not cached
    if (!homeLoaded) {
      homeLoaded = true;
      loadHome().catch(function() { homeLoaded = false; });
    } else {
      renderHome();
    }
  }
  if (page === 'overview' && !overviewLoaded) {
    overviewLoaded = true;
    loadOverviewDashboard().catch(function() { overviewLoaded = false; });
  }
  if (page === 'weekly' && !weeklyLoaded) {
    // Show empty state — user must allocate from Fleet Inventory first
  }
}

// ── Fleet Inventory ──────────────────────────────────

async function loadFleet() {
  try {
    fleetData = await api('/api/fleet');
    renderFleet();
  } catch (e) {
    console.error('Fleet load failed:', e);
    document.getElementById('fleet-table-wrap').innerHTML =
      '<div class="loading" style="color:var(--negative)">Error: ' + e.message + '</div>';
  }
}

function renderFleet() {
  var d = fleetData;
  if (!d) return;

  var c = d.counts;
  document.getElementById('fleet-subtitle').textContent =
    c.total + ' vehicles in fleet';

  // KPIs
  document.getElementById('fleet-kpis').innerHTML =
    '<div class="fleet-kpi">' +
      '<div class="fleet-kpi-label">Total</div>' +
      '<div class="fleet-kpi-value">' + c.total + '</div>' +
    '</div>' +
    '<div class="fleet-kpi">' +
      '<div class="fleet-kpi-label">Incoming</div>' +
      '<div class="fleet-kpi-value" style="color:#ff6b6b">' + (c.incoming || 0) + '</div>' +
    '</div>' +
    '<div class="fleet-kpi">' +
      '<div class="fleet-kpi-label">Grounded</div>' +
      '<div class="fleet-kpi-value warn">' + (c.grounded || 0) + '</div>' +
    '</div>' +
    '<div class="fleet-kpi">' +
      '<div class="fleet-kpi-label">Transporting</div>' +
      '<div class="fleet-kpi-value accent">' + (c.transporting || 0) + '</div>' +
    '</div>' +
    '<div class="fleet-kpi">' +
      '<div class="fleet-kpi-label">Delivered</div>' +
      '<div class="fleet-kpi-value positive">' + (c.delivered || 0) + '</div>' +
    '</div>';

  // Update week picker label
  updateWeekPickerLabel();
  buildWeekCalendar();

  renderFleetTable();
}

var MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function formatWeekLabel(w) {
  // w is like "2026-03-17" (always a Monday — bucketed from the real
  // GROUNDING_DATE). Display as ISO week: "2026 W12". The ISO week year
  // can differ from the calendar year at year boundaries (e.g. 2026-01-01
  // is still 2025-W53), so we derive the year from the week's Thursday.
  var d = new Date(w + 'T00:00:00Z');
  var target = new Date(d.valueOf());
  // Shift to the Thursday in the current ISO week
  target.setUTCDate(target.getUTCDate() + 4 - (target.getUTCDay() || 7));
  var year = target.getUTCFullYear();
  var yearStart = new Date(Date.UTC(year, 0, 1));
  var weekNo = Math.ceil(((target - yearStart) / 86400000 + 1) / 7);
  return year + ' W' + (weekNo < 10 ? '0' + weekNo : weekNo);
}

function updateWeekPickerLabel() {
  var label = document.getElementById('week-picker-label');
  if (!label) return;
  if (!weekFilter) {
    label.textContent = 'All Weeks';
  } else {
    label.textContent = formatWeekLabel(weekFilter);
  }
}

function buildWeekCalendar() {
  var cal = document.getElementById('week-calendar');
  if (!cal || !fleetData) return;
  var weeks = fleetData.weeks || [];
  if (!weeks.length) { cal.innerHTML = ''; return; }

  // Count vehicles per week
  var counts = {};
  for (var i = 0; i < fleetData.vehicles.length; i++) {
    var w = fleetData.vehicles[i].week;
    counts[w] = (counts[w] || 0) + 1;
  }

  // Group weeks by year-month
  var groups = {};
  for (var i = 0; i < weeks.length; i++) {
    var parts = weeks[i].split('-');
    var ym = parts[0] + '-' + parts[1];
    if (!groups[ym]) groups[ym] = [];
    groups[ym].push(weeks[i]);
  }

  var html = '<div class="wc-option wc-all' + (!weekFilter ? ' active' : '') + '" onclick="filterWeek(null)">All Weeks<span class="wc-count">' + fleetData.vehicles.length + '</span></div>';

  // Newest month first (reverse chronological), and within each month the
  // newest week first. Users scan for "recent weeks" far more than "oldest".
  var yms = Object.keys(groups).sort().reverse();
  for (var g = 0; g < yms.length; g++) {
    var ym = yms[g];
    var ymParts = ym.split('-');
    var monthLabel = MONTH_NAMES[parseInt(ymParts[1]) - 1] + ' ' + ymParts[0];
    html += '<div class="wc-month">' + monthLabel + '</div>';
    var wks = groups[ym].slice().reverse();
    for (var j = 0; j < wks.length; j++) {
      var wk = wks[j];
      var isActive = weekFilter === wk;
      html += '<div class="wc-option' + (isActive ? ' active' : '') + '" onclick="filterWeek(\'' + wk + '\')">' +
        formatWeekLabel(wk) +
        '<span class="wc-count">' + (counts[wk] || 0) + '</span>' +
      '</div>';
    }
  }
  cal.innerHTML = html;
}

function toggleWeekPicker() {
  var cal = document.getElementById('week-calendar');
  if (cal) cal.classList.toggle('open');
}

function filterWeek(week) {
  weekFilter = week;
  updateWeekPickerLabel();
  buildWeekCalendar();
  // Close popover
  var cal = document.getElementById('week-calendar');
  if (cal) cal.classList.remove('open');
  renderFleetTable();
}

function selectWeekVehicles(week) {
  if (!fleetData) return;
  for (var i = 0; i < fleetData.vehicles.length; i++) {
    var v = fleetData.vehicles[i];
    if (v.week === week) {
      selectedVINs.add(v.vin);
    }
  }
  updateAllocateButton();
  renderFleetTable();
}

function sortFleet(col) {
  if (fleetSortCol === col) {
    fleetSortAsc = !fleetSortAsc;
  } else {
    fleetSortCol = col;
    fleetSortAsc = true;
  }
  renderFleetTable();
}

var STATUS_PRIORITY = { 'Incoming': 0, 'Grounded': 1, 'Transporting': 2, 'Delivered': 3 };

function getFleetSortValue(v, col) {
  switch (col) {
    case 'vin': return v.vin || '';
    case 'status': return STATUS_PRIORITY[v.status] != null ? STATUS_PRIORITY[v.status] : 9;
    case 'week': return v.week || '';
    case 'model': return v.model || '';
    case 'location': return (v.source_city || '') + (v.source_state || '');
    case 'residual': return v.residual || 0;
    case 'assigned': return v.assigned_dealer_name || '';
    default: return '';
  }
}

function renderFleetTable() {
  var d = fleetData;
  if (!d) return;

  var vehicles = d.vehicles.slice();
  // Apply status filter
  if (fleetFilter !== 'all') {
    vehicles = vehicles.filter(function(v) { return v.status === fleetFilter; });
  }
  // Apply week filter
  if (weekFilter) {
    vehicles = vehicles.filter(function(v) { return v.week === weekFilter; });
  }
  // Apply sort
  if (fleetSortCol) {
    var dir = fleetSortAsc ? 1 : -1;
    vehicles.sort(function(a, b) {
      var va = getFleetSortValue(a, fleetSortCol);
      var vb = getFleetSortValue(b, fleetSortCol);
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
      va = String(va).toLowerCase();
      vb = String(vb).toLowerCase();
      if (va < vb) return -1 * dir;
      if (va > vb) return 1 * dir;
      return 0;
    });
  }

  var rows = '';
  for (var i = 0; i < vehicles.length; i++) {
    var v = vehicles[i];
    var isSelected = selectedVINs.has(v.vin);
    var statusClass = v.status.toLowerCase();
    var wLabel = formatWeekLabel(v.week);

    rows += '<tr class="' + (isSelected ? 'selected' : '') + '">' +
      '<td class="check-col">' +
        '<input type="checkbox" ' + (isSelected ? 'checked' : '') +
         ' onchange="toggleVIN(\'' + v.vin + '\', this.checked)">' +
      '</td>' +
      '<td style="font-family:monospace;font-size:0.72rem;font-weight:500">' + escHtml(v.vin) + '</td>' +
      '<td><span class="status-badge ' + statusClass + '">' + escHtml(v.status) + '</span></td>' +
      '<td>' + wLabel + '</td>' +
      '<td>' + escHtml(v.model || '') + '</td>' +
      '<td>' + escHtml(v.source_city) + ', ' + escHtml(v.source_state) + '</td>' +
      '<td>$' + fmt(v.residual) + '</td>' +
      '<td>' + (v.assigned_dealer ? escHtml(v.assigned_dealer_name) + ' (' + escHtml(v.assigned_state) + ')' : '<span style="color:var(--muted)">—</span>') + '</td>' +
    '</tr>';
  }

  // Select-all checkbox state: checked only if ALL visible vehicles are selected
  var selectAllChecked = vehicles.length > 0 && vehicles.every(function(v) { return selectedVINs.has(v.vin); });

  var cols = [
    { key: 'vin', label: 'VIN' },
    { key: 'status', label: 'Status' },
    { key: 'week', label: 'Week' },
    { key: 'model', label: 'Model' },
    { key: 'location', label: 'Location' },
    { key: 'residual', label: 'Residual' },
    { key: 'assigned', label: 'Assigned to' },
  ];
  var thHtml = '<th class="check-col"><input type="checkbox" ' + (selectAllChecked ? 'checked' : '') + ' onchange="toggleAllVINs(this.checked)"></th>';
  for (var c = 0; c < cols.length; c++) {
    var col = cols[c];
    var arrow = '';
    if (fleetSortCol === col.key) arrow = fleetSortAsc ? ' ▲' : ' ▼';
    thHtml += '<th class="sortable" onclick="sortFleet(\'' + col.key + '\')">' + col.label + arrow + '</th>';
  }

  document.getElementById('fleet-table-wrap').innerHTML =
    '<table class="fleet-table"><thead><tr>' + thHtml + '</tr></thead><tbody>' + rows + '</tbody></table>';
}

function toggleVIN(vin, checked) {
  if (checked) selectedVINs.add(vin);
  else selectedVINs.delete(vin);
  updateAllocateButton();
  // Update row highlight without full re-render
  renderFleetTable();
}

function toggleAllVINs(checked) {
  var vehicles = fleetData ? fleetData.vehicles.slice() : [];
  if (fleetFilter !== 'all') {
    vehicles = vehicles.filter(function(v) { return v.status === fleetFilter; });
  }
  if (weekFilter) {
    vehicles = vehicles.filter(function(v) { return v.week === weekFilter; });
  }
  for (var i = 0; i < vehicles.length; i++) {
    if (checked) selectedVINs.add(vehicles[i].vin);
    else selectedVINs.delete(vehicles[i].vin);
  }
  updateAllocateButton();
  renderFleetTable();
}

function filterFleet(status, evt) {
  fleetFilter = status;
  document.querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
  if (evt && evt.target) evt.target.classList.add('active');
  // Clear week filter
  weekFilter = null;
  updateWeekPickerLabel();
  buildWeekCalendar();
  renderFleetTable();
}

function updateAllocateButton() {
  var n = selectedVINs.size;
  document.getElementById('selected-count').textContent = n;
  document.getElementById('btn-allocate').disabled = n === 0;
}

// ── Allocate Selected → Animation → Results ──────────

async function allocateSelected(opts) {
  // `opts.overrideVins` lets the Algorithm toggle replay the prior batch
  // under a new scoring mode without touching the user's pending Fleet
  // selection. When provided, the non-idle warning prompt is skipped
  // (the user already accepted it on the original run).
  opts = opts || {};
  var vinList;
  if (opts.overrideVins && opts.overrideVins.length) {
    vinList = opts.overrideVins.slice();
  } else {
    if (selectedVINs.size === 0) return;
    vinList = Array.from(selectedVINs);
  }
  var vinSet = new Set(vinList);

  // Gather context about selected vehicles for bubble labels
  var selVehicles = [];
  var nonIdleVINs = [];
  if (fleetData) {
    for (var i = 0; i < fleetData.vehicles.length; i++) {
      var fv = fleetData.vehicles[i];
      if (vinSet.has(fv.vin)) {
        selVehicles.push(fv);
        if (fv.status === 'Transporting' || fv.status === 'Delivered') {
          nonIdleVINs.push(fv);
        }
      }
    }
  }

  // Warn if selection includes vehicles already in transit or delivered
  // (skipped on rerun — already accepted on the original run).
  if (nonIdleVINs.length > 0 && !opts.overrideVins) {
    var counts = {};
    for (var ni = 0; ni < nonIdleVINs.length; ni++) {
      var s = nonIdleVINs[ni].status;
      counts[s] = (counts[s] || 0) + 1;
    }
    var parts = [];
    if (counts['Transporting']) parts.push(counts['Transporting'] + ' Transporting');
    if (counts['Delivered']) parts.push(counts['Delivered'] + ' Delivered');
    var ok = confirm(
      'Your selection includes ' + parts.join(' and ') +
      ' vehicle' + (nonIdleVINs.length > 1 ? 's' : '') +
      ' that already have dealer assignments.\n\n' +
      'Re-allocating will override their current assignments. Continue?'
    );
    if (!ok) return;
  }

  // Build contextual bubble labels
  var bubbles = buildAnimationBubbles(selVehicles);

  // Show animation (skipped for the silent demo batch run on load)
  if (!opts.silent) showAIAnimation(bubbles, vinList.length);

  try {
    // Fire all three algorithms in parallel so the Algorithm toggle can
    // switch between them instantly without a re-run. Additive ILP, Bucket
    // ILP, and Greedy share the same VIN list and run independently on the
    // server; the responses arrive in roughly the same time window because
    // each spends ~80% of its wall clock inside CBC.
    var commonBody = { n_batch: vinList.length, vin_list: vinList };
    var apiCalls = Promise.all([
      api('/api/weekly', Object.assign({}, commonBody, scoringDefaults, { scoring_mode: 'additive' })),
      api('/api/weekly', Object.assign({}, commonBody, scoringDefaults, { scoring_mode: 'bucket', bucket_signal_field: 'IN_SERVICE' })),
      api('/api/weekly_greedy', Object.assign({}, commonBody, scoringDefaults)),
    ]);
    var minAnim = opts.silent ? Promise.resolve() : new Promise(function(r) { setTimeout(r, 3500); });
    var results = await apiCalls;
    await minAnim;

    var additiveResult = results[0];
    var bucketResult = results[1];
    var greedyResult = results[2];

    hideAIAnimation();
    weeklyAdditiveData = additiveResult;
    weeklyBucketData = bucketResult;
    weeklyGreedyData = greedyResult;
    weeklyData = (scoringMode === 'bucket') ? weeklyBucketData : weeklyAdditiveData;
    if (weeklyMethod === 'greedy') weeklyData = weeklyGreedyData;
    lastAllocatedVINs = vinList.slice();

    // Snapshot each algorithm's original pick so selectDealer() can detect
    // user overrides separately in each view. Overrides are mode-local —
    // editing a vehicle's dealer in the Additive view does NOT change what
    // Bucket or Greedy chose for the same VIN.
    [weeklyAdditiveData, weeklyBucketData, weeklyGreedyData].forEach(function(d) {
      if (!d || !d.vehicles) return;
      for (var i = 0; i < d.vehicles.length; i++) {
        var v = d.vehicles[i];
        v.ilpDealer = v.assigned ? v.assigned.dealer_code : null;
        v.overridden = false;
      }
    });

    weeklyLoaded = true;
    allocationConfirmed = false;
    var confirmBtn = document.getElementById('btn-confirm');
    if (confirmBtn) {
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Confirm Allocation';
      confirmBtn.style.background = '';
    }
    weeklySelectedVINs.clear();
    weeklyCollapsedGroups.clear();
    weeklyShowFilter = 'assigned';
    weeklySourceFilter = '';
    weeklyStateFilter = '';
    weeklyWeekFilter = '';

    // Build allocCompareData from whichever ILP variant the user is viewing.
    // Greedy is the always-on baseline. Switching the toggle later swaps the
    // ILP side via _rebuildAllocCompare() — no API call.
    _rebuildAllocCompare();

    // Update batch date
    var d = new Date();
    document.getElementById('batch-date').textContent =
      'Week of ' + d.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });

    updateAlgorithmToggle();
    updateAlgoStagingHint();
    renderWeekly();
    renderMethodDelta();
    if (!opts.silent) switchPage('weekly');

    // Force dashboard refresh on next visit
    overviewLoaded = false;

    // Clear selection
    selectedVINs.clear();
    updateAllocateButton();
    renderFleetTable();
  } catch (e) {
    hideAIAnimation();
    console.error('Allocation failed:', e);
    if (!opts.silent) alert('Allocation failed: ' + e.message);
  }
}

// Public demo: run one default batch on load so Home, Weekly Allocation and
// Batch Overview open with results. Optimizer only, no AI call. The batch is
// the 15 grounded vehicles that have waited longest.
var DEMO_BATCH_SIZE = 15;

async function runDemoBatch() {
  if (!fleetData || !fleetData.vehicles) return;
  var grounded = fleetData.vehicles.filter(function(v) { return v.status === 'Grounded'; });
  grounded.sort(function(a, b) {
    return String(a.week || '').localeCompare(String(b.week || '')) || String(a.vin).localeCompare(String(b.vin));
  });
  var vins = grounded.slice(0, DEMO_BATCH_SIZE).map(function(v) { return v.vin; });
  if (vins.length) await allocateSelected({ overrideVins: vins, silent: true });
}

function buildAnimationBubbles(vehicles) {
  var bubbles = [];
  // Visual bubbles during the AI-solving animation. Labels reflect the
  // ACTIVE scoring mode so the animation does not mislead under bucket.
  var factors;
  if (scoringMode === 'bucket') {
    factors = [
      { label: 'Dealer Tier', sub: 'Max-anchored 25% IN_SERVICE buckets', color: '#00e676', size: 42 },
      { label: 'Bucket Mult', sub: 'A > B > C > D priority', color: '#81d4fa', size: 38 },
      { label: 'Distance', sub: 'Drivable miles penalty', color: '#ff6b6b', size: 38 },
      { label: 'Property Tax', sub: 'Annualized tax penalty', color: '#ffc107', size: 34 },
      { label: 'Slot Limit', sub: 'Per-dealer cap on incoming cars', color: '#ab47bc', size: 36 },
      { label: 'ILP Stage 1', sub: 'Max assignments', color: '#26c6da', size: 44 },
      { label: 'ILP Stage 2', sub: 'Max allocation score', color: '#66bb6a', size: 44 },
    ];
  } else {
    factors = [
      { label: 'Utilization', sub: 'w_util × UTIL_RATE', color: '#00e676', size: 42 },
      { label: 'Rented Cars', sub: 'w_rented × RENTED (demand)', color: '#81d4fa', size: 38 },
      { label: 'Distance', sub: 'Drivable miles penalty', color: '#ff6b6b', size: 38 },
      { label: 'Property Tax', sub: 'Annualized tax penalty', color: '#ffc107', size: 34 },
      { label: 'Slot Limit', sub: 'Per-dealer cap on incoming cars', color: '#ab47bc', size: 36 },
      { label: 'ILP Stage 1', sub: 'Max assignments', color: '#26c6da', size: 44 },
      { label: 'ILP Stage 2', sub: 'Max allocation score', color: '#66bb6a', size: 44 },
    ];
  }

  for (var i = 0; i < factors.length; i++) {
    bubbles.push(factors[i]);
  }

  // Add contextual bubbles from selected vehicles
  var states = {};
  var sources = {};
  var totalResidual = 0;
  for (var j = 0; j < vehicles.length; j++) {
    var v = vehicles[j];
    states[v.source_state] = (states[v.source_state] || 0) + 1;
    sources[v.source] = (sources[v.source] || 0) + 1;
    totalResidual += v.residual;
  }

  // Vehicle count bubble
  bubbles.push({ label: vehicles.length + ' Vehicles', sub: 'Selected for allocation', color: '#ffffff', size: 46 });

  // Avg residual
  if (vehicles.length > 0) {
    bubbles.push({ label: '$' + fmt(totalResidual / vehicles.length), sub: 'Avg residual value', color: '#80deea', size: 32 });
  }

  // State bubbles
  var stateKeys = Object.keys(states).sort(function(a, b) { return states[b] - states[a]; });
  for (var k = 0; k < Math.min(stateKeys.length, 4); k++) {
    bubbles.push({ label: stateKeys[k], sub: states[stateKeys[k]] + ' vehicle(s)', color: '#90a4ae', size: 26 });
  }

  // Source bubbles
  var srcKeys = Object.keys(sources).sort(function(a, b) { return sources[b] - sources[a]; });
  for (var m = 0; m < Math.min(srcKeys.length, 3); m++) {
    bubbles.push({ label: srcKeys[m], sub: 'Source dealer', color: '#78909c', size: 24 });
  }

  return bubbles;
}

// ── AI Animation (CSS-based — v2 replaced canvas physics) ──

var _aiStatusTimer = null;

function showAIAnimation(bubbles, vehicleCount) {
  // v2: overlay's particle system is pure CSS (see ai-particle / ai-orb classes).
  // This function just toggles the overlay class and rotates the status text.
  var overlay = document.getElementById('ai-overlay');
  if (!overlay) return;
  overlay.classList.add('active');

  var sub = document.getElementById('ai-center-sub');
  var statusTexts = [
    'Analyzing ' + (vehicleCount || 'vehicles in') + ' batch...',
    'Loading dealer candidates...',
    'Computing UTIL × RENTED scores...',
    'Applying distance penalty (cost-aware)...',
    'Solving ILP optimization...',
    'Checking slot capacity constraints...',
    'Ranking final placements...',
    'Finalizing batch...',
  ];
  var idx = 0;
  if (sub) sub.textContent = statusTexts[0];
  if (_aiStatusTimer) clearInterval(_aiStatusTimer);
  _aiStatusTimer = setInterval(function() {
    idx++;
    if (sub) sub.textContent = statusTexts[idx % statusTexts.length];
  }, 600);
}


function hideAIAnimation() {
  // v2: CSS animations stop when overlay loses .active; just clear the status timer
  if (_aiStatusTimer) { clearInterval(_aiStatusTimer); _aiStatusTimer = null; }
  var overlay = document.getElementById('ai-overlay');
  if (overlay) overlay.classList.remove('active');
}

function hexToRgb(hex) {
  if (hex.startsWith('#')) {
    var val = parseInt(hex.slice(1), 16);
    return { r: (val >> 16) & 255, g: (val >> 8) & 255, b: val & 255 };
  }
  // Named colors fallback
  return { r: 0, g: 212, b: 255 };
}

// ── Weekly Allocation ────────────────────────────────

function renderWeekly() {
  var d = getActiveWeeklyData();
  if (!d) return;

  // Show confirm button (the toggles stay visible at all times so the
  // user can stage scoring_mode / method before the first batch runs).
  var confirmBtn = document.getElementById('btn-confirm');
  if (confirmBtn) confirmBtn.style.display = '';
  var toolbar = document.getElementById('weekly-toolbar');
  if (toolbar) toolbar.style.display = '';

  document.getElementById('batch-kpis').innerHTML =
    renderBatchKPIs({
      batch_size: d.batch_size,
      n_assigned: d.n_assigned,
      cars_high_util: countHighUtil(d.vehicles),
      avg_dest_util: avgDestUtil(d.vehicles),
      total_distance: d.total_distance,
    });

  // Populate filter dropdowns
  _populateWeeklyFilters(d);

  // Group vehicles by source
  var groups = {};
  var groupOrder = [];
  for (var i = 0; i < d.vehicles.length; i++) {
    var v = d.vehicles[i];
    var key = v.source;
    if (!groups[key]) {
      groups[key] = { source: v.source, city: v.source_city, state: v.source_state, vehicles: [] };
      groupOrder.push(key);
    }
    groups[key].vehicles.push({ v: v, idx: i });
  }

  var html = '';
  for (var g = 0; g < groupOrder.length; g++) {
    var grp = groups[groupOrder[g]];
    var count = grp.vehicles.length;
    var grpAssigned = 0;
    for (var gi = 0; gi < count; gi++) {
      if (grp.vehicles[gi].v.assigned) grpAssigned++;
    }

    // Determine if this source group should be hidden by filters
    var groupHidden = false;
    if (weeklySourceFilter && grp.source !== weeklySourceFilter) groupHidden = true;
    if (weeklyStateFilter && grp.state !== weeklyStateFilter) groupHidden = true;
    if (weeklyShowFilter === 'assigned' && grpAssigned === 0) groupHidden = true;

    var isCollapsed = weeklyCollapsedGroups.has(grp.source);
    var assignedLabel = grpAssigned + '/' + count + ' assigned';
    var countLabel = count === 1 ? '1 vehicle' : count + ' vehicles';

    html += '<div class="source-group' + (groupHidden ? ' wt-hidden' : '') + '" data-source="' + escHtml(grp.source) + '" data-state="' + escHtml(grp.state) + '">' +
      '<div class="source-group-header" onclick="toggleSourceGroup(\'' + escHtml(grp.source) + '\')" style="cursor:pointer">' +
        '<div class="source-group-title">' +
          '<span class="source-group-toggle' + (isCollapsed ? ' collapsed' : '') + '" id="sgt-' + escHtml(grp.source) + '">&#9660;</span>' +
          '<span class="source-group-code">' + escHtml(grp.source) + '</span>' +
          '<span class="source-group-loc">' + escHtml(grp.city) + ', ' + escHtml(grp.state) + '</span>' +
        '</div>' +
        '<div class="source-group-stats">' +
          '<span class="source-group-count">' + assignedLabel + '</span>' +
        '</div>' +
      '</div>';

    // Table body (collapsible)
    html += '<div class="source-group-body' + (isCollapsed ? ' collapsed' : '') + '" id="sgb-' + escHtml(grp.source) + '">';
    html += '<table class="source-group-table"><thead><tr>' +
      '<th style="width:30px"><input type="checkbox" class="sg-checkbox" onchange="toggleGroupCheckbox(\'' + escHtml(grp.source) + '\', this.checked)" ' +
        (_isGroupAllSelected(grp) ? 'checked' : '') + '></th>' +
      '<th>VIN</th><th>Residual</th><th></th><th>Assigned To</th><th>Distance</th><th>Utilization</th><th>Tax Rate</th><th>Prop Tax</th><th>Choice <span class="info-icon" onclick="showInfoPopover(event, \'rank_column\')">ⓘ</span></th>' +
    '</tr></thead><tbody>';

    for (var si = 0; si < count; si++) {
      var sv = grp.vehicles[si].v;
      var sa = sv.assigned;
      var sidx = grp.vehicles[si].idx;

      // Week filter: hide vehicles not matching selected week
      var vHidden = false;
      if (weeklyWeekFilter) {
        // fleetData has week info; match by VIN
        var vWeek = _getVehicleWeek(sv.vin);
        if (vWeek && vWeek !== weeklyWeekFilter) vHidden = true;
      }
      if (weeklyShowFilter === 'assigned' && !sa) vHidden = true;

      var dealerDropdown = buildDealerDropdown(sv, sidx);
      var isChecked = weeklySelectedVINs.has(sv.vin);
      var checkboxHtml = '<td><input type="checkbox" class="sg-checkbox" data-vin="' + escHtml(sv.vin) + '" onchange="toggleWeeklyVIN(\'' + escHtml(sv.vin) + '\', this.checked)"' + (isChecked ? ' checked' : '') + '></td>';

      if (sa) {
        html += '<tr id="vrow-' + sidx + '" data-vin="' + escHtml(sv.vin) + '"' + (vHidden ? ' style="display:none"' : '') + '>' +
          checkboxHtml +
          '<td class="sg-vin">' + escHtml(sv.vin) + '</td>' +
          '<td>$' + fmt(sv.residual) + '</td>' +
          '<td class="sg-arrow">&rarr;</td>' +
          '<td>' + dealerDropdown + '</td>' +
          '<td id="vcol-dist-' + sidx + '">' + sa.distance + ' mi</td>' +
          '<td id="vcol-util-' + sidx + '">' + (sa.utilization != null ? sa.utilization.toFixed(1) + '%' : '&mdash;') + '</td>' +
          '<td id="vcol-tax-rate-' + sidx + '">' + (sa.prop_tax_rate || 0) + '%</td>' +
          '<td id="vcol-tax-' + sidx + '">$' + fmt(sa.prop_tax || 0) + '</td>' +
          '<td id="vcol-net-' + sidx + '">' + rankCell(sa.rank, sa.alloc_score) + '</td>' +
        '</tr>';
      } else {
        html += '<tr id="vrow-' + sidx + '" data-vin="' + escHtml(sv.vin) + '" class="sg-unassigned"' + (vHidden ? ' style="display:none"' : '') + '>' +
          checkboxHtml +
          '<td class="sg-vin">' + escHtml(sv.vin) + '</td>' +
          '<td>$' + fmt(sv.residual) + '</td>' +
          '<td class="sg-arrow" style="color:var(--negative)">&times;</td>' +
          '<td>' + dealerDropdown + '</td>' +
          '<td id="vcol-dist-' + sidx + '">&mdash;</td>' +
          '<td id="vcol-util-' + sidx + '">&mdash;</td>' +
          '<td id="vcol-tax-rate-' + sidx + '">&mdash;</td>' +
          '<td id="vcol-tax-' + sidx + '">&mdash;</td>' +
          '<td id="vcol-net-' + sidx + '">&mdash;</td>' +
        '</tr>';
      }
      // Constraint warning placeholder
      html += '<tr class="sg-reason-row" id="cwarn-' + sidx + '" style="display:none"><td colspan="10"></td></tr>';
      // Reasoning row
      html += '<tr class="sg-reason-row"' + (vHidden ? ' style="display:none"' : '') + '><td colspan="10"><span class="sg-reason"><strong>Why:</strong> ' + escHtml(sv.reasoning) + '</span></td></tr>';
      // Alternatives (hidden)
      var alts = sv.alternatives || [];
      if (alts.length > 1) {
        html += '<tr class="sg-alts-row" id="alts-' + sidx + '" style="display:none"><td colspan="10">' + renderAltsTable(alts, sa, sidx) + '</td></tr>';
      }
    }
    html += '</tbody></table></div></div>';
  }
  document.getElementById('vehicle-list').innerHTML = html;
  _updateWeeklyBatchCount();
}

// ── Weekly Toolbar: Filters & Batch Select ───────────

function _populateWeeklyFilters(d) {
  var sources = {}, states = {}, weeks = {};
  for (var i = 0; i < d.vehicles.length; i++) {
    var v = d.vehicles[i];
    sources[v.source] = v.source_city + ', ' + v.source_state;
    states[v.source_state] = true;
    var w = _getVehicleWeek(v.vin);
    if (w) weeks[w] = true;
  }

  var srcSel = document.getElementById('wt-source-filter');
  var prevSrc = srcSel.value;
  srcSel.innerHTML = '<option value="">All Sources</option>';
  Object.keys(sources).sort().forEach(function(s) {
    srcSel.innerHTML += '<option value="' + escHtml(s) + '"' + (s === prevSrc ? ' selected' : '') + '>' + escHtml(s) + ' — ' + escHtml(sources[s]) + '</option>';
  });

  var stSel = document.getElementById('wt-state-filter');
  var prevSt = stSel.value;
  stSel.innerHTML = '<option value="">All States</option>';
  Object.keys(states).sort().forEach(function(s) {
    stSel.innerHTML += '<option value="' + escHtml(s) + '"' + (s === prevSt ? ' selected' : '') + '>' + escHtml(s) + '</option>';
  });

  var wkSel = document.getElementById('wt-week-filter');
  var prevWk = wkSel.value;
  wkSel.innerHTML = '<option value="">All Weeks</option>';
  Object.keys(weeks).sort().forEach(function(w) {
    wkSel.innerHTML += '<option value="' + escHtml(w) + '"' + (w === prevWk ? ' selected' : '') + '>' + escHtml(w) + '</option>';
  });
}

function _getVehicleWeek(vin) {
  if (!fleetData || !fleetData.vehicles) return '';
  for (var i = 0; i < fleetData.vehicles.length; i++) {
    if (fleetData.vehicles[i].vin === vin) return fleetData.vehicles[i].week || '';
  }
  return '';
}

function _isGroupAllSelected(grp) {
  for (var i = 0; i < grp.vehicles.length; i++) {
    var v = grp.vehicles[i].v;
    if (v.assigned && !weeklySelectedVINs.has(v.vin)) return false;
  }
  return grp.vehicles.some(function(item) { return item.v.assigned; });
}

function setWeeklyFilter(mode) {
  weeklyShowFilter = mode;
  var btns = document.querySelectorAll('#wt-assigned-toggle .wt-btn');
  btns.forEach(function(b) { b.classList.remove('active'); });
  if (mode === 'assigned') btns[0].classList.add('active');
  else btns[1].classList.add('active');
  applyWeeklyFilters();
}

function applyWeeklyFilters() {
  weeklySourceFilter = document.getElementById('wt-source-filter').value;
  weeklyStateFilter = document.getElementById('wt-state-filter').value;
  weeklyWeekFilter = document.getElementById('wt-week-filter').value;
  renderWeekly();
}

function toggleSourceGroup(source) {
  if (weeklyCollapsedGroups.has(source)) {
    weeklyCollapsedGroups.delete(source);
  } else {
    weeklyCollapsedGroups.add(source);
  }
  var toggle = document.getElementById('sgt-' + source);
  var body = document.getElementById('sgb-' + source);
  if (toggle) toggle.classList.toggle('collapsed');
  if (body) body.classList.toggle('collapsed');
}

function toggleGroupCheckbox(source, checked) {
  if (!weeklyData) return;
  for (var i = 0; i < weeklyData.vehicles.length; i++) {
    var v = weeklyData.vehicles[i];
    if (v.source === source && v.assigned) {
      if (checked) weeklySelectedVINs.add(v.vin);
      else weeklySelectedVINs.delete(v.vin);
    }
  }
  // Update individual checkboxes in this group
  var grpEl = document.querySelector('.source-group[data-source="' + source + '"]');
  if (grpEl) {
    grpEl.querySelectorAll('tbody .sg-checkbox').forEach(function(cb) {
      var vin = cb.getAttribute('data-vin');
      if (vin) cb.checked = weeklySelectedVINs.has(vin);
    });
  }
  _updateWeeklyBatchCount();
}

function toggleWeeklyVIN(vin, checked) {
  if (checked) weeklySelectedVINs.add(vin);
  else weeklySelectedVINs.delete(vin);
  _updateWeeklyBatchCount();
}

function weeklySelectAll() {
  if (!weeklyData) return;
  // Select all visible assigned vehicles
  var visible = document.querySelectorAll('#vehicle-list tr[data-vin]:not([style*="display:none"])');
  visible.forEach(function(tr) {
    var vin = tr.getAttribute('data-vin');
    if (vin) {
      // Only select if assigned
      for (var i = 0; i < weeklyData.vehicles.length; i++) {
        if (weeklyData.vehicles[i].vin === vin && weeklyData.vehicles[i].assigned) {
          weeklySelectedVINs.add(vin);
          break;
        }
      }
    }
  });
  // Update all visible checkboxes
  document.querySelectorAll('#vehicle-list .sg-checkbox[data-vin]').forEach(function(cb) {
    cb.checked = weeklySelectedVINs.has(cb.getAttribute('data-vin'));
  });
  // Update group header checkboxes
  document.querySelectorAll('#vehicle-list thead .sg-checkbox').forEach(function(cb) { cb.checked = true; });
  _updateWeeklyBatchCount();
}

function weeklyDeselectAll() {
  weeklySelectedVINs.clear();
  document.querySelectorAll('#vehicle-list .sg-checkbox').forEach(function(cb) { cb.checked = false; });
  _updateWeeklyBatchCount();
}

function _updateWeeklyBatchCount() {
  var el = document.getElementById('wt-batch-count');
  if (el) el.textContent = weeklySelectedVINs.size + ' selected';
}

// ── Dealer Dropdown + Constraint Validation ──────────

function buildDealerDropdown(vehicle, idx) {
  var alts = vehicle.alternatives || [];
  var currentDealer = vehicle.assigned ? vehicle.assigned.dealer_code : '';
  if (alts.length === 0) return '<span style="color:var(--muted)">No options</span>';

  var label = '— Unassigned —';
  if (vehicle.assigned) {
    label = escHtml(vehicle.assigned.dealer_name) + ' (' + escHtml(vehicle.assigned.state) + ')';
  }

  return '<div class="dd-wrap" id="dd-' + idx + '">' +
    '<button class="dd-selected" onclick="toggleDealerDD(' + idx + ')">' +
      '<span class="dd-label">' + label + '</span>' +
      '<span class="dd-caret">&#9662;</span>' +
    '</button>' +
    '<div class="dd-menu" id="dd-menu-' + idx + '">' +
      '<input type="text" class="dd-search" placeholder="Search dealer..." oninput="filterDealerDD(' + idx + ', this.value)">' +
      '<div class="dd-options" id="dd-opts-' + idx + '">' +
        _buildDDOptions(alts, currentDealer, idx) +
      '</div>' +
    '</div>' +
    '<a class="dd-details" onclick="toggleAlts(' + idx + ')">details</a>' +
  '</div>';
}

function _buildDDOptions(alts, currentDealer, idx, query) {
  var q = (query || '').toLowerCase();
  var html = '<div class="dd-option' + (!currentDealer ? ' active' : '') + '" onclick="selectDealer(' + idx + ', \'\')">' +
    '<span class="dd-opt-name">— Unassign —</span></div>';
  for (var i = 0; i < alts.length; i++) {
    var a = alts[i];
    if (q && a.dealer_name.toLowerCase().indexOf(q) < 0 && a.dealer_code.toLowerCase().indexOf(q) < 0 && a.state.toLowerCase().indexOf(q) < 0) continue;
    var cls = a.dealer_code === currentDealer ? ' active' : '';
    var rankCls = a.rank === 1 ? ' positive' : (a.rank <= 3 ? '' : ' negative');
    var rankTxt = a.rank === 1 ? '★ #1' : '#' + a.rank;
    var rentedTxt = (a.rented != null) ? (' &middot; ' + a.rented + ' rented') : '';
    html += '<div class="dd-option' + cls + '" onclick="selectDealer(' + idx + ', \'' + escHtml(a.dealer_code) + '\')">' +
      '<span class="dd-opt-name">' + escHtml(a.dealer_code) + ' — ' + escHtml(a.dealer_name) + '</span>' +
      '<span class="dd-opt-meta">' + escHtml(a.state) + ' &middot; ' + a.distance + 'mi &middot; ' + a.utilization + '% util' + rentedTxt + '</span>' +
      '<span class="dd-opt-score' + rankCls + '" title="Algorithm ranking; raw score: ' + fmtSigned(a.alloc_score) + '">' + rankTxt + '</span>' +
    '</div>';
  }
  return html;
}

function toggleDealerDD(idx) {
  var menu = document.getElementById('dd-menu-' + idx);
  if (!menu) return;
  var isOpen = menu.classList.contains('open');
  // Close all other open menus
  document.querySelectorAll('.dd-menu.open').forEach(function(m) { m.classList.remove('open'); });
  if (!isOpen) {
    menu.classList.add('open');
    var input = menu.querySelector('.dd-search');
    if (input) { input.value = ''; input.focus(); }
  }
}

function filterDealerDD(idx, query) {
  var activeData = getActiveWeeklyData();
  if (!activeData) return;
  var v = activeData.vehicles[idx];
  var currentDealer = v.assigned ? v.assigned.dealer_code : '';
  var optsEl = document.getElementById('dd-opts-' + idx);
  if (optsEl) optsEl.innerHTML = _buildDDOptions(v.alternatives || [], currentDealer, idx, query);
}

function selectDealer(idx, dealerCode) {
  var menu = document.getElementById('dd-menu-' + idx);
  if (menu) menu.classList.remove('open');
  changeDealer(idx, dealerCode);
  // Update the label
  var activeData = getActiveWeeklyData();
  if (!activeData) return;
  var v = activeData.vehicles[idx];
  var labelEl = document.querySelector('#dd-' + idx + ' .dd-label');
  if (labelEl) {
    if (v.assigned) {
      labelEl.textContent = v.assigned.dealer_name + ' (' + v.assigned.state + ')';
    } else {
      labelEl.textContent = '— Unassigned —';
    }
  }
}

// Close dropdown when clicking outside
document.addEventListener('mousedown', function(e) {
  if (!e.target.closest('.dd-wrap')) {
    document.querySelectorAll('.dd-menu.open').forEach(function(m) { m.classList.remove('open'); });
  }
  if (!e.target.closest('.week-picker-wrap')) {
    var cal = document.getElementById('week-calendar');
    if (cal) cal.classList.remove('open');
  }
});

function changeDealer(idx, newDealerCode) {
  var activeData = getActiveWeeklyData();
  if (!activeData || !activeData.vehicles[idx]) return;

  // Reset confirmed state — user changed the allocation
  if (allocationConfirmed) {
    allocationConfirmed = false;
    var btn = document.getElementById('btn-confirm');
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Confirm Allocation';
      btn.style.background = '';
    }
  }

  var v = activeData.vehicles[idx];
  var alts = v.alternatives || [];

  // Find the new dealer in alternatives
  var newDealer = null;
  if (newDealerCode) {
    for (var i = 0; i < alts.length; i++) {
      if (alts[i].dealer_code === newDealerCode) { newDealer = alts[i]; break; }
    }
  }

  // Update assignment
  var oldDealer = v.assigned ? v.assigned.dealer_code : null;
  v.assigned = newDealer;
  v.assigned_dealer = newDealerCode || null;
  // override = current selection differs from the ILP's original pick.
  // If the user reverts to ilpDealer, the flag clears automatically.
  v.overridden = (newDealerCode || null) !== (v.ilpDealer || null);

  // Update row cells
  var row = document.getElementById('vrow-' + idx);
  if (row) {
    if (newDealer) {
      row.classList.remove('sg-unassigned');
      var arrow = row.querySelector('.sg-arrow');
      if (arrow) { arrow.textContent = '→'; arrow.style.color = ''; }
      setCell('vcol-dist-' + idx, newDealer.distance + ' mi');
      setCell('vcol-util-' + idx, newDealer.utilization != null ? newDealer.utilization.toFixed(1) + '%' : '—');
      setCell('vcol-tax-rate-' + idx, (newDealer.prop_tax_rate || 0) + '%');
      setCell('vcol-tax-' + idx, '$' + fmt(newDealer.prop_tax || 0));
      var netEl = document.getElementById('vcol-net-' + idx);
      if (netEl) { netEl.innerHTML = rankCell(newDealer.rank, newDealer.alloc_score); }
    } else {
      row.classList.add('sg-unassigned');
      var arrow = row.querySelector('.sg-arrow');
      if (arrow) { arrow.textContent = '✗'; arrow.style.color = 'var(--negative)'; }
      setCell('vcol-dist-' + idx, '—');
      setCell('vcol-util-' + idx, '—');
      setCell('vcol-tax-rate-' + idx, '—');
      setCell('vcol-tax-' + idx, '—');
      setCell('vcol-net-' + idx, '—');
    }
  }

  // Check constraints for this vehicle AND re-check other vehicles affected by the change
  refreshAllConstraints();

  // Recalculate batch KPIs
  recalcBatchKPIs();
}

function setCell(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = text;
}

function checkConstraints(changedIdx, newDealerCode, oldDealerCode) {
  var activeData = getActiveWeeklyData();
  if (!activeData || !newDealerCode) return null;

  // Count how many vehicles in batch are assigned to the new dealer
  var count = 0;
  for (var i = 0; i < activeData.vehicles.length; i++) {
    var v = activeData.vehicles[i];
    if (v.assigned && v.assigned.dealer_code === newDealerCode) count++;
  }

  // Get capacity from the alternative data
  var vehicle = activeData.vehicles[changedIdx];
  var alts = vehicle.alternatives || [];
  var dealerInfo = null;
  for (var j = 0; j < alts.length; j++) {
    if (alts[j].dealer_code === newDealerCode) { dealerInfo = alts[j]; break; }
  }

  if (dealerInfo && count > dealerInfo.remaining_capacity) {
    return escHtml(dealerInfo.dealer_name) + ' (' + escHtml(newDealerCode) + ') overflow: ' +
      count + ' assigned but dealer can only take ' + dealerInfo.remaining_capacity + ' more car(s). Other vehicles assigned here may need reassignment.';
  }

  // Check source limit (only if backend provided one)
  var sourceLimit = activeData.params && activeData.params.source_limit;
  if (sourceLimit != null) {
    var sourceCount = 0;
    var src = vehicle.source;
    for (var k = 0; k < activeData.vehicles.length; k++) {
      if (activeData.vehicles[k].source === src && activeData.vehicles[k].assigned) sourceCount++;
    }
    if (sourceCount > sourceLimit) {
      return 'Source ' + escHtml(src) + ' has ' + sourceCount + ' vehicles assigned (limit: ' + sourceLimit + '). Reduce assignments from this source.';
    }
  }

  return null;
}

function refreshAllConstraints() {
  var activeData = getActiveWeeklyData();
  if (!activeData) return;
  for (var i = 0; i < activeData.vehicles.length; i++) {
    var v = activeData.vehicles[i];
    var dealerCode = v.assigned ? v.assigned.dealer_code : null;
    var warn = checkConstraints(i, dealerCode, null);
    var cwarnRow = document.getElementById('cwarn-' + i);
    if (cwarnRow) {
      if (warn) {
        cwarnRow.style.display = 'table-row';
        cwarnRow.querySelector('td').innerHTML =
          '<div class="constraint-warn">' +
            '<span class="warn-icon">⚠</span>' +
            '<span class="warn-text">' + warn + '</span>' +
            '<button class="btn-ask-agent" onclick="askAgentConstraint(' + i + ')">Ask Agent</button>' +
          '</div>';
      } else {
        cwarnRow.style.display = 'none';
      }
    }
  }
}

function recalcBatchKPIs() {
  var activeData = getActiveWeeklyData();
  if (!activeData) return;
  var nAssigned = 0, totalNet = 0;
  // Unique-arc distance: one carrier trip moves multiple cars along the same
  // route, so we count each (source, dealer) arc at most once. Must mirror
  // the engine's aggregation so the KPI after a user override stays correct.
  var uniqueArcs = {};
  for (var i = 0; i < activeData.vehicles.length; i++) {
    var v = activeData.vehicles[i];
    if (v.assigned) {
      nAssigned++;
      totalNet += v.assigned.alloc_score;
      var key = v.source + '|' + v.assigned.dealer_code;
      uniqueArcs[key] = v.assigned.distance || 0;
    }
  }
  var totalDistance = 0;
  for (var k in uniqueArcs) totalDistance += uniqueArcs[k];
  activeData.n_assigned = nAssigned;
  activeData.total_alloc_score = Math.round(totalNet * 10000) / 10000;
  activeData.total_distance = Math.round(totalDistance * 10) / 10;

  document.getElementById('batch-kpis').innerHTML =
    renderBatchKPIs({
      batch_size: activeData.batch_size,
      n_assigned: nAssigned,
      cars_high_util: countHighUtil(activeData.vehicles),
      avg_dest_util: avgDestUtil(activeData.vehicles),
      total_distance: totalDistance,
    });
}

// Count vehicles whose assigned dealer has UTIL_RATE ≥ 80% — an
// algorithm-independent measure of destination quality (dealer property,
// not rank). Updates live when the user overrides a dealer.
function countHighUtil(vehicles) {
  var n = 0;
  for (var i = 0; i < vehicles.length; i++) {
    var v = vehicles[i];
    if (v.assigned && v.assigned.utilization != null && v.assigned.utilization >= 80) n++;
  }
  return n;
}

// Average destination utilization (%) across all assigned vehicles —
// independent of algorithm ranking.
function avgDestUtil(vehicles) {
  var sum = 0, n = 0;
  for (var i = 0; i < vehicles.length; i++) {
    var v = vehicles[i];
    if (v.assigned && v.assigned.utilization != null) { sum += v.assigned.utilization; n++; }
  }
  return n ? sum / n : 0;
}

function renderBatchKPIs(k) {
  var huRate = k.n_assigned ? (k.cars_high_util / k.n_assigned * 100) : 0;
  var huCls = huRate >= 80 ? 'positive' : huRate >= 50 ? '' : 'negative';
  var duCls = k.avg_dest_util >= 80 ? 'positive' : k.avg_dest_util >= 60 ? '' : 'negative';
  var duTxt = k.avg_dest_util > 0 ? k.avg_dest_util.toFixed(1) + '%' : '—';
  return '<div class="batch-kpi">' +
      '<div class="batch-kpi-label">Vehicles</div>' +
      '<div class="batch-kpi-value">' + k.batch_size + '</div>' +
    '</div>' +
    '<div class="batch-kpi">' +
      '<div class="batch-kpi-label">Assigned</div>' +
      '<div class="batch-kpi-value ' + (k.n_assigned === k.batch_size ? 'positive' : '') + '">' + k.n_assigned + '/' + k.batch_size + '</div>' +
    '</div>' +
    '<div class="batch-kpi">' +
      '<div class="batch-kpi-label">At dealers ≥80% util <span class="info-icon" onclick="showInfoPopover(event, \'high_util_kpi\')">ⓘ</span></div>' +
      '<div class="batch-kpi-value ' + huCls + '">' + k.cars_high_util + '/' + k.n_assigned + ' (' + huRate.toFixed(0) + '%)</div>' +
    '</div>' +
    '<div class="batch-kpi">' +
      '<div class="batch-kpi-label">Avg destination util <span class="info-icon" onclick="showInfoPopover(event, \'avg_dest_util\')">ⓘ</span></div>' +
      '<div class="batch-kpi-value ' + duCls + '">' + duTxt + '</div>' +
    '</div>' +
    '<div class="batch-kpi">' +
      '<div class="batch-kpi-label">Total distance <span class="info-icon" onclick="showInfoPopover(event, \'total_distance\')">ⓘ</span></div>' +
      '<div class="batch-kpi-value">' + fmt(k.total_distance) + ' mi</div>' +
    '</div>';
}

// ── Confirm Allocation ────────────────────────────────

async function confirmAllocation() {
  if (!weeklyData || !weeklyData.vehicles) return;

  // Check for any constraint warnings
  var hasWarns = false;
  for (var i = 0; i < weeklyData.vehicles.length; i++) {
    var cwarn = document.getElementById('cwarn-' + i);
    if (cwarn && cwarn.style.display !== 'none') { hasWarns = true; break; }
  }
  if (hasWarns) {
    if (!confirm('There are constraint warnings. Confirm anyway?')) return;
  }

  // Build assignments — if batch selected, only confirm those; otherwise all
  var useSelection = weeklySelectedVINs.size > 0;
  var assignments = [];
  for (var j = 0; j < weeklyData.vehicles.length; j++) {
    var v = weeklyData.vehicles[j];
    if (useSelection && !weeklySelectedVINs.has(v.vin)) continue;
    assignments.push({
      vin: v.vin,
      dealer_code: v.assigned ? v.assigned.dealer_code : '',
      dealer_name: v.assigned ? v.assigned.dealer_name : '',
    });
  }

  var btn = document.getElementById('btn-confirm');
  btn.disabled = true;
  btn.textContent = 'Confirming...';

  try {
    var result = await api('/api/confirm', { assignments: assignments });

    btn.textContent = 'Confirmed ✓';
    btn.style.background = 'rgba(0,230,118,0.25)';
    btn.disabled = true;
    allocationConfirmed = true;

    // Reload all data before switching to dashboard
    await loadFleet();
    overviewLoaded = false;

    setTimeout(function() {
      switchPage('overview');
    }, 1000);

  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Confirm Allocation';
    alert('Confirmation failed: ' + e.message);
  }
}

function askAgentConstraint(idx) {
  if (!weeklyData || !weeklyData.vehicles[idx]) return;
  var v = weeklyData.vehicles[idx];
  var dealer = v.assigned ? v.assigned.dealer_name + ' (' + v.assigned.dealer_code + ')' : 'unassigned';
  var question = 'I changed ' + v.vin + ' to ' + dealer + ' but it caused a constraint violation. What are the implications and what do you suggest?';
  document.getElementById('chat-input').value = question;
  sendChat();
}

// ── Agent Suggestion Application ─────────────────────

function applySuggestion(vin, dealerCode) {
  var activeData = getActiveWeeklyData();
  if (!activeData) return;
  for (var i = 0; i < activeData.vehicles.length; i++) {
    if (activeData.vehicles[i].vin === vin) {
      changeDealer(i, dealerCode);
      // Update the dropdown label to reflect the new dealer
      var v = activeData.vehicles[i];
      var labelEl = document.querySelector('#dd-' + i + ' .dd-label');
      if (labelEl) {
        if (v.assigned) {
          labelEl.textContent = v.assigned.dealer_name + ' (' + v.assigned.state + ')';
        } else {
          labelEl.textContent = '— Unassigned —';
        }
      }
      focusSuggestionVehicle(vin);
      break;
    }
  }
}

function focusSuggestionVehicle(vin) {
  var activeData = getActiveWeeklyData();
  if (!activeData || !activeData.vehicles) return false;

  var idx = -1;
  var vehicle = null;
  for (var i = 0; i < activeData.vehicles.length; i++) {
    if (activeData.vehicles[i].vin === vin) {
      idx = i;
      vehicle = activeData.vehicles[i];
      break;
    }
  }
  if (idx < 0 || !vehicle) return false;

  if (currentPage !== 'weekly') switchPage('weekly');

  var rerenderNeeded = false;
  if (weeklySourceFilter && weeklySourceFilter !== vehicle.source) {
    var srcSel = document.getElementById('wt-source-filter');
    if (srcSel) srcSel.value = '';
    weeklySourceFilter = '';
    rerenderNeeded = true;
  }
  if (weeklyStateFilter && weeklyStateFilter !== vehicle.source_state) {
    var stSel = document.getElementById('wt-state-filter');
    if (stSel) stSel.value = '';
    weeklyStateFilter = '';
    rerenderNeeded = true;
  }
  if (weeklyWeekFilter) {
    var vehicleWeek = _getVehicleWeek(vehicle.vin);
    if (vehicleWeek && vehicleWeek !== weeklyWeekFilter) {
      var wkSel = document.getElementById('wt-week-filter');
      if (wkSel) wkSel.value = '';
      weeklyWeekFilter = '';
      rerenderNeeded = true;
    }
  }
  if (weeklyShowFilter === 'assigned' && !vehicle.assigned) {
    weeklyShowFilter = 'all';
    var btns = document.querySelectorAll('#wt-assigned-toggle .wt-btn');
    btns.forEach(function(b) { b.classList.remove('active'); });
    if (btns[1]) btns[1].classList.add('active');
    rerenderNeeded = true;
  }

  if (weeklyCollapsedGroups.has(vehicle.source)) {
    weeklyCollapsedGroups.delete(vehicle.source);
    rerenderNeeded = true;
  }
  if (rerenderNeeded) renderWeekly();

  var elId = 'vrow-' + idx;
  var el = document.getElementById(elId);
  if (!el) return false;
  scrollWeeklyRowIntoView(el, function() {
    agentFocusElement(elId);
  });
  return true;
}

function scrollWeeklyRowIntoView(row, done) {
  var container = document.getElementById('page-container');
  if (!row || !container) {
    if (done) done();
    return;
  }
  requestAnimationFrame(function() {
    var rowRect = row.getBoundingClientRect();
    var containerRect = container.getBoundingClientRect();
    var targetTop = container.scrollTop + rowRect.top - containerRect.top - Math.max(80, container.clientHeight * 0.28);
    container.scrollTo({ top: Math.max(0, targetTop), behavior: 'smooth' });
    setTimeout(function() {
      if (done) done();
    }, 180);
  });
}

// ── Agent Focus Ring + Auto-Scan ─────────────────────

var agentScanTimer = null;
var agentScanIndex = 0;
var agentScanElements = [];

function agentFocusElement(elementId) {
  // Move previous focus to visited
  for (var i = 0; i < agentFocusTargets.length; i++) {
    var prev = document.getElementById(agentFocusTargets[i]);
    if (prev) {
      prev.classList.remove('agent-focus');
      prev.classList.add('agent-visited');
    }
  }
  // Apply focus to new element
  var el = document.getElementById(elementId);
  if (el) {
    el.classList.remove('agent-visited');
    el.classList.add('agent-focus');
    if (agentFocusTargets.indexOf(elementId) === -1) {
      agentFocusTargets.push(elementId);
    }
  }
}

function clearAgentFocus() {
  stopAgentScan();
  for (var i = 0; i < agentFocusTargets.length; i++) {
    var el = document.getElementById(agentFocusTargets[i]);
    if (el) {
      el.classList.remove('agent-focus');
      el.classList.remove('agent-visited');
    }
  }
  agentFocusTargets = [];
}

function startAgentScan() {
  stopAgentScan();
  agentScanElements = collectScanTargets();
  if (agentScanElements.length === 0) return;
  agentScanIndex = 0;

  // Immediately highlight the first element
  agentFocusElement(agentScanElements[0]);

  agentScanTimer = setInterval(function() {
    agentScanIndex++;
    if (agentScanIndex >= agentScanElements.length) {
      // Done scanning — stop but keep visited highlights
      stopAgentScan();
      return;
    }
    agentFocusElement(agentScanElements[agentScanIndex]);
  }, 600);
}

function stopAgentScan() {
  if (agentScanTimer) {
    clearInterval(agentScanTimer);
    agentScanTimer = null;
  }
}

function collectScanTargets() {
  var targets = [];
  if (currentPage === 'fleet') {
    // Scan fleet KPI cards, then visible table rows
    var kpis = document.querySelectorAll('#fleet-kpis .fleet-kpi');
    for (var k = 0; k < kpis.length; k++) {
      if (!kpis[k].id) kpis[k].id = 'fkpi-' + k;
      targets.push(kpis[k].id);
    }
    var rows = document.querySelectorAll('.fleet-table tbody tr');
    for (var r = 0; r < Math.min(rows.length, 8); r++) {
      if (!rows[r].id) rows[r].id = 'frow-' + r;
      targets.push(rows[r].id);
    }
  } else if (currentPage === 'weekly' && weeklyData) {
    // Scan batch KPIs, then vehicle rows
    var bkpis = document.querySelectorAll('#batch-kpis .batch-kpi');
    for (var b = 0; b < bkpis.length; b++) {
      if (!bkpis[b].id) bkpis[b].id = 'bkpi-' + b;
      targets.push(bkpis[b].id);
    }
    for (var v = 0; v < weeklyData.vehicles.length; v++) {
      targets.push('vrow-' + v);
    }
  } else if (currentPage === 'overview') {
    // Scan KPI cards, then panels
    var okpis = document.querySelectorAll('#kpi-grid .kpi-card');
    for (var o = 0; o < okpis.length; o++) {
      if (!okpis[o].id) okpis[o].id = 'okpi-' + o;
      targets.push(okpis[o].id);
    }
    var panels = document.querySelectorAll('#page-overview .panel');
    for (var p = 0; p < panels.length; p++) {
      if (!panels[p].id) panels[p].id = 'opanel-' + p;
      targets.push(panels[p].id);
    }
  }
  return targets;
}

var ALT_PAGE_SIZE = 5;
var altPages = {};    // altPages[uid] = current page number
var altSearch = {};   // altSearch[uid] = search string

function renderAltsTable(alts, chosen, uid) {
  var page = altPages[uid] || 0;
  var query = (altSearch[uid] || '').toLowerCase();

  // Filter by search
  var filtered = alts;
  if (query) {
    filtered = alts.filter(function(a) {
      return a.dealer_code.toLowerCase().indexOf(query) >= 0 ||
             a.dealer_name.toLowerCase().indexOf(query) >= 0 ||
             a.state.toLowerCase().indexOf(query) >= 0;
    });
  }

  var totalPages = Math.max(1, Math.ceil(filtered.length / ALT_PAGE_SIZE));
  if (page >= totalPages) page = totalPages - 1;
  var start = page * ALT_PAGE_SIZE;
  var pageItems = filtered.slice(start, start + ALT_PAGE_SIZE);

  var rows = '';
  for (var j = 0; j < pageItems.length; j++) {
    var alt = pageItems[j];
    var isCh = chosen && alt.dealer_code === chosen.dealer_code;
    var rankCls = alt.rank === 1 ? 'var(--positive)' : (alt.rank <= 3 ? '#ffc107' : 'var(--negative)');
    rows += '<tr class="' + (isCh ? 'chosen' : '') + '">' +
      '<td style="font-weight:600;color:' + rankCls + '">#' + alt.rank + '</td>' +
      '<td>' + (isCh ? '★ ' : '') + escHtml(alt.dealer_code) + '</td>' +
      '<td>' + escHtml(alt.dealer_name) + '</td>' +
      '<td>' + escHtml(alt.state) + '</td>' +
      '<td>' + alt.distance + ' mi</td>' +
      '<td>' + alt.utilization + '%</td>' +
      '<td>' + (alt.rented != null ? alt.rented : '—') + '</td>' +
      '<td>' + (alt.prop_tax_rate || 0) + '%</td>' +
      '<td>$' + fmt(alt.prop_tax) + '</td>' +
      '<td class="score-sub">' + fmtSigned(alt.alloc_score) + '</td>' +
    '</tr>';
  }

  var searchBar = '<input type="text" class="alt-search" placeholder="Search dealer..." value="' + escHtml(query) + '" oninput="altSearchChange(\'' + uid + '\', this.value)">';
  var pagination = '';
  if (totalPages > 1) {
    pagination = '<div class="alt-pagination">' +
      '<button class="alt-page-btn" onclick="altPageChange(\'' + uid + '\', -1)"' + (page <= 0 ? ' disabled' : '') + '>&laquo;</button>' +
      '<span class="alt-page-info">' + (page + 1) + ' / ' + totalPages + ' (' + filtered.length + ' dealers)</span>' +
      '<button class="alt-page-btn" onclick="altPageChange(\'' + uid + '\', 1)"' + (page >= totalPages - 1 ? ' disabled' : '') + '>&raquo;</button>' +
    '</div>';
  } else {
    pagination = '<div class="alt-pagination"><span class="alt-page-info">' + filtered.length + ' dealer' + (filtered.length !== 1 ? 's' : '') + '</span></div>';
  }

  return searchBar +
    '<table class="alt-table"><thead><tr><th>Rank</th><th>Code</th><th>Name</th><th>State</th><th>Dist</th><th>Utilization</th><th title="Cars currently earning at this dealer — primary demand signal (2026-04-21 pivot)">Rented</th><th>Tax Rate</th><th>Prop Tax</th><th class="score-sub">Score</th></tr></thead><tbody>' + rows + '</tbody></table>' +
    pagination;
}

function altPageChange(uid, delta) {
  altPages[uid] = (altPages[uid] || 0) + delta;
  _rerenderAlts(uid);
}

function altSearchChange(uid, val) {
  altSearch[uid] = val;
  altPages[uid] = 0;
  _rerenderAlts(uid);
  // Restore focus to search input after rerender
  var el = document.getElementById('alts-' + uid);
  if (el) {
    var input = el.querySelector('.alt-search');
    if (input) { input.focus(); input.value = val; input.setSelectionRange(val.length, val.length); }
  }
}

function _rerenderAlts(uid) {
  var el = document.getElementById('alts-' + uid);
  if (!el) return;
  var activeData = getActiveWeeklyData();
  if (!activeData) return;
  var idx = parseInt(uid);
  var v = activeData.vehicles[idx];
  if (!v) return;
  var content = renderAltsTable(v.alternatives || [], v.assigned, uid);
  // If el is a <tr> (table row), wrap in <td>; if <div> (card view), set directly
  if (el.tagName === 'TR') {
    el.innerHTML = '<td colspan="10">' + content + '</td>';
  } else {
    el.innerHTML = content;
  }
}

function renderVehicleCard(v, idx) {
  var a = v.assigned;
  var mainHtml;

  if (a) {
    mainHtml =
      '<div class="vc-vehicle">' +
        '<div class="vc-vin">' + escHtml(v.vin) + '</div>' +
        '<div class="vc-source">' + metaRow(['From ' + escHtml(v.source), escHtml(v.source_city) + ', ' + escHtml(v.source_state)]) + '</div>' +
        '<div class="vc-residual">Residual: $' + fmt(v.residual) + '</div>' +
      '</div>' +
      '<div class="vc-arrow">→</div>' +
      '<div class="vc-dealer">' +
        '<div class="vc-dealer-name">' + escHtml(a.dealer_name) + '</div>' +
        '<div class="vc-dealer-meta">' + metaRow([escHtml(a.dealer_code), escHtml(a.state), a.distance + ' mi']) + '</div>' +
      '</div>' +
      '<div class="vc-metrics">' +
        '<div class="vc-metric"><div class="vc-metric-label">Utilization</div><div class="vc-metric-value">' + (a.utilization != null ? a.utilization.toFixed(1) + '%' : '—') + '</div></div>' +
        '<div class="vc-metric"><div class="vc-metric-label">Property tax</div><div class="vc-metric-value">$' + fmt(a.prop_tax || 0) + '</div></div>' +
        '<div class="vc-metric"><div class="vc-metric-label">Algorithm pick</div><div class="vc-metric-value ' + (a.rank === 1 ? 'positive' : '') + '" title="Raw score (internal): ' + fmtSigned(a.alloc_score) + '">Rank ' + a.rank + '</div></div>' +
      '</div>';
  } else {
    mainHtml =
      '<div class="vc-vehicle">' +
        '<div class="vc-vin">' + escHtml(v.vin) + '</div>' +
        '<div class="vc-source">' + metaRow(['From ' + escHtml(v.source), escHtml(v.source_city) + ', ' + escHtml(v.source_state)]) + '</div>' +
        '<div class="vc-residual">Residual: $' + fmt(v.residual) + '</div>' +
      '</div>' +
      '<div class="vc-arrow">✗</div>' +
      '<div class="vc-dealer">' +
        '<div class="vc-dealer-name" style="color:var(--negative)">Unassigned</div>' +
        '<div class="vc-dealer-meta">No feasible dealer with slots remaining</div>' +
      '</div>' +
      '<div class="vc-metrics"></div>';
  }

  var alts = v.alternatives || [];
  var altHtml = '';
  if (alts.length > 1) {
    altHtml =
      '<div class="vc-expand" onclick="toggleAlts(' + idx + ')">&#9654; ' + alts.length + ' candidate dealers</div>' +
      '<div class="vc-alternatives" id="alts-' + idx + '">' +
        renderAltsTable(alts, a, idx) +
      '</div>';
  }

  return '<div class="vehicle-card ' + (a ? '' : 'unassigned') + '">' +
    '<div class="vc-main">' + mainHtml + '</div>' +
    '<div class="vc-reasoning"><strong>Why:</strong> ' + escHtml(v.reasoning) + '</div>' +
    altHtml +
  '</div>';
}

function toggleAlts(idx) {
  var el = document.getElementById('alts-' + idx);
  if (!el) return;
  // Table row style (source group)
  if (el.tagName === 'TR') {
    var visible = el.style.display !== 'none';
    el.style.display = visible ? 'none' : 'table-row';
    return;
  }
  // Card style (single vehicle)
  el.classList.toggle('open');
  var toggle = el.previousElementSibling;
  if (toggle) {
    var n = (weeklyData && weeklyData.vehicles[idx]) ? weeklyData.vehicles[idx].alternatives.length : 0;
    toggle.textContent = (el.classList.contains('open') ? '▼ ' : '▶ ') + n + ' candidate dealers';
  }
}

// ── Algorithm Toggle (Additive ILP / Bucket ILP / Greedy) ──────────
//
// Three buttons map to three algorithms. Internally the state is still
// stored as (weeklyMethod, scoringMode) — Greedy is mode-agnostic so its
// `scoringMode` carries whatever the last ILP run used. Picking Additive
// ILP / Bucket ILP stages the NEXT allocation; picking Greedy re-renders
// the cached batch using the already-fetched greedy data (no re-run).

function setAlgorithm(alg) {
  if (alg !== 'additive' && alg !== 'bucket' && alg !== 'greedy') return;
  if (alg === 'greedy') {
    weeklyMethod = 'greedy';
  } else {
    weeklyMethod = 'ilp';
    scoringMode = alg;
  }
  // Pure pointer swap into the pre-computed cache — Allocate Selected
  // already fired all three algorithms in parallel, so this is instant.
  weeklyData = (weeklyMethod === 'greedy') ? weeklyGreedyData
              : (scoringMode === 'bucket') ? weeklyBucketData
              : weeklyAdditiveData;

  updateAlgorithmToggle();
  updateAlgoStagingHint();

  if (!weeklyData) {
    // No batch loaded yet — toggle just stages the active mode for the
    // next Allocate Selected click. Banner / Delta have nothing to render.
    renderAlgoComparison();
    return;
  }

  // Comparison panel's ILP column tracks the current ILP-mode selection.
  _rebuildAllocCompare();
  renderWeekly();
  renderMethodDelta();
  renderAlgoComparison();

  // Batch Overview reads `weeklyData` for its 5 KPIs, the allocation
  // routes map, the rank-distribution histogram, and the per-dealer
  // load chart. Without this call the four panels stay frozen on
  // whichever algorithm was active the first time the user visited
  // the Batch Overview tab. renderDashboard() requires fleetData +
  // overview to be loaded; the overview tab's first-visit fetch
  // (loadOverviewDashboard) sets both, so the guard mirrors the
  // precondition inside renderDashboard().
  if (fleetData && overview) {
    renderDashboard();
  }
}

// Compose allocCompareData from the currently-displayed ILP cache + the
// always-on Greedy baseline. Called after the parallel allocate and after
// every Algorithm-toggle change so the Batch Overview panel re-paints
// with no API call.
function _rebuildAllocCompare() {
  if (!weeklyGreedyData) return;
  var ilpSource = (scoringMode === 'bucket') ? weeklyBucketData : weeklyAdditiveData;
  if (!ilpSource) return;
  var vehShim = function(v) {
    return {
      vin: v.vin, source: v.source, assigned_dealer: v.assigned_dealer,
      assigned: v.assigned ? {
        dealer_code: v.assigned.dealer_code, distance: v.assigned.distance,
        alloc_score: v.assigned.alloc_score, rank: v.assigned.rank,
        utilization: v.assigned.utilization, rented: v.assigned.rented,
        in_service: v.assigned.in_service,
      } : null,
    };
  };
  allocCompareData = {
    ilp: {
      rank1_pct: ilpSource.rank1_pct, avg_rank: ilpSource.avg_rank,
      total_distance: ilpSource.total_distance, n_assigned: ilpSource.n_assigned,
      scoring_mode: ilpSource.scoring_mode || scoringMode,
      params: ilpSource.params || {},
      vehicles: ilpSource.vehicles.map(vehShim),
    },
    greedy: {
      rank1_pct: weeklyGreedyData.rank1_pct, avg_rank: weeklyGreedyData.avg_rank,
      total_distance: weeklyGreedyData.total_distance, n_assigned: weeklyGreedyData.n_assigned,
      scoring_mode: 'additive',
      vehicles: weeklyGreedyData.vehicles.map(vehShim),
    },
  };
}

function updateAlgoStagingHint() {
  // Pre-batch hint: tell the user that Allocate runs all three algorithms
  // and the toggle is just a view switcher. The hint disappears as soon as
  // a batch is loaded — at that point the toggle truly is instant.
  var grp = document.getElementById('algorithm-toggle');
  if (!grp) return;
  var existing = document.getElementById('algo-staging-hint');
  var noBatch = !lastAllocatedVINs;
  if (noBatch) {
    if (!existing) {
      var hint = document.createElement('span');
      hint.id = 'algo-staging-hint';
      hint.style.cssText = 'margin-left: 10px; font-size: 0.7rem; color: var(--muted); font-style: italic;';
      grp.parentNode.appendChild(hint);
      existing = hint;
    }
    existing.textContent = 'Allocate Selected runs all three algorithms; toggle then switches the view instantly.';
  } else if (existing) {
    existing.remove();
  }
}

function updateAlgorithmToggle() {
  var grp = document.getElementById('algorithm-toggle');
  if (!grp) return;
  var btns = grp.querySelectorAll('.method-btn');
  var active = (weeklyMethod === 'greedy') ? 'greedy' : scoringMode;
  btns.forEach(function(b) {
    var label = b.textContent.trim().toLowerCase();
    // "additive ilp" → matches when active === 'additive'; same for bucket; "greedy" matches greedy.
    var matches = (active === 'additive' && label.indexOf('additive') === 0)
               || (active === 'bucket' && label.indexOf('bucket') === 0)
               || (active === 'greedy' && label === 'greedy');
    b.classList.toggle('active', matches);
  });
}

// Back-compat shims so existing callers in this file and tests keep working.
function setMethod(method) { setAlgorithm(method === 'greedy' ? 'greedy' : scoringMode); }
function setScoringMode(mode) { setAlgorithm(mode); }
function updateMethodToggle() { updateAlgorithmToggle(); }
function updateScoringModeToggle() { updateAlgorithmToggle(); }

function getActiveWeeklyData() {
  if (weeklyMethod === 'greedy' && weeklyGreedyData) return weeklyGreedyData;
  return weeklyData;
}

function renderMethodDelta() {
  // ILP-vs-Greedy delta badge removed 2026-05-22. The "avg IN_SERVICE at
  // dest" leg read 0.0 on the Greedy side because greedy responses don't
  // carry IN_SERVICE per assignment, making the comparison look broken.
  // The Batch Overview's full comparison panel covers the same axes
  // correctly with the dealer-master IN_SERVICE fallback, so the inline
  // badge is redundant. Function kept as a no-op so existing callers
  // don't need editing.
  var el = document.getElementById('method-delta');
  if (el) el.remove();
}

// ── Algorithm Comparison (Overview Dashboard) ────────

function renderAlgoComparison() {
  var banner = document.getElementById('algo-banner');
  var container = document.getElementById('algo-comparison');
  if (!banner || !container) return;
  var labelEl = document.getElementById('algo-method-label');
  var subEl = document.getElementById('algo-banner-sub');
  var modeDetailEl = document.getElementById('algo-mode-detail');

  if (!allocCompareData) {
    banner.className = 'algo-banner inactive';
    if (labelEl) labelEl.textContent = 'V2 Integer Linear Programming (ILP)';
    if (subEl) subEl.textContent = 'Run an allocation to see ILP vs Greedy comparison';
    if (modeDetailEl) { modeDetailEl.style.display = 'none'; modeDetailEl.innerHTML = ''; }
    container.innerHTML = '';
    return;
  }

  var ilp = allocCompareData.ilp;
  var greedy = allocCompareData.greedy;
  var dataMode = (ilp && ilp.scoring_mode) || (weeklyData && weeklyData.scoring_mode) || 'additive';
  var modeStaged = scoringMode && scoringMode !== dataMode;

  // Aggregate algorithm-independent destination-quality stats. The
  // demand-depth axis depends on scoring_mode: additive uses RENTED,
  // bucket uses IN_SERVICE (the field that drives the tier).
  function aggDestStats(data) {
    var n = 0, highUtil = 0, utilSum = 0, demandSum = 0;
    var vs = data.vehicles || [];
    for (var i = 0; i < vs.length; i++) {
      var a = vs[i].assigned;
      if (!a) continue;
      n++;
      var u = a.utilization != null ? a.utilization : 0;
      if (u >= 80) highUtil++;
      utilSum += u;
      var demandRaw = (dataMode === 'bucket')
        ? (a.in_service != null ? a.in_service : 0)
        : (a.rented != null ? a.rented : 0);
      demandSum += demandRaw;
    }
    return {
      n: n,
      highUtilCount: highUtil,
      highUtilPct: n ? (highUtil / n * 100) : 0,
      avgUtil: n ? (utilSum / n) : 0,
      avgDemand: n ? (demandSum / n) : 0,
    };
  }
  var iStats = aggDestStats(ilp);
  var gStats = aggDestStats(greedy);

  // Activate banner — lead with destination-util gain (algorithm-independent).
  banner.className = 'algo-banner';
  var utilDelta = +(iStats.avgUtil - gStats.avgUtil).toFixed(1);

  if (labelEl) {
    labelEl.textContent = (dataMode === 'bucket')
      ? 'ILP with bucket scoring'
      : 'ILP with additive scoring';
  }
  var subCopy = (dataMode === 'bucket')
    ? ''
    : '';
  if (subEl) {
    subEl.innerHTML = subCopy +
      '<span class="algo-delta-badge' + (utilDelta >= 0 ? '' : ' negative') + '">ILP ' +
      (utilDelta >= 0 ? '+' : '') + utilDelta + 'pp destination utilization</span>';
  }

  // Surface the active-mode params plus a staged-toggle hint if user is mid-switch.
  if (modeDetailEl) {
    var p = (ilp && ilp.params) || {};
    var lines = [];
    if (dataMode === 'bucket') {
      var mults = p.bucket_mults || [];
      lines.push(metaRow(['Active parameters', 'bucket_mults [' + mults.join(', ') + ']',
                 'signal ' + (p.signal_field || 'IN_SERVICE'),
                 'w_dist ' + (p.w_dist != null ? p.w_dist : '—'),
                 'w_tax ' + (p.w_tax != null ? p.w_tax : '—')]));
    } else {
      lines.push(metaRow(['Active parameters', 'w_util ' + (p.w_util != null ? p.w_util : '—'),
                 'w_rented ' + (p.w_rented != null ? p.w_rented : '—'),
                 'w_dist ' + (p.w_dist != null ? p.w_dist : '—'),
                 'w_tax ' + (p.w_tax != null ? p.w_tax : '—')]));
    }
    if (modeStaged) {
      lines.push('Next allocation uses <strong>' + scoringMode + '</strong> scoring.');
    }
    modeDetailEl.innerHTML = lines.join('<br>');
    modeDetailEl.style.display = 'block';
  }

  // Side-by-side KPIs — destination dealer properties, NOT algorithm rank
  var fmtMi = function(x) { return fmt(x) + ' mi'; };
  var fmtPct = function(x) { return (x != null ? x.toFixed(1) + '%' : '—'); };
  var fmtFrac = function(x, n) { return x + ' / ' + n + ' (' + Math.round(x / n * 100) + '%)'; };
  var fmtNum = function(x) { return x != null ? x.toFixed(1) : '—'; };
  var demandLabel = (dataMode === 'bucket')
    ? 'Avg dealer size (IN_SERVICE)'
    : 'Avg demand depth (RENTED)';
  var metrics = [
    { label: 'Assigned',
      g: greedy.n_assigned, v: ilp.n_assigned,
      fmt: function(x){ return x; } },
    { label: 'At ≥80% util dealer',
      g: gStats.highUtilCount, v: iStats.highUtilCount,
      fmt: function(x){ return fmtFrac(x, iStats.n); } },
    { label: 'Avg destination util',
      g: gStats.avgUtil, v: iStats.avgUtil,
      fmt: function(x){ return fmtPct(x); } },
    { label: demandLabel,
      g: gStats.avgDemand, v: iStats.avgDemand,
      fmt: function(x){ return fmtNum(x) + ' cars'; } },
    { label: 'Total distance',
      g: greedy.total_distance, v: ilp.total_distance,
      fmt: fmtMi },
  ];

  var html = '<div class="algo-columns">';

  // Greedy column
  html += '<div class="algo-column"><div class="algo-column-header greedy">Greedy Baseline (Distance Only)</div>';
  for (var i = 0; i < metrics.length; i++) {
    var m = metrics[i];
    html += '<div class="algo-metric"><span class="algo-metric-label">' + m.label + '</span><span class="algo-metric-value">' + m.fmt(m.g) + '</span></div>';
  }
  html += '</div>';

  // ILP column
  html += '<div class="algo-column"><div class="algo-column-header ilp">V2 ILP (Multi-Factor Optimization)</div>';
  for (var i = 0; i < metrics.length; i++) {
    var m = metrics[i];
    var vVal = m.fmt(m.v);
    var diff = m.v - m.g;
    var diffStr = '';
    if (diff !== 0 && m.label !== 'Assigned') {
      // Positive delta = good when: higher is better (util, util%, rented).
      // Positive delta = bad when:  higher is worse (distance).
      var higherBetter = (m.label === 'At ≥80% util dealer' ||
                          m.label === 'Avg destination util' ||
                          m.label === demandLabel);
      var cls = higherBetter ? (diff > 0 ? 'positive' : 'negative')
                             : (diff < 0 ? 'positive' : 'negative');
      diffStr = ' <span class="algo-delta-badge ' + cls + '">' + (diff > 0 ? '+' : '') + m.fmt(diff) + '</span>';
    }
    html += '<div class="algo-metric"><span class="algo-metric-label">' + m.label + '</span><span class="algo-metric-value">' + vVal + diffStr + '</span></div>';
  }
  html += '</div></div>';

  // Diff table toggle
  var ilpVins = ilp.vehicles || [];
  var greedyVins = greedy.vehicles || [];
  var greedyMap = {};
  for (var gi = 0; gi < greedyVins.length; gi++) {
    greedyMap[greedyVins[gi].vin] = greedyVins[gi];
  }

  var diffs = [];
  for (var vi = 0; vi < ilpVins.length; vi++) {
    var iv = ilpVins[vi];
    var gv = greedyMap[iv.vin];
    if (!gv) continue;
    var iDealer = iv.assigned_dealer || '—';
    var gDealer = gv.assigned_dealer || '—';
    if (iDealer !== gDealer) {
      diffs.push({
        vin: iv.vin, source: iv.source,
        gDealer: gDealer, gDist: gv.assigned ? gv.assigned.distance : '—', gRank: gv.assigned ? gv.assigned.rank : null,
        iDealer: iDealer, iDist: iv.assigned ? iv.assigned.distance : '—', iRank: iv.assigned ? iv.assigned.rank : null,
      });
    }
  }

  if (diffs.length > 0) {
    html += '<button class="algo-diff-toggle" onclick="toggleAlgoDiff()">' + diffs.length + ' vehicles differ — Show Details</button>';
    html += '<div id="algo-diff-table" style="display:none">';
    html += '<table class="data-table" style="font-size:0.72rem"><thead><tr>';
    html += '<th>VIN</th><th>Source</th><th>Greedy →</th><th>Dist</th><th>Rank</th><th>ILP →</th><th>Dist</th><th>Rank</th><th>ILP Gain</th>';
    html += '</tr></thead><tbody>';
    for (var di = 0; di < diffs.length; di++) {
      var d = diffs[di];
      // Gain = how many rank slots better ILP is than greedy (positive = ILP picked higher-ranked dealer)
      var gain = (d.gRank || 99) - (d.iRank || 99);
      var cls = gain > 0 ? 'positive' : (gain < 0 ? 'negative' : '');
      html += '<tr><td>' + d.vin.slice(-6) + '</td><td>' + d.source + '</td>';
      html += '<td>' + d.gDealer + '</td><td>' + d.gDist + ' mi</td><td>#' + (d.gRank == null ? '—' : d.gRank) + '</td>';
      html += '<td style="color:var(--accent)">' + d.iDealer + '</td><td>' + d.iDist + ' mi</td><td>#' + (d.iRank == null ? '—' : d.iRank) + '</td>';
      html += '<td class="' + cls + '">' + (gain > 0 ? '+' : '') + gain + ' ranks</td></tr>';
    }
    html += '</tbody></table></div>';
  } else {
    html += '<div style="font-size:0.75rem;color:var(--muted);margin-top:8px">All vehicles assigned to the same dealers by both methods.</div>';
  }

  container.innerHTML = html;
}

function toggleAlgoDiff() {
  var el = document.getElementById('algo-diff-table');
  if (el) el.style.display = el.style.display === 'none' ? 'block' : 'none';
}

// ── Overview Dashboard ───────────────────────────────

async function loadOverviewDashboard() {
  try {
    overview = await api('/api/overview');
    await loadFleet();
    renderDashboard();
  } catch (e) {
    console.error('Dashboard load failed:', e);
  }
}

function renderDashboard() {
  if (!fleetData || !overview) return;

  // Update allocation section label
  var label = document.getElementById('alloc-section-label');
  if (label) {
    if (weeklyData && weeklyData.n_assigned > 0) {
      // Destination-dealer stats (algorithm-independent)
      var uSum = 0, rSum = 0, n = 0;
      for (var vi = 0; vi < weeklyData.vehicles.length; vi++) {
        var a = weeklyData.vehicles[vi].assigned;
        if (!a) continue;
        uSum += (a.utilization != null ? a.utilization : 0);
        rSum += (a.rented != null ? a.rented : 0);
        n++;
      }
      var avgU = n ? (uSum / n).toFixed(1) : '—';
      var avgR = n ? (rSum / n).toFixed(1) : '—';
      label.innerHTML = metaRow(['Current allocation', weeklyData.n_assigned + ' vehicles',
        'Avg destination util ' + avgU + '%', 'Avg demand depth ' + avgR + ' cars']);
    } else {
      label.textContent = 'No allocation yet';
    }
  }

  // v2: Batch Overview shows ONLY per-batch content.
  // Fleet-wide vizzes (status donut / pipeline / dealer scatter / dealer heatmap)
  // moved to Home tab. Old renderStatusDonut/renderPipeline/etc. are now dead
  // code; renderHome() uses CSS/SVG-based replacements.
  renderBatchOverviewKPIs();
  renderAlgoComparison();
  renderMap();
  renderRankDist();
  renderDealerLoad();
}

// ── Dashboard KPIs ───────────────────────────────────

// Aggregate dealer-property metrics over the placed fleet
// (vehicles with STATUS Transporting or Delivered). These are the
// "what did the algorithm actually do for the fleet" numbers — they're
// independent of algorithm ranking (only look at destination dealer
// properties: UTIL_RATE and RENTED).
function computeAlgorithmOutcomeKPIs() {
  var result = { placed_count: 0, high_util_count: 0, high_util_pct: 0, avg_rented: 0 };
  if (!fleetData || !fleetData.vehicles || !overview || !overview.dealers) return result;

  // Build quick dealer lookup: code -> { UTIL_RATE, RENTED }
  var dealerMap = {};
  for (var i = 0; i < overview.dealers.length; i++) {
    var dl = overview.dealers[i];
    dealerMap[dl.DEALER_CODE] = dl;
  }

  var rentedSum = 0, placedN = 0, highUtilN = 0;
  for (var v_i = 0; v_i < fleetData.vehicles.length; v_i++) {
    var v = fleetData.vehicles[v_i];
    if (v.status !== 'Transporting' && v.status !== 'Delivered') continue;
    if (!v.assigned_dealer) continue;
    var d = dealerMap[v.assigned_dealer];
    if (!d) continue;
    placedN++;
    if ((d.UTIL_RATE || 0) * 100 >= 80) highUtilN++;
    rentedSum += d.RENTED || 0;
  }
  result.placed_count = placedN;
  result.high_util_count = highUtilN;
  result.high_util_pct = placedN ? (highUtilN / placedN * 100) : 0;
  result.avg_rented = placedN ? rentedSum / placedN : 0;
  return result;
}

function renderOverviewKPIs() {
  // Algorithm-outcome KPIs (not lifecycle). Compute by joining the
  // placed vehicles (Transporting or Delivered) with their assigned
  // dealer's properties from overview.dealers.
  var placedStats = computeAlgorithmOutcomeKPIs();

  var cards = [
    {
      label: 'Fleet placed',
      value: placedStats.placed_count,
      delta: placedStats.placed_count > 0
        ? placedStats.placed_count + ' cars allocated this session'
        : 'No cars allocated yet',
      cls: placedStats.placed_count > 0 ? 'positive' : 'neutral',
    },
    {
      label: 'At high-utilization dealers <span class="info-icon" onclick="showInfoPopover(event, \'high_util_kpi\')">ⓘ</span>',
      value: placedStats.placed_count > 0
        ? placedStats.high_util_count + ' / ' + placedStats.placed_count +
          ' (' + Math.round(placedStats.high_util_pct) + '%)'
        : '—',
      delta: 'Placed at dealer with UTIL ≥ 80%',
      cls: placedStats.high_util_pct >= 80 ? 'positive'
         : placedStats.high_util_pct >= 50 ? 'neutral' : 'negative',
    },
    {
      label: 'Avg demand depth <span class="info-icon" onclick="showInfoPopover(event, \'demand_depth\')">ⓘ</span>',
      value: placedStats.placed_count > 0
        ? placedStats.avg_rented.toFixed(1)
        : '—',
      delta: placedStats.placed_count > 0
        ? 'Avg cars already rented at each destination'
        : 'Placed fleet has no destinations yet',
      cls: placedStats.placed_count > 0 ? 'positive' : 'neutral',
    },
  ];
  var hasAlloc = weeklyData && weeklyData.n_assigned > 0;
  if (hasAlloc) {
    cards.push({ label: 'Last allocation distance', value: fmt(weeklyData.total_distance) + ' mi', delta: weeklyData.n_assigned + ' vehicles moved', cls: 'positive' });
  } else {
    cards.push({ label: 'Last Allocation', value: '—', delta: 'No allocation run yet', cls: 'neutral' });
  }

  var html = '';
  for (var i = 0; i < cards.length; i++) {
    var cd = cards[i];
    html += '<div class="kpi-card"><div class="kpi-label">' + cd.label + '</div><div class="kpi-value">' + cd.value + '</div><div class="kpi-delta ' + cd.cls + '">' + cd.delta + '</div></div>';
  }
  document.getElementById('kpi-grid').innerHTML = html;
}

// ── Fleet Pipeline (stacked bar by week) ─────────────

function renderPipeline() {
  if (!fleetData) return;
  var vehicles = fleetData.vehicles;
  var weeks = fleetData.weeks || [];
  var statuses = ['Incoming', 'Delivered', 'Grounded', 'Transporting'];
  var colors = { Incoming: '#ff6b6b', Grounded: '#ffc107', Transporting: '#00d4ff', Delivered: '#00e676' };

  // Count per week per status
  var weekCounts = {};
  for (var w = 0; w < weeks.length; w++) weekCounts[weeks[w]] = { Incoming: 0, Grounded: 0, Transporting: 0, Delivered: 0 };
  for (var i = 0; i < vehicles.length; i++) {
    var v = vehicles[i];
    if (weekCounts[v.week]) weekCounts[v.week][v.status]++;
  }

  var weekLabels = weeks.map(function(w) { var p = w.split('-'); return parseInt(p[1]) + '/' + parseInt(p[2]); });
  var traces = [];
  for (var s = 0; s < statuses.length; s++) {
    var st = statuses[s];
    traces.push({
      type: 'bar', name: st,
      x: weekLabels,
      y: weeks.map(function(w) { return weekCounts[w][st]; }),
      marker: { color: colors[st] },
    });
  }

  Plotly.newPlot('chart-pipeline', traces, {
    ...PLOT_LAYOUT, barmode: 'stack', height: 220,
    margin: { l: 48, r: 16, t: 8, b: 56 },
    legend: { orientation: 'h', y: 1.2, x: 0.5, xanchor: 'center', font: { size: 10 } },
    xaxis: { ...PLOT_LAYOUT.xaxis, title: 'Week', tickangle: -45, tickfont: { size: 9 } },
    yaxis: { ...PLOT_LAYOUT.yaxis, title: 'Vehicles' },
  }, PLOT_CONFIG);
}

// ── Fleet Status Donut ───────────────────────────────

function renderStatusDonut() {
  if (!fleetData) return;
  var c = fleetData.counts;
  var labels = ['Incoming', 'Grounded', 'Transporting', 'Delivered'];
  var values = [c.incoming || 0, c.grounded || 0, c.transporting || 0, c.delivered || 0];
  var colors = ['#ff6b6b', '#ffc107', '#00d4ff', '#00e676'];

  Plotly.newPlot('chart-status-donut', [{
    type: 'pie', labels: labels, values: values,
    hole: 0.55, marker: { colors: colors, line: { color: '#06080c', width: 2 } },
    textinfo: 'label+value', textfont: { size: 10, color: '#e0e4ea' },
    textposition: 'inside',
    hovertemplate: '%{label}: %{value} vehicles (%{percent})<extra></extra>',
  }], {
    ...PLOT_LAYOUT, height: 220, showlegend: false,
    margin: { l: 20, r: 20, t: 20, b: 20 },
    annotations: [{ text: '<b>' + (c.total || 0) + '</b><br>total', showarrow: false, font: { size: 14, color: '#e0e4ea' }, x: 0.5, y: 0.5 }],
  }, PLOT_CONFIG);
}

// ── Dealer Performance Scatter ───────────────────────

function renderDealerScatter() {
  if (!overview) return;
  var dealers = overview.dealers || [];
  if (!dealers.length) return;
  // x = UTIL_RATE, y = RENTED — the two positive inputs to alloc_score
  // (w_util × UTIL + w_rented × RENTED). IN_SERVICE sizes the marker for
  // operational context only; it is NOT a scoring input.
  Plotly.newPlot('chart-scatter', [{
    type: 'scatter', mode: 'markers',
    x: dealers.map(function(dl) { return (dl.UTIL_RATE * 100).toFixed(1); }),
    y: dealers.map(function(dl) { return dl.RENTED || 0; }),
    marker: {
      size: dealers.map(function(dl) { return Math.max(10, Math.sqrt(Math.max(1, dl.IN_SERVICE || 1)) * 3 + 8); }),
      color: dealers.map(function(dl) { return dl.UTIL_RATE; }),
      colorscale: [[0, C.neg], [0.5, '#ffc107'], [1, C.pos]],
      colorbar: { title: { text: 'Util', font: { size: 10 } }, thickness: 10, len: 0.6, x: 1.0, xpad: 0 },
      line: { width: 1, color: 'rgba(255,255,255,0.15)' },
    },
    text: dealers.map(function(dl) {
      return '<b>' + dl.DEALER_NAME + '</b><br>' + dl.DEALER_CODE + ', ' + dl.STATE +
        '<br>Utilization: ' + (dl.UTIL_RATE * 100).toFixed(0) + '%' +
        '<br>Rented: ' + (dl.RENTED != null ? dl.RENTED : '—') +
        '<br>In service: ' + (dl.IN_SERVICE != null ? dl.IN_SERVICE : '—');
    }),
    hoverinfo: 'text',
  }], {
    ...PLOT_LAYOUT, height: 460,
    xaxis: { ...PLOT_LAYOUT.xaxis, title: 'Utilization %', zeroline: false },
    yaxis: { ...PLOT_LAYOUT.yaxis, title: 'Rented Cars (absolute)', zeroline: false },
    margin: { l: 56, r: 64, t: 20, b: 44 },
  }, { ...PLOT_CONFIG, responsive: true });

  // Re-fit after layout settles
  setTimeout(function() {
    var el = document.getElementById('chart-scatter');
    if (el && el.clientHeight > 0) {
      Plotly.relayout('chart-scatter', { height: el.clientHeight });
    }
  }, 100);
}

// ── Dealer Rankings Table ────────────────────────────

function renderDealerRankings() {
  if (!overview) return;
  // Score formula switches with the active scoring_mode:
  // * additive (2026-04-21): w_util × UTIL + w_rented × RENTED. Weights from
  //   scoringDefaults (= engine.DEFAULT_*).
  // * bucket (2026-05-21 spec): bucket_mult(tier_of_dealer) on IN_SERVICE.
  //   Tier cuts are 25% slices of the network's max IN_SERVICE.
  var scoreOf;
  if (scoringMode === 'bucket') {
    var inServiceVals = overview.dealers.map(function(d) { return d.IN_SERVICE || 0; });
    var maxInService = Math.max.apply(null, inServiceVals.length ? inServiceVals : [1]);
    // Working-assumption multipliers — visual only, mirrors the production
    // calibrated vector's shape. The actual scoring vector is sent by the
    // server on /api/weekly; this panel is a fleet-wide preview.
    var BUCKET_MULTS = [3.7203, 2.8848, 0.7957, 0.6498];
    var tierOf = function(v) {
      if (maxInService <= 0) return 3;
      if (v >= maxInService) return 0;
      if (v <= 0) return 3;
      var ratio = v / maxInService;
      for (var k = 1; k <= 3; k++) {
        var cut = (4 - k) / 4;
        if (ratio > cut) return k - 1;
      }
      return 3;
    };
    scoreOf = function(dl) {
      return BUCKET_MULTS[tierOf(dl.IN_SERVICE || 0)];
    };
  } else {
    var W_UTIL = scoringDefaults.w_util;
    var W_RENTED = scoringDefaults.w_rented;
    scoreOf = function(dl) {
      var u = dl.UTIL_RATE || 0;
      var r = Math.max(0, dl.RENTED || 0);
      return W_UTIL * u + W_RENTED * r;
    };
  }
  var dealers = overview.dealers.slice().sort(function(a, b) { return scoreOf(b) - scoreOf(a); });
  if (!dealers.length) return;

  var maxScore = scoreOf(dealers[0]);
  var rows = '';
  for (var i = 0; i < dealers.length; i++) {
    var dl = dealers[i];
    var score = scoreOf(dl);
    var pct = maxScore > 0 ? Math.round(score / maxScore * 100) : 0;
    var barColor = dl.UTIL_RATE >= 0.7 ? 'var(--positive)' : dl.UTIL_RATE >= 0.4 ? '#ffc107' : 'var(--negative)';
    rows += '<tr>' +
      '<td style="font-weight:500">' + escHtml(dl.DEALER_CODE) + '</td>' +
      '<td>' + escHtml(dl.DEALER_NAME) + '</td>' +
      '<td>' + escHtml(dl.STATE) + '</td>' +
      '<td>' + (dl.UTIL_RATE * 100).toFixed(0) + '%</td>' +
      '<td>' + (dl.RENTED != null ? dl.RENTED : '—') + '</td>' +
      '<td>' + (dl.IN_SERVICE != null ? dl.IN_SERVICE : '—') + '</td>' +
      '<td>' + score.toFixed(3) + '</td>' +
      '<td>' + ((dl.IN_SERVICE != null && dl.RENTED != null) ? (dl.IN_SERVICE - dl.RENTED) : '—') + '</td>' +
      '<td style="width:120px"><div class="rank-bar-bg"><div class="rank-bar" style="width:' + pct + '%;background:' + barColor + '"></div></div></td>' +
    '</tr>';
  }
  document.getElementById('dealer-table').innerHTML =
    '<table class="data-table"><thead><tr><th>Code</th><th>Name</th><th>State</th><th>Util</th><th>Rented</th><th>In Service</th><th>Score</th><th>Capacity</th><th>Rank</th></tr></thead><tbody>' + rows + '</tbody></table>';
}

// ── Allocation Map ───────────────────────────────────

function renderMap() {
  if (!overview) return;
  var container = document.getElementById('map-container');
  container.innerHTML = '';
  var dealers = overview.dealers || [];
  var sources = overview.sources || [];

  // If we have allocation data, show arcs
  var layers = [];

  // Dealer dots (blue) — drawn first, slightly larger, with white stroke so
  // they remain visible as a ring around any overlapping source dot.
  // radiusMinPixels / radiusMaxPixels clamp apparent size across zoom levels
  // so the red-inside-blue visual stays consistent whether zoomed in or out.
  layers.push(new deck.ScatterplotLayer({ id: 'dealers', data: dealers,
    getPosition: function(r) { return [r.LONGITUDE, r.LATITUDE]; },
    getRadius: 22000,
    radiusMinPixels: 8, radiusMaxPixels: 24,
    getFillColor: [0, 212, 255, 210],
    stroked: true, getLineColor: [255, 255, 255, 230],
    getLineWidth: 2, lineWidthMinPixels: 1.5,
    pickable: true }));

  // Source dots (red) — drawn on top, smaller, so they sit centered inside
  // the blue dealer ring when coords overlap (see Milwaukee: D025 ↔ FD14,
  // plus ~22 near-overlaps within ~10km).
  layers.push(new deck.ScatterplotLayer({ id: 'sources', data: sources,
    getPosition: function(r) { return [r.SOURCE_LON, r.SOURCE_LAT]; },
    getRadius: 14000,
    radiusMinPixels: 5, radiusMaxPixels: 16,
    getFillColor: [255, 107, 107, 220],
    stroked: true, getLineColor: [255, 255, 255, 230],
    getLineWidth: 1.5, lineWidthMinPixels: 1,
    pickable: true }));

  // If latest allocation exists, show arcs
  if (weeklyData && weeklyData.vehicles) {
    var arcData = [];
    for (var i = 0; i < weeklyData.vehicles.length; i++) {
      var v = weeklyData.vehicles[i];
      if (v.assigned && v.source_lat && v.source_lon) {
        arcData.push({
          slat: v.source_lat, slon: v.source_lon,
          dlat: v.assigned.lat, dlon: v.assigned.lon,
          rank: v.assigned.rank, vin: v.vin,
          dealer: v.assigned.dealer_name,
        });
      }
    }
    if (arcData.length > 0) {
      layers.push(new deck.ArcLayer({ id: 'alloc-arcs', data: arcData,
        getSourcePosition: function(r) { return [r.slon, r.slat]; },
        getTargetPosition: function(r) { return [r.dlon, r.dlat]; },
        getSourceColor: [255, 107, 107, 180],
        getTargetColor: [0, 212, 255, 220],
        getWidth: 2, widthMinPixels: 1, widthMaxPixels: 4, pickable: true }));
    }
  }

  // Destroy previous instance to avoid stale sizing
  if (deckInstance) {
    deckInstance.finalize();
    deckInstance = null;
  }
  container.innerHTML = '';

  // Defer creation so container has its final layout size
  setTimeout(function() {
    deckInstance = new deck.DeckGL({
      container: container,
      mapStyle: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
      initialViewState: { longitude: -96, latitude: 38.5, zoom: 3.5, pitch: 0 },
      controller: true,
      layers: layers,
      getTooltip: function(info) {
        var obj = info.object;
        if (!obj) return null;
        if (obj.DEALER_NAME) {
          var rented = (obj.RENTED != null) ? obj.RENTED : '—';
          var inSvc = (obj.IN_SERVICE != null) ? obj.IN_SERVICE : '—';
          var cap = (obj.IN_SERVICE != null && obj.RENTED != null) ? (inSvc - rented) : '—';
          return { text:
            obj.DEALER_NAME + ' (' + obj.STATE + ')' +
            '\nUtilization: ' + (obj.UTIL_RATE * 100).toFixed(0) + '%' +
            '\nRented: ' + rented +
            '\nIn Service: ' + inSvc +
            '\nCapacity: ' + cap
          };
        }
        if (obj.SOURCE) return { text: 'Source: ' + obj.SOURCE + '\n' + obj.CITY + ', ' + obj.STATE };
        if (obj.vin) return { text: obj.vin + '\n→ ' + obj.dealer + '\nAlgorithm pick: Rank ' + obj.rank };
        return null;
      },
    });
  }, 50);
}

function toggleMapFullscreen() {
  var panel = document.getElementById('map-panel');
  panel.classList.toggle('fullscreen');
  setTimeout(function() {
    if (deckInstance) {
      var container = document.getElementById('map-container');
      deckInstance.setProps({ width: container.clientWidth, height: container.clientHeight });
    }
  }, 50);
}


// ── Chat ─────────────────────────────────────────────

// (sessionId + _newClientId defined at the top — used for both
// /api/* header and Claude CLI session key. "Clear chat" regenerates
// it to start a fresh session AND drop server-side allocation state.)
var chatMsgCounter = 0;
// Mirrors the server's session.chat_count budget. The initial authoritative
// value comes from /api/chat_status; subsequent /api/chat responses refresh it
// through X-Chat-Remaining / X-Chat-Limit headers.
var chatQuotaRemaining = 0;
var chatQuotaLimit = 0;

function updateChatQuotaBanner(remainingHeader, limitHeader) {
  if (remainingHeader != null) chatQuotaRemaining = parseInt(remainingHeader, 10) || 0;
  if (limitHeader != null) {
    var parsedLimit = parseInt(limitHeader, 10);
    if (!Number.isNaN(parsedLimit)) chatQuotaLimit = parsedLimit;
  }
  var banner = document.getElementById('chat-quota-banner');
  var remEl = document.getElementById('quota-remaining');
  var nounEl = document.getElementById('quota-noun');
  var textEl = banner ? banner.querySelector('.quota-text') : null;
  if (!banner || !remEl) return;
  if (chatQuotaLimit <= 0) {
    if (textEl) textEl.textContent = 'Agent chat is disabled for this deployment.';
  } else if (textEl) {
    textEl.innerHTML =
      'Demo chat limit — <strong id="quota-remaining">' + chatQuotaRemaining + '</strong> ' +
      '<span id="quota-noun">' + (chatQuotaRemaining === 1 ? 'message' : 'messages') + '</span> ' +
      'remaining this session. Refresh page to reset.';
  } else {
    remEl.textContent = chatQuotaRemaining;
    if (nounEl) nounEl.textContent = chatQuotaRemaining === 1 ? 'message' : 'messages';
  }
  if (chatQuotaRemaining <= 0) {
    banner.classList.add('exhausted');
    var input = document.getElementById('chat-input');
    var send  = document.getElementById('chat-send');
    if (input) {
      input.disabled = true;
      input.placeholder = chatQuotaLimit <= 0
        ? 'Agent chat disabled for this deployment'
        : 'Quota exhausted — refresh to start a new session';
    }
    if (send)  send.disabled = true;
  } else {
    banner.classList.remove('exhausted');
    var input2 = document.getElementById('chat-input');
    var send2 = document.getElementById('chat-send');
    if (input2) {
      input2.disabled = false;
      input2.placeholder = 'Ask the agent anything...';
    }
    if (send2) send2.disabled = false;
  }
}

async function refreshChatStatus() {
  var status = await api('/api/chat_status');
  if (status.demo) {
    // Gateway owns the quota in demo mode. Show its rolling-24h counters when
    // present; never lock the input on this app's own (absent) cap.
    chatQuotaLimit = 1;
    chatQuotaRemaining = 1;
    var banner = document.getElementById('chat-quota-banner');
    var textEl = banner ? banner.querySelector('.quota-text') : null;
    if (banner) banner.classList.remove('exhausted');
    if (textEl) {
      var lim = status.quota_limit;
      var rem = status.quota_remaining;
      if (rem != null && lim != null && lim !== 'unlimited') {
        textEl.textContent = 'Demo quota — ' + rem + ' of ' + lim + ' AI actions remaining (rolling 24h).';
      } else if (lim === 'unlimited') {
        textEl.textContent = 'Owner access — unlimited AI actions.';
      } else {
        textEl.textContent = 'Ask the agent anything.';
      }
    }
    var input = document.getElementById('chat-input');
    var send = document.getElementById('chat-send');
    if (input) { input.disabled = false; input.placeholder = 'Ask the agent anything...'; }
    if (send) send.disabled = false;
    return;
  }
  updateChatQuotaBanner(status.remaining, status.limit);
}

function askSample(el) {
  document.getElementById('chat-input').value = el.textContent;
  sendChat();
}

// ── Info icon popover ─────────────────────────────────
// Click on a ⓘ icon → show popover with explanation + "Ask AI" button.
// Ask AI → pre-fill chat input with the question and auto-send.
// Close on outside click or × button.
var INFO_TOPICS = {
  tax_saved_trend: {
    title: 'Annual tax saved vs Greedy',
    body: 'ILP avoids high-tax dealers Greedy would route to. Real annual property-tax dollars per batch.',
    question: 'How is the annual tax saved vs Greedy calculated?',
  },
  dealer_concentration: {
    title: 'Dealer concentration',
    body: 'How spread out placements are. 0% = perfectly even across all dealers, 100% = all cars to one dealer. Lower = healthier distribution.',
    question: 'What does dealer concentration measure and why does lower mean healthier?',
  },
  rank_column: {
    title: 'How ranks work',
    body: 'For every vehicle the algorithm scores all feasible dealers and sorts them by score. Rank 1 is the algorithm\'s top pick. When a car shows Rank 2+ it means its Rank 1 dealer had no slots left, or the ILP traded this car\'s personal top pick for a better total-batch outcome.',
    question: 'How is the Rank in the Choice column calculated for each vehicle?',
  },
  high_util_kpi: {
    title: 'At ≥80% util dealer',
    body: 'Counts cars placed at destination dealers whose UTIL_RATE column is ≥80%. This is an algorithm-independent quality check — it just looks at the dealer\'s own utilization, not the algorithm\'s ranking. 80% is a common industry threshold for a "busy" fleet location.',
    question: 'Why is 80% the threshold for "high-util" dealer and why does it matter for the business?',
  },
  avg_dest_util: {
    title: 'Avg destination utilization',
    body: 'Averages the UTIL_RATE of each assigned car\'s destination dealer across the batch. Changes live when you override a car to a different dealer. Independent of how the algorithm ranked candidates.',
    question: 'How is average destination utilization computed and why does it update when I change an assignment?',
  },
  total_distance: {
    title: 'Total distance',
    body: 'Sums each unique (source → destination) route exactly once. A single carrier truck can haul multiple cars on the same route, so we count each route\'s miles once, not per vehicle. This is the actual trucking cost driver, not the sum of per-vehicle distances.',
    question: 'Is total distance counted per vehicle or per carrier trip? How is shared routing handled?',
  },
  ilp_vs_greedy: {
    title: 'ILP vs Greedy',
    body: 'Greedy sends each car to its nearest feasible dealer. ILP optimizes a combined score (utilization + demand + distance + tax) across the whole batch, sometimes accepting longer shipping to reach higher-demand destinations. Toggle to see what each method would do on the same cars.',
    question: 'What is the difference between the ILP and Greedy allocation methods and when does ILP help most?',
  },
  demand_depth: {
    title: 'Demand depth',
    body: 'The average "RENTED" count at destination dealers — how many cars are currently rented out at each place this batch\'s cars ended up. Higher demand depth means the algorithm sent cars to dealers where there is already demonstrated, active demand (client review 2026-04-17 criterion).',
    question: 'What does "demand depth" mean and how does the algorithm use the RENTED signal?',
  },
  algo_comparison: {
    title: 'Algorithm comparison',
    body: 'Same vehicle list, two different optimizers. Metrics shown are destination-dealer properties (utilization, RENTED or IN_SERVICE depending on scoring mode) and operating cost (distance, tax) — all algorithm-independent so the comparison is fair. Deltas tell you how much each method wins on each axis.',
    question: 'What metrics are used to compare ILP vs Greedy and why are they algorithm-independent?',
  },
  scoring_mode: {
    title: 'Scoring mode',
    body: 'Two util-side scoring strategies coexist. Additive (production default) scores each dealer as w_util × UTIL_RATE + w_rented × RENTED. Bucket (2026-05-21 spec, calibration) puts each dealer in one of four max-anchored 25% range tiers on IN_SERVICE and uses the tier multiplier as the util-side score. The toggle takes effect on the next allocation run; the cached batch is not rescored. Both modes share the same distance and tax penalty terms and the same ILP solver.',
    question: 'How does the bucket scoring mode differ from the additive mode and when should I switch?',
  },
  algorithm: {
    title: 'Algorithm',
    body: 'Three options. <strong>Additive ILP</strong> — the production default; the ILP optimizes a continuous score (w_util × UTIL + w_rented × RENTED − w_dist × dist − w_tax × tax). <strong>Bucket ILP</strong> — the same ILP solver but the util side collapses to a categorical tier on IN_SERVICE (max-anchored 25% slices, per the 2026-05-21 teammate spec); distance and tax break within-tier ties. <strong>Greedy</strong> — nearest-feasible-dealer per vehicle, distance only, ignores both scoring modes. Allocate Selected runs all three in parallel; the toggle then switches the view instantly without re-running anything.',
    question: 'What is the difference between Additive ILP, Bucket ILP, and Greedy, and when should I pick each?',
  },
};

function showInfoPopover(evt, topicKey) {
  evt.stopPropagation();
  evt.preventDefault();
  closeInfoPopover(); // close any open one
  var t = INFO_TOPICS[topicKey];
  if (!t) return;
  var icon = evt.currentTarget;
  var rect = icon.getBoundingClientRect();
  var pop = document.createElement('div');
  pop.className = 'info-popover';
  pop.id = 'info-popover-active';
  pop.innerHTML =
    '<button class="info-popover-close" onclick="closeInfoPopover()">×</button>' +
    '<div class="info-popover-title">' + t.title + '</div>' +
    '<div>' + t.body + '</div>' +
    '<button class="info-popover-ask" ' +
      'onclick="askAiFromInfo(\'' + topicKey + '\')">💬 Ask AI for details</button>';
  document.body.appendChild(pop);
  // Position below icon, anchored left; stay within viewport
  var top = rect.bottom + window.scrollY + 6;
  var left = rect.left + window.scrollX;
  var popRect = pop.getBoundingClientRect();
  if (left + popRect.width > window.innerWidth - 12) {
    left = window.innerWidth - popRect.width - 12;
  }
  pop.style.top = top + 'px';
  pop.style.left = left + 'px';
  // Outside click to close
  setTimeout(function() {
    document.addEventListener('click', closeInfoPopover, { once: true });
  }, 0);
}

function closeInfoPopover() {
  var pop = document.getElementById('info-popover-active');
  if (pop) pop.remove();
}

function askAiFromInfo(topicKey) {
  var t = INFO_TOPICS[topicKey];
  if (!t) return;
  closeInfoPopover();
  var inp = document.getElementById('chat-input');
  if (inp) {
    inp.value = t.question;
    inp.focus();
  }
  if (typeof sendChat === 'function') sendChat();
}

function buildChatContext() {
  var ctx = '';
  if (currentPage === 'fleet' && fleetData) {
    var fc = fleetData.counts;
    ctx += 'PAGE: Fleet Inventory\n';
    ctx += 'FLEET: ' + fc.total + ' total, ' + (fc.incoming||0) + ' incoming, ' + (fc.grounded||0) + ' grounded, ' + (fc.transporting||0) + ' transporting, ' + (fc.delivered||0) + ' delivered\n';
    if (selectedVINs.size > 0) ctx += 'SELECTED: ' + selectedVINs.size + ' vehicles for allocation\n';
  }
  if (currentPage === 'weekly' && weeklyData) {
    var w = weeklyData;
    ctx += 'PAGE: Weekly Allocation (' + w.batch_size + ' vehicles)\n';
    ctx += 'ASSIGNED: ' + w.n_assigned + '/' + w.batch_size + ', avg_rank=' + (w.avg_rank != null ? w.avg_rank : '-') + ', rank1_rate=' + (w.rank1_pct != null ? w.rank1_pct + '%' : '-') + '\n';
    var ctxMode = (w.scoring_mode === 'bucket') ? 'bucket' : 'additive';
    ctx += 'SCORING_MODE: ' + ctxMode + '\n';
    if (w.params) {
      // Business knobs lead; internal normalizers are for integrator audit only
      // and should not be quoted to the user by the agent. The params block's
      // shape depends on scoring_mode; agent must read the mode tag above
      // before reasoning about which weights are active.
      if (ctxMode === 'bucket') {
        var bm = w.params.bucket_mults || [];
        ctx += 'PARAMS: bucket_mults=[' + bm.join(', ') + ']' +
               ', signal_field=' + (w.params.signal_field || 'IN_SERVICE') +
               ', expected_stay_months=' + w.params.expected_stay_months + '\n';
      } else {
        ctx += 'PARAMS: w_util=' + (w.params.w_util != null ? w.params.w_util : scoringDefaults.w_util) +
               ', w_rented=' + (w.params.w_rented != null ? w.params.w_rented : scoringDefaults.w_rented) +
               ', expected_stay_months=' + w.params.expected_stay_months + '\n';
      }
    }
    ctx += 'VEHICLES (current user-modified state):\n';
    for (var i = 0; i < w.vehicles.length; i++) {
      var wv = w.vehicles[i];
      var wa = wv.assigned;
      if (wa) {
        var line = '  ' + wv.vin + ' (source:' + wv.source + ') -> ' + wa.dealer_code + ' ' + wa.dealer_name + ' (' + wa.state + '), rank=' + wa.rank + ', dist=' + wa.distance + 'mi, util_score=' + wa.utilization_score + ', prop_tax=$' + wa.prop_tax + ', raw_score=' + wa.alloc_score;
        var alts = wv.alternatives || [];
        if (wv.overridden) {
          // Only mark as user override when the user actually clicked through the dropdown.
          var algoPick = alts.length > 0 ? alts[0].dealer_code : 'unknown';
          line += ' [USER OVERRIDE — algorithm rank-1 was ' + algoPick + ']';
        } else if (wa.rank !== 1) {
          // ILP chose a non-rank-1 dealer because the rank-1 option was at capacity
          // or giving it to another vehicle produced a higher total score.
          line += ' [ILP chose rank ' + wa.rank + ' — rank-1 was ' + (alts[0] ? alts[0].dealer_code : '?') + ' but unavailable due to slot-limit / optimization trade-off]';
        }
        ctx += line + '\n';
      } else {
        ctx += '  ' + wv.vin + ' (source:' + wv.source + ') -> UNASSIGNED\n';
      }
      // Include constraint warnings visible on screen
      var cwarn = document.getElementById('cwarn-' + i);
      if (cwarn && cwarn.style.display !== 'none') {
        var warnText = cwarn.querySelector('.warn-text');
        if (warnText) ctx += '    ⚠ CONSTRAINT WARNING: ' + warnText.textContent + '\n';
      }
    }
  }
  if (currentPage === 'overview') {
    ctx += 'PAGE: Overview Dashboard\n';
    if (fleetData) {
      var fc = fleetData.counts;
      ctx += 'FLEET: ' + fc.total + ' total, ' + (fc.incoming||0) + ' incoming, ' + (fc.grounded||0) + ' grounded, ' + (fc.transporting||0) + ' transporting, ' + (fc.delivered||0) + ' delivered\n';
    }
    if (weeklyData) {
      ctx += 'LAST ALLOCATION: ' + weeklyData.n_assigned + ' assigned, avg_rank=' + (weeklyData.avg_rank != null ? weeklyData.avg_rank : '-') + ', rank1_rate=' + (weeklyData.rank1_pct != null ? weeklyData.rank1_pct + '%' : '-') + '\n';
    }
    if (overview) {
      ctx += 'DEALERS: ' + overview.total_dealers + ' total, total_slot_headroom=' + overview.total_capacity + '\n';
    }
  }
  return ctx;
}

async function sendChat() {
  var input = document.getElementById('chat-input');
  var msg = input.value.trim();
  if (!msg) return;
  input.value = '';

  var ctx = buildChatContext();

  // Clear previous agent focus highlights
  clearAgentFocus();

  var msgs = document.getElementById('chat-messages');
  if (chatMsgCounter === 0) {
    msgs.innerHTML = '';
  }

  addChatMessage('user', escHtml(msg));

  // Create the assistant bubble with initial status
  var bubbleId = addChatMessage('assistant', '<div class="agent-status" id="agent-status"><div class="spinner-sm"></div> <span class="agent-status-text">Connecting...</span></div>');
  document.getElementById('chat-send').disabled = true;

  var toolCallsAccum = [];
  var finalEvent = null;

  try {
    var res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Session-Id': sessionId },
      body: JSON.stringify({ message: msg, context: ctx, client_id: sessionId }),
    });

    // Reflect the server's current quota count regardless of outcome.
    updateChatQuotaBanner(res.headers.get('X-Chat-Remaining'),
                          res.headers.get('X-Chat-Limit'));

    if (res.status === 429) {
      // Quota exhausted — render a system message instead of the agent reply.
      var quotaErr = await res.json().catch(function() { return {}; });
      var quotaEl = document.getElementById(bubbleId);
      if (quotaEl) {
        quotaEl.querySelector('.chat-bubble').innerHTML =
          '<div style="color: var(--negative); font-size: 0.82rem; line-height: 1.5">' +
            '<strong>Quota exhausted.</strong><br>' +
            escHtml(quotaErr.message || "You've used your messages for this session. Refresh the page to start a new session.") +
          '</div>';
      }
      document.getElementById('chat-send').disabled = true;
      document.getElementById('chat-messages').scrollTop = 999999;
      return;
    }

    if (!res.ok) throw new Error('API ' + res.status);

    var reader = res.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';

    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;

      buffer += decoder.decode(chunk.value, { stream: true });

      // Parse SSE lines
      var lines = buffer.split('\n');
      buffer = lines.pop(); // keep incomplete line in buffer

      for (var li = 0; li < lines.length; li++) {
        var line = lines[li].trim();
        if (!line.startsWith('data: ')) continue;
        var payload = line.substring(6);
        if (payload === '[DONE]') continue;

        var event;
        try { event = JSON.parse(payload); } catch (e) { continue; }

        handleAgentEvent(event, bubbleId, toolCallsAccum);

        if (event.type === 'answer') {
          finalEvent = event;
        }
      }
    }

    // Process any remaining buffer
    if (buffer.trim().startsWith('data: ')) {
      var lastPayload = buffer.trim().substring(6);
      if (lastPayload !== '[DONE]') {
        try {
          var lastEvent = JSON.parse(lastPayload);
          handleAgentEvent(lastEvent, bubbleId, toolCallsAccum);
          if (lastEvent.type === 'answer') finalEvent = lastEvent;
        } catch (e) {}
      }
    }

    // Stop scanning and clear highlights when answer arrives
    clearAgentFocus();

    // If we got a final answer, render the final formatted version
    if (finalEvent) {
      var respEl = document.getElementById(bubbleId);
      if (respEl) {
        var chatBubble = respEl.querySelector('.chat-bubble');
        // Build prefix (tool calls)
        var prefix = '';
        if (finalEvent.tool_calls && finalEvent.tool_calls.length > 0) {
          prefix = buildToolCallsHtml(finalEvent.tool_calls);
        }
        // Build suffix (suggestions)
        var suffix = '';
        if (finalEvent.suggestions && finalEvent.suggestions.length > 0) {
          suffix = renderAgentSuggestions(finalEvent.suggestions);
        }
        // Smoothly upgrade: keep streaming text, just format it in place
        var streamingSpan = chatBubble.querySelector('.streaming-text');
        if (streamingSpan) {
          // Streaming was active — format the existing text without flash
          streamingSpan.innerHTML = formatMarkdown(finalEvent.answer || 'No response.');
          streamingSpan.className = 'formatted-answer';
          // Prepend tool calls if any
          if (prefix) chatBubble.insertAdjacentHTML('afterbegin', prefix);
          // Append suggestions if any
          if (suffix) chatBubble.insertAdjacentHTML('beforeend', suffix);
        } else {
          // No streaming happened (fallback) — full replace
          chatBubble.innerHTML = prefix + formatMarkdown(finalEvent.answer || 'No response.') + suffix;
        }
      }

      if (finalEvent.cost > 0 && respEl) {
        var costDiv = document.createElement('div');
        costDiv.className = 'chat-cost';
        costDiv.textContent = '$' + finalEvent.cost.toFixed(4);
        respEl.appendChild(costDiv);
      }

      if (finalEvent.dashboard_refresh) {
        if (currentPage === 'overview') loadOverviewDashboard();
      }
    } else {
      // No final event — show whatever we have
      var fallbackEl = document.getElementById(bubbleId);
      if (fallbackEl) fallbackEl.querySelector('.chat-bubble').innerHTML = '<span style="color:var(--muted)">No response received.</span>';
    }

  } catch (e) {
    clearAgentFocus();
    var errEl = document.getElementById(bubbleId);
    if (errEl) errEl.querySelector('.chat-bubble').innerHTML = '<span style="color:var(--negative)">Error: ' + escHtml(e.message) + '</span>';
  }

  // Re-enable send only if quota allows — otherwise updateChatQuotaBanner
  // has already locked the input + button down.
  if (chatQuotaRemaining > 0) {
    document.getElementById('chat-send').disabled = false;
  }
  document.getElementById('chat-messages').scrollTop = 999999;
}

function handleAgentEvent(event, bubbleId, toolCallsAccum) {
  var statusEl = document.getElementById('agent-status');
  var msgsContainer = document.getElementById('chat-messages');

  if (event.type === 'focus') {
    // Agent is looking at a specific resource
    var target = event.target || '';
    var elId = '';
    if (target.startsWith('vin:')) {
      var vin = target.substring(4);
      if (weeklyData) {
        for (var fi = 0; fi < weeklyData.vehicles.length; fi++) {
          if (weeklyData.vehicles[fi].vin === vin) { elId = 'vrow-' + fi; break; }
        }
      }
    } else {
      elId = target;
    }
    if (elId) agentFocusElement(elId);
  }
  else if (event.type === 'token') {
    // Streaming answer token — must be outside statusEl check
    var bubble = document.getElementById(bubbleId);
    if (bubble) {
      var chatBubble = bubble.querySelector('.chat-bubble');
      var agentStatus = chatBubble.querySelector('#agent-status');
      if (agentStatus) agentStatus.remove();
      var tokenSpan = chatBubble.querySelector('.streaming-text');
      if (!tokenSpan) {
        tokenSpan = document.createElement('span');
        tokenSpan.className = 'streaming-text';
        chatBubble.appendChild(tokenSpan);
      }
      tokenSpan.textContent += event.text;
    }
  }
  else if (statusEl) {
    if (event.type === 'status') {
      statusEl.innerHTML = '<div class="spinner-sm"></div> <span class="agent-status-text">' + event.icon + ' ' + escHtml(event.text) + '</span>';
      // Start auto-scan when agent begins thinking
      if (!agentScanTimer) startAgentScan();
    }
    else if (event.type === 'tool_start') {
      var label = event.label || event.name;
      var argsStr = event.args_preview ? ' (' + escHtml(event.args_preview) + ')' : '';
      statusEl.innerHTML =
        '<div class="spinner-sm"></div> <span class="agent-status-text">⚙ ' + escHtml(label) + argsStr + '</span>';
      toolCallsAccum.push({ name: event.name, label: label, startTime: Date.now() });
      startAgentScan();
    }
    else if (event.type === 'tool_done') {
      // Use client-side elapsed time for accurate display
      var lastTool = toolCallsAccum.length > 0 ? toolCallsAccum[toolCallsAccum.length - 1] : null;
      var elapsed = lastTool ? ((Date.now() - lastTool.startTime) / 1000) : 0;
      var dur = elapsed >= 0.1 ? elapsed.toFixed(1) + 's' : Math.round(elapsed * 1000) + 'ms';
      if (lastTool) lastTool.duration = elapsed;
      var ok = !event.error;
      statusEl.innerHTML =
        '<span class="agent-status-text" style="color:' + (ok ? 'var(--positive)' : 'var(--negative)') + '">' +
        (ok ? '✓' : '✗') + ' ' + escHtml(event.name) + ' ' + dur + '</span>';
      stopAgentScan();
    }
    else if (event.type === 'error') {
      statusEl.innerHTML = '<span class="agent-status-text" style="color:var(--negative)">⚠ ' + escHtml(event.text) + '</span>';
      stopAgentScan();
    }
  }

  if (msgsContainer) msgsContainer.scrollTop = 999999;
}

function buildToolCallsHtml(toolCalls) {
  var summary = '';
  var detailParts = [];
  for (var i = 0; i < toolCalls.length; i++) {
    var tc = toolCalls[i];
    var ok = !tc.error;
    var durStr = '';
    if (tc.duration != null) durStr = tc.duration >= 0.1 ? tc.duration.toFixed(1) + 's' : Math.round(tc.duration * 1000) + 'ms';
    var statusTitle = ok ? '' : ' title="' + escHtml(String(tc.error || '')).replace(/"/g, '&quot;') + '"';
    summary += '<div class="tool-call"><span class="tool-icon">⚙</span><span class="tool-name">' + escHtml(tc.name) + '</span><span class="tool-dur">' + durStr + '</span><span class="tool-status ' + (ok ? 'ok' : 'err') + '"' + statusTitle + '>' + (ok ? '✓' : '✗') + '</span></div>';
    if (!ok && tc.error) {
      summary += '<div class="tool-error">' + escHtml(String(tc.error)) + '</div>';
    }
    var args = JSON.stringify(tc.args || {}, null, 2);
    var result = typeof tc.result === 'string' ? tc.result : JSON.stringify(tc.result, null, 2);
    var preview = (result || '').substring(0, 300);
    detailParts.push('▸ ' + tc.name + '(' + args + ')\n→ ' + preview + (result && result.length > 300 ? '...' : ''));
  }
  return '<div class="tool-calls-group"><div class="tool-calls-toggle" onclick="this.nextElementSibling.classList.toggle(\'open\')">▶ ' + toolCalls.length + ' tool' + (toolCalls.length > 1 ? 's' : '') + ' executed</div><div class="tool-calls-detail">' + escHtml(detailParts.join('\n\n')) + '</div>' + summary + '</div>';
}

function renderAgentSuggestions(suggestions) {
  var html = '<div class="agent-suggestions">';
  html += '<div class="agent-suggestions-label">Suggested actions</div>';
  for (var i = 0; i < suggestions.length; i++) {
    var s = suggestions[i];
    html += '<div class="agent-suggestion-row" data-vin="' + escHtml(s.vin) + '" data-dealer-code="' + escHtml(s.dealer_code) + '">' +
      '<button class="agent-suggestion-jump" type="button" title="Jump to this VIN">' +
        '<span class="sugg-vin">' + escHtml(s.vin) + '</span>' +
        '<span class="sugg-arrow">→</span>' +
        '<span class="sugg-dealer">' + escHtml(s.dealer_name || s.dealer_code) + '</span>' +
      '</button>' +
      '<button class="sugg-apply" type="button">Apply</button>' +
    '</div>';
  }
  html += '</div>';
  return html;
}

function installAgentSuggestionHandlers() {
  document.addEventListener('click', function(e) {
    var row = e.target.closest('.agent-suggestion-row');
    if (!row) return;
    var vin = row.getAttribute('data-vin') || '';
    var dealerCode = row.getAttribute('data-dealer-code') || '';
    if (!vin) return;
    if (e.target.closest('.sugg-apply')) {
      applySuggestion(vin, dealerCode);
    } else {
      focusSuggestionVehicle(vin);
    }
  });
}

function addChatMessage(role, content) {
  var id = 'cmsg-' + (++chatMsgCounter);
  var msgs = document.getElementById('chat-messages');
  msgs.insertAdjacentHTML('beforeend', '<div class="chat-msg ' + role + '" id="' + id + '"><div class="chat-bubble">' + content + '</div></div>');
  msgs.scrollTop = 999999;
  return id;
}

function formatMarkdown(text) {
  if (!text) return '';
  var html;
  if (typeof marked !== 'undefined') {
    marked.setOptions({ breaks: true, gfm: true });
    html = marked.parse(text);
  } else {
    html = '<p>' + text
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>')
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/`(.*?)`/g, '<code>$1</code>') + '</p>';
  }
  // Wrap <table> elements in a scrollable container
  html = html.replace(/<table>/g, '<div class="table-wrap"><table>').replace(/<\/table>/g, '</table></div>');
  return html;
}

function escHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Drawer ───────────────────────────────────────────


// ── Helpers ──────────────────────────────────────────

function fmt(n) { return Math.round(Math.abs(n)).toLocaleString('en-US'); }
function fmtScore(n) {
  if (n == null || isNaN(n)) return '—';
  var a = Math.abs(n);
  if (a >= 100) return a.toFixed(0);
  if (a >= 10) return a.toFixed(1);
  return a.toFixed(3);
}
function fmtDelta(n) { return (n >= 0 ? '+' : '-') + fmtScore(n); }
function fmtSigned(n) { return (n < 0 ? '-' : '') + fmtScore(n); }

// Render a cell showing the vehicle's assignment rank as the primary signal,
// with the raw alloc_score as a small gray subtitle for traceability.
// rank=1 gets a star + positive color; 2–3 neutral; 4+ negative.
function rankCell(rank, score) {
  if (rank == null) return '<span class="score-sub">—</span>';
  var cls = rank === 1 ? 'top' : (rank <= 3 ? 'mid' : 'low');
  return '<div class="rank-cell">'
       + '<span class="rank-primary ' + cls + '">Rank ' + rank + '</span>'
       + '<span class="score-sub">' + fmtSigned(score) + '</span>'
       + '</div>';
}

// ── Reset ────────────────────────────────────────────

async function resetAll() {
  if (!confirm('Reset all data to original state? This will undo all allocations.')) return;
  try {
    await api('/api/reset', {});
    // Clear all frontend state
    weeklyData = null;
    weeklyGreedyData = null;
    allocCompareData = null;
    allocationConfirmed = false;
    weeklyLoaded = false;
    overviewLoaded = false;
    selectedVINs.clear();
    weeklySelectedVINs.clear();
    weeklyCollapsedGroups.clear();
    sessionId = _newClientId();
    chatMsgCounter = 0;
    clearAgentFocus();
    // Reset weekly page
    document.getElementById('vehicle-list').innerHTML =
      '<div class="weekly-empty"><div class="weekly-empty-icon">⚡</div>' +
      '<div class="weekly-empty-title">No Allocation Yet</div>' +
      '<div class="weekly-empty-sub">Select vehicles in <a href="#" onclick="switchPage(\'fleet\');return false">Fleet Inventory</a> and click <strong>Allocate Selected</strong> to run the optimizer.</div></div>';
    document.getElementById('batch-kpis').innerHTML = '';
    document.getElementById('batch-date').textContent = '';
    var confirmBtn2 = document.getElementById('btn-confirm');
    if (confirmBtn2) confirmBtn2.style.display = 'none';
    var wtEl = document.getElementById('weekly-toolbar');
    if (wtEl) wtEl.style.display = 'none';
    // Reset chat — restore welcome screen from the hidden template
    document.getElementById('chat-messages').innerHTML = document.getElementById('chat-welcome-tpl').innerHTML;
    sessionId = _newClientId();
    chatMsgCounter = 0;
    try { await refreshChatStatus(); } catch (e) {}
    // Reset cached Home/Overview state so they re-fetch on next visit
    homeLoaded = false;
    homeData = null;
    overviewLoaded = false;
    // Reload fleet
    await loadFleet();
    if (DEMO_MODE) await runDemoBatch();
    // Refresh /api/overview so the heatmap reflects the fresh state
    try { overview = await api('/api/overview'); } catch (e) {}
    switchPage('home');
    updateAllocateButton();
  } catch (e) {
    alert('Reset failed: ' + e.message);
  }
}

// ══════════════════════════════════════════════════════════════════════
// v2 — Home tab renderers + per-batch Overview viz
// All new functions appended here so we don't disturb the existing code.
// ══════════════════════════════════════════════════════════════════════

async function loadHome() {
  try {
    var res = await fetch('/api/home', { headers: { 'X-Session-Id': sessionId } });
    if (!res.ok) throw new Error('API ' + res.status);
    homeData = await res.json();
    // Also ensure overview is loaded so per-dealer scatter has data
    if (!overview) {
      try { overview = await api('/api/overview'); } catch (e) {}
    }
    renderHome();
  } catch (e) {
    console.error('Home load failed:', e);
    var c = document.getElementById('home-hero-content');
    if (c) c.innerHTML = '<div style="color: var(--negative); padding: 20px">Failed to load home: ' + escHtml(e.message) + '</div>';
  }
}

function renderHome() {
  if (!homeData) return;
  renderHomeHero();
  renderHomeTrends();
  renderHomeStatusDonut();
  renderHomePipeline();
  // Geographic dealer map (full-width, deck.gl + MapLibre, color = util band, size = IN_SERVICE)
  if (overview) {
    try { renderDealerMap(); } catch (e) { console.warn('Dealer map render failed:', e); }
  }
  renderDealerHeatmap();
  // Production's Plotly scatter (proper axes, ticks, hover tooltips)
  if (overview) {
    try { renderDealerScatter(); } catch (e) { console.warn('Scatter render failed:', e); }
  }
  renderHomeTopMoves();
  renderHomeWatchlist();
}

// Dealer-performance heatmap on a real US map. Two toggleable layers:
//   FaaS dealers (default ON)  — colored by util band, sized by RENTED
//   Grounding (source) dealers (default OFF) — fixed red dots
var _homeDealerMapInstance = null;
var _homeMapShowFaaS = true;     // default: FaaS visible
var _homeMapShowGrounding = false; // default: Grounding hidden

// Util-band color → matches CSS .util-vhigh / .util-high / .util-good / .util-mid / .util-low / .util-vlow
function _homeMapBandColor(util) {
  if (util >= 0.90) return [0, 230, 118, 230];   // vhigh — vivid green
  if (util >= 0.75) return [0, 212, 155, 220];   // high — teal
  if (util >= 0.65) return [0, 212, 255, 215];   // good — cyan
  if (util >= 0.50) return [139, 151, 168, 210]; // mid — gray
  if (util >= 0.40) return [255, 193, 7, 225];   // low — amber
  return [255, 82, 82, 235];                      // vlow — red
}

function _buildHomeMapLayers() {
  if (!overview) return [];
  var dealers = overview.dealers || [];
  var sources = overview.sources || [];
  var layers = [];

  if (_homeMapShowFaaS) {
    // FaaS dealer dots — color = util band (heatmap), size = RENTED (pixel-based so
    // the RENTED variation stays visible at every zoom level).
    layers.push(new deck.ScatterplotLayer({
      id: 'home-dealers',
      data: dealers,
      getPosition: function(r) { return [r.LONGITUDE, r.LATITUDE]; },
      // RENTED 0 → 5 px floor, 50 → 15 px, 100 → 19 px, 200 → 25 px
      getRadius: function(r) { return 5 + Math.sqrt((r.RENTED || 0) + 1) * 1.4; },
      radiusUnits: 'pixels',
      getFillColor: function(r) { return _homeMapBandColor(r.UTIL_RATE || 0); },
      stroked: true, getLineColor: [255, 255, 255, 230],
      getLineWidth: 1.5, lineWidthUnits: 'pixels',
      pickable: true,
    }));
  }

  if (_homeMapShowGrounding) {
    // Source dots (grounding dealers) — red, smaller, fixed size — operational context only
    layers.push(new deck.ScatterplotLayer({
      id: 'home-sources',
      data: sources,
      getPosition: function(r) { return [r.SOURCE_LON, r.SOURCE_LAT]; },
      getRadius: 14000,
      radiusMinPixels: 5, radiusMaxPixels: 16,
      getFillColor: [255, 107, 107, 220],
      stroked: true, getLineColor: [255, 255, 255, 230],
      getLineWidth: 1.5, lineWidthMinPixels: 1,
      pickable: true,
    }));
  }

  return layers;
}

function toggleHomeMapLayer(layer) {
  if (layer === 'faas') _homeMapShowFaaS = !_homeMapShowFaaS;
  if (layer === 'grounding') _homeMapShowGrounding = !_homeMapShowGrounding;
  var fb = document.getElementById('toggle-faas');
  var gb = document.getElementById('toggle-grounding');
  if (fb) fb.classList.toggle('active', _homeMapShowFaaS);
  if (gb) gb.classList.toggle('active', _homeMapShowGrounding);
  // Update layers without recreating deck.gl — preserves camera state (zoom/pan)
  if (_homeDealerMapInstance) {
    _homeDealerMapInstance.setProps({ layers: _buildHomeMapLayers() });
  }
}

function renderDealerMap() {
  if (!overview) return;
  var container = document.getElementById('home-dealer-map');
  if (!container) return;

  // Update counts shown inside toggle buttons
  var faasCount = (overview.dealers || []).length;
  var grdCount = (overview.sources || []).length;
  var fcEl = document.getElementById('toggle-faas-count');
  var gcEl = document.getElementById('toggle-grounding-count');
  if (fcEl) fcEl.textContent = faasCount;
  if (gcEl) gcEl.textContent = grdCount;

  // If instance exists already, just update layers (e.g., overview data refreshed)
  if (_homeDealerMapInstance) {
    _homeDealerMapInstance.setProps({ layers: _buildHomeMapLayers() });
    return;
  }

  container.innerHTML = '';
  setTimeout(function() {
    _homeDealerMapInstance = new deck.DeckGL({
      container: container,
      mapStyle: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
      initialViewState: { longitude: -96, latitude: 38.5, zoom: 3.5, pitch: 0 },
      controller: true,
      layers: _buildHomeMapLayers(),
      getTooltip: function(info) {
        var o = info.object;
        if (!o) return null;
        if (o.DEALER_NAME) {
          var u = ((o.UTIL_RATE || 0) * 100).toFixed(0);
          var r = (o.RENTED != null) ? o.RENTED : '—';
          var is = (o.IN_SERVICE != null) ? o.IN_SERVICE : '—';
          var cap = (o.IN_SERVICE != null && o.RENTED != null) ? (is - r) : '—';
          return {
            text:
              o.DEALER_NAME + ' (' + o.STATE + ')' +
              '\nUtilization: ' + u + '%' +
              '\nRented: ' + r +
              '\nIn Service: ' + is +
              '\nIdle Capacity: ' + cap,
          };
        }
        if (o.SOURCE) {
          return { text: 'Source: ' + o.SOURCE + '\n' + (o.CITY || '') + ', ' + (o.STATE || '') };
        }
        return null;
      },
    });
  }, 50);
}

function renderHomeHero() {
  var hero = document.getElementById('home-hero-content');
  if (!hero) return;

  var wd = weeklyData, wgd = weeklyGreedyData;
  var html = '';

  // Compute current-week label from today
  var now = new Date();
  var monday = new Date(now); monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
  var sunday = new Date(monday); sunday.setDate(monday.getDate() + 6);
  var fmtDate = function(d) { return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }); };
  var weekNum = Math.ceil(((now - new Date(now.getFullYear(), 0, 1)) / 86400000 + 1) / 7);
  var weekLabel = metaRow(['Week ' + weekNum, fmtDate(monday) + ' – ' + fmtDate(sunday) + ', ' + sunday.getFullYear()]);

  if (!wd || !wd.vehicles) {
    html =
      '<div class="overview-eyebrow">' + weekLabel + '</div>' +
      '<h1 class="overview-title">Weekly allocation brief</h1>' +
      '<p class="overview-subtitle">' +
        'Run a <a href="#" onclick="switchPage(\'fleet\');return false" style="color: var(--accent); text-decoration: none">weekly allocation</a> to populate this brief with savings, KPIs and top moves.' +
      '</p>';
    hero.innerHTML = html;
    return;
  }

  // Allocation exists — compute hero KPIs
  var nTotal = wd.vehicles.length;
  var assigned = wd.vehicles.filter(function(v) { return v.assigned; });
  var nAssigned = assigned.length;
  var totalDist = 0, utilSum = 0, ilpTax = 0, ilpRank1 = 0;
  assigned.forEach(function(v) {
    totalDist += (v.assigned.distance || 0);
    utilSum += (v.assigned.utilization || 0);
    ilpTax += (v.assigned.prop_tax || 0);
    if (v.assigned.rank === 1) ilpRank1++;
  });
  // v.assigned.utilization is already a percentage (0-100), NOT a fraction.
  // The earlier `* 100` multiplied it again, hence the "8470%" bug.
  var avgUtilPct = nAssigned > 0 ? (utilSum / nAssigned) : 0;
  var ilpRank1Pct = nAssigned > 0 ? (ilpRank1 / nAssigned * 100) : 0;

  // ILP-vs-Greedy hero: lead with Rank-1 placement rate (AGENTS.md
  // rule 4: rank is the only per-vehicle quality metric safe to compare;
  // raw alloc_score isn't comparable across vehicles). Joint $-cost
  // framing got abandoned because ILP necessarily travels ≥ Greedy
  // (Greedy IS shortest distance) — that delta swamped the tax savings
  // on most batches and showed $0.
  //
  // Annual-tax-savings is reported as a real dollar secondary metric;
  // it's always positive when w_tax is calibrated correctly.
  //
  // IMPORTANT: distance uses the backend's `total_distance` field
  // (unique-arc deduplicated — multiple cars on the same source→dest
  // pair share a single carrier trip), NOT a per-vehicle sum which
  // would double-count shared arcs.
  var hasGreedy = !!(wgd && wgd.vehicles);
  var grRank1Pct = 0, ilpTaxDelta = 0, ilpDistDelta = 0;
  if (hasGreedy) {
    var grAssigned = 0, grRank1 = 0, grTax = 0;
    wgd.vehicles.forEach(function(v) {
      if (v.assigned) {
        grAssigned++;
        if (v.assigned.rank === 1) grRank1++;
        grTax += (v.assigned.prop_tax || 0);
      }
    });
    grRank1Pct = grAssigned > 0 ? (grRank1 / grAssigned * 100) : 0;
    ilpTaxDelta = grTax - ilpTax;
    // Use the deduplicated backend total_distance for the trip-cost frame.
    var ilpTotalDist = (typeof wd.total_distance === 'number') ? wd.total_distance : totalDist;
    var grTotalDist  = (typeof wgd.total_distance === 'number') ? wgd.total_distance : 0;
    ilpDistDelta = ilpTotalDist - grTotalDist;
  }

  var status = nAssigned >= nTotal * 0.9 ? 'on-track' : 'watch';
  var statusBadge = status === 'on-track'
    ? '<span style="background: rgba(0,230,118,0.15); border: 1px solid rgba(0,230,118,0.3); color: var(--positive); padding: 4px 11px; border-radius: 99px; font-size: 0.66rem; font-weight: 700;  display: inline-flex; align-items: center; gap: 6px;"><span style="width: 6px; height: 6px; background: var(--positive); border-radius: 50%; box-shadow: 0 0 6px rgba(0,230,118,0.6)"></span>On track</span>'
    : '<span style="background: rgba(255,193,7,0.15); border: 1px solid rgba(255,193,7,0.3); color: #ffc107; padding: 4px 11px; border-radius: 99px; font-size: 0.66rem; font-weight: 700;  display: inline-flex; align-items: center; gap: 6px;"><span style="width: 6px; height: 6px; background: #ffc107; border-radius: 50%"></span>Watch</span>';

  // Subtitle stays short — rank-vs-Greedy detail lives in the hero
  // card below to avoid an awkward line wrap on narrower viewports.
  html =
    '<div style="display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 16px; margin-bottom: 24px">' +
      '<div>' +
        '<div class="overview-eyebrow">' + weekLabel + '</div>' +
        '<h1 class="overview-title">Weekly allocation brief</h1>' +
        '<p class="overview-subtitle"><strong>' + nAssigned + ' of ' + nTotal + ' vehicles placed.</strong></p>' +
      '</div>' +
      statusBadge +
    '</div>' +
    '<div style="display: grid; grid-template-columns: ' + (hasGreedy ? '5fr 7fr' : '1fr') + '; gap: 12px">';

  if (hasGreedy) {
    // Sub-detail blends real-$ tax savings with the distance trade-off.
    var taxLine = ilpTaxDelta > 0
      ? 'Annual tax saved <strong style="color: var(--positive)">$' +
          ilpTaxDelta.toLocaleString(undefined, {maximumFractionDigits: 0}) +
        '</strong> vs Greedy'
      : 'Annual tax neutral vs Greedy';
    if (ilpDistDelta !== 0) {
      taxLine = metaRow([taxLine, 'ILP drives ' + (ilpDistDelta > 0 ? '+' : '−') +
        Math.abs(ilpDistDelta).toLocaleString(undefined, {maximumFractionDigits: 0}) + ' mi for higher utilization']);
    }
    html +=
      '<div class="featured-savings">' +
        '<div class="featured-label">Rank-1 placement vs Greedy</div>' +
        '<div style="display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px">' +
          '<span class="featured-number">' + ilpRank1Pct.toFixed(0) + '<span style="font-size: 1.4rem; color: var(--muted); font-weight: 600">%</span></span>' +
          '<span style="font-size: 0.78rem; color: var(--muted)">vs ' + grRank1Pct.toFixed(0) + '% Greedy</span>' +
        '</div>' +
        '<div style="color: var(--muted); font-size: 0.78rem">' + taxLine + '</div>' +
      '</div>';
  }

  html +=
    '<div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px">' +
      '<div class="hero-kpi-card"><div class="hero-kpi-label">Placed</div><div class="hero-kpi-number">' + nAssigned + '<span style="color: var(--muted); font-size: 1rem; font-weight: 400">/' + nTotal + '</span></div></div>' +
      '<div class="hero-kpi-card"><div class="hero-kpi-label">Avg util</div><div class="hero-kpi-number">' + avgUtilPct.toFixed(0) + '<span style="color: var(--muted); font-size: 1rem; font-weight: 400">%</span></div></div>' +
      '<div class="hero-kpi-card"><div class="hero-kpi-label">Total distance</div><div class="hero-kpi-number">' + (function() {
        // Backend total_distance is unique-arc deduplicated (multiple cars
        // sharing a source→dest pair count once — they ride one trucking
        // run together). Per-vehicle sum is what you get on this batch
        // if you ignore co-loading; fall back if older payload.
        var d = (typeof wd.total_distance === 'number') ? wd.total_distance : totalDist;
        return d >= 1000 ? (d / 1000).toFixed(1) + 'k' : d.toFixed(0);
      })() + '<span style="color: var(--muted); font-size: 1rem; font-weight: 400"> mi</span></div></div>' +
    '</div>' +
    '</div>';

  hero.innerHTML = html;
}

function renderHomeTrends() {
  var grid = document.getElementById('home-trends-grid');
  if (!grid || !homeData) return;
  var t = homeData.trends;
  // Mark section as synthetic if backend flagged it (no allocation history yet)
  var subEl = document.getElementById('home-trends-sub');
  if (subEl) {
    if (t && t._placeholder) {
      subEl.innerHTML = metaRow(['Last 8 weeks', '<em style="color: #ffc107">Synthetic placeholder</em>']);
    } else {
      subEl.textContent = 'Last 8 weeks';
    }
  }

  function spark(series, invert) {
    var min = Math.min.apply(null, series);
    var max = Math.max.apply(null, series);
    var range = (max - min) || 1;
    var pts = series.map(function(v, i) {
      var x = (i / (series.length - 1)) * 100;
      var yn = (v - min) / range;
      if (invert) yn = 1 - yn;
      return { x: x, y: 28 - yn * 24 };
    });
    var path = 'M ' + pts.map(function(p) { return p.x.toFixed(1) + ' ' + p.y.toFixed(1); }).join(' L ');
    return { stroke: path, fill: path + ' L 100 30 L 0 30 Z' };
  }

  function deltaPct(arr, invert) {
    var d = arr[arr.length - 1] - arr[0];
    if (invert) d = -d;
    var pct = (Math.abs(d) / Math.max(Math.abs(arr[0]), 1)) * 100;
    var sign = d >= 0 ? '+' : '−';
    return sign + pct.toFixed(0) + '%';
  }

  // HHI is a market-concentration index (Herfindahl-Hirschman). Real
  // demo viewers don't know the 0–10,000 raw scale, so divide by 100
  // and label it "Concentration" with a plain-English footnote. Avg
  // Distance card was removed: ILP intentionally trades distance for
  // util gains, so "Avg distance went down" isn't an unambiguously
  // good signal — keeping it on the home brief was misleading.
  var hhiSeries = t.hhi || [];
  var hhiPct = hhiSeries.map(function(v) { return v / 100; });
  var taxSeries = t.annual_tax_saved || [];
  var taxNow = taxSeries.length ? taxSeries[taxSeries.length - 1] : 0;
  var taxFirst = taxSeries.length ? taxSeries[0] : 0;
  var taxDelta = taxNow - taxFirst;
  var taxDeltaStr = (taxDelta >= 0 ? '+$' : '−$') +
    Math.abs(taxDelta).toLocaleString(undefined, {maximumFractionDigits: 0});

  var cards = [
    { label: 'Placement rate', val: t.placement_rate[t.placement_rate.length - 1] + '%', series: t.placement_rate, color: 'success', invert: false, delta: '+' + (t.placement_rate[t.placement_rate.length - 1] - t.placement_rate[0]) + ' pp', stroke: 'var(--accent)', fill: 'rgba(0,212,255,0.20)' },
    { label: 'Rank-1 placements', val: t.rank1_pct[t.rank1_pct.length - 1] + '%', series: t.rank1_pct, color: 'success', invert: false, delta: '+' + (t.rank1_pct[t.rank1_pct.length - 1] - t.rank1_pct[0]) + ' pp', stroke: 'var(--accent)', fill: 'rgba(0,212,255,0.20)' },
    {
      label: 'Annual tax saved vs Greedy <span class="info-icon" onclick="showInfoPopover(event, \'tax_saved_trend\')">ⓘ</span>',
      val: taxSeries.length
        ? '$' + taxNow.toLocaleString(undefined, {maximumFractionDigits: 0})
        : '—',
      series: taxSeries,
      color: 'success', invert: false,
      delta: taxDeltaStr,
      stroke: 'var(--positive)', fill: 'rgba(0,230,118,0.18)',
    },
    {
      label: 'Dealer concentration <span class="info-icon" onclick="showInfoPopover(event, \'dealer_concentration\')">ⓘ</span>',
      val: hhiPct.length ? hhiPct[hhiPct.length - 1].toFixed(1) + '%' : '—',
      series: hhiPct,
      color: 'success', invert: true,
      delta: deltaPct(hhiPct, true),
      stroke: '#a78bfa', fill: 'rgba(167,139,250,0.18)',
    },
  ];

  grid.innerHTML = cards.map(function(c) {
    var p = spark(c.series, c.invert);
    return (
      '<div class="trend-card">' +
        '<div class="trend-card-label">' + c.label + '</div>' +
        '<div style="display: flex; align-items: baseline; justify-content: space-between">' +
          '<div class="trend-card-value">' + c.val + '</div>' +
          '<div class="trend-card-delta" style="color: var(--' + c.color + ')">' + c.delta + '</div>' +
        '</div>' +
        '<svg viewBox="0 0 100 30" class="sparkline-svg" preserveAspectRatio="none">' +
          '<path d="' + p.fill + '" fill="' + c.fill + '"/>' +
          '<path d="' + p.stroke + '" stroke="' + c.stroke + '" stroke-width="2" fill="none"/>' +
        '</svg>' +
        (c.footnote ? '<div style="color: var(--muted); font-size: 0.65rem; margin-top: 6px; line-height: 1.4">' + c.footnote + '</div>' : '') +
      '</div>'
    );
  }).join('');
}

function renderHomeStatusDonut() {
  var container = document.getElementById('chart-status-donut');
  if (!container || !homeData) return;
  var fs = homeData.fleet_state;
  var total = fs.total || (fs.grounded + fs.transporting + fs.delivered + fs.incoming) || 1;
  var segs = [
    { label: 'Grounded', count: fs.grounded, color: '#ffc107' },
    { label: 'Transporting', count: fs.transporting, color: 'var(--accent)' },
    { label: 'Delivered', count: fs.delivered, color: 'var(--positive)' },
    { label: 'Incoming', count: fs.incoming, color: 'rgba(255,255,255,0.25)' },
  ];
  var circ = 2 * Math.PI * 15.9;
  var offset = 0;
  var arcs = segs.map(function(s) {
    var frac = s.count / total;
    var dash = frac * circ;
    var gap = circ - dash;
    var html = '<circle cx="18" cy="18" r="15.9" fill="none" stroke="' + s.color + '" stroke-width="3.5" stroke-dasharray="' + dash.toFixed(2) + ' ' + gap.toFixed(2) + '" stroke-dashoffset="' + (-offset).toFixed(2) + '" transform="rotate(-90 18 18)"/>';
    offset += dash;
    return html;
  }).join('');
  var legend = segs.map(function(s) {
    return '<div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 0.78rem">' +
      '<span style="width: 8px; height: 8px; background: ' + s.color + '; border-radius: 50%; flex-shrink: 0"></span>' +
      '<span style="color: var(--muted)">' + s.label + '</span>' +
      '<span style="margin-left: auto; font-weight: 600; font-variant-numeric: tabular-nums">' + s.count + '</span>' +
    '</div>';
  }).join('');
  container.innerHTML =
    '<div style="display: flex; align-items: center; gap: 16px">' +
      '<svg viewBox="0 0 36 36" style="width: 130px; height: 130px; flex-shrink: 0">' +
        '<circle cx="18" cy="18" r="15.9" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="3.5"/>' +
        arcs +
        '<text x="18" y="17.5" text-anchor="middle" style="font-size: 5.5px; fill: var(--text); font-weight: 700">' + total + '</text>' +
        '<text x="18" y="22" text-anchor="middle" style="font-size: 2.6px; fill: var(--muted)">total</text>' +
      '</svg>' +
      '<div style="flex: 1">' + legend + '</div>' +
    '</div>';
}

function renderHomePipeline() {
  var container = document.getElementById('chart-pipeline');
  if (!container || !homeData) return;
  var weeks = homeData.fleet_state.pipeline_by_week || [];
  var label = document.getElementById('home-pipeline-label');
  if (label) {
    var anySynthetic = weeks.some(function(w) { return w._synthetic; });
    label.innerHTML = anySynthetic
      ? metaRow(['Pipeline by week', '<em style="color: #ffc107">Prior weeks synthetic</em>'])
      : 'Pipeline by week';
  }
  if (!weeks.length) { container.innerHTML = ''; return; }
  var maxTotal = Math.max.apply(null, weeks.map(function(w) { return w.grounded + w.transporting + w.delivered; })) || 1;
  // Pixel heights: a percentage height inside an auto-height flex item resolves to 0.
  var BAR_AREA_PX = 112;
  var bars = weeks.map(function(w, idx) {
    var total = w.grounded + w.transporting + w.delivered;
    var heightPx = Math.round((total / maxTotal) * (BAR_AREA_PX - 8)) + 8;
    var gPct = (w.grounded / (total || 1)) * 100;
    var tPct = (w.transporting / (total || 1)) * 100;
    var dPct = (w.delivered / (total || 1)) * 100;
    var isCur = idx === weeks.length - 1;
    var opacity = isCur ? 1 : (0.7 + idx * 0.05);
    return (
      '<div style="display: flex; flex-direction: column; align-items: center; flex: 1">' +
        '<div style="font-size: 0.7rem; font-weight: 600; font-variant-numeric: tabular-nums; margin-bottom: 4px; color: ' + (isCur ? 'var(--text)' : 'var(--muted)') + '">' + fmt(total) + '</div>' +
        '<div style="width: 100%; height: ' + heightPx + 'px; display: flex; flex-direction: column; opacity: ' + opacity + '">' +
          '<div style="background: #ffc107; height: ' + gPct + '%"></div>' +
          '<div style="background: var(--accent); height: ' + tPct + '%"></div>' +
          '<div style="background: var(--positive); height: ' + dPct + '%"></div>' +
        '</div>' +
        '<div style="color: ' + (isCur ? 'var(--accent)' : 'var(--muted)') + '; font-size: 0.65rem; margin-top: 6px; font-weight: ' + (isCur ? '600' : '400') + '">' + escHtml(w.week) + (isCur ? ' ←' : '') + '</div>' +
      '</div>'
    );
  }).join('');
  container.innerHTML = '<div style="display: flex; align-items: flex-end; gap: 12px; height: 160px">' + bars + '</div>';
}

function renderDealerHeatmap() {
  var container = document.getElementById('dealer-table');
  if (!container || !homeData) return;
  // Ensure container has the heatmap class (production might have set it to other classes)
  container.className = 'dealer-heatmap';
  var heatmap = homeData.dealer_heatmap || [];
  if (!heatmap.length) {
    container.innerHTML = '<div style="grid-column: 1/-1; color: var(--muted); padding: 24px; text-align: center; font-size: 0.84rem">No dealer data.</div>';
    return;
  }
  container.innerHTML = heatmap.map(function(d) {
    var pct = (d.util * 100).toFixed(0);
    var title = (d.full_name || d.dealer_code) + ': ' + pct + '% utilization, ' + d.rented + ' rented';
    return (
      '<div class="dealer-cell util-' + escHtml(d.band) + '" data-dealer="' + escHtml(d.dealer_code) +
        '" title="' + escHtml(title) +
        '" onclick="showDealerPopover(event, \'' + escHtml(d.dealer_code) + '\')">' +
        '<div class="dealer-code">' + escHtml(d.code.slice(0, 7)) + '</div>' +
        '<div class="dealer-util-display">' + pct + '%</div>' +
        '<div class="rented-mini">' + d.rented + ' rented</div>' +
      '</div>'
    );
  }).join('');
}

// ── Dealer popover (click on a .dealer-cell) ─────────────────────────
// Surfaces full dealer details (util, RENTED, IN_SERVICE, idle capacity,
// state, prop tax rate) anchored next to the clicked cell. Joins data
// from homeData.dealer_heatmap (the per-dealer band rows) with the
// /api/overview dealers payload (for state + prop_tax_rate).

function _lookupOverviewDealer(dealerCode) {
  if (!overview || !overview.dealers) return null;
  for (var i = 0; i < overview.dealers.length; i++) {
    if (overview.dealers[i].DEALER_CODE === dealerCode) return overview.dealers[i];
  }
  return null;
}

function closeDealerPopover() {
  var pop = document.getElementById('dealer-popover-active');
  if (pop) pop.remove();
}

function showDealerPopover(evt, dealerCode) {
  evt.stopPropagation();
  evt.preventDefault();
  closeDealerPopover();

  // Find heatmap row (frontend-shaped) and overview row (full backend shape).
  var hm = (homeData && homeData.dealer_heatmap) || [];
  var d = null;
  for (var i = 0; i < hm.length; i++) {
    if (hm[i].dealer_code === dealerCode) { d = hm[i]; break; }
  }
  var od = _lookupOverviewDealer(dealerCode);
  if (!d && !od) return;

  var name      = (d && d.full_name) || (od && od.DEALER_NAME) || dealerCode;
  var util      = d ? d.util : (od ? (od.UTIL_RATE || 0) : 0);
  var rented    = d ? d.rented : (od ? Math.round(od.RENTED || 0) : 0);
  var inService = d ? d.in_service : (od ? Math.round(od.IN_SERVICE || 0) : 0);
  var idleCap   = Math.max(0, inService - rented);
  var band      = d ? d.band : (util >= 0.90 ? 'vhigh' : util >= 0.75 ? 'high' : util >= 0.65 ? 'good' : util >= 0.50 ? 'mid' : util >= 0.40 ? 'low' : 'vlow');
  var state     = od ? (od.STATE || '') : '';
  var taxRate   = od ? (od.PROP_TAX_RATE || 0) : 0;
  var trueCap   = od ? Math.round(od.TRUE_CAPACITY || 0) : 0;
  var deliv     = od ? Math.round(od.DELIVERED_COUNT || 0) : 0;
  var remCap    = od ? Math.round(od.REMAINING_CAPACITY || 0) : 0;

  var pct = (util * 100).toFixed(0);

  var pop = document.createElement('div');
  pop.className = 'dealer-popover';
  pop.id = 'dealer-popover-active';
  pop.innerHTML =
    '<div class="dp-head">' +
      '<div class="dp-head-text">' +
        '<div class="dp-name">' + escHtml(name) + '</div>' +
        '<div class="dp-meta"><span>' + escHtml(dealerCode) + '</span>' +
          (state ? ' <span class="state-pill">' + escHtml(state) + '</span>' : '') +
        '</div>' +
      '</div>' +
      '<button class="dp-close" onclick="closeDealerPopover()" aria-label="Close">×</button>' +
    '</div>' +
    '<div class="dp-stats">' +
      '<div class="dp-stat">' +
        '<div class="dp-stat-label">Utilization</div>' +
        '<div class="dp-stat-value dp-util-' + band + '">' + pct + '<span class="unit">%</span></div>' +
      '</div>' +
      '<div class="dp-stat">' +
        '<div class="dp-stat-label">Rented</div>' +
        '<div class="dp-stat-value">' + rented + '</div>' +
      '</div>' +
      '<div class="dp-stat">' +
        '<div class="dp-stat-label">In Service</div>' +
        '<div class="dp-stat-value">' + inService + '</div>' +
      '</div>' +
      '<div class="dp-stat">' +
        '<div class="dp-stat-label">Capacity (idle)</div>' +
        '<div class="dp-stat-value">' + idleCap + '</div>' +
      '</div>' +
      (taxRate ? (
        '<div class="dp-stat">' +
          '<div class="dp-stat-label">Property Tax</div>' +
          '<div class="dp-stat-value">' + (taxRate * 100).toFixed(2) + '<span class="unit">%</span></div>' +
        '</div>'
      ) : '') +
      (trueCap ? (
        '<div class="dp-stat">' +
          '<div class="dp-stat-label">Slots Remaining</div>' +
          '<div class="dp-stat-value">' + remCap + '<span class="unit">/' + trueCap + '</span></div>' +
        '</div>'
      ) : '') +
    '</div>' +
    '<div class="dp-foot">' +
      'Capacity (idle) = In Service − Rented. Slots Remaining is the ILP\'s per-dealer placement cap.' +
    '</div>';

  document.body.appendChild(pop);

  // Position: prefer below-right of the clicked cell, but flip into the
  // viewport if it would overflow.
  var anchor = evt.currentTarget.getBoundingClientRect();
  var popW = pop.offsetWidth, popH = pop.offsetHeight;
  var top  = anchor.bottom + window.scrollY + 8;
  var left = anchor.left + window.scrollX;
  if (left + popW > window.innerWidth - 12) left = window.innerWidth - popW - 12;
  if (left < 12) left = 12;
  if (top + popH > window.innerHeight + window.scrollY - 12) {
    // not enough room below — flip above
    top = anchor.top + window.scrollY - popH - 8;
  }
  pop.style.top = top + 'px';
  pop.style.left = left + 'px';

  // Outside-click closes (next frame so this click doesn't immediately trigger).
  setTimeout(function() {
    document.addEventListener('click', function _outside(e) {
      if (pop.contains(e.target)) return;
      closeDealerPopover();
      document.removeEventListener('click', _outside);
    });
  }, 0);
}

function renderHomeDealerScatter() {
  var container = document.getElementById('chart-scatter');
  if (!container || !homeData) return;
  var heatmap = homeData.dealer_heatmap || [];
  if (!heatmap.length) { container.innerHTML = ''; return; }

  // Axis range from data
  var maxUtil = 1.0;
  var maxRented = Math.max.apply(null, heatmap.map(function(d) { return d.rented; })) || 1;
  var maxInService = Math.max.apply(null, heatmap.map(function(d) { return d.in_service; })) || 1;

  // Y-axis follows the active scoring mode's demand-side signal:
  // additive uses RENTED (client review 2026-04-17), bucket uses IN_SERVICE (the field
  // that drives the tier). Marker size is always IN_SERVICE so the
  // dealer-size dimension never disappears.
  var useInServiceY = (scoringMode === 'bucket');
  var yMax = useInServiceY ? maxInService : maxRented;
  var yLabel = useInServiceY ? 'IN_SERVICE →' : 'RENTED →';

  // SVG viewBox: 100w x 70h, axes start at x=8 / y=62
  var points = heatmap.map(function(d) {
    var x = 8 + (d.util / maxUtil) * 88;
    var yVal = useInServiceY ? d.in_service : d.rented;
    var y = 62 - (yVal / yMax) * 56;
    var r = 1.0 + (d.in_service / maxInService) * 3.0;
    var color = d.band === 'vhigh' || d.band === 'high' ? 'var(--positive)' :
                d.band === 'good' ? 'var(--accent)' :
                d.band === 'mid' ? '#a3a8af' :
                '#ffc107';
    var opacity = d.band === 'vlow' ? 0.4 : (0.55 + (d.util * 0.5));
    return '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + r.toFixed(1) + '" fill="' + color + '" opacity="' + opacity.toFixed(2) + '"><title>' + escHtml(d.full_name || d.code) + ': ' + (d.util * 100).toFixed(0) + '% utilization, ' + d.rented + ' rented, ' + d.in_service + ' in service</title></circle>';
  }).join('');

  container.innerHTML =
    '<svg viewBox="0 0 100 70" preserveAspectRatio="none" style="width: 100%; height: 280px; background: rgba(255,255,255,0.015); border-radius: 8px">' +
      '<line x1="8" y1="62" x2="98" y2="62" stroke="var(--border)" stroke-width="0.2"/>' +
      '<line x1="8" y1="4" x2="8" y2="62" stroke="var(--border)" stroke-width="0.2"/>' +
      points +
      '<text x="50" y="68" text-anchor="middle" font-size="2.5" fill="var(--muted)">UTILIZATION →</text>' +
      '<text x="3" y="33" text-anchor="middle" font-size="2.5" fill="var(--muted)" transform="rotate(-90 3 33)">' + yLabel + '</text>' +
    '</svg>';

  // Title + footnote follow the active mode so the panel description matches the axes.
  var titleEl = document.getElementById('scatter-title');
  var footEl = document.getElementById('scatter-footnote');
  if (titleEl) {
    titleEl.textContent = useInServiceY
      ? 'Utilization vs cars in service'
      : 'Utilization vs rented cars';
  }
  if (footEl) {
    footEl.innerHTML = useInServiceY
      ? ''
      : '';
  }
}

function renderHomeTopMoves() {
  var container = document.getElementById('home-top-moves');
  if (!container || !homeData) return;
  var moves = homeData.top_moves || [];
  if (!moves.length) {
    container.innerHTML = '<div style="grid-column: 1/-1; color: var(--muted); font-size: 0.84rem; text-align: center; padding: 24px">Top moves appear once an allocation has run.</div>';
    return;
  }
  container.innerHTML = moves.map(function(m) {
    var symbol = m.tag === 'win' ? '▲' : m.tag === 'demand' ? '●' : '⚖';
    var metrics = (m.metrics || []).map(function(mm) {
      return '<div><div class="narrative-metric-label">' + escHtml(mm.label) + '</div><div class="narrative-metric-value">' + escHtml(mm.value) + '</div></div>';
    }).join('');
    return (
      '<div class="narrative-card">' +
        '<div class="narrative-tag ' + escHtml(m.tag) + '">' + symbol + ' ' + escHtml(m.tag_label) + '</div>' +
        '<h3>' + escHtml(m.title) + '</h3>' +
        '<p>' + escHtml(m.body) + '</p>' +
        '<div class="narrative-metrics">' + metrics + '</div>' +
      '</div>'
    );
  }).join('');
}

function renderHomeWatchlist() {
  var container = document.getElementById('home-watchlist');
  if (!container || !homeData) return;
  var items = homeData.watchlist || [];

  // Update "Last refreshed X ago" line
  var meta = homeData.watchlist_meta || {};
  updateWatchlistTime(meta.refreshed_at);

  if (!items.length) {
    container.innerHTML =
      '<div class="empty-state" style="padding: 28px 20px">' +
        '<div class="empty-state-icon" style="font-size: 1.8rem">↻</div>' +
        '<div class="empty-state-title">No watchlist data yet</div>' +
        '<div class="empty-state-sub">Click <strong>Refresh</strong> above to compute alerts from current fleet state. Top 5 by severity will appear here.</div>' +
      '</div>';
    return;
  }

  container.innerHTML = items.map(function(it) {
    // Map severity to visual class
    var sev = it.severity || 'medium';
    var sevClass = (sev === 'critical' || sev === 'high') ? 'rose' :
                   (sev === 'medium') ? 'amber' : '';
    // Map template_id (preferred) or signal type to an icon
    var ico = '•';
    var tid = it.template_id || '';
    if (tid.indexOf('saturation') === 0) ico = '⚠';
    else if (tid.indexOf('deferred') === 0) ico = '⚡';
    else if (tid.indexOf('stuck') === 0) ico = '⏱';
    else if (tid.indexOf('underutil') === 0) ico = '↘';
    else if (tid.indexOf('source_pressure') === 0) ico = '↑';
    else if (tid.indexOf('capacity_mismatch') === 0) ico = '⚖';
    // Severity hint as fallback
    else if (sev === 'critical' || sev === 'high') ico = '⚠';
    else if (sev === 'medium') ico = '⚡';
    else ico = '✓';

    return (
      '<div class="watch-item ' + sevClass + '">' +
        '<div class="watch-icon">' + ico + '</div>' +
        '<div class="watch-body">' +
          '<h4>' + escHtml(it.title || '') + '</h4>' +
          '<p>' + escHtml(it.body || '') + '</p>' +
        '</div>' +
        '<button style="background: rgba(255,255,255,0.04); border: 1px solid var(--border); color: var(--muted); padding: 5px 12px; border-radius: 6px; font-size: 0.74rem; align-self: center; cursor: pointer; font-family: inherit">' + escHtml(it.action_label || 'Review') + ' →</button>' +
      '</div>'
    );
  }).join('');
}

function updateWatchlistTime(iso) {
  var el = document.getElementById('watchlist-time');
  var absEl = document.getElementById('watchlist-time-abs');
  if (!el) return;
  if (!iso) {
    el.textContent = 'never';
    if (absEl) absEl.textContent = '';
    return;
  }
  try {
    var d = new Date(iso);
    var now = new Date();
    var diffSec = Math.max(0, Math.floor((now - d) / 1000));
    if (diffSec < 5) el.textContent = 'just now';
    else if (diffSec < 60) el.textContent = diffSec + 's ago';
    else if (diffSec < 3600) el.textContent = Math.floor(diffSec / 60) + ' min ago';
    else if (diffSec < 86400) el.textContent = Math.floor(diffSec / 3600) + ' h ago';
    else el.textContent = d.toLocaleDateString();
    if (absEl) {
      var pad = function(n) { return n < 10 ? '0' + n : '' + n; };
      var tzAbbr = (d.toLocaleTimeString('en-US', { timeZoneName: 'short' }).split(' ').pop()) || '';
      absEl.textContent =
        ' (' + d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
        ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) +
        (tzAbbr ? ' ' + tzAbbr : '') + ')';
    }
  } catch (e) {
    el.textContent = 'just now';
    if (absEl) absEl.textContent = '';
  }
}

async function refreshWatchlist() {
  var btn = document.getElementById('btn-watchlist-refresh');
  var iconEl = document.getElementById('watchlist-refresh-icon');
  var labelEl = document.getElementById('watchlist-refresh-label');
  if (btn) btn.disabled = true;
  if (iconEl) iconEl.innerHTML = '<span class="spinner-sm" style="width:12px;height:12px;border-width:2px"></span>';
  if (labelEl) labelEl.textContent = 'Refreshing…';

  try {
    var res = await fetch('/api/watchlist/refresh', {
      method: 'POST',
      headers: { 'X-Session-Id': sessionId },
    });
    if (res.status === 429) {
      // Quota exhausted at the gateway — show its message verbatim in the
      // watchlist area; the rest of the page keeps working.
      var quotaErr = await res.json().catch(function() { return {}; });
      var wlEl = document.getElementById('home-watchlist');
      if (wlEl) {
        wlEl.innerHTML =
          '<div style="color: var(--negative); font-size: 0.84rem; line-height: 1.5; ' +
          'padding: 16px; background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: 12px">' +
          '<strong>Quota exhausted.</strong><br>' +
          escHtml(quotaErr.message || 'Limit reached. Try again later.') +
          '</div>';
      }
      return;
    }
    if (!res.ok) throw new Error('API ' + res.status);
    var data = await res.json();
    // Update homeData so renderHomeWatchlist sees the new alerts
    if (!homeData) homeData = {};
    homeData.watchlist = data.alerts || [];
    homeData.watchlist_meta = {
      refreshed_at: data.refreshed_at,
      count: data.count,
      max_items: data.max_items,
    };
    renderHomeWatchlist();
  } catch (e) {
    console.error('Watchlist refresh failed:', e);
    alert('Watchlist refresh failed: ' + e.message + '\n\nCheck server logs — if the Claude CLI is unavailable, the endpoint falls back to deterministic templates. If you still see this error, the server may be down.');
  } finally {
    if (btn) btn.disabled = false;
    if (iconEl) iconEl.textContent = '↻';
    if (labelEl) labelEl.textContent = 'Refresh';
  }
}

// ── Batch Overview — per-batch only ──────────────────────────────────────

function renderBatchOverviewKPIs() {
  var grid = document.getElementById('kpi-grid');
  if (!grid) return;
  var wd = weeklyData;
  if (!wd || !wd.vehicles) {
    grid.innerHTML = '<div style="grid-column: 1/-1; color: var(--muted); padding: 32px; text-align: center; font-size: 0.86rem; background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: 14px">Run an allocation in <a href="#" onclick="switchPage(\'fleet\');return false" style="color: var(--accent); text-decoration: none">Fleet Inventory</a> to see batch KPIs.</div>';
    return;
  }
  var nTotal = wd.vehicles.length;
  var assigned = wd.vehicles.filter(function(v) { return v.assigned; });
  var nAssigned = assigned.length;
  var rank1 = assigned.filter(function(v) { return v.assigned.rank === 1; }).length;
  var utilSum = 0;
  assigned.forEach(function(v) {
    utilSum += (v.assigned.utilization || 0);
  });
  // v.assigned.utilization is already in 0–100 percentage form; the
  // previous `* 100` produced values like 8470%.
  var avgUtilPct = nAssigned ? utilSum / nAssigned : 0;
  // Backend total_distance is unique-arc deduplicated — multiple cars
  // sharing a source→dest pair count once because they ride together.
  // Per-vehicle sum would double-count shared arcs and disagree with
  // the comparison panel below.
  var totalDist = (typeof wd.total_distance === 'number')
    ? wd.total_distance
    : assigned.reduce(function(s, v) { return s + (v.assigned.distance || 0); }, 0);

  grid.innerHTML =
    '<div class="kpi-card"><div class="kpi-label">Vehicles in batch</div><div class="kpi-value">' + nTotal + '</div></div>' +
    '<div class="kpi-card"><div class="kpi-label">Assigned</div><div class="kpi-value">' + nAssigned + '<span style="color: var(--muted); font-size: 0.95rem; font-weight: 400"> / ' + nTotal + '</span></div><div class="kpi-delta" style="color: var(--positive)">' + Math.round(nAssigned / Math.max(nTotal, 1) * 100) + '% placement</div></div>' +
    '<div class="kpi-card"><div class="kpi-label">Rank-1 placements</div><div class="kpi-value">' + rank1 + '<span style="color: var(--muted); font-size: 0.95rem; font-weight: 400"> / ' + nAssigned + '</span></div><div class="kpi-delta">' + Math.round(rank1 / Math.max(nAssigned, 1) * 100) + '% of assigned</div></div>' +
    '<div class="kpi-card"><div class="kpi-label">Avg util at destination</div><div class="kpi-value">' + avgUtilPct.toFixed(0) + '<span style="color: var(--muted); font-size: 0.95rem; font-weight: 400">%</span></div></div>' +
    '<div class="kpi-card"><div class="kpi-label">Total distance</div><div class="kpi-value">' + totalDist.toLocaleString(undefined, {maximumFractionDigits: 0}) + '<span style="color: var(--muted); font-size: 0.95rem; font-weight: 400"> mi</span></div></div>';
}

function renderRankDist() {
  var container = document.getElementById('chart-rank-dist');
  if (!container) return;
  var wd = weeklyData;
  if (!wd || !wd.vehicles) {
    container.innerHTML = '<div style="padding: 24px; color: var(--muted); font-size: 0.84rem; text-align: center">Run an allocation to see rank distribution.</div>';
    return;
  }
  var counts = { r1: 0, r2: 0, r3: 0, r4: 0, deferred: 0 };
  var sumRank = 0, n = 0;
  wd.vehicles.forEach(function(v) {
    if (!v.assigned) { counts.deferred++; return; }
    var r = v.assigned.rank || 1;
    if (r === 1) counts.r1++;
    else if (r === 2) counts.r2++;
    else if (r === 3) counts.r3++;
    else counts.r4++;
    sumRank += r; n++;
  });
  var avgRank = n ? (sumRank / n).toFixed(2) : '—';
  var rank1Pct = wd.vehicles.length ? (counts.r1 / wd.vehicles.length * 100).toFixed(0) : 0;
  var maxC = Math.max(counts.r1, counts.r2, counts.r3, counts.r4, counts.deferred, 1);
  // Tick labels (5 rounded values from 0 to maxC)
  var tick = Math.ceil(maxC / 4);
  var ticks = [tick * 4, tick * 3, tick * 2, tick, 0];
  var bars = [
    { label: 'Rank 1', count: counts.r1, cls: 'r1' },
    { label: 'Rank 2', count: counts.r2, cls: 'r2' },
    { label: 'Rank 3', count: counts.r3, cls: 'r3' },
    { label: 'Rank 4', count: counts.r4, cls: 'r4-zero', isZero: counts.r4 === 0 },
    { label: 'Deferred', count: counts.deferred, cls: 'r-deferred' },
  ];
  container.innerHTML =
    '<div class="chart-wrap">' +
      '<div class="chart-yaxis">' + ticks.map(function(t) { return '<span>' + t + '</span>'; }).join('') + '</div>' +
      '<div class="chart-main">' +
        '<div class="chart-plot">' +
          '<div class="chart-gridline" style="top: 0%"></div>' +
          '<div class="chart-gridline" style="top: 25%"></div>' +
          '<div class="chart-gridline" style="top: 50%"></div>' +
          '<div class="chart-gridline" style="top: 75%"></div>' +
          bars.map(function(b) {
            var heightPct = b.isZero ? 0 : (b.count / maxC * 100);
            var bar = b.isZero
              ? '<div class="chart-bar r4-zero"></div>'
              : '<div class="chart-bar ' + b.cls + '" style="height: ' + heightPct + '%"></div>';
            return '<div class="chart-col"><div class="chart-col-value ' + b.cls + '">' + b.count + '</div>' + bar + '</div>';
          }).join('') +
        '</div>' +
        '<div class="chart-xaxis">' + bars.map(function(b) { return '<div class="chart-xlabel">' + b.label + '</div>'; }).join('') + '</div>' +
      '</div>' +
    '</div>' +
    '<div style="padding: 4px 18px 14px; color: var(--muted); font-size: 0.78rem">' +
      metaRow(['<strong style="color: var(--positive)">' + rank1Pct + '% Rank-1</strong>',
        'Avg rank <strong style="color: var(--text)">' + avgRank + '</strong>',
        counts.deferred > 0 ? counts.deferred + ' deferred at slot cap' : '']) +
    '</div>';
}

function renderDealerLoad() {
  var container = document.getElementById('chart-dealer-load');
  if (!container) return;
  var wd = weeklyData;
  if (!wd || !wd.vehicles) {
    container.innerHTML = '<div style="padding: 24px; color: var(--muted); font-size: 0.84rem; text-align: center">Run an allocation to see per-dealer load.</div>';
    return;
  }
  // Tally per-dealer placement counts
  var counts = {};
  var locations = {};
  wd.vehicles.forEach(function(v) {
    var a = v.assigned;
    if (!a) return;
    var key = a.dealer_code || a.dealer_name;
    if (!counts[key]) {
      counts[key] = 0;
      locations[key] = { name: a.dealer_name, state: a.state || '' };
    }
    counts[key]++;
  });
  var sorted = Object.keys(counts).map(function(k) {
    return { key: k, count: counts[k], name: locations[k].name, state: locations[k].state };
  }).sort(function(a, b) { return b.count - a.count; });

  if (!sorted.length) {
    container.innerHTML = '<div style="padding: 24px; color: var(--muted); font-size: 0.84rem; text-align: center">No assignments in this batch.</div>';
    return;
  }

  var maxC = sorted[0].count || 1;
  var top = sorted.slice(0, 7);
  var others = sorted.slice(7);

  var rows = top.map(function(d) {
    var pct = (d.count / maxC * 100).toFixed(1);
    var displayName = (d.name || d.key || '?').replace('FaaS_Dealer_', '');
    return (
      '<div class="dealer-load-row">' +
        '<div class="dealer-load-name">' + escHtml(displayName) + '<span class="dl-loc">' + escHtml(d.state || '—') + '</span></div>' +
        '<div class="dealer-load-bar-track"><div class="dealer-load-bar-fill" style="width: ' + pct + '%"></div></div>' +
        '<div class="dealer-load-count">' + d.count + '</div>' +
      '</div>'
    );
  }).join('');

  if (others.length) {
    var otherCount = others.reduce(function(s, d) { return s + d.count; }, 0);
    var otherPct = (otherCount / Math.max(sorted.reduce(function(s, d) { return s + d.count; }, 0), 1) * 100).toFixed(1);
    rows += (
      '<div class="dealer-load-row">' +
        '<div class="dealer-load-name" style="color: var(--muted)">' + others.length + ' other dealers<span class="dl-loc">avg ' + (otherCount / others.length).toFixed(1) + ' per dealer</span></div>' +
        '<div class="dealer-load-bar-track"><div class="dealer-load-bar-fill muted" style="width: ' + otherPct + '%"></div></div>' +
        '<div class="dealer-load-count" style="color: var(--muted)">' + otherCount + '<span class="dl-cap-sub">total</span></div>' +
      '</div>'
    );
  }

  var uniqueDealers = sorted.length;
  var top3Sum = sorted.slice(0, 3).reduce(function(s, d) { return s + d.count; }, 0);
  var totalAssigned = sorted.reduce(function(s, d) { return s + d.count; }, 0);
  var top3Pct = Math.round(top3Sum / Math.max(totalAssigned, 1) * 100);

  container.innerHTML =
    '<div class="dealer-load-list">' + rows + '</div>' +
    '<div style="padding: 14px 4px 8px; color: var(--muted); font-size: 0.78rem">' +
      metaRow(['<strong style="color: var(--text)">' + uniqueDealers + ' of 30</strong> dealers received a vehicle',
        'Top 3 took <strong style="color: var(--text)">' + top3Sum + ' cars, ' + top3Pct + '%</strong>']) +
    '</div>';
}

// ── Mobile chrome (chat drawer + nav-tab label shortening) ─────────────────
//
// Phones ≤600px get a slide-up chat drawer instead of the right sidebar.
// `toggleChatDrawer()` toggles the `.open` class on #chat-panel and the
// backdrop. ESC closes. Above 600px the CSS hides the FAB / backdrop and
// the chat-panel stays in its sidebar position regardless of the `.open`
// class, so this code is a pure no-op on desktop.

function toggleChatDrawer(open) {
  var panel = document.getElementById('chat-panel');
  var backdrop = document.getElementById('chat-fab-backdrop');
  if (!panel) return;
  var willOpen = (open === undefined) ? !panel.classList.contains('open') : !!open;
  // Chat panel is `display: none` on every page except where chat is
  // intended; flip it to flex so the drawer can actually show.
  if (willOpen) panel.style.display = 'flex';
  panel.classList.toggle('open', willOpen);
  if (backdrop) backdrop.classList.toggle('open', willOpen);
  // Focus the input when opening so the user can type immediately.
  if (willOpen) {
    var input = document.getElementById('chat-input');
    if (input) setTimeout(function() { input.focus(); }, 200);
  }
}

function toggleChatFloat(open) {
  var panel = document.getElementById('chat-panel');
  var btn = document.getElementById('chat-layout-toggle');
  if (!panel || window.matchMedia('(max-width: 600px)').matches) return;
  var willFloat = (open === undefined) ? !document.body.classList.contains('chat-floating') : !!open;
  if (willFloat && currentPage === 'home') switchPage(weeklyLoaded ? 'weekly' : 'fleet');
  document.body.classList.toggle('chat-floating', willFloat);
  panel.style.display = 'flex';
  if (willFloat) {
    var defaultWidth = Math.min(460, Math.max(360, Math.round(window.innerWidth * 0.32)));
    var defaultHeight = Math.min(720, Math.max(460, Math.round(window.innerHeight * 0.68)));
    var left = Math.max(16, Math.round(window.innerWidth - defaultWidth - 18));
    var top = Math.max(58, Math.round(window.innerHeight - defaultHeight - 18));
    if (!panel.dataset.floatInitialized || !panel.style.left || !panel.style.top) {
      panel.style.width = defaultWidth + 'px';
      panel.style.height = defaultHeight + 'px';
      panel.style.left = left + 'px';
      panel.style.top = top + 'px';
      panel.dataset.floatInitialized = '1';
    } else {
      keepChatFloatInViewport();
    }
  } else {
    panel.style.width = '';
    panel.style.height = '';
    panel.style.left = '';
    panel.style.top = '';
  }
  if (btn) {
    btn.setAttribute('aria-pressed', willFloat ? 'true' : 'false');
    btn.textContent = willFloat ? '↙' : '↗';
    btn.title = willFloat ? 'Dock chat' : 'Pop out chat';
  }
  var input = document.getElementById('chat-input');
  if (willFloat && input) setTimeout(function() { input.focus(); }, 120);
}

function keepChatFloatInViewport() {
  var panel = document.getElementById('chat-panel');
  if (!panel || !document.body.classList.contains('chat-floating')) return;
  var rect = panel.getBoundingClientRect();
  var minVisible = 80;
  var left = Math.min(Math.max(rect.left, 8), window.innerWidth - minVisible);
  var top = Math.min(Math.max(rect.top, 52), window.innerHeight - minVisible);
  panel.style.left = Math.round(left) + 'px';
  panel.style.top = Math.round(top) + 'px';
}

function installChatFloatDrag() {
  var panel = document.getElementById('chat-panel');
  var bar = document.getElementById('chat-popout-bar');
  if (!panel || !bar) return;
  var drag = null;
  bar.addEventListener('pointerdown', function(e) {
    if (!document.body.classList.contains('chat-floating')) return;
    if (e.target.closest('button')) return;
    var rect = panel.getBoundingClientRect();
    drag = {
      startX: e.clientX,
      startY: e.clientY,
      left: rect.left,
      top: rect.top,
    };
    bar.setPointerCapture(e.pointerId);
    panel.classList.add('dragging');
  });
  bar.addEventListener('pointermove', function(e) {
    if (!drag) return;
    var nextLeft = drag.left + e.clientX - drag.startX;
    var nextTop = drag.top + e.clientY - drag.startY;
    var rect = panel.getBoundingClientRect();
    nextLeft = Math.min(Math.max(nextLeft, 8), window.innerWidth - Math.min(120, rect.width));
    nextTop = Math.min(Math.max(nextTop, 52), window.innerHeight - 80);
    panel.style.left = Math.round(nextLeft) + 'px';
    panel.style.top = Math.round(nextTop) + 'px';
  });
  function stopDrag(e) {
    if (!drag) return;
    drag = null;
    panel.classList.remove('dragging');
    try { bar.releasePointerCapture(e.pointerId); } catch (_) {}
  }
  bar.addEventListener('pointerup', stopDrag);
  bar.addEventListener('pointercancel', stopDrag);
}

function installMobileChrome() {
  // ESC closes the chat drawer when open on mobile.
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      var panel = document.getElementById('chat-panel');
      if (panel && panel.classList.contains('open')) toggleChatDrawer(false);
      if (document.body.classList.contains('chat-floating')) toggleChatFloat(false);
    }
  });

  // Shorten nav-tab labels on very narrow viewports so they fit on one
  // line without truncating mid-word. We store originals so they can be
  // restored if the viewport grows again (e.g. device rotation).
  var navTabs = document.querySelectorAll('.nav-tab');
  var shortLabels = {
    'Fleet Inventory': 'Fleet',
    'Weekly Allocation': 'Weekly',
    'Batch Overview': 'Overview',
  };
  navTabs.forEach(function(tab) {
    if (!tab.dataset.fullLabel) tab.dataset.fullLabel = tab.textContent.trim();
  });

  function applyNavLabels() {
    var narrow = window.matchMedia('(max-width: 600px)').matches;
    navTabs.forEach(function(tab) {
      var full = tab.dataset.fullLabel;
      tab.textContent = narrow && shortLabels[full] ? shortLabels[full] : full;
    });
  }
  applyNavLabels();
  window.addEventListener('resize', applyNavLabels);
  window.addEventListener('resize', function() {
    if (window.matchMedia('(max-width: 600px)').matches) toggleChatFloat(false);
    else keepChatFloatInViewport();
  });
  installAgentSuggestionHandlers();
  installChatFloatDrag();
}

// ── Start ────────────────────────────────────────────
init();
