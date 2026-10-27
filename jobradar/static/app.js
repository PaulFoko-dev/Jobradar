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
  followUps: { due: [], upcoming: [] },
  notifs: [],
  drafting: new Set(), // candidatures dont le brouillon de relance est en cours de rédaction
  modalApp: null, // candidature affichée dans la fiche, pour la rafraîchir après une action
};

// Fuseau du navigateur : sert à afficher les dates et à construire les horaires d'entretien.
const BROWSER_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;

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

// Date calendaire « AAAA-MM-JJ » lue en heure locale (new Date("AAAA-MM-JJ") la lirait en UTC).
function ymdDate(ymd) {
  const [y, m, d] = String(ymd).slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d);
}

function fmtYmd(ymd, opts = { day: "2-digit", month: "2-digit" }) {
  if (!ymd) return "";
  const d = ymdDate(ymd);
  return isNaN(d) ? String(ymd) : d.toLocaleDateString("fr-FR", opts);
}

const LONG_DAY = { day: "numeric", month: "long", year: "numeric" };

// Instant ISO (UTC ou avec décalage) affiché dans le fuseau de l'utilisateur.
function fmtDay(iso, opts = LONG_DAY) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleDateString("fr-FR", opts);
}

function fmtTime(iso, timeZone) {
  return new Date(iso).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", timeZone });
}

function fmtAgo(iso) {
  const min = Math.round((Date.now() - new Date(iso)) / 6e4);
  if (isNaN(min)) return "";
  if (min < 1) return "à l'instant";
  if (min < 60) return `il y a ${min} min`;
  if (min < 24 * 60) return `il y a ${Math.round(min / 60)} h`;
  return fmtDate(iso);
}

function plural(n, word) {
  return `${n} ${word}${Math.abs(n) > 1 ? "s" : ""}`;
}

// Champs « date » et « heure » saisis → ISO 8601 avec le décalage du navigateur à cette date (heure d'été comprise).
function localIso(date, time) {
  const d = new Date(`${date}T${time}`);
  if (!date || !time || isNaN(d)) return null;
  const p = n => String(n).padStart(2, "0");
  const off = -d.getTimezoneOffset();
  const abs = Math.abs(off);
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:00`
    + `${off >= 0 ? "+" : "-"}${p(Math.floor(abs / 60))}:${p(abs % 60)}`;
}

// Inverse de localIso : instant ISO → valeurs des champs date et heure, dans le fuseau du navigateur.
function localParts(iso) {
  const d = new Date(iso);
  const p = n => String(n).padStart(2, "0");
  return { date: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`, time: `${p(d.getHours())}:${p(d.getMinutes())}` };
}

// Bouton occupé pendant un appel lent : désactivé (pas de double clic) avec un indicateur.
async function busy(btn, label, fn) {
  if (!btn || btn.disabled) return;
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span> ${esc(label)}`;
  try { return await fn(); } finally {
    if (btn.isConnected) { btn.disabled = false; btn.innerHTML = old; }
  }
}

function copyText(text, done) {
  if (!navigator.clipboard) { toast("Copie impossible ici : sélectionne le texte et copie-le."); return; }
  navigator.clipboard.writeText(text).then(() => toast(done), () => toast("Copie refusée par le navigateur : sélectionne le texte et copie-le."));
}

function lockScroll() {
  document.body.style.overflow = $("#modal").hidden && $("#dialog").hidden && $("#notifPanel").hidden ? "" : "hidden";
}

function showView(id) {
  $$(".view").forEach(v => (v.hidden = v.id !== id));
  $("#trackerNav").classList.toggle("on", id === "viewTracker");
  $("#profileNav")?.classList.toggle("on", id === "viewProfile");
  $("#autoApplyNav").classList.toggle("on", id === "viewAutoApply");
  if (id !== "viewResults") $$("#searchList .search-item").forEach(b => b.classList.remove("on"));
}

function prefs(id) {
  const def = { zone: "flex", zoneThr: 75, min: 40, status: "active", source: "", sort: "score", noNationality: true };
  try { return { ...def, ...JSON.parse(localStorage.getItem(`jr-prefs-${id}`) || "{}") }; } catch { return def; }
}
function savePrefs(id, p) {
  try { localStorage.setItem(`jr-prefs-${id}`, JSON.stringify(p)); } catch {}
}

function applyAIInfo(info) {
  state.info = info;
  state.aiLabel = info.label;
  $$(".ai-label").forEach(el => (el.textContent = info.label));
  $$('[data-ai-provider]').forEach(select => {
    select.value = info.provider;
    for (const option of select.options) {
      option.disabled = info.providers?.[option.value] === false;
    }
  });
  $("#aiProviderStatus").textContent = "Collecte depuis ce PC";
}

async function changeAIProvider(event) {
  const select = event.currentTarget;
  const previous = state.info?.provider;
  $$('[data-ai-provider]').forEach(item => (item.disabled = true));
  $("#analyzeBtn").disabled = true;
  $("#reanalyzeBtn").disabled = true;
  $("#aiProviderStatus").textContent = "Changement en cours…";
  try {
    const info = await api("/api/settings/ai-provider", {
      method: "PUT",
      body: { provider: select.value },
    });
    applyAIInfo({ ...state.info, ...info });
    toast(`Les prochaines analyses utiliseront ${info.label}`);
  } catch (error) {
    if (previous) $$('[data-ai-provider]').forEach(item => (item.value = previous));
    $("#aiProviderStatus").textContent = "Changement impossible";
    toast(error.message, 5000);
  } finally {
    $$('[data-ai-provider]').forEach(item => (item.disabled = false));
    $("#analyzeBtn").disabled = false;
    $("#reanalyzeBtn").disabled = false;
  }
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
  loadAutoApply();
  await loadOffers();
  pollStatus();
}

// ------------------------------------------------------------------ auto-candidature

const AA_STEPS = {
  queued: "en attente", prepare: "rédaction des documents", render: "création des fichiers", apply: "mise en file de l'envoi",
  queued_for_submission: "envoi assisté lancé", manual: "documents prêts, envoi manuel requis",
};

function autoApplyStepLabel(job) {
  if (job.status === "failed") return "arrêtée";
  if (job.status === "queued") return AA_STEPS.queued;
  return AA_STEPS[job.step] || (job.status === "done" ? "terminée" : "en cours");
}

function autoApplyResult(job) {
  const attempt = job.attempt;
  const reason = attempt?.intervention?.message || job.error || "";
  if (job.sent_by_hand) return "Envoyée par toi ✓ (marquée dans le suivi)";
  if (!attempt) return job.status === "failed" ? `Préparation arrêtée : ${reason}` : "Préparation en cours…";
  if (attempt.status === "submitted") return "Envoyée ✓";
  if (attempt.status === "needs_user") return `À finir toi-même : ${reason}`;
  if (["queued", "running"].includes(attempt.status)) return "En cours…";
  if (attempt.status === "manual_open") return "À finir toi-même : envoi manuel ouvert";
  return reason || attempt.status;
}

function autoApplyGroup(job) {
  const attempt = job.attempt;
  if (attempt?.status === "submitted" || job.sent_by_hand) return "sent";
  if (["queued", "running"].includes(job.status) || ["queued", "running"].includes(attempt?.status)) return "running";
  return "todo";
}

function autoApplyItem(job) {
  const attempt = job.attempt;
  return `<li class="aa-${autoApplyGroup(job)}"><span class="aa-state ${esc(job.status)}">${esc(autoApplyStepLabel(job))}</span>
    <b>${esc(job.company || "Entreprise")} — ${esc(job.title || "Poste")}</b>
    ${job.search_name ? `<span class="aa-search muted">${esc(job.search_name)}</span>` : ""}
    <span class="aa-result">${esc(autoApplyResult(job))}</span>
    ${job.supervision?.understanding ? `<span class="aa-ai">IA : ${esc(job.supervision.understanding)}</span>` : ""}
    <button class="link" type="button" data-aa-app="${esc(job.application_id)}">Voir la candidature</button>
    ${job.resumable ? `<button class="link" type="button" data-aa-retry="${attempt.id}">Reprendre</button>` : ""}
    ${job.proofs?.["avant-envoi.png"] ? `<a class="link" target="_blank" rel="noopener" href="${esc(job.proofs["avant-envoi.png"])}">Voir la capture</a>` : ""}
    ${job.proofs?.["confirmation.png"] ? `<a class="link" target="_blank" rel="noopener" href="${esc(job.proofs["confirmation.png"])}">Confirmation</a>` : ""}
    ${job.proofs?.["journal.md"] ? `<a class="link" target="_blank" rel="noopener" href="${esc(job.proofs["journal.md"])}">Journal</a>` : ""}</li>`;
}

function autoApplyActive(recent) {
  return recent.some(job => autoApplyGroup(job) === "running");
}

// Bandeau compact sur la page d'une recherche : le détail vit sur la page « Candidatures auto ».
function renderAutoApply(info) {
  const { settings, today, recent = [] } = info;
  const todo = recent.filter(job => job.resumable).length;
  const running = recent.filter(job => autoApplyGroup(job) === "running").length;
  $("#autoApply").classList.toggle("on", settings.enabled);
  $("#aaBarText").textContent = [
    settings.enabled ? `Auto-candidature active : score ≥ ${settings.min_score}, ${settings.daily_limit} par jour` : "Auto-candidature désactivée",
    today ? `${today} aujourd'hui` : "",
    running ? `${running} en cours` : "",
    todo ? `${todo} à reprendre` : "",
  ].filter(Boolean).join(" · ");
  if (running) {
    clearTimeout(state.autoApplyTimer);
    state.autoApplyTimer = setTimeout(loadAutoApply, 5000);
  }
}

async function loadAutoApply() {
  const id = state.currentId;
  try {
    const info = await api(`/api/searches/${id}/auto-apply`);
    if (id === state.currentId) renderAutoApply(info);
  } catch (e) { $("#aaBarText").textContent = `Auto-candidature indisponible : ${e.message}`; }
}

function renderAutoApplyPage(info) {
  state.autoApplyInfo = info;
  const { searches, recent } = info;
  const today = searches.reduce((n, s) => n + s.today, 0);
  const resumable = recent.filter(job => job.resumable).length;
  const sent = recent.filter(job => autoApplyGroup(job) === "sent").length;
  $("#autoApplyCount").textContent = resumable;
  $("#autoApplyCount").hidden = !resumable;
  $("#aaRetryAll").disabled = !resumable;
  $("#aaRetryAll").textContent = resumable ? `Tout reprendre (${resumable})` : "Tout reprendre";
  $("#aaStatus").textContent = `Aujourd'hui : ${today} candidature${today > 1 ? "s" : ""} automatique${today > 1 ? "s" : ""}`
    + ` · ${sent} envoyée${sent > 1 ? "s" : ""} au total · ${resumable} à reprendre.`;
  $("#aaSearches").innerHTML = searches.length ? searches.map(s => `<li data-aa-search="${s.id}" class="${s.settings.enabled ? "on" : ""}">
      <label class="switch-row">
        <input type="checkbox" role="switch" data-aa-enabled ${s.settings.enabled ? "checked" : ""}>
        <span class="switch-ui" aria-hidden="true"></span>
        <span><b>${esc(s.name)}</b><small class="muted">${s.settings.enabled
          ? `Active · ${s.today} aujourd'hui, encore ${s.remaining_today} possible${s.remaining_today > 1 ? "s" : ""}`
          : "Désactivée : rien n'est préparé ni envoyé sans toi."}</small></span>
      </label>
      <div class="auto-apply-fields">
        <label class="inline">Score ≥ <input data-aa-min type="number" min="0" max="100" step="5" value="${s.settings.min_score}"></label>
        <label class="inline">au plus <input data-aa-limit type="number" min="1" max="50" step="1" value="${s.settings.daily_limit}"> par jour</label>
        <button class="btn sm" type="button" data-aa-save>Enregistrer</button>
      </div></li>`).join("") : `<li class="muted">Crée d'abord une recherche.</li>`;
  $$("[data-aa-search]", $("#aaSearches")).forEach(row => {
    const id = Number(row.dataset.aaSearch), input = $("[data-aa-enabled]", row);
    input.onchange = async () => {
      const wanted = input.checked;
      input.disabled = true;
      try { if (!(await saveAutoApply(id, row, wanted))) input.checked = !wanted; }
      catch (e) { input.checked = !wanted; toast(e.message, 6000); }
      finally { input.disabled = false; }
    };
    $("[data-aa-save]", row).onclick = async event => {
      try { await saveAutoApply(id, row, input.checked, event.currentTarget); } catch (e) { toast(e.message, 6000); }
    };
  });
  renderAutoApplyList();
  if (autoApplyActive(recent) && !$("#viewAutoApply").hidden) {
    clearTimeout(state.autoApplyTimer);
    state.autoApplyTimer = setTimeout(loadAutoApplyPage, 5000);
  }
}

function renderAutoApplyList() {
  const filter = chipValues($("#aaFilter"))[0] || "all";
  const recent = (state.autoApplyInfo?.recent || []).filter(job => filter === "all" || autoApplyGroup(job) === filter);
  $("#aaRecent").innerHTML = recent.map(autoApplyItem).join("");
  $("#aaEmpty").hidden = recent.length > 0;
  $$("[data-aa-app]", $("#aaRecent")).forEach(button => (button.onclick = async () => {
    await openTracker();
    openAppModal(Number(button.dataset.aaApp));
  }));
  $$("[data-aa-retry]", $("#aaRecent")).forEach(button => (button.onclick = async () => {
    try {
      await api(`/api/application-attempts/${button.dataset.aaRetry}/retry`, { method: "POST" });
      toast("Candidature relancée");
    } catch (e) { toast(e.message, 5000); }
    await loadAutoApplyPage();
  }));
}

async function loadAutoApplyPage() {
  try { renderAutoApplyPage(await api("/api/auto-apply")); }
  catch (e) { $("#aaStatus").textContent = `Auto-candidature indisponible : ${e.message}`; }
}

async function openAutoApply() {
  stopPolling();
  state.currentId = null;
  showView("viewAutoApply");
  await loadAutoApplyPage();
}

async function retryAllAutoApply(button) {
  const count = (state.autoApplyInfo?.recent || []).filter(job => job.resumable).length;
  if (!count || !confirm(`Relancer ${count} candidature${count > 1 ? "s" : ""} arrêtée${count > 1 ? "s" : ""} ?\n\nLe robot les reprend une par une ; celles qui aboutissent sont envoyées sans relecture. Les offres closes, déjà envoyées ou à finir sur un site manuel ne sont pas relancées.`)) return;
  const { retried } = await busy(button, "Relance…", () => api("/api/auto-apply/retry", { method: "POST" }));
  toast(retried ? `${retried} candidature${retried > 1 ? "s" : ""} relancée${retried > 1 ? "s" : ""}` : "Aucune candidature à relancer");
  await loadAutoApplyPage();
}

