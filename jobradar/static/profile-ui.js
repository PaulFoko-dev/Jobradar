"use strict";

(() => {
  const ui = window.JobRadarUI;
  if (!ui) throw new Error("Interface JobRadar indisponible.");
  const { api, esc, toast, showView, stopPolling, readBase64 } = ui;
  const root = document.querySelector("#profileRoot");
  const nav = document.querySelector("#profileNav");
  const MAX_BYTES = 10 * 1024 * 1024;
  const ACCEPTED = /\.(docx|pdf|txt|md)$/i;
  const SECTION_LABELS = {
    experiences: "Expériences", projects: "Projets", education: "Formation",
    certifications: "Certifications", skills: "Compétences", languages: "Langues",
  };
  const LINK_FIELDS = [["LinkedIn", "https://www.linkedin.com/in/ton-profil"], ["GitHub", "https://github.com/ton-compte"],
    ["Portfolio", "https://ton-site.fr"]];
  const IDENTITY_FIELDS = ["full_name", "email", "phone", "address_line1", "postal_code", "city", "region", "country"];
  const APPLICATION_FIELDS = [
    ["availability", "Date de disponibilité", "ex. 1er février 2027"],
    ["notice_period", "Délai de préavis", "ex. Aucun ou 1 mois"],
    ["work_authorization", "Autorisation de travail dans le pays du poste", "oui_non"],
    ["sponsorship", "Besoin d’un visa ou d’un parrainage employeur", "oui_non"],
    ["desired_salary", "Rémunération souhaitée", "ex. 1 400 € brut/mois"],
    ["relocation", "Mobilité géographique (prêt à déménager)", "oui_non"],
    ["driving_licence", "Permis de conduire B", "oui_non"],
    ["internship_agreement", "Convention de stage possible avec ton école", "oui_non"],
    ["internship_duration", "Durée de stage souhaitée", "ex. 6 mois"],
  ];
  let current = null;
  let versions = [];
  let busy = false;

  // Même classement que le serveur (profile._link_label) : LinkedIn, GitHub, sinon Portfolio.
  function linkLabel(link) {
    const url = String(link?.url || link || "").toLowerCase();
    return url.includes("linkedin") ? "LinkedIn" : url.includes("github") ? "GitHub" : "Portfolio";
  }

  function linkOf(identity, label) {
    return (identity.links || []).find(link => linkLabel(link) === label)?.url || "";
  }

  function normaliseUrl(value) {
    const url = value.trim();
    if (!url) return "";
    return /^https?:\/\//i.test(url) ? url : `https://${url}`;
  }

  function standardAnswer(key) {
    return String(current.standard_answers?.[key]?.answer || "");
  }

  // État de la mémoire : une réponse expirée n'est plus utilisée par le robot tant qu'elle n'est pas confirmée.
  function answerState(key) {
    const item = current.standard_answers?.[key];
    if (!item?.answer || !item.validated_at) return "";
    const day = value => new Date(value).toLocaleDateString("fr-FR");
    const expired = item.expires_at && new Date(item.expires_at) <= new Date();
    const status = !item.expires_at ? `validée le ${day(item.validated_at)}`
      : expired ? `<b class="answer-expired">expirée depuis le ${day(item.expires_at)} : le robot te la redemandera</b>`
      : `valable jusqu'au ${day(item.expires_at)}`;
    const confirm = item.expires_at ? ` <button class="link" type="button" data-confirm-answer="${esc(key)}">Toujours valable</button>` : "";
    return `<small class="answer-state">${status}${confirm}</small>`;
  }

  function applicationField([key, label, hint]) {
    const value = standardAnswer(key);
    if (hint === "oui_non") return `<label class="field"><span>${label}</span><select name="standard-${key}">
      <option value="">Non renseigné</option><option value="Oui" ${value === "Oui" ? "selected" : ""}>Oui</option>
      <option value="Non" ${value === "Non" ? "selected" : ""}>Non</option></select>${answerState(key)}</label>`;
    return `<label class="field"><span>${label}</span><input name="standard-${key}" value="${esc(value)}" placeholder="${hint}">${answerState(key)}</label>`;
  }

  document.addEventListener("click", async event => {
    const key = event.target?.dataset?.confirmAnswer;
    if (!key) return;
    try {
      current = await api(`/api/profile/answers/${encodeURIComponent(key)}/confirm`, { method: "POST" });
      toast("Réponse confirmée");
      event.target.closest(".answer-state").outerHTML = answerState(key);
    } catch (error) { toast(error.message, 5000); }
  });

  function values(item) {
    const title = item.title || item.name || item.degree || item.label || "Élément sans titre";
    const subtitle = item.company || item.organization || item.school || item.issuer || item.category || "";
    const period = [item.start || item.date, item.end].filter(Boolean).join(" – ");
    const details = (item.bullets || []).map(x => x.text || x).filter(Boolean);
    const technologies = item.technologies || item.keywords || [];
    return { title, subtitle, period, details, technologies, level: item.level || "" };
  }

  function sourceCards() {
    if (!current.sources.length) {
      return `<div class="profile-empty"><b>Aucun document de base.</b><p>Importe au moins un CV ou une lettre pour construire ton profil sans invention.</p></div>`;
    }
    return `<div class="profile-source-list">${current.sources.map(source => `
      <article class="profile-source">
        <div><span class="badge">${source.kind === "cv" ? "CV" : "Lettre"}</span>
          <b>${esc(source.name)}</b><p>${esc(source.filename)} · ${esc((source.language || "fr").toUpperCase())}</p>
          ${(source.tags || []).map(tag => `<span class="tag">${esc(tag)}</span>`).join("")}</div>
        <button class="btn sm ghost danger" data-source-delete="${esc(source.id)}">Supprimer</button>
      </article>`).join("")}</div>`;
  }

  function sectionCards(key) {
    const items = current[key] || [];
    if (!items.length) return `<p class="muted small">Aucune information enregistrée.</p>`;
    return `<div class="profile-items">${items.map(item => {
      const v = values(item);
      return `<article class="profile-item">
        <div class="profile-item-head"><div><b>${esc(v.title)}</b>${v.subtitle ? `<span>${esc(v.subtitle)}</span>` : ""}</div>
          ${v.period ? `<small>${esc(v.period)}</small>` : ""}</div>
        ${v.level ? `<span class="badge">${esc(v.level)}</span>` : ""}
        ${v.details.length ? `<ul>${v.details.map(text => `<li>${esc(text)}</li>`).join("")}</ul>` : ""}
        ${v.technologies.length ? `<p class="muted small">${v.technologies.map(esc).join(" · ")}</p>` : ""}
      </article>`;
    }).join("")}</div>`;
  }

  function conflictCards() {
    const conflicts = (current.conflicts || []).filter(item => !item.resolved);
    if (!conflicts.length) return `<p class="e-ok">✓ Aucun conflit à résoudre.</p>`;
    return conflicts.map(item => `<article class="profile-conflict" role="group" aria-labelledby="conf-${esc(item.id)}">
      <b id="conf-${esc(item.id)}">${esc(item.field)} : quelle valeur conserver ?</b>
      <div class="profile-conflict-values">${(item.values || []).map((value, index) => `
        <label><input type="radio" name="conflict-${esc(item.id)}" value="${index}"> ${esc(value)}</label>`).join("")}</div>
      <button class="btn sm primary" data-conflict="${esc(item.id)}">Valider ce choix</button>
    </article>`).join("");
  }

  function versionRows() {
    if (!versions.length) return `<p class="muted small">Aucun historique pour le moment.</p>`;
    return `<div class="profile-versions">${versions.map(item => `<div>
      <span><b>Version ${item.version}</b>${item.current ? ` <span class="badge">Actuelle</span>` : ""}<small>${esc(ui.fmtDateTime(item.updated_at))}</small></span>
      <span><button class="btn sm" data-version-view="${item.version}">Consulter</button>
      ${item.current ? "" : `<button class="btn sm ghost" data-version-restore="${item.version}">Restaurer</button>`}</span>
    </div>`).join("")}</div>`;
  }

  function render() {
    if (!current) return;
    const identity = current.identity || {};
    root.innerHTML = `<header class="page-head"><div><h1>Mon profil</h1>
        <p class="muted">Les faits utilisés dans tes candidatures, avec leurs sources. Ton identité reste locale et n'est jamais envoyée à l'IA.</p></div>
        <button class="btn primary" id="profileImportToggle">+ Importer un document</button></header>
      <section class="panel profile-import" id="profileImportPanel" hidden>
        <h2>Importer un CV ou une lettre</h2>
        <form id="profileImportForm"><div class="row-2">
          <label class="field"><span>Type</span><select name="kind"><option value="cv">CV</option><option value="lettre">Lettre</option></select></label>
          <label class="field"><span>Nom dans JobRadar</span><input name="name" maxlength="120" required placeholder="ex. CV cybersécurité"></label>
        </div><label class="field"><span>Étiquettes</span><input name="tags" placeholder="stage, SOC, français"></label>
        <label class="field"><span>Fichier DOCX, PDF, TXT ou Markdown — 10 Mo maximum</span><input name="file" type="file" accept=".docx,.pdf,.txt,.md" required></label>
        <div class="profile-actions"><button class="btn primary" type="submit">Importer et analyser</button><span class="muted small" id="profileImportStatus" aria-live="polite"></span></div></form>
      </section>
      <div class="profile-layout"><div class="profile-main">
        <section class="panel"><div class="sec-head"><h2>Identité et présentation</h2><button class="btn sm primary" id="profileSave">Enregistrer</button></div>
          <form id="profileIdentity"><div class="row-2">
            <label class="field"><span>Nom complet</span><input name="full_name" value="${esc(identity.full_name)}"></label>
            <label class="field"><span>E-mail</span><input name="email" type="email" value="${esc(identity.email)}"></label>
            <label class="field"><span>Téléphone</span><input name="phone" value="${esc(identity.phone)}"></label>
            <label class="field"><span>Adresse</span><input name="address_line1" autocomplete="street-address" value="${esc(identity.address_line1)}"></label>
            <label class="field"><span>Code postal</span><input name="postal_code" autocomplete="postal-code" value="${esc(identity.postal_code)}"></label>
            <label class="field"><span>Ville</span><input name="city" value="${esc(identity.city)}"></label>
            <label class="field"><span>Région / département</span><input name="region" value="${esc(identity.region)}"></label>
            <label class="field"><span>Pays de résidence</span><input name="country" autocomplete="country-name" value="${esc(identity.country)}"></label>
            ${LINK_FIELDS.map(([label, placeholder]) => `<label class="field"><span>${label}</span><input name="link-${label}" type="url" inputmode="url" placeholder="${placeholder}" value="${esc(linkOf(identity, label))}"></label>`).join("")}
          </div><label class="field"><span>Titre professionnel</span><input name="headline" value="${esc(current.headline)}"></label>
          <label class="field"><span>Résumé</span><textarea name="summary" rows="5">${esc(current.summary)}</textarea></label>
          <h3>Informations souvent demandées</h3><p class="muted small">Elles servent de réponses types. Laisse vide toute réponse qui dépend de l’offre.</p>
          <div class="row-2">${APPLICATION_FIELDS.map(applicationField).join("")}</div></form>
        </section>
        ${Object.entries(SECTION_LABELS).map(([key, label]) => `<section class="panel"><h2>${label}</h2>${sectionCards(key)}</section>`).join("")}
      </div><aside class="profile-side">
        <section class="panel"><h2>Renforcer mon profil</h2><div id="coachPanel"><p class="muted small">Analyse du profil…</p></div></section>
        <section class="panel"><h2>Documents de base</h2>${sourceCards()}</section>
        <section class="panel"><h2>Conflits</h2>${conflictCards()}</section>
        <section class="panel"><h2>Historique</h2>${versionRows()}</section>
      </aside></div>
      <div id="profileVersionDialog" class="profile-version-dialog" hidden></div>`;
    bind();
    loadCoach();
  }

  // Enrichissement guidé : ce qui affaiblit les CV, et comment le compléter sans rien inventer.
  async function loadCoach() {
    const panel = document.querySelector("#coachPanel");
    if (!panel) return;
    let report;
    try { report = await api("/api/profile/coach"); }
    catch (error) { panel.innerHTML = `<p class="muted small">${esc(error.message)}</p>`; return; }
    const items = report.items.slice(0, 8).map(row => `<li><b>${esc(row.title)}</b><br><small>${esc(row.problems.join(" · "))}</small>
      <button class="link" type="button" data-coach-enrich="${esc(row.id)}" data-coach-title="${esc(row.title)}">Compléter</button></li>`).join("");
    const duplicates = report.duplicates.map(row => `<li>Doublon probable : <b>${esc(row.titles[0])}</b> / <b>${esc(row.titles[1])}</b>
      <button class="link" type="button" data-coach-merge="${esc(row.keep)}" data-coach-drop="${esc(row.drop)}">Fusionner</button></li>`).join("");
    const skills = report.unproven_skills.length ? `<p class="small">Compétences sans preuve dans tes expériences ou projets :
      ${esc(report.unproven_skills.slice(0, 12).join(", "))}${report.unproven_skills.length > 12 ? "…" : ""}. Elles n'apparaîtront
      que dans la liste des compétences, jamais dans une puce.</p>` : "";
    panel.innerHTML = (items || duplicates || skills)
      ? `<p class="muted small">Plus tes éléments sont précis (rôle, outils, résultats chiffrés), plus chaque CV met en avant ce que l'offre demande.</p>
         <ul class="coach-list">${duplicates}${items}</ul>${skills}
         <button class="btn sm" type="button" data-coach-link>Rattacher les compétences à leurs preuves</button>`
      : `<p class="muted small">Ton profil est bien fourni : rien à renforcer pour l'instant.</p>`;
    panel.querySelectorAll("[data-coach-enrich]").forEach(button => (button.onclick = () =>
      openEnrich(button.dataset.coachEnrich, button.dataset.coachTitle, report.questions)));
    panel.querySelectorAll("[data-coach-merge]").forEach(button => (button.onclick = () => runOnce(button, async () => {
      current = await api("/api/profile/coach/merge", { method: "POST", body: { keep: button.dataset.coachMerge, drop: button.dataset.coachDrop } });
      toast("Éléments fusionnés");
      render();
    })));
    const link = panel.querySelector("[data-coach-link]");
    if (link) link.onclick = () => runOnce(link, async () => {
      const result = await api("/api/profile/coach/link-skills", { method: "POST" });
      current = result;
      toast(`${result.linked} preuve(s) rattachée(s)`);
      render();
    });
  }

  function openEnrich(itemId, title, questions) {
    openDialog(`<div class="m-pad"><h2 id="dialogTitle">Compléter « ${esc(title)} »</h2>
      <p class="muted small">Réponds avec tes propres mots ; l'IA en tire des puces sans rien ajouter. Une question sans réponse est ignorée.</p>
      <form id="coachForm">${questions.map(question => `<label class="field"><span>${esc(question.label)}</span>
        <textarea name="${esc(question.key)}" rows="2" maxlength="1500"></textarea></label>`).join("")}</form>
      <p class="small" id="coachResult" role="status"></p></div>
      <div class="m-foot"><button class="btn" type="button" data-dialog-close>Annuler</button>
        <button class="btn primary" type="button" id="coachSubmit">Ajouter les puces</button></div>`);
    document.querySelector("[data-dialog-close]").onclick = closeDialog;
    const submit = document.querySelector("#coachSubmit");
    submit.onclick = () => runOnce(submit, async () => {
      const answers = Object.fromEntries(new FormData(document.querySelector("#coachForm")).entries());
      document.querySelector("#coachResult").textContent = "Rédaction des puces…";
      try {
        current = await api(`/api/profile/coach/${encodeURIComponent(itemId)}/enrich`, { method: "POST", body: { answers } });
        closeDialog();
        toast(`${(current.added || []).length} puce(s) ajoutée(s)`);
        render();
      } catch (error) { document.querySelector("#coachResult").textContent = error.message; throw error; }
    });
  }

  async function load() {
    stopPolling();
    showView("viewProfile");
    nav.classList.add("on");
    root.innerHTML = `<div class="panel"><p class="muted">Chargement du profil…</p></div>`;
    try {
      [current, versions] = await Promise.all([api("/api/profile"), api("/api/profile/versions")]);
      render();
    } catch (error) {
      root.innerHTML = `<div class="e-note err" role="alert"><b>Profil indisponible.</b> ${esc(error.message)}</div>`;
    }
  }

  async function runOnce(button, operation) {
    if (busy) return;
    busy = true;
    if (button) button.disabled = true;
    try { await operation(); } catch (error) { toast(error.message, 6000); }
    finally { busy = false; if (button?.isConnected) button.disabled = false; }
  }

  function bind() {
    document.querySelector("#profileImportToggle").onclick = () => {
      const panel = document.querySelector("#profileImportPanel");
      panel.hidden = !panel.hidden;
      if (!panel.hidden) panel.querySelector("input[name=name]").focus();
    };
    document.querySelector("#profileImportForm").onsubmit = event => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = form.querySelector("button[type=submit]");
      runOnce(button, async () => {
        const file = form.elements.file.files[0];
        if (!file || !ACCEPTED.test(file.name)) throw new Error("Choisis un fichier DOCX, PDF, TXT ou Markdown.");
        if (!file.size || file.size > MAX_BYTES) throw new Error("Le fichier doit être non vide et ne pas dépasser 10 Mo.");
        document.querySelector("#profileImportStatus").textContent = "Extraction et analyse en cours…";
        const body = { kind: form.elements.kind.value, name: form.elements.name.value.trim(),
          tags: form.elements.tags.value.split(",").map(x => x.trim()).filter(Boolean), filename: file.name,
          content_base64: await readBase64(file) };
        const result = await api("/api/profile/sources", { method: "POST", body });
        current = result.profile;
        versions = await api("/api/profile/versions");
        toast("Document importé et profil mis à jour");
        render();
      });
    };
    document.querySelector("#profileSave").onclick = event => runOnce(event.currentTarget, async () => {
      const form = document.querySelector("#profileIdentity");
      const identity = Object.fromEntries(IDENTITY_FIELDS.map(key => [key, form.elements[key].value.trim()]));
      // Seul le lien affiché dans chaque champ est remplacé ; les autres liens importés sont conservés.
      const links = current.identity.links || [];
      const shown = new Set(LINK_FIELDS.map(([label]) => links.find(link => linkLabel(link) === label)).filter(Boolean));
      identity.links = [
        ...links.filter(link => !shown.has(link)),
        ...LINK_FIELDS.map(([label]) => [label, normaliseUrl(form.elements[`link-${label}`].value)])
          .filter(([, url]) => url).map(([label, url]) => ({ label, url })),
      ];
      const standard_answers = { ...(current.standard_answers || {}) };
      APPLICATION_FIELDS.forEach(([key, question]) => {
        const answer = form.elements[`standard-${key}`].value.trim();
        if (answer) standard_answers[key] = { question, answer, sensitive: false };
        else delete standard_answers[key];
      });
      current = await api("/api/profile", { method: "PUT", body: { identity, standard_answers,
        headline: form.elements.headline.value.trim(), summary: form.elements.summary.value.trim() } });
      versions = await api("/api/profile/versions");
      toast("Profil enregistré"); render();
    });
    root.querySelectorAll("[data-source-delete]").forEach(button => button.onclick = () => runOnce(button, async () => {
      if (!window.confirm("Supprimer ce document de base ? Les faits déjà extraits restent dans le profil.")) return;
      current = await api(`/api/profile/sources/${encodeURIComponent(button.dataset.sourceDelete)}`, { method: "DELETE" });
      versions = await api("/api/profile/versions"); toast("Document supprimé"); render();
    }));
    root.querySelectorAll("[data-conflict]").forEach(button => button.onclick = () => runOnce(button, async () => {
      const conflict = current.conflicts.find(item => item.id === button.dataset.conflict);
      const checked = root.querySelector(`input[name="conflict-${CSS.escape(conflict.id)}"]:checked`);
      if (!checked) throw new Error("Choisis une valeur avant de valider.");
      current = await api("/api/profile/conflicts/resolve", { method: "POST", body: { conflict_id: conflict.id, value: conflict.values[+checked.value] } });
      versions = await api("/api/profile/versions"); toast("Conflit résolu"); render();
    }));
    root.querySelectorAll("[data-version-view]").forEach(button => button.onclick = () => runOnce(button, async () => {
      const snapshot = await api(`/api/profile/versions/${button.dataset.versionView}`);
      const dialog = document.querySelector("#profileVersionDialog");
      dialog.innerHTML = `<div class="profile-version-card" role="dialog" aria-modal="true" aria-labelledby="profileVersionTitle">
        <div class="sec-head"><h2 id="profileVersionTitle">Version ${snapshot.version}</h2><button class="btn sm" data-version-close>Fermer</button></div>
        <p>${esc(snapshot.headline || "Sans titre")}</p><p class="muted">${esc(snapshot.summary || "Sans résumé")}</p>
        <dl>${Object.entries(SECTION_LABELS).map(([key, label]) => `<div><dt>${label}</dt><dd>${(snapshot[key] || []).length}</dd></div>`).join("")}</dl></div>`;
      dialog.hidden = false;
      dialog.querySelector("[data-version-close]").onclick = () => { dialog.hidden = true; button.focus(); };
      dialog.querySelector("[data-version-close]").focus();
    }));
    root.querySelectorAll("[data-version-restore]").forEach(button => button.onclick = () => runOnce(button, async () => {
      if (!window.confirm(`Restaurer la version ${button.dataset.versionRestore} ? La version actuelle restera dans l'historique.`)) return;
      current = await api(`/api/profile/restore/${button.dataset.versionRestore}`, { method: "POST" });
      versions = await api("/api/profile/versions"); toast("Version restaurée"); render();
    }));
  }

  nav.addEventListener("click", load);
  window.JobRadarProfile = Object.freeze({ open: load, reload: load });
})();
