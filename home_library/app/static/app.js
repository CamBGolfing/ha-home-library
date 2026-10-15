/* Home Library front end. Plain JS, hash routing, relative URLs so it works under HA ingress. */
"use strict";

// ------------------------------------------------------------------ utilities

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const view = $("#view");

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const store = {
  get(key, fallback) { try { const v = localStorage.getItem("hl." + key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem("hl." + key, JSON.stringify(value)); } catch { /* storage unavailable */ } },
};

// The signed-in person: { user, via, setup_needed, is_addon }
let ME = { user: null };
const role = () => ME.user?.role || "";
const isEditor = () => ["editor", "owner"].includes(role());
const isOwner = () => role() === "owner";

async function request(method, url, body) {
  const opts = { method, headers: { "X-Library-Request": "1" }, credentials: "same-origin" };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const res = await fetch(url, opts);
  if (res.status === 401 && !url.startsWith("api/auth/")) {
    // Session ended (signed out elsewhere, account disabled). Back to sign-in.
    ME = { user: null, setup_needed: false };
    renderSignIn();
    throw new Error("Sign in to continue");
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join(", ");
    throw new Error(msg || `Request failed (${res.status})`);
  }
  return data;
}
const api = {
  get: (u) => request("GET", u),
  post: (u, b) => request("POST", u, b ?? {}),
  patch: (u, b) => request("PATCH", u, b),
  put: (u, b) => request("PUT", u, b),
  del: (u) => request("DELETE", u),
};

function toast(message, { error = false, action, onAction, ms = 3800 } = {}) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.innerHTML = `<span>${esc(message)}</span>`;
  if (action) {
    const b = document.createElement("button");
    b.textContent = action;
    b.onclick = () => { el.remove(); onAction(); };
    el.append(b);
  }
  $("#toasts").append(el);
  setTimeout(() => el.remove(), ms);
}
const fail = (e) => toast(e.message || String(e), { error: true });

