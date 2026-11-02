/* ReproHub — 极少量原生 JS：主题切换、阅读操作、笔记保存 */
(function () {
  const root = document.documentElement;
  const saved = localStorage.getItem("rh-theme");
  if (saved) root.setAttribute("data-theme", saved);
  const tbtn = document.getElementById("theme-toggle");
  if (tbtn) {
    const label = () => (root.getAttribute("data-theme") === "dark" ? "浅色" : root.getAttribute("data-theme") === "light" ? "跟随系统" : "深色");
    tbtn.textContent = label();
    tbtn.addEventListener("click", () => {
      const cur = root.getAttribute("data-theme");
      const next = cur === "dark" ? "light" : cur === "light" ? "" : "dark";
      if (next) { root.setAttribute("data-theme", next); localStorage.setItem("rh-theme", next); }
      else { root.removeAttribute("data-theme"); localStorage.removeItem("rh-theme"); }
      tbtn.textContent = label();
    });
  }

  async function post(url, body) {
    const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    if (!r.ok) { const t = await r.text(); throw new Error(r.status + " " + t.slice(0, 200)); }
    return r.json();
  }

  function applyReading(itemId, reading) {
    document.querySelectorAll('[data-item="' + itemId + '"]').forEach((el) => {
      const field = el.getAttribute("data-field");
      const val = el.getAttribute("data-value");
      if (!field) return;
      let on = false;
      if (field === "status") on = reading.status === val;
      else if (field === "starred") on = !!reading.starred;
      else if (field === "read_later") on = !!reading.read_later;
      else if (field === "rating") on = String(reading.rating || "") === val;
      el.classList.toggle("on", on);
    });
    document.querySelectorAll('article[data-item-id="' + itemId + '"]').forEach((a) => a.classList.toggle("read", reading.status === "read"));
  }

  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-item][data-field]");
    if (!btn) return;
    ev.preventDefault();
    const id = btn.getAttribute("data-item");
    const field = btn.getAttribute("data-field");
    let value = btn.getAttribute("data-value");
    const body = {};
    if (field === "status") body.status = btn.classList.contains("on") ? "unread" : value;
    else if (field === "starred" || field === "read_later") body[field] = !btn.classList.contains("on");
    else if (field === "rating") body.rating = btn.classList.contains("on") ? 0 : parseInt(value, 10);
    else return;
    try {
      const reading = await post("/api/v1/reading/" + id, body);
      applyReading(id, reading);
    } catch (e) { alert("操作失败：" + e.message + "\n（非本机访问需先登录 /login）"); }
  });

  const notesForm = document.getElementById("notes-form");
  if (notesForm) {
    notesForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const id = notesForm.getAttribute("data-item");
      const notes = notesForm.querySelector("textarea[name=notes]").value;
      const tags = notesForm.querySelector("input[name=tags]").value.split(/[,，;；\s]+/).filter(Boolean);
      const status = document.getElementById("notes-status");
      try {
        await post("/api/v1/reading/" + id, { notes, tags });
        status.textContent = "已保存";
        setTimeout(() => (status.textContent = ""), 2000);
      } catch (e) { status.textContent = "保存失败：" + e.message; }
    });
  }

  document.querySelectorAll("form[data-collection-add]").forEach((f) => {
    f.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const cid = f.querySelector("select[name=cid]").value;
      const id = f.getAttribute("data-item");
      if (!cid) return;
      try { await post("/api/v1/collections/" + cid + "/items/" + id, {}); f.querySelector(".ok").textContent = "已加入"; }
      catch (e) { alert("失败：" + e.message); }
    });
  });
})();
