// Quiz Builder: author custom multiple-choice decks, save them, and take them in chat.
(() => {
  const el = (id) => document.getElementById(id);
  const questionsEl = el("questions");
  const qTemplate = el("q-template");
  const saveMsg = el("save-msg");
  const OPTION_COUNT = 4;

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s ?? "";
    return d.innerHTML;
  }

  // Build one question block: text, N options each with a "correct" radio, explanation.
  function addQuestion() {
    const node = qTemplate.content.firstElementChild.cloneNode(true);
    const optsWrap = node.querySelector(".quizb-options");
    const groupName = "correct-" + Math.random().toString(36).slice(2);
    for (let i = 0; i < OPTION_COUNT; i++) {
      const row = document.createElement("label");
      row.className = "quizb-opt";
      row.innerHTML =
        `<input type="radio" name="${groupName}" ${i === 0 ? "checked" : ""} title="Mark as the correct answer" />` +
        `<span class="quizb-opt-key">${String.fromCharCode(65 + i)}</span>`;
      const input = document.createElement("input");
      input.type = "text";
      input.className = "panel-input quizb-opt-text";
      input.placeholder = `Option ${String.fromCharCode(65 + i)}`;
      row.appendChild(input);
      optsWrap.appendChild(row);
    }
    node.querySelector(".quizb-remove").addEventListener("click", () => {
      node.remove();
      renumber();
    });
    questionsEl.appendChild(node);
    renumber();
    node.querySelector(".quizb-qtext").focus();
  }

  function renumber() {
    [...questionsEl.querySelectorAll(".quizb-q")].forEach((q, i) => {
      q.querySelector(".quizb-q-num").textContent = `Question ${i + 1}`;
    });
  }

  function gather() {
    const questions = [];
    for (const q of questionsEl.querySelectorAll(".quizb-q")) {
      const text = q.querySelector(".quizb-qtext").value.trim();
      const optInputs = [...q.querySelectorAll(".quizb-opt-text")];
      const radios = [...q.querySelectorAll('input[type="radio"]')];
      const options = optInputs.map((i) => i.value.trim());
      const answer = radios.findIndex((r) => r.checked);
      const filled = options.filter(Boolean);
      if (!text || filled.length < 2) continue; // skip incomplete rows
      // Compact options to the non-empty ones, keeping the correct index aligned.
      const kept = [];
      let newAnswer = 0;
      options.forEach((o, i) => {
        if (o) { if (i === answer) newAnswer = kept.length; kept.push(o); }
      });
      questions.push({
        q: text,
        options: kept,
        answer: newAnswer,
        explain: q.querySelector(".quizb-explain").value.trim(),
      });
    }
    return questions;
  }

  async function saveDeck() {
    const name = el("deck-name").value.trim();
    const questions = gather();
    saveMsg.className = "hint";
    if (!name) { saveMsg.textContent = "Give the deck a name."; return; }
    if (!questions.length) { saveMsg.textContent = "Add at least one complete question (text + 2 options)."; return; }
    saveMsg.textContent = "Saving…";
    try {
      const res = await fetch("/api/quiz/decks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, questions }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      saveMsg.textContent = `Saved “${data.deck.name}” (${data.deck.count} questions). Take it in chat: “start ${data.deck.name} test”.`;
      el("deck-name").value = "";
      questionsEl.innerHTML = "";
      addQuestion();
      loadDecks();
    } catch (err) {
      saveMsg.textContent = `Couldn't save: ${err.message}`;
    }
  }

  async function loadDecks() {
    const listEl = el("deck-list");
    try {
      const res = await fetch("/api/quiz/decks");
      const data = await res.json();
      const custom = (data.decks || []).filter((d) => d.custom);
      if (!custom.length) {
        listEl.innerHTML = `<p class="hint">No custom decks yet. Build one above — or in chat, try “quiz me on &lt;topic&gt;”.</p>`;
        return;
      }
      listEl.innerHTML = "";
      for (const d of custom) {
        const row = document.createElement("div");
        row.className = "quizb-deck";
        row.innerHTML =
          `<div><strong>${esc(d.name)}</strong> <span class="hint">· ${d.count} question${d.count === 1 ? "" : "s"}</span>` +
          `<div class="hint">Take it in chat: <code class="inline-code">start ${esc(d.name)} test</code></div></div>`;
        const del = document.createElement("button");
        del.className = "panel-btn danger";
        del.textContent = "Delete";
        del.addEventListener("click", () => deleteDeck(d.slug, d.name));
        row.appendChild(del);
        listEl.appendChild(row);
      }
    } catch {
      listEl.innerHTML = `<p class="hint">Couldn't load your decks.</p>`;
    }
  }

  async function deleteDeck(slug, name) {
    if (!confirm(`Delete “${name}”? This can't be undone.`)) return;
    try {
      await fetch(`/api/quiz/decks/${encodeURIComponent(slug)}`, { method: "DELETE" });
      loadDecks();
    } catch { /* ignore */ }
  }

  el("add-q").addEventListener("click", addQuestion);
  el("save-deck").addEventListener("click", saveDeck);
  addQuestion();
  loadDecks();
})();
