// Écran « Aujourd'hui » : une seule liste d'actions à la place des notifications éparpillées.
(() => {
  const nav = $("#todayNav");

  function card(title, count, body, tone = "") {
    return `<article class="panel today-card ${tone}"><div class="sec-head"><h3>${title}</h3>
      ${count !== null ? `<span class="count ${count ? "hot" : ""}">${count}</span>` : ""}</div>${body}</article>`;
  }

  function line(item, action, label) {
    return `<li><span><b>${esc(item.title || "Sans titre")}</b>${item.company ? ` <span class="muted">· ${esc(item.company)}</span>` : ""}
      ${item.score != null ? ` <span class="badge">${esc(item.score)}</span>` : ""}</span>
      <button class="btn sm" type="button" ${action}>${label}</button></li>`;
  }

  function render(data) {
    const interventions = data.interventions.reduce((total, group) => total + group.items.length, 0);
    const reminders = data.reminders.length;
    const todo = interventions + data.ready_to_send.length + reminders;
    $("#todayCount").textContent = todo;
    $("#todayCount").classList.toggle("hot", todo > 0);
    const sections = [];
    if (!data.vault.initialized || !data.vault.unlocked) {
      sections.push(card("Coffre chiffré", null, `<p class="small">${data.vault.initialized
        ? "Ton coffre est verrouillé : saisis ta clé de récupération pour accéder à tes données."
        : "Tes données ne sont pas encore chiffrées sur ton disque."}</p>
        <button class="btn primary sm" type="button" data-today-vault>${data.vault.initialized ? "Déverrouiller" : "Activer le coffre"}</button>`, "warn"));
    }
    sections.push(card("À débloquer", interventions, interventions
      ? data.interventions.map(group => `<h4 class="today-group">${esc(group.label)} (${group.items.length})</h4>
          <ul class="today-list">${group.items.map(item => line(item, `data-today-app="${item.application_id}"`, "Ouvrir")).join("")}</ul>`).join("")
      : `<p class="muted small">Rien ne bloque le robot pour l'instant.</p>`));
    sections.push(card("Prêtes à envoyer", data.ready_to_send.length, data.ready_to_send.length
      ? `<ul class="today-list">${data.ready_to_send.map(item => line(item, `data-today-app="${item.id}"`, "Envoyer")).join("")}</ul>`
      : `<p class="muted small">Aucun CV prêt en attente d'envoi.</p>`));
    const reminderLabels = { follow_up_due: "Relance à faire", interview_reminder: "Entretien à venir", response_detected: "Réponse reçue" };
    sections.push(card("Relances, entretiens et réponses", reminders, reminders
      ? `<ul class="today-list">${data.reminders.map(item => line({ title: `${reminderLabels[item.kind] || "Rappel"} · ${item.title || ""}`,
          company: item.company }, `data-today-notif="${item.id}"`, "Voir")).join("")}</ul>`
      : `<p class="muted small">Aucun rappel.${data.inbox_enabled ? "" : " Active la lecture de ta boîte de réception (Réglages) pour repérer les réponses des recruteurs."}</p>`));
    sections.push(card("Nouvelles offres très pertinentes (7 jours)", data.new_offers.length, data.new_offers.length
      ? `<ul class="today-list">${data.new_offers.map(item => line(item, `data-today-offer="${item.id}" data-today-search="${item.search_id}"`, "Voir l'offre")).join("")}</ul>`
      : `<p class="muted small">Pas de nouvelle offre notée 85 ou plus cette semaine.</p>`));
    $("#todayBody").innerHTML = sections.join("");
    bind();
  }

  function bind() {
    $("[data-today-vault]")?.addEventListener("click", async () => {
      const status = await api("/api/vault").catch(() => ({}));
      if (status.initialized) window.location.reload();  // verrouillé : l'écran de déverrouillage s'ouvre au chargement
      else window.JobRadarVault?.open();
    });
    document.querySelectorAll("[data-today-app]").forEach(button => (button.onclick = async () => {
      await openTracker();
      try { await window.JobRadarApply.open(Number(button.dataset.todayApp)); } catch (error) { toast(error.message, 5000); }
    }));
    document.querySelectorAll("[data-today-offer]").forEach(button => (button.onclick = async () => {
      await openResults(Number(button.dataset.todaySearch));
      if (state.offers.some(offer => offer.id === Number(button.dataset.todayOffer))) openModal(Number(button.dataset.todayOffer));
    }));
    document.querySelectorAll("[data-today-notif]").forEach(button => (button.onclick = () =>
      openNotif(state.notifs?.find(item => item.id === Number(button.dataset.todayNotif)))));
  }

  async function open() {
    showView("viewToday");
    try { render(await api("/api/today")); }
    catch (error) { $("#todayBody").innerHTML = `<div class="e-note err">${esc(error.message)}</div>`; }
  }

  async function refreshCount() {
    try {
      const data = await api("/api/today");
      if (!$("#viewToday").hidden) render(data);
      else {
        const todo = data.interventions.reduce((t, g) => t + g.items.length, 0) + data.ready_to_send.length + data.reminders.length;
        $("#todayCount").textContent = todo;
        $("#todayCount").classList.toggle("hot", todo > 0);
      }
    } catch {}
  }

  nav?.addEventListener("click", open);
  setInterval(refreshCount, 2 * 6e4);
  window.JobRadarToday = Object.freeze({ open, refresh: refreshCount });
})();
