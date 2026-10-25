// Live developer request feed. Kept separate from the metrics dashboard so each page
// initializes only the polling and rendering it needs.
(() => {
  const feed = document.getElementById("live-feed");
  const count = document.getElementById("live-count");
  const npu = document.getElementById("npu-status");
  let retryMs = 1000;
  let loaded = false;
  const traceCache = new Map();
  const traceOpenState = new Map();

  const addText = (parent, className, value) => {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = value ?? "";
    parent.appendChild(span);
    return span;
  };

  function render(rows) {
    const active = rows.filter(row => row.status === "streaming").length;
    count.textContent = `· ${active} active · ${rows.length} shown`;
    const busy = rows.find(row => row.status === "streaming" && row.is_npu);
    npu.innerHTML = busy
      ? `<span class="metric-npu-badge metric-npu-busy">⚡ NPU busy</span>`
      : `<span class="metric-npu-badge metric-npu-idle">NPU idle</span>`;
    feed.replaceChildren();
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "metric-dev-empty";
      empty.textContent = "Waiting for inference traffic…";
      feed.appendChild(empty);
      return;
    }
    for (const row of rows) {
      const done = row.status !== "streaming";
      const line = document.createElement("div");
      line.className = `metric-dev-line dev-${row.status}`;
      const time = new Date((row.finished_at || row.started_at || 0) * 1000)
        .toLocaleTimeString([], {hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit"});
      addText(line, "dev-time", time);
      addText(line, "dev-level", row.status === "error" ? "ERROR" : done ? "DONE " : "INFO ");
      addText(line, "dev-id", `req#${row.id ?? "—"}`);
      addText(line, "dev-client", row.source || "unknown");
      addText(line, "dev-route", `${row.backend || "unknown"} / ${row.model || "unknown"}`);
      if (row.is_npu) addText(line, "dev-npu", "NPU");
      addText(line, "dev-event", done ? (row.status === "error" ? "request failed" : "generation completed") : "streaming");
      const stats = [`${row.tokens ?? 0} tok`, row.tokens_per_sec != null ? `${row.tokens_per_sec} tok/s` : null,
        row.ttft_s != null ? `TTFT ${row.ttft_s}s` : null, `${row.elapsed_s ?? 0}s`,
        row.pct != null ? `${row.pct}% max` : null].filter(Boolean).join(" · ");
      addText(line, "dev-stats", stats);
      if (row.error_detail) addText(line, "dev-error-detail", row.error_detail);
      if (row.id != null) {
        const details = document.createElement("details");
        details.className = "dev-trace";
        details.open = traceOpenState.get(String(row.id)) === true;
        const summary = document.createElement("summary");
        summary.textContent = "View request and response";
        const panel = document.createElement("div");
        panel.className = "dev-trace-panel";
        details.append(summary, panel);
        const drawTrace = (trace) => {
          panel.replaceChildren();
          for (const [heading, value] of [["REQUEST", trace.request_content], ["RESPONSE", trace.response_content]]) {
            if (!value) continue;
            addText(panel, "dev-trace-heading", heading + (heading === "RESPONSE" && trace.response_truncated ? " · first 4,000 characters" : ""));
            const pre = document.createElement("pre");
            pre.textContent = value;
            panel.appendChild(pre);
          }
          if (!panel.childElementCount) addText(panel, "hint", "No request or response text was captured.");
        };
        if (traceCache.has(row.id)) drawTrace(traceCache.get(row.id));
        else addText(panel, "hint", "Open to load the request and response text.");
        details.addEventListener("toggle", async () => {
          traceOpenState.set(String(row.id), details.open);
          if (!details.open || traceCache.has(row.id) || details.dataset.loading) return;
          details.dataset.loading = "true";
          panel.replaceChildren();
          addText(panel, "hint", "Loading request trace…");
          try {
            const response = await fetch(`/api/metrics/live/${encodeURIComponent(row.id)}/trace`);
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
            traceCache.set(row.id, data);
            drawTrace(data);
          } catch (error) {
            panel.replaceChildren();
            addText(panel, "dev-error-detail", `Could not load trace: ${error.message}`);
            delete details.dataset.loading;
          }
        });
        line.appendChild(details);
      }
      feed.appendChild(line);
    }
  }

  async function poll() {
    try {
      const response = await fetch("/api/metrics/live");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      render(data.rows || []);
      loaded = true;
      retryMs = 1000;
      document.getElementById("live-refresh-error")?.remove();
    } catch (error) {
      retryMs = Math.min(retryMs * 2, 30000);
      let notice = document.getElementById("live-refresh-error");
      if (!notice) {
        notice = document.createElement("p");
        notice.id = "live-refresh-error";
        notice.className = "hint";
        feed.before(notice);
      }
      notice.textContent = `Refresh failed: ${error.message}. ${loaded ? "Showing the last loaded logs. " : ""}Retrying in ${retryMs / 1000}s.`;
    }
    setTimeout(poll, retryMs);
  }

  poll();
})();
