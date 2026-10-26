(() => {
  "use strict";

  const searchForm = document.getElementById("catalog-search-form");
  const searchInput = document.getElementById("catalog-search-input");
  const resultsEl = document.getElementById("catalog-results");
  const allSearchForm = document.getElementById("all-search-form");
  const allSearchInput = document.getElementById("all-search-input");
  const allResultsEl = document.getElementById("all-results");

  const vllmSearchForm = document.getElementById("vllm-search-form");
  const vllmSearchInput = document.getElementById("vllm-search-input");
  const vllmResultsEl = document.getElementById("vllm-results");

  const lmForm = document.getElementById("lmstudio-download-form");
  const lmQueryEl = document.getElementById("lmstudio-download-query");
  const lmBtn = document.getElementById("lmstudio-download-btn");

  const lmSearchForm = document.getElementById("lmstudio-search-form");
  const lmSearchInput = document.getElementById("lmstudio-search-input");
  const lmResultsEl = document.getElementById("lmstudio-results");

  const koboldSearchForm = document.getElementById("kobold-search-form");
  const koboldSearchInput = document.getElementById("kobold-search-input");
  const koboldResultsEl = document.getElementById("kobold-results");

  const npuSearchForm = document.getElementById("npu-search-form");
  const npuSearchInput = document.getElementById("npu-search-input");
  const npuResultsEl = document.getElementById("npu-results");

  const progressWrap = document.getElementById("download-progress");
  const progressTitle = document.getElementById("download-progress-title");
  const progressPct = document.getElementById("download-progress-pct");
  const progressBar = document.getElementById("download-progress-bar");
  const progressDetail = document.getElementById("download-progress-detail");
  const progressLog = document.getElementById("download-progress-log");
  const cancelBtn = document.getElementById("download-cancel-btn");

  // Production may retain an older Jinja template until the server restarts.
  // Keep the versioned script compatible with that page during active downloads.
  let toolFilterEls = [...document.querySelectorAll("[data-tools-backend]")];
  if (!toolFilterEls.length) {
    const filter = document.createElement("div");
    filter.className = "download-tool-filters";
    filter.innerHTML = '<strong>Tool calling filters:</strong>' +
      [['lemonade','Lemonade'],['vllm','vLLM'],['lmstudio','LM Studio'],['kobold','KoboldCPP'],['fastflowlm','FastFlowLM']]
        .map(([key,label]) => `<label><input data-tools-backend="${key}" type="checkbox" /> ${label}</label>`).join("") +
      '<strong>Model size:</strong>' +
      [['small','Small (up to 4B)'],['medium','Medium (over 4B to 13B)'],['large','Large (over 13B)']]
        .map(([key,label]) => `<label><input data-parameter-band="${key}" type="checkbox" /> ${label}</label>`).join("");
    document.querySelector(".console-head").appendChild(filter);
    toolFilterEls = [...filter.querySelectorAll("[data-tools-backend]")];
  }
  const searchResults = new Map();
  const parameterFilterEls = [...document.querySelectorAll("[data-parameter-band]")];
  // Browsers may restore checkbox state after a reload. Start a fresh page view with
  // unfiltered catalogs so an empty Search all submission really shows every model.
  for (const filter of [...toolFilterEls, ...parameterFilterEls]) filter.checked = false;
  for (const filter of [...toolFilterEls, ...parameterFilterEls]) filter.addEventListener("change", () => {
    for (const [container, cached] of searchResults) {
      renderResults(cached.results, { container, onDownload: cached.onDownload });
    }
  });

  let busy = false;
  let currentDownload = null;

  function trackDownload(backend, modelId) {
    currentDownload = { backend, model_id: modelId };
    cancelBtn.classList.toggle("hidden", !["lmstudio", "vllm", "fastflowlm"].includes(backend));
    cancelBtn.disabled = false;
  }

  function finishTracking() {
    currentDownload = null;
    cancelBtn.classList.add("hidden");
    cancelBtn.disabled = false;
  }

  cancelBtn.addEventListener("click", async () => {
    if (!currentDownload || cancelBtn.disabled) return;
    if (!window.confirm(`Cancel ${currentDownload.model_id} and delete its partial download files?`)) return;
    cancelBtn.disabled = true;
    progressDetail.textContent = "Cancelling and clearing partial files…";
    try {
      const res = await fetch("/api/models/downloads/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(currentDownload),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      progressDetail.textContent =
        `Cancelled. Cleared ${data.files_removed} partial file(s) (${formatBytes(data.bytes_removed)}).`;
      finishTracking();
      busy = false;
    } catch (err) {
      progressDetail.textContent = `Cancellation failed: ${err.message}`;
      cancelBtn.disabled = false;
    }
  });

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function formatSize(gb) {
    if (gb == null) return "size unknown";
    if (gb < 1) return `${Math.round(gb * 1024)} MB`;
    return `${gb.toFixed(1)} GB`;
  }

  function formatBytes(n) {
    if (n == null) return "";
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(0)} MB`;
    return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
  }

  function startProgress(title, { showLog = false, indeterminate = false } = {}) {
    progressWrap.classList.remove("hidden");
    progressTitle.textContent = title;
    progressBar.style.width = indeterminate ? "100%" : "0%";
    progressPct.textContent = indeterminate ? "" : "0%";
    progressDetail.textContent = "";
    progressLog.textContent = "";
    progressLog.classList.toggle("hidden", !showLog);
  }

  // Consumes this app's `data: {...}\n\n` SSE convention from a fetch() stream (used by
  // both download endpoints) — not EventSource, since these are POST requests.
  async function streamSse(response, onPayload) {
    if (!response.body) return;
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        let payload;
        try {
          payload = JSON.parse(line.slice(5).trim());
        } catch {
          continue;
        }
        onPayload(payload);
      }
    }
  }

  async function searchCatalog(query) {
    resultsEl.innerHTML = `<p class="hint">Searching&hellip;</p>`;
    try {
      const res = await fetch(`/api/models/search?backend=lemonade&q=${encodeURIComponent(query)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "search failed");
      renderResults(data.results || [], { container: resultsEl, onDownload: downloadCatalogModel });
    } catch (err) {
      resultsEl.innerHTML = `<p class="hint">Could not search: ${escapeHtml(err.message)}</p>`;
    }
  }

  async function searchAll(query) {
    allResultsEl.innerHTML = `<p class="hint">Searching all backends&hellip;</p>`;
    try {
      const res = await fetch(`/api/models/search?backend=all&q=${encodeURIComponent(query)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "search failed");
      if (!data.results?.length && data.backend_errors?.length) {
        allResultsEl.innerHTML = `<p class="hint">No models could be loaded. Backend search failed: ${data.backend_errors.map(escapeHtml).join(", ")}.</p>`;
        return;
      }
      renderResults(data.results || [], {
        container: allResultsEl,
        onDownload: downloadAllBackendModel,
        backendErrors: data.backend_errors || [],
      });
      if (data.results?.length && data.backend_errors?.length) {
        const note = document.createElement("p");
        note.className = "hint";
        note.textContent = `Some backends could not be searched: ${data.backend_errors.join(", ")}.`;
        allResultsEl.prepend(note);
      }
    } catch (err) {
      allResultsEl.innerHTML = `<p class="hint">Could not search: ${escapeHtml(err.message)}</p>`;
    }
  }

  async function searchVllm(query) {
    // Empty query returns vLLM's curated suggestions (server-side) rather than a blank
    // prompt — a vetted list of models that actually load on vLLM (full HF repos, not
    // GGUF), shown first the way Lemonade's catalog is. A real query does a live HF
    // search. Both render as the same cards ("staff pick" + description marks the
    // suggested ones).
    vllmResultsEl.innerHTML = query
      ? `<p class="hint">Searching&hellip;</p>`
      : `<p class="hint">Loading suggested models&hellip;</p>`;
    try {
      const res = await fetch(`/api/models/search?backend=vllm&q=${encodeURIComponent(query)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "search failed");
      renderResults(data.results || [], { container: vllmResultsEl, onDownload: downloadVllmModel });
    } catch (err) {
      vllmResultsEl.innerHTML = `<p class="hint">Could not load: ${escapeHtml(err.message)}</p>`;
    }
  }

  // Generic backend search → cards. Used by LM Studio / KoboldCPP (both search the HF
  // GGUF index server-side) and FastFlowLM (its own NPU catalog). Empty query browses each
  // backend's available catalog/popular Hugging Face results.
  async function searchBackend(query, { backend, container, input, onDownload, browseHint, emptyIsCatalog }) {
    container.innerHTML = `<p class="hint">Searching&hellip;</p>`;
    try {
      const res = await fetch(`/api/models/search?backend=${backend}&q=${encodeURIComponent(query)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "search failed");
      renderResults(data.results || [], { container, onDownload });
    } catch (err) {
      container.innerHTML = `<p class="hint">Could not search: ${escapeHtml(err.message)}</p>`;
    }
  }

  const searchLmstudio = (q) => searchBackend(q, {
    backend: "lmstudio", container: lmResultsEl, input: lmSearchInput,
    onDownload: (id, _src, btn, sizeGb) => downloadFreeformCard(id, "lmstudio", btn, () => searchLmstudio(lmSearchInput.value.trim()), sizeGb),
    browseHint: "Search Hugging Face for a GGUF model to download.",
  });
  const searchKobold = (q) => searchBackend(q, {
    backend: "kobold", container: koboldResultsEl, input: koboldSearchInput,
    onDownload: (id, _src, btn, sizeGb) => downloadStructuredCard(id, "kobold", btn, () => searchKobold(koboldSearchInput.value.trim()), sizeGb),
    browseHint: "Search Hugging Face for a GGUF model to download.",
  });
  const searchFastflow = (q) => searchBackend(q, {
    backend: "fastflowlm", container: npuResultsEl, input: npuSearchInput, emptyIsCatalog: true,
    onDownload: (id, _src, btn, sizeGb) => downloadStructuredCard(id, "fastflowlm", btn, () => searchFastflow(npuSearchInput.value.trim()), sizeGb),
    browseHint: "Search FastFlowLM's NPU catalog.",
  });

  function formatDate(iso) {
    if (!iso) return null;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function formatCount(n) {
    if (n == null) return null;
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
    return `${n}`;
  }

  function renderResults(results, { container, onDownload, backendErrors = [] }) {
    searchResults.set(container, { results, onDownload });
    const activeFilters = new Set(toolFilterEls.filter((el) => el.checked).map((el) => el.dataset.toolsBackend));
    results = results.filter((r) => !activeFilters.has(r.backend) || r.supports_tool_calling === true);
    const activeBands = new Set(parameterFilterEls.filter((el) => el.checked).map((el) => el.dataset.parameterBand));
    if (activeBands.size) {
      results = results.filter((r) => {
        const count = parameterCountBillions(r);
        if (count == null) return false;
        const band = count <= 4 ? "small" : count <= 13 ? "medium" : "large";
        return activeBands.has(band);
      });
    }
    if (results.length === 0) {
      const detail = searchResults.get(container).results.length
        ? "No models match the selected filters. Clear or adjust the tool calling and model size filters."
        : backendErrors.length
          ? `No models returned. Backend search failed: ${backendErrors.map(escapeHtml).join(", ")}.`
          : "No models were returned by the available catalogs.";
      container.innerHTML = `<p class="hint">${detail}</p>`;
      return;
    }
    container.innerHTML = "";
    for (const r of results) {
      const card = document.createElement("div");
      card.className = "model-card";

      // Every value here can come from an external API (Hugging Face) — escaped
      // individually before joining, not trusted as safe HTML.
      const metaParts = [];
      if (r.supports_tool_calling === true) metaParts.push("supports tool calling");
      if (r.backend_label) metaParts.push(r.backend_label);
      // Size is always shown — the backend fills a real download size for every result
      // (default GGUF quant, or full repo); formatSize renders null as "size unknown".
      metaParts.push(formatSize(r.size_gb));
      const type = r.pipeline_tag || (r.labels || [])[0];
      if (type) metaParts.push(type);
      const updated = formatDate(r.last_modified);
      if (updated) metaParts.push(`updated ${updated}`);
      if (r.likes != null) metaParts.push(`★ ${formatCount(r.likes)}`);
      if (r.downloads != null) metaParts.push(`${formatCount(r.downloads)} downloads`);
      if (r.source === "huggingface") metaParts.push("Hugging Face");
      if (r.format) metaParts.push(r.format);
      if (r.compatibility) metaParts.push(r.compatibility);
      const contextTokens = Number(r.max_context_length || r.context_window_tokens || r.context_length || r.max_position_embeddings || 0);
      if (Number.isSafeInteger(contextTokens) && contextTokens > 0) metaParts.push(`max ${contextTokens.toLocaleString()} tokens`);
      const metaHtml = metaParts.map(escapeHtml).join(" &middot; ");

      const descHtml = r.description
        ? `<div class="model-desc">${escapeHtml(r.description)}</div>`
        : "";

      // "staff pick" is now a distinct visible badge, not buried as plain text inside the
      // comma-separated meta line — the whole point of flagging a suggested/curated model is
      // to make it easy to spot at a glance for someone who doesn't know what to pick yet.
      const pickBadge = r.suggested ? `<span class="badge staff-pick">staff pick</span>` : "";
      const toolSupport = r.supports_tool_calling === true
        ? "yes"
        : r.supports_tool_calling === false ? "no" : "unknown";
      const toolBadge = `<span class="badge" title="${r.supports_tool_calling === true || r.supports_tool_calling === false ? "Reported by model catalog metadata." : "The catalog does not report tool calling support."}">tool calling: ${toolSupport}</span>`;
      card.innerHTML = `
        <div class="model-card-top">
          <div>
            <div class="model-name">${escapeHtml(r.id || "")}</div>
            <div class="model-meta">${metaHtml}</div>
            ${descHtml}
          </div>
          <div class="model-card-badges">
            ${pickBadge}
            ${toolBadge}
            <span class="badge ${r.downloaded ? "loaded" : "not-loaded"}">${r.downloaded ? "downloaded" : "not downloaded"}</span>
          </div>
        </div>
        <div class="model-card-actions"></div>
      `;
      const actions = card.querySelector(".model-card-actions");
      if (r.source_url) {
        const sourceLink = document.createElement("a");
        sourceLink.href = r.source_url;
        sourceLink.target = "_blank";
        sourceLink.rel = "noopener noreferrer";
        sourceLink.className = "panel-btn secondary";
        sourceLink.textContent = "Model source";
        actions.appendChild(sourceLink);
      }
      if (r.downloaded) {
        const note = document.createElement("span");
        note.className = "hint";
        note.textContent = "Already downloaded — manage it from the Models panel.";
        actions.appendChild(note);
      } else {
        const btn = document.createElement("button");
        btn.textContent = "Download";
        btn.addEventListener("click", () => onDownload(
          r.download_query || r.id, r.source || "local", btn, r.size_gb, r.backend, r
        ));
        actions.appendChild(btn);
      }
      container.appendChild(card);
    }
  }

  function parameterCountBillions(result) {
    const explicit = result.parameter_count_billions ?? result.parameters_billions;
    if (Number.isFinite(Number(explicit)) && Number(explicit) > 0) return Number(explicit);
    const text = [result.parameter_size, result.id, result.description].filter(Boolean).join(" ");
    const match = text.match(/(?:^|[^\d])(\d+(?:\.\d+)?)\s*(?:billion\b|b\b)/i);
    return match ? Number(match[1]) : null;
  }

  function downloadAllBackendModel(query, source, btn, sizeGb, backend, result) {
    const refresh = () => searchAll(allSearchInput.value.trim());
    if (backend === "lemonade") {
      return downloadCatalogModel(result.id, source, btn, sizeGb);
    }
    if (backend === "lmstudio") {
      return downloadFreeformCard(query, backend, btn, refresh, sizeGb);
    }
    return downloadStructuredCard(query, backend, btn, refresh, sizeGb);
  }

  async function downloadCatalogModel(modelId, source, triggerBtn, sizeGb) {
    if (busy) return;
    if (!window.confirm(`Download ${modelId} (${formatSize(sizeGb)})? This may take a while depending on size and your connection.`)) return;
    busy = true;
    if (triggerBtn) triggerBtn.disabled = true;
    const label = source === "huggingface" ? `${modelId} (auto-picking a quantization)…` : `${modelId}…`;
    trackDownload("lemonade", modelId);
    startProgress(`Downloading ${label}`);

    try {
      const res = await fetch("/api/models/download-catalog", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_id: modelId, source }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      let sawError = null;
      await streamSse(res, (payload) => {
        if (payload.error) {
          sawError = payload.error;
          return;
        }
        if (payload.done) {
          progressBar.style.width = "100%";
          progressPct.textContent = "100%";
          progressDetail.textContent = "Download complete.";
          return;
        }
        const pct = payload.percent || 0;
        progressBar.style.width = `${pct}%`;
        progressPct.textContent = `${pct}%`;
        const bytesText = payload.bytes_total
          ? ` — ${formatBytes(payload.bytes_downloaded)} / ${formatBytes(payload.bytes_total)}`
          : "";
        progressDetail.textContent =
          `${payload.file || ""} (file ${payload.file_index ?? "?"}/${payload.total_files ?? "?"})${bytesText}`;
      });
      if (sawError) throw new Error(sawError);
      await searchCatalog(searchInput.value.trim());
    } catch (err) {
      progressDetail.textContent = `Error: ${err.message}`;
    } finally {
      busy = false;
      finishTracking();
      if (triggerBtn) triggerBtn.disabled = false;
    }
  }

  // vLLM's download step (see vllm_client.download_model) now yields real byte-level
  // progress by polling bytes written to disk, not by parsing the `hf` CLI's output —
  // confirmed live that output stays completely silent for the whole download in every
  // configuration tried, which is what made downloads look stuck/wrong before. Same
  // payload shape and rendering as downloadCatalogModel below, just no file_index/
  // total_files (a vLLM repo download is tracked as one aggregate size, not per-file).
  async function downloadVllmModel(repoId, _source, triggerBtn, sizeGb) {
    if (busy) return;
    if (!window.confirm(`Download ${repoId} (${formatSize(sizeGb)})? This may take a while depending on size and your connection.`)) return;
    busy = true;
    if (triggerBtn) triggerBtn.disabled = true;
    trackDownload("vllm", repoId);
    startProgress(`Downloading ${repoId}…`);

    try {
      const res = await fetch("/api/models/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: repoId, backend: "vllm" }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      let sawError = null;
      await streamSse(res, (payload) => {
        if (payload.error) {
          sawError = payload.error;
          return;
        }
        if (payload.done) {
          progressBar.style.width = "100%";
          progressPct.textContent = "100%";
          progressDetail.textContent = "Download complete.";
          return;
        }
        const pct = payload.percent ?? 0;
        progressBar.style.width = `${pct}%`;
        progressPct.textContent = payload.percent != null ? `${pct}%` : "";
        progressDetail.textContent = payload.bytes_total
          ? `${formatBytes(payload.bytes_downloaded)} / ${formatBytes(payload.bytes_total)}`
          : formatBytes(payload.bytes_downloaded || 0);
      });
      if (sawError) throw new Error(sawError);
      await searchVllm(vllmSearchInput.value.trim());
    } catch (err) {
      progressDetail.textContent = `Error: ${err.message}`;
    } finally {
      busy = false;
      finishTracking();
      if (triggerBtn) triggerBtn.disabled = false;
    }
  }

  // Card "Download" for backends whose download yields structured byte-level progress
  // (KoboldCPP, FastFlowLM) — same stream/render as downloadVllmModel, just generic over
  // backend, and refreshes the originating grid when done.
  async function downloadStructuredCard(query, backend, triggerBtn, refresh, sizeGb) {
    if (busy) return;
    if (!window.confirm(`Download ${query} (${formatSize(sizeGb)})? This may take a while depending on size and your connection.`)) return;
    busy = true;
    if (triggerBtn) triggerBtn.disabled = true;
    trackDownload(backend, query);
    startProgress(`Downloading ${query}…`);
    try {
      const res = await fetch("/api/models/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, backend }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      let sawError = null;
      await streamSse(res, (payload) => {
        if (payload.error) { sawError = payload.error; return; }
        if (payload.done) {
          progressBar.style.width = "100%";
          progressPct.textContent = "100%";
          progressDetail.textContent = "Download complete.";
          return;
        }
        const pct = payload.percent ?? 0;
        progressBar.style.width = `${pct}%`;
        progressPct.textContent = payload.percent != null ? `${pct}%` : "";
        progressDetail.textContent = payload.bytes_total
          ? `${formatBytes(payload.bytes_downloaded)} / ${formatBytes(payload.bytes_total)}`
          : (payload.file || "");
      });
      if (sawError) throw new Error(sawError);
      if (refresh) await refresh();
    } catch (err) {
      progressDetail.textContent = `Error: ${err.message}`;
    } finally {
      busy = false;
      finishTracking();
      if (triggerBtn) triggerBtn.disabled = false;
    }
  }

  // Card "Download" for LM Studio — its download path (lms get) only ever emits text
  // lines, no byte progress, so the bar is indeterminate and the scrolling log is the
  // real indicator (same shape as runNameDownload's by-name form).
  async function downloadFreeformCard(query, backend, triggerBtn, refresh, sizeGb) {
    if (busy) return;
    if (!window.confirm(`Download ${query} (${formatSize(sizeGb)})? This may take a while depending on size and your connection.`)) return;
    busy = true;
    if (triggerBtn) triggerBtn.disabled = true;
    trackDownload(backend, query);
    startProgress(`Downloading ${query}…`, { showLog: true, indeterminate: true });
    try {
      const res = await fetch("/api/models/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, backend }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      let sawError = null;
      await streamSse(res, (payload) => {
        if (payload.error) { sawError = payload.error; return; }
        if (payload.done) { progressDetail.textContent = "Download complete."; return; }
        progressLog.textContent += (progressLog.textContent ? "\n" : "") + (payload.line || "");
        progressLog.scrollTop = progressLog.scrollHeight;
      });
      if (sawError) throw new Error(sawError);
      if (refresh) await refresh();
    } catch (err) {
      progressDetail.textContent = `Error: ${err.message}`;
    } finally {
      busy = false;
      finishTracking();
      if (triggerBtn) triggerBtn.disabled = false;
    }
  }

  searchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    searchCatalog(searchInput.value.trim());
  });

  allSearchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    searchAll(allSearchInput.value.trim());
  });

  lmSearchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    searchLmstudio(lmSearchInput.value.trim());
  });

  koboldSearchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    searchKobold(koboldSearchInput.value.trim());
  });

  npuSearchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    searchFastflow(npuSearchInput.value.trim());
  });

  vllmSearchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    searchVllm(vllmSearchInput.value.trim());
  });

  // Start with the recommended Lemonade chat model so a new installation has a
  // clear first choice; users can edit the query to browse other catalog entries.
  searchCatalog(searchInput.value.trim());
  // vLLM: empty query loads the curated suggestions (models vetted to load on vLLM),
  // shown first the way Lemonade's catalog is.
  searchVllm("");
  // FastFlowLM has a fixed NPU catalog, so its empty query loads the whole thing to browse
  // (installed first). LM Studio / Kobold have no catalog — they wait for a search term.
  searchFastflow("");

  // Shared by both "exact name" forms (LM Studio's fuzzy resolve, and Ryzen AI NPU's
  // exact-tag registration) — both go through /api/models/download and only ever yield
  // text lines (no byte-level progress, unlike the catalog path above), so the bar is
  // indeterminate and the scrolling log is the real progress indicator for these two.
  async function runNameDownload({ query, backend, queryEl, btnEl, confirmText, resolvingLabel }) {
    if (busy || !query) return;
    if (confirmText && !window.confirm(confirmText)) return;

    busy = true;
    btnEl.disabled = true;
    queryEl.disabled = true;
    trackDownload(backend, query);
    startProgress(resolvingLabel, { showLog: true, indeterminate: true });

    try {
      const res = await fetch("/api/models/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, backend }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      let sawError = null;
      await streamSse(res, (payload) => {
        if (payload.error) {
          sawError = payload.error;
          return;
        }
        if (payload.done) {
          progressDetail.textContent = "Download complete.";
          return;
        }
        progressLog.textContent += (progressLog.textContent ? "\n" : "") + (payload.line || "");
        progressLog.scrollTop = progressLog.scrollHeight;
      });
      if (sawError) throw new Error(sawError);
      queryEl.value = "";
    } catch (err) {
      progressDetail.textContent = `Error: ${err.message}`;
    } finally {
      busy = false;
      finishTracking();
      btnEl.disabled = false;
      queryEl.disabled = false;
    }
  }

  lmForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const query = lmQueryEl.value.trim();
    runNameDownload({
      query,
      backend: "lmstudio",
      queryEl: lmQueryEl,
      btnEl: lmBtn,
      confirmText: `Search LM Studio's catalog for "${query}" and download the best match?`,
      resolvingLabel: `Resolving "${query}" on LM Studio…`,
    });
  });

  const npuForm = document.getElementById("npu-download-form");
  const npuQueryEl = document.getElementById("npu-download-query");
  const npuBtn = document.getElementById("npu-download-btn");

  npuForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const query = npuQueryEl.value.trim();
    runNameDownload({
      query,
      backend: "lemonade",
      queryEl: npuQueryEl,
      btnEl: npuBtn,
      confirmText: `Register and download NPU model "${query}" on Ryzen AI (Lemonade)?`,
      resolvingLabel: `Downloading "${query}" on Ryzen AI (Lemonade)…`,
    });
  });

  // Downloads run server-side independent of any one browser tab's connection — a
  // download started here (or in another tab) keeps going even if this page reloads.
  // Real user report: reloading the Downloads page, or opening it fresh, mid-download
  // showed nothing at all, because progress previously only ever lived inside the SSE
  // stream of whichever request originally started it. This polls a server-side
  // snapshot (see /api/models/downloads/active in app.py) so an *observing* tab — one
  // that didn't start the download itself, `busy` is false, no SSE stream attached —
  // can still show it's happening and keep the bar current until it finishes.
  let observingRemoteDownload = false;
  let observingRemoteBackend = null;

  function renderFromSnapshot(entry) {
    const label = entry.source === "huggingface" ? `${entry.model_id} (auto-picking a quantization)…` : `${entry.model_id}…`;
    progressWrap.classList.remove("hidden");
    trackDownload(entry.backend, entry.model_id);
    progressTitle.textContent = `Downloading ${label}`;
    if (entry.kind === "structured" && entry.progress) {
      const p = entry.progress;
      const pct = p.percent || 0;
      progressBar.style.width = `${pct}%`;
      progressPct.textContent = p.done ? "100%" : `${pct}%`;
      const bytesText = p.bytes_total ? ` — ${formatBytes(p.bytes_downloaded)} / ${formatBytes(p.bytes_total)}` : "";
      progressDetail.textContent = p.done
        ? "Download complete."
        : `${p.file || ""} (file ${p.file_index ?? "?"}/${p.total_files ?? "?"})${bytesText}`;
      progressLog.classList.add("hidden");
    } else {
      progressBar.style.width = "100%";
      progressPct.textContent = "";
      progressLog.classList.remove("hidden");
      progressLog.textContent = (entry.lines || []).join("\n");
      progressLog.scrollTop = progressLog.scrollHeight;
    }
  }

  let activeDownloadsPollPending = false;
  async function pollActiveDownloads() {
    if (busy || activeDownloadsPollPending || document.hidden) return;
    activeDownloadsPollPending = true;
    try {
      const res = await fetch("/api/models/downloads/active");
      if (!res.ok) return;
      const data = await res.json();
      const downloads = data.downloads || [];
      if (downloads.length === 0) {
        if (observingRemoteDownload) {
          // Was showing someone else's in-flight download and it just finished —
          // refresh whichever backend's results it belonged to, so the
          // newly-downloaded model's state updates too.
          observingRemoteDownload = false;
          const refreshers = {
            vllm: () => searchVllm(vllmSearchInput.value.trim()),
            kobold: () => searchKobold(koboldSearchInput.value.trim()),
            fastflowlm: () => searchFastflow(npuSearchInput.value.trim()),
            lmstudio: () => searchLmstudio(lmSearchInput.value.trim()),
          };
          (refreshers[observingRemoteBackend] || (() => searchCatalog(searchInput.value.trim())))();
          observingRemoteBackend = null;
          finishTracking();
        }
        return;
      }
      observingRemoteDownload = true;
      observingRemoteBackend = downloads[0].backend;
      renderFromSnapshot(downloads[0]);
    } catch {
      // transient — next poll tick will catch up, same fail-soft spirit as the rest of this page
    } finally {
      activeDownloadsPollPending = false;
    }
  }

  setInterval(pollActiveDownloads, 2000);
  pollActiveDownloads();

  // "New to Hearth?" banner: real gap this closes — Ryzen AI/vLLM/FastFlowLM already show
  // curated picks with no search needed, but a first-time user landing on this page had zero
  // indication of that, or of what to search for on LM Studio/KoboldCPP (which show nothing
  // until you type). Dismissible (localStorage) rather than tied to "zero models downloaded",
  // since that check is fragile — what if a fresh user already grabbed one unrelated model, or
  // an existing user wants to still see it once. Once dismissed it stays gone for that browser;
  // clearing site data (or a different browser/profile) shows it again, which is fine — it's
  // meant for someone new to this specific browser's Hearth session, not a one-time global flag.
  const gettingStartedBanner = document.getElementById("getting-started-banner");
  const gettingStartedDismiss = document.getElementById("getting-started-dismiss");
  const GETTING_STARTED_KEY = "hearth-downloads-getting-started-dismissed";
  if (gettingStartedBanner && !localStorage.getItem(GETTING_STARTED_KEY)) {
    gettingStartedBanner.classList.remove("hidden");
  }
  if (gettingStartedDismiss) {
    gettingStartedDismiss.addEventListener("click", () => {
      localStorage.setItem(GETTING_STARTED_KEY, "1");
      gettingStartedBanner.classList.add("hidden");
    });
  }
})();