function hue(text) {
  let h = 0;
  for (const c of String(text)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

function coverHTML(book, { remote } = {}) {
  const urls = remote ? (book.cover_urls || []) : (book.cover_url ? [book.cover_url] : []);
  const cloth = `<div class="cover cloth" style="--h:${hue(book.title || "")}"><span class="t">${esc(book.title || "Untitled")}</span>${book.authors ? `<span class="a">${esc(book.authors.split(",")[0])}</span>` : ""}</div>`;
  if (!urls.length) return cloth;
  return `<div class="cover"><img src="${esc(urls[0])}" alt="" loading="lazy" data-fallbacks="${esc(JSON.stringify(urls.slice(1)))}" data-cloth="${esc(cloth)}" onerror="coverFallback(this)"></div>`;
}
window.coverFallback = (img) => {
  const rest = JSON.parse(img.dataset.fallbacks || "[]");
  if (rest.length) { img.dataset.fallbacks = JSON.stringify(rest.slice(1)); img.src = rest[0]; return; }
  img.parentElement.outerHTML = img.dataset.cloth;
};

const localISO = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const today = () => localISO(new Date());
const plusDays = (n) => { const d = new Date(); d.setDate(d.getDate() + n); return localISO(d); };
function fmtDate(d) {
  if (!d) return "";
  const dt = new Date(d.length === 10 ? d + "T12:00:00" : d.replace(" ", "T") + "Z");
  return dt.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const loading = () => { view.innerHTML = `<div class="loading"><div class="spinner" aria-label="Loading"></div></div>`; };
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

// Shared lookups used across views
const cache = { shelves: [], tags: [] };
async function refreshMeta() {
  [cache.shelves, cache.tags] = await Promise.all([api.get("api/shelves"), api.get("api/tags")]);
}
const shelfLabel = (s) => s ? (s.room ? `${s.name} (${s.room})` : s.name) : "Not shelved";
const shelfOptions = (selected, { none = "Not shelved" } = {}) =>
  `<option value="">${esc(none)}</option>` + cache.shelves.map((s) =>
    `<option value="${s.id}" ${String(s.id) === String(selected ?? "") ? "selected" : ""}>${esc(shelfLabel(s))}</option>`).join("");
const tagDatalist = () => `<datalist id="tagList">${cache.tags.map((t) => `<option value="${esc(t.name)}">`).join("")}</datalist>`;

async function updateBadge() {
  if (!ME.user) return;
  try {
    const s = await api.get("api/stats");
    const badge = $("#loanBadge");
    // Editors: pending borrow requests come first, since they need an answer.
    const asks = isEditor() ? s.requests : 0;
    const n = asks || s.loaned;
    badge.hidden = !n;
    badge.textContent = n;
    badge.classList.toggle("overdue", !asks && s.overdue > 0);
    badge.classList.toggle("ask", asks > 0);
    badge.title = asks ? `${plural(asks, "borrow request")} waiting` : s.overdue ? `${s.overdue} overdue` : `${s.loaned} on loan`;
  } catch { /* ignore */ }
}

// Modal form helper: resolves with FormData, or null if cancelled.
function modalForm(title, fieldsHTML, submitLabel = "Save") {
  const dlg = $("#modal");
  dlg.innerHTML = `<form method="dialog"><h2>${esc(title)}</h2>${fieldsHTML}
    <div class="btn-row"><button class="btn btn-quiet" value="cancel" formnovalidate>Cancel</button>
    <button class="btn btn-primary" value="ok">${esc(submitLabel)}</button></div></form>`;
  return new Promise((resolve) => {
    const form = $("form", dlg);
    form.addEventListener("submit", (e) => {
      const ok = e.submitter?.value === "ok";
      resolve(ok ? new FormData(form) : null);
    }, { once: true });
    dlg.addEventListener("cancel", () => resolve(null), { once: true });
    dlg.showModal();
    $("input, select, textarea", form)?.focus();
  });
}
async function confirmDialog(title, message, label = "Delete") {
  const fd = await modalForm(title, `<p>${esc(message)}</p>`, label);
  return fd !== null;
}

// ------------------------------------------------------------------ router

let cleanup = null;
let draft = null; // prefill for the new-book form

// [pattern, handler, minimum role]
const routes = [
  [/^\/?$/, renderLibrary, "viewer"],
  [/^\/book\/(\d+)$/, (m) => renderBookById(+m[1]), "viewer"],
  [/^\/book\/(\d+)\/edit$/, (m) => renderEdit(+m[1]), "editor"],
  [/^\/new$/, () => renderEdit(null), "editor"],
  [/^\/scan$/, renderScan, "editor"],
  [/^\/add$/, renderAdd, "editor"],
  [/^\/loans$/, renderLoans, "viewer"],
  [/^\/organize$/, renderOrganize, "editor"],
  [/^\/account$/, renderAccount, "viewer"],
];
const RANK = { viewer: 0, editor: 1, owner: 2 };

async function route() {
  if (cleanup) { try { await cleanup(); } catch { /* ignore */ } cleanup = null; }
  const path = location.hash.replace(/^#/, "") || "/";
  const [pathname, query = ""] = path.split("?");
  const params = Object.fromEntries(new URLSearchParams(query));
  const invite = pathname.match(/^\/invite\/([\w-]+)$/);
  if (invite) { await renderInvite(invite[1]); return; }
  if (!ME.user) { renderSignIn(); return; }
  document.body.classList.remove("signed-out");
  updateBadge();
  const section = pathname.split("/")[1] || "library";
  const navKey = { book: "library", new: "library", add: "library", "": "library" }[section] ?? section;
  $$("[data-nav]").forEach((a) => { if (a.dataset.nav !== navKey) a.removeAttribute("aria-current"); else a.setAttribute("aria-current", "page"); });
  for (const [re, fn, minRole] of routes) {
    const m = pathname.match(re);
    if (m && RANK[role()] < RANK[minRole]) { location.hash = "#/"; return; }
    if (m) {
      try { await fn(m, params); } catch (e) { view.innerHTML = `<div class="empty"><h2>Something went wrong</h2><p>${esc(e.message)}</p><a class="btn" href="#/">Back to the library</a></div>`; }
      view.focus({ preventScroll: true });
      window.scrollTo(0, 0);
      return;
    }
  }
  location.hash = "#/";
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ library

async function renderLibrary(_m, params) {
  await refreshMeta();
  const f = { q: "", status: "owned", shelf: "", tag: "", sort: "added", ...store.get("filters", {}) };
  if (params.shelf !== undefined) { Object.assign(f, { shelf: params.shelf, tag: "", q: "", status: "owned" }); }
  if (params.tag !== undefined) { Object.assign(f, { tag: params.tag, shelf: "", q: "", status: "" }); }

  view.innerHTML = `
    <div class="page-head">
      <div><h1>Library</h1><p class="stat-line" id="statLine">&nbsp;</p></div>
      ${isEditor() ? `<div class="btn-row"><a class="btn btn-primary hide-mobile" href="#/scan">Scan books</a><a class="btn" href="#/add">Add by title</a></div>` : ""}
    </div>
    <label class="search">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></svg>
      <input type="search" id="q" placeholder="Search titles, authors, series, ISBN, tags" value="${esc(f.q)}" autocomplete="off" aria-label="Search the library">
    </label>
    <div class="filters">
      <button class="chip" data-status="owned">Owned</button>
      <button class="chip" data-status="wishlist">Wishlist</button>
      <button class="chip" data-status="loaned">${isEditor() ? "On loan" : "Checked out"}</button>
      <button class="chip" data-status="">Everything</button>
      <select id="fShelf" aria-label="Shelf"><option value="">All shelves</option><option value="0">Not shelved</option>
        ${cache.shelves.map((s) => `<option value="${s.id}">${esc(shelfLabel(s))}</option>`).join("")}</select>
      <select id="fTag" aria-label="Tag"><option value="">All tags</option>
        ${cache.tags.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>
      <select id="fSort" aria-label="Sort">
        <option value="added">Recently added</option><option value="title">Title</option>
        <option value="author">Author</option><option value="series">Series</option><option value="published">Newest published</option>
      </select>
    </div>
    <div id="results"></div>`;

  $("#fShelf").value = f.shelf; $("#fTag").value = f.tag; $("#fSort").value = f.sort;
  const syncChips = () => $$(".chip[data-status]").forEach((c) => c.setAttribute("aria-pressed", c.dataset.status === f.status));
  syncChips();

  const load = async () => {
    store.set("filters", f);
    const qs = new URLSearchParams({ sort: f.sort });
    if (f.q) qs.set("q", f.q);
    if (f.shelf !== "") qs.set("shelf", f.shelf);
    if (f.tag) qs.set("tag", f.tag);
    if (f.status === "loaned") qs.set("loaned", "true");
    else if (f.status) qs.set("status", f.status);
    const { total, books } = await api.get("api/books?" + qs);
    const results = $("#results");
    if (!results) return;
    if (!books.length) {
      const filtered = f.q || f.shelf !== "" || f.tag || f.status !== "owned";
      results.innerHTML = filtered
        ? `<div class="empty"><h2>No matches</h2><p>Nothing here fits those filters. Try a different search or clear the filters.</p><button class="btn" id="clearF">Clear filters</button></div>`
        : isEditor()
          ? `<div class="empty"><h2>Your shelves are empty</h2><p>Scan the barcode on the back of a book and it'll show up here with its cover.</p><div class="btn-row"><a class="btn btn-primary" href="#/scan">Scan your first book</a><a class="btn" href="#/add">Add without a barcode</a></div></div>`
          : `<div class="empty"><h2>No books yet</h2><p>Nothing has been cataloged so far. Check back soon.</p></div>`;
      $("#clearF")?.addEventListener("click", () => { Object.assign(f, { q: "", status: "owned", shelf: "", tag: "" }); $("#q").value = ""; $("#fShelf").value = ""; $("#fTag").value = ""; syncChips(); load(); });
      return;
    }
    results.innerHTML = `<p class="muted small" style="margin-bottom:14px">${total === books.length ? plural(total, "book") : `Showing ${books.length} of ${total}`}</p>
      <div class="grid">${books.map(cardHTML).join("")}</div>`;
  };

  const loadStats = async () => {
    const s = await api.get("api/stats");
    const bits = [`<strong>${s.books}</strong> ${s.books === 1 ? "book" : "books"}`];
    if (s.shelves) bits.push(`on ${plural(s.shelves, "shelf").replace("shelfs", "shelves")}`);
    let line = bits.join(" ");
    if (isEditor() && s.loaned) line += `, ${s.loaned} on loan`;
    if (!isEditor() && s.loaned) line += `, ${s.loaned} checked out to you`;
    if (s.wishlist) line += `, ${s.wishlist} on the wishlist`;
    $("#statLine").innerHTML = line;
  };

  $("#q").addEventListener("input", debounce((e) => { f.q = e.target.value.trim(); load().catch(fail); }, 220));
  $$(".chip[data-status]").forEach((c) => c.addEventListener("click", () => { f.status = c.dataset.status; syncChips(); load().catch(fail); }));
  $("#fShelf").addEventListener("change", (e) => { f.shelf = e.target.value; load().catch(fail); });
  $("#fTag").addEventListener("change", (e) => { f.tag = e.target.value; load().catch(fail); });
  $("#fSort").addEventListener("change", (e) => { f.sort = e.target.value; load().catch(fail); });

  await Promise.all([load(), loadStats()]);
}

function cardHTML(b) {
  let flag = "";
  const loan = b.active_loan;
  if (loan && !isEditor()) {
    const overdue = loan.mine && loan.due_at && loan.due_at < today();
    flag = `<span class="flag ${overdue ? "overdue" : ""}">${loan.mine ? (overdue ? "Overdue, you have it" : "You have it") : "Checked out"}</span>`;
  } else if (loan) {
    const overdue = loan.due_at && loan.due_at < today();
    flag = `<span class="flag ${overdue ? "overdue" : ""}">${overdue ? "Overdue" : "With"} ${esc(loan.borrower)}</span>`;
  } else if (b.status === "wishlist") flag = `<span class="flag">Wishlist</span>`;
  return `<a class="card" href="#/book/${b.id}">${coverHTML(b)}
    <span class="card-title">${esc(b.title)}</span>
    ${b.authors ? `<span class="card-author">${esc(b.authors)}</span>` : ""}${flag}</a>`;
}

// ------------------------------------------------------------------ book detail

async function renderBookById(id) {
  loading();
  await refreshMeta();
  const b = await api.get(`api/books/${id}`);
  if (!isEditor()) { renderBookForViewer(b); return; }
  const loan = b.active_loan;
  const overdue = loan?.due_at && loan.due_at < today();
  const facts = [
    ["Published", [b.publisher, b.published].filter(Boolean).join(", ")],
    ["Pages", b.pages], ["Format", b.format], ["Language", b.language],
    ["ISBN", [b.isbn13, b.isbn10].filter(Boolean).join(" / ")],
    ["Subjects", b.subjects], ["Copies", b.copies > 1 ? b.copies : ""],
    ["Added", [fmtDate(b.created_at), b.added_by_name && `by ${b.added_by_name}`].filter(Boolean).join(" ")], ["Data from", b.source],
  ].filter(([, v]) => v);

  view.innerHTML = `
    <p style="margin-bottom:16px"><a href="#/" class="btn btn-quiet btn-sm" style="padding-left:0">&larr; Library</a></p>
    <div class="detail">
      <div>
        <div class="detail-cover">${coverHTML(b)}</div>
        <div class="cover-actions">
          <label class="btn btn-sm">Use a photo<input type="file" id="coverFile" accept="image/*" capture="environment" hidden></label>
          ${b.isbn13 ? `<button class="btn btn-sm" id="coverRefresh">Find cover online</button>` : ""}
        </div>
      </div>
      <div class="stack">
        <div class="detail-title">
          ${b.series ? `<p class="series-line">${esc(b.series)}${b.series_index != null ? `, book ${esc(+b.series_index)}` : ""}</p>` : ""}
          <h1>${esc(b.title)}</h1>
          ${b.subtitle ? `<p class="sub">${esc(b.subtitle)}</p>` : ""}
          ${b.authors ? `<p class="by">by ${esc(b.authors)}</p>` : ""}
          ${b.status === "wishlist" ? `<p class="notice" style="margin-top:12px">On the wishlist, not on a shelf yet. <button class="btn btn-sm btn-gilt" id="gotIt" style="margin-left:6px">We got it</button></p>` : ""}
        </div>

        ${b.status === "owned" ? `
        <div class="loan-box ${overdue ? "overdue" : ""}">
          ${loan
            ? `<div><strong>${overdue ? "Overdue" : "Loaned"} to ${esc(loan.borrower)}</strong><p class="small muted">Since ${fmtDate(loan.loaned_at)}${loan.due_at ? `, due ${fmtDate(loan.due_at)}` : ""}</p></div>
               <button class="btn btn-primary btn-sm" id="returnLoan">Mark returned</button>`
            : `<div><strong>On the shelf</strong><p class="small muted">Lend it out to keep track of who has it.</p></div>
               <button class="btn btn-sm" id="lend">Lend</button>`}
        </div>` : ""}
        ${(b.requests || []).map((r) => `
        <div class="loan-box ask">
          <div><strong>${esc(r.display_name)} asked to borrow this</strong>
            <p class="small muted">${fmtDate(r.created_at)}${r.message ? `: "${esc(r.message)}"` : ""}</p></div>
          <div class="btn-row">${loan ? "" : `<button class="btn btn-primary btn-sm" data-approve="${r.id}">Lend it</button>`}<button class="btn btn-sm" data-decline="${r.id}">Decline</button></div>
        </div>`).join("")}

        <div class="form-grid">
          <label class="field"><span>Shelf</span><select id="shelfSel">${shelfOptions(b.shelf_id)}</select></label>
          <div class="field"><span>Tags</span>
            <div class="tag-editor" id="tagEditor">
              ${b.tags.map((t) => `<span class="tag" style="--tag:${esc(t.color)}">${esc(t.name)}<button aria-label="Remove ${esc(t.name)}" data-remove="${esc(t.name)}">&times;</button></span>`).join("")}
              <input list="tagList" id="tagInput" placeholder="Add a tag" aria-label="Add a tag">${tagDatalist()}
            </div>
          </div>
        </div>

        ${b.description ? `<div><p class="description clamped" id="desc">${esc(b.description)}</p><button class="btn btn-quiet btn-sm" id="more" style="padding-left:0">Read more</button></div>` : ""}
        ${b.notes ? `<div class="panel"><h3 style="margin-bottom:6px">Notes</h3><p style="white-space:pre-line">${esc(b.notes)}</p></div>` : ""}

        <dl class="facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>

        ${b.loans.length ? `<div><h3 style="margin-bottom:6px">Loan history</h3><div class="list">${b.loans.map((l) => `
          <div class="row"><div class="grow"><p style="color:var(--ink)">${esc(l.borrower)}</p><p>${fmtDate(l.loaned_at)} to ${l.returned_at ? fmtDate(l.returned_at) : "now"}</p></div></div>`).join("")}</div></div>` : ""}

        <div class="btn-row">
          <a class="btn" href="#/book/${b.id}/edit">Edit details</a>
          <button class="btn btn-danger btn-quiet" id="del">Delete book</button>
        </div>
      </div>
    </div>`;

  const reload = () => renderBookById(id);
  const patch = (body) => api.patch(`api/books/${id}`, body);

  $("#shelfSel").addEventListener("change", async (e) => {
    try { await patch({ shelf_id: e.target.value ? +e.target.value : null }); toast("Shelf updated"); } catch (err) { fail(err); }
  });

  const currentTags = b.tags.map((t) => t.name);
  const saveTags = async (names) => { try { await patch({ tags: names }); await reload(); } catch (err) { fail(err); } };
  $("#tagInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      const v = e.target.value.trim().replace(/,$/, "");
      if (v && !currentTags.some((t) => t.toLowerCase() === v.toLowerCase())) saveTags([...currentTags, v]);
    }
  });
  $("#tagInput").addEventListener("change", (e) => {
    const v = e.target.value.trim();
    if (v && cache.tags.some((t) => t.name === v) && !currentTags.includes(v)) saveTags([...currentTags, v]);
  });
  $$("[data-remove]").forEach((btn) => btn.addEventListener("click", () => saveTags(currentTags.filter((t) => t !== btn.dataset.remove))));

  $("#more")?.addEventListener("click", (e) => { $("#desc").classList.remove("clamped"); e.target.remove(); });
  $("#gotIt")?.addEventListener("click", async () => { try { await patch({ status: "owned" }); toast("Moved to your library"); reload(); } catch (err) { fail(err); } });

  $("#lend")?.addEventListener("click", async () => {
    const people = await api.get("api/borrowers").catch(() => []);
    const fd = await modalForm(`Lend "${b.title}"`, `
      <label class="field"><span>Who's borrowing it?</span><input name="borrower" list="people" required autocomplete="off"></label>
      <datalist id="people">${people.map((p) => `<option value="${esc(p)}">`).join("")}</datalist>
      <div class="form-grid">
        <label class="field"><span>Lent on</span><input type="date" name="loaned_at" value="${today()}"></label>
        <label class="field"><span>Due back (optional)</span><input type="date" name="due_at" value="${plusDays(30)}"></label>
      </div>
      <label class="field"><span>Note (optional)</span><input name="notes"></label>`, "Lend book");
    if (!fd) return;
    try {
      await api.post(`api/books/${id}/loans`, { borrower: fd.get("borrower"), loaned_at: fd.get("loaned_at") || null, due_at: fd.get("due_at") || null, notes: fd.get("notes") || "" });
      toast(`Lent to ${fd.get("borrower")}`); updateBadge(); reload();
    } catch (err) { fail(err); }
  });
  $("#returnLoan")?.addEventListener("click", async () => {
    try { await api.post(`api/loans/${loan.id}/return`); toast("Marked returned"); updateBadge(); reload(); } catch (err) { fail(err); }
  });
  wireRequestButtons(reload);

  $("#coverFile").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", await shrinkImage(file), "cover.jpg");
    try { await request("POST", `api/books/${id}/cover`, fd); toast("Cover updated"); reload(); } catch (err) { fail(err); }
  });
  $("#coverRefresh")?.addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Looking...";
    try { await api.post(`api/books/${id}/cover/refresh`); toast("Found a cover"); reload(); } catch (err) { fail(err); e.target.disabled = false; e.target.textContent = "Find cover online"; }
  });

  $("#del").addEventListener("click", async () => {
    if (!(await confirmDialog("Delete this book?", `"${b.title}" and its loan history will be removed from the library.`))) return;
    try { await api.del(`api/books/${id}`); toast("Book deleted"); location.hash = "#/"; } catch (err) { fail(err); }
  });
}

