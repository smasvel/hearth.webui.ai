// AI Browser History page: searchable, paginated table of every page the AI's
// headless browser visited. "Live" search = debounced re-query as you type (300ms),
// resetting to page 1 — server-side filtering via /api/browse-history so search
// works across the whole history, not just the currently loaded page of rows.
(() => {
  const searchEl = document.getElementById("history-search");
  const rowsEl = document.getElementById("history-rows");
  const emptyEl = document.getElementById("history-empty");
  const countEl = document.getElementById("history-count");
  const prevBtn = document.getElementById("history-prev");
  const nextBtn = document.getElementById("history-next");
  const pageLabel = document.getElementById("history-page-label");

  const PER_PAGE = 25;
  let page = 1;
  let totalPages = 1;
  let debounceTimer = null;
  // Guards against out-of-order responses: fast typing fires overlapping fetches,
  // and an older, slower response landing after a newer one would repaint the
  // table with stale rows.
  let fetchSeq = 0;

  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function esc(s) {
    const div = document.createElement("div");
    div.textContent = s ?? "";
    return div.innerHTML;
  }

  async function load() {
    const seq = ++fetchSeq;
    const params = new URLSearchParams({ page: String(page), per_page: String(PER_PAGE) });
    const q = searchEl.value.trim();
    if (q) params.set("q", q);
    let data;
    try {
      const res = await fetch(`/api/browse-history?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      data = await res.json();
    } catch (err) {
      if (seq !== fetchSeq) return;
      rowsEl.innerHTML = "";
      emptyEl.textContent = `Failed to load history: ${err.message}`;
      emptyEl.classList.remove("hidden");
      return;
    }
    if (seq !== fetchSeq) return;

    totalPages = Math.max(1, Math.ceil(data.total / data.per_page));
    if (page > totalPages) { page = totalPages; load(); return; }

    countEl.textContent = q
      ? `${data.total} match${data.total === 1 ? "" : "es"}`
      : `${data.total} visit${data.total === 1 ? "" : "s"} recorded`;
    pageLabel.textContent = `Page ${data.page} of ${totalPages}`;
    prevBtn.disabled = data.page <= 1;
    nextBtn.disabled = data.page >= totalPages;

    rowsEl.innerHTML = "";
    emptyEl.classList.toggle("hidden", data.entries.length > 0);
    emptyEl.textContent = q ? "No matches." : "No visits recorded yet.";

    for (const e of data.entries) {
      const tr = document.createElement("tr");
      if (!e.ok) tr.className = "h-fail";
      const title = e.title || e.url;
      const source = e.source || "unknown";
      tr.innerHTML = `
        <td class="h-time">${fmtTime(e.visited_at)}</td>
        <td class="h-url">
          <a href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">${esc(title)}</a>
          ${e.title ? `<span class="h-link">${esc(e.url)}</span>` : ""}
        </td>
        <td>${esc(e.query || "—")}</td>
        <td><span class="history-badge" data-source="${esc(source)}">${esc(source)}</span></td>
        <td><span class="status-dot ${e.ok ? "ok" : "fail"}"></span>${e.ok ? "ok" : "failed"}</td>
      `;
      rowsEl.appendChild(tr);
    }
  }

  searchEl.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => { page = 1; load(); }, 300);
  });
  prevBtn.addEventListener("click", () => { if (page > 1) { page--; load(); } });
  nextBtn.addEventListener("click", () => { if (page < totalPages) { page++; load(); } });

  load();
})();
