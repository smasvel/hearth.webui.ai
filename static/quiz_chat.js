// In-chat practice-test platform. Any quiz — built-in banks (DMV states, US Citizenship,
// Motorcycle, CDL, Trivia), a user's custom decks, or an AI-generated quiz on any topic —
// runs inside the normal chat window. The user types a keyword and questions + instant
// reasoning appear as chat bubbles. app.js's chat-form handler calls Quiz.maybeHandle(text)
// first and, if it returns true, skips sending the text to the LLM.
//
//   start DMV NC test          resume DMV NC test now       next
//   start citizenship test     start motorcycle test        DMV score / score
//   quiz me on the solar system, 10 questions               stop test
//   list tests                 start "My AWS deck" test      (custom deck by name)
//
// No-repeat guarantee: questions are drawn from a shuffled deck WITHOUT replacement within
// a test, and a lifetime "seen" set (persisted per deck in localStorage) makes retakes
// prefer unseen questions. Progress is persisted, so "resume" (and refresh / chat-switch)
// picks up the exact question you were on.
//
// CSS note: the visual classes are still named `dmv-*` for historical reasons — they're
// generic quiz styles now, reused across every deck.
(() => {
  const messagesEl = document.getElementById("messages");
  if (!messagesEl) return;

  const LS_KEY = (slug) => `quiz_sess_${slug}`;
  const LS_INDEX = "quiz_sess_index"; // list of deck slugs that have a saved session
  // Fallback category words so start/resume intent is recognised even before the deck
  // registry finishes loading (registry aliases are the real resolver).
  const CATEGORY_HINTS = ["dmv", "citizenship", "civics", "naturalization",
    "motorcycle", "moto", "cdl", "commercial", "trivia", "permit"];

  let registry = [];          // [{slug,name,category,aliases,meta,count,custom}]
  let registryLoaded = null;  // promise
  const banks = {};           // slug -> full bank {name, meta, questions:[...]}
  let session = null;         // active in-memory session (mirrors localStorage)

  // ---- small DOM helpers -----------------------------------------------------------
  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s ?? "";
    return d.innerHTML;
  }
  function scrollDown() { messagesEl.scrollTop = messagesEl.scrollHeight; }

  function echoUser(text) {
    const wrap = document.createElement("div");
    wrap.className = "msg-wrap user";
    const el = document.createElement("div");
    el.className = "msg user";
    el.textContent = text;
    wrap.appendChild(el);
    messagesEl.appendChild(wrap);
    scrollDown();
  }
  function botBubble() {
    const wrap = document.createElement("div");
    wrap.className = "msg-wrap assistant";
    const el = document.createElement("div");
    el.className = "msg assistant";
    const body = document.createElement("div");
    body.className = "answer-content dmv-chat";
    el.appendChild(body);
    wrap.appendChild(el);
    messagesEl.appendChild(wrap);
    scrollDown();
    return body;
  }
  function botText(html) { const b = botBubble(); b.innerHTML = html; scrollDown(); return b; }
  function letter(i) { return String.fromCharCode(65 + i); }

  // ---- registry --------------------------------------------------------------------
  function loadRegistry() {
    if (!registryLoaded) {
      registryLoaded = fetch("/api/quiz/decks")
        .then((r) => r.json())
        .then((d) => { registry = d.decks || []; return registry; })
        .catch(() => { registry = []; return registry; });
    }
    return registryLoaded;
  }

  // Resolve which deck a phrase refers to: match slug / name / aliases, longest match wins
  // (so "california dmv" beats the bare "dmv" alias). Returns a registry row or null.
  function resolveDeck(text) {
    let best = null, bestLen = 0;
    for (const d of registry) {
      const cands = [d.slug, d.name, ...(d.aliases || [])].map((c) => (c || "").toLowerCase());
      for (const c of cands) {
        if (c && c.length > bestLen && text.includes(c)) { best = d; bestLen = c.length; }
      }
    }
    return best;
  }

  // ---- persistence -----------------------------------------------------------------
  function load(slug) {
    try { const r = localStorage.getItem(LS_KEY(slug)); return r ? JSON.parse(r) : null; }
    catch { return null; }
  }
  function sessionSlugs() {
    try { return JSON.parse(localStorage.getItem(LS_INDEX)) || []; } catch { return []; }
  }
  function save() {
    if (!session) return;
    try {
      localStorage.setItem(LS_KEY(session.deck), JSON.stringify(session));
      const idx = sessionSlugs();
      if (!idx.includes(session.deck)) {
        idx.push(session.deck);
        localStorage.setItem(LS_INDEX, JSON.stringify(idx));
      }
    } catch { /* storage full/disabled — quiz still works this page load */ }
  }

  // A bank for the session's deck: cached, or embedded (AI decks), or fetched by slug.
  async function ensureBank(s) {
    if (banks[s.deck]) return banks[s.deck];
    if (s.aiQuestions) {
      banks[s.deck] = { slug: s.deck, name: s.deckName, meta: s.meta, questions: s.aiQuestions };
      return banks[s.deck];
    }
    const res = await fetch(`/api/quiz/bank?slug=${encodeURIComponent(s.deck)}`);
    if (!res.ok) throw new Error(`Couldn't load the “${s.deckName || s.deck}” question bank.`);
    banks[s.deck] = await res.json();
    return banks[s.deck];
  }
  function questionById(s, id) {
    return banks[s.deck]?.questions.find((q) => q.id === id) || null;
  }

  function shuffled(arr) {
    const a = arr.slice();
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }
  // Choose the question ids for a new test so tests don't repeat questions.
  //   • Draw only from questions NOT seen this cycle whenever possible.
  //   • The ENTIRE drawn deck is marked seen up front (returned as `seenAfter`), so the
  //     next test can never contain a question from this one — even ones you skip or don't
  //     reach.
  //   • When the bank no longer has `size` unseen questions, this test ends the cycle: it
  //     refills preferring questions NOT in the previous test (`lastDeck`) so two tests in a
  //     row never overlap, then starts a fresh cycle (`seenAfter` = just this deck).
  // Returns unique ids only (a question can't appear twice within one test).
  function buildDeck(bank, seenIds, size, lastDeck) {
    const allIds = bank.questions.map((q) => q.id);
    const seen = new Set(seenIds);
    const unseen = shuffled(allIds.filter((id) => !seen.has(id)));
    if (unseen.length >= size) {
      const deck = unseen.slice(0, size);
      return { deck, seenAfter: seenIds.concat(deck), cycled: false };
    }
    // Bank (nearly) exhausted → end the cycle. Refill from already-seen questions, avoiding
    // the previous test's questions first, so consecutive tests don't share questions.
    const last = new Set(lastDeck || []);
    const refill = shuffled(allIds.filter((id) => seen.has(id) && !last.has(id)))
      .concat(shuffled(allIds.filter((id) => seen.has(id) && last.has(id))));
    const deck = [...new Set(unseen.concat(refill))].slice(0, size);
    return { deck, seenAfter: deck.slice(), cycled: true };
  }

  // ---- chat-history persistence ----------------------------------------------------
  function logTurns(turns) {
    if (!session || !session.convId || !turns.length) return;
    fetch("/api/quiz/log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversation_id: session.convId,
        title: `${session.deckName} — practice test`,
        turns,
      }),
    }).then(() => window.ChatApp && window.ChatApp.refreshConversations()).catch(() => {});
  }

  // ---- starting a test -------------------------------------------------------------
  // Build a session from an already-resolved bank object (used by both deck-start and AI
  // generation). `aiQuestions` present => embed the bank so it survives reloads.
  function beginSession(bank, { aiQuestions = null } = {}) {
    const slug = bank.slug;
    const prev = load(slug);
    const seenIds = (prev && !aiQuestions) ? (prev.seenIds || []) : []; // AI decks: fresh each time
    const size = Math.min(bank.meta.official_test_size || bank.questions.length, bank.questions.length);
    const { deck, seenAfter, cycled } = buildDeck(bank, seenIds, size, prev && prev.lastDeck);
    banks[slug] = bank;

    session = {
      deck: slug,
      deckName: bank.name,
      meta: bank.meta,
      aiQuestions,                 // full question list for AI decks; null otherwise
      // The whole drawn deck is marked seen now, so the NEXT test won't repeat any of it.
      seenIds: seenAfter.slice(),
      lastDeck: deck.slice(),      // used to avoid back-to-back repeats when the bank cycles
      queue: deck,
      total: deck.length,
      asked: [],
      correct: 0,
      current: null,
      answered: false,
      active: true,
      convId: (window.ChatApp && window.ChatApp.ensureConversationId()) || null,
    };
    const passPct = bank.meta.pass_percent || 70;
    const bankSize = bank.questions.length;
    let cycleNote = "";
    if (bankSize <= size) {
      cycleNote = ` (This bank has only ${bankSize} questions, so every test uses all of them — they'll come up in a new order each time.)`;
    } else if (cycled) {
      cycleNote = ` You've now been through all ${bankSize} questions in this bank — nice — so a fresh cycle starts here, and it won't repeat your last test's questions.`;
    }
    botText(
      `<p><strong>${esc(bank.name)} — practice test.</strong> ${session.total} questions, ` +
      `${passPct}% to pass${bank.meta.source ? ` (${esc(bank.meta.source)})` : ""}.${cycleNote}</p>` +
      `<p class="hint">Tap an answer or type the letter (A–D). Type <strong>next</strong> to continue, ` +
      `<strong>score</strong> to check progress, or <strong>stop test</strong> to end.</p>`
    );
    save();
    logTurns([{ role: "system", content:
      `📝 Started ${bank.name} practice test — ${session.total} questions, ${passPct}% to pass` +
      `${bank.meta.source ? ` (${bank.meta.source})` : ""}.` }]);
    askNext();
  }

  async function startDeck(row) {
    let bank;
    try { bank = await ensureBank({ deck: row.slug, deckName: row.name }); }
    catch (err) { botText(`<p>${esc(err.message)}</p>`); return; }
    beginSession(bank);
  }

  async function generateQuiz(topic, count) {
    const model = window.ChatApp && window.ChatApp.getModel ? window.ChatApp.getModel() : "";
    if (!model) {
      botText(`<p>To build an AI quiz I need a model loaded — pick one in the top bar, then try again.</p>`);
      return;
    }
    const wait = botText(
      `<p>🧠 Generating a ${count}-question quiz on <strong>${esc(topic)}</strong> with the loaded model… ` +
      `<span class="hint">(this can take a moment on a local model)</span></p>`
    );
    try {
      const res = await fetch("/api/quiz/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic, count, model }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      wait.innerHTML = `<p>✅ Quiz ready: <strong>${esc(data.name)}</strong> — ${data.questions.length} questions.</p>`;
      // Synthetic slug so the session persists/resumes like any other deck.
      const slug = `ai-${Date.now().toString(36)}`;
      beginSession({ slug, name: data.name, meta: data.meta, questions: data.questions },
                   { aiQuestions: data.questions });
    } catch (err) {
      wait.innerHTML = `<p>Couldn't generate that quiz: ${esc(err.message)} <span class="hint">Try again or lower the question count.</span></p>`;
    }
  }

  // ---- question / answer flow ------------------------------------------------------
  function askNext() {
    if (!session) return;
    if (session.queue.length === 0) { finishTest(); return; }
    session.current = session.queue.shift();
    session.answered = false;
    save();
    renderQuestion(questionById(session, session.current));
  }

  function renderQuestion(q) {
    if (!q) return;
    const num = session.asked.length + 1;
    const body = botBubble();
    const pct = Math.round((session.asked.length / session.total) * 100);
    body.innerHTML =
      `<div class="dmv-chat-head">` +
      `<span class="dmv-chat-qnum">Question ${num} of ${session.total}</span>` +
      `<span class="dmv-chat-score">${session.asked.length ? `${session.correct}/${session.asked.length} correct` : ""}</span>` +
      `</div>` +
      `<div class="dmv-chat-progress"><div class="dmv-chat-progress-fill" style="width:${pct}%"></div></div>` +
      `<div class="dmv-chat-question">${esc(q.q)}</div>`;
    const opts = document.createElement("div");
    opts.className = "dmv-chat-options";
    q.options.forEach((opt, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "dmv-option";
      btn.innerHTML = `<span class="dmv-option-key">${letter(i)}</span><span>${esc(opt)}</span>`;
      btn.addEventListener("click", () => answer(i, body, opts, q));
      opts.appendChild(btn);
    });
    body.appendChild(opts);
    scrollDown();
    body._optsEl = opts;
    body._q = q;
    session._liveBody = body;
  }

  function answer(chosen, body, optsEl, q) {
    if (!session || session.answered) return;
    session.answered = true;
    const isRight = chosen === q.answer;
    if (isRight) session.correct++;
    session.asked.push({ id: q.id, chosen, correct: isRight });
    // (No seen-tracking needed here — buildDeck already marked the whole deck seen at start,
    // which is what guarantees the next test won't repeat any of these questions.)

    optsEl.querySelectorAll(".dmv-option").forEach((b, i) => {
      b.classList.add("locked");
      b.disabled = true;
      if (i === q.answer) b.classList.add("correct");
      else if (i === chosen) b.classList.add("wrong");
    });

    const explain = document.createElement("div");
    explain.className = "dmv-explain";
    const last = session.queue.length === 0;
    explain.innerHTML =
      `<div class="dmv-explain-head ${isRight ? "ok" : "no"}">${isRight ? "✓ Correct" : "✕ Not quite"}</div>` +
      `<div>${esc(q.explain)}</div>` +
      `<div class="dmv-chat-actions"></div>`;
    body.appendChild(explain);
    const nextBtn = document.createElement("button");
    nextBtn.type = "button";
    nextBtn.className = "dmv-chat-next";
    nextBtn.textContent = last ? "See results" : "Next question";
    nextBtn.addEventListener("click", () => askNext());
    explain.querySelector(".dmv-chat-actions").appendChild(nextBtn);
    save();
    scrollDown();

    const num = session.asked.length;
    logTurns([
      { role: "user", content: `Q${num}. ${q.q}\n\nMy answer: ${letter(chosen)}) ${q.options[chosen]}` },
      { role: "assistant", content: isRight
        ? `✓ **Correct.**\n\n${q.explain}`
        : `✕ **Not quite.** Correct answer: ${letter(q.answer)}) ${q.options[q.answer]}\n\n${q.explain}` },
    ]);
  }

  function answerByLetter(l) {
    const body = session && session._liveBody;
    if (!body || session.answered || !body._q) return false;
    const idx = l.toLowerCase().charCodeAt(0) - 97;
    if (idx < 0 || idx >= body._q.options.length) return false;
    answer(idx, body, body._optsEl, body._q);
    return true;
  }

  function finishTest() {
    if (!session) return;
    const total = session.asked.length;
    const passPct = session.meta.pass_percent || 70;
    const pct = total ? Math.round((session.correct / total) * 100) : 0;
    const passed = pct >= passPct;
    const missed = session.asked.filter((a) => !a.correct);

    let reviewHtml = "";
    if (missed.length) {
      reviewHtml = `<div class="dmv-chat-review"><div class="dmv-chat-review-title">Review the ${missed.length} you missed:</div>`;
      for (const a of missed) {
        const q = questionById(session, a.id);
        if (!q) continue;
        reviewHtml +=
          `<div class="dmv-chat-review-item">` +
          `<div class="dmv-review-q">${esc(q.q)}</div>` +
          `<div class="dmv-review-line no">Your answer: ${esc(a.chosen == null ? "—" : q.options[a.chosen])}</div>` +
          `<div class="dmv-review-line ok">Correct: ${esc(q.options[q.answer])}</div>` +
          `<div class="dmv-review-explain">${esc(q.explain)}</div></div>`;
      }
      reviewHtml += `</div>`;
    }
    const again = session.aiQuestions
      ? `Say <em>“quiz me on …”</em> for another AI quiz.`
      : `Say <em>“start ${esc(session.deckName)} test”</em> for a fresh set — no repeats until you've seen the whole bank.`;
    botText(
      `<div class="dmv-chat-result">` +
      `<span class="dmv-result-badge ${passed ? "pass" : "fail"}">${passed ? "PASS" : "KEEP PRACTICING"}</span>` +
      `<span class="dmv-big">${session.correct}<span class="dmv-of">/${total}</span></span>` +
      `<span class="dmv-pct">${pct}%</span></div>` +
      `<p>${passed
        ? `Nice — that's at or above the ${passPct}% pass mark for ${esc(session.deckName)}.`
        : `You need ${passPct}% to pass ${esc(session.deckName)}. Review the misses and try again.`}</p>` +
      reviewHtml + `<p class="hint">${again}</p>`
    );

    const summary = [
      `**Result: ${session.correct}/${total} (${pct}%) — ${passed ? "PASS" : "KEEP PRACTICING"}** · ${session.deckName}`,
      passed ? `At or above the ${passPct}% pass mark.` : `You need ${passPct}% to pass. Review and retry.`,
    ];
    if (missed.length) {
      summary.push("", `Missed ${missed.length}:`);
      for (const a of missed) {
        const q = questionById(session, a.id);
        if (q) summary.push(`- ${q.q} → **${letter(q.answer)}) ${q.options[q.answer]}**`);
      }
    }
    logTurns([{ role: "assistant", content: summary.join("\n") }]);
    session.active = false;
    session.current = null;
    save();
  }

  // ---- resume / score / stop / list ------------------------------------------------
  function activeSession() { return session && session.active ? session : null; }

  async function resumeDeck(row) {
    const slug = row ? row.slug : (session ? session.deck : null);
    const prev = slug ? load(slug) : null;
    if (!prev || !prev.active || (prev.queue?.length === 0 && prev.current == null)) {
      botText(`<p>No ${esc(row ? row.name : "test")} in progress. Say <em>“start ${esc(row ? row.name : "…")} test”</em> to begin.</p>`);
      return;
    }
    session = prev;
    try { await ensureBank(session); } catch (err) { botText(`<p>${esc(err.message)}</p>`); return; }
    botText(
      `<p><strong>Resuming ${esc(session.deckName)}.</strong> ${session.asked.length} answered, ` +
      `${session.correct} correct so far.</p>`
    );
    if (session.current != null && !session.answered) renderQuestion(questionById(session, session.current));
    else askNext();
  }

  function showScore(row) {
    const s = activeSession() || (row ? load(row.slug) : session);
    if (!s || !s.asked || s.asked.length === 0) {
      botText(`<p>No progress yet. Say <em>“start … test”</em> or <em>“list tests”</em> to see what's available.</p>`);
      return;
    }
    const done = s.asked.length;
    const pct = Math.round((s.correct / done) * 100);
    const remaining = (s.queue?.length || 0) + (s.current != null && !s.answered ? 1 : 0);
    botText(
      `<p><strong>${esc(s.deckName)} — current test:</strong> ${s.correct}/${done} correct (${pct}%). ` +
      `${remaining ? `${remaining} to go — say <em>“resume”</em>.` : "Test complete."}` +
      `${s.aiQuestions ? "" : `<br><span class="hint">Lifetime: ${(s.seenIds || []).length} unique questions seen.</span>`}</p>`
    );
  }

  function stopTest(row) {
    const s = activeSession() || (row ? load(row.slug) : session);
    if (!s) { botText(`<p>No test is running.</p>`); return; }
    s.active = false; s.current = null;
    try { localStorage.setItem(LS_KEY(s.deck), JSON.stringify(s)); } catch {}
    if (session && session.deck === s.deck) session.active = false;
    botText(`<p>Stopped ${esc(s.deckName)}. Progress saved — say <em>“resume ${esc(s.deckName)} test”</em> to pick up, or <em>“start …”</em> for a fresh set.</p>`);
  }

  async function showList() {
    await loadRegistry();
    const byCat = {};
    for (const d of registry) (byCat[d.category] = byCat[d.category] || []).push(d);
    const CAT_LABEL = { dmv: "🚗 DMV / driving", citizenship: "🗽 Citizenship", motorcycle: "🏍️ Motorcycle",
      cdl: "🚛 CDL", trivia: "🧠 Trivia", custom: "⭐ Your custom decks" };
    let html = `<p><strong>Practice tests you can take — right here in chat:</strong></p>`;
    for (const cat of ["dmv", "citizenship", "motorcycle", "cdl", "trivia", "custom"]) {
      const list = byCat[cat];
      if (!list || !list.length) continue;
      html += `<p style="margin:8px 0 2px"><strong>${CAT_LABEL[cat] || cat}</strong></p><ul class="dmv-chat-help">`;
      for (const d of list) html += `<li><strong>start ${esc(d.name.replace(/^DMV — /, "DMV ").replace(/ \(.*\)$/, ""))} test</strong> — ${d.count} questions</li>`;
      html += `</ul>`;
    }
    html += `<p style="margin-top:10px">Or generate one on <em>any</em> topic: <strong>“quiz me on the water cycle, 8 questions”</strong>.</p>` +
      `<p class="hint">Build & save your own at <a href="/quizzes">Quiz Builder</a>. Answer by tapping or typing A–D; type <strong>next</strong>, <strong>score</strong>, or <strong>stop test</strong> anytime.</p>`;
    botText(html);
  }

  // ---- resume-on-load / chat-switch (called by app.js) -----------------------------
  function findActiveByConv(convId) {
    for (const slug of sessionSlugs()) {
      const s = load(slug);
      if (s && s.active && s.convId === convId && (s.queue?.length || s.current != null)) return s;
    }
    return null;
  }
  async function resumeInConversation(convId) {
    const s = findActiveByConv(convId);
    if (!s) return false;
    session = s;
    try { await ensureBank(session); } catch { return false; }
    botText(`<p class="hint">▶ Resuming ${esc(session.deckName)} — ${session.asked.length} answered, ${session.correct} correct. Pick up where you left off:</p>`);
    if (session.current != null && !session.answered) renderQuestion(questionById(session, session.current));
    else askNext();
    return true;
  }
  let autoRestoreTries = 0;
  function autoRestore() {
    // app.js defines window.ChatApp synchronously, but guard against unusual load/order
    // timing (deferred scripts, bfcache) by retrying briefly instead of silently bailing —
    // bailing here is exactly how an in-progress test "disappears" after a refresh.
    if (!window.ChatApp || !window.ChatApp.loadConversation) {
      if (autoRestoreTries++ < 40) setTimeout(autoRestore, 50);
      return;
    }
    for (const slug of sessionSlugs()) {
      const s = load(slug);
      if (s && s.active && s.convId && (s.queue?.length || s.current != null)) {
        // If the saved conversation was deleted, loadConversation resolves false — retire
        // the stale session so it doesn't try to auto-restore a dead chat on every refresh.
        Promise.resolve(window.ChatApp.loadConversation(s.convId)).then((ok) => {
          if (ok === false) {
            s.active = false;
            try { localStorage.setItem(LS_KEY(slug), JSON.stringify(s)); } catch {}
          }
        });
        return;
      }
    }
  }

  // ---- command parsing -------------------------------------------------------------
  function extractCount(t) {
    const m = t.match(/(\d{1,2})\s*(?:questions?|q\b|items?)/) || t.match(/\b(\d{1,2})\b/);
    if (m) { const n = parseInt(m[1], 10); if (n >= 1) return Math.min(n, 20); }
    return 8;
  }
  function extractTopic(t) {
    let m = t.match(/quiz me (?:on|about) (.+)/) ||
            t.match(/(?:generate|make|create|build|give me|write)\s+(?:me\s+)?(?:a|an|another)?\s*(?:\d{1,2}[- ]?(?:question)?s?\s*)?quiz (?:on|about) (.+)/) ||
            t.match(/(?:test|quiz) me (?:on|about) (.+)/);
    if (!m) return null;
    // strip a trailing "... , N questions" clause from the topic
    return m[1].replace(/[,:;]?\s*\d{1,2}\s*(?:questions?|q)\b.*$/i, "").replace(/[.?!]+$/, "").trim();
  }

  // Returns true if this text was a quiz command (handled inline, not sent to the LLM).
  function maybeHandle(text) {
    const raw = (text || "").trim();
    const t = raw.toLowerCase();
    const active = activeSession();

    // Answer the on-screen question by a bare letter.
    if (active && session.current != null && !session.answered) {
      const m = t.match(/^(?:answer\s+)?([a-d])[).:]?$/);
      if (m) {
        echoUser(raw);
        if (!answerByLetter(m[1])) botText(`<p class="hint">That letter isn't an option for this question.</p>`);
        return true;
      }
    }
    // next / skip / continue while a test is active.
    if (active && /^(next( question)?|skip|continue|next one)$/.test(t)) {
      echoUser(raw); askNext(); return true;
    }

    // AI-generated quiz on any topic.
    const topic = extractTopic(t);
    if (topic) { echoUser(raw); generateQuiz(topic, extractCount(t)); return true; }

    // List available tests.
    if (/\b(list|show|what)\b.*\b(tests?|quiz|quizzes|decks?)\b/.test(t) ||
        /^(tests?|quizzes|practice tests?)\??$/.test(t)) {
      echoUser(raw); showList(); return true;
    }

    // Real quiz commands are short imperative phrases ("start DMV NC test", "score",
    // "list tests" -- see the examples at the top of this file, none over ~6 words).
    // Without a length cap, a bare "test"/"quiz" word anywhere combined with a word like
    // "progress"/"score" hijacks ordinary long sentences too -- e.g. a coding request like
    // "write a test to track progress of the code generation" matched both mentionsQuizWord
    // and the score/progress regex below and got swallowed here instead of reaching the LLM.
    const isShortCommand = raw.trim().split(/\s+/).length <= 8;

    const mentionsQuizWord = isShortCommand && /\b(test|quiz|exam|practice)\b/.test(t);
    const hasHint = mentionsQuizWord ||
      (isShortCommand && CATEGORY_HINTS.some((h) => new RegExp(`\\b${h}\\b`).test(t))) ||
      (isShortCommand && registry.some((d) => [d.slug, d.name, ...(d.aliases || [])].some((c) => c && t.includes((c || "").toLowerCase()))));

    if (!hasHint) return false;

    if (/\b(stop|end|quit|cancel|exit|pause)\b/.test(t)) {
      echoUser(raw); stopTest(active ? null : resolveDeck(t)); return true;
    }
    if (/\b(score|results?|progress|how am i)\b/.test(t)) {
      echoUser(raw); showScore(active ? null : resolveDeck(t)); return true;
    }
    if (/\b(resume|pick up|carry on)\b/.test(t) || (/\bcontinue\b/.test(t) && !active)) {
      echoUser(raw);
      loadRegistry().then(() => resumeDeck(resolveDeck(t)));
      return true;
    }
    if (/\b(start|begin|take|new|do|give me)\b/.test(t) || mentionsQuizWord) {
      echoUser(raw);
      loadRegistry().then(() => {
        const row = resolveDeck(t);
        if (row) startDeck(row);
        else showList(); // couldn't tell which test — show the menu
      });
      return true;
    }
    return false;
  }

  window.Quiz = { maybeHandle, resumeInConversation };

  loadRegistry();
  if (document.readyState === "loading") window.addEventListener("DOMContentLoaded", autoRestore);
  else autoRestore();
})();