function bookFacts(b) {
  return [
    ["Published", [b.publisher, b.published].filter(Boolean).join(", ")],
    ["Pages", b.pages], ["Format", b.format], ["Language", b.language],
    ["ISBN", [b.isbn13, b.isbn10].filter(Boolean).join(" / ")], ["Subjects", b.subjects],
    ["Shelf", b.shelf ? shelfLabel(b.shelf) : ""],
  ].filter(([, v]) => v);
}

function renderBookForViewer(b) {
  const loan = b.active_loan;
  const overdue = loan?.mine && loan.due_at && loan.due_at < today();
  const req = b.my_request;
  let status;
  if (b.status !== "owned") {
    status = `<p class="notice">On the wishlist. It isn't in the library yet.</p>`;
  } else if (loan?.mine) {
    status = `<div class="loan-box ${overdue ? "overdue" : ""}"><div><strong>${overdue ? "Overdue: you have this one" : "You have this one"}</strong>
      <p class="small muted">${loan.due_at ? `Due back ${fmtDate(loan.due_at)}` : "No due date"}</p></div></div>`;
  } else if (req) {
    status = `<div class="loan-box ask"><div><strong>You asked to borrow this</strong>
      <p class="small muted">Sent ${fmtDate(req.created_at)}. You'll see it under My books once it's lent to you.</p></div>
      <button class="btn btn-sm" id="cancelReq">Cancel request</button></div>`;
  } else {
    status = `<div class="loan-box"><div><strong>${loan ? "Checked out right now" : "On the shelf"}</strong>
      <p class="small muted">${loan ? "Ask anyway and you'll be next in line." : "Ask to borrow it and the owner will get your request."}</p></div>
      <button class="btn btn-primary btn-sm" id="askBorrow">Ask to borrow</button></div>`;
  }
  view.innerHTML = `
    <p style="margin-bottom:16px"><a href="#/" class="btn btn-quiet btn-sm" style="padding-left:0">&larr; Library</a></p>
    <div class="detail">
      <div><div class="detail-cover">${coverHTML(b)}</div></div>
      <div class="stack">
        <div class="detail-title">
          ${b.series ? `<p class="series-line">${esc(b.series)}${b.series_index != null ? `, book ${esc(+b.series_index)}` : ""}</p>` : ""}
          <h1>${esc(b.title)}</h1>
          ${b.subtitle ? `<p class="sub">${esc(b.subtitle)}</p>` : ""}
          ${b.authors ? `<p class="by">by ${esc(b.authors)}</p>` : ""}
        </div>
        ${status}
        ${b.tags.length ? `<div class="tag-editor">${b.tags.map((t) => `<a class="tag" style="--tag:${esc(t.color)};text-decoration:none" href="#/?tag=${t.id}">${esc(t.name)}</a>`).join("")}</div>` : ""}
        ${b.description ? `<div><p class="description clamped" id="desc">${esc(b.description)}</p><button class="btn btn-quiet btn-sm" id="more" style="padding-left:0">Read more</button></div>` : ""}
        <dl class="facts">${bookFacts(b).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      </div>
    </div>`;
  $("#more")?.addEventListener("click", (e) => { $("#desc").classList.remove("clamped"); e.target.remove(); });
  $("#askBorrow")?.addEventListener("click", async () => {
    const fd = await modalForm(`Borrow "${b.title}"`, `
      <p class="muted">The library owner will see your request and let you know.</p>
      <label class="field"><span>Message (optional)</span><input name="message" placeholder="e.g. I can pick it up Sunday" maxlength="500"></label>`, "Send request");
    if (!fd) return;
    try { await api.post(`api/books/${b.id}/requests`, { message: fd.get("message") || "" }); toast("Request sent"); updateBadge(); renderBookById(b.id); }
    catch (err) { fail(err); }
  });
  $("#cancelReq")?.addEventListener("click", async () => {
    try { await api.post(`api/requests/${req.id}/cancel`); toast("Request cancelled"); updateBadge(); renderBookById(b.id); } catch (err) { fail(err); }
  });
}

function wireRequestButtons(after) {
  $$("[data-approve]").forEach((btn) => btn.addEventListener("click", async () => {
    const fd = await modalForm("Lend this book", `
      <label class="field"><span>Due back (optional)</span><input type="date" name="due_at" value="${plusDays(30)}"></label>`, "Lend book");
    if (!fd) return;
    try { const loan = await api.post(`api/requests/${btn.dataset.approve}/approve`, { due_at: fd.get("due_at") || null }); toast(`Lent to ${loan.borrower}`); updateBadge(); after(); }
    catch (err) { fail(err); }
  }));
  $$("[data-decline]").forEach((btn) => btn.addEventListener("click", async () => {
    try { await api.post(`api/requests/${btn.dataset.decline}/decline`); toast("Request declined"); updateBadge(); after(); } catch (err) { fail(err); }
  }));
}

// Phone photos are huge; scale covers down before upload.
async function shrinkImage(file, max = 1000) {
  try {
    const bmp = await createImageBitmap(file);
    const scale = Math.min(1, max / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * scale); c.height = Math.round(bmp.height * scale);
    c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
    return await new Promise((res) => c.toBlob(res, "image/jpeg", 0.85));
  } catch { return file; }
}

// ------------------------------------------------------------------ edit / new

const FORMATS = ["", "Hardcover", "Paperback", "Mass market paperback", "Board book", "Picture book", "Graphic novel", "Ebook", "Audiobook", "Other"];