async function saveAutoApply(searchId, row, enabled, button) {
  const minScore = Number($("[data-aa-min]", row).value), dailyLimit = Number($("[data-aa-limit]", row).value);
  if (!Number.isInteger(minScore) || minScore < 0 || minScore > 100) throw new Error("Le score minimal doit être un entier entre 0 et 100.");
  if (!Number.isInteger(dailyLimit) || dailyLimit < 1 || dailyLimit > 50) throw new Error("Le plafond quotidien doit être un entier entre 1 et 50.");
  if (enabled && !confirm(`Activer l'auto-candidature pour cette recherche ?\n\nPour chaque offre notée ${minScore} ou plus (au plus ${dailyLimit} par jour), JobRadar va préparer CV et lettre puis envoyer la candidature automatiquement, sans relecture de ta part. Les offres Indeed et LinkedIn auront seulement leurs documents préparés.`)) {
    return false;
  }
  const run = () => api(`/api/searches/${searchId}/auto-apply`, {
    method: "PUT", body: { enabled, min_score: minScore, daily_limit: dailyLimit },
  });
  const info = button ? await busy(button, "Enregistrement…", run) : await run();
  if (info.scheduled) toast(`${info.scheduled} offre${info.scheduled > 1 ? "s" : ""} confiée${info.scheduled > 1 ? "s" : ""} à l'auto-candidature`);
  else toast(enabled ? "Auto-candidature active" : "Auto-candidature désactivée");
  await loadAutoApplyPage();
  return true;
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
  $("#qNoNationality").checked = p.noNationality;
}

function readFilters() {
  const p = {
    zone: chipValues($("#qZone"))[0] || "flex",
    zoneThr: +$("#qZoneThr").value || 0,
    min: +$("#qMin").value,
    status: $("#qStatus").value,
    source: $("#qSource").value,
    sort: $("#qSort").value,
    noNationality: $("#qNoNationality").checked,
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
    // Les entreprises exclues ne sont visibles que dans leur propre vue, sans filtre de score ni de zone
    // (leurs offres ne sont plus analysées).
    const excluded = o.company_rule === "excluded";
    if (excluded !== (p.status === "excluded")) return false;
    if (excluded) return matchesText(o, q) && (!p.source || o.links.some(l => l.source === p.source));
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
    if (p.noNationality && o.nationality_required) return false;
    return matchesText(o, q);
  });
  if (p.sort === "priority") list.sort((a, b) => a.priority_order - b.priority_order);
  else if (p.sort === "date") list.sort((a, b) => (b.date_posted || b.first_seen).localeCompare(a.date_posted || a.first_seen));
  else list.sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  return list;
}

function matchesText(o, q) {
  return !q || `${o.title} ${o.company} ${o.location} ${o.analysis?.city || ""}`.toLowerCase().includes(q);
}

// Publiée il y a 2 jours au plus : instant exact si la source le donne, sinon date calendaire.
function isFresh(o) {
  if (o.published_at_utc) return Date.now() - new Date(o.published_at_utc) <= 2 * 864e5;
  if (!o.date_posted) return false;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((today - ymdDate(o.date_posted)) / 864e5) <= 2;
}

function deadlineBadge(o) {
  const d = o.deadline;
  if (!d?.date || d.state === "absente") return "";
  const tip = `Date limite de candidature : ${fmtYmd(d.date, LONG_DAY)}${d.evidence ? ` — « ${d.evidence} »` : ""}`;
  const badge = (cls, text) => `<span class="badge ${cls}" title="${esc(tip)}">${esc(text)}</span>`;
  if (d.state === "expiree") return badge("dl-past", "Date limite dépassée");
  if (d.state === "aujourdhui") return badge("dl-today", "Clôture aujourd'hui");
  if (d.urgent) return badge("dl-soon", `Bientôt close · ${fmtYmd(d.date)}`);
  return badge("dl", `Clôture le ${fmtYmd(d.date)}`);
}

function alreadyAppliedBadge(o) {
  const x = o.already_applied;
  if (!x) return "";
  const tip = `Candidature « ${x.title} » envoyée le ${fmtDay(x.applied_at)}`;
  return `<span class="badge applied-here" title="${esc(tip)}">Déjà postulé ici</span>`;
}

function companyStar(o) {
  return o.company_rule === "favorite" ? `<span class="fav-co" title="Entreprise favorite" aria-label="Entreprise favorite">★</span> ` : "";
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
    ${all.some(o => o.company_rule === "excluded") ? `<span class="stat"><b>${all.filter(o => o.company_rule === "excluded").length}</b> d'entreprises exclues</span>` : ""}
    ${all.some(o => o.nationality_required) ? `<span class="stat" title="Offres exigeant la nationalité française ou européenne"><b>${all.filter(o => o.nationality_required).length}</b> avec condition de nationalité</span>` : ""}
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
    <article class="card ${o.status === "ignored" || o.deadline?.state === "expiree" ? "dim" : ""}" data-id="${o.id}" tabindex="0">
      <div class="card-top">
        ${logoAv(o.company, o.logo)}
        <div class="card-title">
          <h4>${esc(o.title)}</h4>
          <div class="company">${companyStar(o)}${esc(o.company || "Entreprise non précisée")} · ${esc(city || "Lieu non précisé")}</div>
        </div>
        ${scoreRing(o.score)}
      </div>
      <div class="badges">
        ${deadlineBadge(o)}
        ${isFresh(o) ? `<span class="badge fresh">Nouvelle</span>` : ""}
        ${alreadyAppliedBadge(o)}
        ${o.company_rule === "excluded" ? `<span class="badge dl-past">Entreprise exclue</span>` : ""}
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
        <button class="icon-btn ${o.status === "fav" ? "on" : ""}" data-fav="${o.id}" title="Favori" aria-label="${o.status === "fav" ? "Retirer des favoris" : "Ajouter aux favoris"}" aria-pressed="${o.status === "fav"}">${o.status === "fav" ? "★" : "☆"}</button>
      </div>
    </article>`;
  }).join("");

  const empty = $("#empty");
  empty.hidden = list.length > 0;
  empty.innerHTML = $("#qStatus").value === "excluded"
    ? "Aucune offre d'une entreprise exclue. Pour exclure une entreprise, ouvre l'une de ses offres."
    : all.length
    ? "Aucune offre ne correspond aux filtres. Essaie la zone « Souple » ou baisse le score minimum."
    : "Aucune offre pour l'instant. Clique sur « Actualiser maintenant » pour lancer la collecte.";
  if (all.length && !list.length && $("#qMin").value > 0 && $("#qStatus").value !== "excluded") empty.innerHTML += " Les offres pas encore analysées n'ont pas de score : mets le score minimum à 0 pour les voir.";

  $$(".card").forEach(card => {
    card.onclick = e => {
      const fav = e.target.closest("[data-fav]");
      if (fav) { e.stopPropagation(); toggleStatus(+fav.dataset.fav, "fav"); return; }
      openModal(+card.dataset.id);
    };
    card.onkeydown = e => { if (e.key === "Enter" && e.target === card) openModal(+card.dataset.id); };
  });
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

function deadlineSection(o) {
  const d = o.deadline;
  if (!d?.date || d.state === "absente") return "";
  const left = d.state === "expiree" ? `dépassée depuis ${plural(-d.days_left, "jour")}`
    : d.state === "aujourdhui" ? "c'est aujourd'hui" : `dans ${plural(d.days_left, "jour")}`;
  return `
      <div class="m-section full deadline-box ${d.state === "expiree" ? "past" : d.urgent || d.state === "aujourdhui" ? "urgent" : ""}">
        <h3>Date limite de candidature</h3>
        <p class="dl-date"><b>${esc(fmtYmd(d.date, { weekday: "long", ...LONG_DAY }))}</b> · ${esc(left)}</p>
        ${d.evidence ? `<blockquote>« ${esc(d.evidence)} »</blockquote>` : ""}
        <p class="muted small">Repérée automatiquement dans le texte de l'offre : vérifie-la sur le site avant de t'organiser.</p>
      </div>`;
}

function companyActions(o) {
  if (!o.company) return "";
  const fav = o.company_rule === "favorite";
  const excl = o.company_rule === "excluded";
  return `<div class="co-actions">
          <button class="btn sm ${fav ? "on" : ""}" id="coFav" aria-pressed="${fav}">★ Entreprise favorite</button>
          <button class="btn sm ghost ${excl ? "" : "danger"}" id="coExcl">${excl ? "Ne plus exclure cette entreprise" : "Exclure cette entreprise"}</button>
        </div>`;
}

function appliedWarning(o) {
  const x = o.already_applied;
  if (!x) return "";
  return `<p class="applied-warn" role="note">⚠ Tu as déjà postulé chez <b>${esc(o.company)}</b> le ${esc(fmtDay(x.applied_at))}
        (« ${esc(x.title)} », ${esc(APP_STATUS[x.status]?.label.toLowerCase() || x.status)}).
        <button class="link" id="goApplied">Voir cette candidature</button></p>`;
}

async function setCompanyRule(o, disposition) {
  if (disposition === "excluded") {
    const ok = await askConfirm({
      title: `Exclure « ${o.company} » ?`,
      text: "Ses offres seront masquées de tes résultats, ne seront plus envoyées à l'analyse IA et ne déclencheront plus d'alerte. "
        + "Elles restent en mémoire : tu pourras revenir sur ce choix depuis le filtre « Entreprises exclues ».",
      ok: "Exclure l'entreprise",
      danger: true,
    });
    if (!ok) return;
  }
  try {
    await api("/api/company-rules", { method: "PUT", body: { company: o.company, disposition } });
  } catch (e) { toast(e.message, 5000); return; }
  toast({ favorite: `★ ${o.company} ajoutée aux entreprises favorites`, excluded: `${o.company} est exclue : ses offres sont masquées`,
    neutral: `${o.company} n'a plus de règle particulière` }[disposition]);
  // La règle touche toutes les offres de l'entreprise et leur ordre de priorité : on recharge la liste.
  try { await loadOffers(); } catch (e) { toast(e.message, 5000); return; }
  if (!$("#modal").hidden) openModal(o.id);
}

