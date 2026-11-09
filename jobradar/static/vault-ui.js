// Coffre chiffré : activation (clé de récupération, consentement, chiffrement de l'existant) et déverrouillage.
(() => {
  const STEPS = ["Coffre", "Clé de récupération", "Consentement", "Chiffrement"];

  function stepper(index) {
    return `<ol class="vault-steps" aria-label="Étapes">${STEPS.map((label, i) =>
      `<li class="${i < index ? "done" : i === index ? "current" : ""}" ${i === index ? 'aria-current="step"' : ""}>${label}</li>`).join("")}</ol>`;
  }

  function frame(index, body, foot) {
    return `<div class="m-pad vault-modal"><h2 id="dialogTitle">Coffre chiffré de JobRadar</h2>${stepper(index)}${body}</div>
      <div class="m-foot">${foot}</div>`;
  }

  function showIntro() {
    openDialog(frame(0, `<p>Ton profil, tes CV, tes lettres, tes archives de candidature et les captures du robot contiennent
      des données personnelles. Le coffre les chiffre sur ton disque (AES-256) ; JobRadar les déchiffre seulement pendant qu'il s'en sert.</p>
      <ul class="vault-points"><li>La clé principale reste dans le trousseau de ton système.</li>
      <li>Une <b>clé de récupération</b> te sera montrée <b>une seule fois</b> : c'est le seul moyen de retrouver tes données
      sur un autre ordinateur ou si le trousseau est perdu.</li>
      <li>Rien n'est envoyé hors de ton ordinateur.</li></ul>`,
      `<button class="btn ghost" type="button" data-vault-later>Plus tard</button>
       <button class="btn primary" type="button" data-vault-create>Créer mon coffre</button>`));
    $("[data-vault-later]").onclick = closeDialog;
    $("[data-vault-create]").onclick = async () => {
      try { showKey((await api("/api/vault/init", { method: "POST" })).recovery_key); }
      catch (error) { toast(error.message, 6000); }
    };
  }

  function showKey(key) {
    openDialog(frame(1, `<p>Note cette clé et range-la <b>hors de cet ordinateur</b> (papier, gestionnaire de mots de passe).
      Elle ne sera plus jamais affichée.</p>
      <p class="vault-key" data-vault-key tabindex="0">${esc(key)}</p>
      <div class="vault-actions"><button class="btn sm" type="button" data-vault-copy>Copier</button>
        <button class="btn sm" type="button" data-vault-download>Télécharger (.txt)</button></div>
      <label class="vault-check"><input type="checkbox" data-vault-saved> J'ai sauvegardé ma clé de récupération hors de cet ordinateur</label>`,
      `<button class="btn primary" type="button" data-vault-next disabled>Continuer</button>`));
    $("[data-vault-copy]").onclick = async () => {
      try { await navigator.clipboard.writeText(key); toast("Clé copiée"); } catch { toast("Copie impossible : sélectionne la clé", 4000); }
    };
    $("[data-vault-download]").onclick = () => {
      const link = document.createElement("a");
      link.href = URL.createObjectURL(new Blob([`Clé de récupération JobRadar\n\n${key}\n`], { type: "text/plain" }));
      link.download = "jobradar-cle-de-recuperation.txt";
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    };
    $("[data-vault-saved]").onchange = event => ($("[data-vault-next]").disabled = !event.target.checked);
    $("[data-vault-next]").onclick = async () => {
      try { await api("/api/vault/confirm-recovery", { method: "POST" }); showConsent(); }
      catch (error) { toast(error.message, 6000); }
    };
  }

  function showConsent() {
    openDialog(frame(2, `<p>Pour adapter tes CV et lettres, l'IA reçoit le <b>contenu professionnel</b> de ton profil :
      expériences, projets, compétences, formations. Elle ne reçoit <b>jamais</b> ton identité, tes coordonnées, ton adresse,
      tes réponses types, tes identifiants ni tes données sensibles.</p>
      <label class="vault-check"><input type="checkbox" data-vault-consent checked>
        J'autorise l'envoi du contenu professionnel de mes CV à l'IA (révocable à tout moment dans les Réglages)</label>`,
      `<button class="btn primary" type="button" data-vault-next>Continuer</button>`));
    $("[data-vault-next]").onclick = async () => {
      try {
        await api("/api/vault/consent", { method: "PUT", body: { consent: $("[data-vault-consent]").checked } });
        showMigration();
      } catch (error) { toast(error.message, 6000); }
    };
  }

  function showMigration() {
    openDialog(frame(3, `<p>Dernière étape : chiffrer les données déjà présentes (base, profil, archives des candidatures,
      captures du robot). Cela peut prendre une minute ; tu peux relancer sans risque si c'est interrompu.</p>
      <p class="muted small" data-vault-result role="status"></p>`,
      `<button class="btn primary" type="button" data-vault-run>Chiffrer mes données maintenant</button>`));
    $("[data-vault-run]").onclick = async event => {
      event.target.disabled = true;
      $("[data-vault-result]").textContent = "Chiffrement en cours…";
      try {
        const done = await api("/api/vault/migrate", { method: "POST" });
        $("[data-vault-result]").textContent = `Terminé : ${done.values} valeur(s) et ${done.files} fichier(s) chiffrés.`;
        $(".vault-modal").closest("#dialogBody").querySelector(".m-foot").innerHTML =
          `<button class="btn primary" type="button" data-vault-done>Fermer</button>`;
        $("[data-vault-done]").onclick = closeDialog;
      } catch (error) {
        event.target.disabled = false;
        $("[data-vault-result]").textContent = error.message;
      }
    };
  }

  function showUnlock() {
    openDialog(`<div class="m-pad vault-modal"><h2 id="dialogTitle">Coffre verrouillé</h2>
      <p>La clé principale est introuvable dans le trousseau de ce système (nouvel ordinateur, trousseau réinitialisé ou verrouillé).
      Saisis ta clé de récupération pour accéder de nouveau à tes données.</p>
      <form data-vault-unlock><label class="field"><span>Clé de récupération</span>
        <input name="key" autocomplete="off" spellcheck="false" required minlength="20" placeholder="ABCD-EFGH-…"></label>
        <p class="muted small" data-vault-result role="status"></p></form></div>
      <div class="m-foot"><button class="btn primary" type="button" data-vault-submit>Déverrouiller</button></div>`);
    const submit = async () => {
      try {
        await api("/api/vault/unlock", { method: "POST", body: { recovery_key: $("[data-vault-unlock] input").value } });
        closeDialog();
        toast("Coffre déverrouillé");
        location.reload();
      } catch (error) { $("[data-vault-result]").textContent = error.message; }
    };
    $("[data-vault-submit]").onclick = submit;
    $("[data-vault-unlock]").onsubmit = event => { event.preventDefault(); submit(); };
  }

  async function check() {
    let status;
    try { status = await api("/api/vault"); } catch { return; }
    if (!status.initialized) {
      if (sessionStorageGet("vault-later") !== "1") showIntro();
    } else if (!status.unlocked) {
      showUnlock();
    } else if (!status.recovery_confirmed) {
      toast("Coffre créé, mais la sauvegarde de la clé de récupération n'est pas confirmée : recrée-le depuis les Réglages si tu l'as perdue.", 8000);
    }
  }

  function sessionStorageGet(key) {
    try { return sessionStorage.getItem(key); } catch { return null; }
  }

  document.addEventListener("click", event => {
    if (event.target.matches?.("[data-vault-later]")) {
      try { sessionStorage.setItem("vault-later", "1"); } catch {}
    }
  });
  window.JobRadarVault = Object.freeze({ check, open: showIntro, consent: showConsent, migrate: showMigration });
  // Première ouverture : le guide passe d'abord, l'écran du coffre suit sa fermeture.
  (window.JobRadarAuth?.ready() || Promise.resolve()).then(() => {
    if (window.JobRadarGuide?.pending) window.addEventListener("jobradar:guide-closed", check, { once: true });
    else check();
  });
})();