async function renderEdit(id) {
  loading();
  await refreshMeta();
  const isNew = id == null;
  const defaults = store.get("scanDefaults", {});
  const b = isNew
    ? { title: "", status: "owned", copies: 1, shelf_id: defaults.shelf || null, tags: [], ...(draft || {}) }
    : await api.get(`api/books/${id}`);
  const tagText = (b.tags || []).map((t) => (typeof t === "string" ? t : t.name)).join(", ");
  const val = (k) => esc(b[k] ?? "");

  view.innerHTML = `
    <div class="page-head"><div><h1>${isNew ? "Add a book" : "Edit details"}</h1>
      ${isNew && b.source ? `<p class="muted">Filled in from ${esc(b.source)}. Check it over, then save.</p>` : ""}</div></div>
    <form id="bookForm" class="detail">
      <div class="detail-cover">${coverHTML(b, { remote: isNew })}</div>
      <div class="stack">
        <div class="form-grid">
          <label class="field span-2"><span>Title</span><input name="title" value="${val("title")}" required></label>
          <label class="field span-2"><span>Subtitle</span><input name="subtitle" value="${val("subtitle")}"></label>
          <label class="field span-2"><span>Authors</span><input name="authors" value="${val("authors")}" placeholder="Separate several with commas"></label>
          <label class="field"><span>Series</span><input name="series" value="${val("series")}"></label>
          <label class="field"><span>Number in series</span><input type="number" step="0.5" name="series_index" value="${b.series_index ?? ""}"></label>
          <label class="field"><span>Shelf</span><select name="shelf_id">${shelfOptions(b.shelf_id)}</select></label>
          <label class="field"><span>Status</span><select name="status">
            <option value="owned" ${b.status === "owned" ? "selected" : ""}>In our library</option>
            <option value="wishlist" ${b.status === "wishlist" ? "selected" : ""}>Wishlist</option></select></label>
          <label class="field span-2"><span>Tags</span><input name="tags" value="${esc(tagText)}" placeholder="Kids, Favorites, Signed" list="tagList">${tagDatalist()}</label>
          <label class="field"><span>Format</span><select name="format">${FORMATS.map((x) => `<option ${x === (b.format || "") ? "selected" : ""} value="${esc(x)}">${esc(x || "Not set")}</option>`).join("")}</select></label>
          <label class="field"><span>Copies</span><input type="number" min="1" name="copies" value="${b.copies || 1}"></label>
          <label class="field"><span>Publisher</span><input name="publisher" value="${val("publisher")}"></label>
          <label class="field"><span>Published</span><input name="published" value="${val("published")}"></label>
          <label class="field"><span>Pages</span><input type="number" min="0" name="pages" value="${b.pages ?? ""}"></label>
          <label class="field"><span>Language</span><input name="language" value="${val("language")}"></label>
          <label class="field"><span>ISBN-13</span><input name="isbn13" value="${val("isbn13")}" inputmode="numeric"></label>
          <label class="field"><span>ISBN-10</span><input name="isbn10" value="${val("isbn10")}"></label>
          <label class="field span-2"><span>Subjects</span><input name="subjects" value="${val("subjects")}"></label>
          <label class="field span-2"><span>Description</span><textarea name="description" rows="5">${val("description")}</textarea></label>
          <label class="field span-2"><span>Notes</span><textarea name="notes" rows="3" placeholder="Signed, gift from Grandma, missing dust jacket...">${val("notes")}</textarea></label>
        </div>
        <div class="btn-row">
          <button class="btn btn-primary" type="submit">${isNew ? "Add to library" : "Save changes"}</button>
          <a class="btn btn-quiet" href="${isNew ? "#/add" : `#/book/${id}`}">Cancel</a>
        </div>
      </div>
    </form>`;

  $("#bookForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const num = (k) => (fd.get(k) === "" ? null : Number(fd.get(k)));
    const body = {};
    for (const k of ["title", "subtitle", "authors", "series", "status", "format", "publisher", "published", "language", "isbn13", "isbn10", "subjects", "description", "notes"]) body[k] = (fd.get(k) || "").trim();
    body.isbn13 = body.isbn13 || null; body.isbn10 = body.isbn10 || null;
    body.series_index = num("series_index"); body.pages = num("pages"); body.copies = num("copies") || 1;
    body.shelf_id = num("shelf_id");
    body.tags = (fd.get("tags") || "").split(",").map((t) => t.trim()).filter(Boolean);
    const btn = $("button[type=submit]", e.target);
    btn.disabled = true;
    try {
      let saved;
      if (isNew) {
        body.cover_urls = b.cover_urls || [];
        body.source = b.source || "";
        saved = await api.post("api/books", body);
        draft = null;
        toast(body.status === "wishlist" ? "Added to the wishlist" : "Added to the library");
      } else {
        saved = await api.patch(`api/books/${id}`, body);
        toast("Changes saved");
      }
      location.hash = `#/book/${saved.id}`;
    } catch (err) { fail(err); btn.disabled = false; }
  });
}

// ------------------------------------------------------------------ add (no barcode)

async function renderAdd() {
  view.innerHTML = `
    <div class="page-head"><div><h1>Add a book</h1><p class="muted">No barcode handy? Search by title or author, type an ISBN, or enter it by hand.</p></div></div>
    <div class="stack">
      <form class="panel" id="isbnForm">
        <h2>Type an ISBN</h2>
        <div class="inline-form nowrap"><input name="code" inputmode="numeric" placeholder="978..." aria-label="ISBN" autocomplete="off"><button class="btn btn-primary">Look up</button></div>
      </form>
      <form class="panel" id="searchForm">
        <h2>Search by title or author</h2>
        <div class="inline-form nowrap"><input name="q" placeholder="e.g. Where the Wild Things Are" aria-label="Title or author" autocomplete="off"><button class="btn btn-primary">Search</button></div>
        <div id="searchResults" class="list" style="margin-top:8px"></div>
      </form>
      <div class="panel"><h2>Enter it yourself</h2><p class="muted" style="margin-bottom:12px">For old, rare or homemade books that aren't in any online catalog.</p>
        <button class="btn" id="manual">Start a blank entry</button></div>
    </div>`;

  $("#manual").addEventListener("click", () => { draft = null; location.hash = "#/new"; });

  $("#isbnForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = new FormData(e.target).get("code").trim();
    if (!code) return;
    const btn = $("button", e.target); btn.disabled = true; btn.textContent = "Looking up...";
    try {
      const r = await api.get(`api/lookup/${encodeURIComponent(code)}`);
      if (r.owned?.length) {
        toast(`Already in your library: ${r.owned[0].title}`, { action: "Open", onAction: () => { location.hash = `#/book/${r.owned[0].id}`; }, ms: 7000 });
      }
      if (!r.found) { toast(r.error, { error: true }); draft = { isbn13: r.isbn13, isbn10: r.isbn10 }; if (r.kind !== "invalid") location.hash = "#/new"; return; }
      draft = lookupToDraft(r); location.hash = "#/new";
    } catch (err) { fail(err); } finally { btn.disabled = false; btn.textContent = "Look up"; }
  });

  $("#searchForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = new FormData(e.target).get("q").trim();
    if (q.length < 2) return;
    const out = $("#searchResults");
    out.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;
    try {
      const results = await api.get(`api/search?q=${encodeURIComponent(q)}`);
      if (!results.length) { out.innerHTML = `<p class="muted" style="padding:12px 0">No results. Try fewer words, or enter it yourself below.</p>`; return; }
      out.innerHTML = results.map((r, i) => `
        <div class="row"><div class="thumb">${coverHTML(r, { remote: true })}</div>
          <div class="grow"><a href="#" data-pick="${i}">${esc(r.title)}</a><p>${esc([r.authors, r.published].filter(Boolean).join(", "))}</p></div>
          <button class="btn btn-sm" type="button" data-pick="${i}">Choose</button></div>`).join("");
      $$("[data-pick]", out).forEach((el) => el.addEventListener("click", (ev) => {
        ev.preventDefault();
        draft = lookupToDraft(results[+el.dataset.pick]);
        location.hash = "#/new";
      }));
    } catch (err) { out.innerHTML = ""; fail(err); }
  });
}

function lookupToDraft(r) {
  const keep = ["title", "subtitle", "authors", "publisher", "published", "pages", "language", "description", "subjects", "isbn13", "isbn10", "cover_urls", "source"];
  return Object.fromEntries(keep.filter((k) => r[k] != null).map((k) => [k, r[k]]));
}

// ------------------------------------------------------------------ scan

const scanSession = { log: [] };

