(() => {
  const BACKEND_LABELS = { lmstudio: "LM Studio", lemonade: "Ryzen AI", vllm: "vLLM", opencode: "opencode.ai", kobold: "KoboldCPP", fastflowlm: "FastFlowLM" };
  const modelListEl = document.getElementById("model-list");
  const modelSelectEl = document.getElementById("model-select");
  let preferredChatModel = modelSelectEl.dataset.preferredModel || "";
  const refreshBtn = document.getElementById("refresh-models");
  const messagesEl = document.getElementById("messages");
  const chatForm = document.getElementById("chat-form");
  const chatInput = document.getElementById("chat-input");
  const sendBtn = document.getElementById("send-btn");
  const documentAttachBtn = document.getElementById("document-attach-btn");
  const documentAttachInput = document.getElementById("document-attach-input");
  let pendingDocuments = [];
  documentAttachBtn.addEventListener("click", () => { if (!busy) documentAttachInput.click(); });
  documentAttachInput.addEventListener("change", () => {
    for (const file of Array.from(documentAttachInput.files || [])) {
      if (pendingDocuments.length >= 5) {
        addSystemMessage("Attach up to five documents per message.", true);
        break;
      } else if (!/\.(pdf|docx|pptx|xlsx|txt|csv)$/i.test(file.name)) {
        addSystemMessage(`Unsupported document: ${file.name}. Choose PDF, Word, PowerPoint, Excel, TXT, or CSV.`, true);
      } else if (file.size > 15 * 1024 * 1024) {
        addSystemMessage(`${file.name} exceeds the 15 MB per-file limit.`, true);
      } else pendingDocuments.push(file);
    }
    documentAttachInput.value = "";
    updateDocumentAttachmentLabel();
  });
  function updateDocumentAttachmentLabel() {
    documentAttachBtn.title = pendingDocuments.length
      ? `Attached: ${pendingDocuments.map(f => f.name).join(", ")} (click to add more)`
      : "Attach PDF, Word, PowerPoint, Excel, TXT, or CSV files";
    documentAttachBtn.classList.toggle("active", pendingDocuments.length > 0);
  }
  const clearBtn = document.getElementById("clear-chat");
  const copyChatBtn = document.getElementById("copy-chat");
  const securityScanBtn = document.getElementById("security-scan-btn");
  const temperatureEl = document.getElementById("temperature");
  const statusDot = document.getElementById("backend-status");
  const statusText = document.getElementById("backend-status-text");
  const optIdentifier = document.getElementById("opt-identifier");
  const optContext = document.getElementById("opt-context");
  const optTtl = document.getElementById("opt-ttl");
  const optGpu = document.getElementById("opt-gpu");
  const historyListEl = document.getElementById("chat-history-list");
  const newChatBtn = document.getElementById("new-chat");
  const sidebarEl = document.getElementById("sidebar");
  const sidebarOverlayEl = document.getElementById("sidebar-overlay");
  const menuToggleBtn = document.getElementById("menu-toggle");
  const multilineToggle = document.getElementById("multiline-toggle");
  const speakToggle = document.getElementById("speak-toggle");
  const micBtn = document.getElementById("mic-btn");
  const translateMicBtn = document.getElementById("translate-mic-btn");
  const translateLanguage = document.getElementById("translate-language");
  const conversationBtn = document.getElementById("conversation-btn");
  const voiceStatusEl = document.getElementById("voice-status");
  const composerHintEl = document.getElementById("composer-hint");
  const modelsToggleBtn = document.getElementById("models-toggle");
  const modelsPanelEl = document.getElementById("models-panel");
  const helpToggleBtn = document.getElementById("help-toggle");
  const helpPanelEl = document.getElementById("help-panel");
  const copyCaCertBtn = document.getElementById("copy-ca-cert");
  const copyConfigYamlBtn = document.getElementById("copy-config-yaml");
  const copyMacTrustBtn = document.getElementById("copy-mac-trust");
  const copyWindowsTrustBtn = document.getElementById("copy-windows-trust");
  const copyOpencodeConfigBtn = document.getElementById("copy-opencode-config");
  const copyTailscaleConfigYamlBtn = document.getElementById("copy-tailscale-config-yaml");
  const copyTailscaleOpencodeConfigBtn = document.getElementById("copy-tailscale-opencode-config");
  const copyTailscaleLinuxTrustBtn = document.getElementById("copy-tailscale-linux-trust");
  const copyTailscaleMacTrustBtn = document.getElementById("copy-tailscale-mac-trust");
  const copyTailscaleWindowsTrustBtn = document.getElementById("copy-tailscale-windows-trust");

  let models = [];
  let history = [];
  // Wrap elements for the user/assistant bubbles, kept strictly parallel to `history`
  // (same length, same order — system/error bubbles are NOT included). Lets edit /
  // regenerate / retry map a bubble back to its live history index via indexOf, robust
  // to messages being added or removed in between.
  let msgWraps = [];
  let busy = false;
  let conversations = [];
  let currentConversationId = null;
  let multilineMode = localStorage.getItem("multilineMode") === "1";
  let speakMode = localStorage.getItem("speakMode") === "1";
  let generationPollTimer = null;
  let conversationViewVersion = 0;
  let renderedMessagesKey = null;
  let activeReplyEl = null;
  let visibleGeneration = null;
  let generationPlayback = null;
  let latestConversationRequest = 0;
  let generationErrorShown = false;
  let mediaRecorder = null;
  let recordedChunks = [];
  let conversationMode = false;
  let vadAudioContext = null;
  let vadAnalyser = null;
  let vadStream = null;
  let vadRafId = null;
  let vadListening = false;
  let vadSpeechStart = null;
  let vadSilenceStart = null;
  let conversationRecorder = null;
  let conversationChunks = [];
  let voicePlaybackContext = null;

  function unlockVoicePlayback() {
    // iPhone Chrome uses WebKit's autoplay rules. Resume/create the audio session
    // synchronously inside a real tap; TTS arrives later after fetch/model latency,
    // but sources played through this approved context remain audible.
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;
    if (!voicePlaybackContext || voicePlaybackContext.state === "closed") {
      voicePlaybackContext = new AudioContextClass();
    }
    if (voicePlaybackContext.state === "suspended") {
      voicePlaybackContext.resume().catch(() => {});
    }
    // A zero-gain one-sample source commits the audio session during the gesture;
    // resume() alone is insufficient on some iOS/WebKit versions.
    try {
      const buffer = voicePlaybackContext.createBuffer(1, 1, voicePlaybackContext.sampleRate);
      const source = voicePlaybackContext.createBufferSource();
      const gain = voicePlaybackContext.createGain();
      gain.gain.value = 0;
      source.buffer = buffer;
      source.connect(gain).connect(voicePlaybackContext.destination);
      source.start(0);
    } catch (_) {}
  }

  function openSidebar() {
    sidebarEl.classList.add("open");
    sidebarOverlayEl.classList.add("open");
  }

  function closeSidebar() {
    sidebarEl.classList.remove("open");
    sidebarOverlayEl.classList.remove("open");
  }

  function toggleSidebar() {
    sidebarEl.classList.contains("open") ? closeSidebar() : openSidebar();
  }

  menuToggleBtn.addEventListener("click", toggleSidebar);
  sidebarOverlayEl.addEventListener("click", closeSidebar);

  // Admin-only: the Models/Users buttons and panels don't exist in the DOM at all
  // for non-admin users (server-side gated), so every reference to them below
  // must be guarded.
  function makeFlyoutPanel(toggleBtn, panelEl) {
    function open() {
      panelEl.classList.remove("hidden");
    }
    function close() {
      panelEl.classList.add("hidden");
    }
    function toggle() {
      panelEl.classList.contains("hidden") ? open() : close();
    }
    toggleBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      toggle();
    });
    document.addEventListener("click", (e) => {
      if (panelEl.classList.contains("hidden")) return;
      if (panelEl.contains(e.target) || e.target === toggleBtn) return;
      close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") close();
    });
    return { open, close };
  }

  if (modelsToggleBtn) makeFlyoutPanel(modelsToggleBtn, modelsPanelEl);
  if (helpToggleBtn) makeFlyoutPanel(helpToggleBtn, helpPanelEl);

  function wireCopyButton(btn, sourceEl) {
    if (!btn) return;
    btn.addEventListener("click", () => {
      navigator.clipboard
        .writeText(sourceEl.textContent)
        .then(() => {
          const original = btn.textContent;
          btn.textContent = "✓";
          setTimeout(() => {
            btn.textContent = original;
          }, 1200);
        })
        .catch(() => addSystemMessage("Could not copy to clipboard.", true));
    });
  }

  wireCopyButton(copyCaCertBtn, document.getElementById("help-ca-cert"));
  wireCopyButton(copyConfigYamlBtn, document.getElementById("help-config-yaml"));
  wireCopyButton(copyMacTrustBtn, document.getElementById("help-mac-trust"));
  wireCopyButton(copyWindowsTrustBtn, document.getElementById("help-windows-trust"));
  wireCopyButton(copyOpencodeConfigBtn, document.getElementById("help-opencode-config"));
  wireCopyButton(copyTailscaleConfigYamlBtn, document.getElementById("help-tailscale-config-yaml"));
  wireCopyButton(copyTailscaleOpencodeConfigBtn, document.getElementById("help-tailscale-opencode-config"));
  wireCopyButton(copyTailscaleLinuxTrustBtn, document.getElementById("help-tailscale-linux-trust"));
  wireCopyButton(copyTailscaleMacTrustBtn, document.getElementById("help-tailscale-mac-trust"));
  wireCopyButton(copyTailscaleWindowsTrustBtn, document.getElementById("help-tailscale-windows-trust"));

  function updateComposerHint() {
    composerHintEl.textContent = multilineMode
      ? "Enter for new line · Ctrl+Enter (or ⌘+Enter) to send"
      : "Enter to send · Shift+Enter for new line";
  }

  function updateMultilineBtn() {
    multilineToggle.classList.toggle("active", multilineMode);
    multilineToggle.setAttribute("aria-pressed", multilineMode ? "true" : "false");
  }

  updateMultilineBtn();
  updateComposerHint();

  multilineToggle.addEventListener("click", () => {
    multilineMode = !multilineMode;
    localStorage.setItem("multilineMode", multilineMode ? "1" : "0");
    updateMultilineBtn();
    updateComposerHint();
  });

  const insertCodeBtn = document.getElementById("insert-code-btn");
  const codeModalOverlay = document.getElementById("code-modal-overlay");
  const codeModalTextarea = document.getElementById("code-modal-textarea");
  const codeModalLang = document.getElementById("code-modal-lang");
  const codeModalClose = document.getElementById("code-modal-close");
  const codeModalCancel = document.getElementById("code-modal-cancel");
  const codeModalInsert = document.getElementById("code-modal-insert");

  function openCodeModal() {
    codeModalOverlay.classList.remove("hidden");
    codeModalTextarea.value = "";
    codeModalLang.value = "";
    codeModalTextarea.focus();
  }

  function closeCodeModal() {
    codeModalOverlay.classList.add("hidden");
  }

  insertCodeBtn.addEventListener("click", openCodeModal);
  codeModalClose.addEventListener("click", closeCodeModal);
  codeModalCancel.addEventListener("click", closeCodeModal);
  codeModalOverlay.addEventListener("click", (e) => {
    if (e.target === codeModalOverlay) closeCodeModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !codeModalOverlay.classList.contains("hidden")) closeCodeModal();
  });

  codeModalInsert.addEventListener("click", () => {
    const code = codeModalTextarea.value;
    if (!code.trim()) {
      closeCodeModal();
      return;
    }
    const fence = "```" + codeModalLang.value + "\n" + code.replace(/\n?$/, "\n") + "```\n";
    const start = chatInput.selectionStart ?? chatInput.value.length;
    const end = chatInput.selectionEnd ?? chatInput.value.length;
    const before = chatInput.value.slice(0, start);
    const after = chatInput.value.slice(end);
    const needsLeadingNewline = before.length > 0 && !before.endsWith("\n");
    chatInput.value = before + (needsLeadingNewline ? "\n" : "") + fence + after;
    chatInput.style.height = "auto";
    chatInput.style.height = Math.min(chatInput.scrollHeight, 500) + "px";
    closeCodeModal();
    chatInput.focus();
    const caret = before.length + (needsLeadingNewline ? 1 : 0) + fence.length;
    chatInput.setSelectionRange(caret, caret);
  });

  function updateSpeakBtn() {
    speakToggle.classList.toggle("active", speakMode);
    speakToggle.setAttribute("aria-pressed", speakMode ? "true" : "false");
  }

  updateSpeakBtn();

  speakToggle.addEventListener("click", () => {
    speakMode = !speakMode;
    if (speakMode) unlockVoicePlayback();
    localStorage.setItem("speakMode", speakMode ? "1" : "0");
    updateSpeakBtn();
  });

  function setStatus(ok, text) {
    statusDot.className = "status-dot " + (ok ? "ok" : "err");
    statusText.textContent = text;
  }

  async function fetchModels() {
    try {
      const res = await fetch("/api/models");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to list models");
      models = data.models || [];
      setStatus(true, `${models.length} model(s) on disk`);
      renderModelList();
      renderModelSelect();
      refreshHelp();
    } catch (err) {
      setStatus(false, err.message);
      if (modelListEl) {
        modelListEl.innerHTML = `<p class="hint">Could not reach LM Studio.<br>${escapeHtml(err.message)}</p>`;
      }
    }
  }

  // Model state can change from another Hearth tab, a runtime restart, or a model
  // loaded outside the current page. Keep the chat picker in sync without a manual
  // reload; fetchModels preserves the current selection when it is still available.
  setInterval(() => {
    if (!document.hidden) fetchModels();
  }, 15000);

  function formatModelSize(gb) {
    if (typeof gb !== "number" || gb <= 0) return null;
    return gb < 1 ? `${Math.round(gb * 1000)} MB` : `${gb.toFixed(1)} GB`;
  }

  // Keep the Help panel current with what's actually downloaded/loadable. All of the help's
  // machine-specific data (LAN IP, Caddy token/ports, CA cert, and the Continue / OpenCode
  // config blocks built from the live model list) is generated server-side at /api/help and
  // rendered into the page at load. After any model change (download / load / unload /
  // delete — all call fetchModels()) we re-fetch it so a newly downloaded model shows up in
  // the configs without a full reload. The initial call is skipped because the page was just
  // server-rendered with fresh data.
  let helpRefreshedOnce = false;
  async function refreshHelp() {
    if (!document.getElementById("help-opencode-config")) return; // Help not in DOM
    if (!helpRefreshedOnce) { helpRefreshedOnce = true; return; }  // load render is already fresh
    try {
      const res = await fetch("/api/help");
      if (!res.ok) return;
      const d = await res.json();
      const set = (id, val) => {
        const el = document.getElementById(id);
        if (el && typeof val === "string") el.textContent = val;
      };
      set("help-flm-status", d.flm_status);
      set("help-opencode-config", d.opencode_config);
      set("help-config-yaml", d.continue_yaml);
      set("help-ca-cert", d.ca_cert);
      set("help-tailscale-config-yaml", d.tailscale_continue_yaml);
      set("help-tailscale-opencode-config", d.tailscale_opencode_config);
      for (const [name, command] of Object.entries(d.tailscale_ca_trust_commands || {})) {
        set(`help-tailscale-${name === "macos" ? "mac" : name}-trust`, command);
      }
      for (const [name, url] of Object.entries(d.tailscale_links || {})) {
        const link = document.getElementById(`help-tailscale-${name}-link`);
        if (link) {
          link.href = url;
          if (name === "chat") link.textContent = `Chat — ${url}`;
        }
      }
    } catch { /* keep the server-rendered content on any failure */ }
  }

  function buildModelCard(m) {
    const loaded = m.state === "loaded";
    const unavailable = m.loadable === false;
    const externallyManaged = m.externally_managed === true;
    const card = document.createElement("div");
    card.className = "model-card";
    // Meta line: publisher · quantization · type · size — only the parts that exist,
    // so a backend that doesn't report one (e.g. LM Studio has no on-disk size) just
    // shows fewer segments instead of stray separators.
    const size = formatModelSize(m.size_gb);
    const metaParts = [m.publisher, m.quantization, m.type].filter(Boolean).map(escapeHtml);
    const metaHtml = metaParts.join(" &middot; ")
      + (size ? ` &middot; <span class="model-size">${escapeHtml(size)}</span>` : "");
    card.innerHTML = `
      <div class="model-card-top">
        <div>
          <div class="model-name">${escapeHtml(m.id)}</div>
          <div class="model-meta">${metaHtml}</div>
        </div>
        <span class="badge ${loaded ? "loaded" : "not-loaded"}">${loaded ? "loaded" : (unavailable ? "converted · blocked" : "not loaded")}</span>
      </div>
      <div class="model-card-actions"></div>
    `;
    const actions = card.querySelector(".model-card-actions");
    if (m.resource_warning) {
      // Same warning text the Models page shows, placed directly above the Load
      // button — this panel is where models actually get loaded from during a chat,
      // so a model that previously hung the machine must announce itself here too.
      const warning = document.createElement("div");
      warning.className = "model-load-warning";
      const label = document.createElement("div");
      label.className = "model-load-warning-label";
      label.textContent = `⚠ ${m.resource_warning.label}`;
      const message = document.createElement("div");
      message.textContent = m.resource_warning.summary;
      warning.append(label, message);
      actions.appendChild(warning);
    }
    if (externallyManaged) {
      const note = document.createElement("span");
      note.className = "hint";
      note.textContent = "Hosted remotely; managed by opencode.ai";
      actions.appendChild(note);
    } else if (loaded) {
      const unloadBtn = document.createElement("button");
      unloadBtn.textContent = "Unload";
      unloadBtn.className = "danger";
      unloadBtn.onclick = () => unloadModel(m.id);
      actions.appendChild(unloadBtn);
    } else if (unavailable) {
      const blocker = document.createElement("span");
      blocker.className = "hint";
      blocker.textContent = m.load_error || "Runtime support is not ready.";
      blocker.title = blocker.textContent;
      actions.appendChild(blocker);
      const loadBtn = document.createElement("button");
      loadBtn.textContent = "Load";
      loadBtn.onclick = () => addSystemMessage(
        `Cannot load ${m.id}: ${m.load_error || "runtime support is not ready"}`, true,
      );
      actions.appendChild(loadBtn);
    } else {
      const loadBtn = document.createElement("button");
      loadBtn.textContent = "Load";
      loadBtn.onclick = () => loadModel(m.id);
      actions.appendChild(loadBtn);
    }
    if (m.type !== "embeddings" && m.deletable !== false) {
      // Embedding model is exempt — deleting it would quietly break cross-chat
      // memory for everyone, and it's ~80MB, not worth offering to reclaim.
      const deleteBtn = document.createElement("button");
      deleteBtn.textContent = "Delete";
      deleteBtn.className = "danger";
      deleteBtn.title = "Remove from disk permanently";
      deleteBtn.onclick = () => deleteModel(m.id);
      actions.appendChild(deleteBtn);
    }
    return card;
  }

  function renderModelList() {
    if (!modelListEl) return; // non-admin: no Models panel in the DOM
    if (models.length === 0) {
      modelListEl.innerHTML = `<p class="hint">No models found on disk.</p>`;
      return;
    }
    modelListEl.innerHTML = "";
    const groups = [
      { key: "lmstudio", label: "LM Studio" },
      { key: "lemonade", label: "Ryzen AI (NPU)" },
      { key: "vllm", label: "vLLM" },
      { key: "kobold", label: "KoboldCPP" },
      { key: "fastflowlm", label: "FastFlowLM (NPU)" },
    ];
    for (const group of groups) {
      const groupModels = models.filter((m) => m.backend === group.key);
      if (groupModels.length === 0) continue;
      const heading = document.createElement("div");
      heading.className = "model-group-heading";
      heading.textContent = group.label;
      modelListEl.appendChild(heading);
      for (const m of groupModels) {
        modelListEl.appendChild(buildModelCard(m));
      }
    }
    // Anything without a recognized backend tag (shouldn't happen, but don't let it
    // silently disappear if a future backend forgets to set one).
    for (const m of models.filter((m) => !groups.some((g) => g.key === m.backend))) {
      modelListEl.appendChild(buildModelCard(m));
    }
  }

  const NON_CHAT_TYPES = ["embeddings", "transcription", "tts", "image", "video"];

  function renderModelSelect() {
    // Embedding/transcription/TTS/image models aren't a chat target (they'd just produce
    // broken responses) — admins still see them in the Models panel for load/unload
    // (Whisper/Kokoro need to stay loaded for voice input/output to work), but never in
    // this chat picker. Non-admins never see them at all (filtered server-side already).
    const loaded = models.filter((m) => m.state === "loaded" && !NON_CHAT_TYPES.includes(m.type));
    const previous = preferredChatModel || modelSelectEl.value;
    modelSelectEl.innerHTML = "";
    if (preferredChatModel && !loaded.some((m) => m.id === preferredChatModel)) {
      const unavailable = new Option(`${preferredChatModel} · unavailable`, "", true, true);
      unavailable.disabled = true;
      modelSelectEl.add(unavailable);
    } else if (!loaded.length) {
      modelSelectEl.add(new Option("No model loaded", ""));
    }
    for (const m of loaded) {
      const opt = document.createElement("option");
      opt.value = m.id;
      const label = BACKEND_LABELS[m.backend];
      const displayName = (m.name && m.name !== m.id) ? m.name : m.id;
      const typeLabel = (m.quantization || "").replace("remote opencode.ai (", "").replace(")", "");
      opt.textContent = `${displayName} · ${typeLabel || label || m.backend}`;
      opt.title = opt.textContent;
      modelSelectEl.appendChild(opt);
    }
    if (loaded.some((m) => m.id === previous)) {
      modelSelectEl.value = previous;
    }
    // The selector is width-capped and truncates long ids — expose the full current
    // name on hover so nothing is lost.
    modelSelectEl.title = modelSelectEl.selectedOptions[0]?.textContent || "";
  }

  modelSelectEl.addEventListener("change", async () => {
    const selected = modelSelectEl.value;
    if (!selected) return;
    preferredChatModel = selected;
    modelSelectEl.title = modelSelectEl.selectedOptions[0]?.textContent || "";
    try {
      const response = await fetch("/api/chat/model-preference", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({model: selected}),
        keepalive: true,
      });
      if (!response.ok) throw new Error("Could not save the chat model preference.");
    } catch {
      if (preferredChatModel === selected) addSystemMessage("Could not save the chat model preference.", true);
    }
  });

  async function loadModel(modelKey) {
    // The server keeps one chat/reasoning model loaded across all backends. Small
    // support models are exempt because they cannot answer chat requests.
    const targetBackend = models.find((m) => m.id === modelKey)?.backend;
    const supportTypes = new Set(["embeddings", "embedding", "transcription", "tts"]);
    const otherLoaded = models.filter(
      (m) => m.state === "loaded" && m.id !== modelKey
        && m.externally_managed !== true
        && !supportTypes.has(m.type)
    );
    if (otherLoaded.length > 0 || targetBackend === "lemonade") {
      const lines = [`Load ${modelKey}?`];
      if (otherLoaded.length > 0) {
        const names = otherLoaded.map((m) => m.id).join(", ");
        lines.push(`The currently loaded chat model will be unloaded: ${names}.`);
      }
      if (targetBackend === "lemonade") {
        lines.push("Voice input, voice output, and embedding/RAG support models will be kept loaded.");
      }
      const ok = window.confirm(
        lines.join("\n\n")
      );
      if (!ok) return;
    }

    setStatus(true, `Loading ${modelKey}…`);
    try {
      const res = await fetch("/api/models/load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: modelKey,
          identifier: optIdentifier.value.trim() || undefined,
          context_length: optContext.value ? Number(optContext.value) : undefined,
          ttl: optTtl.value ? Number(optTtl.value) : undefined,
          gpu: optGpu.value.trim() || undefined,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "load failed");
      if (data.unloaded && data.unloaded.length > 0) {
        addSystemMessage(`Unloaded ${data.unloaded.join(", ")} to load ${modelKey}`);
      }
      addSystemMessage(`Loaded ${modelKey}`);
    } catch (err) {
      addSystemMessage(`Failed to load ${modelKey}: ${err.message}`, true);
    }
    await fetchModels();
  }

  async function unloadModel(identifier) {
    try {
      const res = await fetch("/api/models/unload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ identifier }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "unload failed");
      addSystemMessage(`Unloaded ${identifier}`);
    } catch (err) {
      addSystemMessage(`Failed to unload ${identifier}: ${err.message}`, true);
    }
    await fetchModels();
  }

  async function deleteModel(identifier) {
    const ok = window.confirm(
      `Permanently delete "${identifier}" from disk? This frees up storage (and memory, if it's ` +
        `loaded) but can't be undone — you'd need to re-download it.`
    );
    if (!ok) return;
    try {
      const res = await fetch("/api/models/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ identifier }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "delete failed");
      addSystemMessage(`Deleted ${identifier}`);
    } catch (err) {
      addSystemMessage(`Failed to delete ${identifier}: ${err.message}`, true);
    }
    await fetchModels();
  }

  async function fetchConversations() {
    try {
      const res = await fetch("/api/conversations");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to list conversations");
      conversations = data.conversations || [];
      renderConversationList();
    } catch (err) {
      historyListEl.innerHTML = `<p class="hint">Could not load history.<br>${escapeHtml(err.message)}</p>`;
    }
  }

  // Bridge for the in-chat quiz platform (quiz_chat.js): it renders its own bubbles but
  // needs a real conversation to attach its Q&A to, so a practice test is saved in chat
  // history like any other chat. We deliberately do NOT push quiz turns into the LLM `history`
  // array — a 25-question test shouldn't balloon the model's context — persistence is
  // server-side only, and reloading the conversation rebuilds it from stored messages.
  window.ChatApp = {
    ensureConversationId() {
      if (!currentConversationId) currentConversationId = crypto.randomUUID();
      return currentConversationId;
    },
    getConversationId: () => currentConversationId,
    refreshConversations: () => fetchConversations(),
    loadConversation: (id) => loadConversation(id),
    getModel: () => modelSelectEl.value,
  };

  function renderConversationList() {
    if (conversations.length === 0) {
      historyListEl.innerHTML = `<p class="hint">No chats yet.</p>`;
      return;
    }
    historyListEl.innerHTML = "";
    for (const c of conversations) {
      const item = document.createElement("div");
      item.className = "history-item" + (c.id === currentConversationId ? " active" : "");
      const title = document.createElement("span");
      title.className = "history-item-title";
      title.textContent = c.title || "New chat";
      title.title = c.title || "New chat";
      const del = document.createElement("button");
      del.className = "history-item-delete";
      del.textContent = "×";
      del.title = "Delete chat";
      del.onclick = (e) => {
        e.stopPropagation();
        deleteConversation(c.id);
      };
      item.appendChild(title);
      item.appendChild(del);
      item.onclick = () => loadConversation(c.id);
      historyListEl.appendChild(item);
    }
  }

  function newChat() {
    clearTimeout(generationPollTimer);
    conversationViewVersion++;
    renderedMessagesKey = null;
    activeReplyEl = null;
    visibleGeneration = null;
    generationPlayback = null;
    setChatBusy(false);
    sessionStorage.removeItem("hearth-current-chat");
    currentConversationId = null;
    history = [];
    msgWraps = [];
    messagesEl.innerHTML = "";
    renderConversationList();
    closeSidebar();
    chatInput.focus();
  }

  function setChatBusy(value, stopping = false) {
    busy = value;
    sendBtn.textContent = value ? (stopping ? "Stopping…" : "Stop") : "Send";
    sendBtn.classList.toggle("stop-btn", value);
    sendBtn.disabled = stopping;
  }

  function scheduleChatRefresh(id, version, delay = 250) {
    clearTimeout(generationPollTimer);
    generationPollTimer = setTimeout(() => {
      if (currentConversationId === id && conversationViewVersion === version && !document.hidden) {
        loadConversation(id, { refresh: true });
      }
    }, delay);
  }

  function renderConversationSnapshot(data) {
    const generation = data.generation;
    visibleGeneration = generation;
    const running = generation?.status === "running";
    const saved = [...data.messages];
    // A newly accepted job can be observed before its worker saves the user turn.
    if (running && saved.length <= generation.base_message_count && generation.user_text) {
      saved.push({ role: "user", content: generation.user_text, created_at: generation.started_at });
    }
    const stickToBottom = renderedMessagesKey === null ||
      messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 80;
    const scrollTop = messagesEl.scrollTop;
    const key = JSON.stringify([saved, generation?.status === "stopped"]);
    if (key !== renderedMessagesKey) {
      renderedMessagesKey = key;
      history = saved.map((m) => ({ role: m.role, content: m.content }));
      msgWraps = [];
      activeReplyEl = null;
      generationErrorShown = false;
      messagesEl.innerHTML = "";
      for (const [index, m] of saved.entries()) {
        const el = addBubble(m.role, m.content, {
          timestamp: new Date(m.created_at * 1000), duration: m.duration,
          model: m.model, backend: m.backend, totalTokens: m.total_tokens,
          topsUsed: m.tops_used, topsMax: m.tops_max, npuPercent: m.npu_percent,
          stopped: generation?.status === "stopped" && m === saved.at(-1) && m.role === "assistant",
        }, m.reasoning);
        msgWraps.push(el._wrapEl);
        if (m.role === "user") addUserControls(el._wrapEl);
        else if (m.role === "assistant") {
          addAssistantControls(el._wrapEl);
          const agentButton = document.getElementById("chat-create-agent-btn");
          if (agentButton && m.backend === "hearth-agent-draft") {
            const origin = saved.slice(0, index).reverse().find(message => message.role === "user");
            const action = document.createElement("button");
            action.type = "button";
            action.className = "panel-btn";
            action.textContent = "Open assignment draft";
            action.addEventListener("click", () => openAgentDraft(origin?.content || "", agentButton.dataset.draftUrl));
            el.append(action);
          }
          if (agentButton && m.backend === "hearth-agent-schedule") {
            const action = document.createElement("a");
            action.className = "panel-btn";
            action.href = agentButton.dataset.schedulesUrl;
            action.textContent = "Open Agent schedules";
            el.append(action);
          }
        }
      }
    }
    // The persisted assistant wins as soon as it is saved; never append it twice.
    if (running && data.messages.length <= generation.base_message_count + 1) {
      if (!activeReplyEl) {
        activeReplyEl = addBubble("assistant", "");
        addAssistantControls(activeReplyEl._wrapEl);
      }
      if (generation.content || generation.reasoning) {
        clearWaitingIndicator(activeReplyEl);
        if (activeReplyEl._renderedContent !== generation.content) {
          activeReplyEl._answerEl.innerHTML = renderMarkdown(generation.content || "", true);
          activeReplyEl._renderedContent = generation.content;
        }
      }
      activeReplyEl._thinkingContentEl.textContent = generation.reasoning || "";
      activeReplyEl._thinkingEl.classList.toggle("hidden", !generation.reasoning);
      activeReplyEl._thinkingEl.classList.toggle("live", !generation.content);
      activeReplyEl._thinkingEl.open = !!generation.reasoning && !generation.content;
    } else if (activeReplyEl) {
      activeReplyEl._wrapEl.remove();
      activeReplyEl = null;
    }
    if (generation?.error && !generationErrorShown) {
      if (generation.preserved_reply) addFailedRegeneration(generation.error, generation.preserved_reply, generation.base_message_count - 1, generation.user_text);
      else addChatError(generation.error, generation.user_text, generation.base_message_count);
      generationErrorShown = true;
    }
    setChatBusy(running, !!generation?.stop_requested && running);
    renderLinkedAgents(data.agents || []);
    if (stickToBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
    else messagesEl.scrollTop = scrollTop;
    updateGenerationPlayback(data);
  }

  const agentReportAttempts = new Map();

  function renderLinkedAgents(agents) {
    let panel = document.getElementById("chat-linked-agents");
    if (!agents.length) {
      panel?.remove();
      return;
    }
    if (!panel) {
      panel = document.createElement("section");
      panel.id = "chat-linked-agents";
      panel.className = "chat-linked-agents";
      panel.setAttribute("aria-label", "Agent results");
      messagesEl.append(panel);
    }
    const model = modelSelectEl.value;
    const signature = JSON.stringify([agents, model, busy, [...agentReportAttempts]]);
    if (panel.dataset.signature === signature) return;
    panel.dataset.signature = signature;
    panel.replaceChildren();
    for (const agent of agents) {
      const card = document.createElement("article");
      card.className = "msg assistant";
      const heading = document.createElement("strong");
      heading.textContent = `${agent.title} · ${(agent.status || "").replaceAll("_", " ")}`;
      const result = document.createElement("div");
      result.className = "agent-chat-report";
      result.innerHTML = renderMarkdown(agent.formatted_summary || agent.summary || "");
      const note = document.createElement("div");
      note.className = "hint";
      note.textContent = [agent.completion, agent.error].filter(Boolean).join("\n");
      const schedules = document.createElement("div");
      schedules.className = "hint";
      schedules.textContent = (agent.schedules || []).map(schedule => {
        const time = schedule.run_time ? ` at ${schedule.run_time} ${schedule.timezone}` : "";
        const next = schedule.enabled && schedule.next_run_at
          ? ` · next ${new Date(schedule.next_run_at * 1000).toLocaleString()}` : "";
        return `${schedule.enabled ? "Scheduled" : "Schedule paused"} · every ${schedule.interval_value} ${schedule.interval_unit}${time}${next}`;
      }).join("\n") || "No recurring schedule saved.";
      const link = document.createElement("a");
      link.href = `/agents/${encodeURIComponent(agent.id)}`;
      link.textContent = "Open assignment and full result";
      const scheduleLink = document.createElement("a");
      scheduleLink.href = agent.schedules?.length ? "/agents/schedules" :
        `/agents/schedules?task=${encodeURIComponent(agent.id)}`;
      scheduleLink.textContent = agent.schedules?.length ? "Manage schedules" : "Set up a schedule";
      card.append(heading, schedules, result);
      if (agent.formatted_summary && agent.summary) {
        const original = document.createElement("details");
        const label = document.createElement("summary");
        label.textContent = "Original agent result";
        const raw = document.createElement("div");
        raw.textContent = agent.summary;
        original.append(label, raw);
        card.append(original);
      } else if (agent.status === "completed" && agent.summary) {
        const key = `${agent.id}:${agent.run_count}:${model}`;
        const state = agentReportAttempts.get(key);
        const formattingStatus = document.createElement("div");
        formattingStatus.className = "hint";
        formattingStatus.setAttribute("role", "status");
        if (!model) formattingStatus.textContent = "Select a chat model to format this result.";
        else if (state === "failed") {
          formattingStatus.textContent = "The selected model couldn’t format this result. The original is shown.";
          const retry = document.createElement("button");
          retry.type = "button";
          retry.className = "panel-btn";
          retry.textContent = "Retry formatting";
          retry.addEventListener("click", () => {
            agentReportAttempts.delete(key);
            renderLinkedAgents(agents);
          });
          card.append(retry);
        } else if (state || !busy) {
          formattingStatus.textContent = "Formatting result with the chat model…";
          if (!state && !busy) void formatLinkedAgent(agent, model, key);
        }
        card.append(formattingStatus);
      }
      card.append(note, link, scheduleLink);
      panel.append(card);
    }
  }

  async function formatLinkedAgent(agent, model, key) {
    agentReportAttempts.set(key, "pending");
    try {
      const response = await fetch(`/api/agent-tasks/${encodeURIComponent(agent.id)}/chat-report`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({model}),
      });
      if (!response.ok) throw new Error("Formatting failed.");
      if (response.status === 202) agentReportAttempts.delete(key);
      else agentReportAttempts.set(key, "done");
    } catch {
      agentReportAttempts.set(key, "failed");
    }
  }

  function updateGenerationPlayback(data) {
    const playback = generationPlayback;
    const generation = data.generation;
    if (!playback || !generation || playback.id !== generation.id || playback.finished) return;
    const reply = generation.status === "running" ? generation.content :
      (data.messages.at(-1)?.role === "assistant" ? data.messages.at(-1).content : generation.content);
    const spokenText = (reply || "").split("**Pages browsed (screenshots):**")[0];
    if (playback.shouldSpeak) {
      playback.spokenUpTo = enqueueSentencesFromStream(spokenText, playback.spokenUpTo);
    }
    if (generation.status !== "running") {
      playback.finished = true;
      if (playback.shouldSpeak && spokenText.slice(playback.spokenUpTo).trim()) {
        enqueueSpeech(spokenText.slice(playback.spokenUpTo));
      }
      if (conversationMode) {
        showVoiceStatus("Speaking…");
        waitForSpeechQueueDrain().then(() => {
          if (conversationMode && generationPlayback === playback && currentConversationId === data.id) {
            beginListeningCycle();
          }
        });
      }
      fetchConversations();
    }
  }

  async function loadConversation(id, { refresh = false } = {}) {
    if (!refresh) {
      clearTimeout(generationPollTimer);
      conversationViewVersion++;
      currentConversationId = id;
      renderedMessagesKey = null;
      activeReplyEl = null;
      generationPlayback = null;
    }
    const version = conversationViewVersion;
    const requestNumber = ++latestConversationRequest;
    try {
      const res = await fetch(`/api/conversations/${id}`, { cache: "no-store" });
      const data = await res.json();
      // A slow response for a previous chat must never overwrite the current view.
      if (currentConversationId !== id || conversationViewVersion !== version || requestNumber !== latestConversationRequest) return false;
      if (res.status === 404) { newChat(); return false; }
      if (!res.ok) throw new Error(data.error || "failed to load chat");
      sessionStorage.setItem("hearth-current-chat", id);
      renderConversationSnapshot(data);
      renderConversationList();
      if (!refresh) {
        closeSidebar();
        if (window.Quiz?.resumeInConversation) window.Quiz.resumeInConversation(data.id);
      }
      if (data.generation?.status === "running") scheduleChatRefresh(id, version);
      else if (data.agents?.length) scheduleChatRefresh(id, version, 3000);
      return true;
    } catch (err) {
      if (currentConversationId !== id || conversationViewVersion !== version || requestNumber !== latestConversationRequest) return false;
      // Losing the network only loses progress updates, never the server's job.
      if (!refresh) addSystemMessage(`Failed to load chat: ${err.message}`, true);
      scheduleChatRefresh(id, version, 2000);
      return false;
    }
  }

  function recoverVisibleChat() {
    if (!document.hidden && currentConversationId) {
      loadConversation(currentConversationId, { refresh: true });
    }
  }
  document.addEventListener("visibilitychange", recoverVisibleChat);
  window.addEventListener("focus", recoverVisibleChat);
  window.addEventListener("pageshow", recoverVisibleChat);

  async function deleteConversation(id) {
    try {
      await fetch(`/api/conversations/${id}`, { method: "DELETE" });
      if (id === currentConversationId) newChat();
      await fetchConversations();
    } catch (err) {
      addSystemMessage(`Failed to delete chat: ${err.message}`, true);
    }
  }

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ---------- Markdown-lite rendering + code blocks ----------

  const LANG_EXTENSIONS = {
    python: "py", py: "py",
    javascript: "js", js: "js",
    typescript: "ts", ts: "ts",
    jsx: "jsx", tsx: "tsx",
    json: "json",
    html: "html", htm: "html",
    css: "css", scss: "scss",
    java: "java",
    c: "c",
    cpp: "cpp", "c++": "cpp",
    csharp: "cs", cs: "cs",
    go: "go", golang: "go",
    rust: "rs", rs: "rs",
    ruby: "rb", rb: "rb",
    php: "php",
    sql: "sql",
    bash: "sh", sh: "sh", shell: "sh", zsh: "sh",
    powershell: "ps1", ps1: "ps1",
    yaml: "yaml", yml: "yaml",
    xml: "xml",
    markdown: "md", md: "md",
    dockerfile: "Dockerfile",
    toml: "toml",
    ini: "ini",
    kotlin: "kt",
    swift: "swift",
    r: "r",
    lua: "lua",
    perl: "pl",
    scala: "scala",
    dart: "dart",
  };

  function extForLang(lang) {
    const key = (lang || "").toLowerCase();
    return LANG_EXTENSIONS[key] || "txt";
  }

  const CODE_KEYWORDS = new Set([
    "function", "def", "class", "return", "if", "else", "elif", "for", "foreach", "while", "import",
    "from", "export", "default", "const", "let", "var", "public", "private", "protected", "static",
    "void", "int", "float", "double", "bool", "boolean", "string", "true", "false", "null", "nil",
    "none", "self", "this", "new", "try", "except", "catch", "finally", "throw", "throws", "switch",
    "case", "break", "continue", "async", "await", "lambda", "struct", "interface", "extends",
    "implements", "package", "namespace", "using", "include", "module", "fn", "impl", "match", "pub",
    "mut", "use", "type", "enum", "yield", "with", "as", "in", "is", "not", "and", "or", "del",
    "global", "assert", "raise", "pass", "end", "then", "do", "begin", "typedef", "template",
    "sizeof", "operator", "virtual", "override", "abstract", "final", "instanceof",
  ]);

  const TOKEN_RE = new RegExp(
    [
      "(//[^\\n]*|#[^\\n]*|/\\*[\\s\\S]*?\\*/)", // comment
      '("(?:\\\\.|[^"\\\\])*"|\'(?:\\\\.|[^\'\\\\])*\'|`(?:\\\\.|[^`\\\\])*`)', // string
      "(\\b\\d+(?:\\.\\d+)?\\b)", // number
      "(\\b[A-Za-z_][A-Za-z0-9_]*\\b)", // word
    ].join("|"),
    "g"
  );

  function highlightCode(code) {
    let out = "";
    let last = 0;
    let m;
    TOKEN_RE.lastIndex = 0;
    while ((m = TOKEN_RE.exec(code)) !== null) {
      out += escapeHtml(code.slice(last, m.index));
      if (m[1] !== undefined) {
        out += `<span class="tok-comment">${escapeHtml(m[1])}</span>`;
      } else if (m[2] !== undefined) {
        out += `<span class="tok-string">${escapeHtml(m[2])}</span>`;
      } else if (m[3] !== undefined) {
        out += `<span class="tok-number">${escapeHtml(m[3])}</span>`;
      } else if (m[4] !== undefined) {
        const word = m[4];
        out += CODE_KEYWORDS.has(word.toLowerCase())
          ? `<span class="tok-keyword">${escapeHtml(word)}</span>`
          : escapeHtml(word);
      }
      last = TOKEN_RE.lastIndex;
    }
    out += escapeHtml(code.slice(last));
    return out;
  }

  function renderCodeBlock(rawCode, lang) {
    const displayLang = lang || "text";
    return (
      `<div class="code-block" data-lang="${escapeHtml(displayLang)}" data-code="${escapeHtml(rawCode)}">` +
      `<div class="code-block-header">` +
      `<span class="code-lang">${escapeHtml(displayLang)}</span>` +
      `<div class="code-block-actions">` +
      `<button type="button" class="code-btn code-copy">Copy</button>` +
      `<button type="button" class="code-btn code-download">Download</button>` +
      `</div></div>` +
      `<pre><code>${highlightCode(rawCode)}</code></pre>` +
      `</div>`
    );
  }

  function renderInline(text) {
    let html = escapeHtml(text);
    html = html.replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');
    html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    return html;
  }

  function renderParagraphText(text) {
    if (!text) return "";
    let html = escapeHtml(text);
    html = html.replace(
      /!\[video:\s*([^\]]*)\]\((\/api\/videos\/[a-zA-Z0-9_.-]+)\)/g,
      '<div class="generated-media" data-media-type="video"><video src="$2" class="chat-video" controls playsinline preload="metadata" aria-label="$1"></video><div class="generated-media-actions"><button type="button" class="media-copy-btn">Copy link</button><button type="button" class="media-share-btn">Share</button><a class="video-download" href="$2" download>Download video</a></div></div>'
    );
    html = html.replace(
      /!\[([^\]]*)\]\((\/api\/conversations\/[a-zA-Z0-9_-]+\/charts\/\d+\.svg)\)/g,
      '<figure class="chat-static-preview chat-chart"><img src="$2" alt="$1" class="chat-image chat-chart-image" /><a class="video-download" href="$2" target="_blank" rel="noopener">Open chart full size</a></figure>'
    );
    html = html.replace(
      /!\[([^\]]*)\]\((\/api\/images\/[a-zA-Z0-9_.-]+)\)/g,
      '<div class="generated-media" data-media-type="image"><img src="$2" alt="$1" class="chat-image" loading="lazy" /><div class="generated-media-actions"><button type="button" class="media-copy-btn">Copy image</button><button type="button" class="media-share-btn">Share</button><a class="video-download" href="$2" download>Download image</a></div></div>'
    );
    html = html.replace(/`([^`\n]+)`/g, '<code class="inline-code">$1</code>');
    html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    return html;
  }

  function renderParagraph(text) {
    const imagePattern = /!\[([^\]\n]*)\]\((https:\/\/[^\s<>()]+)\)/g;
    let html = "";
    let cursor = 0;
    for (const match of text.matchAll(imagePattern)) {
      let url;
      try {
        url = new URL(match[2]);
      } catch {
        continue;
      }
      if (url.protocol !== "https:" || url.username || url.password ||
          text.slice(cursor, match.index).split("`").length % 2 === 0) continue;
      html += renderParagraphText(text.slice(cursor, match.index));
      const alt = escapeHtml(match[1]);
      html += `<button type="button" class="code-btn chat-load-image" data-url="${escapeHtml(url.href)}" data-alt="${alt}">Load image${alt ? `: ${alt}` : ""}</button>`;
      cursor = match.index + match[0].length;
    }
    return html + renderParagraphText(text.slice(cursor));
  }

  // Generated media stays on Hearth. Copy places image pixels on the clipboard;
  // videos use a copyable local link because browsers do not commonly support
  // video/webm clipboard payloads. Share uses native file sharing when available.
  messagesEl.addEventListener("click", async (event) => {
    const button = event.target.closest?.(".media-copy-btn, .media-share-btn");
    if (!button) return;
    const card = button.closest(".generated-media");
    const media = card?.querySelector("img, video");
    const url = media?.currentSrc || media?.src;
    if (!url) return;
    const originalText = button.textContent;
    button.disabled = true;
    const isImage = card.dataset.mediaType === "image";
    const mediaUrl = new URL(url, location.href).href;
    const loadBlob = async () => {
      const response = await fetch(url, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`Media request failed (${response.status})`);
      return response.blob();
    };
    const imageAsPng = async (blob) => {
      const bitmap = await createImageBitmap(blob);
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      canvas.getContext("2d").drawImage(bitmap, 0, 0);
      bitmap.close();
      return new Promise((resolve, reject) => canvas.toBlob(
        (file) => file ? resolve(file) : reject(new Error("Could not prepare image")),
        "image/png"
      ));
    };
    try {
      if (button.classList.contains("media-copy-btn")) {
        if (isImage && navigator.clipboard?.write && window.ClipboardItem) {
          const png = await imageAsPng(await loadBlob());
          await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
          button.textContent = "Copied image";
        } else {
          await navigator.clipboard.writeText(mediaUrl);
          button.textContent = "Copied link";
        }
      } else {
        const title = isImage ? "Generated image" : "Generated video";
        if (!navigator.share) {
          await navigator.clipboard.writeText(mediaUrl);
          button.textContent = "Link copied";
        } else {
          const mimeType = isImage ? "image/png" : "video/webm";
          const shareProbe = new File([], `hearth-generated.${isImage ? "png" : "webm"}`, { type: mimeType });
          if (navigator.canShare?.({ files: [shareProbe] })) {
            const blob = await loadBlob();
            const fileData = isImage ? await imageAsPng(blob) : blob;
            const file = new File([fileData], `hearth-generated.${isImage ? "png" : "webm"}`, { type: mimeType });
            await navigator.share({ title, files: [file] });
          } else {
            await navigator.share({ title, url: mediaUrl });
          }
          button.textContent = "Shared";
        }
      }
      setTimeout(() => { button.textContent = originalText; button.disabled = false; }, 1800);
    } catch (error) {
      if (error?.name === "AbortError") {
        button.disabled = false;
        return;
      }
      button.disabled = false;
      addSystemMessage(`Could not ${button.classList.contains("media-copy-btn") ? "copy" : "share"} this media: ${error.message}`, true);
    }
  });

  // GFM-style pipe tables, e.g.:
  //   | Model | Params | Speed |
  //   |-------|-------:|:-----:|
  //   | A     | 7B     | fast  |
  function splitTableRow(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
  }

  function tableCellAlign(sepCell) {
    const left = sepCell.startsWith(":");
    const right = sepCell.endsWith(":");
    if (left && right) return "center";
    if (right) return "right";
    return "";
  }

  function isTableSeparatorRow(line) {
    if (!line.includes("|") && !line.includes("-")) return false;
    const cells = splitTableRow(line);
    if (!cells.length) return false;
    return cells.every((c) => /^:?-+:?$/.test(c));
  }

  function parseTableBlock(lines, startIdx) {
    if (startIdx + 1 >= lines.length) return null;
    if (!lines[startIdx].includes("|")) return null;
    if (!isTableSeparatorRow(lines[startIdx + 1])) return null;
    const header = splitTableRow(lines[startIdx]);
    const aligns = splitTableRow(lines[startIdx + 1]).map(tableCellAlign);
    const body = [];
    let i = startIdx + 2;
    while (i < lines.length && lines[i].trim() !== "" && lines[i].includes("|")) {
      body.push(splitTableRow(lines[i]));
      i++;
    }
    return { header, aligns, body, endIdx: i };
  }

  function renderTableBlock(table) {
    const { header, aligns, body } = table;
    const tsv = [header, ...body].map((r) => r.join("\t")).join("\n");
    let html = `<div class="table-block" data-tsv="${escapeHtml(tsv)}">`;
    html += `<div class="table-block-actions"><button type="button" class="table-btn table-copy">Copy table</button></div>`;
    html += `<div class="table-scroll"><table><thead><tr>`;
    header.forEach((cell, idx) => {
      const style = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
      html += `<th${style}>${renderInline(cell)}</th>`;
    });
    html += "</tr></thead><tbody>";
    body.forEach((row) => {
      html += "<tr>";
      header.forEach((_, idx) => {
        const style = aligns[idx] ? ` style="text-align:${aligns[idx]}"` : "";
        html += `<td${style}>${renderInline(row[idx] || "")}</td>`;
      });
      html += "</tr>";
    });
    html += "</tbody></table></div></div>";
    return html;
  }

  function renderProse(text) {
    if (!text) return "";
    const lines = text.split("\n");
    let html = "";
    let paraBuf = [];
    const flushPara = () => {
      if (paraBuf.length) {
        html += renderParagraph(paraBuf.join("\n"));
        paraBuf = [];
      }
    };
    let i = 0;
    while (i < lines.length) {
      const table = parseTableBlock(lines, i);
      if (table) {
        flushPara();
        html += renderTableBlock(table);
        i = table.endIdx;
        continue;
      }
      // ATX-style headings ("### Title", optionally closed with trailing #'s). These
      // were previously falling through to renderParagraph and showing up as literal
      // "### Title" text in the bubble instead of an actual styled heading.
      const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(lines[i]);
      if (heading) {
        flushPara();
        const level = heading[1].length;
        html += `<h${level} class="msg-heading">${renderInline(heading[2])}</h${level}>`;
        i++;
        continue;
      }
      paraBuf.push(lines[i]);
      i++;
    }
    flushPara();
    return html;
  }

  const previewTags = new Set(("a article aside b blockquote br caption code col colgroup dd div dl dt em " +
    "figcaption figure footer h1 h2 h3 h4 h5 h6 header hr i img li main ol p pre section small span " +
    "strong sub sup table tbody td th thead tfoot tr ul svg g path rect circle ellipse line polyline " +
    "polygon text tspan title desc").split(" "));
  const previewAttributes = new Set(("alt aria-label class colspan rowspan scope style title width height " +
    "viewbox preserveaspectratio role d x y x1 x2 y1 y2 cx cy r rx ry points transform fill " +
    "fill-opacity stroke stroke-width stroke-dasharray stroke-linecap opacity text-anchor font-size font-weight").split(" "));
  const chartTags = new Set(("svg g path rect circle ellipse line polyline polygon text tspan title desc").split(" "));

  function renderStaticPreview(code, lang) {
    const template = document.createElement("template");
    template.innerHTML = code;
    const svgNamespace = "http://www.w3.org/2000/svg";
    if (lang === "svg") {
      const chart = template.content.firstElementChild;
      if (!chart || chart.localName !== "svg" || chart.namespaceURI !== svgNamespace) {
        return renderCodeBlock(code, lang);
      }
      for (const element of [chart, ...chart.querySelectorAll("*")]) {
        if (element.namespaceURI !== svgNamespace || !chartTags.has(element.localName)) {
          element.remove();
          continue;
        }
        for (const attribute of [...element.attributes]) {
          const name = attribute.name.toLowerCase();
          if (attribute.namespaceURI || name === "style" || name === "class" ||
              !previewAttributes.has(name) || /url\s*\(/i.test(attribute.value)) {
            element.removeAttribute(attribute.name);
          }
        }
      }
      return `<figure class="chat-static-preview chat-chart">${chart.outerHTML}` +
        `<details><summary>View SVG source</summary>${renderCodeBlock(code, lang)}</details></figure>`;
    }
    for (const element of template.content.querySelectorAll("*")) {
      if (!previewTags.has(element.localName.toLowerCase())) {
        element.remove();
        continue;
      }
      for (const attribute of [...element.attributes]) {
        const inlineImage = element.localName === "img" && attribute.name === "src" &&
          /^data:image\/(?:png|jpeg|gif|webp|avif);base64,[a-z0-9+/=]+$/i.test(attribute.value);
        if (!inlineImage && !previewAttributes.has(attribute.name.toLowerCase())) {
          element.removeAttribute(attribute.name);
        }
      }
    }
    const policy = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";
    const documentHtml = `<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${policy}"><style>
      body{margin:12px;font:15px system-ui;color:#16202a;background:#fff;overflow-wrap:anywhere}
      img,svg{max-width:100%;height:auto} table{border-collapse:collapse} td,th{border:1px solid #b7c3ce;padding:6px}
      </style>${template.innerHTML}`;
    return `<figure class="chat-static-preview"><iframe title="Chat reply preview" sandbox srcdoc="${escapeHtml(documentHtml)}"></iframe>` +
      `<details><summary>View ${lang.toUpperCase()} source</summary>${renderCodeBlock(code, lang)}</details></figure>`;
  }

  function renderDataChart(code) {
    let spec;
    try {
      spec = JSON.parse(code);
    } catch {
      return renderCodeBlock(code, "chart");
    }
    if (!spec || !["line", "bar"].includes(spec.type) || !Array.isArray(spec.values) ||
        !Array.isArray(spec.labels) || spec.values.length !== spec.labels.length ||
        spec.values.length < 2 || spec.values.length > 500 ||
        !spec.values.every(value => typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= 1e15) ||
        !spec.labels.every(label => typeof label === "string")) {
      return renderCodeBlock(code, "chart");
    }
    const bar = spec.type === "bar";
    const minimum = Math.min(...spec.values, ...(bar ? [0] : []));
    const maximum = Math.max(...spec.values, ...(bar ? [0] : []));
    const range = maximum - minimum || 1;
    const left = 86, top = 30, width = 590, height = 215;
    const x = (index) => left + width * (bar ? (index + 0.5) / spec.values.length : index / (spec.values.length - 1));
    const y = (value) => top + height * (maximum - value) / range;
    const title = escapeHtml(typeof spec.title === "string" ? spec.title.slice(0, 100) : "Chart");
    let marks = "";
    for (let tick = 0; tick <= 4; tick++) {
      const value = maximum - range * tick / 4;
      const row = y(value);
      marks += `<line x1="${left}" x2="${left + width}" y1="${row}" y2="${row}" stroke="#dce4eb"/>`;
      marks += `<text x="${left - 8}" y="${row + 4}" text-anchor="end" font-size="12">${escapeHtml(Number(value.toPrecision(5)).toLocaleString())}</text>`;
    }
    const stride = Math.max(1, Math.ceil((spec.labels.length - 1) / 5));
    for (let index = 0; index < spec.labels.length; index++) {
      if (index % stride && index !== spec.labels.length - 1) continue;
      const anchor = index === 0 ? "start" : index === spec.labels.length - 1 ? "end" : "middle";
      marks += `<text x="${x(index)}" y="268" text-anchor="${anchor}" font-size="11">${escapeHtml(spec.labels[index].slice(0, 18))}</text>`;
    }
    if (bar) {
      const barWidth = Math.min(56, width / spec.values.length * 0.7);
      spec.values.forEach((value, index) => {
        marks += `<rect x="${x(index) - barWidth / 2}" y="${Math.min(y(0), y(value))}" width="${barWidth}" height="${Math.abs(y(0) - y(value))}" fill="#1665a8"><title>${escapeHtml(spec.labels[index].slice(0, 100))}: ${value}</title></rect>`;
      });
    } else {
      const points = spec.values.map((value, index) => `${x(index)},${y(value)}`).join(" ");
      marks += `<polyline points="${points}" fill="none" stroke="#1665a8" stroke-width="2"/>`;
    }
    return `<figure class="chat-static-preview chat-chart"><figcaption>${title}</figcaption><svg viewBox="0 0 700 282" role="img" aria-label="${title}">${marks}</svg>` +
      `<details><summary>View chart data</summary>${renderCodeBlock(code, "json")}</details></figure>`;
  }

  function renderMarkdown(text, allowPreview = false) {
    if (!text) return "";
    const trimmed = text.trim();
    if (allowPreview && /^<svg(?:\s|>)[\s\S]*<\/svg>$/i.test(trimmed)) {
      return renderStaticPreview(trimmed, "svg");
    }
    if (allowPreview && /^(?:<!doctype html>\s*)?<html(?:\s|>)[\s\S]*<\/html>$/i.test(trimmed)) {
      return renderStaticPreview(trimmed, "html");
    }
    const fenceRe = /```([a-zA-Z0-9_+-]*)\r?\n([\s\S]*?)(?:```|$)/g;
    let html = "";
    let lastIndex = 0;
    let match;
    while ((match = fenceRe.exec(text)) !== null) {
      html += renderProse(text.slice(lastIndex, match.index));
      const lang = match[1];
      const code = match[2].replace(/\n$/, "");
      const previewLang = lang.toLowerCase();
      const closed = match[0].endsWith("```");
      if (allowPreview && closed && previewLang === "chart") {
        html += renderDataChart(code);
      } else if (allowPreview && closed && ["html", "svg"].includes(previewLang)) {
        html += renderStaticPreview(code, previewLang);
      } else {
        html += renderCodeBlock(code, lang);
      }
      lastIndex = fenceRe.lastIndex;
    }
    html += renderProse(text.slice(lastIndex));
    return html;
  }

  function downloadText(filename, content) {
    const blob = new Blob([content], { type: "application/octet-stream" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  messagesEl.addEventListener("click", (event) => {
    const button = event.target.closest(".chat-load-image");
    if (!button) return;
    const image = document.createElement("img");
    image.className = "chat-image";
    image.alt = button.dataset.alt || "Image from chat reply";
    image.referrerPolicy = "no-referrer";
    image.addEventListener("error", () => {
      button.textContent = "Image unavailable. Retry";
      image.replaceWith(button);
    });
    image.src = button.dataset.url;
    button.replaceWith(image);
  });

  messagesEl.addEventListener("click", (e) => {
    const tableCopyBtn = e.target.closest(".table-copy");
    if (tableCopyBtn) {
      const tableBlock = e.target.closest(".table-block");
      const tsv = tableBlock ? tableBlock.dataset.tsv || "" : "";
      navigator.clipboard
        .writeText(tsv)
        .then(() => {
          const original = tableCopyBtn.textContent;
          tableCopyBtn.textContent = "Copied!";
          setTimeout(() => {
            tableCopyBtn.textContent = original;
          }, 1500);
        })
        .catch(() => addSystemMessage("Could not copy to clipboard.", true));
      return;
    }

    const copyBtn = e.target.closest(".code-copy");
    const downloadBtn = e.target.closest(".code-download");
    if (!copyBtn && !downloadBtn) return;
    const block = e.target.closest(".code-block");
    if (!block) return;
    const code = block.dataset.code || "";
    const lang = block.dataset.lang || "";

    if (copyBtn) {
      navigator.clipboard
        .writeText(code)
        .then(() => {
          const original = copyBtn.textContent;
          copyBtn.textContent = "Copied!";
          setTimeout(() => {
            copyBtn.textContent = original;
          }, 1500);
        })
        .catch(() => addSystemMessage("Could not copy to clipboard.", true));
    }

    if (downloadBtn) {
      const ext = extForLang(lang);
      const defaultName = ext === "Dockerfile" ? "Dockerfile" : `snippet.${ext}`;
      const filename = window.prompt("Save as:", defaultName);
      if (filename) downloadText(filename, code);
    }
  });

  function formatTimestamp(date) {
    return date.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function formatDuration(seconds) {
    if (seconds < 60) return `${seconds.toFixed(1)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}m ${s}s`;
  }

  function setBubbleMeta(metaEl, role, meta) {
    if (!meta) return;
    const parts = [];
    if (meta.timestamp) parts.push(formatTimestamp(meta.timestamp));
    if (role === "assistant" && typeof meta.duration === "number") {
      parts.push(formatDuration(meta.duration));
    }
    if (role === "assistant" && meta.model) {
      const label = BACKEND_LABELS[meta.backend];
      parts.push(label ? `${meta.model} (${label})` : meta.model);
    }
    if (role === "assistant" && typeof meta.totalTokens === "number") {
      parts.push(`${meta.totalTokens} tokens`);
    }
    // NPU-only — Lemonade doesn't report per-response compute the way it reports
    // per-response tokens, so this is a live utilization % sampled right as the
    // response finished, not an average over it. LM Studio responses never have
    // this (no NPU involved), so the field is simply absent for those.
    if (role === "assistant" && typeof meta.topsUsed === "number") {
      parts.push(`~${meta.topsUsed}/${meta.topsMax} TOPS (NPU ${Math.round(meta.npuPercent)}%)`);
    }
    if (role === "assistant" && meta.stopped) {
      parts.push("stopped");
    }
    metaEl.textContent = parts.join(" · ");
  }

  // Builds the "still working" indicator shown inside an empty assistant bubble.
  // Show elapsed waiting time without claiming the backend has emitted reasoning.
  function buildWaitingIndicator() {
    const wrap = document.createElement("div");
    wrap.className = "waiting-indicator";
    wrap.innerHTML = `
      <span class="wi-orb" aria-hidden="true">
        <span class="wi-orb-ring"></span>
        <span class="wi-orb-ring wi-orb-ring-2"></span>
        <span class="wi-orb-core"></span>
      </span>
      <span class="wi-label" role="status">Waiting for model · 0s<span class="wi-dots"><span>.</span><span>.</span><span>.</span></span></span>
    `;
    const labelText = wrap.querySelector(".wi-label");
    const started = Date.now();
    const timer = setInterval(() => {
      labelText.firstChild.textContent = `Waiting for model · ${Math.floor((Date.now() - started) / 1000)}s`;
    }, 1000);
    wrap._stop = () => clearInterval(timer);
    return wrap;
  }

  // Removes the waiting indicator (if still present) the moment real output —
  // reasoning or content — starts arriving for this bubble.
  function clearWaitingIndicator(assistantEl) {
    if (!assistantEl._waitingEl) return;
    assistantEl._waitingEl._stop();
    assistantEl._waitingEl.remove();
    assistantEl._waitingEl = null;
  }

  function addBubble(role, text, meta, reasoning) {
    const wrap = document.createElement("div");
    wrap.className = `msg-wrap ${role}`;

    const el = document.createElement("div");
    el.className = `msg ${role}`;

    if (role === "assistant") {
      const thinking = document.createElement("details");
      thinking.className = "thinking-block hidden";
      const summary = document.createElement("summary");
      summary.className = "thinking-summary";
      summary.textContent = "Thinking";
      const thinkingContent = document.createElement("div");
      thinkingContent.className = "thinking-content";
      thinking.appendChild(summary);
      thinking.appendChild(thinkingContent);
      el.appendChild(thinking);

      const answer = document.createElement("div");
      answer.className = "answer-content";
      el.appendChild(answer);

      el._thinkingEl = thinking;
      el._thinkingContentEl = thinkingContent;
      el._answerEl = answer;

      if (reasoning) {
        thinkingContent.textContent = reasoning;
        thinking.classList.remove("hidden");
      }
      answer.innerHTML = renderMarkdown(text, true);

      // A freshly-created, still-empty assistant bubble means we're between "user hit
      // send" and "first token arrived" — anywhere from instant to tens of seconds on a
      // cold NPU/model load. Show something alive there instead of a blank box, and let
      // the caller clear it (clearWaitingIndicator) the moment real content shows up.
      if (!text && !reasoning) {
        el._waitingEl = buildWaitingIndicator();
        answer.appendChild(el._waitingEl);
      }
    } else {
      el.textContent = text;
    }
    wrap.appendChild(el);

    const metaEl = document.createElement("div");
    metaEl.className = "msg-meta";
    wrap.appendChild(metaEl);
    el._metaEl = metaEl;
    el._wrapEl = wrap;
    setBubbleMeta(metaEl, role, meta);

    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  function addSystemMessage(text, isError) {
    addBubble(isError ? "error" : "system", text);
  }

  chatInput.addEventListener("input", () => {
    chatInput.style.height = "auto";
    chatInput.style.height = Math.min(chatInput.scrollHeight, 500) + "px";
  });

  chatInput.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    const shouldSend = multilineMode ? e.ctrlKey || e.metaKey : !e.shiftKey;
    if (shouldSend) {
      e.preventDefault();
      chatForm.requestSubmit();
    }
    // otherwise let Enter insert a newline as normal
  });

  clearBtn.addEventListener("click", newChat);
  newChatBtn.addEventListener("click", newChat);

  copyChatBtn?.addEventListener("click", () => {
    const entries = msgWraps.map((wrap) => {
      const role = wrap.classList.contains("user") ? "You" :
        wrap.classList.contains("assistant") ? "Assistant" : null;
      if (!role) return null;
      const bubble = wrap.querySelector(".msg");
      if (!bubble) return null;
      if (role === "Assistant") {
        const answer = bubble.querySelector(".answer-content");
        if (!answer) return null;
        const copy = answer.cloneNode(true);
        copy.querySelectorAll(".code-block-actions, .table-block-actions, .waiting-indicator")
          .forEach((el) => el.remove());
        const text = (copy.innerText || copy.textContent || "").trim();
        return text ? `${role}:\n${text}` : null;
      }
      const text = (bubble.innerText || bubble.textContent || "").trim();
      return text ? `${role}:\n${text}` : null;
    }).filter(Boolean);

    // The live assistant bubble is intentionally outside msgWraps until the server saves
    // the turn. Include its current text so Copy chat also works during generation.
    if (activeReplyEl) {
      const answer = activeReplyEl.querySelector(".answer-content");
      const copy = answer?.cloneNode(true);
      copy?.querySelectorAll(".code-block-actions, .table-block-actions, .waiting-indicator")
        .forEach((el) => el.remove());
      const text = (copy?.innerText || copy?.textContent || "").trim();
      if (text) entries.push(`Assistant:\n${text}`);
    }
    if (!entries.length) return;
    navigator.clipboard.writeText(entries.join("\n\n")).then(() => {
      const original = copyChatBtn.textContent;
      copyChatBtn.textContent = "Copied";
      setTimeout(() => { copyChatBtn.textContent = original; }, 1200);
    }).catch(() => addSystemMessage("Could not copy chat to clipboard.", true));
  });

  if (refreshBtn) refreshBtn.addEventListener("click", fetchModels);
  document.getElementById("chat-create-agent-btn")?.addEventListener("click", (event) => {
    const latest = [...history].reverse().find(message => message.role === "user")?.content;
    const goal = chatInput.value.trim() || (typeof latest === "string" ? latest.trim() : "");
    openAgentDraft(goal, event.currentTarget.dataset.draftUrl);
  });

  function openAgentDraft(goal, url) {
    if (!goal) {
      window.location.assign(url);
      return;
    }
    const form = document.createElement("form");
    form.method = "post";
    form.action = url;
    for (const [name, value] of [
      ["csrf_token", document.querySelector('meta[name="csrf-token"]').content],
      ["chat_goal", goal.slice(0, 20000)],
      ["origin_conversation_id", currentConversationId || ""],
    ]) {
      const input = document.createElement("input");
      input.type = "hidden";
      input.name = name;
      input.value = value;
      form.appendChild(input);
    }
    document.body.appendChild(form);
    form.requestSubmit();
  }
  if (securityScanBtn) {
    securityScanBtn.addEventListener("click", () => {
      chatInput.value = "/security-scan";
      chatForm.requestSubmit();
    });
  }

  async function stopGeneration() {
    const id = currentConversationId;
    if (!id) return;
    setChatBusy(true, true);
    try {
      const res = await fetch(`/api/conversations/${id}/generation/stop`, { method: "POST" });
      if (!res.ok) throw new Error("Could not stop the response.");
      if (currentConversationId === id) await loadConversation(id, { refresh: true });
    } catch (err) {
      if (currentConversationId === id) {
        addSystemMessage(err.message, true);
        setChatBusy(true);
      }
    }
  }

  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    if (busy) {
      stopGeneration();
      return;
    }
    const text = chatInput.value.trim();
    if (!text && !pendingDocuments.length) return;
    if (speakMode || conversationMode) unlockVoicePlayback();
    // In-chat practice tests: "start DMV NC test", "start citizenship test", "quiz me on
    // <topic>", "resume", "next", "score", "stop test", "list tests". If Quiz claims the
    // message, it runs the quiz inline and we don't send anything to the LLM.
    if (!pendingDocuments.length && window.Quiz && window.Quiz.maybeHandle(text)) {
      chatInput.value = "";
      chatInput.style.height = "auto";
      chatInput.focus();
      return;
    }
    const isSecurityScan = ["/security-scan", "/security scan"].includes(text.toLowerCase());
    const isVideoRequest = /\b(?:generate|create|make|render|produce)\b[^.!?\n]{0,50}\b(?:video|movie|clip|animation)\b|\b(?:video|movie|clip|animation)\s+(?:of|showing|depicting|featuring)\b/i.test(text);
    if (!modelSelectEl.value && !isSecurityScan && !isVideoRequest && !isAgentChatRequest(text)) {
      addSystemMessage("Load a model first.", true);
      return;
    }
    chatInput.value = "";
    chatInput.style.height = "auto";
    const docs = pendingDocuments.splice(0);
    updateDocumentAttachmentLabel();
    runChatTurn(text, "", null, docs);
  });

  // Rewind the conversation to just before the user message at `userIndex`, then
  // re-run from there with `text`. The single path behind edit, regenerate, and retry:
  // truncates the SERVER's stored history to match (so the chat endpoint's re-save of
  // the user message doesn't duplicate it), drops the local bubbles/history from that
  // point, then runs a fresh turn.
  async function resendFromUser(userIndex, text) {
    if (busy || userIndex < 0) return;
    const previousReply = history[userIndex + 1]?.role === "assistant" ? history[userIndex + 1].content : "";
    const isRegeneration = Boolean(previousReply) && text === history[userIndex]?.content;
    const originalHistory = history.map((message) => ({ ...message }));
    const originalWraps = msgWraps.slice();
    if (currentConversationId) {
      try {
        await fetch(`/api/conversations/${currentConversationId}/truncate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ keep: userIndex }),
        });
      } catch {
        // Non-fatal: the resend still works locally; worst case a few stale rows
        // remain server-side, which the next successful turn's history overwrites.
      }
    }
    for (let i = msgWraps.length - 1; i >= userIndex; i--) msgWraps[i]?.remove();
    msgWraps.length = userIndex;
    history.length = userIndex;
    await runChatTurn(text, isRegeneration ? previousReply : "", isRegeneration ? { userIndex, text, originalHistory, originalWraps, previousReply } : null);
  }

  // Start a server-owned turn. Polling can stop with the tab; generation cannot.
  function isAgentChatRequest(text) {
    return /^(?:(?:please|can you|could you|would you)\s+)*(?:create|start|set up|schedule)\b[^\n]{0,100}\b(?:agent(?:ic)?|assignment)\b/i.test(text);
  }

  async function runChatTurn(text, preservedReply = "", regenerationFallback = null, documents = []) {
    const isSecurityScan = ["/security-scan", "/security scan"].includes(text.toLowerCase());
    const isVideoRequest = /\b(?:generate|create|make|render|produce)\b[^.!?\n]{0,50}\b(?:video|movie|clip|animation)\b|\b(?:video|movie|clip|animation)\s+(?:of|showing|depicting|featuring)\b/i.test(text);
    const model = modelSelectEl.value || (isSecurityScan ? "system-security-scan" : (isVideoRequest ? "gemini-omni-1.1-flash" : ""));
    if (!model && !isAgentChatRequest(text)) { addSystemMessage("Load a model first.", true); return; }
    if (!currentConversationId) currentConversationId = crypto.randomUUID();
    const id = currentConversationId;
    const version = conversationViewVersion;
    sessionStorage.setItem("hearth-current-chat", id);
    history.push({ role: "user", content: text });
    const userEl = addBubble("user", text, { timestamp: new Date() });
    msgWraps.push(userEl._wrapEl);
    addUserControls(userEl._wrapEl);
    activeReplyEl = addBubble("assistant", "");
    addAssistantControls(activeReplyEl._wrapEl);
    renderedMessagesKey = null;
    setChatBusy(true);
    sendBtn.disabled = true;
    try {
      const payload = { model, messages: history, temperature: Number(temperatureEl.value) || 0.7,
        conversation_id: id, preserved_reply: preservedReply };
      const requestBody = documents.length ? new FormData() : JSON.stringify(payload);
      if (documents.length) {
        requestBody.append("payload", JSON.stringify(payload));
        documents.forEach(file => requestBody.append("documents", file, file.name));
      }
      const res = await fetch("/api/chat/start", {
        method: "POST",
        // max_tokens is intentionally omitted so the server applies the admin-configured
        // default (Settings -> Chat request limits); sending a hardcoded value here would
        // silently ignore that setting.
        body: requestBody,
      });
      const data = await res.json();
      if (currentConversationId !== id || conversationViewVersion !== version) return;
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      generationPlayback = { id: data.generation.id, spokenUpTo: 0,
        shouldSpeak: conversationMode || speakMode, finished: false };
      await loadConversation(id, { refresh: true });
      await fetchConversations();
    } catch (err) {
      if (currentConversationId !== id || conversationViewVersion !== version) return;
      if (regenerationFallback) {
        history = regenerationFallback.originalHistory;
        msgWraps = regenerationFallback.originalWraps;
        for (const wrap of msgWraps) messagesEl.appendChild(wrap);
        const userIndex = regenerationFallback.userIndex;
        addFailedRegeneration(err.message, regenerationFallback.previousReply, userIndex, regenerationFallback.text);
        renderedMessagesKey = null;
        setChatBusy(false);
        return;
      }
      // A request may have reached the server despite a browser/network error.
      addSystemMessage(`Chat connection interrupted: ${err.message}. Reconnecting…`, true);
      setChatBusy(false);
      await loadConversation(id, { refresh: true });
    }
  }

  // ---- Message actions: edit / regenerate / retry -------------------------------
  // All three funnel through resendFromUser(). Controls are re-added on load and after
  // each turn; they map their own bubble to a live history index via msgWraps.indexOf,
  // so they keep working as messages above them get edited or removed.

  function makeActionBtn(label, title, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "msg-action-btn";
    b.textContent = label;
    b.title = title;
    b.addEventListener("click", onClick);
    return b;
  }

  function addUserControls(wrap) {
    if (wrap.querySelector(".msg-actions")) return;
    const bar = document.createElement("div");
    bar.className = "msg-actions";
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "msg-action-btn msg-copy-btn";
    copyBtn.title = "Copy message";
    copyBtn.setAttribute("aria-label", "Copy message");
    copyBtn.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5" y="2" width="9" height="10" rx="1.5"></rect><path d="M11 13.5H3.5A1.5 1.5 0 0 1 2 12V4.5"></path></svg>';
    copyBtn.addEventListener("click", () => {
      const bubble = wrap.querySelector(".msg.user");
      const text = (bubble?.innerText || bubble?.textContent || "").trim();
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {
        copyBtn.classList.add("copied");
        copyBtn.textContent = "✓";
        copyBtn.title = "Copied";
        copyBtn.setAttribute("aria-label", "Copied");
        setTimeout(() => {
          copyBtn.classList.remove("copied");
          copyBtn.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5" y="2" width="9" height="10" rx="1.5"></rect><path d="M11 13.5H3.5A1.5 1.5 0 0 1 2 12V4.5"></path></svg>';
          copyBtn.title = "Copy message";
          copyBtn.setAttribute("aria-label", "Copy message");
        }, 1200);
      }).catch(() => addSystemMessage("Could not copy message to clipboard.", true));
    });
    bar.appendChild(copyBtn);
    bar.appendChild(makeActionBtn("Edit", "Edit this message and resend", () => startEditUser(wrap)));
    bar.appendChild(makeActionBtn("Regenerate", "Resend this message for a new reply", () => {
      if (busy) return;
      const i = msgWraps.indexOf(wrap);
      if (i >= 0) resendFromUser(i, history[i].content);
    }));
    wrap.appendChild(bar);
  }

  function addAssistantControls(wrap) {
    if (wrap.querySelector(".msg-actions")) return;
    const bar = document.createElement("div");
    bar.className = "msg-actions";
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "msg-action-btn msg-copy-btn";
    copyBtn.title = "Copy response";
    copyBtn.setAttribute("aria-label", "Copy response");
    copyBtn.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5" y="2" width="9" height="10" rx="1.5"></rect><path d="M11 13.5H3.5A1.5 1.5 0 0 1 2 12V4.5"></path></svg>';
    copyBtn.addEventListener("click", () => {
      const answerEl = wrap.querySelector(".answer-content");
      if (!answerEl) return;
      const copy = answerEl.cloneNode(true);
      copy.querySelectorAll(".code-block-actions, .table-block-actions, .waiting-indicator")
        .forEach((el) => el.remove());
      const text = (copy.innerText || copy.textContent || "").trimEnd();
      if (!text) return;
      navigator.clipboard.writeText(text).then(() => {
        copyBtn.classList.add("copied");
        copyBtn.title = "Copied";
        copyBtn.setAttribute("aria-label", "Copied");
        copyBtn.innerHTML = "✓";
        setTimeout(() => {
          copyBtn.classList.remove("copied");
          copyBtn.title = "Copy response";
          copyBtn.setAttribute("aria-label", "Copy response");
          copyBtn.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5" y="2" width="9" height="10" rx="1.5"></rect><path d="M11 13.5H3.5A1.5 1.5 0 0 1 2 12V4.5"></path></svg>';
        }, 1200);
      }).catch(() => addSystemMessage("Could not copy to clipboard.", true));
    });
    bar.appendChild(copyBtn);
    bar.appendChild(makeActionBtn("Regenerate", "Discard this reply and generate a new one", () => {
      if (busy) return;
      const j = msgWraps.indexOf(wrap);
      // The user message that produced this reply is the one right before it.
      if (j >= 1 && history[j - 1]?.role === "user") resendFromUser(j - 1, history[j - 1].content);
    }));
    wrap.appendChild(bar);
  }

  function startEditUser(wrap) {
    if (busy) return;
    if (wrap.querySelector(".msg-edit")) return; // already editing
    const msgEl = wrap.querySelector(".msg.user");
    const actions = wrap.querySelector(".msg-actions");
    const original = history[msgWraps.indexOf(wrap)]?.content ?? msgEl.textContent;

    const editor = document.createElement("div");
    editor.className = "msg-edit";
    const ta = document.createElement("textarea");
    ta.className = "msg-edit-input";
    ta.value = original;
    const row = document.createElement("div");
    row.className = "msg-edit-actions";
    const save = makeActionBtn("Save & resend", "Resend the edited message", () => {
      const t = ta.value.trim();
      if (!t) return;
      const i = msgWraps.indexOf(wrap);
      if (i >= 0) resendFromUser(i, t);
    });
    save.classList.add("primary");
    const cancel = makeActionBtn("Cancel", "Discard edits", () => {
      editor.remove();
      msgEl.classList.remove("hidden");
      if (actions) actions.classList.remove("hidden");
    });
    row.appendChild(save);
    row.appendChild(cancel);
    editor.appendChild(ta);
    editor.appendChild(row);

    msgEl.classList.add("hidden");
    if (actions) actions.classList.add("hidden");
    wrap.insertBefore(editor, wrap.firstChild);
    ta.focus();
    ta.style.height = "auto";
    ta.style.height = `${ta.scrollHeight}px`;
    // Enter to save, Shift+Enter for newline; Esc to cancel.
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); save.click(); }
      else if (e.key === "Escape") { e.preventDefault(); cancel.click(); }
    });
  }

  function addChatError(message, failedText, retryIndex) {
    const wrap = document.createElement("div");
    wrap.className = "msg-wrap error";
    const el = document.createElement("div");
    el.className = "msg error";
    el.textContent = `Chat error: ${message}`;
    wrap.appendChild(el);
    const bar = document.createElement("div");
    bar.className = "msg-actions";
    bar.appendChild(makeActionBtn("Retry", "Try sending that message again", () => {
      if (busy) return;
      wrap.remove();
      resendFromUser(retryIndex, failedText);
    }));
    wrap.appendChild(bar);
    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function addFailedRegeneration(message, oldReply, retryIndex, failedText) {
    const wrap = document.createElement("div");
    wrap.className = "msg-wrap regeneration-failed";
    const label = document.createElement("div");
    label.className = "regeneration-failed-label";
    label.textContent = `Regeneration failed: ${message}. Previous reply:`;
    wrap.appendChild(label);
    const bubble = document.createElement("div");
    bubble.className = "msg assistant preserved-reply";
    const answer = document.createElement("div");
    answer.className = "answer-content";
    answer.innerHTML = renderMarkdown(oldReply, true);
    bubble.appendChild(answer);
    wrap.appendChild(bubble);
    const bar = document.createElement("div");
    bar.className = "msg-actions";
    bar.appendChild(makeActionBtn("Retry", "Try regenerating this reply again", () => {
      if (!busy) resendFromUser(retryIndex, failedText);
    }));
    wrap.appendChild(bar);
    messagesEl.appendChild(wrap);
  }

  // --- Voice: mic input (record -> transcribe -> auto-send) and spoken responses
  // (assistant reply -> local TTS -> auto-play). Turn-based, not continuous/streaming -
  // see the README's voice section for why a real-time, interruptible voice mode isn't
  // realistic on this hardware (a local STT->LLM->TTS pipeline is multi-second-per-turn
  // no matter how each stage is optimized).

  const MIC_CONSTRAINTS = { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } };

  // Chrome on iPhone is WebKit-based and commonly records AAC/MP4, while desktop
  // Chrome prefers WebM/Opus. Pick a format the current browser explicitly supports
  // instead of relying on a WebM default that iOS may reject.
  const RECORDING_MIME_CANDIDATES = [
    "audio/mp4;codecs=mp4a.40.2", "audio/mp4", "audio/webm;codecs=opus", "audio/webm",
  ];

  function preferredRecordingMimeType() {
    if (!window.MediaRecorder || typeof MediaRecorder.isTypeSupported !== "function") return "";
    return RECORDING_MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type)) || "";
  }

  function createAudioRecorder(stream) {
    const mimeType = preferredRecordingMimeType();
    return mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
  }

  function recordingFile(blob, stem) {
    const type = (blob.type || "").toLowerCase();
    const extension = type.includes("mp4") || type.includes("aac") ? "m4a" : "webm";
    return `${stem}.${extension}`;
  }

  function microphoneErrorMessage(err) {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      return "Microphone access requires HTTPS. Open Hearth using its https:// address, then reload.";
    }
    if (err?.name === "NotAllowedError" || err?.name === "PermissionDeniedError") {
      return "Microphone permission is blocked. On iPhone: open Settings → Apps → Chrome → Microphone and enable it; then return to this page, tap the address-bar site controls, allow Microphone, and reload.";
    }
    if (err?.name === "NotFoundError" || err?.name === "DevicesNotFoundError") {
      return "No microphone is available to Chrome. Check iPhone microphone restrictions and Bluetooth audio devices.";
    }
    if (err?.name === "NotReadableError" || err?.name === "TrackStartError") {
      return "The microphone is busy in another app or tab. Close the other recorder/call and try again.";
    }
    return err?.message || "Chrome could not start the microphone.";
  }

  let voiceRecordingMode = "transcribe";

  async function startRecording(mode = "transcribe") {
    try {
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
        throw new DOMException("Microphone recording is unavailable in this browser context.", "NotSupportedError");
      }
      voiceRecordingMode = mode;
      const stream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
      recordedChunks = [];
      mediaRecorder = createAudioRecorder(stream);
      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) recordedChunks.push(e.data);
      };
      mediaRecorder.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType || preferredRecordingMimeType() || "audio/webm" });
        if (voiceRecordingMode === "translate") await sendTranslationRecording(blob);
        else await sendRecording(blob);
      };
      mediaRecorder.start();
      if (mode === "translate") {
        translateMicBtn.classList.add("active", "recording");
        translateMicBtn.setAttribute("aria-pressed", "true");
        translateMicBtn.title = "Recording for translation… click to stop";
      } else {
        micBtn.classList.add("active", "recording");
        micBtn.setAttribute("aria-pressed", "true");
        micBtn.title = "Recording… click to stop";
      }
    } catch (err) {
      addSystemMessage(`Microphone error: ${microphoneErrorMessage(err)}`, true);
    }
  }

  function stopRecording() {
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
      mediaRecorder.stop();
    }
    micBtn.classList.remove("active", "recording");
    micBtn.setAttribute("aria-pressed", "false");
    micBtn.title = "Record a voice message (click to start, click again to stop)";
    translateMicBtn.classList.remove("active", "recording");
    translateMicBtn.setAttribute("aria-pressed", "false");
    translateMicBtn.title = "Record speech and translate it with local Gemma 4";
  }

  // A non-2xx response isn't always JSON (a 413 Payload Too Large, a proxy error page, or
  // an unhandled 500 all come back as HTML) -- res.json() on those throws a confusing raw
  // parse error ("Unexpected token '<'") instead of the actual problem. Read as text first
  // and only parse as JSON when it looks like JSON, so callers get a real message either way.
  async function parseJsonResponse(res) {
    const raw = await res.text();
    let data = null;
    try {
      data = raw ? JSON.parse(raw) : null;
    } catch {
      if (!res.ok) throw new Error(`HTTP ${res.status}${raw ? `: ${raw.slice(0, 200)}` : ""}`);
      throw new Error("server returned a non-JSON response");
    }
    if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data;
  }

  async function sendTranslationRecording(blob) {
    translateMicBtn.disabled = true;
    translateLanguage.disabled = true;
    showVoiceStatus(`Translating speech to ${translateLanguage.value} with Gemma 4…`);
    try {
      const formData = new FormData();
      formData.append("audio", blob, recordingFile(blob, "translation"));
      formData.append("target_language", translateLanguage.value);
      const res = await fetch("/api/voice/translate", { method: "POST", body: formData });
      const data = await parseJsonResponse(res);
      const text = (data.text || "").trim();
      if (!text) throw new Error("No translation was returned");
      if (data.transcript) addSystemMessage(`Heard: ${data.transcript}`);
      addSystemMessage(`${data.target_language}: ${text}`);
      // The installed Kokoro voice is English-only and produces misleading,
      // unintelligible audio for these scripts. Keep the translated text accurate.
      const nativeTextTarget = ["Tamil", "Telugu", "Kannada", "Malayalam"].includes(data.target_language);
      const speech = nativeTextTarget ? null : await fetchSpeechAudio(text);
      if (speech) {
        await playAudioBlob(speech);
      }
    } catch (err) {
      addSystemMessage(`Voice translation error: ${err.message}`, true);
    } finally {
      translateMicBtn.disabled = false;
      translateLanguage.disabled = false;
      hideVoiceStatus();
    }
  }

  async function sendRecording(blob) {
    micBtn.disabled = true;
    const originalPlaceholder = chatInput.placeholder;
    chatInput.placeholder = "Transcribing…";
    try {
      const formData = new FormData();
      formData.append("audio", blob, recordingFile(blob, "recording"));
      const res = await fetch("/api/voice/transcribe", { method: "POST", body: formData });
      const data = await parseJsonResponse(res);
      const text = (data.text || "").trim();
      if (!text) {
        addSystemMessage("No speech detected in that recording.", true);
        return;
      }
      chatInput.value = text;
      chatInput.style.height = "auto";
      chatInput.style.height = `${chatInput.scrollHeight}px`;
      // Auto-send once transcribed - matches the voice-assistant UX this was asked to
      // mirror (you speak a complete message and stop, same as releasing a
      // push-to-talk button, rather than needing to also press Send).
      chatForm.requestSubmit();
    } catch (err) {
      addSystemMessage(`Transcription error: ${err.message}`, true);
    } finally {
      micBtn.disabled = false;
      chatInput.placeholder = originalPlaceholder;
    }
  }

  micBtn.addEventListener("click", () => {
    if (busy) return;
    if (mediaRecorder && mediaRecorder.state === "recording") {
      stopRecording();
    } else {
      startRecording();
    }
  });

  translateMicBtn.addEventListener("click", () => {
    if (busy) return;
    if (mediaRecorder && mediaRecorder.state === "recording") stopRecording();
    else startRecording("translate");
  });

  const translateAttachBtn = document.getElementById("translate-attach-btn");
  const translateAttachInput = document.getElementById("translate-attach-input");

  translateAttachBtn.addEventListener("click", () => {
    if (busy) return;
    translateAttachInput.click();
  });

  async function sendImageTranslation(file) {
    translateAttachBtn.disabled = true;
    translateLanguage.disabled = true;
    showVoiceStatus(`Reading and translating image text to ${translateLanguage.value} with Gemma 4…`);
    try {
      const formData = new FormData();
      formData.append("image", file, file.name || "image.png");
      formData.append("target_language", translateLanguage.value);
      const res = await fetch("/api/image/translate", { method: "POST", body: formData });
      const data = await parseJsonResponse(res);
      const text = (data.text || "").trim();
      if (!text) throw new Error("No translation was returned");
      addSystemMessage(`${data.target_language}: ${text}`);
    } catch (err) {
      addSystemMessage(`Image translation error: ${err.message}`, true);
    } finally {
      translateAttachBtn.disabled = false;
      translateLanguage.disabled = false;
      hideVoiceStatus();
    }
  }

  async function sendVideoTranslation(file) {
    translateAttachBtn.disabled = true;
    translateLanguage.disabled = true;
    showVoiceStatus(`Extracting and translating video audio to ${translateLanguage.value} with Gemma 4…`);
    try {
      const formData = new FormData();
      formData.append("video", file, file.name || "video.mp4");
      formData.append("target_language", translateLanguage.value);
      const res = await fetch("/api/video/translate", { method: "POST", body: formData });
      const data = await parseJsonResponse(res);
      const text = (data.text || "").trim();
      if (!text) throw new Error("No translation was returned");
      if (data.transcript) addSystemMessage(`Heard: ${data.transcript}`);
      addSystemMessage(`${data.target_language}: ${text}`);
    } catch (err) {
      addSystemMessage(`Video translation error: ${err.message}`, true);
    } finally {
      translateAttachBtn.disabled = false;
      translateLanguage.disabled = false;
      hideVoiceStatus();
    }
  }

  translateAttachInput.addEventListener("change", () => {
    const file = translateAttachInput.files[0];
    translateAttachInput.value = "";
    if (!file) return;
    if (file.type.startsWith("video/")) {
      sendVideoTranslation(file);
    } else if (file.type.startsWith("image/")) {
      sendImageTranslation(file);
    } else {
      addSystemMessage(`Unsupported file type: ${file.type || "unknown"}. Attach an image or video.`, true);
    }
  });

  function stripMarkdownForSpeech(text) {
    return text
      .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
      .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/```[\s\S]*?```/g, "")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/^#{1,6}\s+/gm, "")
      .replace(/\*\*([^*]+)\*\*/g, "$1")
      .replace(/\*([^*]+)\*/g, "$1")
      .replace(/__([^_]+)__/g, "$1")
      .replace(/^>\s+/gm, "")
      .replace(/^[-*+]\s+/gm, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  // Voice playback failures used to be swallowed completely silently (by design, so a
  // TTS hiccup could never break the actual text chat) - which made "I'm not hearing
  // anything" impossible to diagnose, since nothing ever indicated *why*. Still fails
  // soft (chat keeps working regardless), but now logs to the console and surfaces once
  // per page load via a system message, so a real failure (autoplay blocked, backend
  // error, network drop) is visible instead of indistinguishable from "nothing to say."
  let voiceErrorShown = false;
  function reportVoiceError(context, detail) {
    console.error(`[voice] ${context}:`, detail);
    if (voiceErrorShown) return;
    voiceErrorShown = true;
    addSystemMessage(`Voice playback issue (${context}): ${detail}. Check the browser console for details.`, true);
  }

  async function fetchSpeechAudio(text) {
    try {
      const res = await fetch("/api/voice/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        reportVoiceError("synthesis request failed", data.error || `HTTP ${res.status}`);
        return null;
      }
      return await res.blob();
    } catch (err) {
      reportVoiceError("synthesis request failed", err.message);
      return null; // best-effort - a dropped chunk just gets skipped, not a hard failure
    }
  }

  function playAudioBlob(blob) {
    if (voicePlaybackContext && voicePlaybackContext.state === "running") {
      return blob.arrayBuffer()
        .then((bytes) => voicePlaybackContext.decodeAudioData(bytes))
        .then((buffer) => new Promise((resolve) => {
          const source = voicePlaybackContext.createBufferSource();
          source.buffer = buffer;
          source.connect(voicePlaybackContext.destination);
          source.addEventListener("ended", resolve, { once: true });
          source.start(0);
        }))
        .catch((err) => {
          reportVoiceError("audio playback failed", err.message || "iPhone could not decode the speech audio");
        });
    }
    return new Promise((resolve) => {
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.playsInline = true;
      audio.addEventListener("ended", () => {
        URL.revokeObjectURL(url);
        resolve();
      });
      audio.addEventListener("error", () => {
        reportVoiceError("audio playback failed", audio.error?.message || "unknown decode/playback error");
        URL.revokeObjectURL(url);
        resolve();
      });
      audio.play().catch((err) => {
        // The most likely real-world cause here: browser autoplay policy blocking
        // programmatic playback (err.name === "NotAllowedError") if this fires far
        // enough removed from the original click that the browser no longer considers
        // it user-initiated.
        reportVoiceError("playback blocked", err.name === "NotAllowedError" ? "browser blocked autoplay" : err.message);
        URL.revokeObjectURL(url);
        resolve();
      });
    });
  }

  // --- Speech queue: sentences are pushed in as they stream in from the LLM (see
  // enqueueSentencesFromStream() below, called from inside the SSE loop), not only
  // once the full reply is done. Fixed a real, measured problem: even with the
  // chunked/pipelined synthesis added earlier, speech only ever started *after* the
  // full reply had finished generating - for a slower model or a long response, that
  // "thinking" time alone (independent of TTS) could be 10s of seconds of silence
  // before the first chunk was even requested. Speaking completed sentences as they
  // arrive means the first sentence can be playing while the model is still generating
  // the rest of the reply.
  const speechQueue = [];
  let speechQueueRunning = false;
  let playingSpeech = false;
  let speechQueueDrainResolvers = [];

  function prefetchNextSpeech() {
    if (playingSpeech && speechQueue.length && !speechQueue[0].audio) {
      const next = speechQueue[0];
      next.audio = fetchSpeechAudio(next.text);
    }
  }

  function enqueueSpeech(text) {
    const cleaned = stripMarkdownForSpeech(text);
    if (!cleaned) return;
    speechQueue.push({text: cleaned, audio: null});
    if (!speechQueueRunning) runSpeechQueue();
    else prefetchNextSpeech();
  }

  async function runSpeechQueue() {
    speechQueueRunning = true;
    try {
      while (speechQueue.length > 0) {
        const item = speechQueue.shift();
        const blob = await (item.audio || fetchSpeechAudio(item.text));
        if (!blob) continue;
        playingSpeech = true;
        prefetchNextSpeech();
        try {
          await playAudioBlob(blob);
        } finally {
          playingSpeech = false;
        }
      }
    } finally {
      speechQueueRunning = false;
      const resolvers = speechQueueDrainResolvers;
      speechQueueDrainResolvers = [];
      resolvers.forEach((resolve) => resolve());
    }
  }

  // Resolves once the queue is fully drained (nothing left queued or playing) -
  // sentences arrive and get queued incrementally over the lifetime of one response
  // rather than all at once, so there's no single call to await here.
  function waitForSpeechQueueDrain() {
    return new Promise((resolve) => {
      if (!speechQueueRunning && speechQueue.length === 0) return resolve();
      speechQueueDrainResolvers.push(resolve);
    });
  }

  function isInsideCodeFence(text) {
    return (text.match(/```/g) || []).length % 2 === 1;
  }

  // Call this after every delta appended to assistantText (only while voice output is
  // active - see the two call sites below). Finds newly-completed sentences beyond
  // `spokenUpTo` and queues each one individually the moment it's complete, advancing
  // spokenUpTo past what's been queued. Stops at (doesn't queue past) an unclosed
  // ``` code fence - stripMarkdownForSpeech only knows how to strip a *complete*
  // ```...``` block; queuing a sentence that happens to fall mid-block, before the
  // closing fence has even streamed in yet, would read raw code/symbols aloud. Returns
  // the updated spokenUpTo for the caller to store.
  function enqueueSentencesFromStream(fullText, spokenUpTo) {
    const tail = fullText.slice(spokenUpTo);
    const matches = [...tail.matchAll(/[^.!?\n]+[.!?\n]+/g)];
    let consumed = 0;
    for (const m of matches) {
      const candidateEnd = spokenUpTo + m.index + m[0].length;
      if (isInsideCodeFence(fullText.slice(0, candidateEnd))) break;
      consumed = m.index + m[0].length;
    }
    if (consumed === 0 && tail.length >= 160) {
      // Start speaking a long answer before its first full stop. Prefer a clause
      // boundary and fall back to a word boundary without reading code aloud.
      const prefix = tail.slice(0, 160);
      const clauseEnd = Math.max(prefix.lastIndexOf(", "), prefix.lastIndexOf("; "),
                                 prefix.lastIndexOf(": "));
      const boundary = clauseEnd >= 60 ? clauseEnd + 1 : prefix.lastIndexOf(" ");
      if (boundary >= 60 && !isInsideCodeFence(fullText.slice(0, spokenUpTo + boundary))) {
        consumed = boundary;
      }
    }
    if (consumed > 0) {
      enqueueSpeech(tail.slice(0, consumed));
      return spokenUpTo + consumed;
    }
    return spokenUpTo;
  }

  // --- Hands-free voice conversation mode: automatic voice-activity detection (VAD)
  // instead of click-to-record. Listens continuously via the Web Audio API's
  // AnalyserNode (measuring live volume, no model/network round-trip needed for this
  // part), auto-stops and transcribes once it detects the user has stopped talking,
  // auto-sends, speaks the reply, then automatically starts listening again - a full
  // listen -> transcribe -> respond -> speak -> listen loop with no button presses
  // once started. Still turn-based under the hood (see the README's voice section for
  // why true full-duplex/interruptible voice isn't realistic on this hardware), but
  // this removes the "click to start every turn" friction that was the actual
  // complaint - the loop itself is now fully automatic, matching a real conversation's
  // back-and-forth rhythm rather than a walkie-talkie button.
  const VAD_SILENCE_RMS = 0.02;
  const VAD_SILENCE_DURATION_MS = 1200;
  const VAD_MIN_SPEECH_MS = 300;
  const VAD_MAX_UTTERANCE_MS = 30000;

  function showVoiceStatus(text) {
    voiceStatusEl.textContent = text;
    voiceStatusEl.classList.remove("hidden");
  }

  function hideVoiceStatus() {
    voiceStatusEl.classList.add("hidden");
  }

  function computeRMS(analyser, buffer) {
    analyser.getByteTimeDomainData(buffer);
    let sum = 0;
    for (let i = 0; i < buffer.length; i++) {
      const v = (buffer[i] - 128) / 128;
      sum += v * v;
    }
    return Math.sqrt(sum / buffer.length);
  }

  async function startConversationMode() {
    if (conversationMode) return;
    try {
      vadStream = await navigator.mediaDevices.getUserMedia(MIC_CONSTRAINTS);
    } catch (err) {
      addSystemMessage(`Microphone error: ${microphoneErrorMessage(err)}`, true);
      return;
    }
    conversationMode = true;
    conversationBtn.classList.add("active", "conversation-active");
    conversationBtn.setAttribute("aria-pressed", "true");
    micBtn.disabled = true; // avoid two concurrent mic streams/recorders
    vadAudioContext = new (window.AudioContext || window.webkitAudioContext)();
    if (vadAudioContext.state === "suspended") await vadAudioContext.resume();
    const source = vadAudioContext.createMediaStreamSource(vadStream);
    vadAnalyser = vadAudioContext.createAnalyser();
    vadAnalyser.fftSize = 512;
    source.connect(vadAnalyser);
    beginListeningCycle();
  }

  function stopConversationMode() {
    conversationMode = false;
    vadListening = false;
    if (vadRafId) cancelAnimationFrame(vadRafId);
    if (conversationRecorder && conversationRecorder.state !== "inactive") {
      conversationRecorder.onstop = null;
      conversationRecorder.stop();
    }
    if (vadStream) vadStream.getTracks().forEach((t) => t.stop());
    if (vadAudioContext) vadAudioContext.close();
    vadStream = null;
    vadAudioContext = null;
    vadAnalyser = null;
    conversationBtn.classList.remove("active", "conversation-active", "conversation-listening");
    conversationBtn.setAttribute("aria-pressed", "false");
    micBtn.disabled = false;
    hideVoiceStatus();
  }

  function beginListeningCycle() {
    if (!conversationMode) return;
    vadListening = true;
    vadSpeechStart = null;
    vadSilenceStart = null;
    conversationChunks = [];
    conversationBtn.classList.add("conversation-listening"); // green -> red while actually recording
    try {
      conversationRecorder = createAudioRecorder(vadStream);
    } catch (err) {
      addSystemMessage(`Microphone error: ${microphoneErrorMessage(err)}`, true);
      stopConversationMode();
      return;
    }
    conversationRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) conversationChunks.push(e.data);
    };
    conversationRecorder.start();
    showVoiceStatus("Listening…");
    runVadLoop();
  }

  function runVadLoop() {
    if (!conversationMode || !vadListening) return;
    const buffer = new Uint8Array(vadAnalyser.fftSize);
    const rms = computeRMS(vadAnalyser, buffer);
    const now = Date.now();
    if (rms > VAD_SILENCE_RMS) {
      if (vadSpeechStart === null) vadSpeechStart = now;
      vadSilenceStart = null;
    } else if (vadSpeechStart !== null) {
      if (vadSilenceStart === null) vadSilenceStart = now;
      const spokeLongEnough = now - vadSpeechStart > VAD_MIN_SPEECH_MS;
      const quietLongEnough = now - vadSilenceStart > VAD_SILENCE_DURATION_MS;
      if (spokeLongEnough && quietLongEnough) {
        endListeningCycle();
        return;
      }
    }
    if (vadSpeechStart !== null && now - vadSpeechStart > VAD_MAX_UTTERANCE_MS) {
      endListeningCycle();
      return;
    }
    vadRafId = requestAnimationFrame(runVadLoop);
  }

  async function endListeningCycle() {
    vadListening = false;
    conversationBtn.classList.remove("conversation-listening"); // back to green - no longer recording
    const hadSpeech = vadSpeechStart !== null;
    showVoiceStatus("Processing…");
    await new Promise((resolve) => {
      if (conversationRecorder.state === "inactive") return resolve();
      conversationRecorder.onstop = resolve;
      conversationRecorder.stop();
    });
    if (!conversationMode) return;
    if (!hadSpeech || conversationChunks.length === 0) {
      beginListeningCycle();
      return;
    }
    const blob = new Blob(conversationChunks, { type: conversationRecorder.mimeType || preferredRecordingMimeType() || "audio/webm" });
    try {
      const formData = new FormData();
      formData.append("audio", blob, recordingFile(blob, "utterance"));
      const res = await fetch("/api/voice/transcribe", { method: "POST", body: formData });
      const data = await parseJsonResponse(res);
      const text = (data.text || "").trim();
      if (!text) {
        if (conversationMode) beginListeningCycle();
        return;
      }
      chatInput.value = text;
      chatInput.style.height = "auto";
      chatInput.style.height = `${chatInput.scrollHeight}px`;
      hideVoiceStatus();
      // Resuming the listen cycle after the reply happens inside the chat-form submit
      // handler above (it checks conversationMode once the response is fully spoken) -
      // not here, since this only kicks off the turn, it doesn't wait for it.
      chatForm.requestSubmit();
    } catch (err) {
      addSystemMessage(`Voice error: ${err.message}`, true);
      if (conversationMode) beginListeningCycle();
    }
  }

  conversationBtn.addEventListener("click", () => {
    if (conversationMode) {
      stopConversationMode();
    } else {
      unlockVoicePlayback();
      startConversationMode();
    }
  });

  fetchModels();
  fetchConversations();
  const chatUrl = new URL(location.href);
  const linkedChat = chatUrl.searchParams.get("conversation");
  if (linkedChat) {
    chatUrl.searchParams.delete("conversation");
    window.history.replaceState(null, "", chatUrl);
  }
  const previousChat = linkedChat || sessionStorage.getItem("hearth-current-chat");
  if (previousChat) loadConversation(previousChat);
})();
