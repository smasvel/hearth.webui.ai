(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const source = $("converter-source");
  const availability = $("converter-availability");
  const validation = $("validation-result");
  const startButton = $("start-conversion");
  const ack = $("kernel-ack");
  let validatedPath = null;
  let currentJobId = null;
  let pollTimer = null;

  const escapeHtml = (text) => String(text).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);

  async function jsonFetch(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }

  function setStartState() {
    startButton.disabled = !validatedPath || !ack.checked || source.value !== validatedPath;
  }

  function populateModels(models) {
    source.innerHTML = "";
    if (!models.length) {
      source.innerHTML = '<option value="">No local GGUF models found</option>';
      return;
    }
    for (const model of models) {
      const option = document.createElement("option");
      option.value = model.path;
      option.textContent = `${model.name} — ${model.size_gb} GB`;
      option.title = model.path;
      source.appendChild(option);
    }
  }

  function renderBackendStatus(backend) {
    const profile = backend.profile || {};
    const report = (backend.conversions || [])[0];
    const ready = Boolean(report && report.ready);
    const version = backend.runtime_version || "unknown";
    const minimum = (profile.runtime || {}).minimum_version || "unknown";
    const notice = $("backend-readiness");
    notice.className = `converter-notice ${ready ? "ready" : "blocked"}`;
    notice.textContent = ready
      ? `Runtime-ready · FLM ${version} · ${(profile.runtime || {}).tag || "profile registered"}`
      : `Engine development required · FLM ${version} (minimum ${minimum})`;

    const kernels = report ? report.kernels : Object.entries(profile.kernels || {}).map(([name, value]) => ({
      name, policy: value.policy, reason: value.reason, exists: false, validated: false,
    }));
    $("kernel-readiness-grid").innerHTML = kernels.map((kernel) => {
      const state = kernel.validated ? "validated" : (kernel.exists ? "unvalidated" : "missing");
      const badge = kernel.validated ? "loaded" : "not-loaded";
      return `<div><span class="badge ${badge}">${escapeHtml(state)}</span> ` +
        `<code class="inline-code">${escapeHtml(kernel.name)}</code><br>` +
        `<span class="hint">${escapeHtml(kernel.policy)} · ${escapeHtml(kernel.reason)}</span></div>`;
    }).join("");

    const blockers = report ? (report.blockers || []) : [];
    const warnings = report ? (report.warnings || []) : [];
    $("backend-blockers").innerHTML = [
      ...blockers.map((item) => `<li>${escapeHtml(item.message)}</li>`),
      ...warnings.map((item) => `<li>Warning: ${escapeHtml(item)}</li>`),
    ].join("") || "<li>No conversion artifact has been assessed yet.</li>";
  }

  async function loadPage() {
    try {
      const data = await jsonFetch("/api/flm-converter");
      populateModels(data.models || []);
      renderBackendStatus(data.backend || {});
      const ready = data.converter.available;
      availability.className = `converter-notice ${ready ? "ready" : "blocked"}`;
      availability.textContent = ready
        ? `Converter ready · output: ${data.converter.output_root}`
        : `Converter unavailable · ${(data.converter.missing || []).join("; ")}`;
      startButton.disabled = true;
      if (data.jobs && data.jobs.length) renderJob(data.jobs[0]);
    } catch (error) {
      availability.className = "converter-notice blocked";
      availability.textContent = error.message;
    }
  }

  async function validateSelected() {
    validatedPath = null;
    setStartState();
    if (!source.value) return;
    validation.className = "converter-validation hint";
    validation.textContent = "Reading GGUF metadata and checking tensor coverage…";
    try {
      const data = await jsonFetch("/api/flm-converter/validate", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({path: source.value}),
      });
      validatedPath = data.source;
      validation.className = "converter-validation ready";
      validation.innerHTML = `<strong>Eligible:</strong> ${escapeHtml(data.profile)} · ${data.layers} layers · ` +
        `${data.hidden_size} hidden · ${data.intermediate_size} intermediate · ${data.tensors} tensors · 0 unmapped.<br>` +
        `<strong>Runtime:</strong> text-only Q4NX weights supported; six kernels, the host ABI, artifact binding, and acceptance tests remain gated.`;
    } catch (error) {
      validation.className = "converter-validation blocked";
      validation.textContent = error.message;
    }
    setStartState();
  }

  function renderJob(job) {
    currentJobId = job.id;
    $("conversion-empty").classList.add("hidden");
    $("conversion-job").classList.remove("hidden");
    $("conversion-title").textContent = `${job.name} · ${job.state}`;
    $("conversion-percent").textContent = `${job.progress || 0}%`;
    $("conversion-bar").style.width = `${job.progress || 0}%`;
    $("conversion-detail").textContent = `${job.detail || ""} · output: ${job.output}`;
    $("conversion-log").textContent = (job.logs || []).join("\n");
    $("conversion-log").scrollTop = $("conversion-log").scrollHeight;
    const active = ["queued", "running", "cancelling"].includes(job.state);
    $("cancel-conversion").classList.toggle("hidden", !active);
    $("cancel-conversion").disabled = job.state === "cancelling";
    startButton.disabled = active || !validatedPath || !ack.checked;
    if (active && !pollTimer) pollTimer = window.setInterval(pollJob, 1000);
    if (!active && pollTimer) {
      window.clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  async function pollJob() {
    if (!currentJobId) return;
    try { renderJob(await jsonFetch(`/api/flm-converter/jobs/${currentJobId}`)); }
    catch (error) { $("conversion-detail").textContent = error.message; }
  }

  $("converter-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    startButton.disabled = true;
    try {
      const job = await jsonFetch("/api/flm-converter/jobs", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({path: source.value, name: $("converter-name").value,
          acknowledge_unrunnable: ack.checked}),
      });
      renderJob(job);
    } catch (error) {
      validation.className = "converter-validation blocked";
      validation.textContent = error.message;
      setStartState();
    }
  });

  $("validate-model").addEventListener("click", validateSelected);
  $("refresh-models").addEventListener("click", loadPage);
  source.addEventListener("change", () => { validatedPath = null; validation.textContent = "Validate this selection before converting."; setStartState(); });
  ack.addEventListener("change", setStartState);
  $("cancel-conversion").addEventListener("click", async () => {
    if (!currentJobId) return;
    try { renderJob(await jsonFetch(`/api/flm-converter/jobs/${currentJobId}/cancel`, {method: "POST"})); }
    catch (error) { $("conversion-detail").textContent = error.message; }
  });

  loadPage();
})();