async function renderScan() {
  await refreshMeta();
  const defaults = { mode: "ask", shelf: "", tags: "", ...store.get("scanDefaults", {}) };
  const inFrame = window.self !== window.top;

  view.innerHTML = `
    <div class="page-head"><div><h1>Scan books</h1><p class="muted">Hold the barcode about 6 inches (15 cm) from the camera. It doesn't need to fill the box.</p></div>
      <div class="segmented" role="group" aria-label="When a book is scanned">
        <button type="button" data-mode="ask">Ask first</button>
        <button type="button" data-mode="instant">Add instantly</button>
      </div></div>
    <div class="scan-layout">
      <div class="stack">
        <div class="viewfinder" id="vf">
          <video id="camVideo" playsinline muted autoplay></video>
          <div class="reticle" id="reticle" hidden></div>
          <div class="vf-msg" id="vfMsg"><div class="spinner" style="margin:auto"></div><p>Starting the camera...</p></div>
        </div>
        <div class="cam-tools" id="camTools">
          <select id="camPick" aria-label="Camera" hidden></select>
          <button type="button" class="btn btn-sm" id="zoomBtn" hidden>Zoom 2x</button>
          <button type="button" class="btn btn-sm" id="torchBtn" aria-pressed="false" hidden>Light</button>
          <label class="btn btn-sm">Take a photo<input type="file" id="photoScan" accept="image/*" capture="environment" hidden></label>
        </div>
        <p class="small muted" id="camHint" hidden></p>
        <form id="manualCode" class="inline-form nowrap" style="margin-top:0">
          <input name="code" id="codeInput" inputmode="numeric" placeholder="Or type / use a USB scanner" aria-label="Barcode or ISBN" autocomplete="off">
          <button class="btn">Look up</button>
        </form>
      </div>
      <div class="stack">
        <div id="resultArea"></div>
        <div class="panel">
          <h3 style="margin-bottom:10px">Put scanned books on</h3>
          <div class="form-grid">
            <label class="field"><span>Shelf</span><select id="dShelf">${shelfOptions(defaults.shelf, { none: "No shelf yet" })}</select></label>
            <label class="field"><span>Tags</span><input id="dTags" value="${esc(defaults.tags)}" placeholder="e.g. Kids" list="tagList">${tagDatalist()}</label>
          </div>
        </div>
        <div class="panel session-log" id="sessionLog"></div>
      </div>
    </div>`;

  // Settings
  const saveDefaults = () => store.set("scanDefaults", defaults);
  const syncMode = () => $$("[data-mode]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.mode === defaults.mode));
  syncMode();
  $$("[data-mode]").forEach((b) => b.addEventListener("click", () => { defaults.mode = b.dataset.mode; syncMode(); saveDefaults(); }));
  $("#dShelf").addEventListener("change", (e) => { defaults.shelf = e.target.value; saveDefaults(); });
  $("#dTags").addEventListener("change", (e) => { defaults.tags = e.target.value; saveDefaults(); });
  const defaultTags = () => defaults.tags.split(",").map((t) => t.trim()).filter(Boolean);
  const defaultShelf = () => (defaults.shelf ? +defaults.shelf : null);

  // Session log
  const renderLog = () => {
    const el = $("#sessionLog");
    if (!el) return;
    const added = scanSession.log.filter((l) => l.result === "added").length;
    el.innerHTML = `<h3 style="margin-bottom:6px">This session${added ? `: ${plural(added, "book")} added` : ""}</h3>` +
      (scanSession.log.length
        ? `<div class="list">${scanSession.log.slice(0, 25).map((l) => `
            <div class="row"><span class="status-dot ${l.result}"></span><div class="grow">
              ${l.bookId ? `<a href="#/book/${l.bookId}">${esc(l.title)}</a>` : `<span>${esc(l.title)}</span>`}
              <p>${esc(l.note)}</p></div></div>`).join("")}</div>`
        : `<p class="muted small">Scanned books will be listed here.</p>`);
  };
  const logEntry = (entry) => { scanSession.log.unshift(entry); renderLog(); };
  renderLog();

  // Feedback
  let audio;
  const beep = (ok = true) => {
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const o = audio.createOscillator(), g = audio.createGain();
      o.frequency.value = ok ? 1320 : 330; g.gain.value = 0.08;
      o.connect(g); g.connect(audio.destination); o.start(); o.stop(audio.currentTime + (ok ? 0.09 : 0.25));
    } catch { /* no audio */ }
    navigator.vibrate?.(ok ? 60 : [80, 60, 80]);
    const vf = $("#vf"); vf?.classList.add("flash"); setTimeout(() => vf?.classList.remove("flash"), 450);
  };

  // Scanner
  let scanner = null, busy = false, last = { code: "", at: 0 };
  const pause = () => { scanner?.pause(); };
  const resume = () => { busy = false; scanner?.resume(); };

  const handleCode = async (raw, { fromCamera = true } = {}) => {
    const code = String(raw).replace(/[^0-9Xx]/g, "");
    if (![8, 10, 12, 13].includes(code.length)) return;
    if (fromCamera && (busy || (code === last.code && Date.now() - last.at < 4000))) return;
    last = { code, at: Date.now() };
    busy = true;
    beep(true);
    if (defaults.mode === "instant") await quickAdd(code);
    else { pause(); await askFirst(code); }
  };

  const quickAdd = async (code) => {
    try {
      const r = await api.post("api/quick-add", { code, shelf_id: defaultShelf(), tags: defaultTags() });
      if (r.result === "added") {
        logEntry({ result: "added", title: r.book.title, bookId: r.book.id, note: r.book.authors || "Added" });
        toast(`Added ${r.book.title}`);
      } else if (r.result === "duplicate") {
        beep(false);
        logEntry({ result: "duplicate", title: r.book.title, bookId: r.book.id, note: `Already in the library${r.book.shelf ? ` on ${r.book.shelf.name}` : ""}` });
        toast(`Already have ${r.book.title}`, { action: "Add another copy", onAction: () => addCopy(r.book) });
      } else {
        beep(false);
        logEntry({ result: "not_found", title: code, note: "Not found online. Add it by hand from the Add page." });
        toast("No record found for that barcode", { error: true });
      }
    } catch (e) { logEntry({ result: "error", title: code, note: e.message }); fail(e); }
    finally { setTimeout(() => { busy = false; }, 900); }
  };

  const addCopy = async (book) => {
    try {
      await api.patch(`api/books/${book.id}`, { copies: (book.copies || 1) + 1 });
      toast(`Now ${(book.copies || 1) + 1} copies of ${book.title}`);
      logEntry({ result: "added", title: book.title, bookId: book.id, note: "Added another copy" });
    } catch (e) { fail(e); }
  };

  const askFirst = async (code) => {
    const area = $("#resultArea");
    area.innerHTML = `<div class="panel"><div class="loading" style="padding:24px"><div class="spinner"></div></div></div>`;
    let r;
    try { r = await api.get(`api/lookup/${encodeURIComponent(code)}`); }
    catch (e) { area.innerHTML = ""; fail(e); resume(); return; }

    const owned = r.owned?.[0];
    const done = () => { area.innerHTML = ""; resume(); $("#codeInput")?.focus({ preventScroll: true }); };

    if (!r.found && !owned) {
      beep(false);
      area.innerHTML = `<div class="panel stack">
        <div><h3>No record for ${esc(code)}</h3><p class="muted small" style="margin-top:4px">${esc(r.error || "")}</p></div>
        <div class="btn-row">${r.kind !== "invalid" ? `<button class="btn btn-primary" id="byHand">Add by hand</button>` : ""}<button class="btn" id="skip">Scan the next one</button></div></div>`;
      $("#byHand")?.addEventListener("click", () => { draft = { isbn13: r.isbn13, isbn10: r.isbn10, tags: defaultTags(), shelf_id: defaultShelf() }; location.hash = "#/new"; });
      $("#skip").addEventListener("click", done);
      logEntry({ result: "not_found", title: code, note: "Not found online" });
      return;
    }

    const show = r.found ? r : { ...owned, cover_urls: owned.cover_url ? [owned.cover_url] : [] };
    area.innerHTML = `<div class="panel stack">
      <div class="result">
        <div>${coverHTML(show, { remote: true })}</div>
        <div><h3>${esc(show.title)}</h3>${show.subtitle ? `<p class="muted small">${esc(show.subtitle)}</p>` : ""}
          <p style="margin-top:4px">${esc(show.authors || "")}</p>
          <p class="muted small">${esc([show.publisher, show.published].filter(Boolean).join(", "))}</p></div>
      </div>
      ${owned ? `<p class="notice warn"><strong>You already have this.</strong> ${owned.shelf ? `It's on ${esc(shelfLabel(owned.shelf))}.` : "It isn't on a shelf yet."}${owned.copies > 1 ? ` ${owned.copies} copies.` : ""}${owned.active_loan ? ` Currently with ${esc(owned.active_loan.borrower)}.` : ""}${owned.status === "wishlist" ? " It's on the wishlist." : ""}</p>` : ""}
      <div class="btn-row">
        ${owned
          ? `<a class="btn btn-primary" href="#/book/${owned.id}">Open it</a><button class="btn" id="copy">Add another copy</button>`
          : `<button class="btn btn-primary" id="add">Add to library</button><button class="btn" id="wish">Add to wishlist</button><button class="btn btn-quiet" id="edit">Edit first</button>`}
        <button class="btn btn-quiet" id="skip">Skip</button>
      </div></div>`;
    if (owned) beep(false);

    const add = async (status) => {
      try {
        const book = await api.post("api/books", { ...lookupToDraft(r), status, shelf_id: defaultShelf(), tags: defaultTags() });
        logEntry({ result: "added", title: book.title, bookId: book.id, note: status === "wishlist" ? "Added to wishlist" : (book.authors || "Added") });
        toast(status === "wishlist" ? "Added to the wishlist" : `Added ${book.title}`);
        done();
      } catch (e) { fail(e); }
    };
    $("#add")?.addEventListener("click", () => add("owned"));
    $("#wish")?.addEventListener("click", () => add("wishlist"));
    $("#edit")?.addEventListener("click", () => { draft = { ...lookupToDraft(r), tags: defaultTags(), shelf_id: defaultShelf() }; location.hash = "#/new"; });
    $("#copy")?.addEventListener("click", async () => { await addCopy(owned); done(); });
    $("#skip").addEventListener("click", () => { if (owned) logEntry({ result: "duplicate", title: owned.title, bookId: owned.id, note: "Already in the library" }); done(); });
  };

  $("#manualCode").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("#codeInput");
    const v = input.value.trim();
    input.value = "";
    if (v) { busy = false; handleCode(v, { fromCamera: false }); }
  });

  // Camera start
  const vfMsg = $("#vfMsg");
  const cameraProblem = (title, text, { retry = false } = {}) => {
    vfMsg.hidden = false; $("#reticle").hidden = true;
    vfMsg.innerHTML = `<h3>${esc(title)}</h3><p>${text}</p><div class="btn-row" style="justify-content:center">
      ${retry ? `<button class="btn btn-gilt" id="retryCam">Try again</button>` : ""}
      ${inFrame ? `<button class="btn btn-gilt" id="popOut">Open in its own tab</button>` : ""}</div>`;
    $("#retryCam")?.addEventListener("click", () => { vfMsg.innerHTML = `<div class="spinner" style="margin:auto"></div><p>Starting the camera...</p>`; startCamera(); });
    $("#popOut")?.addEventListener("click", () => window.open(location.href, "_blank"));
    $("#codeInput").focus();
  };

  // Photo fallback: the phone's own camera app focuses up close, then we read the picture.
  $("#photoScan").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    pause();
    try {
      const code = await BookScanner.decodeImageFile(file);
      if (code) { busy = false; await handleCode(code, { fromCamera: false }); }
      else { toast("Couldn't find a barcode in that photo. Try again a little farther back, with the barcode level.", { error: true, ms: 6000 }); resume(); }
    } catch (err) { fail(err); resume(); }
  });

  const setupCameraTools = () => {
    // Lens picker, when the phone exposes more than one back camera.
    const pick = $("#camPick");
    if (scanner.cameras.length > 1) {
      const current = scanner.deviceId();
      pick.innerHTML = scanner.cameras.map((c, i) =>
        `<option value="${esc(c.deviceId)}" ${c.deviceId === current ? "selected" : ""}>${esc(c.label || `Camera ${i + 1}`)}</option>`).join("");
      pick.hidden = false;
      pick.onchange = async () => {
        try { await scanner.useCamera(pick.value); setupCameraTools(); } catch (err) { fail(err); }
      };
    } else pick.hidden = true;

    // Zoom lets you hold the phone back past the minimum focus distance.
    const zoomBtn = $("#zoomBtn");
    const z = scanner.zoomRange();
    if (z && z.max >= 1.5) {
      const steps = [1, 2, 3].filter((v) => v <= z.max);
      zoomBtn.hidden = false;
      const label = () => { zoomBtn.textContent = `Zoom ${Math.round(scanner.currentZoom())}x`; };
      label();
      zoomBtn.onclick = async () => {
        const cur = Math.round(scanner.currentZoom());
        const next = steps[(steps.indexOf(cur) + 1) % steps.length] || 1;
        try { await scanner.setZoom(next); } catch { /* not supported after all */ }
        label();
      };
    } else zoomBtn.hidden = true;

    const torchBtn = $("#torchBtn");
    if (scanner.hasTorch()) {
      torchBtn.hidden = false;
      torchBtn.onclick = async () => {
        const on = torchBtn.getAttribute("aria-pressed") !== "true";
        try { await scanner.setTorch(on); torchBtn.setAttribute("aria-pressed", on); } catch (err) { fail(err); }
      };
    } else torchBtn.hidden = true;

    const hint = $("#camHint");
    const isApple = /iPhone|iPad/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    if (isApple) {
      hint.hidden = false;
      hint.textContent = scanner.cameras.length > 1
        ? "Blurry up close? Pull the phone back a bit, or try another camera from the list. Take a photo always works."
        : "Blurry up close? Pull the phone back a bit, or use Take a photo.";
    }
  };

  const startCamera = async () => {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      cameraProblem("Camera needs a secure connection",
        "Browsers only allow the live camera over HTTPS. Open the library through your HTTPS address (Home Assistant with SSL, Nabu Casa, or a reverse proxy). Take a photo, a USB barcode scanner, or typing the ISBN below work anywhere.");
      return;
    }
    if (typeof BookScanner === "undefined") { cameraProblem("Scanner didn't load", "Reload the page and try again.", { retry: true }); return; }
    scanner = new BookScanner($("#camVideo"), {
      box: $("#reticle"),
      onDetect: (code) => handleCode(code),
      onError: (err) => console.warn("scan", err),
    });
    try {
      $("#reticle").hidden = false;   // needed to map the box onto the video
      await scanner.start();
      if (!$("#vf")) { await stop(); return; } // navigated away while starting
      vfMsg.hidden = true;
      setupCameraTools();
    } catch (err) {
      scanner = null;
      const name = err?.name || String(err);
      if (/NotAllowed|Permission/i.test(name)) {
        cameraProblem("Camera access was blocked",
          inFrame ? "This page is inside another app (like Home Assistant), which may not pass the camera through. Open it in its own tab, or allow camera access in your browser settings. Take a photo still works."
                  : "Allow camera access for this site in your browser settings, then try again. Take a photo still works.", { retry: true });
      } else if (/NotFound|Overconstrained/i.test(name)) {
        cameraProblem("No camera found", "This device doesn't seem to have a camera. Use a USB scanner or type the ISBN below.");
      } else {
        cameraProblem("Couldn't start the camera", esc(err?.message || name), { retry: true });
      }
    }
  };

  const stop = async () => {
    const s = scanner; scanner = null;
    if (s) await s.stop();
  };
  const onVisibility = () => { if (document.hidden) pause(); else if (!busy) resume(); };
  document.addEventListener("visibilitychange", onVisibility);
  cleanup = async () => { document.removeEventListener("visibilitychange", onVisibility); await stop(); };

  startCamera();
}