function openModal(id) {
  const o = state.offers.find(x => x.id === id);
  if (!o) return;
  state.modalApp = null;
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
        <div class="company">${companyStar(o)}${esc(o.company || "")} · ${esc(a.city && a.city !== "Non précisé" ? a.city : o.location || "")}</div>
        <div class="badges" style="margin-top:8px">
          ${deadlineBadge(o)}
          ${isFresh(o) ? `<span class="badge fresh">Nouvelle</span>` : ""}
          ${alreadyAppliedBadge(o)}
          ${o.company_rule === "excluded" ? `<span class="badge dl-past">Entreprise exclue</span>` : ""}
          ${contractBadge(o)}
          ${o.in_zone === false ? `<span class="badge out">Hors zone</span>` : ""}
          ${a.verdict ? `<span class="badge" style="color:${scoreColor(o.score)}">${esc(a.verdict)}</span>` : ""}
          <span class="badge">publiée ${esc(fmtDate(o.date_posted || o.first_seen))}</span>
        </div>
        ${companyActions(o)}
      </div>
      ${scoreRing(o.score, true)}
    </div>
    <div class="m-body">
      ${deadlineSection(o)}
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
      ${appliedWarning(o)}
      <div class="left">
        <button class="btn ${o.status === "fav" ? "on" : ""}" data-st="fav">★ Favori</button>
        <button class="btn ${o.status === "ignored" ? "on" : ""}" data-st="ignored">Masquer</button>
        ${o.app_id
          ? `<select class="st-select" id="mAppStatus" style="--st:${APP_STATUS[o.app_status]?.color}">${Object.entries(APP_STATUS).map(([k, m]) => `<option value="${k}" ${k === o.app_status ? "selected" : ""}>${m.label}</option>`).join("")}</select>`
          : `<span class="track-pill">Pas encore dans le suivi</span>`}
      </div>
      <div class="offer-actions">
        <button class="btn" id="prepareDocsStart" type="button">Préparer CV et lettre</button>
        <button class="btn primary lg" id="applyStart" type="button">Candidater</button>
      </div>
    </div>`;
  $$("[data-st]", $("#modalBody")).forEach(b => (b.onclick = () => toggleStatus(id, b.dataset.st)));
  const coFav = $("#coFav");
  if (coFav) coFav.onclick = () => setCompanyRule(o, o.company_rule === "favorite" ? "neutral" : "favorite");
  const coExcl = $("#coExcl");
  if (coExcl) coExcl.onclick = () => setCompanyRule(o, o.company_rule === "excluded" ? "neutral" : "excluded");
  const goApplied = $("#goApplied");
  if (goApplied) goApplied.onclick = async () => {
    closeModal();
    await openTracker();
    openAppModal(o.already_applied.application_id);
  };
  $("#prepareDocsStart").onclick = async event => {
    const button = event.currentTarget;
    try {
      await busy(button, "Ouverture…", () => trackOffer(o));
      closeModal();
      openApplicationDocuments(o.app_id, true);
    } catch (error) { toast(error.message, 6000); }
  };
  $("#applyStart").onclick = async event => {
    const button = event.currentTarget;
    try {
      await busy(button, "Préparation…", () => trackOffer(o));
      await window.JobRadarApply.open(o.app_id);
    } catch (error) { toast(error.message, 6000); }
  };
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
  if (copy) copy.onclick = () => copyText((a.cv_keywords || []).join(", "), "Mots-clés copiés");
  $("#modal").hidden = false;
  lockScroll();
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
  await Promise.all([loadApps(), loadFollowUps()]);
  renderTracker();
  loadTrackerInsights();
}

let trackerAnalytics = null;

async function loadTrackerInsights() {
  try {
    const [skills, applications] = await Promise.all([
      api("/api/analytics/skills?limit=15"), api("/api/analytics/applications"),
    ]);
    trackerAnalytics = applications;
    renderSkillInsights(skills);
    renderApplicationInsights();
  } catch (error) {
    $("#skillInsights").innerHTML = `<p class="muted small">${esc(error.message)}</p>`;
    $("#applicationInsights").innerHTML = `<p class="muted small">${esc(error.message)}</p>`;
  }
}

function renderSkillInsights(data) {
  const root = $("#skillInsights");
  if (!data.items.length) {
    root.innerHTML = `<p class="muted small">Analyse des offres nécessaire pour afficher les tendances.</p>`;
    return;
  }
  const max = Math.max(...data.items.map(item => item.offers), 1);
  root.innerHTML = data.items.map(item => `<div class="skill-insight">
    <div><b>${esc(item.name)}</b><span>${item.offers} offre${item.offers > 1 ? "s" : ""} · ${item.share} %</span></div>
    <div class="insight-bar"><i style="--w:${Math.round(100 * item.offers / max)}%"></i></div>
  </div>`).join("");
}

function renderApplicationInsights() {
  if (!trackerAnalytics) return;
  const rows = trackerAnalytics[$("#statsDimension").value] || [];
  $("#applicationInsights").innerHTML = rows.length ? `<table><thead><tr><th>Groupe</th><th>Envoyées</th><th>Réponses</th><th>Entretiens</th></tr></thead><tbody>${rows.map(row => `<tr>
    <td>${esc(row.label)}</td><td>${row.sent}</td><td>${row.response_rate == null ? "–" : `${row.response_rate} %`}</td><td>${row.interview_rate == null ? "–" : `${row.interview_rate} %`}</td>
  </tr>`).join("")}</tbody></table>` : `<p class="muted small">Pas encore assez de données.</p>`;
}

document.addEventListener("change", event => {
  if (event.target?.id === "statsDimension") renderApplicationInsights();
});

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
          ${a.follow_up?.is_due || a.interviews_count || a.offer_online === false || isCapturing(a.capture_status) ? `<div class="k-flags">
            ${a.follow_up?.is_due ? `<span class="k-flag due">↩ À relancer</span>` : ""}
            ${a.interviews_count ? `<span class="k-flag">📅 ${plural(a.interviews_count, "entretien")}</span>` : ""}
            ${offlineBadge(a, "k-flag gone")}${captureFlag(a)}</div>` : ""}
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
        <td><div class="cell-offer">${logoAv(a.company, a.logo, "md")}<div><b>${esc(a.title)}</b><div class="sub">${esc(a.company || "")}${a.location ? " · " + esc(a.location) : ""}</div>
          ${a.offer_online === false || isCapturing(a.capture_status) ? `<div class="k-flags">${offlineBadge(a, "k-flag gone")}${captureFlag(a)}</div>` : ""}</div></div></td>
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
  scheduleTrackerRefresh();
}

// Captures en cours : le suivi est relu toutes les 4 s (sans gêner un glisser-déposer ni un menu ouvert)
// jusqu'à la fin des captures, pour retirer l'indicateur des cartes.
function scheduleTrackerRefresh() {
  clearTimeout(state.trackerTimer);
  if (!state.apps.some(a => isCapturing(a.capture_status))) return;
  state.trackerTimer = setTimeout(async () => {
    if ($("#viewTracker").hidden) return;
    const busyUi = $(".kcard.dragging") || document.activeElement?.matches?.("#tList select");
    if (busyUi) { scheduleTrackerRefresh(); return; }
    try { await loadApps(); } catch { scheduleTrackerRefresh(); return; }
    if (!$("#viewTracker").hidden) renderTracker();
  }, 4000);
}

async function setAppStatus(id, status) {
  const a = state.apps.find(x => x.id === id);
  if (!a || a.status === status) return;
  let upd;
  try { upd = await api(`/api/applications/${id}`, { method: "PATCH", body: { status } }); } catch (e) { toast(e.message, 5000); return; }
  Object.assign(a, upd);
  state.apps.sort((x, y) => y.updated_at.localeCompare(x.updated_at));
  renderTracker();
  loadFollowUps(); // la date d'envoi et donc la relance dépendent du statut
  toast(`${a.title.slice(0, 40)} → ${APP_STATUS[status].label}`);
}

// État de la relance d'une candidature (texte affiché et actions possibles dans la fiche).
function followUpInfo(a) {
  const fu = a.follow_up || {};
  if (a.follow_up_disabled) return { text: "Rappels de relance désactivés", actions: ["enable"] };
  if (fu.is_due) return { text: `À faire depuis le ${fmtDay(fu.due_at)}`, cls: "due", actions: ["draft", "snooze", "disable"] };
  if (fu.due_at) {
    const snoozed = a.follow_up_snoozed_until_utc && new Date(a.follow_up_snoozed_until_utc) >= new Date(fu.due_at);
    return { text: `${snoozed ? "Reportée au" : "Possible à partir du"} ${fmtDay(fu.due_at)}`, actions: ["draft", "disable"] };
  }
  if (a.status === "relance") return { text: "Relance effectuée", actions: [] };
  if (!a.applied_at) return { text: "Après l'envoi de la candidature", actions: [] };
  return { text: "Plus nécessaire à ce stade", actions: [] };
}

function draftLabel(id) {
  return state.drafting.has(id) ? `<span class="spinner"></span> Rédaction en cours…` : "Rédiger la relance";
}

function followUpButtons(id, actions, cls = "") {
  const b = {
    draft: `<button class="btn sm primary" data-fu="draft" data-draft="${id}" ${state.drafting.has(id) ? "disabled" : ""}>${draftLabel(id)}</button>`,
    snooze: `<button class="btn sm ${cls}" data-fu="snooze" data-id="${id}">Reporter de 3 jours</button>`,
    disable: `<button class="btn sm ghost" data-fu="disable" data-id="${id}">Ne plus rappeler</button>`,
    enable: `<button class="btn sm" data-fu="enable" data-id="${id}">Réactiver les rappels</button>`,
  };
  return actions.map(k => b[k]).join("");
}

function bindFollowUpButtons(root) {
  $$("[data-fu]", root).forEach(btn => (btn.onclick = e => {
    e.stopPropagation();
    const id = +(btn.dataset.draft || btn.dataset.id);
    if (btn.dataset.fu === "draft") draftFollowUp(id);
    else followUpAction(id, btn.dataset.fu, btn);
  }));
}

function openAppModal(id) {
  const a = state.apps.find(x => x.id === id);
  if (!a) return;
  // Réaffichage de la même fiche après une action : on garde la position de défilement.
  const same = state.modalApp === id && !$("#modal").hidden;
  const keep = same ? $("#modal .modal-card").scrollTop : 0;
  const tab = same ? state.appTab || "suivi" : "suivi";
  if (!same) stopArchivePoll();
  state.modalApp = id;
  state.appTab = tab;
  const fu = followUpInfo(a);
  $("#modalBody").innerHTML = `
    <div class="m-head">
      ${logoAv(a.company, a.logo, "lg")}
      <div><h2>${esc(a.title)}</h2>
        <div class="company">${esc(a.company || "")}${a.location ? " · " + esc(a.location) : ""}</div>
        <div class="badges" style="margin-top:8px">${stBadge(a.status)}${offlineBadge(a)}<span class="badge">suivie depuis le ${new Date(a.created_at).toLocaleDateString("fr-FR")}</span></div>
      </div>
      ${scoreRing(a.score, true)}
    </div>
    ${tabsHtml("app", APP_TABS, tab, "Sections de la candidature", "m-tabs")}
    ${tabPanel("app", "suivi", tab, `<div class="m-pad">
      <div><h3>Statut</h3>
        <div class="stepper">${Object.entries(APP_STATUS).map(([k, m]) =>
          `<button data-set="${k}" class="${k === a.status ? "on" : ""}" style="--st:${m.color}"><span class="st-dot" style="--st:${m.color}"></span>${m.label}</button>`).join("")}</div>
      </div>
      <div class="app-facts">
        <div class="fact"><small>Candidature envoyée</small>${a.applied_at ? esc(fmtDay(a.applied_at)) : "Pas encore"}</div>
        <div class="fact ${fu.cls || ""}"><small>Relance</small>${esc(fu.text)}</div>
        ${fu.actions.length ? `<div class="fu-actions">${followUpButtons(id, fu.actions)}</div>` : ""}
      </div>
      <div>
        <div class="sec-head"><h3>Entretiens</h3><div class="sec-actions"><button class="btn sm" id="prepGenerate">Préparer l'entretien</button><button class="btn sm" id="ivAdd">+ Ajouter un entretien</button></div></div>
        <div id="ivForm" hidden></div>
        <div id="ivList" class="iv-list"><p class="muted small">Chargement des entretiens…</p></div>
        <div id="prepList" class="prep-list"><p class="muted small">Chargement des fiches de préparation…</p></div>
      </div>
      <div><div class="sec-head"><h3>Contacts</h3><button class="btn sm" id="contactAdd">+ Ajouter un contact</button></div>
        <div id="contactList" class="contact-list"><p class="muted small">Chargement des contacts…</p></div></div>
      <div><h3>Notes</h3>
        <textarea id="appNotes" aria-label="Notes" placeholder="Contact RH, questions posées, points à préparer…">${esc(a.notes)}</textarea>
      </div>
      <div><h3>Historique</h3>
        <ul class="timeline">${[...a.history].reverse().map(h => `
          <li style="--st:${APP_STATUS[h.status]?.color || "var(--muted)"}">${esc(APP_STATUS[h.status]?.label || h.status)}
            <small>${new Date(h.at).toLocaleString("fr-FR", { dateStyle: "medium", timeStyle: "short" })}</small></li>`).join("")}</ul>
      </div>
    </div>`)}
    ${tabPanel("app", "offre", tab, archiveOfferSkeleton())}
    ${tabPanel("app", "docs", tab, archiveDocsSkeleton())}
    <div class="m-foot">
      <div class="left"><button class="btn ghost danger" id="appDelete">Retirer du suivi</button></div>
      ${a.url ? `<button class="btn primary" id="appApply" type="button">Candidater</button>` : ""}
    </div>`;
  $("#appApply")?.addEventListener("click", () => window.JobRadarApply.open(id));
  bindFollowUpButtons($("#modalBody"));
  $("#ivAdd").onclick = () => openInterviewForm(a);
  loadInterviews(a);
  $("#prepGenerate").onclick = e => generateInterviewPreparation(a, e.currentTarget);
  loadInterviewPreparations(a);
  $("#contactAdd").onclick = () => openContactForm(a);
  loadApplicationContacts(a);
  $$("[data-set]", $("#modalBody")).forEach(b => (b.onclick = async () => {
    await setAppStatus(id, b.dataset.set);
    openAppModal(id);
  }));
  const notes = $("#appNotes");
  notes.onblur = async () => {
    if (notes.value === a.notes) return;
    try {
      Object.assign(a, await api(`/api/applications/${id}`, { method: "PATCH", body: { notes: notes.value } }));
    } catch (e) { toast(e.message, 5000); return; }
    renderTracker();
    toast("Notes enregistrées");
  };
  $("#appDelete").onclick = async () => {
    const ok = await askConfirm({ title: "Retirer cette candidature du suivi ?", text: "Son historique, ses notes et ses entretiens seront supprimés.", ok: "Retirer", danger: true });
    if (!ok) return;
    try { await api(`/api/applications/${id}`, { method: "DELETE" }); } catch (e) { toast(e.message, 5000); return; }
    closeModal();
    await Promise.all([loadApps(), loadFollowUps()]);
    renderTracker();
  };
  bindTabs($(".m-tabs", $("#modalBody")), k => { state.appTab = k; });
  bindArchiveStatic(id);
  $("#modal").hidden = false;
  $("#modal .modal-card").scrollTop = keep;
  lockScroll();
  renderArchive(id); // dernier état connu tout de suite, puis relecture
  refreshArchive(id, { snapshot: !same || state.archive?.id !== id });
}

// Point d'entrée partagé par la fiche d'offre et le dialogue « Candidater ».
// Il ouvre directement l'atelier Documents, sans obliger l'utilisateur à le chercher.
function openApplicationDocuments(id, startPreparation = false) {
  openAppModal(id);
  const tab = $("#app-tab-docs");
  if (!tab) return;
  tab.click();
  if (startPreparation) {
    const application = state.apps.find(item => item.id === id);
    window.JobRadarDocuments?.prepare(application).catch(error => toast(error.message, 6000));
  }
}

function openAddAppModal() {
  state.modalApp = null;
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
    try {
      await api("/api/applications", { method: "POST", body: {
        title, company: $("#naCompany").value.trim(), location: $("#naLocation").value.trim(),
        url: $("#naUrl").value.trim(), status: $("#naStatus").value,
      } });
    } catch (e) { toast(e.message, 5000); return; }
    closeModal();
    await Promise.all([loadApps(), loadFollowUps()]);
    renderTracker();
  };
  $("#modal").hidden = false;
  lockScroll();
  $("#naTitle").focus();
}

// ------------------------------------------------------------------ onglets (fiche de candidature, réglages)

// tabs : [[clé, libellé HTML constant]] ; les panneaux associés sont produits par tabPanel avec le même préfixe.
function tabsHtml(prefix, tabs, current, label, cls = "") {
  return `<div class="tabs ${cls}" role="tablist" aria-label="${esc(label)}">${tabs.map(([k, l]) => `
    <button type="button" role="tab" class="tab" id="${prefix}-tab-${k}" data-tab="${k}" aria-controls="${prefix}-panel-${k}"
      aria-selected="${k === current}" tabindex="${k === current ? 0 : -1}">${l}</button>`).join("")}</div>`;
}

function tabPanel(prefix, k, current, html) {
  return `<div role="tabpanel" class="tabpanel" id="${prefix}-panel-${k}" aria-labelledby="${prefix}-tab-${k}" tabindex="-1" ${k === current ? "" : "hidden"}>${html}</div>`;
}

// Clic, flèches gauche/droite, Début et Fin (motif « onglets » de l'ARIA APG, activation automatique).
function bindTabs(list, onSelect) {
  if (!list) return;
  const tabs = $$('[role="tab"]', list);
  const select = (t, focus) => {
    tabs.forEach(x => {
      const on = x === t;
      x.setAttribute("aria-selected", on);
      x.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(x.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    });
    if (focus) t.focus();
    onSelect?.(t.dataset.tab);
  };
  tabs.forEach((t, i) => {
    t.onclick = () => select(t);
    t.onkeydown = e => {
      const j = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (j === undefined) return;
      e.preventDefault();
      select(tabs[(j + tabs.length) % tabs.length], true);
    };
  });
}

// ------------------------------------------------------------------ archives des candidatures

const APP_TABS = [["suivi", "Suivi"], ["offre", "Offre"], ["docs", "Documents"]];
const CAPTURE_LEVELS = {
  page: "Copie de la page d'origine",
  page_navigateur_dedie: "Copie de la page (navigateur dédié)",
  reconstituee: "Copie reconstituée",
  indisponible: "Copie indisponible",
};
const CAPTURE_HELP = {
  page: "La page de l'offre a été enregistrée telle qu'elle était publiée sur le site.",
  page_navigateur_dedie: "La page, protégée, a été enregistrée avec le navigateur dédié de JobRadar.",
  reconstituee: "La page d'origine n'a pas pu être copiée : le document est reconstitué à partir du texte et de l'analyse de l'offre.",
  indisponible: "Aucune copie n'a pu être faite. Tu peux recapturer plus tard ou joindre ta propre capture.",
};
const MAX_ATTACH_BYTES = 20 * 1024 * 1024; // même limite que le serveur
const DATE_TIME = { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" };

const isCapturing = st => st === "pending" || st === "running";

function fmtSize(n) {
  if (n == null || isNaN(n)) return "";
  if (n < 1024) return `${n} o`;
  const units = ["Ko", "Mo", "Go", "To"];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toLocaleString("fr-FR", { maximumFractionDigits: v < 10 ? 1 : 0 })} ${units[i]}`;
}

function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString("fr-FR", DATE_TIME);
}

function archiveFileUrl(id, path) {
  return `/api/applications/${encodeURIComponent(id)}/archive/file?path=${encodeURIComponent(path)}`;
}

function offlineBadge(a, cls = "badge gone") {
  if (a.offer_online !== false) return "";
  const when = a.offer_checked_at_utc || a.offer_checked_at;
  const tip = `Le lien d'origine ne répond plus${when ? ` (vérifié le ${fmtDateTime(when)})` : ""} : la copie archivée prend le relais.`;
  return `<span class="${cls}" title="${esc(tip)}">Annonce retirée</span>`;
}

function captureFlag(a) {
  return isCapturing(a.capture_status)
    ? `<span class="k-flag cap" title="Copie de l'offre en cours"><span class="spinner dark" aria-hidden="true"></span> Copie de l'offre…</span>` : "";
}

