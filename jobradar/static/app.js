"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  searches: [],
  currentId: null,
  offers: [],
  editing: null, // { id, criteria }
  pollTimer: null,
  apps: [],
  trackerView: "kanban",
};

const APP_STATUS = {
  consultee: { label: "Offre consultée", short: "Consultée", color: "#8b93a7" },
  envoyee: { label: "Candidature envoyée", short: "Envoyée", color: "#087CFA" },
  relance: { label: "Relancée", color: "#8b5cf6" },
  entretien_rh: { label: "Entretien RH", color: "#f59e0b" },
  entretien_tech: { label: "Entretien technique", short: "Entretien tech.", color: "#f97316" },
  retenu: { label: "Retenu", color: "#16a34a" },
  refuse: { label: "Refusé", color: "#dc2626" },
  abandon: { label: "Abandonnée", color: "#6b7280" },
};

function stBadge(st) {
  const m = APP_STATUS[st];
  return m ? `<span class="badge" style="--st:${m.color};color:${m.color};background:color-mix(in srgb, ${m.color} 14%, transparent)">${esc(m.label)}</span>` : "";
}

const CONTRACT_LABELS = {
  stage: "Stage", alternance: "Alternance", cdi: "CDI", cdd: "CDD",
  professionnalisation: "Professionnalisation", freelance: "Freelance", interim: "Intérim", vie: "VIE",
};

// ------------------------------------------------------------------ utilitaires

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function safeUrl(u) {
  try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.href : "#"; } catch { return "#"; }
}

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