// ------------------------------------------------------------------ loans

async function renderLoans(_m, params) {
  loading();
  if (!isEditor()) { await renderMyBooks(); return; }
  const history = params.history === "1";
  const [loans, requests] = await Promise.all([api.get(`api/loans?active=${!history}`), history ? [] : api.get("api/requests")]);
  const overdueCount = loans.filter((l) => l.overdue).length;
  view.innerHTML = `
    <div class="page-head"><div><h1>Loans</h1><p class="muted">${history ? "Every book lent out, newest first." : loans.length ? `${plural(loans.length, "book")} out right now${overdueCount ? `, ${overdueCount} overdue` : ""}.` : ""}</p></div>
      <a class="btn btn-sm" href="${history ? "#/loans" : "#/loans?history=1"}">${history ? "Show books out now" : "Show history"}</a></div>
    ${requests.length ? `<div class="panel" style="margin-bottom:16px"><h2>Asked to borrow</h2><div class="list">${requests.map((r) => `
      <div class="row stack-mobile">
        <div class="thumb">${coverHTML(r)}</div>
        <div class="grow"><a href="#/book/${r.book_id}">${esc(r.title)}</a>
          <p><strong style="color:var(--ink)">${esc(r.display_name)}</strong>, ${fmtDate(r.created_at)}${r.message ? `: "${esc(r.message)}"` : ""}${r.book_out ? ". Checked out right now." : ""}</p></div>
        <div class="btn-row">${r.book_out ? "" : `<button class="btn btn-primary btn-sm" data-approve="${r.id}">Lend it</button>`}<button class="btn btn-sm btn-quiet" data-decline="${r.id}">Decline</button></div>
      </div>`).join("")}</div></div>` : ""}
    ${loans.length ? `<div class="panel"><div class="list">${loans.map((l) => `
      <div class="row ${l.overdue ? "overdue" : ""}">
        <div class="thumb">${coverHTML(l)}</div>
        <div class="grow"><a href="#/book/${l.book_id}">${esc(l.title)}</a>
          <p>${esc(l.borrower)}, since ${fmtDate(l.loaned_at)}${l.returned_at ? `. Returned ${fmtDate(l.returned_at)}` : l.due_at ? `. <span class="due">${l.overdue ? "Was due" : "Due"} ${fmtDate(l.due_at)}</span>` : ""}</p></div>
        ${l.returned_at ? "" : `<button class="btn btn-sm" data-return="${l.id}">Returned</button>`}
      </div>`).join("")}</div></div>`
    : `<div class="empty"><h2>${history ? "Nothing lent yet" : "Every book is home"}</h2><p>To lend a book, open it from the library and choose Lend.</p></div>`}`;
  $$("[data-return]").forEach((b) => b.addEventListener("click", async () => {
    try { await api.post(`api/loans/${b.dataset.return}/return`); toast("Marked returned"); updateBadge(); renderLoans(null, params); } catch (e) { fail(e); }
  }));
  wireRequestButtons(() => renderLoans(null, params));
}