function stopArchivePoll() {
  clearTimeout(state.archiveTimer);
  state.archiveTimer = null;
}

// Actions longues en cours, par candidature : boutons désactivés même si l'affichage est régénéré.
state.arcBusy = new Set();
const arcKey = (id, action) => `${id}:${action}`;

function archiveOfferSkeleton() {
  return `<div class="m-pad arc">
      <div id="arcStatus" aria-live="polite"><p class="muted small">Chargement de l'archive…</p></div>
      <input type="file" id="arcFile" accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg" hidden>
      <div id="arcPreview"></div>
      <div id="arcSnap"></div>
    </div>`;
}

function archiveDocsSkeleton() {
  return `<div class="m-pad arc">
      <div id="applicationDocuments"><p class="muted small">Chargement des documents…</p></div>
      <details class="archive-files"><summary>Autres fichiers du dossier de candidature</summary>
        <div id="arcDocs"><p class="muted small">Chargement des fichiers…</p></div>
      </details>
    </div>`;
}

// Écrit le HTML seulement s'il a changé : l'aperçu PDF n'est pas rechargé à chaque interrogation.
function setHtml(el, html) {
  if (!el || el._html === html) return false;
  el.innerHTML = html;
  el._html = html;
  return true;
}

async function refreshArchive(id, { snapshot = false } = {}) {
  stopArchivePoll();
  const prev = state.archive?.id === id ? state.archive : null;
  let view = null, err = null, snap = prev?.snap, snapErr = prev?.snapErr ?? null;
  try { view = await api(`/api/applications/${id}/archive`); } catch (e) { err = e.message; }
  if (snapshot || !prev) {
    try { snap = await api(`/api/applications/${id}/offer-snapshot`); snapErr = null; } catch (e) { snap = null; snapErr = e.message; }
  }
  if (state.modalApp !== id || $("#modal").hidden) return; // fiche fermée ou remplacée entre-temps
  state.archive = { id, view, err, snap, snapErr };
  syncAppCapture(id, view);
  renderArchive(id);
  if (view && isCapturing(view.capture?.status)) state.archiveTimer = setTimeout(() => refreshArchive(id), 3000);
}

// Reporte l'état de la capture sur la candidature (cartes du suivi) quand il change.
function syncAppCapture(id, view) {
  const a = state.apps.find(x => x.id === id);
  if (!a || !view) return;
  const c = view.capture || {};
  const changed = a.capture_status !== c.status || a.offer_online !== view.offer_online;
  Object.assign(a, { capture_status: c.status, capture_level: c.level, capture_error: c.error, captured_at_utc: c.captured_at,
    offer_online: view.offer_online, offer_checked_at_utc: view.offer_checked_at });
  if (changed && !$("#viewTracker").hidden) renderTracker();
}

function renderArchive(id) {
  const st = state.archive?.id === id ? state.archive : null;
  if (!st || !$("#arcStatus")) return;
  if (!st.view) {
    const msg = `<div class="e-note err" role="alert"><b>Archive indisponible.</b> ${esc(st.err || "")}
      <br><span class="small">Si le dossier existe encore sur le disque, « Réindexer les archives » dans les Réglages le rattache au suivi.</span></div>`;
    setHtml($("#arcStatus"), msg);
    setHtml($("#arcPreview"), "");
    setHtml($("#arcDocs"), msg);
  } else {
    if (setHtml($("#arcStatus"), captureStatusHtml(id, st.view))) bindArchiveActions(id, $("#arcStatus"));
    setHtml($("#arcPreview"), previewHtml(id, st.view));
    if (setHtml($("#arcDocs"), docsHtml(id, st.view))) bindArchiveActions(id, $("#arcDocs"));
  }
  setHtml($("#arcSnap"), snapshotHtml(st));
  const offerTab = $("#app-tab-offre");
  if (offerTab) offerTab.innerHTML = `Offre${st.view && isCapturing(st.view.capture?.status) ? ` <span class="spinner dark" aria-label="capture en cours"></span>` : ""}`;
  const application = state.apps.find(item => item.id === id);
  if (application && window.JobRadarDocuments) {
    window.JobRadarDocuments.renderDocumentsTab(application, st.view?.files || []);
  }
}

function captureStatusHtml(id, view) {
  const c = view.capture || {};
  const running = isCapturing(c.status);
  let cls = "", title, text;
  if (c.status === "pending") {
    cls = "run"; title = "Copie de l'offre en attente"; text = "La capture va démarrer dans quelques secondes.";
  } else if (c.status === "running") {
    cls = "run"; title = "Copie de l'offre en cours…"; text = "JobRadar enregistre la page de l'offre : de quelques secondes à une minute.";
  } else if (c.status === "done") {
    cls = c.level === "indisponible" ? "warn" : c.level === "reconstituee" ? "mid" : "ok";
    title = CAPTURE_LEVELS[c.level] || "Copie enregistrée";
    text = `${CAPTURE_HELP[c.level] || ""}${c.captured_at ? ` Enregistrée le ${fmtDateTime(c.captured_at)}.` : ""}`;
  } else if (c.status === "failed") {
    cls = "err"; title = "La copie de l'offre a échoué"; text = "Tu peux recapturer ou joindre ta propre capture (PDF, PNG ou JPEG).";
  } else {
    title = "Pas encore de copie de l'offre"; text = "Lance une capture pour archiver la page de l'offre.";
  }
  const icon = running ? `<span class="spinner dark" aria-hidden="true"></span>`
    : { ok: "✓", mid: "≈", warn: "!", err: "✕" }[cls] || "•";
  const recapBusy = running || state.arcBusy.has(arcKey(id, "recapture"));
  const attachBusy = state.arcBusy.has(arcKey(id, "attach"));
  return `
    <div class="cap-card ${cls}">
      <span class="cap-ic" aria-hidden="true">${icon}</span>
      <div class="cap-text">
        <b>${esc(title)}</b>
        ${c.error ? `<span class="cap-err">${esc(c.error)}</span>` : ""}
        <span>${esc(text)}</span>
      </div>
    </div>
    ${view.offer_online === false ? `<div class="e-note warn" role="note"><b>Annonce retirée.</b> Le lien d'origine ne répond plus${view.offer_checked_at
      ? ` (vérifié le ${esc(fmtDateTime(view.offer_checked_at))})` : ""} : la copie archivée ci-dessous prend le relais.</div>` : ""}
    <div class="arc-actions">
      <button class="btn sm" data-arc="recapture" ${recapBusy ? "disabled" : ""}
        title="Refait la copie de la page. Peut ouvrir une fenêtre de navigateur si la page est protégée (connexion, vérification anti-robot).">
        ${state.arcBusy.has(arcKey(id, "recapture")) ? `<span class="spinner dark"></span> Demande…` : "↻ Recapturer"}</button>
      <button class="btn sm" data-arc="attach" ${attachBusy ? "disabled" : ""} title="PDF, PNG ou JPEG, 20 Mo au plus">
        ${attachBusy ? `<span class="spinner dark"></span> Envoi…` : "Joindre une capture"}</button>
      <button class="btn sm ghost" data-arc="open">Ouvrir le dossier</button>
    </div>`;
}