function hue(str) {
  let h = 0;
  for (const c of String(str)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

function initials(name) {
  const words = String(name || "?").replace(/[^\p{L}\p{N} ]/gu, " ").trim().split(/\s+/);
  return ((words[0]?.[0] || "?") + (words[1]?.[0] || "")).toUpperCase();
}

// Logo de l'entreprise, ou monogramme coloré si aucun logo n'est connu ou s'il ne charge pas.
function logoAv(company, logo, size = "") {
  const mono = `<div class="logo-av mono ${size}" style="--h:${hue(company || "?")}">${esc(initials(company))}</div>`;
  if (!logo || safeUrl(logo) === "#") return mono;
  return `<div class="logo-av ${size}" data-mono="${esc(mono)}"><img src="${esc(safeUrl(logo))}" alt="" loading="lazy"
    referrerpolicy="no-referrer" onerror="this.parentElement.outerHTML = this.parentElement.dataset.mono"></div>`;
}

function scoreColor(s) {
  if (s == null) return "var(--muted)";
  if (s >= 75) return "var(--good)";
  if (s >= 50) return "var(--mid)";
  return "var(--low)";
}

function scoreRing(s, lg = false) {
  return `<div class="score ${lg ? "lg" : ""}" style="--p:${s ?? 0};--c:${scoreColor(s)}"><span>${s ?? "–"}</span></div>`;
}

function fmtDate(d) {
  if (!d) return "";
  const x = new Date(d);
  if (isNaN(x)) return d;
  const days = Math.round((Date.now() - x) / 864e5);
  if (days <= 0) return "aujourd'hui";
  if (days === 1) return "hier";
  if (days < 31) return `il y a ${days} j`;
  return x.toLocaleDateString("fr-FR");
}

function showView(id) {
  $$(".view").forEach(v => (v.hidden = v.id !== id));
  $("#trackerNav").classList.toggle("on", id === "viewTracker");
  if (id !== "viewResults") $$("#searchList .search-item").forEach(b => b.classList.remove("on"));
}

function prefs(id) {
  const def = { zone: "flex", zoneThr: 75, min: 40, status: "active", source: "", sort: "score" };
  try { return { ...def, ...JSON.parse(localStorage.getItem(`jr-prefs-${id}`) || "{}") }; } catch { return def; }
}
function savePrefs(id, p) {
  try { localStorage.setItem(`jr-prefs-${id}`, JSON.stringify(p)); } catch {}
}

// ------------------------------------------------------------------ barre latérale

async function loadSearches() {
  state.searches = await api("/api/searches");
  const nav = $("#searchList");
  nav.innerHTML = state.searches.map(s => `
    <button class="search-item ${s.id === state.currentId ? "on" : ""}" data-id="${s.id}">
      <span>${esc(s.name)}</span><span class="count">${s.offer_count}</span>
    </button>`).join("") || `<p class="muted small">Aucune recherche enregistrée.</p>`;
  $$(".search-item", nav).forEach(b => (b.onclick = () => openResults(+b.dataset.id)));
  loadApps();
}

async function loadApps() {
  state.apps = await api("/api/applications");
  $("#trackerCount").textContent = state.apps.length;
  return state.apps;
}

// ------------------------------------------------------------------ éditeur de recherche

function setChips(container, values) {
  $$("button", container).forEach(b => b.classList.toggle("on", values.includes(b.dataset.v)));
}
function chipValues(container) {
  return $$("button.on", container).map(b => b.dataset.v);
}
$$(".chips-select").forEach(c => c.addEventListener("click", e => {
  if (e.target.tagName === "BUTTON") e.target.classList.toggle("on");
}));
$$(".segmented").forEach(c => c.addEventListener("click", e => {
  if (e.target.tagName !== "BUTTON") return;
  $$("button", c).forEach(b => b.classList.toggle("on", b === e.target));
  c.dispatchEvent(new Event("change"));
}));

function readForm() {
  return {
    domaine: $("#fDomain").value.trim(),
    types_de_contrat: chipValues($("#fContracts")),
    localisations: $("#fLocation").value.trim(),
    niveau: $("#fLevel").value,
    duree: $("#fDuration").value.trim(),
    debut: $("#fStart").value,
    mots_cles: $("#fKeywords").value.trim(),
    publiees_depuis_jours: +$("#fDays").value,
  };
}

function fillForm(form = {}, prompt = "", frequency = "daily") {
  $("#fDomain").value = form.domaine || "";
  setChips($("#fContracts"), form.types_de_contrat || []);
  $("#fLocation").value = form.localisations || "";
  $("#fLevel").value = form.niveau ?? "Bac+5";
  $("#fDuration").value = form.duree || "";
  $("#fStart").value = form.debut || "";
  $("#fKeywords").value = form.mots_cles || "";
  $("#fDays").value = String(form.publiees_depuis_jours || 30);
  $("#fPrompt").value = prompt;
  setChips($("#fFrequency"), [frequency]);
}

function openEditor(search = null) {
  stopPolling();
  state.currentId = search?.id ?? null;
  state.editing = { id: search?.id ?? null, criteria: search ? structuredClone(search.criteria) : null };
  $("#editorTitle").textContent = search ? `Modifier « ${search.name} »` : "Nouvelle recherche";
  fillForm(search?.form, search?.prompt, search?.frequency);
  if (search) renderCriteria(search.name);
  else $("#criteriaPanel").hidden = true;
  showView("viewEditor");
  loadSearches();
}
window.openEditor = openEditor;

async function analyze() {
  const btn = $("#analyzeBtn");
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> ${esc(state.aiLabel)} analyse ta demande…`;
  try {
    const crit = await api("/api/analyze", { method: "POST", body: { form: readForm(), prompt: $("#fPrompt").value } });
    state.editing.criteria = crit;
    renderCriteria(crit.name);
    $("#criteriaPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    toast(`Analyse impossible : ${e.message}`, 6000);
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}

function renderCriteria(name) {
  const c = state.editing.criteria;
  $("#criteriaPanel").hidden = false;
  $("#cName").value = name || c.name || "";
  $("#cSummary").textContent = c.summary || "";
  const conf = $("#cConflicts");
  conf.hidden = !(c.conflicts && c.conflicts.length);
  conf.innerHTML = `<strong>⚠ Points à vérifier</strong><ul>${(c.conflicts || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;

  $$(".tags.editable").forEach(box => renderTagBox(box, c, box.dataset.key));
  renderLocations();

  const contracts = Object.entries(CONTRACT_LABELS)
    .map(([v, l]) => `<button data-v="${v}" class="${(c.contract_types || []).includes(v) ? "on" : ""}">${l}</button>`).join("");
  $("#cMeta").innerHTML = `
    <span class="k">Contrats</span><div class="chips-select" id="cContracts">${contracts}</div>
    <span class="k">Niveau</span><input data-f="education_level" value="${esc(c.education_level)}">
    <span class="k">Durée</span><input data-f="duration" value="${esc(c.duration)}">
    <span class="k">Début</span><input data-f="start_date" value="${esc(c.start_date)}">
    <span class="k">Publiées depuis (j)</span><input data-f="posted_within_days" type="number" min="1" value="${esc(c.posted_within_days)}">`;
  $("#cContracts").onclick = e => {
    if (e.target.tagName !== "BUTTON") return;
    e.target.classList.toggle("on");
    c.contract_types = chipValues($("#cContracts"));
  };
  $$("#cMeta input[data-f]").forEach(i => (i.oninput = () => {
    c[i.dataset.f] = i.type === "number" ? +i.value : i.value;
  }));
}

function renderTagBox(box, obj, key) {
  obj[key] = obj[key] || [];
  box.innerHTML = obj[key].map((t, i) => `<span class="tag">${esc(t)}<button data-i="${i}" title="Retirer">×</button></span>`).join("")
    + `<input class="tag-input" placeholder="+ ajouter">`;
  $$("button", box).forEach(b => (b.onclick = () => { obj[key].splice(+b.dataset.i, 1); renderTagBox(box, obj, key); }));
  const inp = $("input", box);
  inp.onkeydown = e => {
    if (e.key === "Enter" && inp.value.trim()) {
      e.preventDefault();
      obj[key].push(inp.value.trim());
      renderTagBox(box, obj, key);
      $("input", box).focus();
    }
  };
}

function renderLocations() {
  const c = state.editing.criteria;
  c.locations = c.locations || [];
  const box = $("#cLocations");
  box.innerHTML = c.locations.map((l, i) => `
    <div class="loc"><b>${esc(l.label)}</b>
      <span class="muted small">rayon</span><input type="number" min="0" data-i="${i}" value="${l.radius_km ?? 0}"><span class="muted small">km</span>
      <button class="icon-btn" data-del="${i}" title="Retirer">×</button></div>`).join("")
    + `<input class="tag-input" placeholder="+ ajouter une ville ou région">`;
  $$("input[data-i]", box).forEach(i => (i.oninput = () => (c.locations[+i.dataset.i].radius_km = +i.value)));
  $$("button[data-del]", box).forEach(b => (b.onclick = () => { c.locations.splice(+b.dataset.del, 1); renderLocations(); }));
  const inp = $("input.tag-input", box);
  inp.onkeydown = e => {
    if (e.key === "Enter" && inp.value.trim()) {
      const v = inp.value.trim();
      c.locations.push({ label: v, search_term: v, kind: "city", radius_km: 30, ft_region: "", ft_departement: "", ft_commune: "" });
      renderLocations();
    }
  };
}

async function saveAndRun() {
  const ed = state.editing;
  if (!ed?.criteria) return;
  if (!ed.criteria.search_queries?.length || !ed.criteria.locations?.length) {
    toast("Il faut au moins une requête et une zone géographique.");
    return;
  }
  const body = {
    name: $("#cName").value.trim() || ed.criteria.name || "Ma recherche",
    form: readForm(),
    prompt: $("#fPrompt").value,
    criteria: ed.criteria,
    frequency: chipValues($("#fFrequency"))[0] || "daily",
  };
  try {
    const { id } = ed.id
      ? await api(`/api/searches/${ed.id}`, { method: "PUT", body })
      : await api("/api/searches", { method: "POST", body });
    await api(`/api/searches/${id}/run`, { method: "POST" });
    toast("Recherche lancée : les robots parcourent les sites…");
    await openResults(id);
  } catch (e) {
    toast(e.message, 5000);
  }
}

// ------------------------------------------------------------------ résultats

async function openResults(id) {
  state.currentId = id;
  await loadSearches();
  const s = state.searches.find(x => x.id === id);
  if (!s) return;
  $("#rTitle").textContent = s.name;
  $("#rSummary").textContent = s.criteria.summary || "";
  $("#rMeta").textContent = (s.frequency === "daily" ? "Mise à jour quotidienne" : "Mise à jour à la demande")
    + (s.last_run ? ` · dernière exécution ${new Date(s.last_run).toLocaleString("fr-FR")}` : " · jamais exécutée");
  applyPrefsToFilters(prefs(id));
  showView("viewResults");
  await loadOffers();
  pollStatus();
}

async function loadOffers() {
  state.offers = await api(`/api/searches/${state.currentId}/offers`);
  const sources = new Set();
  state.offers.forEach(o => o.links.forEach(l => sources.add(l.source)));
  const sel = $("#qSource");
  const cur = prefs(state.currentId).source;
  sel.innerHTML = `<option value="">Toutes les sources</option>` +
    [...sources].sort().map(s => `<option ${s === cur ? "selected" : ""}>${esc(s)}</option>`).join("");
  renderOffers();
}

function applyPrefsToFilters(p) {
  setChips($("#qZone"), [p.zone]);
  $("#qZoneThr").value = p.zoneThr;
  $("#qZoneThrWrap").hidden = p.zone !== "flex";
  $("#qMin").value = p.min;
  $("#qMinVal").textContent = p.min;
  $("#qStatus").value = p.status;
  $("#qSort").value = p.sort;
}

function readFilters() {
  const p = {
    zone: chipValues($("#qZone"))[0] || "flex",
    zoneThr: +$("#qZoneThr").value || 0,
    min: +$("#qMin").value,
    status: $("#qStatus").value,
    source: $("#qSource").value,
    sort: $("#qSort").value,
  };
  savePrefs(state.currentId, p);
  $("#qZoneThrWrap").hidden = p.zone !== "flex";
  $("#qMinVal").textContent = p.min;
  return p;
}

function filteredOffers() {
  const p = readFilters();
  const q = $("#qText").value.trim().toLowerCase();
  let list = state.offers.filter(o => {
    if (p.status === "active" && (!["new", "fav"].includes(o.status) || (o.app_status && o.app_status !== "consultee"))) return false;
    if (["fav", "ignored"].includes(p.status) && o.status !== p.status) return false;
    if (p.status === "applied" && !o.app_id) return false;
    if (o.score != null && o.score < p.min) return false;
    if (o.score == null && p.min > 0) return false;
    if (o.in_zone === false) {
      if (p.zone === "strict") return false;
      if ((o.score ?? 0) < p.zoneThr) return false;
    }
    if (p.source && !o.links.some(l => l.source === p.source)) return false;
    if (q) {
      const hay = `${o.title} ${o.company} ${o.location} ${o.analysis?.city || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  if (p.sort === "date") list.sort((a, b) => (b.date_posted || b.first_seen).localeCompare(a.date_posted || a.first_seen));
  else list.sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  return list;
}

function contractBadge(o) {
  const c = o.analysis?.contract || o.contract;
  return c && c !== "Non précisé" ? `<span class="badge contract">${esc(c)}</span>` : "";
}

function renderOffers() {
  const list = filteredOffers();
  const all = state.offers;
  const pending = all.filter(o => o.score == null).length;
  $("#stats").innerHTML = `
    <span class="stat"><b>${list.length}</b> affichées</span>
    ${all.length - list.length ? `<span class="stat"><b>${all.length - list.length}</b> masquées par les filtres</span>` : ""}
    <span class="stat"><b>${all.length}</b> au total</span>
    <span class="stat"><b>${all.filter(o => !o.viewed && o.status !== "ignored").length}</b> non lues</span>
    <span class="stat"><b>${all.filter(o => (o.score ?? 0) >= 75).length}</b> très pertinentes</span>
    <span class="stat"><b>${all.filter(o => o.app_id).length}</b> dans le suivi</span>
    ${pending ? `<span class="stat"><b>${pending}</b> en attente d'analyse · <button class="link" id="scorePending">analyser maintenant</button></span>` : ""}`;
  const sp = $("#scorePending");
  if (sp) sp.onclick = async () => {
    try {
      await api(`/api/searches/${state.currentId}/run?score_only=true`, { method: "POST" });
      toast("Analyse des offres en attente lancée");
      setTimeout(pollStatus, 500);
    } catch (e) { toast(e.message, 5000); }
  };

  $("#cards").innerHTML = list.map(o => {
    const a = o.analysis || {};
    const city = a.city && a.city !== "Non précisé" ? a.city : o.location;
    const sources = [...new Set(o.links.map(l => l.source))];
    return `
    <article class="card ${o.status === "ignored" ? "dim" : ""}" data-id="${o.id}">
      <div class="card-top">
        ${logoAv(o.company, o.logo)}
        <div class="card-title">
          <h4>${esc(o.title)}</h4>
          <div class="company">${esc(o.company || "Entreprise non précisée")} · ${esc(city || "Lieu non précisé")}</div>
        </div>
        ${scoreRing(o.score)}
      </div>
      <div class="badges">
        ${contractBadge(o)}
        ${a.duration && a.duration !== "Non précisé" ? `<span class="badge">${esc(a.duration)}</span>` : ""}
        ${a.start_date && a.start_date !== "Non précisé" ? `<span class="badge">début ${esc(a.start_date)}</span>` : ""}
        ${o.in_zone === false ? `<span class="badge out">Hors zone</span>` : ""}
        ${o.status === "fav" ? `<span class="badge status-fav">★ Favori</span>` : ""}
        ${stBadge(o.app_status)}
      </div>
      ${a.verdict ? `<div class="verdict" style="color:${scoreColor(o.score)}">${esc(a.verdict)}</div>` : `<div class="verdict muted">Analyse en attente</div>`}
      <p class="summary">${esc(a.summary || (o.description || "").slice(0, 220))}</p>
      <div class="card-foot">
        <span>${o.viewed ? "" : `<b style="color:var(--accent)">● Nouveau</b> · `}${esc(sources.join(" · "))} · ${esc(fmtDate(o.date_posted || o.first_seen))}</span>
        <button class="icon-btn ${o.status === "fav" ? "on" : ""}" data-fav="${o.id}" title="Favori">${o.status === "fav" ? "★" : "☆"}</button>
      </div>
    </article>`;
  }).join("");

  const empty = $("#empty");
  empty.hidden = list.length > 0;
  empty.innerHTML = all.length
    ? "Aucune offre ne correspond aux filtres. Essaie la zone « Souple » ou baisse le score minimum."
    : "Aucune offre pour l'instant. Clique sur « Actualiser maintenant » pour lancer la collecte.";
  if (all.length && !list.length && $("#qMin").value > 0) empty.innerHTML += " Les offres pas encore analysées n'ont pas de score : mets le score minimum à 0 pour les voir.";

  $$(".card").forEach(card => (card.onclick = e => {
    const fav = e.target.closest("[data-fav]");
    if (fav) { e.stopPropagation(); toggleStatus(+fav.dataset.fav, "fav"); return; }
    openModal(+card.dataset.id);
  }));
}

async function setStatus(id, status) {
  const o = state.offers.find(x => x.id === id);
  o.status = status;
  await api(`/api/offers/${id}`, { method: "PATCH", body: { status } });
  renderOffers();
  if (!$("#modal").hidden) openModal(id);
}

function toggleStatus(id, status) {
  const o = state.offers.find(x => x.id === id);
  setStatus(id, o.status === status ? "new" : status);
}

// ------------------------------------------------------------------ modal

function list(items) {
  return items?.length ? `<ul>${items.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="muted">Non précisé</p>`;
}

function openModal(id) {
  const o = state.offers.find(x => x.id === id);
  if (!o) return;
  if (!o.viewed) {
    o.viewed = true;
    api(`/api/offers/${id}`, { method: "PATCH", body: { viewed: true } }).then(renderOffers);
  }
  const a = o.analysis || {};
  const apply = o.links.find(l => l.apply) || o.links[0] || { url: o.url };
  const fact = (label, v) => v && v !== "Non précisé" ? `<div class="fact"><small>${label}</small>${esc(v)}</div>` : "";

  $("#modalBody").innerHTML = `
    <div class="m-head">
      ${logoAv(o.company, o.logo, "lg")}
      <div>
        <h2>${esc(o.title)}</h2>
        <div class="company">${esc(o.company || "")} · ${esc(a.city && a.city !== "Non précisé" ? a.city : o.location || "")}</div>
        <div class="badges" style="margin-top:8px">
          ${contractBadge(o)}
          ${o.in_zone === false ? `<span class="badge out">Hors zone</span>` : ""}
          ${a.verdict ? `<span class="badge" style="color:${scoreColor(o.score)}">${esc(a.verdict)}</span>` : ""}
          <span class="badge">publiée ${esc(fmtDate(o.date_posted || o.first_seen))}</span>
        </div>
      </div>
      ${scoreRing(o.score, true)}
    </div>
    <div class="m-body">
      ${o.analysis ? `
      <div class="m-section full facts">
        ${fact("Contrat", a.contract)}${fact("Durée", a.duration)}${fact("Début", a.start_date)}
        ${fact("Niveau", a.education_level)}${fact("Lieu", a.city)}${fact("Télétravail", a.remote)}
      </div>
      <div class="m-section full"><h3>Pourquoi ce score</h3>
        <div class="reasons"><div class="pos">${list(a.match_reasons)}</div><div class="neg">${list(a.mismatch_reasons)}</div></div>
      </div>
      <div class="m-section full"><h3>En bref</h3><p style="margin:0">${esc(a.summary)}</p></div>
      <div class="m-section"><h3>Missions</h3>${list(a.missions)}</div>
      <div class="m-section"><h3>Profil recherché</h3>${list(a.profile)}</div>
      <div class="m-section full cv-box">
        <h3>Adapter mon CV à cette offre <button class="link" id="copyKw">Copier les mots-clés</button></h3>
        <div class="tags">${(a.cv_keywords || []).map(k => `<span class="tag">${esc(k)}</span>`).join("")}</div>
        ${list(a.cv_tips)}
      </div>
      ${a.red_flags?.length ? `<div class="m-section full flags"><h3>Points d'attention</h3>${list(a.red_flags)}</div>` : ""}
      ` : `<div class="m-section full"><p class="muted">Cette offre n'a pas encore été analysée par l'IA : elle le sera à la prochaine actualisation.</p></div>`}
      <div class="m-section full">
        <details class="desc" ${o.analysis ? "" : "open"}><summary>Description complète de l'offre</summary>
          <div class="desc-text">${esc(o.description || "Description non disponible : consulte l'offre sur le site source.")}</div>
        </details>
      </div>
      <div class="m-section full"><h3>Sources</h3>
        <div class="links">${o.links.map(l => `<a href="${esc(safeUrl(l.url))}" target="_blank" rel="noopener noreferrer">${esc(l.source)} ↗</a>`).join("")}</div>
      </div>
    </div>
    <div class="m-foot">
      <div class="left">
        <button class="btn ${o.status === "fav" ? "on" : ""}" data-st="fav">★ Favori</button>
        <button class="btn ${o.status === "ignored" ? "on" : ""}" data-st="ignored">Masquer</button>
        ${o.app_id
          ? `<select class="st-select" id="mAppStatus" style="--st:${APP_STATUS[o.app_status]?.color}">${Object.entries(APP_STATUS).map(([k, m]) => `<option value="${k}" ${k === o.app_status ? "selected" : ""}>${m.label}</option>`).join("")}</select>`
          : `<span class="track-pill">Pas encore dans le suivi</span>`}
      </div>
      <a class="btn primary lg" id="applyLink" href="${esc(safeUrl(apply.url))}" target="_blank" rel="noopener noreferrer">Voir l'offre et candidater ↗</a>
    </div>`;
  $$("[data-st]", $("#modalBody")).forEach(b => (b.onclick = () => toggleStatus(id, b.dataset.st)));
  $("#applyLink").addEventListener("click", async () => {
    await trackOffer(o);
    armConfirm(o.app_id);
  });
  const sel = $("#mAppStatus");
  if (sel) sel.onchange = async () => {
    await api(`/api/applications/${o.app_id}`, { method: "PATCH", body: { status: sel.value } });
    o.app_status = sel.value;
    sel.style.setProperty("--st", APP_STATUS[sel.value].color);
    renderOffers();
    loadApps();
    toast(`Statut : ${APP_STATUS[sel.value].label}`);
  };
  const copy = $("#copyKw");
  if (copy) copy.onclick = () => navigator.clipboard.writeText((a.cv_keywords || []).join(", ")).then(() => toast("Mots-clés copiés"));
  $("#modal").hidden = false;
  document.body.style.overflow = "hidden";
}

async function trackOffer(o) {
  if (o.app_id) return;
  const app = await api("/api/applications", { method: "POST", body: { offer_id: o.id } });
  o.app_id = app.id;
  o.app_status = app.status;
  await loadApps();
  renderOffers();
  if (!$("#modal").hidden) openModal(o.id);
  toast("Ajoutée au suivi des candidatures");
}

// ------------------------------------------------------------------ suivi des candidatures

async function openTracker() {
  stopPolling();
  state.currentId = null;
  showView("viewTracker");
  await loadApps();
  renderTracker();
}

function relDate(iso) {
  return fmtDate(iso);
}

function renderTracker() {
  const apps = state.apps;
  const n = st => apps.filter(a => a.status === st).length;
  const sent = apps.filter(a => a.status !== "consultee" && a.status !== "abandon").length;
  const answered = n("entretien_rh") + n("entretien_tech") + n("retenu") + n("refuse");
  $("#tStats").innerHTML = [
    ["Suivies", apps.length],
    ["Envoyées", sent],
    ["En entretien", n("entretien_rh") + n("entretien_tech")],
    ["Retenues", n("retenu")],
    ["Refusées", n("refuse")],
    ["Taux de réponse", sent ? `${Math.round((100 * answered) / sent)} %` : "–"],
  ].map(([l, v]) => `<div class="kpi"><b>${v}</b><span>${l}</span></div>`).join("");

  $("#tEmpty").hidden = apps.length > 0;
  setChips($("#tView"), [state.trackerView]);
  $("#tKanban").hidden = state.trackerView !== "kanban" || !apps.length;
  $("#tList").hidden = state.trackerView !== "list" || !apps.length;

  // Kanban
  $("#tKanban").innerHTML = Object.entries(APP_STATUS).map(([k, m]) => {
    const items = apps.filter(a => a.status === k);
    return `<div class="kcol" data-st="${k}">
      <div class="kcol-head" title="${m.label}"><span class="st-dot" style="--st:${m.color}"></span>${m.short || m.label}<span class="count">${items.length}</span></div>
      <div class="kcards">${items.map(a => `
        <div class="kcard" draggable="true" data-app="${a.id}" style="--st:${m.color}">
          <h5>${esc(a.title)}</h5>
          <div class="who">${logoAv(a.company, a.logo, "sm")}<span class="company">${esc(a.company || "")}</span></div>
          <div class="meta"><span>${esc(relDate(a.updated_at))}${a.notes ? " · 📝" : ""}</span>
            ${a.score != null ? `<span class="mini-score" style="color:${scoreColor(a.score)}">${a.score}</span>` : ""}</div>
        </div>`).join("")}</div>
    </div>`;
  }).join("");

  $$(".kcard").forEach(c => {
    c.ondragstart = e => { e.dataTransfer.setData("text/plain", c.dataset.app); c.classList.add("dragging"); };
    c.ondragend = () => c.classList.remove("dragging");
    c.onclick = () => openAppModal(+c.dataset.app);
  });
  $$(".kcol").forEach(col => {
    col.ondragover = e => { e.preventDefault(); col.classList.add("over"); };
    col.ondragleave = () => col.classList.remove("over");
    col.ondrop = e => {
      e.preventDefault();
      col.classList.remove("over");
      setAppStatus(+e.dataTransfer.getData("text/plain"), col.dataset.st);
    };
  });

  // Liste
  const opts = cur => Object.entries(APP_STATUS).map(([k, m]) => `<option value="${k}" ${k === cur ? "selected" : ""}>${m.label}</option>`).join("");
  $("#tList").innerHTML = `<table class="apps">
    <thead><tr><th>Offre</th><th>Statut</th><th>Score</th><th>Ajoutée</th><th>Mise à jour</th><th></th></tr></thead>
    <tbody>${apps.map(a => `
      <tr data-app="${a.id}">
        <td><div class="cell-offer">${logoAv(a.company, a.logo, "md")}<div><b>${esc(a.title)}</b><div class="sub">${esc(a.company || "")}${a.location ? " · " + esc(a.location) : ""}</div></div></div></td>
        <td><select class="st-select" data-sel="${a.id}" style="--st:${APP_STATUS[a.status].color}">${opts(a.status)}</select></td>
        <td>${a.score != null ? `<span class="mini-score" style="color:${scoreColor(a.score)}">${a.score}</span>` : "–"}</td>
        <td class="sub">${new Date(a.created_at).toLocaleDateString("fr-FR")}</td>
        <td class="sub">${esc(relDate(a.updated_at))}</td>
        <td>${a.url ? `<a class="link" href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer" data-stop data-open-app="${a.id}">Offre ↗</a>` : ""}</td>
      </tr>`).join("")}</tbody></table>`;
  $$("#tList tr[data-app]").forEach(tr => (tr.onclick = e => {
    if (e.target.closest("select, [data-stop]")) return;
    openAppModal(+tr.dataset.app);
  }));
  $$("#tList select[data-sel]").forEach(s => (s.onchange = () => setAppStatus(+s.dataset.sel, s.value)));
  $$("[data-open-app]").forEach(l => l.addEventListener("click", () => armConfirm(+l.dataset.openApp)));
}

async function setAppStatus(id, status) {
  const a = state.apps.find(x => x.id === id);
  if (!a || a.status === status) return;
  const upd = await api(`/api/applications/${id}`, { method: "PATCH", body: { status } });
  Object.assign(a, upd);
  state.apps.sort((x, y) => y.updated_at.localeCompare(x.updated_at));
  renderTracker();
  toast(`${a.title.slice(0, 40)} → ${APP_STATUS[status].label}`);
}

function openAppModal(id) {
  const a = state.apps.find(x => x.id === id);
  if (!a) return;
  $("#modalBody").innerHTML = `
    <div class="m-head">
      ${logoAv(a.company, a.logo, "lg")}
      <div><h2>${esc(a.title)}</h2>
        <div class="company">${esc(a.company || "")}${a.location ? " · " + esc(a.location) : ""}</div>
        <div class="badges" style="margin-top:8px">${stBadge(a.status)}<span class="badge">suivie depuis le ${new Date(a.created_at).toLocaleDateString("fr-FR")}</span></div>
      </div>
      ${scoreRing(a.score, true)}
    </div>
    <div class="m-pad">
      <div><h3>Statut</h3>
        <div class="stepper">${Object.entries(APP_STATUS).map(([k, m]) =>
          `<button data-set="${k}" class="${k === a.status ? "on" : ""}" style="--st:${m.color}"><span class="st-dot" style="--st:${m.color}"></span>${m.label}</button>`).join("")}</div>
      </div>
      <div><h3>Notes</h3>
        <textarea id="appNotes" placeholder="Contact RH, date d'entretien, questions posées, relance prévue…">${esc(a.notes)}</textarea>
      </div>
      <div><h3>Historique</h3>
        <ul class="timeline">${[...a.history].reverse().map(h => `
          <li style="--st:${APP_STATUS[h.status]?.color || "var(--muted)"}">${esc(APP_STATUS[h.status]?.label || h.status)}
            <small>${new Date(h.at).toLocaleString("fr-FR", { dateStyle: "medium", timeStyle: "short" })}</small></li>`).join("")}</ul>
      </div>
    </div>
    <div class="m-foot">
      <div class="left"><button class="btn ghost danger" id="appDelete">Retirer du suivi</button></div>
      ${a.url ? `<a class="btn primary" id="appOpen" href="${esc(safeUrl(a.url))}" target="_blank" rel="noopener noreferrer">Ouvrir l'offre ↗</a>` : ""}
    </div>`;
  $("#appOpen")?.addEventListener("click", () => armConfirm(id));
  $$("[data-set]", $("#modalBody")).forEach(b => (b.onclick = async () => {
    await setAppStatus(id, b.dataset.set);
    openAppModal(id);
  }));
  const notes = $("#appNotes");
  notes.onblur = async () => {
    if (notes.value === a.notes) return;
    Object.assign(a, await api(`/api/applications/${id}`, { method: "PATCH", body: { notes: notes.value } }));
    renderTracker();
    toast("Notes enregistrées");
  };
  $("#appDelete").onclick = async () => {
    if (!confirm("Retirer cette candidature du suivi ?")) return;
    await api(`/api/applications/${id}`, { method: "DELETE" });
    closeModal();
    await loadApps();
    renderTracker();
  };
  $("#modal").hidden = false;
  document.body.style.overflow = "hidden";
}

function openAddAppModal() {
  $("#modalBody").innerHTML = `
    <div class="m-pad">
      <h2>Ajouter une candidature</h2>
      <p class="muted">Pour suivre une candidature trouvée en dehors de JobRadar.</p>
      <label class="field"><span>Intitulé du poste</span><input id="naTitle"></label>
      <div class="row-2">
        <label class="field"><span>Entreprise</span><input id="naCompany"></label>
        <label class="field"><span>Lieu</span><input id="naLocation"></label>
      </div>
      <label class="field"><span>Lien de l'offre</span><input id="naUrl" placeholder="https://"></label>
      <label class="field"><span>Statut</span><select id="naStatus">${Object.entries(APP_STATUS).map(([k, m]) => `<option value="${k}" ${k === "envoyee" ? "selected" : ""}>${m.label}</option>`).join("")}</select></label>
    </div>
    <div class="m-foot"><span></span><button class="btn primary" id="naSave">Ajouter au suivi</button></div>`;
  $("#naSave").onclick = async () => {
    const title = $("#naTitle").value.trim();
    if (!title) { toast("Indique au moins l'intitulé du poste"); return; }
    await api("/api/applications", { method: "POST", body: {
      title, company: $("#naCompany").value.trim(), location: $("#naLocation").value.trim(),
      url: $("#naUrl").value.trim(), status: $("#naStatus").value,
    } });
    closeModal();
    await loadApps();
    renderTracker();
  };
  $("#modal").hidden = false;
  document.body.style.overflow = "hidden";
  $("#naTitle").focus();
}

// ------------------------------------------------------------------ confirmation au retour sur JobRadar

// Après « Voir l'offre », on mémorise la candidature ; quand l'onglet JobRadar redevient visible,
// on demande si la candidature a été envoyée.
function armConfirm(appId) {
  const a = state.apps.find(x => x.id === appId);
  if (!a || a.status !== "consultee") return;
  state.pendingConfirm = { appId, at: Date.now() };
}

function maybeAskConfirm() {
  const p = state.pendingConfirm;
  if (!p || document.visibilityState !== "visible" || Date.now() - p.at < 1500) return;
  state.pendingConfirm = null;
  const a = state.apps.find(x => x.id === p.appId);
  if (!a || a.status !== "consultee") return;
  const box = $("#confirm");
  box.innerHTML = `
    <div class="c-top">${logoAv(a.company, a.logo, "md")}
      <div><p class="c-title">As-tu envoyé ta candidature ?</p>
      <p class="c-sub">${esc(a.title)}${a.company ? " · " + esc(a.company) : ""}</p></div></div>
    <div class="c-actions">
      <button class="btn ghost" data-c="later">Pas encore</button>
      <button class="btn primary" data-c="sent">Oui, c'est envoyé</button>
    </div>`;
  box.hidden = false;
  $$("[data-c]", box).forEach(b => (b.onclick = async () => {
    box.hidden = true;
    if (b.dataset.c !== "sent") { toast("L'offre reste dans « Offre consultée »"); return; }
    await setAppStatus(a.id, "envoyee");
    const o = state.offers.find(x => x.id === a.offer_id);
    if (o) { o.app_status = "envoyee"; renderOffers(); }
    if (!$("#modal").hidden && o) openModal(o.id);
    else if (!$("#modal").hidden) openAppModal(a.id);
  }));
}
document.addEventListener("visibilitychange", maybeAskConfirm);
window.addEventListener("focus", maybeAskConfirm);

// ------------------------------------------------------------------ thème

function currentTheme() {
  return document.documentElement.dataset.theme
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
$("#themeBtn").onclick = () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("jr-theme", next); } catch {}
};

function closeModal() {
  $("#modal").hidden = true;
  document.body.style.overflow = "";
}
$$("[data-close]").forEach(el => (el.onclick = closeModal));
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

// ------------------------------------------------------------------ suivi d'exécution

function stopPolling() {
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

async function pollStatus() {
  stopPolling();
  let st;
  try { st = await api("/api/status"); } catch { return; }
  const mine = st.search_id === state.currentId;
  const box = $("#progress");
  box.hidden = !(mine && (st.running || st.phase === "error"));
  $("#runBtn").disabled = st.running;
  if (mine) {
    $("#progressMsg").textContent = st.phase === "error" ? `Erreur : ${st.message}` : st.message;
    $("#progressBar").style.width = `${Math.round(st.progress * 100)}%`;
    $("#progressLog").textContent = st.log.join("\n");
  }
  if (st.running) {
    if (mine && st.phase === "score") loadOffers(); // les cartes apparaissent au fil de l'analyse
    state.pollTimer = setTimeout(pollStatus, 2000);
    state.wasRunning = true;
  } else if (state.wasRunning) {
    state.wasRunning = false;
    if (mine) { await loadOffers(); loadSearches(); toast("Recherche terminée"); }
  }
}

// ------------------------------------------------------------------ événements

$("#newSearchBtn").onclick = () => openEditor();
$("#trackerNav").onclick = openTracker;
$("#addAppBtn").onclick = openAddAppModal;
$("#tView").addEventListener("change", () => { state.trackerView = chipValues($("#tView"))[0]; renderTracker(); });
$("#analyzeBtn").onclick = analyze;
$("#reanalyzeBtn").onclick = analyze;
$("#saveRunBtn").onclick = saveAndRun;
$("#editBtn").onclick = () => openEditor(state.searches.find(s => s.id === state.currentId));
$("#runBtn").onclick = async () => {
  try {
    await api(`/api/searches/${state.currentId}/run`, { method: "POST" });
    toast("Actualisation lancée");
    setTimeout(pollStatus, 500);
  } catch (e) { toast(e.message, 5000); }
};
$("#logToggle").onclick = () => ($("#progressLog").hidden = !$("#progressLog").hidden);
["#qText", "#qMin", "#qStatus", "#qSource", "#qSort", "#qZoneThr"].forEach(s => ($(s).oninput = renderOffers));
$("#qZone").addEventListener("change", renderOffers);

(async function init() {
  try {
    state.aiLabel = (await api("/api/info")).label;
    $$(".ai-label").forEach(el => (el.textContent = state.aiLabel));
  } catch { state.aiLabel = "L'IA"; }
  await loadSearches();
  if (state.searches.length) openResults(state.searches[0].id);
  else showView("viewWelcome");
})();