async function renderMyBooks() {
  const [loans, requests] = await Promise.all([api.get("api/loans"), api.get("api/requests")]);
  const pending = requests.filter((r) => r.status === "pending");
  const recent = requests.filter((r) => r.status === "declined");
  view.innerHTML = `
    <div class="page-head"><div><h1>My books</h1><p class="muted">Books you've borrowed and books you've asked for.</p></div></div>
    <div class="stack">
      <div class="panel"><h2>Borrowed</h2>
        ${loans.length ? `<div class="list">${loans.map((l) => `
          <div class="row ${l.overdue ? "overdue" : ""}"><div class="thumb">${coverHTML(l)}</div>
            <div class="grow"><a href="#/book/${l.book_id}">${esc(l.title)}</a>
              <p>Since ${fmtDate(l.loaned_at)}${l.due_at ? `. <span class="due">${l.overdue ? "Was due" : "Due back"} ${fmtDate(l.due_at)}</span>` : ""}</p></div></div>`).join("")}</div>`
          : `<p class="muted">You don't have any books right now. Find one in the library and choose Ask to borrow.</p>`}
      </div>
      ${pending.length || recent.length ? `<div class="panel"><h2>Requests</h2><div class="list">${[...pending, ...recent].map((r) => `
        <div class="row"><div class="thumb">${coverHTML(r)}</div>
          <div class="grow"><a href="#/book/${r.book_id}">${esc(r.title)}</a>
            <p>${r.status === "pending" ? `Asked ${fmtDate(r.created_at)}, waiting for an answer` : "Not this time"}</p></div>
          ${r.status === "pending" ? `<button class="btn btn-sm btn-quiet" data-cancel="${r.id}">Cancel</button>` : ""}</div>`).join("")}</div></div>` : ""}
    </div>`;
  $$("[data-cancel]").forEach((b) => b.addEventListener("click", async () => {
    try { await api.post(`api/requests/${b.dataset.cancel}/cancel`); toast("Request cancelled"); updateBadge(); renderMyBooks(); } catch (e) { fail(e); }
  }));
}

// ------------------------------------------------------------------ shelves & tags

async function renderOrganize() {
  loading();
  await refreshMeta();
  const byRoom = {};
  cache.shelves.forEach((s) => (byRoom[s.room || ""] ||= []).push(s));
  const rooms = Object.keys(byRoom).sort((a, b) => (a === "") - (b === "") || a.localeCompare(b));

  view.innerHTML = `
    <div class="page-head"><div><h1>Shelves and tags</h1><p class="muted">Shelves say where a book lives. Tags group books however you like.</p></div></div>
    <div class="org-grid">
      <div class="panel">
        <h2>Shelves</h2>
        ${cache.shelves.length ? rooms.map((room) => `
          <h3 class="muted" style="font-family:var(--sans);font-size:14px;margin-top:10px">${esc(room || "No room set")}</h3>
          <div class="list">${byRoom[room].map((s) => `
            <div class="row"><div class="grow"><a href="#/?shelf=${s.id}">${esc(s.name)}</a><p>${plural(s.book_count, "book")}</p></div>
              <button class="btn btn-sm btn-quiet" data-edit-shelf="${s.id}">Edit</button></div>`).join("")}</div>`).join("")
          : `<p class="muted">No shelves yet. Add the bookcases and rooms in your house so you can find things later.</p>`}
        <form class="inline-form" id="shelfForm">
          <input name="name" placeholder="Shelf, e.g. Top shelf" required aria-label="Shelf name">
          <input name="room" placeholder="Room, e.g. Living room" aria-label="Room" list="roomList">
          <datalist id="roomList">${rooms.filter(Boolean).map((r) => `<option value="${esc(r)}">`).join("")}</datalist>
          <button class="btn btn-primary">Add shelf</button>
        </form>
      </div>
      <div class="stack">
        <div class="panel">
          <h2>Tags</h2>
          ${cache.tags.length ? `<div class="list">${cache.tags.map((t) => `
            <div class="row"><div class="grow"><a href="#/?tag=${t.id}"><span class="tag" style="--tag:${esc(t.color)}">${esc(t.name)}</span></a></div>
              <span class="muted small">${plural(t.book_count, "book")}</span>
              <button class="btn btn-sm btn-quiet" data-edit-tag="${t.id}">Edit</button></div>`).join("")}</div>`
            : `<p class="muted">Tags appear here as you add them to books. Kids, Favorites, Signed, To read, or anything else.</p>`}
          <form class="inline-form" id="tagForm">
            <input name="name" placeholder="New tag" required aria-label="Tag name">
            <input type="color" name="color" value="#b98a2a" aria-label="Tag color">
            <button class="btn btn-primary">Add tag</button>
          </form>
        </div>
        <div class="panel">
          <h2>Your data</h2>
          <p class="muted" style="margin-bottom:12px">Download everything as a spreadsheet. On Home Assistant, the library is also included in your regular backups.</p>
          <a class="btn" href="api/export.csv" download>Download CSV</a>
        </div>
      </div>
    </div>`;

  $("#shelfForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try { await api.post("api/shelves", { name: fd.get("name"), room: fd.get("room") || "" }); toast("Shelf added"); renderOrganize(); } catch (err) { fail(err); }
  });
  $("#tagForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try { await api.post("api/tags", { name: fd.get("name"), color: fd.get("color") }); toast("Tag added"); renderOrganize(); } catch (err) { fail(err); }
  });
  $$("[data-edit-shelf]").forEach((b) => b.addEventListener("click", async () => {
    const s = cache.shelves.find((x) => x.id === +b.dataset.editShelf);
    const fd = await modalForm("Edit shelf", `
      <label class="field"><span>Shelf name</span><input name="name" value="${esc(s.name)}" required></label>
      <label class="field"><span>Room</span><input name="room" value="${esc(s.room)}"></label>
      <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="delete"> Delete this shelf (its ${plural(s.book_count, "book")} become unshelved)</label>`);
    if (!fd) return;
    try {
      if (fd.get("delete")) { await api.del(`api/shelves/${s.id}`); toast("Shelf deleted"); }
      else { await api.patch(`api/shelves/${s.id}`, { name: fd.get("name"), room: fd.get("room") || "" }); toast("Shelf saved"); }
      renderOrganize();
    } catch (err) { fail(err); }
  }));
  $$("[data-edit-tag]").forEach((b) => b.addEventListener("click", async () => {
    const t = cache.tags.find((x) => x.id === +b.dataset.editTag);
    const fd = await modalForm("Edit tag", `
      <div class="inline-form" style="margin:0"><input name="name" value="${esc(t.name)}" required aria-label="Tag name"><input type="color" name="color" value="${esc(t.color)}" aria-label="Tag color"></div>
      <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="delete"> Delete this tag (books keep everything else)</label>`);
    if (!fd) return;
    try {
      if (fd.get("delete")) { await api.del(`api/tags/${t.id}`); toast("Tag deleted"); }
      else { await api.patch(`api/tags/${t.id}`, { name: fd.get("name"), color: fd.get("color") }); toast("Tag saved"); }
      renderOrganize();
    } catch (err) { fail(err); }
  }));
}

// ------------------------------------------------------------------ sign in

const ROLE_INFO = {
  owner: ["Owner", "Everything, including inviting people"],
  editor: ["Family", "Add, edit and lend books"],
  viewer: ["Friend", "Browse and ask to borrow"],
};

function authShell(inner) {
  document.body.classList.add("signed-out");
  view.innerHTML = `<div class="auth-wrap"><div class="auth-card">
    <div class="auth-brand"><img src="static/icon.svg" alt="" width="40" height="40"><span>Home Library</span></div>
    ${inner}</div></div>`;
}

async function signedIn() {
  ME = await api.get("api/auth/me");
  applyRole();
  document.body.classList.remove("signed-out");
  if (/^#\/invite\//.test(location.hash)) location.hash = "#/";
  else route();
}

function renderSignIn() {
  if (cleanup) { Promise.resolve(cleanup()).catch(() => {}); cleanup = null; }
  if (ME.setup_needed) {
    authShell(`
      <h1>Set up your library</h1>
      <p class="muted">Create the owner account. You'll invite family and friends from the Account page afterwards.</p>
      <form id="authForm" class="stack" style="margin-top:18px">
        <label class="field"><span>Your name</span><input name="display_name" required autocomplete="name"></label>
        <label class="field"><span>Username</span><input name="username" required autocomplete="username" autocapitalize="none" minlength="2"></label>
        <label class="field"><span>Password</span><input type="password" name="password" required minlength="8" autocomplete="new-password"></label>
        <button class="btn btn-primary">Create library</button>
      </form>`);
    $("#authForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try { await api.post("api/auth/setup", Object.fromEntries(fd)); await signedIn(); } catch (err) { fail(err); }
    });
    return;
  }
  authShell(`
    <h1>Sign in</h1>
    <form id="authForm" class="stack" style="margin-top:18px">
      <label class="field"><span>Username</span><input name="username" required autocomplete="username" autocapitalize="none"></label>
      <label class="field"><span>Password</span><input type="password" name="password" required autocomplete="current-password"></label>
      <button class="btn btn-primary">Sign in</button>
    </form>
    <p class="muted small" style="margin-top:16px">New here? Ask the library owner for an invite link. Forgot your password? They can send you a reset link.</p>`);
  $("#authForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const btn = $("button", e.target); btn.disabled = true;
    try { await api.post("api/auth/login", Object.fromEntries(fd)); await signedIn(); }
    catch (err) { fail(err); btn.disabled = false; }
  });
}

async function renderInvite(token) {
  let info;
  try { info = await api.get(`api/auth/invite/${token}`); }
  catch (err) {
    authShell(`<h1>Link expired</h1><p class="muted" style="margin:8px 0 18px">${esc(err.message)}</p><a class="btn" href="#/">Go to sign in</a>`);
    return;
  }
  const [roleName, roleDesc] = ROLE_INFO[info.role] || ROLE_INFO.viewer;
  const signedInNote = ME.user ? `<p class="notice" style="margin-top:12px">You're signed in as ${esc(ME.user.display_name)}. Continuing switches to the ${info.reset ? "account below" : "new account"}.</p>` : "";
  authShell(info.reset ? `
      <h1>Choose a new password</h1>
      <p class="muted">For ${esc(info.user.display_name)}${info.user.username ? ` (${esc(info.user.username)})` : ""}.</p>${signedInNote}
      <form id="authForm" class="stack" style="margin-top:18px">
        <input type="hidden" name="display_name" value="${esc(info.user.display_name)}">
        <label class="field"><span>Username</span><input name="username" value="${esc(info.user.username || "")}" ${info.user.username ? "readonly" : "required"} autocomplete="username" autocapitalize="none" minlength="2"></label>
        <label class="field"><span>New password</span><input type="password" name="password" required minlength="8" autocomplete="new-password"></label>
        <button class="btn btn-primary">Save and sign in</button>
      </form>` : `
      <h1>You're invited</h1>
      <p class="muted">${info.library_owner ? `${esc(info.library_owner)} invited you to their book library.` : "You've been invited to a book library."} You'll be able to ${esc(roleDesc.toLowerCase())}.</p>${signedInNote}
      <form id="authForm" class="stack" style="margin-top:18px">
        <label class="field"><span>Your name</span><input name="display_name" required autocomplete="name" placeholder="How the owner will see you"></label>
        <label class="field"><span>Username</span><input name="username" required autocomplete="username" autocapitalize="none" minlength="2"></label>
        <label class="field"><span>Password</span><input type="password" name="password" required minlength="8" autocomplete="new-password"><small class="muted">At least 8 characters.</small></label>
        <button class="btn btn-primary">Join as ${esc(roleName.toLowerCase())}</button>
      </form>`);
  $("#authForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const btn = $("button", e.target); btn.disabled = true;
    try { await api.post(`api/auth/invite/${token}`, Object.fromEntries(fd)); toast(info.reset ? "Password saved" : "Welcome to the library"); await signedIn(); }
    catch (err) { fail(err); btn.disabled = false; }
  });
}

// ------------------------------------------------------------------ account & people

function copyText(text, input) {
  const done = () => toast("Link copied");
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(text).then(done, () => { input?.select(); document.execCommand("copy"); done(); });
  else { input?.select(); document.execCommand("copy"); done(); }
}

function showLink(title, link, explain) {
  const dlg = $("#modal");
  dlg.innerHTML = `<form method="dialog"><h2>${esc(title)}</h2><p class="muted">${esc(explain)}</p>
    <input id="linkOut" readonly value="${esc(link)}" aria-label="Link">
    <div class="btn-row">${navigator.share ? `<button type="button" class="btn" id="shareLink">Share</button>` : ""}
      <button type="button" class="btn btn-primary" id="copyLink">Copy link</button><button class="btn btn-quiet" value="cancel">Done</button></div></form>`;
  dlg.showModal();
  $("#linkOut").select();
  $("#copyLink").addEventListener("click", () => copyText(link, $("#linkOut")));
  $("#shareLink")?.addEventListener("click", () => navigator.share({ title: "Home Library", text: title, url: link }).catch(() => {}));
}

