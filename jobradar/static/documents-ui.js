"use strict";

/* Parcours de préparation des CV et lettres. Ce module ne dépend du reste de
 * l'application que par l'API publique window.JobRadarUI. */
(function () {
  const ui = window.JobRadarUI;
  if (!ui) return;

  const { api, esc, toast } = ui;
  const state = { application: null, preparation: null, templates: [], busy: false };
  const q = (selector, root = document) => root.querySelector(selector);
  const qa = (selector, root = document) => [...root.querySelectorAll(selector)];
  const root = () => q("#applicationDocuments");
  const percent = value => `${Number(value ?? 0).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} %`;
  const statusLabels = { brouillon: "Brouillon", pret: "Prêt à envoyer", envoye: "Envoyé" };

  function safeFileUrl(documentId, name) {
    if (!Number.isInteger(Number(documentId)) || !name) return "#";
    return `/api/documents/${encodeURIComponent(documentId)}/file?name=${encodeURIComponent(name)}`;
  }

  function errorBox(message) {
    return `<div class="doc-error" role="alert"><b>Action impossible.</b> ${esc(message)}</div>`;
  }

  async function run(button, label, action) {
    if (state.busy) return;
    state.busy = true;
    const old = button?.textContent;
    if (button) { button.disabled = true; button.textContent = label; }
    try { return await action(); }
    catch (error) {
      const box = q("[data-doc-error]", root());
      if (box) box.innerHTML = errorBox(error.message || String(error));
      else toast(error.message || String(error), 5000);
      throw error;
    } finally {
      state.busy = false;
      if (button?.isConnected) { button.disabled = false; button.textContent = old; }
    }
  }

  function sourceOptions(sources, kind) {
    const filtered = (sources || []).filter(item => item.kind === kind);
    return filtered.map(item => `<option value="${esc(item.id)}">${esc(item.name || item.filename || item.id)}${item.language ? ` · ${esc(item.language.toUpperCase())}` : ""}</option>`).join("");
  }

  function setupHtml(profile) {
    const sources = profile?.sources || [];
    return `<section class="doc-workflow" aria-labelledby="doc-title">
      <div class="doc-heading"><div><h3 id="doc-title">Préparer ma candidature</h3>
        <p>JobRadar adapte uniquement les faits validés de ton profil. Rien n'est envoyé sans ton accord.</p></div></div>
      <div data-doc-error aria-live="assertive"></div>
      <form id="docPrepareForm" class="doc-form">
        <fieldset><legend>Documents à préparer</legend><label><input type="checkbox" name="kind" value="cv" checked> CV</label>
          <label><input type="checkbox" name="kind" value="lettre" checked> Lettre de motivation</label></fieldset>
        <div class="doc-grid"><label>CV de base<select name="base_cv"><option value="">Choix automatique</option>${sourceOptions(sources, "cv")}</select></label>
          <label>Lettre de base<select name="base_letter"><option value="">Choix automatique</option>${sourceOptions(sources, "lettre")}</select></label></div>
        <div class="doc-grid"><label>Langue<select name="language"><option value="">Langue du document de base</option><option value="fr">Français</option><option value="en">Anglais</option></select></label>
          <label>Modèle<select name="template"><option value="">Modèle original si compatible, sinon Sobre ATS</option>${state.templates.map(template => `<option value="${esc(template.kind)}:${esc(template.id)}">${esc(template.name)}</option>`).join("")}</select></label></div>
        <button class="btn primary" type="submit">Analyser et préparer</button>
      </form></section>`;
  }

  function readSetup(form) {
    const kinds = qa('input[name="kind"]:checked', form).map(input => input.value);
    if (!kinds.length) throw new Error("Choisis au moins un document à préparer.");
    const templateValue = form.elements.template.value;
    const [kind, id] = templateValue ? templateValue.split(":", 2) : [];
    return {
      kinds,
      base_cv: form.elements.base_cv.value || null,
      base_letter: form.elements.base_letter.value || null,
      language: form.elements.language.value || null,
      template: id ? { kind, id } : null,
    };
  }

  function questionsHtml(questions) {
    const skills = questions?.skills || [], items = questions?.items || [], missing = questions?.missing || [];
    if (!skills.length && !items.length && !missing.length) return "";
    const total = skills.length + items.length + missing.length;
    let step = 0;
    const controls = index => `<div class="doc-step-actions"><button class="btn ghost" type="button" data-question-prev ${index ? "" : "disabled"}>Précédent</button>
      ${index + 1 < total ? `<button class="btn primary" type="button" data-question-next>Continuer</button>` : `<button class="btn primary" type="submit">Valider mes réponses</button>`}</div>`;
    const skillSteps = skills.map((skill, index) => {
      const current = step++;
      return `<fieldset class="doc-question-step" data-skill="${index}" data-question-step="${current}" ${current ? "hidden" : ""}>
        <legend>Possèdes-tu la compétence « ${esc(skill.name)} » ?</legend>
        ${skill.excerpt ? `<details class="doc-offer-context"><summary>Voir le passage de l'offre</summary><p>${esc(skill.excerpt)}</p></details>` : ""}
        <div class="doc-answer-choices" role="radiogroup" aria-label="Niveau pour ${esc(skill.name)}">
          <label><input type="radio" name="skill-status-${index}" value="maitrise" required><span>Oui, je maîtrise</span></label>
          <label><input type="radio" name="skill-status-${index}" value="notion"><span>J'ai des notions</span></label>
          <label><input type="radio" name="skill-status-${index}" value="inconnu"><span>Non</span></label>
        </div>
        <label class="doc-free-answer">Précise librement ta réponse <span>(facultatif)</span>
          <textarea name="skill-evidence-${index}" maxlength="300" placeholder="Ex. utilisé pendant un projet étudiant pour…, formation suivie…, niveau débutant mais pratique régulière…"></textarea></label>
        <p class="doc-help">Écris naturellement : ce contexte sera conservé comme preuve déclarée, sans inventer d'information.</p>${controls(current)}</fieldset>`;
    }).join("");
    const itemSteps = items.map((item, index) => {
      const current = step++;
      return `<fieldset class="doc-question-step" data-item="${index}" data-question-step="${current}" hidden><legend>Garder cet élément du profil ?</legend>
        <p>${esc(item.reason)}</p><label>Choix<select name="item-action-${index}" required><option value="garder_une_ligne">Conserver en une ligne</option><option value="retirer">Retirer</option>${(item.replacement_ids || []).map(id => `<option value="remplacer:${esc(id)}">Remplacer par ${esc(id)}</option>`).join("")}</select></label>${controls(current)}</fieldset>`;
    }).join("");
    const missingSteps = missing.map((label, index) => {
      const current = step++;
      return `<fieldset class="doc-question-step" data-missing="${index}" data-question-step="${current}" hidden><legend>${esc(label)}</legend>
        <label class="doc-free-answer">Ta réponse<textarea name="missing-${index}" maxlength="2000" required placeholder="Réponds avec tes propres mots…"></textarea></label>
        <label class="doc-remember"><input type="checkbox" name="remember-${index}" checked> Mémoriser cette réponse pour les prochaines candidatures</label>${controls(current)}</fieldset>`;
    }).join("");
    return `<form id="docAnswersForm" class="doc-questions"><div class="doc-question-head"><div><h4>Quelques vérifications</h4>
      <p class="doc-help">Une question à la fois. Tes réponses sont enregistrées et ne seront pas redemandées.</p></div>
      <strong data-question-progress aria-live="polite">1 / ${total}</strong></div><div class="doc-progress"><span data-question-bar></span></div>
      ${skillSteps}${itemSteps}${missingSteps}</form>`;
  }

  function bindQuestionWizard(form) {
    const steps = qa("[data-question-step]", form);
    let current = 0;
    const show = index => {
      current = Math.max(0, Math.min(index, steps.length - 1));
      steps.forEach((step, position) => {
        const active = position === current;
        step.hidden = !active;
        qa("input, select, textarea, button", step).forEach(control => {
          if (!control.matches("[data-question-prev], [data-question-next]") && control.type !== "submit") control.disabled = !active;
        });
      });
      q("[data-question-progress]", form).textContent = `${current + 1} / ${steps.length}`;
      q("[data-question-bar]", form).style.width = `${((current + 1) / steps.length) * 100}%`;
      steps[current].scrollIntoView({ block: "nearest", behavior: "smooth" });
    };
    const valid = () => qa("[required]", steps[current]).every(control => control.reportValidity());
    qa("[data-question-next]", form).forEach(button => button.onclick = () => { if (valid()) show(current + 1); });
    qa("[data-question-prev]", form).forEach(button => button.onclick = () => show(current - 1));
    show(0);
    return () => steps.forEach(step => qa("input, select, textarea", step).forEach(control => { control.disabled = false; }));
  }

  function compositionHtml(composition) {
    if (!composition) return "";
    const sections = (composition.sections || []).map(section => `<section><h5>${esc(section.title || section.type)}</h5>
      ${(section.items || []).map(item => `<article><b>${esc(item.fields?.title || item.title || "")}</b>${item.fields?.organization ? ` · ${esc(item.fields.organization)}` : ""}
        <ul>${(item.bullets || []).map(bullet => `<li>${esc(bullet.text)}</li>`).join("")}</ul></article>`).join("")}
      ${(section.groups || []).map(group => `<p><b>${esc(group.label)}</b> : ${(group.skills || []).map(skill => esc(skill.text)).join(", ")}</p>`).join("")}</section>`).join("");
    return `<div class="doc-preview"><h4>${esc(composition.headline || "CV proposé")}</h4>${composition.summary ? `<p>${esc(composition.summary)}</p>` : ""}${sections}</div>`;
  }

  function letterHtml(letter) {
    if (!letter) return "";
    const recipient = (letter.recipient || []).length ? `<p class="doc-recipient">${letter.recipient.map(esc).join("<br>")}</p>` : "";
    return `<div class="doc-preview">${recipient}<h4>${esc(letter.subject || "Lettre proposée")}</h4><p>${esc(letter.greeting || "")}</p>
      ${(letter.paragraphs || []).map(paragraph => `<p>${esc(paragraph)}</p>`).join("")}<p>${esc(letter.closing || "")}</p></div>`;
  }

  function reviewHtml(result) {
    const hasQuestions = Object.values(result.questions || {}).some(value => value?.length);
    return `<section class="doc-workflow" aria-labelledby="doc-review-title"><div data-doc-error aria-live="assertive"></div>
      <div class="doc-heading"><h3 id="doc-review-title">Proposition adaptée</h3>
        <button class="btn ghost sm" type="button" data-review-close title="La proposition ne sera plus proposée à la réouverture">Abandonner la proposition</button></div>
      ${(result.notes || []).length ? `<div class="doc-notes"><b>Contrôles appliqués</b><ul>${result.notes.map(note => `<li>${esc(note)}</li>`).join("")}</ul></div>` : ""}
      ${questionsHtml(result.questions)}
      <div class="doc-review-grid">${compositionHtml(result.composition)}${letterHtml(result.letter)}</div>
      <details><summary>Différences et données structurées</summary><pre>${esc(JSON.stringify({ composition: result.composition, letter: result.letter }, null, 2))}</pre></details>
      ${hasQuestions ? "" : `<form id="docRefineForm" class="doc-refine"><label for="docRefinePrompt">Demander une correction ou un ajustement à l'IA</label>
        <textarea id="docRefinePrompt" name="instruction" maxlength="2000" required placeholder="Ex. Raccourcis l'accroche, mets davantage en avant mon projet réseau et rends la lettre plus directe."></textarea>
        <p class="doc-help">La demande ne peut ajouter aucun fait absent de ton profil. Un nouvel aperçu sera proposé avant création des fichiers.</p>
        <button class="btn" type="submit">Mettre à jour les aperçus</button></form>
        <div class="doc-actions"><label>Modèle final<select id="docFinalTemplate">${state.templates.map(template => `<option value="${esc(template.kind)}:${esc(template.id)}" ${result.composition?.template?.id === template.id ? "selected" : ""}>${esc(template.name)}</option>`).join("")}</select></label><button class="btn primary" id="docRender" type="button">Créer et télécharger le PDF et le DOCX</button></div>`}
    </section>`;
  }

  function answersBody(form, preparation) {
    const questions = preparation.questions || {};
    return {
      session: preparation.session,
      skills: (questions.skills || []).map((skill, index) => {
        const evidence = form.elements[`skill-evidence-${index}`].value.trim();
        return { name: skill.name, status: form.elements[`skill-status-${index}`].value,
          evidence: evidence ? [evidence] : [] };
      }),
      items: (questions.items || []).map((item, index) => {
        const [action, replacement_id] = form.elements[`item-action-${index}`].value.split(":", 2);
        return { item_id: item.item_id, action, ...(replacement_id ? { replacement_id } : {}) };
      }),
      missing: (questions.missing || []).map((label, index) => ({ question: label,
        answer: form.elements[`missing-${index}`].value.trim(), remember: form.elements[`remember-${index}`].checked })),
      composition: preparation.composition,
      letter: preparation.letter,
    };
  }

  function bindReview(application) {
    const answers = q("#docAnswersForm");
    const enableAllAnswers = answers ? bindQuestionWizard(answers) : null;
    if (answers) answers.onsubmit = event => {
      event.preventDefault();
      enableAllAnswers();
      run(q('button[type="submit"]', answers), "Validation…", async () => {
        state.preparation = await api(`/api/applications/${application.id}/documents/answers`, { method: "POST", body: answersBody(answers, state.preparation) });
        root().innerHTML = reviewHtml(state.preparation);
        bindReview(application);
      }).catch(() => {});
    };
    const refine = q("#docRefineForm");
    if (refine) refine.onsubmit = event => {
      event.preventDefault();
      const instruction = refine.elements.instruction.value.trim();
      if (!instruction) return;
      run(q('button[type="submit"]', refine), "Ajustement…", async () => {
        state.preparation = await api(`/api/applications/${application.id}/documents/refine`, { method: "POST", body: {
          session: state.preparation.session, instruction,
          composition: state.preparation.composition, letter: state.preparation.letter,
        }});
        root().innerHTML = reviewHtml(state.preparation);
        bindReview(application);
        toast("Aperçus ajustés et contrôlés");
      }).catch(() => {});
    };
    const render = q("#docRender");
    if (render) render.onclick = () => run(render, "Création…", async () => {
      const value = q("#docFinalTemplate")?.value || "";
      const [kind, id] = value.split(":", 2);
      await api(`/api/applications/${application.id}/documents/render`, { method: "POST", body: {
        composition: state.preparation.composition, letter: state.preparation.letter,
        template: id ? { kind, id } : null,
      }});
      toast("Documents créés et contrôlés");
      await renderDocumentsTab(application, [], true);
    }).catch(() => {});
  }

  function atsHtml(ats) {
    if (!ats) return `<p class="doc-help">Contrôle ATS indisponible.</p>`;
    const keywords = ats.keyword_coverage || {};
    return `<div class="ats-summary ${ats.ready ? "ready" : "blocked"}"><b>${ats.ready ? "Contrôle ATS réussi" : "Prêt à envoyer bloqué"}</b>
      <span>Lisibilité ${percent(ats.readability)}</span><span>Mots-clés ${esc(keywords.owned_present ?? 0)}/${esc(keywords.owned_total ?? 0)}</span></div>
      ${(ats.issues || []).length ? `<ul class="doc-issues">${ats.issues.map(issue => `<li>${esc(issue.message || issue)}</li>`).join("")}</ul>` : ""}
      ${(ats.render_warnings || []).length ? `<div class="doc-warning">${ats.render_warnings.map(esc).join(" · ")}</div>` : ""}`;
  }

  function documentHtml(document) {
    const files = document.files || [];
    const pdf = files.find(file => String(file.name).toLowerCase().endsWith(".pdf"));
    return `<article class="document-card" data-document="${esc(document.id)}"><header><div><h4>${document.kind === "lettre" ? "Lettre" : "CV"} · version ${esc(document.version)}</h4>
      <span class="doc-status ${esc(document.status)}">${esc(statusLabels[document.status] || document.status)}</span></div><small>${esc(ui.fmtDateTime(document.updated_at_utc || document.created_at_utc))}</small></header>
      ${document.current?.ok === false ? `<div class="ats-summary blocked" data-stale-document><b>Document périmé</b></div><p class="doc-help">${esc(document.current.message)}</p>` : atsHtml(document.ats)}
      ${pdf ? `<iframe class="doc-pdf" title="Aperçu ${document.kind === "lettre" ? "de la lettre" : "du CV"} version ${esc(document.version)}" src="${esc(safeFileUrl(document.id, pdf.name))}"></iframe>` : ""}
      <div class="doc-files">${files.map(file => `<a class="btn sm" href="${esc(safeFileUrl(document.id, file.name))}" download>${esc(file.name)} ↓</a>`).join("")}</div>
      <form class="doc-improve" data-improve-document="${esc(document.id)}">
        <label for="doc-improve-${esc(document.id)}">Que veux-tu améliorer dans ce ${document.kind === "lettre" ? "courrier" : "CV"} ?</label>
        <textarea id="doc-improve-${esc(document.id)}" name="instruction" maxlength="2000" required
          placeholder="Ex. Raccourcis l'accroche, valorise davantage mon projet de pentest, retire les répétitions et rends le ton plus direct."></textarea>
        <p class="doc-help">Indique précisément les éléments à corriger. JobRadar créera une nouvelle version, sans modifier celle-ci et sans inventer de fait.</p>
        <button class="btn sm" type="submit">Générer une version améliorée</button>
      </form>
      <div class="doc-actions">${document.status === "brouillon" ? `<button class="btn primary sm" data-ready="${esc(document.id)}" ${document.ats?.ready !== true || document.current?.ok === false ? "disabled title=\"Le contrôle ATS doit être intégralement réussi avec ton profil actuel\"" : ""}>Marquer prêt à envoyer</button>` : ""}
        ${document.status === "pret" ? `<button class="btn primary sm" data-sent="${esc(document.id)}">Marquer comme envoyé</button>` : ""}</div></article>`;
  }

  async function renderDocumentsTab(application, archiveFiles = [], force = false) {
    state.application = application;
    const host = root();
    if (!host) return;
    if (!force && host.dataset.applicationId === String(application.id)
        && ["loading", "true", "preparing", "review", "failed"].includes(host.dataset.loaded)) return;
    host.dataset.applicationId = String(application.id);
    host.dataset.loaded = "loading";
    host.innerHTML = `<p class="doc-help" role="status">Chargement des documents…</p>`;
    try {
      const [documents, templates, profile, preparation] = await Promise.all([
        api(`/api/applications/${application.id}/documents`), api("/api/templates"), api("/api/profile"),
        api(`/api/applications/${application.id}/documents/preparation`),
      ]);
      state.templates = templates || [];
      host._profile = profile;
      const job = preparation?.job;
      if (job && (["queued", "running"].includes(job.status) || !job.consumed_at_utc)) {
        followPreparation(application, job);  // reprise : attente, revue ou erreur laissées à la fermeture
        return;
      }
      host.innerHTML = `<div data-doc-error aria-live="assertive"></div><div class="doc-toolbar"><div><h3>Documents</h3><p>${documents.length ? "Versions préparées pour cette candidature." : "Aucun CV ou lettre préparé."}</p></div>
        <button class="btn primary" data-start-documents>Préparer ma candidature</button></div>
        <div class="documents-list">${documents.length ? documents.map(documentHtml).join("") : `<div class="doc-empty">Le parcours conserve le modèle de ton CV par défaut et contrôle chaque fichier avant envoi.</div>`}</div>
        ${archiveFiles.length ? `<details><summary>Autres fichiers archivés (${archiveFiles.length})</summary><ul>${archiveFiles.map(file => `<li>${esc(file.name || file.path)}</li>`).join("")}</ul></details>` : ""}`;
      host._profile = profile;
      host.dataset.loaded = "true";
      bindDocumentsTab(application);
    } catch (error) {
      host.dataset.loaded = "false";
      host.innerHTML = errorBox(error.message || String(error));
    }
  }

  async function prepare(application) {
    state.application = application;
    const host = root();
    if (!host) return;
    host.dataset.loaded = "false";
    const { job } = await api(`/api/applications/${application.id}/documents/preparation`);
    if (job && ["queued", "running"].includes(job.status)) { followPreparation(application, job); return; }
    let profile = host._profile;
    if (!profile) profile = await api("/api/profile");
    host.innerHTML = setupHtml(profile);
    const form = q("#docPrepareForm", host);
    form.onsubmit = event => {
      event.preventDefault();
      let body;
      try { body = readSetup(form); } catch (error) { q("[data-doc-error]", host).innerHTML = errorBox(error.message); return; }
      run(q('button[type="submit"]', form), "Lancement…", async () => {
        const job = await api(`/api/applications/${application.id}/documents/prepare`, { method: "POST", body });
        followPreparation(application, job);
      }).catch(() => {});
    };
    q('input[name="kind"][value="cv"]', form)?.focus();
  }

  /* La préparation tourne côté serveur : la page ne fait que la suivre et peut être fermée à tout moment. */
  const POLL_MS = 3000;
  const preparationSteps = { prepare: "Rédaction par l'IA", render: "Création des fichiers", apply: "Mise en file de l'envoi" };

  function pendingHtml(job) {
    const step = preparationSteps[job.step] || (job.status === "queued" ? "En attente de son tour" : "Rédaction par l'IA");
    return `<section class="doc-workflow doc-pending" aria-labelledby="doc-pending-title" aria-busy="true">
      <h3 id="doc-pending-title">${job.origin === "auto" ? "Auto-candidature en cours" : "Préparation en cours"}</h3>
      <p role="status"><span class="spinner dark" aria-hidden="true"></span> ${esc(step)}…</p>
      <p class="doc-help">Tu peux fermer cette fenêtre : JobRadar continue la préparation et tu retrouveras le résultat ici.</p>
    </section>`;
  }

  function failedHtml(job) {
    return `<section class="doc-workflow" aria-labelledby="doc-failed-title"><h3 id="doc-failed-title">${job.origin === "auto" ? "Auto-candidature arrêtée" : "Préparation interrompue"}</h3>
      ${errorBox(job.error || "Erreur inconnue.")}
      <div class="doc-actions"><button class="btn primary" type="button" data-retry-preparation>Relancer une préparation</button>
        <button class="btn ghost" type="button" data-close-preparation>Fermer</button></div></section>`;
  }

  async function consume(application) {
    await api(`/api/applications/${application.id}/documents/preparation/consume`, { method: "POST" });
    await renderDocumentsTab(application, [], true);
  }

  function showReview(application, result) {
    const host = root();
    state.preparation = result;
    host.innerHTML = reviewHtml(result);
    q("[data-review-close]", host)?.addEventListener("click", () => {
      if (!window.confirm("Abandonner cette proposition ? Elle ne sera plus proposée à la réouverture.")) return;
      consume(application).catch(error => toast(error.message, 5000));
    });
    bindReview(application);
  }

  function followPreparation(application, job) {
    const host = root();
    if (!host) return;
    host.dataset.applicationId = String(application.id);
    host.dataset.loaded = "preparing";
    const showing = () => host.isConnected && host.dataset.applicationId === String(application.id)
      && host.dataset.loaded === "preparing";
    const display = current => {
      if (current.status === "done" && current.origin === "manual" && current.result) {
        host.dataset.loaded = "review";
        showReview(application, current.result);
      } else if (current.status === "done") {
        consume(application).catch(() => {});
      } else if (current.status === "failed") {
        host.dataset.loaded = "failed";
        host.innerHTML = failedHtml(current);
        q("[data-retry-preparation]", host).onclick = () => prepare(application).catch(error => toast(error.message, 5000));
        q("[data-close-preparation]", host).onclick = () => consume(application).catch(error => toast(error.message, 5000));
      } else {
        host.innerHTML = pendingHtml(current);
        setTimeout(poll, POLL_MS);
      }
    };
    const poll = async () => {
      if (!showing()) return;  // fiche fermée ou autre candidature : le serveur continue seul
      try {
        const { job: current } = await api(`/api/applications/${application.id}/documents/preparation`);
        if (showing() && current) display(current);
      } catch (error) {
        if (showing()) setTimeout(poll, POLL_MS * 2);
      }
    };
    display(job);
  }

  function bindDocumentsTab(application) {
    const host = root();
    if (!host) return;
    q("[data-start-documents]", host)?.addEventListener("click", () => prepare(application).catch(error => {
      host.innerHTML = errorBox(error.message || String(error));
    }));
    qa("[data-improve-document]", host).forEach(form => form.addEventListener("submit", event => {
      event.preventDefault();
      const instruction = form.elements.instruction.value.trim();
      if (!instruction) return;
      const button = q('button[type="submit"]', form);
      run(button, "Génération…", async () => {
        const result = await api(`/api/documents/${form.dataset.improveDocument}/refine`, {
          method: "POST", body: { instruction },
        });
        toast(`Version ${result.documents?.[0]?.version || "améliorée"} créée et contrôlée`);
        await renderDocumentsTab(application, [], true);
      }).catch(error => {
        toast(error.message || "La régénération a échoué.", 7000);
      });
    }));
    qa("[data-ready]", host).forEach(button => button.addEventListener("click", () => {
      if (!window.confirm("Le contrôle ATS est réussi. Marquer ce document comme prêt à envoyer ?")) return;
      run(button, "Validation…", () => api(`/api/documents/${button.dataset.ready}/ready`, { method: "POST" })
        .then(() => renderDocumentsTab(application, [], true))).catch(() => {});
    }));
    qa("[data-sent]", host).forEach(button => button.addEventListener("click", () => {
      if (!window.confirm("Confirmer que cette version exacte a été envoyée ? Elle sera ensuite figée.")) return;
      run(button, "Enregistrement…", () => api(`/api/documents/${button.dataset.sent}/sent`, { method: "POST" })
        .then(() => renderDocumentsTab(application, [], true))).catch(() => {});
    }));
  }

  window.JobRadarDocuments = Object.freeze({ prepare, renderDocumentsTab, bindDocumentsTab, safeFileUrl });
})();
