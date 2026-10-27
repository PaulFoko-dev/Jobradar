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
            <label class="field"><span>Ville</span><input name="city" value="${esc(identity.city)}"></label>
            ${LINK_FIELDS.map(([label, placeholder]) => `<label class="field"><span>${label}</span><input name="link-${label}" type="url" inputmode="url" placeholder="${placeholder}" value="${esc(linkOf(identity, label))}"></label>`).join("")}
          </div><label class="field"><span>Titre professionnel</span><input name="headline" value="${esc(current.headline)}"></label>
          <label class="field"><span>Résumé</span><textarea name="summary" rows="5">${esc(current.summary)}</textarea></label></form>
        </section>
        ${Object.entries(SECTION_LABELS).map(([key, label]) => `<section class="panel"><h2>${label}</h2>${sectionCards(key)}</section>`).join("")}
      </div><aside class="profile-side">
        <section class="panel"><h2>Documents de base</h2>${sourceCards()}</section>
        <section class="panel"><h2>Conflits</h2>${conflictCards()}</section>
        <section class="panel"><h2>Historique</h2>${versionRows()}</section>
      </aside></div>
      <div id="profileVersionDialog" class="profile-version-dialog" hidden></div>`;
    bind();
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
      const identity = Object.fromEntries(["full_name", "email", "phone", "city"].map(key => [key, form.elements[key].value.trim()]));
      // Seul le lien affiché dans chaque champ est remplacé ; les autres liens importés sont conservés.
      const links = current.identity.links || [];
      const shown = new Set(LINK_FIELDS.map(([label]) => links.find(link => linkLabel(link) === label)).filter(Boolean));
      identity.links = [
        ...links.filter(link => !shown.has(link)),
        ...LINK_FIELDS.map(([label]) => [label, normaliseUrl(form.elements[`link-${label}`].value)])
          .filter(([, url]) => url).map(([label, url]) => ({ label, url })),
      ];
      current = await api("/api/profile", { method: "PUT", body: { identity, headline: form.elements.headline.value.trim(), summary: form.elements.summary.value.trim() } });
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
