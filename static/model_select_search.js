// Search lives inside each model picker; the native select remains the form value.
(() => {
  const ids = ["model-select", "settings-default-model", "tests-model", "tests-history",
    "agent-model", "agent-edit-model", "agent-resume-model"];
  for (const id of ids) {
    const select = document.getElementById(id);
    if (!select) continue;
    const picker = document.createElement("div");
    picker.className = "model-picker";
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "model-picker-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-label", select.getAttribute("aria-label") || "Choose model");
    const menu = document.createElement("div");
    menu.className = "model-picker-menu";
    menu.hidden = true;
    const search = document.createElement("input");
    search.type = "search";
    search.className = "model-picker-search";
    search.placeholder = "Search models…";
    search.setAttribute("aria-label", "Search models");
    search.autocomplete = "off";
    const list = document.createElement("div");
    list.className = "model-picker-list";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Models");
    menu.append(search, list);
    picker.append(trigger, menu);
    select.before(picker);
    picker.append(select);
    select.classList.add("model-picker-native");

    function sync() {
      trigger.textContent = select.selectedOptions[0]?.textContent || "Choose model";
      trigger.disabled = select.disabled;
    }
    function close() {
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      search.value = "";
    }
    function render() {
      const query = search.value.trim().toLocaleLowerCase();
      list.replaceChildren();
      let count = 0;
      for (const option of select.options) {
        if (option.hidden || option.disabled || !option.textContent.toLocaleLowerCase().includes(query)) continue;
        const item = document.createElement("button");
        item.type = "button";
        item.className = "model-picker-option";
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", String(option.selected));
        item.textContent = option.textContent;
        item.addEventListener("click", () => {
          select.value = option.value;
          select.dispatchEvent(new Event("change", {bubbles: true}));
          sync();
          close();
          trigger.focus();
        });
        list.append(item);
        count++;
      }
      if (!count) {
        const empty = document.createElement("div");
        empty.className = "model-picker-empty";
        empty.textContent = "No matching models";
        list.append(empty);
      }
    }
    trigger.addEventListener("click", () => {
      if (!menu.hidden) { close(); return; }
      sync();
      render();
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      search.focus();
    });
    search.addEventListener("input", render);
    search.addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); list.querySelector(".model-picker-option")?.click(); }
    });
    picker.addEventListener("keydown", event => {
      if (event.key === "Escape") { close(); trigger.focus(); }
    });
    document.addEventListener("pointerdown", event => {
      if (!picker.contains(event.target)) close();
    });
    select.addEventListener("change", sync);
    new MutationObserver(() => { sync(); if (!menu.hidden) render(); })
      .observe(select, {childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["disabled"]});
    sync();
  }
})();
