(() => {
  const cpuEl = document.getElementById("stat-cpu");
  const ramEl = document.getElementById("stat-ram");
  const diskEl = document.getElementById("stat-disk");
  const tempEl = document.getElementById("stat-temp");
  if (!cpuEl || !ramEl || !diskEl || !tempEl) return;

  function severityClass(value, thresholds = [70, 90]) {
    const [warn, danger] = thresholds;
    if (value >= danger) return "stat-danger";
    if (value >= warn) return "stat-warn";
    return "stat-ok";
  }

  let pending = false;
  async function fetchSystemStats() {
    if (pending || document.hidden) return;
    pending = true;
    try {
      const response = await fetch("/api/system-stats");
      if (!response.ok) return;
      const stats = await response.json();
      cpuEl.textContent = `CPU ${Math.round(stats.cpu_percent)}%`;
      cpuEl.className = severityClass(stats.cpu_percent);
      ramEl.textContent = `RAM ${stats.mem_used_gb}/${stats.mem_total_gb} GB (${Math.round(stats.mem_percent)}%)`;
      ramEl.className = severityClass(stats.mem_percent);
      diskEl.textContent = `Disk ${stats.disk_free_gb} GB free`;
      diskEl.className = severityClass(stats.disk_percent);
      if (typeof stats.cpu_temp_c === "number") {
        tempEl.textContent = `${Math.round(stats.cpu_temp_c)}°C`;
        tempEl.className = severityClass(stats.cpu_temp_c, [70, 85]);
      } else {
        tempEl.textContent = "";
        tempEl.className = "";
      }
    } catch {
      // Keep the last successful reading on transient errors.
    } finally {
      pending = false;
    }
  }

  fetchSystemStats();
  window.setInterval(fetchSystemStats, 3000);
})();
