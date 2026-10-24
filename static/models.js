// Full-page model manager. Self-contained: lists models from /api/models grouped by
// backend, with Load / Unload / Delete, a summary strip, search filter, and load options.
(() => {
  const el = (id) => document.getElementById(id);
  const groupsEl = el("model-groups");
  const summaryEl = el("model-summary");
  const searchEl = el("model-search");
  const warningsOnlyEl = el("model-warnings-only");
  const toastEl = el("toast");

  const GROUPS = [
    { key: "lmstudio", label: "LM Studio" },
    { key: "lemonade", label: "Ryzen AI (NPU)" },
    { key: "vllm", label: "vLLM" },
    { key: "sglang", label: "SGLang (remote)" },
    { key: "kobold", label: "KoboldCPP" },
    { key: "fastflowlm", label: "FastFlowLM (NPU)" },
    { key: "comfyui", label: "ComfyUI — Image & Video" },
  ];
  const LABEL = Object.fromEntries(GROUPS.map((g) => [g.key, g.label]));
  const NON_CHAT = ["embeddings", "transcription", "tts", "image", "video"];

  let models = [];
  let busy = false;
  let busyModelId = null;   // which card is mid-load/unload/delete, so it can show that visibly
  let busyLabel = "";       // "Loading" / "Unloading" / "Deleting", shown on the card + toast

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s ?? "";
    return d.innerHTML;
  }
  function fmtSize(gb) {
    if (typeof gb !== "number" || gb <= 0) return null;
    return gb < 1 ? `${Math.round(gb * 1000)} MB` : `${gb.toFixed(1)} GB`;
  }
  let toastTimer;
  function toast(msg, isErr, persist) {
    toastEl.textContent = msg;
    toastEl.style.display = "block";
    toastEl.style.borderColor = isErr ? "var(--danger)" : "var(--border)";
    clearTimeout(toastTimer);
    // persist=true skips the auto-hide — used for long-running ops (a cold vLLM/KoboldCPP/
    // FastFlowLM service start can take 60-90+s) so the message doesn't vanish after 4s and
    // leave the page looking idle/hung for the rest of the wait. Real user report: a working
    // 91s vLLM cold-start looked exactly like a failure because of this — the toast disappeared
    // almost immediately and nothing else in the UI changed until the request finally resolved.
    if (!persist) toastTimer = setTimeout(() => { toastEl.style.display = "none"; }, 4000);
  }

  async function fetchModels() {
    try {
      const res = await fetch("/api/models");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to list models");
      models = data.models || [];
      render();
    } catch (err) {
      groupsEl.innerHTML = `<p class="hint">Could not load models: ${esc(err.message)}</p>`;
    }
  }

  function render() {
    renderSummary();
    renderGroups();
  }

  function renderSummary() {
    const loaded = models.filter((m) => m.state === "loaded");
    const totalGb = models.reduce((s, m) => s + (m.size_gb || 0), 0);
    const chip = (val, label) =>
      `<div class="model-sum-chip"><div class="model-sum-val">${val}</div><div class="model-sum-label">${esc(label)}</div></div>`;
    let loadedHtml = "";
    if (loaded.length) {
      loadedHtml = `<div class="model-loaded-now"><span class="hint">Loaded now:</span> ` +
        loaded.map((m) => `<span class="model-loaded-pill" title="${esc(m.backend)}">${esc(m.id)} <span class="hint">· ${esc(LABEL[m.backend] || m.backend)}</span></span>`).join(" ") +
        `</div>`;
    }
    summaryEl.innerHTML =
      `<div class="model-sum-chips">` +
      chip(models.length, "on disk") +
      chip(loaded.length, "loaded") +
      chip(fmtSize(totalGb) || "—", "total size") +
      chip(models.filter(m => m.resource_warning).length, "with resource warnings") +
      `</div>` + loadedHtml;
  }

  function card(m) {
    const loaded = m.state === "loaded";
    const managedAutomatically = m.backend === "comfyui";
    const externallyManaged = m.externally_managed === true;
    const isBusy = m.id === busyModelId;
    const size = fmtSize(m.size_gb);
    const metaParts = [m.publisher, m.quantization, m.type, size].filter(Boolean);
    const meta = metaParts.map(esc).join(" · ");
    const c = document.createElement("div");
    c.className = "model-card2" + (loaded ? " loaded" : "") + (isBusy ? " busy" : "");
    c.dataset.name = (m.id || "").toLowerCase();
    const unavailable = m.loadable === false;
    const badge = isBusy
      ? `<span class="badge loading">${esc(busyLabel)}…</span>`
      : `<span class="badge ${loaded ? "loaded" : "not-loaded"}">${managedAutomatically ? (loaded ? "loaded · ready" : "service unavailable") : (loaded ? "loaded" : (unavailable ? "converted · kernels pending" : "not loaded"))}</span>`;
    c.innerHTML =
      `<div class="model-card2-main">
        <div class="model-card2-name" title="${esc(m.id)}">${esc(m.id)}</div>
        <div class="model-card2-meta">${meta || "&nbsp;"}</div>
      </div>
      ${badge}
      <div class="model-card2-actions"></div>`;
    const contextTokens = Number(m.context_window_tokens);
    if (Number.isSafeInteger(contextTokens) && contextTokens > 0) {
      const contextLabel = document.createElement("span");
      contextLabel.className = "model-context-label";
      const isModelMax = m.context_window_source === "model_max";
      contextLabel.textContent = `${isModelMax ? "max " : "ctx "}${contextTokens.toLocaleString()} tokens`;
      const loadedContext = Number(m.loaded_context_length);
      const sourceText = {
        model_max: "Maximum context reported by the model backend.",
        loaded_runtime: "Current loaded runtime context; the model may support a larger window.",
        configured: "Context length configured for this model or runtime.",
        backend_default: "Hearth's default context for this backend; the model's actual maximum is not reported.",
      }[m.context_window_source] || "Context length reported by the backend.";
      contextLabel.title = isModelMax && Number.isSafeInteger(loadedContext) && loadedContext > 0 && loadedContext !== contextTokens
        ? `${sourceText} Currently loaded at ${loadedContext.toLocaleString()} tokens.`
        : sourceText;
      const metaEl = c.querySelector(".model-card2-meta");
      if (meta) metaEl.append(document.createTextNode(" · "));
      else metaEl.innerHTML = "";
      metaEl.appendChild(contextLabel);
    }
    const actions = c.querySelector(".model-card2-actions");
    if (m.resource_warning) {
      // The warning sits inside the same actions row as the Load button — above it,
      // spanning the row — so the text is read before the click instead of being
      // collapsed below the card where it was easy to miss. Evidence stays collapsed.
      const warning = m.resource_warning;
      const box = document.createElement("div");
      box.className = "model-load-warning";
      const label = document.createElement("div");
      label.className = "model-load-warning-label";
      label.textContent = `⚠ ${warning.label}`;
      const message = document.createElement("div");
      message.textContent = warning.summary;
      box.append(label, message);
      const note = document.createElement("details"); note.className = "model-resource-warning";
      const summary = document.createElement("summary"); summary.textContent = `Evidence & review (${(warning.evidence || []).length})`;
      const scope = document.createElement("p"); scope.className = "hint";
      scope.textContent = `${warning.scope} Reviewed ${warning.reviewed_at}.`;
      const evidence = document.createElement("ul");
      for (const item of warning.evidence || []) {
        const entry = document.createElement("li");
        const source = document.createElement("strong");
        source.textContent = `${new Date(item.observed_at).toLocaleString()} · ${item.source}`;
        const excerpt = document.createElement("p"); excerpt.textContent = item.excerpt;
        entry.append(source, excerpt); evidence.appendChild(entry);
      }
      note.append(summary, scope, evidence); box.appendChild(note);
      actions.appendChild(box);
    }
    // Disabled (not just hidden) while this specific card has an operation in flight, so a
    // slow cold-start (vLLM/KoboldCPP/FastFlowLM can take 60-90+s) can't be double-clicked
    // into a second, conflicting request — the card visibly shows why the buttons are inert
    // instead of just silently no-op'ing like the old `busy` guard alone did.
    if (externallyManaged) {
      const note = document.createElement("span");
      note.className = "hint";
      note.textContent = "Managed by the external SGLang server";
      actions.appendChild(note);
    } else if (managedAutomatically) {
      const note = document.createElement("span");
      note.className = "hint";
      note.textContent = "Auto-loaded when a video is requested";
      actions.appendChild(note);
    } else if (loaded) {
      actions.appendChild(btn("Unload", "", () => unloadModel(m.id), isBusy));
    } else if (unavailable) {
      const note = document.createElement("span");
      note.className = "hint";
      note.textContent = m.load_error || "Runtime support is not ready.";
      note.title = note.textContent;
      actions.appendChild(note);
      // Keep the safety gate, but do not render a dead control. A disabled button
      // emits no click event and gave admins no feedback at all. The server also
      // performs this preflight before unloading anything; this client-side response
      // simply makes the blocker immediate and visible.
      actions.appendChild(btn("Load", "primary", () => {
        toast(`Cannot load ${m.id}: ${m.load_error || "runtime support is not ready"}`, true, true);
      }, false));
    } else {
      actions.appendChild(btn("Load", "primary", () => loadModel(m.id), isBusy));
    }
    if (!managedAutomatically && !externallyManaged && m.type !== "embeddings" && m.deletable !== false) {
      actions.appendChild(btn("Delete", "danger", () => deleteModel(m.id), isBusy));
    }
    if (!managedAutomatically && !unavailable && !NON_CHAT.includes(m.type) && m.type !== "embedding") {
      actions.appendChild(btn("Test tools", "", () => window.hearthModelTests.start([m]), isBusy));
      actions.appendChild(btn("Reset repair", "", () => window.hearthModelTests.reset(m), isBusy));
    }
    return c;
  }
  function btn(label, cls, onClick, disabled) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "model-mini-btn" + (cls ? " " + cls : "");
    b.textContent = label;
    if (disabled || window.hearthModelTests?.isRunning()) b.disabled = true;
    b.addEventListener("click", onClick);
    return b;
  }

  function renderGroups() {
    const q = (searchEl.value || "").trim().toLowerCase();
    groupsEl.innerHTML = "";
    let shown = 0;
    const ordered = [...GROUPS, ...[...new Set(models.map((m) => m.backend))]
      .filter((k) => !LABEL[k]).map((k) => ({ key: k, label: k || "Other" }))];
    for (const g of ordered) {
      let list = models.filter((m) => m.backend === g.key);
      if (q) list = list.filter((m) => (m.id || "").toLowerCase().includes(q));
      if (warningsOnlyEl?.checked) list = list.filter(m => m.resource_warning);
      if (!list.length) continue;
      shown += list.length;
      const section = document.createElement("div");
      section.className = "console-card span-all model-group";
      section.innerHTML = `<h2>${esc(g.label)} <span class="hint">· ${list.length}</span></h2>`;
      const grid = document.createElement("div");
      grid.className = "model-card2-grid";
      list.forEach((m) => grid.appendChild(card(m)));
      section.appendChild(grid);
      groupsEl.appendChild(section);
    }
    if (!shown) {
      groupsEl.innerHTML = warningsOnlyEl?.checked
        ? `<p class="hint">No models with recorded resource warnings match this filter.</p>` : q
        ? `<p class="hint">No models match “${esc(q)}”.</p>`
        : `<p class="hint">No models on disk. Get one from the <a href="/downloads">Downloads page</a>.</p>`;
    }
  }

  warningsOnlyEl?.addEventListener("change", renderGroups);

  // Cold-starting an on-demand backend's service (vLLM/KoboldCPP/FastFlowLM) can take 60-90+s
  // (confirmed live: a working vLLM cold-start took 91s). Real user report: this looked exactly
  // like a failed load, because the old code showed one static toast that auto-hid after 4s and
  // never touched the card again until the whole request finished — nothing on the page visibly
  // changed for the rest of the wait, so a perfectly successful load looked hung/broken. Now the
  // toast persists and ticks up every second, and the specific card shows a "Loading…" badge with
  // its buttons disabled, for the entire duration of the operation.
  function startBusyOp(id, label, baseMsg) {
    busy = true;
    busyModelId = id;
    busyLabel = label;
    renderGroups();
    const t0 = Date.now();
    toast(`${baseMsg} (0s elapsed)`, false, true);
    const timer = setInterval(() => {
      toast(`${baseMsg} (${Math.round((Date.now() - t0) / 1000)}s elapsed)`, false, true);
    }, 1000);
    return () => clearInterval(timer);
  }
  function endBusyOp(stopTicker) {
    stopTicker();
    busy = false;
    busyModelId = null;
    busyLabel = "";
  }

  // vLLM/KoboldCPP/FastFlowLM only run while a model is loaded — no model of theirs currently
  // "loaded" means the service itself is stopped, so loading one starts it from cold (confirmed
  // live: vLLM took 91s). LM Studio/Lemonade are always-on processes, so there's no service to
  // start for them — only the on-demand three ever need this extra warning.
  const ON_DEMAND_BACKENDS = new Set(["vllm", "kobold", "fastflowlm"]);

  async function loadModel(modelKey) {
    if (busy || window.hearthModelTests?.isRunning()) return;
    const targetModel = models.find((m) => m.id === modelKey);
    const target = targetModel?.backend;
    const supportTypes = new Set(["embeddings", "embedding", "transcription", "tts"]);
    const other = models.filter(
      (m) => m.state === "loaded" && m.id !== modelKey
        && !supportTypes.has(m.type)
    );
    const serviceIsDown = ON_DEMAND_BACKENDS.has(target) && !models.some((m) => m.backend === target && m.state === "loaded");

    const lines = [`Load ${modelKey}?`];
    if (other.length) {
      lines.push(`The currently loaded chat model will be unloaded: ${other.map((m) => m.id).join(", ")}.`);
    }
    if (target === "lemonade") {
      lines.push("Voice input, voice output, and embedding/RAG support models will be kept loaded.");
    }
    if (serviceIsDown) {
      lines.push(`${LABEL[target] || target}'s service is currently stopped, so this will start it from cold — that can take up to ~1-2 minutes.`);
    }
    if (!confirm(lines.join("\n\n"))) return;

    const stopTicker = startBusyOp(modelKey, "Loading", `Loading ${modelKey}… (cold starts can take up to ~1-2 min)`);
    try {
      const res = await fetch("/api/models/load", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: modelKey,
          identifier: el("opt-identifier").value.trim() || undefined,
          context_length: el("opt-context").value ? Number(el("opt-context").value) : undefined,
          ttl: el("opt-ttl").value ? Number(el("opt-ttl").value) : undefined,
          gpu: el("opt-gpu").value.trim() || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "load failed");
      toast(data.unloaded?.length ? `Loaded ${modelKey} (unloaded ${data.unloaded.join(", ")})` : `Loaded ${modelKey}`);
    } catch (err) {
      toast(`Failed to load ${modelKey}: ${err.message}`, true);
    }
    endBusyOp(stopTicker);
    fetchModels();
  }

  async function unloadModel(id) {
    if (busy || window.hearthModelTests?.isRunning()) return;
    const target = models.find((m) => m.id === id)?.backend;
    // For the on-demand backends, "unload" isn't just freeing RAM — vllm_client/kobold_client/
    // fastflowlm_client's unload_model() all stop the whole systemd service (confirmed by
    // reading each one directly), since that's the only way vLLM/KoboldCPP work and how
    // FastFlowLM releases the NPU. So unloading here means the *next* Load on this backend is a
    // full cold start again (~1-2 min), not instant — worth surfacing before it happens, same
    // reasoning as the cold-start warning added to loadModel() above.
    const lines = [`Unload ${id}?`];
    if (ON_DEMAND_BACKENDS.has(target)) {
      lines.push(`This stops ${LABEL[target] || target}'s service entirely (it only runs while a model is loaded) — loading a model here again will be a cold start, which can take up to ~1-2 minutes.`);
    }
    if (!confirm(lines.join("\n\n"))) return;

    const stopTicker = startBusyOp(id, "Unloading", `Unloading ${id}…`);
    const controller = new AbortController();
    const requestTimeout = setTimeout(() => controller.abort(), 75000);
    try {
      const res = await fetch("/api/models/unload", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ identifier: id }), signal: controller.signal,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "unload failed");
      toast(`Unloaded ${id}`);
    } catch (err) {
      const detail = err.name === "AbortError"
        ? "request timed out; the backend may still be finishing an active request"
        : err.message;
      toast(`Failed to unload ${id}: ${detail}`, true);
    } finally {
      clearTimeout(requestTimeout);
    }
    endBusyOp(stopTicker);
    fetchModels();
  }

  async function deleteModel(id) {
    if (busy || window.hearthModelTests?.isRunning()) return;
    const m = models.find((mm) => mm.id === id);
    const lines = [`Permanently delete "${id}" from disk? Frees storage (and memory if loaded), but you'd need to re-download it.`];
    // Each backend's delete_model() unloads first if this is the one currently being served
    // (confirmed by reading vllm_client/kobold_client/fastflowlm_client directly) — for the
    // on-demand three that means stopping the whole service too, same as a plain Unload does.
    if (m?.state === "loaded" && ON_DEMAND_BACKENDS.has(m.backend)) {
      lines.push(`It's currently loaded, so this also stops ${LABEL[m.backend] || m.backend}'s service first.`);
    }
    if (!confirm(lines.join("\n\n"))) return;
    const stopTicker = startBusyOp(id, "Deleting", `Deleting ${id}…`);
    try {
      const res = await fetch("/api/models/delete", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ identifier: id }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "delete failed");
      toast(`Deleted ${id}`);
    } catch (err) { toast(`Failed to delete ${id}: ${err.message}`, true); }
    endBusyOp(stopTicker);
    fetchModels();
  }

  el("refresh").addEventListener("click", fetchModels);
  searchEl.addEventListener("input", renderGroups);
  fetchModels();
  document.addEventListener("model-tests-state", () => renderGroups());
  document.addEventListener("model-tests-finished", () => fetchModels());
})();
