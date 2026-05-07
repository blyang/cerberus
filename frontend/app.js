// Cerberus frontend. Vanilla JS. Single page: input -> SSE-driven progress + sorted results.
//
// Note on rendering: every dynamic value reaching the DOM is passed through escapeHtml()
// before any innerHTML assignment. The data sources are:
//   1. /api/runs (server-controlled — our own backend output)
//   2. URL the operator types into the form
// Both are escaped at render time. No third-party content reaches innerHTML un-escaped.

(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const form = $('run-form');
  const urlInput = $('url');
  const envSelect = $('environment');
  const runBtn = $('run-btn');
  const progressSection = $('progress-section');
  const progressLine = $('progress-line');
  const progressBar = $('progress-bar');
  const runIdLine = $('run-id-line');
  const summarySection = $('summary-section');
  const summaryBar = $('summary-bar');
  const reportLink = $('report-link');
  const resultsSection = $('results-section');

  const state = {
    runId: null,
    url: null,
    status: 'idle',
    total: 0,
    completed: 0,
    etaMs: null,
    checks: {},
    manualToggles: {},
    sse: null,
  };

  const SECTIONS = ['Failed (Blocking)', 'Failed (Other)', 'Needs Review', 'Manual', 'Passed', 'N/A'];
  const SECTION_TITLES = {
    A: 'A — Rendering & Status',
    B: 'B — Indexability & Head Controls',
    C: 'C — HTML Semantics',
    D: 'D — Structured Data',
    E: 'E — Performance, Mobile & Security',
    F: 'F — Discoverability, Crawling & Access',
    G: 'G — Internationalization',
    H: 'H — Serving Integrity',
  };

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
  const e = escapeHtml;

  // Allowlist server-controlled status strings to a known set of CSS class suffixes; any unknown
  // value falls back to 'Unknown' rather than being interpolated raw into a class attribute.
  const STATUS_CLASSES = {
    'Pass': 'Pass', 'Fail': 'Fail', 'Manual': 'Manual',
    'Needs Review': 'Needs-Review', 'N/A': 'NA',
    'Pending': 'Pending', 'Running': 'Running',
  };
  function statusClass(status) {
    return 'st-' + (STATUS_CLASSES[status] || 'Unknown');
  }

  // Brief R2.2: each failed item should link back to its checklist row.
  const CHECKLIST_BASE = 'https://docs.google.com/spreadsheets/d/1Kka3oAavdaWHCEDGYbBZFG8vAwq1C_1fpWlV226ozKE/edit';
  function checklistLink(checkId) {
    return CHECKLIST_BASE + '#gid=795173280&range=A:A&search=' + encodeURIComponent(checkId);
  }

  document.addEventListener('DOMContentLoaded', async function () {
    try {
      const cfg = await fetch('/api/config').then(r => r.json());
      if (cfg && cfg.default_url) urlInput.value = cfg.default_url;
      const envs = (cfg && cfg.environments) || ['production'];
      const defaultEnv = (cfg && cfg.default_environment) || 'production';
      while (envSelect.firstChild) envSelect.removeChild(envSelect.firstChild);
      envs.forEach(function (v) {
        const opt = document.createElement('option');
        opt.value = v;
        opt.textContent = v;
        if (v === defaultEnv) opt.selected = true;
        envSelect.appendChild(opt);
      });
    } catch (_) { /* config endpoint optional */ }
    const params = new URLSearchParams(window.location.search);
    const runId = params.get('run');
    if (runId) await attachToRun(runId);
  });

  form.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    const url = urlInput.value.trim();
    if (!url) return;
    const environment = (envSelect && envSelect.value) || 'production';
    runBtn.disabled = true;
    runBtn.textContent = 'Starting...';
    try {
      const res = await fetch('/api/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url, environment }),
      });
      if (!res.ok) throw new Error(await res.text());
      const data = await res.json();
      history.replaceState({}, '', '/?run=' + data.run_id);
      await attachToRun(data.run_id);
    } catch (err) {
      runBtn.disabled = false;
      runBtn.textContent = 'Run';
      alert('Failed to start run: ' + err.message);
    }
  });

  async function attachToRun(runId) {
    state.runId = runId;
    state.checks = {};
    state.manualToggles = {};  // populated by snapshot; init empty so renders before snapshot don't crash
    runBtn.disabled = true;
    runBtn.textContent = 'Running...';
    runIdLine.textContent = 'run: ' + runId;
    progressSection.classList.remove('hidden');

    if (state.sse) state.sse.close();
    const sse = new EventSource('/api/runs/' + runId + '/events');
    state.sse = sse;

    sse.addEventListener('snapshot', function (msg) {
      const data = JSON.parse(msg.data);
      state.url = data.run.url;
      state.status = data.run.status;
      state.manualToggles = data.manual_toggles || {};
      (data.checks || []).forEach(function (c) {
        state.checks[c.check_id] = checkFromRow(c);
      });
      refresh();
      if (data.run.status === 'completed' || data.run.status === 'failed') onRunDone();
    });
    sse.addEventListener('progress', function (msg) {
      const d = JSON.parse(msg.data);
      state.total = d.total;
      state.completed = d.completed;
      state.etaMs = d.eta_ms;
      refreshProgress();
    });
    sse.addEventListener('check_running', function (msg) {
      const d = JSON.parse(msg.data);
      if (state.checks[d.check_id]) {
        state.checks[d.check_id].status = 'Running';
      } else {
        state.checks[d.check_id] = { check_id: d.check_id, status: 'Running', summary: '', details: {} };
      }
      refresh();
    });
    sse.addEventListener('check_completed', function (msg) {
      const d = JSON.parse(msg.data);
      state.checks[d.check_id] = {
        check_id: d.check_id,
        status: d.status,
        summary: d.summary,
        details: d.details || {},
      };
      refresh();
    });
    sse.addEventListener('run_completed', function () { onRunDone(); });
    sse.addEventListener('done', function () { sse.close(); });
    sse.addEventListener('ping', function () { });
    sse.onerror = function () { /* auto-reconnects */ };
  }

  function onRunDone() {
    runBtn.disabled = false;
    runBtn.textContent = 'Run again';
    state.status = 'completed';
    refresh();
  }

  function checkFromRow(row) {
    return {
      check_id: row.check_id,
      status: row.status,
      summary: row.summary,
      details: row.details || {},
    };
  }

  function refreshProgress() {
    const total = state.total || Object.keys(state.checks).length || 1;
    const completed = state.completed;
    const pct = total > 0 ? Math.floor((completed / total) * 100) : 0;
    progressBar.style.width = pct + '%';
    let eta = '';
    if (state.etaMs != null && state.status !== 'completed') {
      eta = ' · ETA ~' + Math.max(1, Math.round(state.etaMs / 1000)) + 's';
    }
    progressLine.textContent = 'Running ' + completed + ' of ' + total + ' checks' + eta;
    if (state.status === 'completed') {
      progressLine.textContent = completed + ' of ' + total + ' checks complete.';
      progressBar.classList.add('bg-emerald-600');
    }
  }

  function refresh() {
    refreshProgress();
    renderSummary();
    renderResults();
  }

  function renderSummary() {
    const counts = {};
    Object.values(state.checks).forEach(function (c) {
      counts[c.status] = (counts[c.status] || 0) + 1;
    });
    if (state.status !== 'completed' || Object.keys(state.checks).length === 0) {
      summarySection.classList.add('hidden');
      reportLink.classList.add('hidden');
      return;
    }
    summarySection.classList.remove('hidden');
    const order = ['Fail', 'Needs Review', 'Manual', 'Pass', 'N/A', 'Running'];
    const html = order.map(function (k) {
      const v = counts[k] || 0;
      return '<div><div class="text-2xl font-semibold">' + v + '</div><div class="text-xs text-slate-500">' + e(k) + '</div></div>';
    }).join('');
    summaryBar.innerHTML = html;
    if (state.runId) {
      reportLink.href = '/api/runs/' + encodeURIComponent(state.runId) + '/report.md';
      reportLink.classList.remove('hidden');
    }
  }

  function severityRank(sev) {
    return sev === 'Blocking' ? 0 : (sev === 'Recommended' ? 1 : 2);
  }

  function bucketOf(check) {
    const sev = check.details.severity || '';
    if (check.status === 'Fail') return sev === 'Blocking' ? 'Failed (Blocking)' : 'Failed (Other)';
    if (check.status === 'Needs Review') return 'Needs Review';
    if (check.status === 'Manual') return 'Manual';
    if (check.status === 'Pass') return 'Passed';
    if (check.status === 'N/A') return 'N/A';
    return null;
  }

  function renderResults() {
    const buckets = {};
    SECTIONS.forEach(function (s) { buckets[s] = []; });
    Object.values(state.checks).forEach(function (c) {
      const b = bucketOf(c);
      if (b) buckets[b].push(c);
    });
    Object.values(buckets).forEach(function (list) {
      list.sort(function (a, b) {
        const sa = severityRank(a.details.severity || '');
        const sb = severityRank(b.details.severity || '');
        if (sa !== sb) return sa - sb;
        return a.check_id.localeCompare(b.check_id);
      });
    });

    if (state.status !== 'completed') {
      resultsSection.innerHTML = renderLiveBySection();
      return;
    }

    const html = SECTIONS.map(function (name) {
      const items = buckets[name] || [];
      if (items.length === 0) return '';
      const collapsed = (name === 'Passed' || name === 'N/A');
      return '<details ' + (collapsed ? '' : 'open') + ' class="bg-white border border-slate-200 rounded-lg">' +
        '<summary class="px-4 py-3 font-semibold text-slate-800 flex items-center justify-between">' +
        '<span>' + e(name) + '</span><span class="text-xs text-slate-500">' + items.length + '</span></summary>' +
        '<div class="border-t border-slate-200 divide-y divide-slate-100">' +
        items.map(renderCheckRow).join('') + '</div></details>';
    }).filter(Boolean).join('');
    resultsSection.innerHTML = html;
  }

  function renderLiveBySection() {
    const bySection = {};
    Object.values(state.checks).forEach(function (c) {
      const sec = (c.details.section) || c.check_id[0];
      bySection[sec] = bySection[sec] || [];
      bySection[sec].push(c);
    });
    const sectionKeys = Object.keys(bySection).sort();
    return sectionKeys.map(function (sec) {
      const items = bySection[sec].sort(function (a, b) { return a.check_id.localeCompare(b.check_id); });
      return '<div class="bg-white border border-slate-200 rounded-lg">' +
        '<div class="px-4 py-2 font-semibold text-slate-800 border-b border-slate-100 text-sm">' +
        e(SECTION_TITLES[sec] || sec) + '</div>' +
        '<div class="divide-y divide-slate-100">' + items.map(renderLiveRow).join('') + '</div></div>';
    }).join('');
  }

  function renderLiveRow(c) {
    const title = c.details.title || '';
    const sev = c.details.severity || '';
    const isRunning = c.status === 'Running';
    return '<div class="px-4 py-2 flex items-center gap-3 ' + (isRunning ? 'row-running' : '') + '">' +
      '<span class="font-mono text-xs text-slate-500 w-12">' + e(c.check_id) + '</span>' +
      '<span class="status-badge sev-' + e(sev) + '">' + e(sev || '·') + '</span>' +
      '<span class="flex-1 text-sm">' + e(title) + '</span>' +
      '<span class="status-badge ' + statusClass(c.status) + '">' + e(c.status) + '</span></div>';
  }

  function renderCheckRow(c) {
    const id = c.check_id;
    const sev = c.details.severity || '';
    const title = c.details.title || '';
    const summary = c.summary || '';
    const subSteps = c.details.sub_steps || [];
    const instruction = c.details.instruction || '';
    const manualMark = state.manualToggles[id];
    const status = c.status;

    const subHtml = subSteps.map(function (s) {
      let block = '<li class="flex items-start gap-2">' +
        '<span class="status-badge ' + statusClass(s.status) + '">' + e(s.status) + '</span>' +
        '<span><span class="font-medium">' + e(s.label) + '</span>' +
        (s.detail ? ' — <span class="text-slate-600">' + e(s.detail) + '</span>' : '') +
        '</span></li>';
      if (s.instruction) {
        block += '<li class="ml-6 text-xs text-amber-800 bg-amber-50 p-2 rounded">' + e(s.instruction) + '</li>';
      }
      return block;
    }).join('');

    const manualToggle = (status === 'Manual' || status === 'Needs Review') ? renderManualToggle(id, manualMark) : '';

    const checklistAnchor = '<a href="' + e(checklistLink(id)) + '" target="_blank" rel="noopener" class="text-xs text-slate-500 underline hover:text-slate-800">checklist row →</a>';

    return '<details class="px-4 py-3" data-id="' + e(id) + '">' +
      '<summary class="flex items-center gap-3">' +
      '<span class="font-mono text-xs text-slate-500 w-12">' + e(id) + '</span>' +
      '<span class="status-badge sev-' + e(sev) + '">' + e(sev) + '</span>' +
      '<span class="flex-1 font-medium text-sm">' + e(title) + '</span>' +
      '<span class="status-badge ' + statusClass(status) + '">' + e(status) + '</span>' +
      (manualMark ? '<span class="text-xs italic text-slate-500">marked: ' + e(manualMark) + '</span>' : '') +
      '</summary>' +
      '<div class="mt-2 ml-16 text-sm space-y-2">' +
      (summary ? '<div class="text-slate-700">' + e(summary) + '</div>' : '') +
      (subSteps.length ? '<ul class="space-y-1">' + subHtml + '</ul>' : '') +
      (instruction ? '<div class="bg-amber-50 border border-amber-200 p-3 rounded text-amber-900 text-sm"><strong>Instructions:</strong> ' + e(instruction) + '</div>' : '') +
      manualToggle +
      '<div class="mt-2">' + checklistAnchor + '</div>' +
      '<details><summary class="text-xs text-slate-500">raw details</summary><pre class="detail-block">' + e(JSON.stringify(safeDetails(c.details), null, 2)) + '</pre></details>' +
      '</div></details>';
  }

  function renderManualToggle(id, current) {
    function btn(label, value, color) {
      const active = current === value;
      const base = 'px-3 py-1 rounded text-xs font-medium border';
      const cls = active ? color + ' border-transparent text-white' : 'bg-white border-slate-300 text-slate-700 hover:bg-slate-50';
      return '<button data-manual="' + e(id) + '" data-value="' + e(value) + '" class="' + base + ' ' + cls + '">' + e(label) + '</button>';
    }
    return '<div class="flex gap-2 items-center mt-2">' +
      '<span class="text-xs text-slate-500">Mark as:</span>' +
      btn('Pass', 'Pass', 'bg-emerald-600') +
      btn('Fail', 'Fail', 'bg-rose-600') +
      (current ? '<button data-manual="' + e(id) + '" data-value="" class="text-xs text-slate-400 underline">clear</button>' : '') +
      '</div>';
  }

  function safeDetails(d) {
    const c = Object.assign({}, d);
    if (Array.isArray(c.json_ld_blocks) && c.json_ld_blocks.length) {
      c.json_ld_blocks = c.json_ld_blocks.map(function (b) { return typeof b === 'object' ? '<JSON-LD block>' : b; });
    }
    return c;
  }

  resultsSection.addEventListener('click', async function (ev) {
    const t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    const id = t.getAttribute('data-manual');
    if (!id) return;
    if (!state.runId) return;
    const value = t.getAttribute('data-value') || '';
    const res = await fetch('/api/runs/' + state.runId + '/manual/' + id, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: value }),
    });
    if (res.ok) {
      if (value) state.manualToggles[id] = value;
      else delete state.manualToggles[id];
      refresh();
    }
  });
})();
