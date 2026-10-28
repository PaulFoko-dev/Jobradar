"use strict";

/* Étape 5 : choix explicite des fichiers, parcours manuel et file assistée. */
(() => {
  const ui = window.JobRadarUI;
  if (!ui) throw new Error("Interface JobRadar indisponible.");
  const { api, esc, safeUrl, toast, openDialog, closeDialog, openApplicationDocuments,
    copyText, loadApps, renderTracker } = ui;
  let busy = false;
  const attemptLabels = { manual_open: "Formulaire manuel ouvert", queued: "En attente", running: "En cours",
    needs_user: "Intervention nécessaire", submitted: "Envoyée", failed: "Échec", cancelled: "Annulée" };

  const documentLabel = document => `${document.kind === "cv" ? "CV" : "Lettre"} · version ${document.version}`;

  function choices(documents, kind, selected, optional = false) {
    const items = documents.filter(document => document.kind === kind);
    return `${optional ? `<option value="">Aucune lettre</option>` : !items.length ? `<option value="">Aucun CV prêt à envoyer</option>` : ""}${items.map(document =>
      `<option value="${document.id}" ${document.id === selected ? "selected" : ""}>${esc(documentLabel(document))} · ATS ${esc(document.ats?.readability ?? "–")} %</option>`
    ).join("")}`;
  }

  function previewHtml(intervention) {
    const values = intervention?.preview || [];
    if (!values.length) return "";
    return `<details class="apply-review" ${intervention.kind === "review_required" ? "open" : ""}><summary>Réponses préparées (${values.length})</summary>
      <dl>${values.map(item => `<div><dt>${esc(item.label)}</dt><dd>${esc(String(item.value ?? ""))}<small>${esc(item.source || "")}</small></dd></div>`).join("")}</dl></details>`;
  }

  function interventionForm(attempt) {
    const intervention = attempt.intervention || {};
    if (intervention.kind !== "missing_fields" || !(intervention.fields || []).length) return "";
    return `<form class="apply-field-form" data-answer-attempt="${attempt.id}"><h4>Informations nécessaires</h4>
      ${(intervention.fields || []).map((field, index) => `<label>${esc(field.label)}${field.kind === "sensitive" ? `<small class="apply-sensitive">Question sensible : réponds uniquement si tu souhaites transmettre cette information.</small>` : ""}
        ${["select", "radio", "listbox"].includes(field.input_kind) && field.options?.length
          ? `<select data-field-index="${index}" required><option value="">Choisir…</option>${field.options.map(option => `<option value="${esc(option)}">${esc(option)}</option>`).join("")}</select>`
          : field.input_kind === "checkbox"
            ? `<select data-field-index="${index}" required><option value="">Choisir…</option><option value="true">Oui</option><option value="false">Non</option></select>`
            : `<textarea data-field-index="${index}" maxlength="2000" required placeholder="Écris ta réponse…"></textarea>`}</label>`).join("")}
      <button class="btn primary sm" type="submit">Enregistrer et reprendre</button></form>`;
  }

  // Après le clic d'envoi, la candidature est peut-être partie : aucune action ne doit pouvoir la renvoyer.
  function attemptActions(attempt) {
    const clicked = Boolean(attempt.submit_clicked_at_utc);
    const unconfirmed = clicked && attempt.status === "needs_user";
    if (unconfirmed) {
      return `<button class="btn sm primary" type="button" data-confirm-sent="${attempt.id}">Elle est bien partie</button>
        <button class="btn sm" type="button" data-manual-attempt="${attempt.id}">Elle n'est pas partie : passer en manuel</button>`;
    }
    return `${["queued", "needs_user", "failed"].includes(attempt.status) && !clicked ? `<button class="btn sm ghost" type="button" data-cancel-attempt="${attempt.id}">Annuler</button>` : ""}
      ${!clicked && (attempt.status === "failed" || (attempt.status === "needs_user" && !["missing_fields", "review_required"].includes(attempt.intervention?.kind)))
        ? `<button class="btn sm" type="button" data-retry-attempt="${attempt.id}">${attempt.status === "failed" ? "Réessayer" : attempt.intervention?.kind === "waiting_user" ? "J'ai terminé, reprendre" : "Reprendre"}</button>` : ""}
      ${attempt.intervention?.action === "manual_fallback" || attempt.status === "failed"
        ? `<button class="btn sm primary" type="button" data-manual-attempt="${attempt.id}">Passer en manuel</button>` : ""}`;
  }

  function selectionHtml(data) {
    const noCv = !data.documents.some(document => document.kind === "cv");
    const assisted = data.assisted;
    return `<div class="m-pad apply-dialog"><h2 id="dialogTitle">Candidater chez ${esc(data.application.company || "cette entreprise")}</h2>
      <p class="muted">${esc(data.application.title)}</p>
      <div class="apply-error" data-apply-error role="alert" aria-live="assertive"></div>
      ${noCv ? `<div class="e-note warn apply-no-cv"><span><b>Aucun CV prêt à envoyer.</b><small>Génère le CV et la lettre adaptés à cette offre, vérifie les aperçus puis télécharge-les.</small></span>
        <button class="btn primary" type="button" data-prepare-documents>Préparer CV et lettre</button></div>` :
        `<button class="btn ghost apply-adjust-docs" type="button" data-prepare-documents>Ajuster ou générer d'autres documents</button>`}
      ${(data.stale_documents || []).map(item => `<div class="e-note warn apply-stale" role="status"><span><b>${esc(documentLabel(item))} non proposé : document périmé.</b>
        <small>${esc(item.message)}</small></span></div>`).join("")}
      ${(data.attempts || []).length ? `<section class="apply-attempts"><h3>Sessions récentes</h3>${data.attempts.slice(0, 5).map(attempt =>
        `<article><div><b>${esc(attemptLabels[attempt.status] || attempt.status)}</b><small>${esc(attempt.mode === "manual" ? "Manuel" : `Assisté · ${attempt.provider}`)}</small>
          ${attempt.intervention?.message ? `<p>${esc(attempt.intervention.message)}</p>` : ""}${attempt.error ? `<p>${esc(attempt.error)}</p>` : ""}
          ${previewHtml(attempt.intervention)}${interventionForm(attempt)}</div>
          <div>${attemptActions(attempt)}
          ${attempt.intervention?.kind === "review_required" ? `<button class="btn sm primary" type="button" data-approve-attempt="${attempt.id}">Autoriser l'envoi</button>` : ""}</div></article>`).join("")}</section>` : ""}
      <form id="applyForm">
        <label class="field"><span>CV envoyé</span><select name="cv" ${noCv ? "disabled" : "required"}>${choices(data.documents, "cv", data.defaults.cv_document_id)}</select></label>
        <label class="field"><span>Lettre de motivation</span><select name="letter">${choices(data.documents, "lettre", data.defaults.include_letter ? data.defaults.letter_document_id : null, true)}</select></label>
        <fieldset class="apply-modes"><legend>Mode de candidature</legend>
          <label><input type="radio" name="mode" value="manual" ${data.defaults.mode !== "assisted" || !assisted.allowed ? "checked" : ""}> <span><b>Manuel</b><small>JobRadar ouvre l'offre et garde les informations utiles à portée de main.</small></span></label>
          <label class="${!assisted.allowed || !assisted.implemented ? "disabled" : ""}"><input type="radio" name="mode" value="assisted" ${data.defaults.mode === "assisted" && assisted.allowed && assisted.implemented ? "checked" : ""} ${!assisted.allowed || !assisted.implemented ? "disabled" : ""}>
            <span><b>Candidature assistée par IA</b><small>${esc(assisted.reason || (assisted.implemented ? "Remplissage contrôlé, vérification puis envoi." : "Ce site n'a pas encore d'adaptateur dédié."))}</small></span></label>
        </fieldset>
        <label class="field apply-review-policy" data-review-policy ${data.defaults.mode === "assisted" && assisted.allowed && assisted.implemented ? "" : "hidden"}><span>Validation avant l'envoi assisté</span>
          <select name="reviewPolicy">
            <option value="first_five" ${data.defaults.review_policy === "first_five" ? "selected" : ""}>Me demander pour les 5 premières candidatures</option>
            <option value="always" ${data.defaults.review_policy === "always" ? "selected" : ""}>Toujours me demander</option>
            <option value="never" ${data.defaults.review_policy === "never" ? "selected" : ""}>Envoyer après les contrôles automatiques</option>
          </select>
          <small>Les champs inconnus, captchas et erreurs demandent toujours ton intervention, quel que soit ce choix.</small>
        </label>
        <label class="apply-remember"><input type="checkbox" name="remember"> Retenir ces choix par défaut</label>
      </form></div>
      <div class="m-foot"><button class="btn ghost" data-apply-cancel>Annuler</button><button class="btn primary" data-apply-start ${noCv ? "disabled" : ""}>Continuer en mode manuel</button></div>`;
  }

  function answerRows(answers) {
    const entries = Object.values(answers || {}).filter(item => item && typeof item === "object");
    if (!entries.length) return `<p class="muted small">Aucune réponse type enregistrée.</p>`;
    return entries.map((item, index) => `<div class="apply-copy-row"><span><b>${esc(item.question || "Réponse type")}</b><small>${esc(item.answer || "")}</small></span>
      <button class="btn sm" type="button" data-copy-answer="${index}">Copier</button></div>`).join("");
  }

  function helpHtml(data) {
    const identity = data.identity || {};
    const identityItems = [["Nom", identity.full_name], ["E-mail", identity.email], ["Téléphone", identity.phone], ["Ville", identity.city],
      ...((identity.links || []).map((value, index) => [`Lien ${index + 1}`, value]))].filter(([, value]) => value);
    return `<div class="m-pad apply-dialog"><h2 id="dialogTitle">Panneau d'aide — candidature manuelle</h2>
      <p class="muted">Les données restent sur ton PC. Ouvre l'offre, puis copie seulement ce dont le formulaire a besoin.</p>
      <section><h3>Coordonnées</h3><div class="apply-copy-list">${identityItems.length ? identityItems.map(([label, value], index) =>
        `<div class="apply-copy-row"><span><b>${esc(label)}</b><small>${esc(value)}</small></span><button class="btn sm" type="button" data-copy-identity="${index}">Copier</button></div>`).join("") : `<p class="e-note warn">Complète tes coordonnées dans « Mon profil ».</p>`}</div></section>
      <section><h3>Documents sélectionnés</h3><div class="apply-files">${data.documents.flatMap(document => document.files.map(file =>
        `<a class="btn" href="${esc(file.url)}" download>${esc(file.name)} ↓</a>`)).join("")}</div></section>
      <section><h3>Réponses types</h3><div class="apply-copy-list">${answerRows(data.standard_answers)}</div></section>
    </div><div class="m-foot"><button class="btn ghost" data-apply-close>Fermer</button>
      <a class="btn primary" href="${esc(safeUrl(data.target_url))}" target="_blank" rel="noopener noreferrer" data-open-target>Ouvrir le formulaire ↗</a>
      <button class="btn primary" data-manual-sent>J'ai envoyé ma candidature</button></div>`;
  }

  function bindHelp(data) {
    const identity = data.identity || {};
    const identityValues = [identity.full_name, identity.email, identity.phone, identity.city, ...(identity.links || [])].filter(Boolean);
    const answers = Object.values(data.standard_answers || {}).filter(item => item && typeof item === "object");
    document.querySelector("[data-apply-close]").onclick = closeDialog;
    document.querySelectorAll("[data-copy-identity]").forEach(button => button.onclick = () =>
      copyText(identityValues[+button.dataset.copyIdentity], "Information copiée"));
    document.querySelectorAll("[data-copy-answer]").forEach(button => button.onclick = () =>
      copyText(answers[+button.dataset.copyAnswer].answer || "", "Réponse copiée"));
    document.querySelector("[data-open-target]").onclick = () => toast("Formulaire ouvert dans un nouvel onglet");
    document.querySelector("[data-manual-sent]").onclick = async event => {
      if (busy) return;
      busy = true;
      event.currentTarget.disabled = true;
      try {
        await api(`/api/application-attempts/${data.attempt.id}/manual-sent`, { method: "POST" });
        await loadApps();
        if (!document.querySelector("#viewTracker").hidden) renderTracker();
        closeDialog();
        toast("Candidature enregistrée comme envoyée");
      } catch (error) {
        event.currentTarget.disabled = false;
        toast(error.message, 6000);
      } finally { busy = false; }
    };
  }

  async function startManual(applicationId, body) {
    const help = await api(`/api/applications/${applicationId}/apply/start`, { method: "POST", body });
    openDialog(helpHtml(help));
    bindHelp(help);
  }

  function bindAttempts(applicationId, attempts = []) {
    document.querySelectorAll("[data-answer-attempt]").forEach(form => form.onsubmit = async event => {
      event.preventDefault();
      if (busy) return;
      const attempt = attempts.find(item => item.id === Number(form.dataset.answerAttempt));
      const fields = attempt?.intervention?.fields || [];
      if (!attempt || !fields.length) return;
      const answers = fields.map((field, index) => {
        const control = form.querySelector(`[data-field-index="${index}"]`);
        let value = control.value;
        if (field.input_kind === "checkbox") value = value === "true";
        return { selector: field.selector, value };
      });
      busy = true;
      const button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      button.textContent = "Reprise…";
      try {
        await api(`/api/application-attempts/${attempt.id}/answers`, { method: "POST", body: { answers } });
        toast("Réponses enregistrées, candidature reprise");
        await open(applicationId);
      } catch (error) {
        document.querySelector("[data-apply-error]").textContent = error.message;
        button.disabled = false;
        button.textContent = "Enregistrer et reprendre";
      } finally { busy = false; }
    });
    document.querySelectorAll("[data-cancel-attempt]").forEach(button => button.onclick = async () => {
      if (busy) return;
      busy = true;
      try {
        await api(`/api/application-attempts/${button.dataset.cancelAttempt}/cancel`, { method: "POST" });
        toast("Session annulée");
        await open(applicationId);
      } catch (error) { toast(error.message, 6000); } finally { busy = false; }
    });
    document.querySelectorAll("[data-retry-attempt]").forEach(button => button.onclick = async () => {
      if (busy) return;
      busy = true;
      try {
        await api(`/api/application-attempts/${button.dataset.retryAttempt}/retry`, { method: "POST" });
        toast("Session replacée dans la file");
        await open(applicationId);
      } catch (error) { toast(error.message, 6000); } finally { busy = false; }
    });
    document.querySelectorAll("[data-manual-attempt]").forEach(button => button.onclick = async () => {
      if (busy) return;
      busy = true;
      try {
        const help = await api(`/api/application-attempts/${button.dataset.manualAttempt}/manual`, { method: "POST" });
        openDialog(helpHtml(help));
        bindHelp(help);
      } catch (error) { toast(error.message, 6000); } finally { busy = false; }
    });
    document.querySelectorAll("[data-confirm-sent]").forEach(button => button.onclick = async () => {
      if (busy) return;
      busy = true;
      button.disabled = true;
      try {
        await api(`/api/application-attempts/${button.dataset.confirmSent}/confirm-sent`, { method: "POST" });
        await loadApps();
        if (!document.querySelector("#viewTracker").hidden) renderTracker();
        toast("Candidature enregistrée comme envoyée");
        await open(applicationId);
      } catch (error) {
        button.disabled = false;
        toast(error.message, 6000);
      } finally { busy = false; }
    });
    document.querySelectorAll("[data-approve-attempt]").forEach(button => button.onclick = async () => {
      if (busy || !window.confirm("Autoriser l'envoi après une nouvelle auto-vérification complète ?")) return;
      busy = true;
      try {
        await api(`/api/application-attempts/${button.dataset.approveAttempt}/approve`, { method: "POST" });
        toast("Envoi autorisé et replacé dans la file");
        await open(applicationId);
      } catch (error) { toast(error.message, 6000); } finally { busy = false; }
    });
  }

  async function open(applicationId) {
    const data = await api(`/api/applications/${applicationId}/apply/options`);
    openDialog(selectionHtml(data));
    bindAttempts(applicationId, data.attempts || []);
    document.querySelector("[data-prepare-documents]")?.addEventListener("click", () => {
      closeDialog();
      openApplicationDocuments(applicationId, true);
    });
    const reviewPolicy = document.querySelector("[data-review-policy]");
    const syncReviewPolicy = () => {
      reviewPolicy.hidden = document.querySelector('#applyForm input[name="mode"]:checked')?.value !== "assisted";
    };
    document.querySelectorAll('#applyForm input[name="mode"]').forEach(control =>
      control.addEventListener("change", syncReviewPolicy));
    syncReviewPolicy();
    document.querySelector("[data-apply-cancel]").onclick = closeDialog;
    document.querySelector("[data-apply-start]").onclick = async event => {
      if (busy) return;
      const form = document.querySelector("#applyForm");
      const cv = Number(form.elements.cv.value);
      if (!cv) return;
      busy = true;
      const button = event.currentTarget;
      button.disabled = true;
      button.textContent = "Préparation…";
      try {
        const body = {
          mode: form.elements.mode.value, cv_document_id: cv,
          letter_document_id: Number(form.elements.letter.value) || null, remember: form.elements.remember.checked,
          review_policy: form.elements.reviewPolicy.value,
        };
        if (body.mode === "assisted") {
          await api(`/api/applications/${applicationId}/apply/start`, { method: "POST", body });
          toast("Candidature ajoutée à la file assistée");
          await open(applicationId);
        } else {
          await startManual(applicationId, body);
        }
      } catch (error) {
        document.querySelector("[data-apply-error]").textContent = error.message;
        button.disabled = false;
        button.textContent = "Continuer en mode manuel";
      } finally { busy = false; }
    };
  }

  window.JobRadarApply = Object.freeze({ open });
})();
