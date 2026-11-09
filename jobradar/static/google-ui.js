// Connexion Google dans le navigateur de JobRadar : explication, ouverture directe, une seule présentation automatique.
(() => {
  const SEEN_KEY = "jobradar-google-vu-v1";
  const seen = () => { try { return localStorage.getItem(SEEN_KEY) === "1"; } catch { return true; } };
  const markSeen = () => { try { localStorage.setItem(SEEN_KEY, "1"); } catch {} };

  function open() {
    markSeen();
    openDialog(`<div class="m-pad google-modal"><h2 id="dialogTitle">Connecte un compte Google au navigateur de JobRadar</h2>
      <p>Beaucoup de sites de candidature (Workday, Welcome to the Jungle, Teamtailor…) proposent <b>« Continuer avec
      Google »</b>. Si un compte Google est connecté dans le navigateur de JobRadar, le robot l'utilise pour se connecter
      ou créer ton compte sur ces sites, sans te demander d'intervenir.</p>
      <ol class="google-steps">
        <li>Clique sur <b>Ouvrir Google dans le navigateur de JobRadar</b> : une fenêtre s'ouvre sur la page de connexion Google.</li>
        <li>Connecte-toi avec le compte que tu utilises pour tes candidatures (idéalement l'adresse e-mail de ton profil).
        Si tu en connectes plusieurs, le robot choisit celui qui correspond à l'e-mail de ton profil.</li>
        <li>Ferme simplement la fenêtre : la session est conservée d'une fois sur l'autre.</li>
      </ol>
      <p class="muted small">Ce navigateur utilise un profil réservé à JobRadar, distinct de ton navigateur habituel : ta
      session Google ne sert qu'aux candidatures. Si Google redemande ton mot de passe ou une double authentification, le
      robot s'arrête et te laisse faire. Tu peux désactiver cette connexion automatique dans Réglages → Candidatures.</p>
      <p class="small" id="googleResult" role="status"></p></div>
      <div class="m-foot"><button class="btn ghost" type="button" data-google-later>Plus tard</button>
        <button class="btn primary" type="button" data-google-open>Ouvrir Google dans le navigateur de JobRadar</button></div>`);
    $("[data-google-later]").onclick = closeDialog;
    $("[data-google-open]").onclick = async event => {
      event.target.disabled = true;
      try {
        const done = await api("/api/settings/browser/google", { method: "POST" });
        $("#googleResult").textContent = `${done.name} est ouvert sur la connexion Google : connecte-toi, puis ferme la fenêtre.`;
      } catch (error) {
        $("#googleResult").textContent = error.message;
        event.target.disabled = false;
      }
    };
  }

  // Présentation automatique une seule fois, après le guide et l'écran du coffre (sans les interrompre).
  function maybeShow() {
    if (seen()) return;
    const attempt = () => {
      if (!$("#dialog").hidden) return setTimeout(attempt, 4000);
      open();
    };
    setTimeout(attempt, 6000);
  }

  window.JobRadarGoogle = Object.freeze({ open });
  maybeShow();
})();
