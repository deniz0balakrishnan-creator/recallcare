// RecallCare dashboard helpers — no build step, no framework.
const RC = {
  toast(msg, ms = 2600) {
    const t = document.getElementById("toast");
    if (!t) return;
    t.textContent = msg; t.style.display = "block";
    clearTimeout(RC._tt); RC._tt = setTimeout(() => (t.style.display = "none"), ms);
  },
  async post(url, body = {}, { reload = true, btn = null, done = null } = {}) {
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || j.ok === false) { RC.toast("Error: " + (j.error || j.detail || r.status)); return null; }
      if (done) done(j);
      if (reload) setTimeout(() => location.reload(), 250);
      return j;
    } catch (e) { RC.toast("Network error"); return null; }
    finally { if (btn) btn.disabled = false; }
  },
  setChannel(name, btn) {
    if (name === "whatsapp" && !confirm("Switch to the REAL WhatsApp channel? Messages will go to allowlisted phones only.")) return;
    RC.post("/api/channel", { channel: name }, { btn });
  },
  resetDemo(btn) {
    if (!confirm("Reset the synthetic clinic and replay the demo storyline? All current demo data is replaced.")) return;
    RC.toast("Resetting demo…", 6000);
    RC.post("/api/demo/reset", {}, { btn });
  },
  store(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } },
  toggleDrawer(force) {
    const d = document.getElementById("drawer");
    if (!d) return;
    const open = force !== undefined ? force : !d.classList.contains("open");
    d.classList.toggle("open", open);
    document.body.classList.toggle("drawer-open", open);
    const f = d.querySelector("iframe");
    if (open && f && !f.src.includes("/simulator")) f.src = f.dataset.src;
    RC.store("rc_drawer", open ? "1" : "0");
  },
  toggleGloss(el) {
    const on = el.checked;
    document.querySelectorAll(".glossable").forEach((n) => n.classList.toggle("show-gloss", on));
    RC.store("rc_gloss", on ? "1" : "0");
  },
};
document.addEventListener("DOMContentLoaded", () => {
  if (RC.store("rc_drawer") === "1") RC.toggleDrawer(true);
  const g = document.getElementById("gloss-toggle");
  if (g) { if (RC.store("rc_gloss") === "0") g.checked = false; RC.toggleGloss(g); }
  document.querySelectorAll(".chat").forEach((c) => (c.scrollTop = c.scrollHeight));
  // Keep the Today page fresh (new escalations, held drafts), but never pull it out from under someone:
  // no reload while the patient phone is open (its typing happens inside an iframe), while a field has
  // focus, or within 20 s of any mouse, scroll or key activity.
  if (document.body.dataset.autorefresh) {
    let lastActivity = Date.now();
    ["mousemove", "scroll", "keydown", "pointerdown", "wheel", "touchstart"].forEach((ev) =>
      window.addEventListener(ev, () => { lastActivity = Date.now(); }, { passive: true }));
    setInterval(() => {
      const a = document.activeElement;
      const busy = document.body.classList.contains("drawer-open") ||
        (a && ["INPUT", "TEXTAREA", "SELECT", "IFRAME"].includes(a.tagName)) || Date.now() - lastActivity < 20000;
      if (!busy) location.reload();
    }, 20000);
  }
});