function previewHtml(id, view) {
  const files = view.files || [];
  const pdf = files.find(f => f.path === "offre/offre.pdf");
  const png = files.find(f => f.path === "offre/offre.png");
  const mine = files.filter(f => f.path.startsWith("offre/capture-utilisateur-"));
  const capturing = isCapturing(view.capture?.status);
  // Le paramètre « v » change avec le fichier : l'aperçu se recharge après une recapture.
  const pdfUrl = pdf ? `${archiveFileUrl(id, pdf.path)}&v=${encodeURIComponent(pdf.modified_at)}` : "";
  return `
    <div class="sec-head arc-head"><h3>Copie archivée</h3>
      <div class="arc-links">
        ${pdf ? `<a class="btn sm" href="${pdfUrl}" target="_blank" rel="noopener">Ouvrir le PDF ↗</a>` : ""}
        ${png ? `<a class="btn sm ghost" href="${archiveFileUrl(id, png.path)}&v=${encodeURIComponent(png.modified_at)}" target="_blank" rel="noopener">Voir la capture d'écran ↗</a>` : ""}
      </div>
    </div>
    ${pdf ? `<iframe class="pdf-frame" src="${pdfUrl}" title="Aperçu de la copie de l'offre (PDF)"></iframe>`
      : `<p class="arc-empty muted small">${capturing ? "La copie apparaîtra ici dès la fin de la capture." : "Aucune copie PDF de l'offre pour l'instant."}</p>`}
    ${mine.length ? `<div class="arc-mine"><h3>Captures jointes</h3><ul class="file-list">${mine.map(f => fileItem(id, f)).join("")}</ul></div>` : ""}`;
}

function fileItem(id, f) {
  const url = archiveFileUrl(id, f.path);
  const viewable = ["pdf", "png", "jpg"].includes(f.kind);
  const name = f.path.split("/").pop();
  const folder = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/") + 1) : "";
  return `
    <li class="file">
      <span class="file-kind k-${esc(f.kind)}" aria-hidden="true">${esc(String(f.kind).toUpperCase())}</span>
      <div class="file-main">
        <span class="file-name">${folder ? `<span class="muted">${esc(folder)}</span>` : ""}${esc(name)}</span>
        <span class="sub">${esc(fmtSize(f.size))} · ${esc(fmtDateTime(f.modified_at))}</span>
      </div>
      <div class="file-actions">
        ${viewable ? `<a class="btn sm ghost" href="${url}" target="_blank" rel="noopener" aria-label="Ouvrir ${esc(name)}">Ouvrir ↗</a>` : ""}
        <a class="btn sm ghost" href="${url}" download="${esc(name)}" aria-label="Télécharger ${esc(name)}">Télécharger</a>
      </div>
    </li>`;
}

function docsHtml(id, view) {
  const files = view.files || [];
  const total = files.reduce((s, f) => s + (f.size || 0), 0);
  return `
    <div class="sec-head"><h3>Fichiers de l'archive <small>· ${plural(files.length, "fichier")}, ${esc(fmtSize(total))}</small></h3>
      <button class="btn sm" data-arc="open">Ouvrir le dossier</button></div>
    <p class="arc-dir muted small" title="Dossier de la candidature">📁 <code>${esc(view.dir)}</code></p>
    ${files.length ? `<ul class="file-list">${files.map(f => fileItem(id, f)).join("")}</ul>`
      : `<p class="muted small">Le dossier est vide pour l'instant.</p>`}`;
}

function snapshotHtml(st) {
  const s = st.snap;
  if (!s) {
    return `<div class="arc-snap"><h3>Texte de l'offre</h3><p class="muted small">${st.snapErr
      ? `${esc(st.snapErr)} Les candidatures ajoutées à la main avant cette version n'ont pas d'instantané.`
      : "Chargement…"}</p></div>`;
  }
  const a = s.analysis || {};
  const fact = (label, v) => v && v !== "Non précisé" ? `<div class="fact"><small>${label}</small>${esc(v)}</div>` : "";
  const known = v => v && v !== "Non précisé" ? v : "";
  const facts = fact("Contrat", known(a.contract) || s.contract) + fact("Lieu", known(a.city) || s.location) + fact("Durée", a.duration)
    + fact("Début", a.start_date) + fact("Publiée le", s.date_posted ? fmtYmd(s.date_posted, LONG_DAY) : "")
    + fact("Date limite", s.application_deadline_date ? fmtYmd(s.application_deadline_date, LONG_DAY) : "");
  const link = s.url && safeUrl(s.url) !== "#" ? `<a class="link" href="${esc(safeUrl(s.url))}" target="_blank" rel="noopener noreferrer">Lien d'origine ↗</a>` : "";
  return `
    <div class="arc-snap">
      <div class="sec-head"><h3>L'offre au moment de la candidature</h3>
        <span class="muted small">Instantané du ${esc(fmtDateTime(s.taken_at_utc))}${link ? " · " : ""}${link}</span></div>
      ${facts ? `<div class="facts">${facts}</div>` : ""}
      ${s.analysis ? `
        ${a.summary ? `<div><h3>En bref</h3><p class="snap-p">${esc(a.summary)}</p></div>` : ""}
        <div class="snap-grid">
          <div><h3>Missions</h3>${list(a.missions)}</div>
          <div><h3>Profil recherché</h3>${list(a.profile)}</div>
        </div>
        ${a.match_reasons?.length || a.mismatch_reasons?.length ? `<div><h3>Pourquoi ce score${s.score != null ? ` · ${esc(s.score)}` : ""}</h3>
          <div class="reasons"><div class="pos">${list(a.match_reasons)}</div><div class="neg">${list(a.mismatch_reasons)}</div></div></div>` : ""}
        ${a.red_flags?.length ? `<div class="flags"><h3>Points d'attention</h3>${list(a.red_flags)}</div>` : ""}` : ""}
      ${s.description ? `<details class="desc" ${s.analysis ? "" : "open"}><summary>Texte complet de l'offre</summary>
        <div class="desc-text">${esc(s.description)}</div></details>`
        : `<p class="muted small">Le texte de l'offre n'a pas été conservé dans l'instantané : la copie archivée ci-dessus fait foi.</p>`}
    </div>`;
}

// Éléments fixes de l'onglet Offre (sélecteur de fichier) : liés une fois par affichage de la fiche.
function bindArchiveStatic(id) {
  const input = $("#arcFile");
  if (input) input.onchange = () => {
    const file = input.files?.[0];
    input.value = "";
    if (file) attachCapture(id, file);
  };
}

function bindArchiveActions(id, root) {
  $$("[data-arc]", root).forEach(b => (b.onclick = () => {
    const act = b.dataset.arc;
    if (act === "recapture") recaptureOffer(id);
    else if (act === "attach") $("#arcFile")?.click();
    else if (act === "open") openArchiveFolder(id, b);
  }));
}

async function withArcBusy(id, action, fn) {
  const key = arcKey(id, action);
  if (state.arcBusy.has(key)) return;
  state.arcBusy.add(key);
  renderArchive(id);
  try { return await fn(); } finally {
    state.arcBusy.delete(key);
    renderArchive(id);
  }
}

async function recaptureOffer(id) {
  await withArcBusy(id, "recapture", async () => {
    try {
      const view = await api(`/api/applications/${id}/archive/recapture`, { method: "POST" });
      if (state.archive?.id === id) state.archive.view = view;
      toast("Nouvelle copie de l'offre demandée");
    } catch (e) { toast(`Recapture impossible : ${e.message}`, 6000); }
  });
  if (state.modalApp === id) refreshArchive(id);
}

function readBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).replace(/^data:[^,]*,/, ""));
    r.onerror = () => reject(new Error("Lecture du fichier impossible."));
    r.readAsDataURL(file);
  });
}

async function attachCapture(id, file) {
  if (!/\.(pdf|png|jpe?g)$/i.test(file.name)) { toast("Seuls les fichiers PDF, PNG et JPEG sont acceptés.", 5000); return; }
  if (file.size > MAX_ATTACH_BYTES) { toast(`Fichier trop lourd (${fmtSize(file.size)}) : 20 Mo au plus.`, 5000); return; }
  if (!file.size) { toast("Le fichier est vide.", 5000); return; }
  await withArcBusy(id, "attach", async () => {
    try {
      const content_base64 = await readBase64(file);
      const res = await api(`/api/applications/${id}/archive/attach`, { method: "POST", body: { filename: file.name, content_base64 } });
      toast(`Capture jointe : ${res.path.split("/").pop()} (${fmtSize(res.size)})`);
    } catch (e) { toast(`Envoi impossible : ${e.message}`, 6000); }
  });
  if (state.modalApp === id) refreshArchive(id);
}

async function openArchiveFolder(id, btn) {
  try {
    const res = await busy(btn, "Ouverture…", () => api(`/api/applications/${id}/archive/open`, { method: "POST" }));
    if (res) toast("Dossier ouvert dans l'explorateur de fichiers");
  } catch (e) { toast(`Ouverture impossible : ${e.message}`, 6000); }
}

// ------------------------------------------------------------------ relances

async function loadFollowUps() {
  try { state.followUps = await api("/api/follow-ups"); } catch (e) { toast(`Relances indisponibles : ${e.message}`, 5000); }
  renderFollowUps();
  return state.followUps;
}

function followUpRow(a, due) {
  const when = due ? `relance due depuis le ${fmtDay(a.follow_up.due_at, { day: "numeric", month: "short" })}`
    : `relance possible le ${fmtDay(a.follow_up.due_at, { weekday: "long", day: "numeric", month: "short" })}`;
  return `
    <div class="fb-row" data-app="${a.id}">
      ${logoAv(a.company, a.logo, "md")}
      <button class="fb-info" data-open="${a.id}" aria-label="Ouvrir la candidature ${esc(a.title)}">
        <b>${esc(a.title)}</b>
        <span class="sub">${a.company ? esc(a.company) + " · " : ""}envoyée le ${esc(fmtDay(a.applied_at, { day: "numeric", month: "short" }))} · ${esc(when)}</span>
      </button>
      <div class="fb-actions">${followUpButtons(a.id, ["draft", "snooze", "disable"])}</div>
    </div>`;
}

function renderFollowUps() {
  const box = $("#tFollow");
  const { due = [], upcoming = [] } = state.followUps;
  box.hidden = !due.length && !upcoming.length;
  if (box.hidden) { box.innerHTML = ""; return; }
  const delay = state.info?.follow_up_delay_days ?? 10;
  box.innerHTML = `
    ${due.length ? `
    <div class="fb-head">
      <h2><span class="fb-ic" aria-hidden="true">↩</span> À relancer <span class="pill">${due.length}</span></h2>
      <p class="muted small">Sans nouvelles depuis ${delay} jours : un message court et poli relance ta candidature.</p>
    </div>
    <div class="fb-list">${due.map(a => followUpRow(a, true)).join("")}</div>` : ""}
    ${upcoming.length ? `
    <div class="fb-head soon"><h3>Bientôt <span class="pill muted-pill">${upcoming.length}</span></h3></div>
    <div class="fb-list">${upcoming.map(a => followUpRow(a, false)).join("")}</div>` : ""}`;
  bindFollowUpButtons(box);
  $$("[data-open]", box).forEach(b => (b.onclick = () => openAppModal(+b.dataset.open)));
}

// Après une action de relance : mise à jour de la candidature partout où elle est affichée.
async function refreshApp(upd) {
  const a = state.apps.find(x => x.id === upd.id);
  if (a) Object.assign(a, upd);
  const o = state.offers.find(x => x.app_id === upd.id);
  if (o) { o.app_status = upd.status; renderOffers(); }
  await loadFollowUps();
  if (!$("#viewTracker").hidden) renderTracker();
  if (state.modalApp === upd.id && !$("#modal").hidden) openAppModal(upd.id);
  loadNotifs();
}

async function followUpAction(id, action, btn) {
  const labels = { snooze: "Report…", disable: "…", enable: "…", sent: "Enregistrement…" };
  const done = { snooze: "Relance reportée de 3 jours", disable: "Plus de rappel de relance pour cette candidature",
    enable: "Rappels de relance réactivés", sent: "Relance enregistrée : statut « Relancée »" };
  let upd;
  try {
    upd = await busy(btn, labels[action], () => api(`/api/applications/${id}/follow-up/${action}`, {
      method: "POST", body: action === "snooze" ? { days: 3 } : undefined,
    }));
  } catch (e) { toast(e.message, 5000); return false; }
  if (!upd) return false;
  toast(done[action]);
  await refreshApp(upd);
  return true;
}

function syncDraftButtons(id) {
  $$(`[data-draft="${id}"]`).forEach(b => { b.disabled = state.drafting.has(id); b.innerHTML = draftLabel(id); });
}

// L'IA met 10 à 60 s : un seul brouillon à la fois par candidature, boutons verrouillés pendant ce temps.
async function draftFollowUp(id) {
  if (state.drafting.has(id)) return;
  state.drafting.add(id);
  syncDraftButtons(id);
  try {
    const draft = await api(`/api/applications/${id}/follow-up/draft`, { method: "POST" });
    openDraftDialog(id, draft);
  } catch (e) {
    toast(`Brouillon impossible : ${e.message}`, 7000);
  } finally {
    state.drafting.delete(id);
    syncDraftButtons(id);
  }
}

function openDraftDialog(id, draft) {
  const a = state.apps.find(x => x.id === id) || state.followUps.due.concat(state.followUps.upcoming).find(x => x.id === id) || {};
  openDialog(`
    <div class="m-pad draft">
      <div>
        <h2 id="dialogTitle">Relance — ${esc(a.title || "candidature")}</h2>
        <p class="muted small">${a.company ? esc(a.company) + " · " : ""}${a.applied_at ? `candidature envoyée le ${esc(fmtDay(a.applied_at))} · ` : ""}brouillon rédigé par ${esc(state.aiLabel)}.
          Relis-le, remplace <b>[Prénom Nom]</b> par ta signature, puis envoie-le depuis ta messagerie.</p>
      </div>
      <label class="field"><span>Objet</span><input id="dSubject"></label>
      <label class="field"><span>Message</span><textarea id="dBody" rows="11"></textarea></label>
    </div>
    <div class="m-foot draft-foot">
      <div class="left">
        <button class="btn" id="dCopySubject">Copier l'objet</button>
        <button class="btn" id="dCopyBody">Copier le message</button>
        <a class="btn" id="dMail">Ouvrir dans ma messagerie ✉</a>
      </div>
      <button class="btn primary" id="dSent">Marquer comme relancée</button>
    </div>`);
  const subject = $("#dSubject");
  const body = $("#dBody");
  // Valeurs posées par propriété : le texte de l'IA n'est jamais interprété comme du HTML.
  subject.value = draft.subject || "";
  body.value = draft.body || "";
  const mail = $("#dMail");
  const syncMail = () => (mail.href = `mailto:?subject=${encodeURIComponent(subject.value)}&body=${encodeURIComponent(body.value)}`);
  subject.oninput = body.oninput = syncMail;
  syncMail();
  $("#dCopySubject").onclick = () => copyText(subject.value, "Objet copié");
  $("#dCopyBody").onclick = () => copyText(body.value, "Message copié");
  $("#dSent").onclick = async e => {
    if (await followUpAction(id, "sent", e.currentTarget)) closeDialog();
  };
  body.focus();
}

// ------------------------------------------------------------------ entretiens

const DURATIONS = [15, 30, 45, 60, 90, 120, 180, 240, 360, 480];
const REMINDERS = [[1440, "1 jour avant"], [120, "2 heures avant"], [60, "1 heure avant"], [15, "15 minutes avant"], [0, "Aucun rappel"]];

function fmtDuration(min) {
  const h = Math.floor(min / 60), m = min % 60;
  return h ? `${h} h${m ? ` ${String(m).padStart(2, "0")}` : ""}` : `${m} min`;
}

async function loadInterviews(a) {
  let items;
  try { items = await api(`/api/applications/${a.id}/interviews`); } catch (e) {
    toast(`Entretiens indisponibles : ${e.message}`, 5000);
    items = [];
  }
  const box = $("#ivList");
  if (!box || state.modalApp !== a.id) return; // la fiche a changé entre-temps
  state.interviews = items;
  // À venir d'abord (chronologique), puis passés, puis annulés.
  const now = Date.now();
  const rank = iv => (iv.status === "cancelled" ? 2 : new Date(iv.ends_at) < now ? 1 : 0);
  items.sort((x, y) => rank(x) - rank(y) || new Date(x.starts_at) - new Date(y.starts_at));
  box.innerHTML = items.length ? items.map(iv => interviewItem(iv, rank(iv))).join("")
    : `<p class="muted small">Aucun entretien prévu. Ajoute-le ici pour l'exporter dans ton agenda et recevoir un rappel.</p>`;
  $$("[data-iv-edit]", box).forEach(b => (b.onclick = () => openInterviewForm(a, items.find(x => x.id === +b.dataset.ivEdit))));
  $$("[data-iv-cancel]", box).forEach(b => (b.onclick = () => cancelInterview(a, items.find(x => x.id === +b.dataset.ivCancel))));
}

