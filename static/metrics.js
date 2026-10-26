// Admin performance metric board. Fetches /api/metrics and renders KPIs, CSS-bar
// breakdowns, an hourly timeline, and a recent-requests table. No external chart libs
// (CSP-safe) — everything is plain DOM + CSS bars.
(() => {
  const el = (id) => document.getElementById(id);
  const rangeSel = el("range");
  const fSource = el("f-source");
  const fBackend = el("f-backend");
  const fType = el("f-type");
  const fStatus = el("f-status");
  const fModel = el("f-model");

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s ?? "";
    return d.innerHTML;
  }
  const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString());
  // Durations are stored in milliseconds for precision, but humans scan seconds more
  // easily on this dashboard. Keep three decimals so sub-second requests do not become 0.
  const formatSeconds = (ms) => (Number(ms) / 1000).toLocaleString(undefined, {
    minimumFractionDigits: 3,
    maximumFractionDigits: 3,
  });
  const SRC_LABEL = { "webui-chat": "Hearth chat", opencode: "OpenCode", vscode: "VSCode/Continue", vscode_continue: "VSCode/Continue", market_analysis: "Market Analysis", lan: "Other LAN", image: "Image" };
  const label = (k) => SRC_LABEL[k] || k;

  function prettyText(value) {
    const text = String(value ?? "").replace(/\r\n/g, "\n").trim();
    if (!text) return "";
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch (_) {
      return text;
    }
  }

  function traceBody(raw, isRequest = false) {
    const text = String(raw ?? "");
    if (!isRequest) return `<pre>${esc(prettyText(text))}</pre>`;
    // The proxy stores prompts as "role: content" blocks. Render those as actual
    // message cards instead of one undifferentiated wall of monospace text.
    const marker = /(?:^|\n)(system|user|assistant|tool|developer): /g;
    const matches = [...text.matchAll(marker)];
    if (!matches.length) return `<pre>${esc(prettyText(text))}</pre>`;
    return matches.map((match, i) => {
      const start = match.index + match[0].length;
      const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
      return `<section class="dev-trace-message">
        <div class="dev-trace-role role-${esc(match[1])}">${esc(match[1])}</div>
        <pre>${esc(prettyText(text.slice(start, end)))}</pre>
      </section>`;
    }).join("");
  }

  const PAGE_SIZE = 50;
  let page = 1;
  const modelSort = {key: "requests", direction: "desc"};
  const recentSort = {key: "id", direction: "desc"};
  let modelRows = [];

  function sortableHeaders(bodyId, keys, state, onSort) {
    const headers = el(bodyId).closest("table").querySelectorAll("thead th");
    const update = () => headers.forEach((th, i) => {
      th.setAttribute("aria-sort", keys[i] === state.key ? (state.direction === "asc" ? "ascending" : "descending") : "none");
      th.querySelector("button").textContent = th.dataset.label + (keys[i] === state.key ? (state.direction === "asc" ? " ↑" : " ↓") : " ↕");
    });
    headers.forEach((th, i) => {
      th.dataset.label = th.textContent.trim();
      const button = document.createElement("button");
      button.type = "button";
      button.className = "metric-sort-button";
      button.addEventListener("click", () => {
        state.direction = state.key === keys[i] && state.direction === "asc" ? "desc" : "asc";
        state.key = keys[i];
        update();
        onSort();
      });
      th.replaceChildren(button);
    });
    update();
  }
  // renderLive replaces the feed HTML every second. Keep disclosure state outside the DOM
  // so an open request/response trace does not collapse on every polling refresh.
  const traceOpenState = new Map();

  async function load() {
    const hours = rangeSel.value;
    let data;
    try {
      const res = await fetch(`/api/metrics?hours=${encodeURIComponent(hours)}`);
      data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    } catch (err) {
      el("kpis").innerHTML = `<p class="hint">Couldn't load metrics: ${esc(err.message)}</p>`;
      return;
    }
    renderKpis(data.totals);
    renderBars("by-source", data.by_source, "source");
    renderBars("by-backend", data.by_backend, "backend");
    renderBars("by-type", data.by_type, "type");
    renderSeries(data.series);
    renderModelReport(data.by_model || []);
  }

  function renderModelReport(rows) {
    modelRows = rows;
    rows = [...rows].sort((a, b) => {
      const x = a[modelSort.key], y = b[modelSort.key];
      if (x == null || y == null) return x == null ? (y == null ? 0 : 1) : -1;
      const comparison = typeof x === "number" ? x - y : String(x).localeCompare(String(y), undefined, {numeric: true, sensitivity: "base"});
      return modelSort.direction === "asc" ? comparison : -comparison;
    });
    const percent = n => n == null ? "—" : `${fmt(n)}%`;
    el("model-report").innerHTML = rows.length ? rows.map(r => `<tr>
      <td>${esc(r.model)}</td><td>${esc(r.backend)}</td>
      ${[r.requests, r.input_tokens, r.output_tokens, r.token_samples,
        r.avg_tps == null ? null : Math.round(r.avg_tps * 10) / 10, r.speed_samples].map(n => `<td class="num">${fmt(n)}</td>`).join("")}
      <td class="num">${r.avg_ms == null ? "—" : formatSeconds(r.avg_ms)}</td>
      ${[r.succeeded, r.client_errors, r.server_errors, r.unknown_status].map(n => `<td class="num">${fmt(n)}</td>`).join("")}
      <td class="num">${percent(r.error_rate)}</td><td class="num">${fmt(r.coding_requests)}</td>
      <td class="num">${fmt(r.coding_errors)}</td><td class="num">${percent(r.coding_accuracy)}</td>
      <td class="num">${fmt(r.reviewed)} / ${fmt(r.coding_requests)}</td>
    </tr>`).join("") : '<tr><td colspan="18" class="hint">No generation requests in this time range.</td></tr>';
  }

  // Recent-requests table: its own fetch/render cycle, separate from load() above, so
  // changing a filter or turning a page doesn't re-fetch/re-render the whole KPI dashboard
  // (and vice versa — the 15s auto-refresh below doesn't reset whatever page/filter the admin
  // is currently looking at). Real gap this replaces: the table used to be an unconditional
  // top-60 list that ignored the range selector entirely and had no way to see past row 60 or
  // narrow down to one backend/model/status while investigating something.
  function populateFilterOptions(selectEl, values, labelFn) {
    const current = selectEl.value;
    const opts = ['<option value="">' + selectEl.dataset.allLabel + '</option>']
      .concat((values || []).map((v) => `<option value="${esc(v)}">${esc(labelFn ? labelFn(v) : v)}</option>`));
    selectEl.innerHTML = opts.join("");
    if (values && values.includes(current)) selectEl.value = current;
  }

  async function loadRecent() {
    const params = new URLSearchParams({
      hours: rangeSel.value, page: String(page), page_size: String(PAGE_SIZE),
      sort: recentSort.key, direction: recentSort.direction,
    });
    if (fSource.value) params.set("source", fSource.value);
    if (fBackend.value) params.set("backend", fBackend.value);
    if (fType.value) params.set("request_type", fType.value);
    if (fStatus.value) params.set("status_class", fStatus.value);
    if (fModel.value.trim()) params.set("model", fModel.value.trim());

    let data;
    try {
      const res = await fetch(`/api/metrics/recent?${params}`);
      data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    } catch (err) {
      el("recent").innerHTML = `<tr><td colspan="13" class="hint">Couldn't load: ${esc(err.message)}</td></tr>`;
      return;
    }
    // Filter dropdowns reflect what's actually in the current time window, not a hardcoded
    // list — e.g. a backend nothing has hit yet in "Last hour" just won't show up as an option.
    populateFilterOptions(fSource, data.filters.sources, label);
    populateFilterOptions(fBackend, data.filters.backends);
    populateFilterOptions(fType, data.filters.request_types);

    renderRecent(data.rows);
    renderPagination(data);
  }

  function renderPagination(data) {
    const { total, page: p, total_pages, page_size } = data;
    page = p;
    const start = total === 0 ? 0 : (p - 1) * page_size + 1;
    const end = Math.min(total, p * page_size);
    el("recent-count").textContent = total ? `Showing ${fmt(start)}–${fmt(end)} of ${fmt(total)}` : "No matching requests.";
    el("page-indicator").textContent = `Page ${p} of ${total_pages}`;
    el("page-prev").disabled = p <= 1;
    el("page-next").disabled = p >= total_pages;
  }

  function kpi(label, value, sub) {
    return `<div class="metric-kpi"><div class="metric-kpi-val">${value}</div>` +
           `<div class="metric-kpi-label">${esc(label)}</div>` +
           (sub ? `<div class="metric-kpi-sub">${esc(sub)}</div>` : "") + `</div>`;
  }
  function renderKpis(t) {
    el("kpis").innerHTML =
      kpi("Requests", fmt(t.requests)) +
      kpi("Tokens generated", fmt(t.output_tokens), `${fmt(t.total_tokens)} total`) +
      kpi("Avg response", t.avg_ms != null ? `${formatSeconds(t.avg_ms)} s` : "—", t.p95_ms != null ? `p95 ${formatSeconds(t.p95_ms)} s` : "") +
      kpi("Avg throughput", t.avg_tps != null ? `${fmt(t.avg_tps)} tok/s` : "—") +
      kpi("Avg NPU TOPS", t.avg_tops != null ? fmt(t.avg_tops) : "—", "when NPU-served") +
      kpi("NPU-served", fmt(t.npu_requests), t.pct_npu != null ? `${t.pct_npu}% of known` : "—");
  }

  function renderBars(id, rows, kind) {
    const wrap = el(id);
    if (!rows || !rows.length) { wrap.innerHTML = `<p class="hint">No data.</p>`; return; }
    const max = Math.max(...rows.map((r) => r.n));
    wrap.innerHTML = rows.map((r) => {
      const pct = max ? Math.round((r.n / max) * 100) : 0;
      const name = kind === "source" ? label(r.k) : r.k;
      const avg = r.avg_ms != null ? `${formatSeconds(r.avg_ms)}s` : "";
      return `<div class="metric-bar-row" title="${esc(name)}: ${r.n} requests${avg ? ", avg " + avg : ""}">
        <span class="metric-bar-name">${esc(name)}</span>
        <span class="metric-bar-track"><span class="metric-bar-fill src-${esc(kind === 'source' ? r.k : '')}" style="width:${pct}%"></span></span>
        <span class="metric-bar-val">${fmt(r.n)}${avg ? ` <span class="hint">· ${avg}</span>` : ""}</span>
      </div>`;
    }).join("");
  }

  function renderSeries(series) {
    const wrap = el("series");
    if (!series || !series.length) { wrap.innerHTML = `<p class="hint">No requests in this window yet.</p>`; return; }
    const max = Math.max(...series.map((s) => s.n));
    let totalTok = 0;
    const bars = series.map((s) => {
      totalTok += s.tokens || 0;
      const h = max ? Math.max(3, Math.round((s.n / max) * 100)) : 3;
      const t = new Date(s.bucket * 1000);
      const lbl = t.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit" });
      return `<div class="metric-tl-bar" title="${esc(lbl)}: ${s.n} req, ${fmt(s.tokens)} tokens">
        <span class="metric-tl-fill" style="height:${h}%"></span></div>`;
    }).join("");
    wrap.innerHTML = `<div class="metric-tl-bars">${bars}</div>`;
    el("series-tokens").textContent = `· ${fmt(totalTok)} tokens in window`;
  }

  function renderRecent(rows) {
    const tb = el("recent");
    if (!rows || !rows.length) { tb.innerHTML = `<tr><td colspan="19" class="hint">No requests recorded yet.</td></tr>`; return; }
    tb.innerHTML = rows.map((r) => {
      const t = new Date(r.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
      const statusCls = r.status >= 500 ? "st-err" : r.status >= 400 ? "st-warn" : "st-ok";
      return `<tr>
        <td class="num">${r.id != null ? fmt(r.id) : "—"}</td>
        <td class="h-time">${esc(t)}</td>
        <td><span class="metric-src src-${esc(r.source)}">${esc(label(r.source))}</span></td>
        <td>${esc(r.backend || "—")}</td>
        <td class="metric-model" title="${esc(r.model || "")}">${esc(r.model || "—")}</td>
        <td>${esc(r.request_type || "—")}</td>
        <td>${esc(r.ask_category || "—")}</td>
        <td class="num">${r.duration_ms != null ? formatSeconds(r.duration_ms) : "—"}</td>
        <td class="num">${r.prompt_tokens != null ? fmt(r.prompt_tokens) : "—"}</td>
        <td class="num">${r.completion_tokens != null ? fmt(r.completion_tokens) : "—"}</td>
        <td class="num">${r.total_tokens != null ? fmt(r.total_tokens) : "—"}</td>
        <td class="num">${r.tokens_per_sec != null ? fmt(r.tokens_per_sec) : "—"}</td>
        <td class="num">${r.tops_used != null ? fmt(r.tops_used) : "—"}</td>
        <td class="num">${r.npu_percent != null ? fmt(r.npu_percent) : "—"}</td>
        <td>${r.is_npu == null ? "—" : (r.is_npu ? "⚡ Yes" : "No")}</td>
        <td class="num ${statusCls}">${r.status != null ? r.status : "—"}</td>
        <td class="metric-ip">${esc(r.client_ip || "—")}</td>
        <td>${esc(r.user_id || "—")}</td>
        <td>${r.ask_category === "code" && ["chat_completion", "completion"].includes(r.request_type) ? `<select class="panel-input coding-review" data-request-id="${r.id}" aria-label="Coding review for request ${r.id}">
          <option value="" ${r.coding_passed == null ? "selected" : ""}>Not reviewed</option>
          <option value="pass" ${r.coding_passed === 1 ? "selected" : ""}>Pass</option>
          <option value="fail" ${r.coding_passed === 0 ? "selected" : ""}>Fail</option>
        </select>` : "—"}</td>
      </tr>`;
    }).join("");
  }

  el("recent").addEventListener("change", async event => {
    const select = event.target.closest(".coding-review");
    if (!select) return;
    select.disabled = true;
    try {
      const res = await fetch(`/api/metrics/requests/${select.dataset.requestId}/coding-review`, {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({passed: select.value === "" ? null : select.value === "pass"}),
      });
      if (!res.ok) throw new Error((await res.json()).error || `HTTP ${res.status}`);
      load();
      select.title = "Review saved";
    } catch (err) {
      alert(`Could not save coding review: ${err.message}`);
      loadRecent();
    } finally { select.disabled = false; }
  });

  // Live activity feed: LM-Studio-dev-log-style view of generations streaming right now -
  // webui chat, plus OpenCode/Continue traffic through Lemonade/FastFlowLM (their LAN shims
  // report it directly, see live_report.py); LM Studio/vLLM/KoboldCPP LAN traffic still can't
  // appear (see live_requests.py). Polled on its own faster interval since it's meant to feel
  // live, independent of the 15s KPI/table refresh and unaffected by the range selector or
  // Recent-requests filters.
  function renderLive(rows) {
    const wrap = el("live-feed");
    const activeCount = rows.filter((r) => r.status === "streaming").length;
    el("live-count").textContent = `· ${activeCount} active · ${rows.length} shown`;
    // "Busy" means an actual currently-streaming (not just-finished) row is NPU-served —
    // derived straight from the same rows this feed already renders, no extra request.
    const npuBusyRow = rows.find((r) => r.status === "streaming" && r.is_npu);
    el("npu-status").innerHTML = npuBusyRow
      ? `<span class="metric-npu-badge metric-npu-busy" title="${esc(npuBusyRow.model || "")}">⚡ NPU busy</span>`
      : `<span class="metric-npu-badge metric-npu-idle">NPU idle</span>`;
    if (!rows.length) {
      wrap.innerHTML = `<div class="metric-dev-empty">Waiting for inference traffic…</div>`;
      return;
    }
    wrap.innerHTML = rows.map((r) => {
      const done = r.status !== "streaming";
      const timestamp = new Date((r.finished_at || r.started_at || 0) * 1000)
        .toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
      const level = r.status === "error" ? "ERROR" : done ? "DONE " : "INFO ";
      const stats = [
        `${fmt(r.tokens)} tok`,
        r.tokens_per_sec != null ? `${r.tokens_per_sec} tok/s` : null,
        r.ttft_s != null ? `TTFT ${r.ttft_s}s` : null,
        `${r.elapsed_s}s`,
        r.pct != null ? `${r.pct}% max` : null,
      ].filter(Boolean).join(" · ");
      return `<div class="metric-dev-line dev-${esc(r.status)}">
        <span class="dev-time">${esc(timestamp)}</span>
        <span class="dev-level">${level}</span>
        <span class="dev-id">req#${fmt(r.id)}</span>
        <span class="dev-client">${esc(label(r.source))}</span>
        <span class="dev-route">${esc(r.backend || "unknown")} / ${esc(r.model || "unknown")}</span>
        ${r.is_npu ? `<span class="dev-npu">NPU</span>` : ""}
        <span class="dev-event" title="${esc(r.error_detail || "")}">${done ? (r.status === "error" ? `request failed${r.status_code ? ` (HTTP ${r.status_code})` : ""}` : "generation completed") : "streaming"}</span>
        <span class="dev-stats">${esc(stats)}</span>
        ${r.error_detail ? `<span class="dev-error-detail">${esc(r.error_detail)}</span>` : ""}
        ${(r.request_content || r.response_content) ? `<details class="dev-trace" data-request-id="${esc(r.id)}" ${traceOpenState.get(String(r.id)) === true ? "open" : ""}>
          <summary>View request and response</summary>
          ${r.request_content ? `<div class="dev-trace-panel"><div class="dev-trace-heading"><span>REQUEST</span><button type="button" class="dev-trace-copy">Copy</button></div>${traceBody(r.request_content, true)}</div>` : ""}
          ${r.response_content ? `<div class="dev-trace-panel"><div class="dev-trace-heading"><span>RESPONSE${r.response_truncated ? " · first 4,000 characters" : ""}</span><button type="button" class="dev-trace-copy">Copy</button></div>${traceBody(r.response_content)}</div>` : ""}
        </details>` : ""}
      </div>`;
    }).join("");
    wrap.querySelectorAll(".dev-trace-copy").forEach((button) => {
      button.addEventListener("click", async () => {
        const text = [...button.closest(".dev-trace-panel").querySelectorAll("pre")]
          .map((pre) => pre.textContent).join("\n\n");
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
        setTimeout(() => { button.textContent = "Copy"; }, 1200);
      });
    });
    wrap.querySelectorAll(".dev-trace").forEach((details) => {
      details.addEventListener("toggle", () => {
        traceOpenState.set(details.dataset.requestId, details.open);
      });
    });
  }

  let liveInFlight = false;
  let liveRetryMs = 1000;
  let liveHasLoaded = false;
  async function loadLive() {
    if (liveInFlight) return;
    liveInFlight = true;
    try {
      const res = await fetch("/api/metrics/live");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      renderLive(data.rows || []);
      liveHasLoaded = true;
      liveRetryMs = 1000;
      el("live-refresh-error")?.remove();
    } catch (err) {
      liveRetryMs = Math.min(liveRetryMs * 2, 30000);
      let notice = el("live-refresh-error");
      if (!notice) {
        notice = document.createElement("p");
        notice.id = "live-refresh-error";
        notice.className = "hint";
        el("live-feed").before(notice);
      }
      const reason = ["TimeoutError", "AbortError"].includes(err.name)
        ? "The refresh timed out" : `Refresh failed: ${err.message}`;
      notice.textContent = `${reason}. ${liveHasLoaded ? "Showing the last loaded logs. " : ""}Retrying in ${liveRetryMs / 1000}s.`;
    } finally {
      liveInFlight = false;
      setTimeout(loadLive, liveRetryMs);
    }
  }

  if (el("live-feed")) {
    loadLive();
    return;
  }

  function refreshAll() {
    load();
    loadRecent();
  }
  // Changing the time range affects both the KPI dashboard and the table -- and since the
  // table's own page numbering is only meaningful within a given filter/range, jumping back
  // to page 1 avoids landing on a now out-of-range page (e.g. page 5 of a wider window
  // suddenly being past the last page of a much narrower one).
  function refreshRecentFromPage1() {
    page = 1;
    loadRecent();
  }

  el("refresh").addEventListener("click", refreshAll);
  el("metrics-reset").addEventListener("click", async () => {
    if (!confirm("Warning 1 of 2: This permanently deletes all saved Metrics request history and coding reviews for every source and model. Continue?")) return;
    if (!confirm("Warning 2 of 2: Reset all performance metrics to zero now? New requests will start accumulating again immediately.")) return;
    const button = el("metrics-reset");
    button.disabled = true;
    try {
      const res = await fetch("/api/metrics/reset", {
        method: "POST",
        headers: {"X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content},
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      page = 1;
      rangeSel.value = "0";
      fSource.value = ""; fBackend.value = ""; fType.value = ""; fStatus.value = ""; fModel.value = "";
      el("metrics-reset-status").textContent = "All saved performance metrics were reset. New requests will be recorded from now on.";
      refreshAll();
    } catch (err) {
      el("metrics-reset-status").textContent = `Could not reset metrics: ${err.message}`;
    } finally {
      button.disabled = false;
    }
  });
  rangeSel.addEventListener("change", () => { page = 1; refreshAll(); });
  [fSource, fBackend, fType, fStatus].forEach((elm) => elm.addEventListener("change", refreshRecentFromPage1));
  fModel.addEventListener("input", () => {
    clearTimeout(fModel._debounce);
    fModel._debounce = setTimeout(refreshRecentFromPage1, 300);
  });
  el("f-clear").addEventListener("click", () => {
    fSource.value = ""; fBackend.value = ""; fType.value = ""; fStatus.value = ""; fModel.value = "";
    refreshRecentFromPage1();
  });
  el("page-prev").addEventListener("click", () => { if (page > 1) { page -= 1; loadRecent(); } });
  el("page-next").addEventListener("click", () => { page += 1; loadRecent(); });
  // The app shell locks window/body scrolling; .console-body is the real vertical
  // scroller on admin pages. Targeting window here was why the arrows appeared inert.
  const pageScroller = document.querySelector(".console-body");
  el("metrics-scroll-top").addEventListener("click", () => {
    pageScroller.scrollTo({ top: 0, behavior: "smooth" });
  });
  el("metrics-scroll-bottom").addEventListener("click", () => {
    pageScroller.scrollTo({ top: pageScroller.scrollHeight, behavior: "smooth" });
  });

  sortableHeaders("model-report", ["model", "backend", "requests", "input_tokens", "output_tokens", "token_samples",
    "avg_tps", "speed_samples", "avg_ms", "succeeded", "client_errors", "server_errors", "unknown_status",
    "error_rate", "coding_requests", "coding_errors", "coding_accuracy", "reviewed"], modelSort, () => renderModelReport(modelRows));
  sortableHeaders("recent", ["id", "ts", "source", "backend", "model", "request_type", "ask_category", "duration_ms",
    "prompt_tokens", "completion_tokens", "total_tokens", "tokens_per_sec", "tops_used", "npu_percent", "is_npu",
    "status", "client_ip", "user_id", "coding_passed"], recentSort, () => { page = 1; loadRecent(); });
  refreshAll();
  setInterval(refreshAll, 15000); // KPIs/table: no need for anything faster
})();
