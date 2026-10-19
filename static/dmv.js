// DMV practice test: start -> one-question-at-a-time quiz with instant feedback ->
// results with review. Questions (including answers/explanations) are fetched once per
// test from /api/dmv/quiz and the whole flow runs client-side — it's a practice aid
// with immediate feedback, not a proctored exam, so no per-answer round trip.
(() => {
  const el = (id) => document.getElementById(id);
  const screens = {
    start: el("dmv-start"),
    quiz: el("dmv-quiz"),
    results: el("dmv-results"),
  };
  const stateSelect = el("dmv-state");
  const stateInfo = el("dmv-state-info");

  let quiz = null;         // { name, meta, questions: [...] }
  let idx = 0;             // current question index
  let correctCount = 0;
  let answered = [];       // per-question chosen index (or null), for review

  function show(name) {
    for (const [k, node] of Object.entries(screens)) node.classList.toggle("hidden", k !== name);
    window.scrollTo(0, 0);
  }

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s ?? "";
    return d.innerHTML;
  }

  async function loadStates() {
    try {
      const res = await fetch("/api/dmv/states");
      const data = await res.json();
      stateSelect.innerHTML = "";
      for (const s of data.states) {
        const opt = document.createElement("option");
        opt.value = s.code;
        opt.textContent = `${s.name} (${s.count} questions)`;
        stateSelect.appendChild(opt);
      }
      renderStateInfo();
    } catch {
      stateInfo.textContent = "Couldn't load states. Try refreshing.";
    }
  }

  function renderStateInfo() {
    const name = stateSelect.options[stateSelect.selectedIndex]?.textContent || "";
    stateInfo.innerHTML = `You'll get a shuffled set for <strong>${esc(name.split(" (")[0])}</strong>.
      Answer each question, see whether you were right, and get an explanation. Retakes reshuffle.`;
  }

  async function startQuiz() {
    const state = stateSelect.value;
    stateInfo.textContent = "Loading questions…";
    try {
      const res = await fetch(`/api/dmv/quiz?state=${encodeURIComponent(state)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      quiz = await res.json();
    } catch (err) {
      stateInfo.textContent = `Couldn't start: ${err.message}`;
      return;
    }
    idx = 0;
    correctCount = 0;
    answered = new Array(quiz.questions.length).fill(null);
    renderQuestion();
    show("quiz");
  }

  function renderQuestion() {
    const total = quiz.questions.length;
    const q = quiz.questions[idx];
    el("dmv-qnum").textContent = `Question ${idx + 1} of ${total}`;
    el("dmv-score-live").textContent = idx > 0 ? `${correctCount}/${idx} correct` : "";
    el("dmv-progress-fill").style.width = `${(idx / total) * 100}%`;
    el("dmv-question").textContent = q.q;

    const optionsEl = el("dmv-options");
    optionsEl.innerHTML = "";
    q.options.forEach((opt, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "dmv-option";
      btn.innerHTML = `<span class="dmv-option-key">${String.fromCharCode(65 + i)}</span><span>${esc(opt)}</span>`;
      btn.addEventListener("click", () => choose(i));
      optionsEl.appendChild(btn);
    });

    el("dmv-explain").classList.add("hidden");
    el("dmv-next-btn").classList.add("hidden");
  }

  function choose(chosen) {
    const q = quiz.questions[idx];
    if (answered[idx] !== null) return; // lock after first answer
    answered[idx] = chosen;
    const isRight = chosen === q.answer;
    if (isRight) correctCount++;

    const buttons = el("dmv-options").querySelectorAll(".dmv-option");
    buttons.forEach((b, i) => {
      b.classList.add("locked");
      if (i === q.answer) b.classList.add("correct");
      else if (i === chosen) b.classList.add("wrong");
    });

    const explain = el("dmv-explain");
    explain.innerHTML =
      `<div class="dmv-explain-head ${isRight ? "ok" : "no"}">${isRight ? "✓ Correct" : "✕ Not quite"}</div>
       <div>${esc(q.explain)}</div>`;
    explain.classList.remove("hidden");

    const nextBtn = el("dmv-next-btn");
    nextBtn.textContent = idx + 1 === quiz.questions.length ? "See results" : "Next question";
    nextBtn.classList.remove("hidden");
    nextBtn.focus();
  }

  function next() {
    if (idx + 1 >= quiz.questions.length) {
      renderResults();
      show("results");
    } else {
      idx++;
      renderQuestion();
    }
  }

  function renderResults() {
    const total = quiz.questions.length;
    const pct = Math.round((correctCount / total) * 100);
    const passPct = quiz.meta.pass_percent || 80;
    const passed = pct >= passPct;

    const badge = el("dmv-result-badge");
    badge.textContent = passed ? "PASS" : "KEEP PRACTICING";
    badge.className = `dmv-result-badge ${passed ? "pass" : "fail"}`;

    el("dmv-result-score").innerHTML =
      `<span class="dmv-big">${correctCount}<span class="dmv-of">/${total}</span></span>
       <span class="dmv-pct">${pct}%</span>`;
    el("dmv-result-msg").textContent = passed
      ? `Nice — that's at or above the ${passPct}% needed to pass the ${quiz.name} knowledge test. Keep it up.`
      : `You need ${passPct}% to pass the ${quiz.name} knowledge test. Review the ones you missed and try again.`;

    // Build the (hidden until requested) review list
    const review = el("dmv-review");
    review.innerHTML = "";
    review.classList.add("hidden");
    el("dmv-review-btn").textContent = "Review answers";
    quiz.questions.forEach((q, i) => {
      const chosen = answered[i];
      const right = chosen === q.answer;
      const item = document.createElement("div");
      item.className = `dmv-review-item console-card ${right ? "ok" : "no"}`;
      const chosenText = chosen === null ? "—" : q.options[chosen];
      item.innerHTML = `
        <div class="dmv-review-q"><span class="dmv-review-mark ${right ? "ok" : "no"}">${right ? "✓" : "✕"}</span> ${esc(q.q)}</div>
        ${right ? "" : `<div class="dmv-review-line no">Your answer: ${esc(chosenText)}</div>`}
        <div class="dmv-review-line ok">Correct: ${esc(q.options[q.answer])}</div>
        <div class="dmv-review-explain">${esc(q.explain)}</div>`;
      review.appendChild(item);
    });
  }

  // wire up
  stateSelect.addEventListener("change", renderStateInfo);
  el("dmv-start-btn").addEventListener("click", startQuiz);
  el("dmv-next-btn").addEventListener("click", next);
  el("dmv-retake-btn").addEventListener("click", () => show("start"));
  el("dmv-review-btn").addEventListener("click", () => {
    const review = el("dmv-review");
    const nowHidden = review.classList.toggle("hidden");
    el("dmv-review-btn").textContent = nowHidden ? "Review answers" : "Hide review";
    if (!nowHidden) review.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  loadStates();
})();
