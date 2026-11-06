(() => {
  const BACKEND_LABELS = { lmstudio: "LM Studio", lemonade: "Ryzen AI / Lemonade", vllm: "vLLM", opencode: "opencode.ai", kobold: "KoboldCPP", q38rocm: "q38rocm (Vulkan/ROCmFPX)", fastflowlm: "FastFlowLM" };
  const BACKEND_ORDER = ["fastflowlm", "q38rocm", "lemonade", "lmstudio", "kobold", "vllm", "opencode"];

  const settingsForm = document.getElementById("settings-form");
  const restartBackendsBtn = document.getElementById("restart-backends");
  const restartQ38rocmBtn = document.getElementById("restart-q38rocm");
  const restartHearthBtn = document.getElementById("restart-hearth");
  const restartResults = document.getElementById("restart-results");
  const restartStatusEl = document.getElementById("restart-status");
  const restartHistoryEl = document.getElementById("restart-history");
  const preserveMandatoryEl = document.getElementById("restart-preserve-mandatory");

  async function loadRestartHistory() {
    try {
      const res = await fetch("/api/admin/service-status");
      const data = await res.json();
      if (restartHistoryEl && data.services) {
        restartHistoryEl.innerHTML = "<strong>Service status:</strong><br/>" + data.services.map(s =>
          `${escapeHtml(s.service)}: ${escapeHtml(s.status)}${s.detail ? " — " + escapeHtml(s.detail) : ""} (updated ${new Date(s.updated_at * 1000).toLocaleTimeString()})`
        ).join("<br/>");
      }
    } catch (e) { /* ignore */ }
  }
  // Render per-backend start/stop controls
  async function renderBackendControls() {
    const container = document.getElementById("backend-controls");
    if (!container) return;
    try {
      const res = await fetch("/api/admin/service-status");
      const data = await res.json();
      const services = data.services || [];
      container.innerHTML = services.map(s => {
        const name = escapeHtml(s.service);
        const status = escapeHtml(s.status);
        const canStart = s.status !== "started";
        const canStop = s.status === "started";
        return `<div style="display: flex; align-items: center; gap: 8px; margin-bottom: 4px;">
          <span style="min-width: 120px;">${name}</span>
          <span style="min-width: 80px; color: ${s.status === 'started' ? 'green' : s.status === 'error' ? 'red' : 'gray'};">${status}</span>
          ${canStart ? `<button class="panel-btn backend-start-btn" data-backend="${name}" type="button">Start</button>` : ''}
          ${canStop ? `<button class="panel-btn backend-stop-btn" data-backend="${name}" type="button">Stop</button>` : ''}
        </div>`;
      }).join("");
      container.querySelectorAll(".backend-start-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            const r = await fetch("/api/admin/start-backend", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content }, body: JSON.stringify({ backend: btn.dataset.backend }) });
            const d = await r.json();
            restartResults.textContent = d.message || (d.ok ? "Started" : "Failed");
          } catch (e) { restartResults.textContent = `Error: ${e.message}`; }
          renderBackendControls();
          loadRestartHistory();
        });
      });
      container.querySelectorAll(".backend-stop-btn").forEach(btn => {
        btn.addEventListener("click", async () => {
          btn.disabled = true;
          try {
            const r = await fetch("/api/admin/stop-backend", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content }, body: JSON.stringify({ backend: btn.dataset.backend }) });
            const d = await r.json();
            restartResults.textContent = d.message || (d.ok ? "Stopped" : "Failed");
          } catch (e) { restartResults.textContent = `Error: ${e.message}`; }
          renderBackendControls();
          loadRestartHistory();
        });
      });
    } catch (e) { container.textContent = `Could not load service status: ${e.message}`; }
  }
  renderBackendControls();

  loadRestartHistory();

  async function pollRestartJob(jobId) {
    const interval = setInterval(async () => {
      try {
        const res = await fetch(`/api/admin/restart-status/${jobId}`);
        const data = await res.json();
        if (restartStatusEl) {
          restartStatusEl.textContent = `${data.status}: ${data.progress}/${data.total} — ${data.detail || ""}`;
        }
        if (data.status === "done" || data.status === "error") {
          clearInterval(interval);
          restartBackendsBtn.disabled = false;
          restartHearthBtn.disabled = false;
          if (restartQ38rocmBtn) restartQ38rocmBtn.disabled = false;
          loadRestartHistory();
        }
      } catch (e) {
        clearInterval(interval);
      }
    }, 1000);
  }

  async function adminAction(url, label) {
    if (!window.confirm(`${label}? This may interrupt active requests.`)) return;
    restartResults.textContent = `${label}…`;
    restartBackendsBtn.disabled = true;
    restartHearthBtn.disabled = true;
    if (restartQ38rocmBtn) restartQ38rocmBtn.disabled = true;
    try {
      let body = {};
      if (url === "/api/admin/restart-backends") {
        const checked = [...document.querySelectorAll(".restart-backend-cb:checked")].map(cb => cb.value);
        body = { backends: checked, preserve_mandatory: preserveMandatoryEl ? preserveMandatoryEl.checked : true };
      }
      const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": document.querySelector('meta[name="csrf-token"]').content }, body: JSON.stringify(body) });
      const data = await response.json();
      if (url.endsWith("restart-hearth")) {
        restartResults.textContent = data.message || "Hearth is restarting.";
        if (data.job_id) pollRestartJob(data.job_id);
      } else {
        restartResults.innerHTML = Object.entries(data).filter(([k]) => k !== "_job_id").map(([name, result]) => `<div>${escapeHtml(name)}: ${result.ok ? "Restart requested" : escapeHtml(result.message)}</div>`).join("");
        if (data._job_id) pollRestartJob(data._job_id);
      }
    } catch (error) {
      restartResults.textContent = `Could not complete ${label}: ${error.message}`;
      restartBackendsBtn.disabled = false;
      restartHearthBtn.disabled = false;
      if (restartQ38rocmBtn) restartQ38rocmBtn.disabled = false;
    }
  }
  restartBackendsBtn?.addEventListener("click", () => adminAction("/api/admin/restart-backends", "Restart selected backends"));
  restartQ38rocmBtn?.addEventListener("click", () => adminAction("/api/admin/restart-q38rocm", "Restart q38rocm"));
  restartHearthBtn?.addEventListener("click", () => adminAction("/api/admin/restart-hearth", "Restart Hearth"));
  const settingsLmstudioUrlEl = document.getElementById("settings-lmstudio-url");
  const settingsLmstudioKeyEl = document.getElementById("settings-lmstudio-key");
  const settingsLemonadeUrlEl = document.getElementById("settings-lemonade-url");
  const settingsLemonadeKeyEl = document.getElementById("settings-lemonade-key");
  const settingsVllmUrlEl = document.getElementById("settings-vllm-url");
  const settingsVllmKeyEl = document.getElementById("settings-vllm-key");
  const settingsOpencodeUrlEl = document.getElementById("settings-opencode-url");
  const settingsOpencodePlanEl = document.getElementById("settings-opencode-plan");
  const settingsOpencodeKeyEl = document.getElementById("settings-opencode-key");
  const settingsKoboldUrlEl = document.getElementById("settings-kobold-url");
  const settingsKoboldKeyEl = document.getElementById("settings-kobold-key");
  const settingsKoboldGpuLayersEl = document.getElementById("settings-kobold-gpu-layers");
  const settingsKoboldDraftModelEl = document.getElementById("settings-kobold-draft-model");
  const settingsKoboldDraftAmountEl = document.getElementById("settings-kobold-draft-amount");
  const settingsQ38rocmUrlEl = document.getElementById("settings-q38rocm-url");
  const settingsQ38rocmKeyEl = document.getElementById("settings-q38rocm-key");
  const settingsFastflowlmUrlEl = document.getElementById("settings-fastflowlm-url");
  const settingsFastflowlmKeyEl = document.getElementById("settings-fastflowlm-key");
  const settingsDefaultModelEl = document.getElementById("settings-default-model");
  const settingsWebSearchEnabledEl = document.getElementById("settings-web-search-enabled");
  const settingsWebSearchModeEl = document.getElementById("settings-web-search-mode");
  const settingsWebSearchMaxResultsEl = document.getElementById("settings-web-search-max-results");
  const settingsWebSearchMaxPagesEl = document.getElementById("settings-web-search-max-pages");
  const settingsWebSearchDirectUrlsEl = document.getElementById("settings-web-search-direct-urls");
  const settingsWebSearchBrowserEnabledEl = document.getElementById("settings-web-search-browser-enabled");
  const settingsCaddyInfoEl = document.getElementById("settings-caddy-info");
  const settingsSaveBtn = document.getElementById("settings-save-btn");
  const settingsTestLmstudioBtn = document.getElementById("settings-test-lmstudio");
  const settingsTestLmstudioResultEl = document.getElementById("settings-test-lmstudio-result");
  const settingsTestLemonadeBtn = document.getElementById("settings-test-lemonade");
  const settingsTestLemonadeResultEl = document.getElementById("settings-test-lemonade-result");
  const settingsTestVllmBtn = document.getElementById("settings-test-vllm");
  const settingsTestVllmResultEl = document.getElementById("settings-test-vllm-result");
  const settingsTestOpencodeBtn = document.getElementById("settings-test-opencode");
  const opencodePlanDefaults = {
    payg: "https://opencode.ai/inference/openai",
    go: "https://opencode.ai/inference/go/openai",
  };
  function opencodeOverrideToSend() {
    // A prefilled plan-derived default is not an override — sending it back would
    // pin that single product endpoint forever (observed: a Go subscriber's plan
    // select was silently neutralized by the auto-filled URL). Only a genuinely
    // custom (advanced) base URL is sent.
    const value = settingsOpencodeUrlEl.value.trim();
    const known = Object.values(opencodePlanDefaults);
    return value && !known.includes(value) ? value : "";
  }
  const settingsTestOpencodeResultEl = document.getElementById("settings-test-opencode-result");
  const opencodeClearKeyBtn = document.getElementById("opencode-clear-key");
  const settingsTestKoboldBtn = document.getElementById("settings-test-kobold");
  const settingsTestKoboldResultEl = document.getElementById("settings-test-kobold-result");
  const settingsTestQ38rocmBtn = document.getElementById("settings-test-q38rocm");
  const settingsTestQ38rocmResultEl = document.getElementById("settings-test-q38rocm-result");
  const settingsTestFastflowlmBtn = document.getElementById("settings-test-fastflowlm");
  const settingsTestFastflowlmResultEl = document.getElementById("settings-test-fastflowlm-result");
  const settingsTestCaddyLmstudioBtn = document.getElementById("settings-test-caddy-lmstudio");
  const settingsTestCaddyLemonadeBtn = document.getElementById("settings-test-caddy-lemonade");
  const settingsTestCaddyVllmBtn = document.getElementById("settings-test-caddy-vllm");
  const settingsTestCaddyKoboldBtn = document.getElementById("settings-test-caddy-kobold");
  const settingsTestCaddyFastflowlmBtn = document.getElementById("settings-test-caddy-fastflowlm");
  const settingsTestCaddyResultEl = document.getElementById("settings-test-caddy-result");
  const settingsLemonadeThinkingEl = document.getElementById("settings-lemonade-thinking");
  const settingsTailscaleDetectedEl = document.getElementById("settings-tailscale-detected");
  const settingsTailscaleEnabledEl = document.getElementById("settings-tailscale-enabled");
  const settingsTailscaleIpOverrideEl = document.getElementById("settings-tailscale-ip-override");
  const settingsTestTailscaleLmstudioBtn = document.getElementById("settings-test-tailscale-lmstudio");
  const settingsTestTailscaleLemonadeBtn = document.getElementById("settings-test-tailscale-lemonade");
  const settingsTestTailscaleVllmBtn = document.getElementById("settings-test-tailscale-vllm");
  const settingsTestTailscaleKoboldBtn = document.getElementById("settings-test-tailscale-kobold");
  const settingsTestTailscaleFastflowlmBtn = document.getElementById("settings-test-tailscale-fastflowlm");
  const settingsTestTailscaleResultEl = document.getElementById("settings-test-tailscale-result");
  const settingsChatDefaultMaxTokensEl = document.getElementById("settings-chat-default-max-tokens");
  const settingsChatMaxMaxTokensEl = document.getElementById("settings-chat-max-max-tokens");
  const settingsChatMaxMessagesEl = document.getElementById("settings-chat-max-messages");
  const settingsChatMaxMessageCharsEl = document.getElementById("settings-chat-max-message-chars");
  const settingsChatMaxToolRoundsEl = document.getElementById("settings-chat-max-tool-rounds");
  const settingsOpencodeCompactionKeepTokensEl = document.getElementById("settings-opencode-compaction-keep-tokens");
  const settingsOpencodeCompactionBufferEl = document.getElementById("settings-opencode-compaction-buffer");
  const settingsOpencodeCompactionAutoEl = document.getElementById("settings-opencode-compaction-auto");
  const settingsOpencodeAdvertiseLoadedContextEl = document.getElementById("settings-opencode-advertise-loaded-context");
  const settingsOpencodeContextFallbackEl = document.getElementById("settings-opencode-context-fallback");

  // An empty field means "clear the override, fall back to the built-in default" — send null
  // rather than Number("") === 0, which the server would reject as out of range.
  const numOrNull = (el) => (el.value.trim() === "" ? null : Number(el.value));

  let models = [];
  let savedDefaultModel = ""; // "backend/model_id", from Settings' lan_default_model ("" = auto)

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function addSystemMessage(text, isError) {
    // No chat pane on this page — surface feedback the same lightweight toast way the
    // Users page does.
    const el = document.createElement("div");
    el.className = `users-toast ${isError ? "test-result-fail" : "test-result-ok"}`;
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  // Populates the "Default LAN model" dropdown from the live model list. Re-applies
  // savedDefaultModel after rebuilding the options so a model-list refresh doesn't reset
  // the admin's saved choice back to "Auto".
  function renderDefaultModelSelect() {
    const chatModels = models.filter((m) =>
      m.loadable !== false && m.lan_compatible !== false
      && !["embeddings", "transcription", "tts"].includes(m.type));
    settingsDefaultModelEl.innerHTML = '<option value="">Auto (recommended)</option>';
    const backends = [...BACKEND_ORDER, ...chatModels.map((m) => m.backend)]
      .filter((backend, index, all) => backend && all.indexOf(backend) === index);
    for (const backend of backends) {
      const backendModels = chatModels.filter((m) => m.backend === backend);
      if (backendModels.length === 0) continue;
      const group = document.createElement("optgroup");
      group.label = BACKEND_LABELS[backend] || backend;
      for (const m of backendModels) {
        const opt = document.createElement("option");
        opt.value = `${m.backend}/${m.id}`;
        // `quantization` is normalized by each client to the actual compute path when
        // known (NPU, GPU, CPU). This prevents an "-NPU" marketing/model name from being
        // mistaken for NPU execution when Lemonade reports a llamacpp/GPU recipe.
        const compute = m.backend === "fastflowlm" ? "NPU" : (m.quantization || "device unknown");
        opt.textContent = `${m.id} — ${compute}${m.state === "loaded" ? " · loaded" : ""}`;
        group.appendChild(opt);
      }
      settingsDefaultModelEl.appendChild(group);
    }
    // Keep the saved choice selected even if it's not in the current list (e.g. a backend
    // that's temporarily unreachable) - falls back to "Auto" display only if truly absent
    // from the dropdown's own options, matching a native <select>'s own behavior.
    settingsDefaultModelEl.value = savedDefaultModel;
  }

  async function fetchModelsForDefault() {
    try {
      const res = await fetch("/api/models");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to list models");
      models = data.models || [];
      renderDefaultModelSelect();
    } catch {
      // Non-fatal for this page - the dropdown just stays at whatever it already had
      // (or "Auto" only, on first load), same fail-soft contract as the rest of this app.
    }
  }

  function applySettings(data) {
    settingsLmstudioUrlEl.value = data.lmstudio_base_url || "";
    settingsLmstudioKeyEl.value = data.lmstudio_api_key || "";
    settingsLemonadeUrlEl.value = data.lemonade_base_url || "";
    settingsLemonadeKeyEl.value = data.lemonade_api_key || "";
    settingsVllmUrlEl.value = data.vllm_base_url || "";
    settingsVllmKeyEl.value = data.vllm_api_key || "";
    settingsOpencodePlanEl.value = data.opencode_api_plan === "payg" ? "payg" : "go";
    settingsOpencodeUrlEl.value = data.opencode_api_base_url || opencodePlanDefaults[settingsOpencodePlanEl.value];
    settingsOpencodeKeyEl.value = data.opencode_api_key || "";
    settingsKoboldUrlEl.value = data.kobold_base_url || "";
    settingsKoboldKeyEl.value = data.kobold_api_key || "";
    settingsKoboldGpuLayersEl.value = data.kobold_gpu_layers || "";
    settingsKoboldDraftModelEl.value = data.kobold_draft_model || "";
    settingsKoboldDraftAmountEl.value = data.kobold_draft_amount || "";
    settingsQ38rocmUrlEl.value = data.q38rocm_base_url || "";
    settingsQ38rocmKeyEl.value = data.q38rocm_api_key || "";
    settingsFastflowlmUrlEl.value = data.fastflowlm_base_url || "";
    settingsFastflowlmKeyEl.value = data.fastflowlm_api_key || "";
    savedDefaultModel = data.lan_default_model || "";
    renderDefaultModelSelect();
    settingsWebSearchEnabledEl.checked = data.web_search_enabled !== false;
    settingsWebSearchModeEl.value = data.web_search_mode || "automatic";
    settingsWebSearchMaxResultsEl.value = data.web_search_max_results ?? 6;
    settingsWebSearchMaxPagesEl.value = data.web_search_max_pages ?? 100;
    settingsWebSearchDirectUrlsEl.checked = data.web_search_direct_urls !== false;
    settingsWebSearchBrowserEnabledEl.checked = data.web_search_browser_enabled !== false;
    updateWebSearchControls();

    settingsChatDefaultMaxTokensEl.value = data.chat_default_max_tokens ?? 32768;
    settingsChatMaxMaxTokensEl.value = data.chat_max_max_tokens ?? 32768;
    settingsChatMaxMessagesEl.value = data.chat_max_messages ?? 500;
    settingsChatMaxMessageCharsEl.value = data.chat_max_message_chars ?? 2000000;
    settingsChatMaxToolRoundsEl.value = data.chat_max_tool_rounds ?? 100;
    settingsOpencodeCompactionKeepTokensEl.value = data.opencode_compaction_keep_tokens ?? 15000;
    settingsOpencodeCompactionBufferEl.value = data.opencode_compaction_buffer ?? "";
    settingsOpencodeCompactionAutoEl.checked = data.opencode_compaction_auto === true;
    settingsOpencodeAdvertiseLoadedContextEl.checked = data.opencode_advertise_loaded_context ?? true;
    settingsOpencodeContextFallbackEl.value = data.opencode_context_fallback ?? "";

    settingsLemonadeThinkingEl.checked = data.lemonade_enable_thinking !== false;

    settingsTailscaleEnabledEl.checked = data.tailscale_enabled !== false;
    settingsTailscaleIpOverrideEl.value = data.tailscale_ip_override || "";
    settingsTailscaleDetectedEl.textContent = data.tailscale_detected_ip
      ? `Auto-detected Tailscale IP: ${data.tailscale_detected_ip}`
      : "No Tailscale IP auto-detected on this machine (tailscale not installed, logged out, or unreachable) — set a manual override below if you know it.";

    if (!data.caddy) {
      settingsCaddyInfoEl.textContent = "Caddyfile not found or not readable — Caddy may not be set up on this machine.";
    } else {
      const lines = [];
      if (data.caddy.bearer_token) lines.push(`Bearer token: ${data.caddy.bearer_token}`);
      if (data.caddy.ports?.lmstudio) lines.push(`LM Studio LAN port: ${data.caddy.ports.lmstudio}`);
      if (data.caddy.ports?.lemonade) lines.push(`Ryzen AI LAN port: ${data.caddy.ports.lemonade}`);
      if (data.caddy.ports?.vllm) lines.push(`vLLM LAN port: ${data.caddy.ports.vllm}`);
      if (data.caddy.ports?.kobold) lines.push(`KoboldCPP LAN port: ${data.caddy.ports.kobold}`);
      if (data.caddy.ports?.fastflowlm) lines.push(`FastFlowLM LAN port: ${data.caddy.ports.fastflowlm}`);
      settingsCaddyInfoEl.innerHTML = lines.length
        ? lines.map((l) => escapeHtml(l)).join("<br>")
        : "Caddyfile found, but no recognized LM Studio/Lemonade proxy blocks in it.";
    }
  }

  function updateWebSearchControls() {
    const enabled = settingsWebSearchEnabledEl.checked;
    settingsWebSearchModeEl.disabled = !enabled;
    settingsWebSearchMaxResultsEl.disabled = !enabled;
    settingsWebSearchBrowserEnabledEl.disabled = !enabled;
    const browserEnabled = enabled && settingsWebSearchBrowserEnabledEl.checked;
    settingsWebSearchMaxPagesEl.disabled = !browserEnabled;
    settingsWebSearchDirectUrlsEl.disabled = !browserEnabled;
  }

  settingsWebSearchEnabledEl.addEventListener("change", updateWebSearchControls);
  settingsWebSearchBrowserEnabledEl.addEventListener("change", updateWebSearchControls);

  async function fetchSettings() {
    try {
      const res = await fetch("/api/settings");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to load settings");
      applySettings(data);
    } catch (err) {
      addSystemMessage(`Failed to load settings: ${err.message}`, true);
    }
  }

  settingsForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    settingsSaveBtn.disabled = true;
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lmstudio_base_url: settingsLmstudioUrlEl.value.trim(),
          lmstudio_api_key: settingsLmstudioKeyEl.value.trim(),
          lemonade_base_url: settingsLemonadeUrlEl.value.trim(),
          lemonade_api_key: settingsLemonadeKeyEl.value.trim(),
          vllm_base_url: settingsVllmUrlEl.value.trim(),
          vllm_api_key: settingsVllmKeyEl.value.trim(),
          opencode_api_plan: settingsOpencodePlanEl.value,
          opencode_api_base_url: opencodeOverrideToSend(),
          // only send a non-empty key; sending the empty field must not erase
          // a stored credential (explicit removal uses opencode_api_clear)
          ...(settingsOpencodeKeyEl.value.trim() ? {opencode_api_key: settingsOpencodeKeyEl.value.trim()} : {}),
          kobold_base_url: settingsKoboldUrlEl.value.trim(),
          kobold_api_key: settingsKoboldKeyEl.value.trim(),
          kobold_gpu_layers: settingsKoboldGpuLayersEl.value.trim(),
          kobold_draft_model: settingsKoboldDraftModelEl.value.trim(),
          kobold_draft_amount: settingsKoboldDraftAmountEl.value.trim(),
          q38rocm_base_url: settingsQ38rocmUrlEl.value.trim(),
          q38rocm_api_key: settingsQ38rocmKeyEl.value.trim(),
          fastflowlm_base_url: settingsFastflowlmUrlEl.value.trim(),
          fastflowlm_api_key: settingsFastflowlmKeyEl.value.trim(),
          lan_default_model: settingsDefaultModelEl.value,
          chat_default_max_tokens: numOrNull(settingsChatDefaultMaxTokensEl),
          chat_max_max_tokens: numOrNull(settingsChatMaxMaxTokensEl),
          chat_max_messages: numOrNull(settingsChatMaxMessagesEl),
          chat_max_message_chars: numOrNull(settingsChatMaxMessageCharsEl),
          chat_max_tool_rounds: numOrNull(settingsChatMaxToolRoundsEl),
          opencode_compaction_keep_tokens: numOrNull(settingsOpencodeCompactionKeepTokensEl),
          opencode_compaction_buffer: numOrNull(settingsOpencodeCompactionBufferEl),
          opencode_compaction_auto: settingsOpencodeCompactionAutoEl.checked,
          opencode_advertise_loaded_context: settingsOpencodeAdvertiseLoadedContextEl.checked,
          opencode_context_fallback: numOrNull(settingsOpencodeContextFallbackEl),
          lemonade_enable_thinking: settingsLemonadeThinkingEl.checked,
          web_search_enabled: settingsWebSearchEnabledEl.checked,
          web_search_mode: settingsWebSearchModeEl.value,
          web_search_max_results: Number(settingsWebSearchMaxResultsEl.value),
          web_search_max_pages: Number(settingsWebSearchMaxPagesEl.value),
          web_search_direct_urls: settingsWebSearchDirectUrlsEl.checked,
          web_search_browser_enabled: settingsWebSearchBrowserEnabledEl.checked,
          tailscale_enabled: settingsTailscaleEnabledEl.checked,
          tailscale_ip_override: settingsTailscaleIpOverrideEl.value.trim(),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to save settings");
      applySettings(data);
      addSystemMessage("Settings saved.");
      await fetchModelsForDefault();
    } catch (err) {
      addSystemMessage(`Failed to save settings: ${err.message}`, true);
    } finally {
      settingsSaveBtn.disabled = false;
    }
  });

  async function runTest(btn, resultEl, target, extra) {
    btn.disabled = true;
    resultEl.textContent = "Testing…";
    resultEl.className = "hint";
    try {
      const res = await fetch("/api/settings/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target, ...extra }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "test failed");
      resultEl.textContent = data.message;
      resultEl.className = `hint ${data.ok ? "test-result-ok" : "test-result-fail"}`;
    } catch (err) {
      resultEl.textContent = err.message;
      resultEl.className = "hint test-result-fail";
    } finally {
      btn.disabled = false;
    }
  }

  settingsTestLmstudioBtn.addEventListener("click", () =>
    runTest(settingsTestLmstudioBtn, settingsTestLmstudioResultEl, "lmstudio", {
      base_url: settingsLmstudioUrlEl.value.trim(),
      api_key: settingsLmstudioKeyEl.value.trim(),
    })
  );
  settingsTestLemonadeBtn.addEventListener("click", () =>
    runTest(settingsTestLemonadeBtn, settingsTestLemonadeResultEl, "lemonade", {
      base_url: settingsLemonadeUrlEl.value.trim(),
      api_key: settingsLemonadeKeyEl.value.trim(),
    })
  );
  settingsTestQ38rocmBtn.addEventListener("click", () =>
    runTest(settingsTestQ38rocmBtn, settingsTestQ38rocmResultEl, "q38rocm", {
      base_url: settingsQ38rocmUrlEl.value.trim(),
      api_key: settingsQ38rocmKeyEl.value.trim(),
    })
  );
  settingsTestVllmBtn.addEventListener("click", () =>
    runTest(settingsTestVllmBtn, settingsTestVllmResultEl, "vllm", {
      base_url: settingsVllmUrlEl.value.trim(),
      api_key: settingsVllmKeyEl.value.trim(),
    })
  );
  settingsTestOpencodeBtn.addEventListener("click", async () => {
    // opencode.ai is configured by the key alone, so testing is configuring:
    // persist what the admin typed (the backend stores it encrypted) and then
    // verify against the live model list. Without this, a successful test left
    // the key unsaved and every picker silently stayed Console-empty.
    const btn = settingsTestOpencodeBtn;
    btn.disabled = true;
    settingsTestOpencodeResultEl.textContent = "Saving plan and key…";
    settingsTestOpencodeResultEl.className = "hint";
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          opencode_api_base_url: settingsOpencodeUrlEl.value.trim(),
          opencode_api_plan: settingsOpencodePlanEl.value,
          opencode_api_key: settingsOpencodeKeyEl.value.trim(),
        }),
      });
      if (!res.ok) {
        const fail = await res.json().catch(() => ({}));
        throw new Error(fail.error || `Saving failed (${res.status})`);
      }
    } catch (err) {
      settingsTestOpencodeResultEl.textContent = err.message;
      settingsTestOpencodeResultEl.className = "hint test-result-fail";
      btn.disabled = false;
      return;
    }
    try {
      await runTest(btn, settingsTestOpencodeResultEl, "opencode", {
        base_url: opencodeOverrideToSend(),
        plan: settingsOpencodePlanEl.value,
        api_key: settingsOpencodeKeyEl.value.trim(),
      });
    } finally {
      btn.disabled = false;
    }
  });
  opencodeClearKeyBtn.addEventListener("click", async () => {
    opencodeClearKeyBtn.disabled = true;
    try {
      const res = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ opencode_api_clear: true }),
      });
      if (!res.ok) {
        const fail = await res.json().catch(() => ({}));
        throw new Error(fail.error || `Removal failed (${res.status})`);
      }
      settingsOpencodeKeyEl.value = "";
      settingsTestOpencodeResultEl.textContent = "Stored opencode.ai key removed; opencode.ai models disappear immediately.";
      settingsTestOpencodeResultEl.className = "hint";
    } catch (err) {
      settingsTestOpencodeResultEl.textContent = err.message;
      settingsTestOpencodeResultEl.className = "hint test-result-fail";
    } finally {
      opencodeClearKeyBtn.disabled = false;
    }
  });
  settingsTestKoboldBtn.addEventListener("click", () =>
    runTest(settingsTestKoboldBtn, settingsTestKoboldResultEl, "kobold", {
      base_url: settingsKoboldUrlEl.value.trim(),
      api_key: settingsKoboldKeyEl.value.trim(),
    })
  );
  settingsTestFastflowlmBtn.addEventListener("click", () =>
    runTest(settingsTestFastflowlmBtn, settingsTestFastflowlmResultEl, "fastflowlm", {
      base_url: settingsFastflowlmUrlEl.value.trim(),
      api_key: settingsFastflowlmKeyEl.value.trim(),
    })
  );
  settingsTestCaddyVllmBtn.addEventListener("click", () =>
    runTest(settingsTestCaddyVllmBtn, settingsTestCaddyResultEl, "caddy_vllm", {})
  );
  settingsTestCaddyKoboldBtn.addEventListener("click", () =>
    runTest(settingsTestCaddyKoboldBtn, settingsTestCaddyResultEl, "caddy_kobold", {})
  );
  settingsTestCaddyFastflowlmBtn.addEventListener("click", () =>
    runTest(settingsTestCaddyFastflowlmBtn, settingsTestCaddyResultEl, "caddy_fastflowlm", {})
  );
  settingsTestCaddyLmstudioBtn.addEventListener("click", () =>
    runTest(settingsTestCaddyLmstudioBtn, settingsTestCaddyResultEl, "caddy_lmstudio", {})
  );
  settingsTestCaddyLemonadeBtn.addEventListener("click", () =>
    runTest(settingsTestCaddyLemonadeBtn, settingsTestCaddyResultEl, "caddy_lemonade", {})
  );
  settingsTestTailscaleLmstudioBtn.addEventListener("click", () =>
    runTest(settingsTestTailscaleLmstudioBtn, settingsTestTailscaleResultEl, "tailscale_lmstudio", {})
  );
  settingsTestTailscaleLemonadeBtn.addEventListener("click", () =>
    runTest(settingsTestTailscaleLemonadeBtn, settingsTestTailscaleResultEl, "tailscale_lemonade", {})
  );
  settingsTestTailscaleVllmBtn.addEventListener("click", () =>
    runTest(settingsTestTailscaleVllmBtn, settingsTestTailscaleResultEl, "tailscale_vllm", {})
  );
  settingsTestTailscaleKoboldBtn.addEventListener("click", () =>
    runTest(settingsTestTailscaleKoboldBtn, settingsTestTailscaleResultEl, "tailscale_kobold", {})
  );
  settingsTestTailscaleFastflowlmBtn.addEventListener("click", () =>
    runTest(settingsTestTailscaleFastflowlmBtn, settingsTestTailscaleResultEl, "tailscale_fastflowlm", {})
  );

  fetchSettings();
  fetchModelsForDefault();
})();