function interviewItem(iv, rank) {
  const start = new Date(iv.starts_at);
  const cancelled = iv.status === "cancelled";
  // Fuseau de l'entretien différent de celui du navigateur : on rappelle l'heure sur place.
  const other = iv.timezone && iv.timezone !== BROWSER_TZ
    ? ` <span class="muted">(${esc(fmtTime(iv.starts_at, iv.timezone))} heure de ${esc(iv.timezone.split("/").pop().replace(/_/g, " "))})</span>` : "";
  const url = iv.meeting_url ? safeUrl(iv.meeting_url) : "#";
  return `
    <div class="iv ${cancelled ? "cancelled" : rank === 1 ? "past" : ""}">
      <div class="iv-date" aria-hidden="true"><b>${start.getDate()}</b><span>${esc(start.toLocaleDateString("fr-FR", { month: "short" }))}</span></div>
      <div class="iv-main">
        <b class="iv-title">${esc(iv.title)}</b>
        ${cancelled ? `<span class="badge dl-past">Annulé</span>` : rank === 1 ? `<span class="badge">Passé</span>` : ""}
        <div class="sub">${esc(fmtDay(iv.starts_at, { weekday: "long", ...LONG_DAY }))} · ${esc(fmtTime(iv.starts_at))} – ${esc(fmtTime(iv.ends_at))}
          (${esc(fmtDuration(iv.duration_minutes))})${other}</div>
        ${iv.location || url !== "#" ? `<div class="sub">${iv.location ? `📍 ${esc(iv.location)}` : ""}${iv.location && url !== "#" ? " · " : ""}${url !== "#"
          ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Rejoindre la visio ↗</a>` : ""}</div>` : ""}
        ${iv.notes ? `<p class="iv-notes">${esc(iv.notes)}</p>` : ""}
      </div>
      <div class="iv-actions">
        <a class="btn sm" href="/api/interviews/${iv.id}.ics" download>${cancelled ? "Exporter l'annulation (.ics)" : "Ajouter à mon agenda (.ics)"}</a>
        ${cancelled ? "" : `<button class="btn sm ghost" data-iv-edit="${iv.id}">Modifier</button>
        <button class="btn sm ghost danger" data-iv-cancel="${iv.id}">Annuler</button>`}
      </div>
    </div>`;
}

function openInterviewForm(a, iv = null) {
  const box = $("#ivForm");
  const at = iv ? localParts(iv.starts_at) : { date: "", time: "" };
  const dur = iv?.duration_minutes ?? 60;
  const rem = iv?.reminder_minutes ?? 1440;
  const durations = DURATIONS.includes(dur) ? DURATIONS : [...DURATIONS, dur].sort((x, y) => x - y);
  const reminders = REMINDERS.some(([v]) => v === rem) ? REMINDERS : [...REMINDERS, [rem, `${rem} minutes avant`]];
  box.innerHTML = `
    <form class="iv-form" novalidate>
      <h4>${iv ? "Modifier l'entretien" : "Nouvel entretien"}</h4>
      <div class="row-3">
        <label class="field"><span>Date</span><input type="date" name="date" required value="${esc(at.date)}"></label>
        <label class="field"><span>Heure</span><input type="time" name="time" required value="${esc(at.time)}"></label>
        <label class="field"><span>Durée</span><select name="duration">${durations.map(d => `<option value="${d}" ${d === dur ? "selected" : ""}>${fmtDuration(d)}</option>`).join("")}</select></label>
      </div>
      <label class="field"><span>Titre</span><input name="title" maxlength="200" placeholder="Entretien — ${esc(a.company || a.title)}" value="${esc(iv?.title || "")}"></label>
      <div class="row-2">
        <label class="field"><span>Lieu</span><input name="location" maxlength="300" placeholder="Adresse, étage, interlocuteur…" value="${esc(iv?.location || "")}"></label>
        <label class="field"><span>Lien de visioconférence</span><input name="meeting_url" type="url" maxlength="2000" placeholder="https://" value="${esc(iv?.meeting_url || "")}"></label>
      </div>
      <label class="field"><span>Notes</span><textarea name="notes" rows="3" maxlength="5000" placeholder="Interlocuteurs, points à préparer…">${esc(iv?.notes || "")}</textarea></label>
      <div class="iv-form-foot">
        <label class="inline">Rappel <select name="reminder">${reminders.map(([v, l]) => `<option value="${v}" ${v === rem ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <span class="muted small">Heure de ton fuseau (${esc(BROWSER_TZ)})</span>
        <div class="iv-form-btns"><button type="button" class="btn ghost" data-iv-close>Annuler</button>
        <button type="submit" class="btn primary">${iv ? "Enregistrer" : "Ajouter l'entretien"}</button></div>
      </div>
    </form>`;
  box.hidden = false;
  $("#ivAdd").hidden = true;
  const form = $("form", box);
  const close = () => { box.hidden = true; box.innerHTML = ""; $("#ivAdd").hidden = false; };
  $("[data-iv-close]", form).onclick = close;
  form.onsubmit = async e => {
    e.preventDefault();
    const f = form.elements;
    const starts = localIso(f.date.value, f.time.value);
    if (!starts) { toast("Indique la date et l'heure de l'entretien."); (f.date.value ? f.time : f.date).focus(); return; }
    const url = f.meeting_url.value.trim();
    if (url && safeUrl(url) === "#") { toast("Le lien de visioconférence doit commencer par https:// ou http://"); f.meeting_url.focus(); return; }
    const body = {
      title: f.title.value.trim(), starts_at: starts, duration_minutes: +f.duration.value, timezone: BROWSER_TZ,
      location: f.location.value.trim(), meeting_url: url, notes: f.notes.value.trim(), reminder_minutes: +f.reminder.value,
    };
    try {
      await busy(e.submitter || $("[type=submit]", form), "Enregistrement…", () => iv
        ? api(`/api/interviews/${iv.id}`, { method: "PATCH", body })
        : api(`/api/applications/${a.id}/interviews`, { method: "POST", body }));
    } catch (err) { toast(err.message, 6000); return; }
    toast(iv ? "Entretien modifié : pense à réimporter le .ics dans ton agenda" : "Entretien ajouté");
    close();
    await loadApps();
    if (!$("#viewTracker").hidden) renderTracker();
    loadInterviews(a);
    loadNotifs();
  };
  form.elements.date.focus();
  form.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

async function cancelInterview(a, iv) {
  const ok = await askConfirm({
    title: "Annuler cet entretien ?",
    text: `${iv.title} — ${fmtDay(iv.starts_at, { weekday: "long", ...LONG_DAY })} à ${fmtTime(iv.starts_at)}. `
      + "Il restera visible comme annulé ; si tu l'avais ajouté à ton agenda, exporte ensuite le .ics d'annulation pour l'y retirer.",
    ok: "Annuler l'entretien",
    cancel: "Garder",
    danger: true,
  });
  if (!ok) return;
  try { await api(`/api/interviews/${iv.id}`, { method: "DELETE" }); } catch (e) { toast(e.message, 5000); return; }
  toast("Entretien annulé");
  await loadApps();
  if (!$("#viewTracker").hidden) renderTracker();
  loadInterviews(a);
  loadNotifs();
}

// ------------------------------------------------------------------ préparation d'entretien (étape 4)

async function loadInterviewPreparations(a) {
  let items;
  try { items = await api(`/api/applications/${a.id}/interview-preparations`); } catch (e) {
    toast(`Préparations indisponibles : ${e.message}`, 5000);
    items = [];
  }
  const box = $("#prepList");
  if (!box || state.modalApp !== a.id) return;
  const prep = items[0];
  box.innerHTML = prep ? preparationHtml(prep)
    : `<p class="muted small">La fiche utilisera uniquement l'offre archivée, les documents envoyés et tes réponses de candidature.</p>`;
  $("[data-simulation-start]", box)?.addEventListener("click", e => startInterviewSimulation(a, prep.id, e.currentTarget));
}

function preparationHtml(preparation) {
  const content = preparation.content || {};
  const questions = content.likely_questions || [];
  const points = content.cv_points || [];
  return `<details class="prep-card" open>
    <summary><span><b>Fiche de préparation</b><small>Générée le ${esc(fmtDay(preparation.created_at_utc))}</small></span><span aria-hidden="true">⌄</span></summary>
    <div class="prep-content">
      <section><h4>Questions probables</h4><ol>${questions.map(item => `<li><b>${esc(item.question)}</b><small>${esc(item.category)} · ${esc(item.why)}</small></li>`).join("")}</ol></section>
      ${points.length ? `<section><h4>Points du CV à défendre</h4>${points.map(item => `<article><b>${esc(item.label)}</b><small>Preuve : ${esc(item.source_ref)}</small><ul>${(item.talking_points || []).map(point => `<li>${esc(point)}</li>`).join("")}</ul>${item.watch_out ? `<p class="prep-warning">Attention : ${esc(item.watch_out)}</p>` : ""}</article>`).join("")}</section>` : ""}
      <section><h4>Questions à poser</h4><ul>${(content.questions_to_ask || []).map(item => `<li>${esc(item)}</li>`).join("")}</ul></section>
      <section><h4>Checklist</h4><ul class="prep-check">${(content.checklist || []).map(item => `<li>☐ ${esc(item)}</li>`).join("")}</ul></section>
      <div class="prep-actions"><button class="btn primary" data-simulation-start type="button">Lancer une simulation</button></div>
      ${preparation.markdown_path ? `<p class="muted small">La fiche est aussi enregistrée dans <code>${esc(preparation.markdown_path)}</code>.</p>` : ""}
    </div></details>`;
}

async function generateInterviewPreparation(a, button) {
  const scheduled = (state.interviews || []).find(item => item.status === "scheduled" && new Date(item.ends_at) > new Date());
  try {
    await busy(button, "Préparation…", () => api(`/api/applications/${a.id}/interview-preparations`, {
      method: "POST", body: { interview_id: scheduled?.id || null },
    }));
  } catch (e) { toast(e.message, 7000); return; }
  toast("Fiche de préparation créée");
  loadInterviewPreparations(a);
}

async function startInterviewSimulation(a, preparationId, button) {
  let simulation;
  try {
    simulation = await busy(button, "Démarrage…", () => api(`/api/applications/${a.id}/interview-simulations`, {
      method: "POST", body: { preparation_id: preparationId },
    }));
  } catch (e) { toast(e.message, 7000); return; }
  renderSimulationDialog(simulation);
}

function renderSimulationDialog(simulation) {
  const finished = simulation.status === "completed";
  const question = simulation.questions?.[simulation.current_question];
  const last = simulation.turns?.[simulation.turns.length - 1];
  openDialog(`<div class="sim-dialog">
    <div class="dialog-head"><div><h2>Simulation d'entretien</h2><p class="muted">Question ${Math.min(simulation.current_question + 1, simulation.questions.length)} sur ${simulation.questions.length}</p></div><button class="icon-btn" data-dialog-close aria-label="Fermer">×</button></div>
    ${last ? simulationFeedbackHtml(last.feedback) : ""}
    ${finished ? `<div class="sim-complete"><b>Simulation terminée</b><p>Relis les retours et recommence plus tard pour rendre tes réponses plus naturelles.</p></div>` : `<form id="simAnswer">
      <div class="sim-question"><span class="badge">${esc(question.category)}</span><h3>${esc(question.question)}</h3><p class="muted">${esc(question.why)}</p></div>
      <label class="field"><span>Ta réponse</span><textarea name="answer" rows="8" maxlength="6000" required placeholder="Réponds comme à l'oral. Pour une expérience, pense à Situation, Tâche, Action, Résultat."></textarea></label>
      <div class="dialog-actions"><button class="btn primary" type="submit">Évaluer ma réponse</button></div></form>`}
  </div>`);
  $("[data-dialog-close]", $("#dialogBody")).onclick = closeDialog;
  const form = $("#simAnswer");
  if (!form) return;
  form.onsubmit = async e => {
    e.preventDefault();
    const submit = $("[type=submit]", form);
    try {
      simulation = await busy(submit, "Évaluation…", () => api(`/api/interview-simulations/${simulation.id}/answers`, {
        method: "POST", body: { question_index: simulation.current_question, answer: form.elements.answer.value },
      }));
    } catch (err) { toast(err.message, 7000); return; }
    renderSimulationDialog(simulation);
  };
  form.elements.answer.focus();
}

function simulationFeedbackHtml(feedback) {
  const star = feedback.star || {};
  return `<div class="sim-feedback"><h3>Retour sur ta réponse</h3>
    <div class="sim-scores"><span>Clarté <b>${feedback.clarity}/5</b></span><span>Structure <b>${feedback.structure}/5</b></span><span>Pertinence <b>${feedback.relevance}/5</b></span></div>
    <div class="sim-star" aria-label="Structure STAR">${[["S", star.situation], ["T", star.task], ["A", star.action], ["R", star.result]].map(([label, ok]) => `<span class="${ok ? "ok" : "miss"}">${label}</span>`).join("")}</div>
    <div class="row-2"><div><b>Points forts</b><ul>${(feedback.strengths || []).map(item => `<li>${esc(item)}</li>`).join("")}</ul></div><div><b>À améliorer</b><ul>${(feedback.improvements || []).map(item => `<li>${esc(item)}</li>`).join("")}</ul></div></div>
    <p><b>Structure conseillée :</b> ${esc(feedback.better_structure || "")}</p></div>`;
}

// ------------------------------------------------------------------ contacts de candidature

async function loadApplicationContacts(a) {
  let items = [];
  try { items = await api(`/api/applications/${a.id}/contacts`); } catch (e) { toast(e.message, 5000); }
  const box = $("#contactList");
  if (!box || state.modalApp !== a.id) return;
  box.innerHTML = items.length ? items.map(contact => `<article class="contact-card">
    <div><b>${esc(contact.name)}</b><small>${esc([contact.role, contact.company].filter(Boolean).join(" · "))}</small>
    ${contact.context ? `<p>${esc(contact.context)}</p>` : ""}</div>
    <div class="contact-actions"><button class="btn sm" data-contact-draft="${contact.id}">Rédiger un message</button><button class="btn sm ghost" data-contact-edit="${contact.id}">Modifier</button><button class="btn sm ghost danger" data-contact-delete="${contact.id}">Supprimer</button></div>
  </article>`).join("") : `<p class="muted small">Ajoute manuellement un recruteur, une personne rencontrée ou un ancien de ton école.</p>`;
  $$('[data-contact-draft]', box).forEach(button => button.onclick = () => openContactDraft(items.find(item => item.id === +button.dataset.contactDraft)));
  $$('[data-contact-edit]', box).forEach(button => button.onclick = () => openContactForm(a, items.find(item => item.id === +button.dataset.contactEdit)));
  $$('[data-contact-delete]', box).forEach(button => button.onclick = async () => {
    if (!await askConfirm({ title: "Supprimer ce contact ?", text: "Cette saisie locale sera supprimée.", ok: "Supprimer", danger: true })) return;
    try { await api(`/api/contacts/${button.dataset.contactDelete}`, { method: "DELETE" }); } catch (e) { toast(e.message); return; }
    loadApplicationContacts(a);
  });
}

function openContactForm(a, contact = null) {
  openDialog(`<form id="contactForm" class="contact-form"><div class="dialog-head"><h2>${contact ? "Modifier" : "Ajouter"} un contact</h2><button type="button" class="icon-btn" data-dialog-close>×</button></div>
    <div class="row-2"><label class="field"><span>Nom</span><input name="name" required maxlength="120" value="${esc(contact?.name || "")}"></label><label class="field"><span>Rôle</span><input name="role" maxlength="120" value="${esc(contact?.role || "")}"></label></div>
    <div class="row-2"><label class="field"><span>E-mail</span><input name="email" type="email" maxlength="254" value="${esc(contact?.email || "")}"></label><label class="field"><span>Entreprise</span><input name="company" maxlength="120" value="${esc(contact?.company || a.company || "")}"></label></div>
    <label class="field"><span>Lien public du profil</span><input name="profile_url" type="url" maxlength="2000" value="${esc(contact?.profile_url || "")}" placeholder="https://"></label>
    <label class="field"><span>Contexte</span><textarea name="context" maxlength="1000" rows="3" placeholder="Rencontré au forum école, recommandé par…">${esc(contact?.context || "")}</textarea></label>
    <label class="field"><span>Notes privées</span><textarea name="notes" maxlength="5000" rows="3">${esc(contact?.notes || "")}</textarea></label>
    <div class="dialog-actions"><button class="btn primary" type="submit">Enregistrer</button></div></form>`);
  $("[data-dialog-close]", $("#dialogBody")).onclick = closeDialog;
  $("#contactForm").onsubmit = async e => {
    e.preventDefault(); const f = e.currentTarget.elements;
    const body = Object.fromEntries(["name", "role", "email", "company", "profile_url", "context", "notes"].map(key => [key, f[key].value.trim()]));
    try { await busy($("[type=submit]", e.currentTarget), "Enregistrement…", () => api(contact ? `/api/contacts/${contact.id}` : `/api/applications/${a.id}/contacts`, { method: contact ? "PATCH" : "POST", body })); }
    catch (err) { toast(err.message, 6000); return; }
    closeDialog(); loadApplicationContacts(a);
  };
}

async function openContactDraft(contact) {
  let draft;
  try { draft = await api(`/api/contacts/${contact.id}/draft`, { method: "POST", body: { purpose: "prise_contact" } }); }
  catch (e) { toast(e.message, 7000); return; }
  openDialog(`<div class="contact-draft"><div class="dialog-head"><h2>Brouillon pour ${esc(contact.name)}</h2><button class="icon-btn" data-dialog-close>×</button></div>
    <p class="muted">Aucun message n'a été envoyé.</p><label class="field"><span>Objet</span><input id="contactDraftSubject" value="${esc(draft.subject)}"></label>
    <label class="field"><span>Message</span><textarea id="contactDraftBody" rows="10">${esc(draft.body)}</textarea></label>
    <div class="dialog-actions"><button class="btn" id="copyContactDraft">Copier le message</button></div></div>`);
  $("[data-dialog-close]", $("#dialogBody")).onclick = closeDialog;
  $("#copyContactDraft").onclick = () => copyText($("#contactDraftBody").value, "Message copié");
}

// ------------------------------------------------------------------ fenêtre secondaire

function openDialog(html, onClose = null) {
  state.dialogReturn = document.activeElement;
  state.dialogOnClose = onClose;
  $("#dialogBody").innerHTML = html;
  $("#dialog").hidden = false;
  lockScroll();
}

function closeDialog() {
  if ($("#dialog").hidden) return;
  $("#dialog").hidden = true;
  $("#dialogBody").innerHTML = "";
  lockScroll();
  const cb = state.dialogOnClose;
  state.dialogOnClose = null;
  cb?.();
  if (state.dialogReturn?.isConnected) state.dialogReturn.focus();
}
$$("[data-dclose]").forEach(el => (el.onclick = closeDialog));

// Confirmation dans la charte (au lieu de window.confirm) ; résout à true seulement sur le bouton de validation.
function askConfirm({ title, text, ok = "Confirmer", cancel = "Annuler", danger = false }) {
  return new Promise(resolve => {
    let answer = false;
    openDialog(`
      <div class="m-pad ask">
        <h2 id="dialogTitle">${esc(title)}</h2>
        <p class="muted">${esc(text)}</p>
      </div>
      <div class="m-foot"><span></span><div class="left">
        <button class="btn ghost" data-ask="no">${esc(cancel)}</button>
        <button class="btn primary ${danger ? "danger-fill" : ""}" data-ask="yes">${esc(ok)}</button>
      </div></div>`, () => resolve(answer));
    $$("[data-ask]", $("#dialogBody")).forEach(b => (b.onclick = () => { answer = b.dataset.ask === "yes"; closeDialog(); }));
    $("[data-ask=yes]", $("#dialogBody")).focus();
  });
}

// ------------------------------------------------------------------ notifications

const NOTIF_ICONS = {
  high_score_offer: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/></svg>`,
  follow_up_due: `<svg viewBox="0 0 24 24"><path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>`,
  interview_reminder: `<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>`,
  application_intervention: `<svg viewBox="0 0 24 24"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5M12 17h.01"/></svg>`,
  auto_apply_failed: `<svg viewBox="0 0 24 24"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5M12 17h.01"/></svg>`,
  auto_apply_manual: `<svg viewBox="0 0 24 24"><path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 14l2 2 4-4"/></svg>`,
  application_submitted: `<svg viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg>`,
};

function notifText(n) {
  const p = n.payload || {};
  // Le titre par défaut d'un entretien contient déjà l'entreprise : pas de doublon.
  const who = p.company && !String(p.title || "").includes(p.company) ? [p.title, p.company].filter(Boolean).join(" — ") : p.title || p.company || "";
  if (n.kind === "high_score_offer") return { head: `Offre très pertinente · ${p.score}`, who, extra: p.verdict || (p.reasons || [])[0] || "" };
  if (n.kind === "follow_up_due") return { head: "Relance à faire", who, extra: p.applied_at ? `Candidature envoyée le ${fmtDay(p.applied_at)}` : "" };
  if (n.kind === "interview_reminder") return {
    head: `Entretien ${fmtDay(p.starts_at, { weekday: "long", day: "numeric", month: "long" })} à ${fmtTime(p.starts_at)}`,
    who, extra: p.location || "",
  };
  if (n.kind === "auto_apply_failed") return { head: "Auto-candidature arrêtée", who, extra: p.message || "Une action est nécessaire." };
  if (n.kind === "auto_apply_manual") return { head: "Documents prêts · envoi manuel", who, extra: p.message || "" };
  if (n.kind === "application_intervention") return { head: "Candidature en attente", who, extra: p.message || "Une action est nécessaire." };
  if (n.kind === "application_submitted") return { head: "Candidature envoyée", who, extra: "Les preuves ont été archivées." };
  return { head: "Notification", who, extra: "" };
}

// « dismissed » = lue ou ignorée ; les autres statuts (pending, delivered sur le bureau…) restent à voir.
const isUnread = n => n.status !== "dismissed";

async function loadNotifs() {
  try { state.notifs = await api("/api/notifications"); } catch (e) {
    if (!$("#notifPanel").hidden) toast(`Notifications indisponibles : ${e.message}`, 5000);
    return;
  }
  const pending = state.notifs.filter(isUnread).length;
  const count = $("#notifCount");
  count.textContent = pending;
  count.classList.toggle("hot", pending > 0);
  $("#notifNav").setAttribute("aria-label", pending ? `Notifications (${pending} non lue${pending > 1 ? "s" : ""})` : "Notifications");
  if (!$("#notifPanel").hidden) renderNotifs();
}

function renderNotifs() {
  const items = state.notifs;
  const pending = items.filter(isUnread);
  $("#notifAll").hidden = !pending.length;
  $("#notifList").innerHTML = items.length ? items.map(n => {
    const t = notifText(n);
    const read = !isUnread(n);
    return `
    <div class="notif ${read ? "read" : ""}">
      <span class="n-ic n-${esc(n.kind)}" aria-hidden="true">${NOTIF_ICONS[n.kind] || NOTIF_ICONS.high_score_offer}</span>
      <button class="n-main" data-nopen="${n.id}">
        <b>${esc(t.head)}</b>
        ${t.who ? `<span class="n-who">${esc(t.who)}</span>` : ""}
        ${t.extra ? `<span class="n-extra">${esc(t.extra)}</span>` : ""}
        <small>${esc(fmtAgo(n.created_at))}</small>
      </button>
      ${read ? "" : `<button class="btn sm ghost" data-ndismiss="${n.id}">Ignorer</button>`}
    </div>`;
  }).join("") : `<p class="empty small">Rien de neuf. Les offres très pertinentes, les relances à faire et les entretiens proches apparaîtront ici.</p>`;
  $$("[data-nopen]").forEach(b => (b.onclick = () => openNotif(state.notifs.find(n => n.id === +b.dataset.nopen))));
  $$("[data-ndismiss]").forEach(b => (b.onclick = () => dismissNotif(+b.dataset.ndismiss)));
}

async function openNotifs() {
  $("#notifPanel").hidden = false;
  lockScroll();
  renderNotifs();
  $("#notifPanel .modal-close").focus();
  await loadNotifs();
}

function closeNotifs() {
  if ($("#notifPanel").hidden) return;
  $("#notifPanel").hidden = true;
  lockScroll();
  $("#notifNav").focus();
}

async function dismissNotif(id) {
  try { await api(`/api/notifications/${id}/dismiss`, { method: "POST" }); } catch (e) { toast(e.message, 5000); return; }
  await loadNotifs();
}

// Clic sur une notification : on la marque comme lue puis on ouvre l'offre ou la candidature concernée.
async function openNotif(n) {
  if (!n) return;
  const p = n.payload || {};
  if (isUnread(n)) api(`/api/notifications/${n.id}/dismiss`, { method: "POST" }).then(loadNotifs, e => toast(e.message, 5000));
  closeNotifs();
  if (n.kind === "high_score_offer") {
    if (state.currentId !== p.search_id || $("#viewResults").hidden) await openResults(p.search_id);
    if (state.offers.some(o => o.id === p.offer_id)) openModal(p.offer_id);
    else toast("Cette offre n'existe plus.");
  } else if (n.kind === "application_intervention" || n.kind === "application_submitted") {
    await openTracker();
    if (state.apps.some(a => a.id === p.application_id)) await window.JobRadarApply.open(p.application_id);
    else toast("Cette candidature n'est plus suivie.");
  } else {
    await openTracker();
    if (state.apps.some(a => a.id === p.application_id)) openAppModal(p.application_id);
    else toast("Cette candidature n'est plus suivie.");
  }
}

$("#notifNav").onclick = openNotifs;
$$("[data-nclose]").forEach(el => (el.onclick = closeNotifs));
$("#notifAll").onclick = async e => {
  try { await busy(e.currentTarget, "…", () => api("/api/notifications/dismiss-all", { method: "POST" })); } catch (err) { toast(err.message, 5000); return; }
  await loadNotifs();
};

// ------------------------------------------------------------------ réglages : alertes par e-mail

// Préréglages indicatifs : ils remplissent seulement l'hôte, le port et la sécurité.
const EMAIL_PRESETS = {
  gmail: { label: "Gmail", host: "smtp.gmail.com", port: 587, security: "starttls" },
  outlook: { label: "Outlook / Hotmail", host: "smtp-mail.outlook.com", port: 587, security: "starttls" },
  orange: { label: "Orange", host: "smtp.orange.fr", port: 465, security: "ssl" },
  free: { label: "Free", host: "smtp.free.fr", port: 465, security: "ssl" },
  other: { label: "Autre" },
};
const EMAIL_HELP = {
  gmail: `Gmail refuse ton mot de passe habituel : il faut un <b>mot de passe d'application</b>, disponible seulement si la validation en deux étapes est active sur ton compte Google.
    <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener noreferrer">Créer un mot de passe d'application ↗</a>`,
  outlook: `Les comptes Outlook / Hotmail personnels n'acceptent plus l'envoi par mot de passe, même d'application : Microsoft exige une connexion moderne (OAuth).
    Envoie depuis une autre adresse (Gmail, Orange, Free…) ; tu peux toujours recevoir les alertes sur ton adresse Outlook.`,
  orange: "Identifiant : ton adresse Orange complète ; mot de passe : celui de ta messagerie Orange.",
  free: "Identifiant : ton adresse Free complète ; mot de passe : celui de ta messagerie Free.",
  other: "Renseigne le serveur indiqué par ton fournisseur. JobRadar n'envoie jamais en clair : STARTTLS (port 587) ou SSL (port 465).",
};
const EMAIL_KINDS = [
  ["high_score_offer", "Offres très pertinentes", () => `score ≥ ${state.info?.alert_min_score ?? 85}`],
  ["follow_up_due", "Relances à faire", () => `${state.info?.follow_up_delay_days ?? 10} jours après l'envoi`],
  ["interview_reminder", "Entretiens à venir", () => "dans les 36 h"],
];

function presetOf(host) {
  if (!host) return "";
  return Object.keys(EMAIL_PRESETS).find(k => EMAIL_PRESETS[k].host === host) || "other";
}

function emailSettingsHtml() {
  return `
    <div class="m-pad settings">
      <div>
        <p class="muted small">Après chaque actualisation, un seul e-mail récapitule les nouvelles alertes, envoyé sur ta propre adresse depuis ton compte de messagerie.
          Il contient les intitulés, entreprises, scores, dates et liens des offres, jamais ton CV ni tes notes.</p>
      </div>
      <label class="switch-row">
        <input type="checkbox" role="switch" id="eEnabled">
        <span class="switch-ui" aria-hidden="true"></span>
        <span><b>Activer les alertes par e-mail</b><small class="muted" id="eEnabledHint"></small></span>
      </label>
      <label class="field"><span>Adresse de réception</span>
        <input id="eTo" type="email" autocomplete="email" spellcheck="false" placeholder="prenom.nom@exemple.fr"></label>

      <fieldset class="e-group">
        <legend>Compte d'envoi (SMTP)</legend>
        <label class="field"><span>Fournisseur</span>
          <select id="ePreset">
            <option value="" disabled>Choisis ton fournisseur…</option>
            ${Object.entries(EMAIL_PRESETS).map(([k, p]) => `<option value="${k}">${esc(p.label)}</option>`).join("")}
          </select></label>
        <p id="eHelp" class="e-help" hidden></p>
        <div class="e-server">
          <label class="field e-host"><span>Serveur</span><input id="eHost" spellcheck="false" autocapitalize="off" placeholder="smtp.exemple.fr"></label>
          <label class="field"><span>Port</span><input id="ePort" type="number" min="1" max="65535" inputmode="numeric"></label>
          <label class="field"><span>Sécurité</span>
            <select id="eSecurity"><option value="starttls">STARTTLS</option><option value="ssl">SSL / TLS</option></select></label>
        </div>
        <label class="field"><span>Identifiant</span>
          <input id="eUser" autocomplete="username" spellcheck="false" autocapitalize="off" placeholder="souvent ton adresse e-mail"></label>
        <label class="field"><span>Mot de passe</span>
          <input id="ePass" type="password" autocomplete="new-password" spellcheck="false"></label>
        <div id="ePassInfo" class="e-pass"></div>
        <label class="field"><span>Adresse d'expédition <small class="muted">(facultatif)</small></span>
          <input id="eFrom" type="email" spellcheck="false" placeholder="identique à l'identifiant"></label>
      </fieldset>

      <fieldset class="e-group">
        <legend>Alertes à recevoir</legend>
        <div class="e-kinds">
          ${EMAIL_KINDS.map(([k, label, hint]) => `
            <label class="check"><input type="checkbox" name="eKind" value="${k}">
              <span>${esc(label)} <small class="muted">${esc(hint())}</small></span></label>`).join("")}
        </div>
      </fieldset>
      <div id="eStatus" class="e-status" aria-live="polite"></div>
    </div>
    <div class="m-foot">
      <button class="btn" id="eTest">Envoyer un e-mail de test</button>
      <button class="btn primary" id="eSave">Enregistrer</button>
    </div>`;
}

function syncEmailHelp() {
  const key = $("#ePreset").value;
  const help = $("#eHelp");
  help.hidden = !key;
  help.innerHTML = key ? EMAIL_HELP[key] : "";
  help.classList.toggle("warn", key === "outlook");
}

function renderEmailStatus(s, outcome = null) {
  const pass = $("#ePassInfo");
  const removable = s.password_storage === "keyring" || s.password_storage === "file";
  const remove = removable ? `<p><button class="link" id="eClear">Supprimer le mot de passe enregistré</button></p>` : "";
  const fileWarn = `<div class="e-note warn" role="note"><b>Trousseau du système indisponible.</b> Le mot de passe est conservé dans
    <code>data/secrets.json</code>, lisible seulement par ton compte. Pour mieux le protéger, active un trousseau (GNOME Keyring, KWallet…)
    ou fournis-le par la variable <code>JOBRADAR_SMTP_PASSWORD</code>.</div>`;
  $("#ePass").placeholder = s.password_set ? "•••••••• (enregistré)" : "";
  const keep = s.password_set ? " Laisse le champ vide pour le conserver." : "";
  pass.innerHTML = {
    keyring: `<p class="e-ok">✓ Enregistré dans le trousseau du système, jamais dans config.json.${keep}</p>${remove}`,
    file: fileWarn + remove,
    env: `<p class="e-ok">✓ Fourni par la variable d'environnement <code>JOBRADAR_SMTP_PASSWORD</code>, prioritaire sur ce champ.</p>`,
  }[s.password_storage] || (s.keyring_available
    ? `<p class="muted small">Il sera enregistré dans le trousseau du système, jamais dans config.json.</p>`
    : fileWarn.replace("est conservé", "sera conservé"));
  const clear = $("#eClear");
  if (clear) clear.onclick = () => saveEmailSettings(clear, { clear: true });

  const lines = [];
  if (outcome) lines.push(`<div class="e-note ${outcome.ok ? "ok" : "err"}" role="${outcome.ok ? "status" : "alert"}">${esc(outcome.text)}</div>`);
  if (s.last_error && !(outcome && !outcome.ok && outcome.text === s.last_error)) {
    lines.push(`<div class="e-note err" role="alert"><b>Dernière erreur :</b> ${esc(s.last_error)}</div>`);
  }
  lines.push(`<p class="muted small">${s.last_sent_at
    ? `Dernier envoi réussi : ${esc(fmtDay(s.last_sent_at, { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" }))}`
    : "Aucun e-mail envoyé pour l'instant."}</p>`);
  $("#eStatus").innerHTML = lines.join("");
  if (outcome) $("#eStatus").scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function syncEnabledHint() {
  $("#eEnabledHint").textContent = $("#eEnabled").checked
    ? "Un récapitulatif après chaque actualisation, s'il y a du nouveau"
    : "Aucun e-mail n'est envoyé";
}

function fillEmailForm(s) {
  $("#eEnabled").checked = s.enabled;
  syncEnabledHint();
  $("#eTo").value = s.to;
  $("#eHost").value = s.smtp_host;
  $("#ePort").value = s.smtp_port;
  $("#eSecurity").value = s.security;
  $("#eUser").value = s.username;
  $("#eFrom").value = s.from;
  $("#ePass").value = "";
  $("#ePreset").value = presetOf(s.smtp_host);
  $$("[name=eKind]").forEach(c => (c.checked = s.kinds.includes(c.value)));
  syncEmailHelp();
  renderEmailStatus(s);
}

function readEmailForm() {
  const port = Number($("#ePort").value);
  if ($("#ePort").value === "" || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("Port invalide : nombre entier entre 1 et 65535 (587 pour STARTTLS, 465 pour SSL).");
  }
  const body = {
    enabled: $("#eEnabled").checked,
    to: $("#eTo").value.trim(),
    smtp_host: $("#eHost").value.trim(),
    smtp_port: port,
    security: $("#eSecurity").value,
    username: $("#eUser").value.trim(),
    from: $("#eFrom").value.trim(),
    kinds: $$("[name=eKind]:checked").map(c => c.value),
  };
  if ($("#ePass").value) body.password = $("#ePass").value;
  return body;
}

// Enregistre le formulaire ; renvoie les réglages à jour, ou null (erreur affichée dans la fenêtre).
async function saveEmailSettings(btn, { clear = false, quiet = false } = {}) {
  try {
    const body = clear ? { clear_password: true } : readEmailForm();
    const put = () => api("/api/settings/email", { method: "PUT", body });
    const s = btn ? await busy(btn, clear ? "Suppression…" : "Enregistrement…", put) : await put();
    if (!s) return null; // double clic : l'enregistrement en cours suffit
    state.emailSettings = s;
    $("#ePass").value = "";
    if (clear) { $("#eEnabled").checked = s.enabled; syncEnabledHint(); }
    renderEmailStatus(s);
    if (!quiet) toast(clear ? "Mot de passe supprimé" : "Réglages enregistrés");
    return s;
  } catch (e) {
    renderEmailStatus(state.emailSettings, { ok: false, text: e.message });
    return null;
  }
}

async function testEmail(btn) {
  await busy(btn, "Envoi…", async () => {
    const s = await saveEmailSettings(null, { quiet: true });
    if (!s) return;
    try {
      await api("/api/settings/email/test", { method: "POST" });
      renderEmailStatus(await api("/api/settings/email"),
        { ok: true, text: `E-mail de test envoyé à ${s.to}. Vérifie ta boîte de réception (et les indésirables).` });
    } catch (e) {
      let fresh = s;
      try { fresh = await api("/api/settings/email"); } catch {}
      renderEmailStatus(fresh, { ok: false, text: e.message });
    }
  });
}

const SETTINGS_TABS = [["email", "Alertes par e-mail"], ["browser", "Candidatures"], ["archives", "Archives et sauvegarde"]];

function browserSettingsHtml() {
  return `<div class="m-pad settings"><fieldset class="e-group"><legend>Navigateur de candidature</legend>
    <label>Moteur <select id="bEngine"><option value="system">Navigateur du système (recommandé)</option><option value="chromium">Chromium intégré</option></select></label>
    <p class="muted small" id="bDetected"></p>
    <label>Exécutable (facultatif) <input id="bExecutable" placeholder="Détection automatique"></label>
    <label>Port CDP <input id="bPort" type="number" min="1024" max="65535"></label>
    <button class="btn primary" id="bSave">Enregistrer</button>
    <button class="btn" id="bOpen" title="Ouvre le navigateur avec le profil réservé à JobRadar : connecte-toi une fois à tes comptes, il s'en souviendra.">Ouvrir le navigateur pour me connecter</button>
    <p class="muted small">Ton profil habituel ne peut pas être piloté (protection des navigateurs) : JobRadar utilise un profil à part. Connecte-toi une fois à tes comptes (Google, HelloWork, Workday, Welcome to the Jungle…) dans cette fenêtre. En terminal : <code>python -m jobradar.cli navigateur</code>.</p></fieldset>
    <fieldset class="e-group"><legend>Identifiant par domaine</legend>
    <label>Domaine <input id="lDomain" placeholder="careers.example.com"></label>
    <label>Identifiant <input id="lUsername" autocomplete="username"></label>
    <label>Mot de passe <input id="lPassword" type="password" autocomplete="new-password"></label>
    <button class="btn" id="lSave">Ajouter</button><ul id="lAccounts" class="file-list"></ul></fieldset></div>`;
}

async function loadBrowserSettings() {
  const s = await api("/api/settings/browser");
  $("#bEngine").value = s.engine; $("#bExecutable").value = s.executable || ""; $("#bPort").value = s.cdp_port;
  const found = s.system_browser, standard = s.default_browser;
  $("#bDetected").textContent = !found ? "Aucun navigateur compatible trouvé : JobRadar utilisera son Chromium intégré."
    : found.is_default ? `Navigateur utilisé : ${found.name} (ton navigateur par défaut).`
    : standard ? `${standard} ne peut pas être piloté : JobRadar utilisera ${found.name}.`
    : `Navigateur utilisé : ${found.name}.`;
  const render = accounts => { $("#lAccounts").innerHTML = accounts.map(a => `<li class="file"><span>${esc(a.domain)} — ${esc(a.username)}</span><button class="btn sm ghost" data-domain="${esc(a.domain)}">Supprimer</button></li>`).join("") || `<li class="muted small">Aucun identifiant.</li>`;
    $$("#lAccounts [data-domain]").forEach(b => b.onclick = async () => { await api(`/api/settings/logins/${encodeURIComponent(b.dataset.domain)}`, {method:"DELETE"}); loadBrowserSettings(); }); };
  render(s.accounts);
  $("#bSave").onclick = async () => { await api("/api/settings/browser", {method:"PUT", body:{engine:$("#bEngine").value, executable:$("#bExecutable").value, cdp_port:Number($("#bPort").value)}}); toast("Navigateur enregistré"); };
  $("#bOpen").onclick = async () => {
    const b = $("#bOpen"); b.disabled = true;
    try { const r = await api("/api/settings/browser/open", {method:"POST"}); toast(`${r.name} ouvert : connecte-toi à tes comptes.`); }
    catch (e) { toast(`Navigateur non ouvert : ${e.message}`, 5000); }
    finally { b.disabled = false; }
  };
  $("#lSave").onclick = async () => { await api("/api/settings/logins", {method:"POST", body:{domain:$("#lDomain").value, username:$("#lUsername").value, password:$("#lPassword").value}}); $("#lPassword").value=""; loadBrowserSettings(); };
}

async function openEmailSettings() {
  let s;
  try { s = await api("/api/settings/email"); } catch (e) { toast(`Réglages indisponibles : ${e.message}`, 5000); return; }
  state.emailSettings = s;
  const tab = state.settingsTab || "email";
  openDialog(`
    <div class="settings-head">
      <h2 id="dialogTitle">Réglages</h2>
      ${tabsHtml("set", SETTINGS_TABS, tab, "Sections des réglages")}
    </div>
    ${tabPanel("set", "email", tab, emailSettingsHtml())}
    ${tabPanel("set", "browser", tab, browserSettingsHtml())}
    ${tabPanel("set", "archives", tab, archivesSettingsHtml())}`);
  bindTabs($(".settings-head .tabs"), k => {
    state.settingsTab = k;
    if (k === "archives") loadArchivesSettings();
    if (k === "browser") loadBrowserSettings();
  });
  bindArchivesSettings();
  if (tab === "archives") loadArchivesSettings();
  if (tab === "browser") loadBrowserSettings();
  fillEmailForm(s);
  $("#ePreset").onchange = () => {
    const p = EMAIL_PRESETS[$("#ePreset").value];
    if (p.host) {
      $("#eHost").value = p.host;
      $("#ePort").value = p.port;
      $("#eSecurity").value = p.security;
    }
    syncEmailHelp();
  };
  $("#eHost").oninput = () => { $("#ePreset").value = presetOf($("#eHost").value.trim()); syncEmailHelp(); };
  // Port usuel proposé quand on change de sécurité (sans écraser un port personnalisé).
  $("#eSecurity").onchange = () => {
    const port = $("#ePort").value;
    if (port === "" || port === "587" || port === "465") $("#ePort").value = $("#eSecurity").value === "ssl" ? 465 : 587;
  };
  $("#eEnabled").onchange = syncEnabledHint;
  $("#eSave").onclick = e => saveEmailSettings(e.currentTarget);
  $("#eTest").onclick = e => testEmail(e.currentTarget);
  $(`#set-tab-${tab}`).focus();
}

// ------------------------------------------------------------------ réglages : archives et sauvegarde

function archivesSettingsHtml() {
  return `
    <div class="m-pad settings arch-set">
      <p class="muted small">Chaque candidature a son propre dossier : copie de l'offre, suivi, entretiens, et bientôt CV et lettres.
        Supprimer une recherche, nettoyer la base ou mettre à jour JobRadar ne touche jamais à ces dossiers.</p>
      <div id="aInfo" class="arch-info" aria-live="polite"><p class="muted small"><span class="spinner dark"></span> Lecture du dossier des archives…</p></div>

      <fieldset class="e-group">
        <legend>Exporter le suivi</legend>
        <p class="muted small">Tableau de toutes tes candidatures (statut, dates, relances, entretiens), lisible dans Excel ou LibreOffice.</p>
        <div class="arch-row"><a class="btn" href="/api/export/applications.csv" download>Exporter le suivi (CSV)</a></div>
      </fieldset>

      <fieldset class="e-group">
        <legend>Sauvegarde</legend>
        <p class="muted small">Un fichier ZIP avec la base de données, les réglages et tous les dossiers de candidatures.
          Ton mot de passe de messagerie n'y figure pas. Garde-le en lieu sûr : il contient tes informations personnelles.</p>
        <div class="arch-row"><button class="btn primary" id="aBackup">Créer une sauvegarde</button></div>
        <div id="aBackupResult" aria-live="polite"></div>
        <div>
          <h3 class="arch-sub">Dernières sauvegardes</h3>
          <ul id="aBackups" class="file-list"><li class="muted small">Chargement…</li></ul>
        </div>
      </fieldset>

      <fieldset class="e-group">
        <legend>Réindexer les archives</legend>
        <p class="muted small">Après une réinstallation ou la restauration d'une sauvegarde : rattache au suivi les dossiers de candidatures
          qui n'y figurent plus. Aucun doublon n'est créé et aucun dossier n'est modifié.</p>
        <div class="arch-row"><button class="btn" id="aReindex">Réindexer les archives</button>
          <span id="aReindexResult" class="muted small" aria-live="polite"></span></div>
      </fieldset>
    </div>`;
}

function backupItem(b) {
  const url = `/api/backups/${encodeURIComponent(b.path)}`;
  return `
    <li class="file">
      <span class="file-kind k-zip" aria-hidden="true">ZIP</span>
      <div class="file-main"><span class="file-name">${esc(b.path)}</span>
        <span class="sub">${esc(fmtSize(b.size_bytes))} · ${esc(fmtDateTime(b.created_at))}</span></div>
      <div class="file-actions"><a class="btn sm ghost" href="${url}" download aria-label="Télécharger ${esc(b.path)}">Télécharger</a></div>
    </li>`;
}

async function loadBackups() {
  const box = $("#aBackups");
  let items;
  try { items = await api("/api/backups"); } catch (e) {
    if (box) box.innerHTML = `<li class="e-note err" role="alert">${esc(e.message)}</li>`;
    return;
  }
  if (!$("#aBackups")) return;
  $("#aBackups").innerHTML = items.length ? items.slice(0, 5).map(backupItem).join("")
    + (items.length > 5 ? `<li class="muted small">… et ${plural(items.length - 5, "sauvegarde")} plus ancienne${items.length - 5 > 1 ? "s" : ""}.</li>` : "")
    : `<li class="muted small">Aucune sauvegarde pour l'instant.</li>`;
}

async function loadArchivesInfo() {
  let info;
  try { info = await api("/api/archives/info"); } catch (e) {
    if ($("#aInfo")) $("#aInfo").innerHTML = `<div class="e-note err" role="alert"><b>Archives indisponibles :</b> ${esc(e.message)}</div>`;
    return;
  }
  if (!$("#aInfo")) return;
  const b = info.browser || {};
  $("#aInfo").innerHTML = `
    <div class="arch-stats">
      <div class="fact arch-root"><small>Dossier des archives</small><code>${esc(info.root)}</code></div>
      <div class="fact"><small>Candidatures archivées</small>${esc(info.count)}</div>
      <div class="fact"><small>Taille totale</small>${esc(fmtSize(info.size_bytes))}</div>
    </div>
    <div class="e-note ${b.available ? "ok" : "warn"}" role="note"><b>Navigateur de capture :</b> ${esc(b.detail || (b.available ? "disponible" : "indisponible"))}
      ${b.available ? "" : `<br><span class="small">Sans navigateur, les copies d'offres sont reconstituées à partir du texte.</span>`}</div>`;
}

function loadArchivesSettings() {
  if (state.archSettingsLoaded === $("#aInfo")) return; // déjà chargé pour cette fenêtre
  state.archSettingsLoaded = $("#aInfo");
  loadArchivesInfo();
  loadBackups();
}

function bindArchivesSettings() {
  $("#aBackup").onclick = async e => {
    let res;
    try { res = await busy(e.currentTarget, "Création de la sauvegarde…", () => api("/api/backup", { method: "POST" })); } catch (err) {
      $("#aBackupResult").innerHTML = `<div class="e-note err" role="alert"><b>Sauvegarde impossible :</b> ${esc(err.message)}</div>`;
      return;
    }
    if (!res || !$("#aBackupResult")) return;
    $("#aBackupResult").innerHTML = `<div class="e-note ok" role="status">✓ Sauvegarde créée : <b>${esc(res.path)}</b> (${esc(fmtSize(res.size_bytes))}).
      <a href="/api/backups/${encodeURIComponent(res.path)}" download>Télécharger la sauvegarde</a></div>`;
    loadBackups();
  };
  $("#aReindex").onclick = async e => {
    let res;
    try { res = await busy(e.currentTarget, "Réindexation…", () => api("/api/archives/reindex", { method: "POST" })); } catch (err) {
      toast(`Réindexation impossible : ${err.message}`, 6000);
      return;
    }
    if (!res) return;
    const text = res.created
      ? `${plural(res.created, "candidature")} rattachée${res.created > 1 ? "s" : ""} au suivi, ${res.skipped} dossier${res.skipped > 1 ? "s" : ""} déjà présent${res.skipped > 1 ? "s" : ""}.`
      : `Rien à rattacher : ${plural(res.skipped, "dossier")} déjà présent${res.skipped > 1 ? "s" : ""} dans le suivi.`;
    if ($("#aReindexResult")) $("#aReindexResult").textContent = text;
    toast(text, 4000);
    if (res.created) {
      await loadApps();
      if (!$("#viewTracker").hidden) renderTracker();
    }
    loadArchivesInfo();
  };
}

$("#settingsNav").onclick = openEmailSettings;

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
  state.modalApp = null;
  stopArchivePoll();
  lockScroll();
}
$$("[data-close]").forEach(el => (el.onclick = closeModal));
// Échap ferme d'abord la couche la plus haute : fenêtre secondaire, puis fiche, puis notifications.
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (!$("#dialog").hidden) closeDialog();
  else if (!$("#modal").hidden) closeModal();
  else closeNotifs();
});

// Filet de sécurité : une erreur d'API non interceptée s'affiche quand même (message « detail » du serveur).
window.addEventListener("unhandledrejection", e => {
  e.preventDefault();
  toast(e.reason?.message || String(e.reason), 5000);
});

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
    if (mine) { await loadOffers(); loadSearches(); loadAutoApply(); toast("Recherche terminée"); }
    loadNotifs(); // de nouvelles alertes ont pu être créées par l'actualisation
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
$("#autoApplyNav").onclick = openAutoApply;
$("#aaManage").onclick = openAutoApply;
$("#aaRefresh").onclick = loadAutoApplyPage;
$("#aaFilter").addEventListener("change", renderAutoApplyList);
$("#aaRetryAll").onclick = async event => {
  try { await retryAllAutoApply(event.currentTarget); } catch (e) { toast(e.message, 6000); }
};
["#qText", "#qMin", "#qStatus", "#qSource", "#qSort", "#qZoneThr", "#qNoNationality"].forEach(s => ($(s).oninput = renderOffers));
$("#qZone").addEventListener("change", renderOffers);
$$('[data-ai-provider]').forEach(select => select.addEventListener("change", changeAIProvider));

// API interne minimale pour les modules d'interface de l'étape 3.
window.JobRadarUI = Object.freeze({
  api, esc, safeUrl, toast, showView, stopPolling, loadApps, renderTracker,
  openAppModal, openApplicationDocuments, closeModal, openDialog, closeDialog, copyText, fmtDateTime, fmtSize, readBase64, state,
});

(async function init() {
  try {
    applyAIInfo(await api("/api/info"));
  } catch { state.aiLabel = "L'IA"; }
  loadNotifs();
  setInterval(loadNotifs, 5 * 6e4); // rappels d'entretien et de relance qui arrivent à échéance
  loadAutoApplyPage(); // compteur « à reprendre » de la barre latérale
  setInterval(loadAutoApplyPage, 2 * 6e4);
  await loadSearches();
  if (state.searches.length) openResults(state.searches[0].id);
  else showView("viewWelcome");
})();