async function renderAccount() {
  loading();
  const u = ME.user;
  const owner = isOwner();
  const [users, invites, settings] = owner
    ? await Promise.all([api.get("api/users"), api.get("api/invites"), api.get("api/settings")])
    : [[], [], null];
  const [roleName, roleDesc] = ROLE_INFO[u.role];
  const viaHA = ME.via === "homeassistant";

  view.innerHTML = `
    <div class="page-head"><div><h1>Account</h1></div></div>
    <div class="org-grid">
      <div class="stack">
        <div class="panel">
          <h2>${esc(u.display_name)}</h2>
          <p class="muted">${esc(roleName)}: ${esc(roleDesc.toLowerCase())}.</p>
          <p class="small muted" style="margin-top:6px">${viaHA ? "Signed in through Home Assistant." : `Signed in as ${esc(u.username)}.`}</p>
          ${viaHA ? "" : `<div class="btn-row" style="margin-top:14px"><button class="btn" id="signOut">Sign out</button></div>`}
        </div>
        <form class="panel" id="pwForm">
          <h2>${u.has_password ? "Change password" : "Sign in outside Home Assistant"}</h2>
          ${u.has_password ? "" : `<p class="muted" style="margin-bottom:12px">Add a username and password so you can use the library's own address, for example on a phone that isn't signed in to Home Assistant.</p>`}
          <div class="stack">
            ${u.has_password
              ? `<label class="field"><span>Current password</span><input type="password" name="current" required autocomplete="current-password"></label>`
              : `<label class="field"><span>Username</span><input name="username" required minlength="2" autocapitalize="none" autocomplete="username"></label>`}
            <label class="field"><span>New password</span><input type="password" name="new" required minlength="8" autocomplete="new-password"></label>
            <div><button class="btn btn-primary">${u.has_password ? "Change password" : "Save sign-in"}</button></div>
          </div>
        </form>
        ${owner ? `
        <form class="panel" id="urlForm">
          <h2>Library address for friends</h2>
          <p class="muted" style="margin-bottom:12px">The web address friends use to reach the library, outside Home Assistant. Invite links are built from it. See the README for setting one up with a Cloudflare Tunnel or Tailscale.</p>
          <div class="inline-form nowrap" style="margin-top:0"><input name="public_url" value="${esc(settings.public_url)}" placeholder="https://library.example.com" inputmode="url" aria-label="Library address"><button class="btn">Save</button></div>
        </form>` : ""}
      </div>
      ${owner ? `
      <div class="stack">
        <div class="panel">
          <h2>People</h2>
          <div class="list">${users.map((p) => `
            <div class="row">
              <div class="grow"><p style="color:var(--ink);font-weight:600">${esc(p.display_name)}${p.id === u.id ? " (you)" : ""}${p.disabled ? ` <span class="flag overdue">Turned off</span>` : ""}</p>
                <p>${[p.ha_linked && "Home Assistant", p.username && `@${p.username}`].filter(Boolean).map(esc).join(", ")}${p.last_seen ? `. Last seen ${fmtDate(p.last_seen)}` : ". Hasn't signed in yet"}</p></div>
              <select data-role="${p.id}" aria-label="Role for ${esc(p.display_name)}" style="width:auto">${Object.entries(ROLE_INFO).map(([k, [n]]) => `<option value="${k}" ${p.role === k ? "selected" : ""}>${n}</option>`).join("")}</select>
              ${p.id === u.id ? "" : `<button class="btn btn-sm btn-quiet" data-person="${p.id}" aria-label="More for ${esc(p.display_name)}">More</button>`}
            </div>`).join("")}</div>
        </div>
        <div class="panel">
          <h2>Invite someone</h2>
          <p class="muted" style="margin-bottom:12px">Make a link and send it to them. It works once and expires in 7 days.</p>
          <form id="inviteForm" class="stack">
            <div class="form-grid">
              <label class="field"><span>Who's it for? (optional)</span><input name="note" placeholder="e.g. Jess from small group"></label>
              <label class="field"><span>They can</span><select name="role">
                <option value="viewer">Browse and ask to borrow</option>
                <option value="editor">Add, edit and lend books</option>
                <option value="owner">Everything, including inviting</option></select></label>
            </div>
            <div><button class="btn btn-primary">Make invite link</button></div>
          </form>
          ${invites.filter((i) => !i.for_user_id).length ? `<h3 style="margin:18px 0 4px">Waiting to be used</h3><div class="list">${invites.filter((i) => !i.for_user_id).map((i) => `
            <div class="row"><div class="grow"><p style="color:var(--ink)">${esc(i.note || "Invite")}</p><p>${esc(ROLE_INFO[i.role][0])}, expires ${fmtDate(i.expires_at)}</p></div>
              <button class="btn btn-sm btn-quiet" data-revoke="${i.id}">Cancel</button></div>`).join("")}</div>` : ""}
        </div>
        ${viaHA ? `<p class="small muted">Family members with their own Home Assistant login can open Library from the sidebar and are added here automatically.</p>` : ""}
      </div>` : ""}
    </div>`;

  $("#signOut")?.addEventListener("click", async () => {
    await api.post("api/auth/logout");
    ME = await api.get("api/auth/me");
    location.hash = "#/";
    renderSignIn();
  });
  $("#pwForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try { await api.post("api/auth/password", Object.fromEntries(fd)); toast(u.has_password ? "Password changed" : "Sign-in saved"); ME = await api.get("api/auth/me"); renderAccount(); }
    catch (err) { fail(err); }
  });
  if (!owner) return;

  $("#urlForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await api.put("api/settings", { public_url: new FormData(e.target).get("public_url") }); toast("Address saved"); } catch (err) { fail(err); }
  });
  $("#inviteForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      const inv = await api.post("api/invites", Object.fromEntries(fd));
      if (!inv.url) {
        toast("Save the library address for friends first, so the link points somewhere they can reach", { error: true, ms: 7000 });
        await api.del(`api/invites/${inv.id}`);
        $("#urlForm input").focus();
        return;
      }
      showLink("Invite link ready", inv.url, "Send this to them by text or email. Anyone with the link can use it once, so send it only to the person you mean.");
      $("#modal").addEventListener("close", () => renderAccount(), { once: true });
    } catch (err) { fail(err); }
  });
  $$("[data-revoke]").forEach((b) => b.addEventListener("click", async () => {
    try { await api.del(`api/invites/${b.dataset.revoke}`); toast("Invite cancelled"); renderAccount(); } catch (err) { fail(err); }
  }));
  $$("[data-role]").forEach((sel) => sel.addEventListener("change", async () => {
    try {
      await api.patch(`api/users/${sel.dataset.role}`, { role: sel.value });
      toast("Access updated");
      if (+sel.dataset.role === u.id) { ME = await api.get("api/auth/me"); applyRole(); location.hash = "#/"; }
    } catch (err) { fail(err); renderAccount(); }
  }));
  $$("[data-person]").forEach((b) => b.addEventListener("click", async () => {
    const p = users.find((x) => x.id === +b.dataset.person);
    const fd = await modalForm(p.display_name, `
      <label class="field"><span>Name</span><input name="display_name" value="${esc(p.display_name)}"></label>
      <fieldset class="stack" style="border:0;padding:0;margin:0">
        <label style="display:flex;gap:8px;align-items:center"><input type="radio" name="action" value="save" checked> Just save the name</label>
        ${p.ha_linked && !p.username ? "" : `<label style="display:flex;gap:8px;align-items:center"><input type="radio" name="action" value="reset"> Make a password reset link</label>`}
        <label style="display:flex;gap:8px;align-items:center"><input type="radio" name="action" value="toggle"> ${p.disabled ? "Turn access back on" : "Turn off access (keeps their loan history)"}</label>
        <label style="display:flex;gap:8px;align-items:center"><input type="radio" name="action" value="remove"> Remove them completely</label>
      </fieldset>`, "Continue");
    if (!fd) return;
    try {
      const action = fd.get("action");
      if (action === "save") { await api.patch(`api/users/${p.id}`, { display_name: fd.get("display_name") }); toast("Saved"); }
      if (action === "toggle") { await api.patch(`api/users/${p.id}`, { disabled: !p.disabled }); toast(p.disabled ? "Access turned on" : "Access turned off"); }
      if (action === "remove") {
        if (!(await confirmDialog(`Remove ${p.display_name}?`, "They won't be able to sign in. Books they borrowed stay in your loan history.", "Remove"))) return;
        await api.del(`api/users/${p.id}`); toast("Removed");
      }
      if (action === "reset") {
        const inv = await api.post(`api/users/${p.id}/reset`);
        if (!inv.url) { toast("Save the library address for friends first", { error: true }); await api.del(`api/invites/${inv.id}`); return; }
        showLink("Password reset link", inv.url, `Send this to ${p.display_name}. It lets them choose a new password and works once.`);
      }
      renderAccount();
    } catch (err) { fail(err); }
  }));
}

// ------------------------------------------------------------------ boot

function applyRole() {
  const r = role();
  $$("[data-min-role]").forEach((el) => { el.hidden = !r || RANK[r] < RANK[el.dataset.minRole]; });
  const loansLabel = $('[data-nav="loans"] span');
  if (loansLabel) loansLabel.textContent = isEditor() ? "Loans" : "My books";
}

async function boot() {
  try { ME = await api.get("api/auth/me"); }
  catch (err) { ME = { user: null }; }
  applyRole();
  route();
}
boot();
