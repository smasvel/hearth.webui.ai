(() => {
  const usersListEl = document.getElementById("users-list");
  const usersPaginationEl = document.getElementById("users-pagination");
  const refreshUsersBtn = document.getElementById("refresh-users");
  const addUserForm = document.getElementById("add-user-form");
  const newUsernameEl = document.getElementById("new-username");
  const newPasswordEl = document.getElementById("new-password");
  const newRoleEl = document.getElementById("new-role");
  const addUserBtn = document.getElementById("add-user-btn");
  const caddySnippetCard = document.getElementById("caddy-snippet-card");
  const caddySnippetUsername = document.getElementById("caddy-snippet-username");
  const caddySnippetText = document.getElementById("caddy-snippet-text");
  const caddySnippetClose = document.getElementById("caddy-snippet-close");
  const caddySnippetCopy = document.getElementById("caddy-snippet-copy");

  let currentPage = 1;
  const perPage = 20;

  function escapeHtml(str) {
    return String(str).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function addSystemMessage(text, isError) {
    // No chat pane on this page — surface feedback the same way errors already
    // show up elsewhere in this app, just via a lightweight toast instead.
    const el = document.createElement("div");
    el.className = `users-toast ${isError ? "test-result-fail" : "test-result-ok"}`;
    el.textContent = text;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 4000);
  }

  function showCaddySnippet(username, snippet) {
    caddySnippetUsername.textContent = username;
    caddySnippetText.textContent = snippet;
    caddySnippetCard.classList.remove("hidden");
    caddySnippetCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  caddySnippetClose.addEventListener("click", () => caddySnippetCard.classList.add("hidden"));
  caddySnippetCopy.addEventListener("click", () => {
    navigator.clipboard.writeText(caddySnippetText.textContent).then(() => {
      const original = caddySnippetCopy.textContent;
      caddySnippetCopy.textContent = "Copied!";
      setTimeout(() => { caddySnippetCopy.textContent = original; }, 1500);
    }).catch(() => addSystemMessage("Could not copy to clipboard.", true));
  });

  async function viewCaddyToken(userId, username) {
    try {
      const res = await fetch(`/api/users/${userId}/caddy-token`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to fetch LAN token");
      showCaddySnippet(username, data.caddy_snippet);
    } catch (err) {
      addSystemMessage(`Could not fetch LAN token for ${username}: ${err.message}`, true);
    }
  }

  async function regenerateCaddyToken(userId, username) {
    const ok = window.confirm(
      `Regenerate the LAN token for "${username}"? The old one will stop working as soon as ` +
      `you paste the new snippet into the Caddyfile and reload Caddy — anything still using ` +
      `the old token (e.g. a device they set up earlier) will need updating too.`
    );
    if (!ok) return;
    try {
      const res = await fetch(`/api/users/${userId}/regenerate-caddy-token`, { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to regenerate LAN token");
      showCaddySnippet(username, data.caddy_snippet);
      addSystemMessage(`Regenerated LAN token for ${username} — update the Caddyfile to apply it.`);
    } catch (err) {
      addSystemMessage(`Could not regenerate LAN token for ${username}: ${err.message}`, true);
    }
  }

  async function fetchUsers(page = currentPage) {
    try {
      const res = await fetch(`/api/users?page=${page}&per_page=${perPage}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to list users");
      currentPage = data.page;
      renderUsersList(data.users || []);
      renderPagination(data);
    } catch (err) {
      usersListEl.innerHTML = `<p class="hint">Could not load users.<br>${escapeHtml(err.message)}</p>`;
    }
  }

  function renderUsersList(users) {
    if (users.length === 0) {
      usersListEl.innerHTML = `<p class="hint">No users found.</p>`;
      return;
    }
    usersListEl.innerHTML = "";
    for (const u of users) {
      const card = document.createElement("div");
      card.className = "model-card";
      card.innerHTML = `
        <div class="model-card-top">
          <div>
            <div class="model-name">${escapeHtml(u.username)}</div>
            <div class="model-meta">Added ${new Date(u.created_at * 1000).toLocaleDateString()}</div>
          </div>
          <span class="badge ${u.role === "admin" ? "loaded" : "not-loaded"}">${escapeHtml(u.role)}</span>
        </div>
        <div class="model-card-actions"></div>
      `;
      const actions = card.querySelector(".model-card-actions");

      const resetBtn = document.createElement("button");
      resetBtn.textContent = "Reset password";
      resetBtn.onclick = () => resetPassword(u.id, u.username);
      actions.appendChild(resetBtn);

      const tokenBtn = document.createElement("button");
      tokenBtn.textContent = "LAN token";
      tokenBtn.title = "View this user's personal Caddy LAN token";
      tokenBtn.onclick = () => viewCaddyToken(u.id, u.username);
      actions.appendChild(tokenBtn);

      const regenBtn = document.createElement("button");
      regenBtn.textContent = "Regenerate token";
      regenBtn.title = "Issue a new LAN token, invalidating the old one";
      regenBtn.onclick = () => regenerateCaddyToken(u.id, u.username);
      actions.appendChild(regenBtn);

      const delBtn = document.createElement("button");
      delBtn.textContent = "Delete";
      delBtn.className = "danger";
      delBtn.onclick = () => deleteUser(u.id, u.username);
      actions.appendChild(delBtn);

      usersListEl.appendChild(card);
    }
  }

  function renderPagination(data) {
    if (data.total_pages <= 1) {
      usersPaginationEl.innerHTML = "";
      return;
    }
    usersPaginationEl.innerHTML = "";
    const prevBtn = document.createElement("button");
    prevBtn.className = "panel-btn";
    prevBtn.textContent = "← Prev";
    prevBtn.disabled = data.page <= 1;
    prevBtn.onclick = () => fetchUsers(data.page - 1);
    usersPaginationEl.appendChild(prevBtn);

    const label = document.createElement("span");
    label.className = "hint";
    label.textContent = `Page ${data.page} of ${data.total_pages} (${data.total} users)`;
    usersPaginationEl.appendChild(label);

    const nextBtn = document.createElement("button");
    nextBtn.className = "panel-btn";
    nextBtn.textContent = "Next →";
    nextBtn.disabled = data.page >= data.total_pages;
    nextBtn.onclick = () => fetchUsers(data.page + 1);
    usersPaginationEl.appendChild(nextBtn);
  }

  async function deleteUser(userId, username) {
    const ok = window.confirm(`Delete user "${username}"? This cannot be undone.`);
    if (!ok) return;
    try {
      const res = await fetch(`/api/users/${userId}`, { method: "DELETE" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "delete failed");
      addSystemMessage(`Deleted user ${username}`);
    } catch (err) {
      addSystemMessage(`Failed to delete user ${username}: ${err.message}`, true);
    }
    // Deleting the last user on a page can leave it empty — step back a page rather
    // than showing a blank list with a stale "page 3 of 3" label.
    await fetchUsers(currentPage);
  }

  async function resetPassword(userId, username) {
    const newPassword = window.prompt(`New password for "${username}" (min 8 characters):`);
    if (newPassword === null) return;
    if (newPassword.length < 8) {
      addSystemMessage("Password must be at least 8 characters — not changed.", true);
      return;
    }
    try {
      const res = await fetch(`/api/users/${userId}/reset-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_password: newPassword }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "reset failed");
      addSystemMessage(`Password reset for ${username}`);
    } catch (err) {
      addSystemMessage(`Failed to reset password for ${username}: ${err.message}`, true);
    }
  }

  refreshUsersBtn.addEventListener("click", () => fetchUsers(currentPage));

  addUserForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = newUsernameEl.value.trim();
    const password = newPasswordEl.value;
    const role = newRoleEl.value;
    if (!username || !password) return;

    addUserBtn.disabled = true;
    try {
      const res = await fetch("/api/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password, role }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to create user");
      addSystemMessage(`Added user ${username} (${role})`);
      if (data.caddy_snippet) showCaddySnippet(username, data.caddy_snippet);
      newUsernameEl.value = "";
      newPasswordEl.value = "";
      newRoleEl.value = "user";
      await fetchUsers(1);
    } catch (err) {
      addSystemMessage(`Failed to add user: ${err.message}`, true);
    } finally {
      addUserBtn.disabled = false;
    }
  });

  fetchUsers();
})();
