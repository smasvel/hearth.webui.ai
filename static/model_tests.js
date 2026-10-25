// Persistent model validation jobs. All model output is rendered as text.
(() => {
  const el = id => document.getElementById(id);
  const activeStates = new Set(["loading", "running", "repairing", "cancelling"]);
  let job = null, running = false, starting = false, timer = null;
  async function api(path, body, method) {
    method = method || (body === undefined ? "GET" : "POST");
    const response = await fetch(path, body === undefined ? {method} : {
      method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
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
    el("tests-run").disabled = running || starting || !el("tests-model").value;
    el("tests-auto-repair").disabled = running;
    el("tests-history").disabled = running;
    el("tests-cancel").disabled = !running || job.status === "cancelling";
    el("tests-delete").disabled = !el("tests-delete-history").value;
    if (wasRunning !== running) document.dispatchEvent(new Event("model-tests-state"));
    if (wasRunning && !running) document.dispatchEvent(new Event("model-tests-finished"));
    if (!job) {
      el("tests-download").hidden = true;
      el("tests-table").hidden = true;
      el("tests-results").replaceChildren();
      el("tests-repairs").textContent = "";
      return;
    }
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
    el("tests-repairs").textContent = job.repairs.map(r => {
      let fixed = r.fixed, remaining = r.remaining;
      // Older reports included blocked dependent workflow steps in repair totals.
      // Recalculate from the saved attempts so those historical summaries read true.
      const attempts = job.results.filter(item => item.backend === r.backend && item.model === r.model);
      const first = attempts.filter(item => item.attempt === 1 && !item.passed && !item.skipped);
      const retry = attempts.filter(item => item.attempt === 2 && !item.passed && !item.skipped);
      if (attempts.some(item => item.attempt === 2)) {
        const key = item => JSON.stringify([item.name, !!item.stream]);
        const before = new Set(first.map(key)), after = new Set(retry.map(key));
        fixed = r.outcome === "Applied and verified"
          ? [...before].filter(name => !after.has(name)).length : 0;
        remaining = after.size;
      }
      return `${r.model}: ${r.outcome}${fixed !== undefined ? ` (${fixed} fixed, ${remaining} remaining)` : ""}`;
    }).join(" · ");
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
    const deleteList = el("tests-delete-history");
    const previousDeleteId = deleteList.value;
    deleteList.replaceChildren();
    const deletable = jobs.filter(item => !activeStates.has(item.status));
    for (const item of deletable) {
      const option = document.createElement("option"); option.value = item.id;
      option.textContent = `${new Date(item.started_at * 1000).toLocaleString()} · ${item.status} · ${item.targets.length} model(s)`;
      deleteList.appendChild(option);
    }
    if (deletable.some(item => item.id === previousDeleteId)) deleteList.value = previousDeleteId;
    el("tests-delete").disabled = !deleteList.value;
    if (!deletable.length) {
      const option = document.createElement("option"); option.value = "";
      option.textContent = jobs.length ? "No older completed reports" : "No saved reports";
      deleteList.appendChild(option);
      el("tests-delete").disabled = true;
    }
    job = jobs.find(j => j.id === selectId) || jobs.find(j => activeStates.has(j.status)) || jobs[0] || null;
    if (job) { list.value = job.id; job = await api(`/api/model-tests/${encodeURIComponent(job.id)}`); }
    render(); schedule();
  }
  async function start(models) {
    if (running || starting) return;
    if (!confirm(`Run the complete test suite for ${models.map(m => m.id).join(", ")}?\n\nLoading a local model unloads other local chat models. The tested local model stays loaded. The suite includes long OpenCode workflows and simulated question answers. Tests can take many minutes. No generated tools will be executed.`)) return;
    starting = true;
    document.dispatchEvent(new Event("model-tests-state"));
    el("tests-run").disabled = true;
    try {
      job = await api("/api/model-tests", {models: models.map(m => ({backend: m.backend, id: m.id})),
        auto_repair: el("tests-auto-repair").checked || new URLSearchParams(window.location.search).get("action") === "test-tools"});
      render(); schedule(); await history(job.id);
      el("model-tests").scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) { el("tests-status").textContent = error.message; el("tests-run").disabled = running || starting; }
    finally { starting = false; el("tests-run").disabled = running || !el("tests-model").value; document.dispatchEvent(new Event("model-tests-state")); }
  }
  async function reset(model) {
    if (running || !confirm(`Reset the automatic compatibility repair for ${model.id}?`)) return;
    try { await api("/api/model-tests/reset-profile", {backend: model.backend, model: model.id});
      el("tests-status").textContent = `Repair profile reset for ${model.id}.`; }
    catch (error) { el("tests-status").textContent = error.message; }
  }
  async function loadModels(preferred) {
    try {
      const {models} = await api("/api/models");
      const supported = new Set(["fastflowlm", "lmstudio", "lemonade", "vllm", "sglang", "kobold"]);
      const media = new Set(["embedding", "embeddings", "transcription", "tts", "image", "video"]);
      const targets = [...new Map(models.filter(m => supported.has(m.backend) && !media.has(m.type) && m.loadable !== false)
        .map(m => [JSON.stringify([m.backend, m.id]), m])).values()];
      const select = el("tests-model"); select.replaceChildren();
      const placeholder = document.createElement("option"); placeholder.value = "";
      placeholder.textContent = targets.length ? "Choose an installed chat model" : "No loadable chat models available";
      select.appendChild(placeholder);
      for (const model of targets) {
        const option = document.createElement("option");
        option.value = JSON.stringify({backend: model.backend, id: model.id});
        option.textContent = `${model.backend} · ${model.name || model.id}`;
        select.appendChild(option);
      }
      if (preferred) {
        const key = JSON.stringify({backend: preferred.backend, id: preferred.id});
        if ([...select.options].some(option => option.value === key)) select.value = key;
      }
      el("tests-run").disabled = running || starting || !select.value;
    } catch (error) { el("tests-status").textContent = `Could not load installed models: ${error.message}`; }
  }
  el("tests-model").addEventListener("change", () => {
    el("tests-run").disabled = running || starting || !el("tests-model").value;
  });
  el("tests-run").addEventListener("click", async () => {
    try {
      const selected = JSON.parse(el("tests-model").value || "null");
      if (!selected) throw new Error("Choose an installed chat model first.");
      const {models} = await api("/api/models");
      const model = models.find(item => item.backend === selected.backend && item.id === selected.id);
      if (!model) throw new Error("That model is no longer available.");
      await start([model]);
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
  el("tests-delete-history").addEventListener("change", event => {
    el("tests-delete").disabled = !event.target.value;
  });
  el("tests-delete").addEventListener("click", async () => {
    const deletedId = el("tests-delete-history").value;
    if (!deletedId) return;
    const report = [...el("tests-delete-history").options].find(option => option.value === deletedId);
    if (!confirm(`Delete ${report?.textContent || "this saved test report"} and its results? This cannot be undone.`)) return;
    try {
      await api(`/api/model-tests/${encodeURIComponent(deletedId)}`, undefined, "DELETE");
      el("tests-delete-status").textContent = running ? "Older report deleted. The active model test continues." : "Saved report deleted.";
      await history(job?.id === deletedId ? undefined : job?.id);
    } catch (error) { el("tests-delete-status").textContent = error.message; }
  });
  window.hearthModelTests = {start, reset, isRunning: () => running || starting};
  Promise.all([history(), loadModels()]).then(async () => {
    const params = new URLSearchParams(window.location.search);
    const backend = params.get("backend"), modelId = params.get("model");
    if (!backend || !modelId) return;
    window.history.replaceState({}, "", window.location.pathname);
    try {
      const selectedKey = JSON.stringify({backend, id: modelId});
      if ([...el("tests-model").options].some(option => option.value === selectedKey)) {
        el("tests-model").value = selectedKey;
        el("tests-run").disabled = running || starting;
      }
      const {models} = await api("/api/models");
      const model = models.find(item => item.backend === backend && item.id === modelId);
      if (!model) throw new Error("That model is no longer available.");
      if (params.get("action") === "reset" && running) el("tests-status").textContent = "A test run is active. Wait for it to finish before resetting a repair profile.";
      else if (params.get("action") === "reset") await reset(model);
      else if (running) el("tests-status").textContent = "A test run is already active; showing its progress instead.";
      else if (params.get("action") === "test-tools") {
        el("tests-auto-repair").checked = true;
        await start([model]);
      } else await start([model]);
    } catch (error) { el("tests-status").textContent = error.message; }
  }).catch(error => { el("tests-status").textContent = `Could not load test history: ${error.message}`; });
})();
