// Ops Tracker front end. State lives in one object; the server is always the source of truth.
const state = { me: null, filter: "", view: "", cursor: null, selectedId: null, selected: null };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const label = (s) => String(s).replace(/_/g, " ");

async function api(path, opts = {}) {
  const res = await fetch(`/api${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "/login") { showLogin(); throw new Error("Please sign in."); }
  return { ok: res.ok, status: res.status, data };
}

// ---------------------------------------------------------------- auth
function showLogin() { $("login").hidden = false; $("app").hidden = true; }
async function start() {
  const r = await api("/me").catch(() => null);
  if (!r || !r.ok) return showLogin();
  state.me = r.data;
  $("login").hidden = true; $("app").hidden = false;
  $("who").textContent = `${r.data.name}${r.data.is_admin ? " (admin)" : ""}`;
  $("unread").textContent = r.data.unread;
  const teamOpts = r.data.teams.map((t) => `<option value="${t.id}">${esc(t.name)} (${t.role})</option>`).join("");
  document.querySelector('#filters select[name="team"]').innerHTML = `<option value="">All my teams</option>${teamOpts}`;
  $("n-team").innerHTML = r.data.teams.filter((t) => t.role !== "viewer").map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("");
  $("new-btn").disabled = !$("n-team").options.length && !r.data.is_admin;
  refresh();
}
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const r = await api("/login", { method: "POST", body: { email: $("email").value, password: $("password").value } });
  $("login-error").hidden = r.ok; $("login-error").textContent = r.data.error || "";
  if (r.ok) start();
});
$("logout-btn").addEventListener("click", async () => { await api("/logout", { method: "POST" }); location.reload(); });

// ---------------------------------------------------------------- list
async function loadSummary() {
  const r = await api("/summary");
  if (!r.ok) return;
  $("t-mine").textContent = r.data.mine ?? 0; $("t-unassigned").textContent = r.data.unassigned ?? 0;
  $("t-urgent").textContent = r.data.urgent ?? 0; $("t-approve").textContent = r.data.to_approve ?? 0;
}
function query(extra = "") {
  const form = new FormData($("filters"));
  const params = new URLSearchParams();
  for (const [k, v] of form) if (v) params.set(k, v);
  new URLSearchParams(state.filter).forEach((v, k) => params.set(k, v));
  if (state.view) params.set("view", state.view);
  if (extra) params.set("cursor", extra);
  return params.toString();
}
async function loadList(append = false) {
  const r = await api(`/items?${query(append ? state.cursor : "")}`);
  if (!r.ok) return;
  state.cursor = r.data.next_cursor;
  const html = r.data.items.map((i) => `<tr class="row ${i.id === state.selectedId ? "sel" : ""}" data-id="${i.id}">
      <td>${i.id}</td><td>${esc(i.title)}</td><td>${esc(i.team_name)}</td>
      <td><span class="pill p-${i.priority}">${i.priority}</span></td>
      <td><span class="pill s-${i.status}">${label(i.status)}</span></td>
      <td>${i.assignee_name ? esc(i.assignee_name) : '<span class="muted">unassigned</span>'}</td></tr>`).join("");
  if (append) $("rows").insertAdjacentHTML("beforeend", html); else $("rows").innerHTML = html;
  $("more").hidden = !state.cursor;
  $("list-empty").hidden = $("rows").children.length > 0;
}
function refresh() { loadSummary(); loadList(); if (state.selectedId) loadDetail(state.selectedId, true); }
$("filters").addEventListener("submit", (e) => { e.preventDefault(); state.filter = ""; state.view = ""; loadList(); });
$("more").addEventListener("click", () => loadList(true));
document.querySelectorAll(".tile").forEach((b) => b.addEventListener("click", () => {
  $("filters").reset();
  state.view = b.dataset.view || ""; state.filter = b.dataset.filter || "";
  loadList();
}));
$("rows").addEventListener("click", (e) => { const tr = e.target.closest("tr.row"); if (tr) loadDetail(Number(tr.dataset.id)); });

// ---------------------------------------------------------------- detail
async function loadDetail(id, quiet = false) {
  const [item, hist] = await Promise.all([api(`/items/${id}`), api(`/items/${id}/history`)]);
  if (!item.ok) { if (!quiet) $("detail").innerHTML = `<div class="banner err">${esc(item.data.error)}</div>`; return; }
  // If someone else changed the item while we were looking at it, say so instead of silently swapping it.
  const changedByOthers = quiet && state.selected && state.selected.id === id && state.selected.version !== item.data.version;
  state.selectedId = id; state.selected = item.data;
  document.querySelectorAll("tr.row").forEach((tr) => tr.classList.toggle("sel", Number(tr.dataset.id) === id));
  renderDetail(hist.data, changedByOthers ? { ok: false, text: "This item was just changed by someone else. You are now seeing the latest version." } : null);
}

function actionsFor(i) {
  const me = state.me, team = me.teams.find((t) => t.id === i.team_id);
  const role = me.is_admin ? "lead" : team?.role;
  const mine = i.assignee_id === me.id, lead = role === "lead", member = role === "member" || lead;
  const a = [];
  if (i.status === "open" && !i.assignee_id && member) a.push(["claim", "Take it"]);
  if (i.status === "in_progress" && (mine || lead)) a.push(i.requires_approval ? ["awaiting_approval", "Request approval"] : ["resolved", "Mark resolved"]);
  if (i.status === "awaiting_approval" && lead && !mine) a.push(["approved", "Approve"], ["in_progress", "Send back"]);
  if (i.status === "approved" && (mine || lead)) a.push(["resolved", "Mark resolved"]);
  if (i.status === "resolved" && lead) a.push(["closed", "Close"]);
  if (i.status === "resolved" && (mine || lead)) a.push(["in_progress", "Reopen"]);
  if (i.status === "closed" && lead) a.push(["open", "Reopen"]);
  return { a, lead, member };
}

function renderDetail(hist, banner) {
  const i = state.selected, { a, lead, member } = actionsFor(i);
  const events = (hist.events || []).map((e) => {
    const d = e.detail, parts = [];
    if (e.type === "updated") for (const [k, v] of Object.entries(d)) parts.push(v && typeof v === "object" ? `${k}: ${esc(label(v.from ?? "none"))} → ${esc(label(v.to ?? "none"))}` : `${k} edited`);
    return `<li><span class="muted">${new Date(e.created_at).toLocaleString()}</span> · <b>${esc(e.actor)}</b> ${esc(e.type)} ${parts.join(", ")}</li>`;
  }).join("");
  const comments = (hist.comments || []).map((c) => `<li><b>${esc(c.author)}</b> <span class="muted">${new Date(c.created_at).toLocaleString()}</span><br>${esc(c.body)}</li>`).join("");
  $("detail").innerHTML = `
    ${banner ? `<div class="banner ${banner.ok ? "ok" : "err"}">${esc(banner.text)}</div>` : ""}
    <h2 style="margin-top:0">#${i.id} ${esc(i.title)}</h2>
    <p><span class="pill p-${i.priority}">${i.priority}</span> <span class="pill s-${i.status}">${label(i.status)}</span>
       <span class="pill">${label(i.kind)}</span> ${i.requires_approval ? '<span class="pill">needs lead approval</span>' : ""}</p>
    <p class="muted">${esc(i.team_name)} · owner: ${esc(i.assignee_name || "unassigned")} · created by ${esc(i.created_by_name)} · version ${i.version}</p>
    <p>${esc(i.description) || '<span class="muted">No description.</span>'}</p>
    <div class="actions">${a.map(([s, t]) => `<button class="primary" data-act="${s}">${t}</button>`).join("")}
      ${lead ? `<select id="prio" aria-label="Change priority">${["low", "medium", "high", "urgent"].map((p) => `<option ${p === i.priority ? "selected" : ""}>${p}</option>`).join("")}</select>` : ""}</div>
    <h3>Comments</h3><ul class="history">${comments || '<li class="muted">No comments yet.</li>'}</ul>
    ${member ? `<form id="comment-form"><textarea id="comment" rows="2" style="width:100%" placeholder="Add a comment" required></textarea><p><button>Comment</button></p></form>` : ""}
    <h3>History</h3><ul class="history">${events}</ul>`;
  $("detail").querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", () => act(b.dataset.act, b)));
  $("prio")?.addEventListener("change", (e) => update({ priority: e.target.value }));
  $("comment-form")?.addEventListener("submit", addComment);
}

async function act(action, button) {
  button.disabled = true;  // stops accidental double clicks; the server is also safe if one gets through
  if (action === "claim") {
    // Optimistic update: show it as ours straight away, then reconcile with what the server decided.
    const before = { ...state.selected };
    state.selected = { ...before, assignee_id: state.me.id, assignee_name: state.me.name, status: "in_progress" };
    renderDetail({ events: [], comments: [] }, { ok: true, text: "Taking this item…" });
    const r = await api(`/items/${before.id}/claim`, { method: "POST" });
    if (!r.ok) { state.selected = before; }
    await loadDetail(before.id);
    if (!r.ok) showBanner(r.data.error, false); else showBanner("It's yours now.", true);
    return refreshLists();
  }
  return update({ status: action });
}

async function update(changes) {
  const i = state.selected;
  const r = await api(`/items/${i.id}`, { method: "PATCH", headers: { "If-Match": `"${i.version}"` }, body: changes });
  await loadDetail(i.id);
  showBanner(r.ok ? "Saved." : r.data.error, r.ok);
  refreshLists();
}

async function addComment(e) {
  e.preventDefault();
  const body = $("comment").value.trim();
  if (!body) return;
  const key = crypto.randomUUID();  // same key on a retry, so the comment is never posted twice
  const send = () => api(`/items/${state.selectedId}/comments`, { method: "POST", headers: { "Idempotency-Key": key }, body: { body } });
  let r = await send().catch(() => null);
  if (!r) r = await send();  // one automatic retry after a network error
  await loadDetail(state.selectedId);
  if (!r.ok) showBanner(r.data.error, false);
}

function showBanner(text, ok) {
  const div = document.createElement("div");
  div.className = `banner ${ok ? "ok" : "err"}`; div.textContent = text;
  $("detail").prepend(div);
}
function refreshLists() { loadSummary(); loadList(); }

// ---------------------------------------------------------------- create
$("new-btn").addEventListener("click", () => { $("new-error").hidden = true; $("new-dialog").showModal(); });
let createKey = null;
$("new-form").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "create") return;
  e.preventDefault();
  createKey = createKey || crypto.randomUUID();
  $("create-btn").disabled = true;
  const r = await api("/items", { method: "POST", headers: { "Idempotency-Key": createKey }, body: {
    title: $("n-title").value, team_id: Number($("n-team").value), kind: $("n-kind").value,
    priority: $("n-priority").value, description: $("n-desc").value } }).catch(() => null);
  $("create-btn").disabled = false;
  if (!r || !r.ok) { $("new-error").hidden = false; $("new-error").textContent = r ? r.data.error : "Network problem. Press Create again; it won't be created twice."; return; }
  createKey = null; $("new-form").reset(); $("new-dialog").close();
  refreshLists(); loadDetail(r.data.id);
});

// ---------------------------------------------------------------- notifications and polling
$("notif-btn").addEventListener("click", async () => {
  const r = await api("/notifications");
  $("notif-list").innerHTML = r.data.map((n) => `<li>${n.read ? "" : "<b>•</b> "}${esc(n.message)} <span class="muted">#${n.item_id}</span></li>`).join("") || "<li class='muted'>Nothing yet.</li>";
  $("notif-dialog").showModal();
  await api("/notifications/read", { method: "POST" }); $("unread").textContent = 0;
});
setInterval(async () => {
  if ($("app").hidden || document.hidden) return;
  refresh();
  const me = await api("/me"); if (me.ok) $("unread").textContent = me.data.unread;
}, 10000);

start();
