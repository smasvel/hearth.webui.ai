// Persistent model validation jobs. All model output is rendered as text.
(() => {
  const el = id => document.getElementById(id);
  const activeStates = new Set(["loading", "running", "repairing", "cancelling"]);
  let job = null, running = false, starting = false, timer = null;
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    return data;
  }
  function cell(row, label, value, className = "") {
    const td = document.createElement("td");
    td.dataset.label = label; td.className = className;
    td.textContent = value ?? ""; row.appendChild(td);
    return td;
  }
  function render() {
    const wasRunning = running;
    running = !!job && activeStates.has(job.status);
    el("tests-all").disabled = running || starting;
    el("tests-auto-repair").disabled = running;
    el("tests-history").disabled = running;
    el("tests-cancel").disabled = !running || job.status === "cancelling";
    if (wasRunning !== running) document.dispatchEvent(new Event("model-tests-state"));
    if (wasRunning && !running) document.dispatchEvent(new Event("model-tests-finished"));
    if (!job) return;
    const selected = [...el("tests-history").options].find(o => o.value === job.id);
    if (selected) selected.textContent = `${new Date(job.started_at * 1000).toLocaleString()} · ${job.status} · ${job.targets.length} model(s)`;
    const latest = new Map();
    for (const r of job.results.filter(r => !r.rolled_back)) latest.set(JSON.stringify([r.backend, r.model, r.name, !!r.stream]), r);
    const passed = [...latest.values()].filter(r => r.passed).length;
    const blocked = [...latest.values()].filter(r => r.skipped).length;
    const failed = latest.size - passed - blocked;
    el("tests-status").textContent = `${job.status} · ${passed} passed · ${failed} failed` +
      (blocked ? ` · ${blocked} dependent steps blocked` : "") +
      (job.current_model ? ` · ${job.current_model.id}` : "") +
      (job.current_check && running ? ` · ${job.current_check.name} (${job.current_check.stream ? "streaming" : "normal"}, ${Math.max(0, Math.round(Date.now() / 1000 - job.current_check.started_at))}s)` : "") +
      (job.error ? ` · ${job.error}` : "") +
      (job.status === "cancelling" ? " · Waiting for the current request to finish." : "");
    el("tests-download").hidden = false;
    el("tests-download").href = `/api/model-tests/${encodeURIComponent(job.id)}?download=1`;
    el("tests-download").download = `model-test-${job.id}.json`;
    const tbody = el("tests-results");
    const expanded = new Set([...tbody.querySelectorAll("details[open]")].map(d => d.dataset.resultKey));
    tbody.replaceChildren();
    for (const [index, result] of job.results.entries()) {
      const row = document.createElement("tr");
      cell(row, "Model", `${result.backend}/${result.model}`, "test-model");
      cell(row, "Check", result.name, "test-check");
      cell(row, "Mode", result.stream ? "Streaming" : "Normal", "test-mode");
      cell(row, "Attempt", result.attempt, "test-number test-attempt");
      const status = result.skipped ? "blocked" : result.passed ? "passed" : "failed";
      const statusCell = cell(row, "Result", "", "test-result");
      const badge = document.createElement("span");
      badge.className = `test-result-badge ${status}`;
      badge.textContent = status[0].toUpperCase() + status.slice(1);
      statusCell.appendChild(badge);
      if (result.rolled_back) statusCell.appendChild(document.createTextNode(" (rolled back)"));
      cell(row, "Seconds", typeof result.seconds === "number" ? result.seconds.toFixed(2) : "—", "test-number test-seconds");
      const detailsCell = cell(row, "Details", "", "test-details");
      if (result.error) {
        const details = document.createElement("details");
        details.dataset.resultKey = `${job.id}:${index}`;
        details.open = expanded.has(details.dataset.resultKey);
        const summary = document.createElement("summary"); summary.textContent = "View details";
        const message = document.createElement("div"); message.textContent = result.error;
        details.append(summary, message); detailsCell.appendChild(details);
      } else {
        detailsCell.textContent = "—";
        detailsCell.classList.add("test-details-empty");
      }
      tbody.appendChild(row);
    }
    el("tests-table").hidden = job.results.length === 0;
    el("tests-repairs").textContent = job.repairs.map(r =>
      `${r.model}: ${r.outcome}${r.fixed !== undefined ? ` (${r.fixed} fixed, ${r.remaining} remaining)` : ""}`
    ).join(" · ");
  }
  function schedule() {
    clearTimeout(timer);
    if (running) timer = setTimeout(poll, 1800);
  }
  async function poll() {
    try { job = await api(`/api/model-tests/${encodeURIComponent(job.id)}`); render(); schedule(); }
    catch (error) {
      el("tests-status").textContent = `Progress unavailable: ${error.message}. Reconnecting…`;
      timer = setTimeout(poll, 4000);
    }
  }
  async function history(selectId) {
    const {jobs} = await api("/api/model-tests");
    const list = el("tests-history"); list.replaceChildren();
    for (const item of jobs) {
      const option = document.createElement("option"); option.value = item.id;
      option.textContent = `${new Date(item.started_at * 1000).toLocaleString()} · ${item.status} · ${item.targets.length} model(s)`;
      list.appendChild(option);
    }
    job = jobs.find(j => j.id === selectId) || jobs.find(j => activeStates.has(j.status)) || jobs[0] || null;
    if (job) { list.value = job.id; job = await api(`/api/model-tests/${encodeURIComponent(job.id)}`); }
    render(); schedule();
  }
  async function start(models) {
    if (running || starting) return;
    if (!confirm(`Run the complete test suite for ${models.length} model(s)?\n\nLoading a local model unloads other local chat models. The last tested local model stays loaded. The suite includes long OpenCode workflows and simulated question answers. Tests can take many minutes per model. No generated tools will be executed.`)) return;
    starting = true;
    document.dispatchEvent(new Event("model-tests-state"));
    el("tests-all").disabled = true;
    try {
      job = await api("/api/model-tests", {models: models.map(m => ({backend: m.backend, id: m.id})),
        auto_repair: el("tests-auto-repair").checked});
      render(); schedule(); await history(job.id);
      el("model-tests").scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) { el("tests-status").textContent = error.message; el("tests-all").disabled = running || starting; }
    finally { starting = false; el("tests-all").disabled = running; document.dispatchEvent(new Event("model-tests-state")); }
  }
  async function reset(model) {
    if (running || !confirm(`Reset the automatic compatibility repair for ${model.id}?`)) return;
    try { await api("/api/model-tests/reset-profile", {backend: model.backend, model: model.id});
      el("tests-status").textContent = `Repair profile reset for ${model.id}.`; }
    catch (error) { el("tests-status").textContent = error.message; }
  }
  el("tests-all").addEventListener("click", async () => {
    try {
      const {models} = await api("/api/models");
      const supported = new Set(["fastflowlm", "lmstudio", "lemonade", "vllm", "sglang", "kobold"]);
      const media = new Set(["embedding", "embeddings", "transcription", "tts", "image", "video"]);
      const targets = models.filter(m => supported.has(m.backend) && !media.has(m.type) && m.loadable !== false);
      if (!targets.length) throw new Error("No loadable chat models are available.");
      await start([...new Map(targets.map(m => [JSON.stringify([m.backend, m.id]), m])).values()]);
    } catch (error) { el("tests-status").textContent = error.message; }
  });
  el("tests-cancel").addEventListener("click", async () => {
    try { job = await api(`/api/model-tests/${encodeURIComponent(job.id)}/cancel`, {}); render(); schedule(); }
    catch (error) { el("tests-status").textContent = error.message; }
  });
  el("tests-history").addEventListener("change", async event => {
    // Do not hide a running job, which would enable conflicting model operations.
    if (running && event.target.value !== job.id) { event.target.value = job.id; return; }
    clearTimeout(timer);
    try { job = await api(`/api/model-tests/${encodeURIComponent(event.target.value)}`); render(); schedule(); }
    catch (error) { el("tests-status").textContent = error.message; }
  });
  window.hearthModelTests = {start, reset, isRunning: () => running || starting};
  history().catch(error => { el("tests-status").textContent = `Could not load test history: ${error.message}`; });
})();
