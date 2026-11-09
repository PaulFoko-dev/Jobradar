// Guide d'utilisation : affiché à la première ouverture, rouvrable depuis la barre latérale (« Guide »).
(() => {
  const SEEN_KEY = "jobradar-guide-vu-v1";
  const STEPS = [
    {
      title: "Bienvenue dans JobRadar",
      icon: "📡",
      body: `<p>JobRadar cherche pour toi des offres de stage, d'alternance ou d'emploi sur une dizaine de sites, les fait
        noter par ton assistant IA (Claude Code ou Codex), prépare des CV et lettres adaptés à chaque offre, et peut
        même envoyer tes candidatures.</p>
        <p>Tout reste sur ton ordinateur. Ce guide prend deux minutes ; tu pourras le rouvrir à tout moment avec le
        bouton <b>Guide</b> de la barre latérale.</p>`,
    },
    {
      title: "1. Remplis ton profil",
      icon: "👤",
      body: `<p>Ouvre <b>Mon profil</b> et importe ton CV (PDF ou DOCX) : JobRadar en tire tes expériences, projets et
        compétences. C'est la matière de tous tes futurs CV : rien n'y sera jamais inventé.</p>
        <ul><li><b>Informations souvent demandées</b> : disponibilité, préavis, autorisation de travail… Le robot les
        réutilise dans les formulaires ; celles qui changent (disponibilité, salaire) expirent et te sont redemandées.</li>
        <li><b>Renforcer mon profil</b> : JobRadar signale les éléments trop pauvres (pas de résultat chiffré, pas
        d'outil) et t'aide à les compléter en quatre questions.</li></ul>`,
    },
    {
      title: "2. Lance une recherche",
      icon: "🔎",
      body: `<p>Clique sur <b>+ Nouvelle recherche</b>, remplis le formulaire et/ou décris ce que tu veux en une phrase
        (« stage de 6 mois en cybersécurité en Île-de-France à partir de janvier »). L'IA en tire des requêtes, des
        mots-clés et des exclusions que tu peux corriger avant de lancer.</p>
        <p>La collecte prend 15 à 30 minutes ; les offres apparaissent au fil de l'analyse. Règle la recherche sur
        <b>Quotidienne</b> pour qu'elle s'actualise seule chaque jour.</p>`,
    },
    {
      title: "3. Trie les résultats",
      icon: "📊",
      body: `<p>Chaque offre reçoit un <b>score de pertinence</b> expliqué (points forts, points faibles, missions,
        mots-clés à reprendre). Le filtre « Score ≥ 40 » masque les offres peu pertinentes ; mets-le à 0 pour tout voir.</p>
        <p>Une même offre publiée sur plusieurs sites, ou sous un autre titre mais menant au même formulaire,
        n'apparaît qu'une fois. ⭐ met une entreprise en favori ; tu peux aussi en exclure.</p>`,
    },
    {
      title: "4. Prépare ton CV et ta lettre",
      icon: "📝",
      body: `<p>Sur une offre, clique sur <b>Candidater</b>. JobRadar classe tes expériences et projets selon l'offre,
        compose un CV d'une page et une lettre, puis contrôle qu'aucun fait n'a été inventé et que le CV passe les
        logiciels de tri (ATS ≥ 95 %).</p>
        <p>Le panneau <b>Pourquoi ce contenu ?</b> montre le classement, et <b>Reformulations</b> compare chaque puce du CV
        au texte de ton profil qui la prouve. Tu peux demander une correction en une phrase avant de créer le PDF.</p>`,
    },
    {
      title: "5. Envoie ta candidature",
      icon: "🚀",
      body: `<ul><li><b>Manuel</b> : JobRadar ouvre l'offre et te donne les documents et réponses à copier.</li>
        <li><b>Assisté</b> : le robot remplit le formulaire dans un navigateur dédié (pièces jointes, listes, étapes
        « Sauvegarder et continuer »), vérifie tout, puis envoie. Il s'arrête et te prévient au moindre doute :
        connexion, captcha, question sensible ou réponse inconnue.</li>
        <li><b>Auto-candidature</b> (sur la page d'une recherche) : au-dessus d'un score choisi et dans un plafond
        quotidien, tout se fait seul, documents compris.</li></ul>
        <p>Connecte-toi une fois à tes comptes avec <b>Réglages → Ouvrir le navigateur pour me connecter</b>.</p>`,
    },
    {
      title: "6. Suis tes candidatures",
      icon: "🗂️",
      body: `<p><b>Mes candidatures</b> présente un Kanban (glisser-déposer) de « Envoyée » à « Retenu », avec un dossier
        d'archive par candidature (copie de l'offre, preuves d'envoi).</p>
        <ul><li>Relances proposées 10 jours après l'envoi, avec un brouillon rédigé.</li>
        <li>Entretiens exportables vers ton agenda.</li>
        <li>Avec la <b>lecture de ta boîte de réception</b> (Réglages), JobRadar repère les réponses des recruteurs et
        te propose le bon statut, sans rien envoyer à l'IA.</li>
        <li>La cloche 🔔 regroupe ce qui demande ton attention ; les statistiques montrent ce qui fonctionne.</li></ul>`,
    },
    {
      title: "7. Tes données sont protégées",
      icon: "🔐",
      body: `<p>Le <b>coffre chiffré</b> protège ton profil, tes documents, tes archives et les captures du robot sur ton
        disque. Garde précieusement ta <b>clé de récupération</b> : c'est le seul moyen de retrouver tes données sur un
        autre ordinateur.</p>
        <p>L'IA ne reçoit jamais ton nom, tes coordonnées, tes réponses types ni tes identifiants : seulement les
        offres, tes critères et, avec ton accord, le contenu professionnel de ton CV.</p>`,
    },
    {
      title: "C'est parti !",
      icon: "✅",
      body: `<p>Ordre conseillé pour bien démarrer :</p>
        <ol><li>active le coffre et sauvegarde ta clé de récupération ;</li>
        <li>importe ton CV dans <b>Mon profil</b> et réponds aux informations souvent demandées ;</li>
        <li>lance ta première recherche ;</li>
        <li>dans <b>Réglages</b>, connecte tes comptes et, si tu veux, l'e-mail et la mise à jour quotidienne.</li></ol>
        <p class="muted small">Le bouton <b>Guide</b> de la barre latérale rouvre ce guide à tout moment.</p>`,
    },
  ];
  let index = 0;

  const seen = () => { try { return localStorage.getItem(SEEN_KEY) === "1"; } catch { return false; } };
  const markSeen = () => { try { localStorage.setItem(SEEN_KEY, "1"); } catch {} };

  function render() {
    const step = STEPS[index];
    const last = index === STEPS.length - 1;
    const dots = STEPS.map((_, i) => `<button type="button" class="guide-dot ${i === index ? "on" : ""}" data-guide-go="${i}"
      aria-label="Étape ${i + 1} sur ${STEPS.length}" ${i === index ? 'aria-current="step"' : ""}></button>`).join("");
    $("#dialogBody").innerHTML = `<div class="m-pad guide-modal"><div class="guide-icon" aria-hidden="true">${step.icon}</div>
      <h2 id="dialogTitle">${step.title}</h2><div class="guide-body">${step.body}</div>
      <div class="guide-dots" role="group" aria-label="Étapes du guide">${dots}</div></div>
      <div class="m-foot"><button class="btn ghost" type="button" data-guide-skip>${last ? "Fermer" : "Passer le guide"}</button>
        <span class="guide-spacer"></span>
        ${index ? `<button class="btn" type="button" data-guide-prev>Précédent</button>` : ""}
        <button class="btn primary" type="button" data-guide-next>${last ? "Commencer" : "Suivant"}</button></div>`;
    $("[data-guide-next]").onclick = () => (last ? close() : go(index + 1));
    $("[data-guide-prev]")?.addEventListener("click", () => go(index - 1));
    $("[data-guide-skip]").onclick = close;
    document.querySelectorAll("[data-guide-go]").forEach(button => (button.onclick = () => go(Number(button.dataset.guideGo))));
    $("[data-guide-next]").focus();
  }

  function go(next) {
    index = Math.max(0, Math.min(STEPS.length - 1, next));
    render();
  }

  function onKey(event) {
    if (!document.querySelector(".guide-modal")) return;
    if (event.key === "ArrowRight") go(index + 1);
    if (event.key === "ArrowLeft") go(index - 1);
  }

  function close() {
    markSeen();
    document.removeEventListener("keydown", onKey);
    closeDialog();
  }

  function open(start = 0) {
    index = start;
    openDialog("", () => { markSeen(); document.removeEventListener("keydown", onKey); window.dispatchEvent(new Event("jobradar:guide-closed")); });
    document.addEventListener("keydown", onKey);
    render();
  }

  $("#guideNav")?.addEventListener("click", () => open(0));
  window.JobRadarGuide = Object.freeze({ open, pending: !seen() });
  if (!seen()) open(0);
})();
