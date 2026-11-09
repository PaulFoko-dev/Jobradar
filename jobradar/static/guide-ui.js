// Visite guidée : chaque étape met en lumière l'élément concerné, floute le reste et place une bulle à côté.
// Affichée à la première ouverture (après la connexion), rouvrable avec « Guide » ou le menu du compte.
(() => {
  // v4 : une étape par zone de l'écran (et non par bouton), parcourues dans l'ordre de lecture : moins de déplacements.
  const SEEN_KEY = "jobradar-guide-vu-v4";
  const STEPS = [
    { title: "Bienvenue dans JobRadar 👋", body: `JobRadar cherche des offres sur une dizaine de sites, les fait noter par ton
      assistant IA, prépare un CV et une lettre adaptés à chaque offre et peut envoyer tes candidatures. Tout reste sur ton
      ordinateur.<br><br>Cette visite en 7 étapes parcourt l'écran dans l'ordre : la <b>barre latérale</b> de haut en bas,
      puis la <b>barre du haut</b>.` },
    { target: "#newSearchBtn", title: "1. Lancer une recherche", body: `Décris ce que tu cherches en une phrase ou avec le
      formulaire. L'IA propose requêtes, mots-clés et exclusions, que tu valides avant de lancer la collecte (15 à 30 min).` },
    { target: ".side-nav", title: "2. Naviguer", body: `<b>Aujourd'hui</b> : ton écran d'accueil, avec tout ce qui demande
      ton attention.<br><b>Mes candidatures</b> : le suivi en Kanban, les relances et les statistiques.<br><b>Candidatures
      auto</b> : le robot qui remplit et envoie les formulaires pour toi, et s'arrête au moindre doute.` },
    { target: "#searchList", title: "3. Mes recherches", body: `Chaque recherche enregistrée liste ses offres, notées et
      expliquées par l'IA. Une même offre publiée sur plusieurs sites n'apparaît qu'une fois.` },
    { target: ".side-bottom", title: "4. Profil et réglages", body: `<b>Mon profil</b>, à remplir en premier : importe ton CV,
      réponds aux informations souvent demandées et suis « Renforcer mon profil ».<br><b>Réglages</b> : coffre chiffré,
      navigateur du robot et connexion Google, boîte de réception, sauvegardes.` },
    { target: ".top-actions", title: "5. La barre du haut", body: `De gauche à droite : le <b>?</b> rouvre ce guide, la lune
      change de thème, la <b>cloche</b> affiche les notifications (la pastille rouge en donne le nombre), et ton
      <b>avatar</b> ouvre le menu du compte (profil, réglages, mot de passe, déconnexion).` },
    { title: "C'est parti !", body: `Pour bien démarrer :<br>1. remplis <b>Mon profil</b> avec ton CV ;<br>2. active le
      <b>coffre chiffré</b> et garde ta clé de récupération ;<br>3. lance ta <b>première recherche</b>.<br><br>Le <b>?</b> de la
      barre du haut rouvre cette visite à tout moment.`, last: true },
  ];
  let index = 0;
  let layer = null;

  const seen = () => { try { return localStorage.getItem(SEEN_KEY) === "1"; } catch { return false; } };
  const markSeen = () => { try { localStorage.setItem(SEEN_KEY, "1"); } catch {} };
  const escape = value => String(value ?? "");

  function build() {
    layer = document.createElement("div");
    layer.className = "tour";
    layer.setAttribute("role", "dialog");
    layer.setAttribute("aria-modal", "true");
    layer.setAttribute("aria-labelledby", "tourTitle");
    layer.innerHTML = `<div class="tour-shade" data-side="top"></div><div class="tour-shade" data-side="bottom"></div>
      <div class="tour-shade" data-side="left"></div><div class="tour-shade" data-side="right"></div>
      <div class="tour-ring" aria-hidden="true"></div><div class="tour-pop"><i class="tour-arrow" aria-hidden="true"></i>
      <div class="tour-content"></div></div>`;
    document.body.appendChild(layer);
    document.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
  }

  function visibleTarget(step) {
    const element = step.target && document.querySelector(step.target);
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    const onScreen = rect.right > 0 && rect.left < window.innerWidth && rect.bottom > 0 && rect.top < window.innerHeight;
    return rect.width && rect.height && onScreen ? element : null;
  }

  function render() {
    const step = STEPS[index];
    const total = STEPS.length;
    layer.querySelector(".tour-content").innerHTML = `
      <div class="tour-step">Étape ${index + 1} sur ${total}</div>
      <h2 id="tourTitle">${escape(step.title)}</h2><p>${step.body}</p>
      <div class="tour-progress"><i style="--p:${Math.round(100 * (index + 1) / total)}%"></i></div>
      <div class="tour-actions"><button class="btn ghost sm" type="button" data-tour-skip>${step.last ? "Fermer" : "Passer"}</button>
        <span></span>${index ? `<button class="btn sm" type="button" data-tour-prev>Précédent</button>` : ""}
        <button class="btn primary sm" type="button" data-tour-next>${step.last ? "Terminer" : "Suivant"}</button></div>`;
    layer.querySelector("[data-tour-next]").onclick = () => (step.last ? close() : go(index + 1));
    layer.querySelector("[data-tour-prev]")?.addEventListener("click", () => go(index - 1));
    layer.querySelector("[data-tour-skip]").onclick = close;
    const target = step.target && document.querySelector(step.target);
    const inDrawer = Boolean(target?.closest("#sidebar")) && window.innerWidth <= 980;
    document.body.classList.toggle("side-open", inDrawer);
    if (inDrawer) setTimeout(place, 260);  // le tiroir coulisse avant la mise en lumière
    visibleTarget(step)?.scrollIntoView({ block: "nearest", inline: "nearest" });
    const pop = layer.querySelector(".tour-pop");
    pop.classList.remove("shown");
    place();
    requestAnimationFrame(() => requestAnimationFrame(() => pop.classList.add("shown")));
    layer.querySelector("[data-tour-next]").focus();
  }

  function place() {
    if (!layer) return;
    const step = STEPS[index];
    const element = visibleTarget(step);
    const pop = layer.querySelector(".tour-pop");
    const ring = layer.querySelector(".tour-ring");
    const shades = Object.fromEntries([...layer.querySelectorAll(".tour-shade")].map(el => [el.dataset.side, el]));
    const vw = window.innerWidth, vh = window.innerHeight, pad = 8, gap = 16;
    if (!element) {  // étape sans cible : bulle centrée, toute la page floutée
      Object.assign(shades.top.style, { left: "0", top: "0", width: `${vw}px`, height: `${vh}px` });
      for (const side of ["bottom", "left", "right"]) shades[side].style.cssText = "width:0;height:0";
      ring.style.display = "none";
      pop.dataset.place = "center";
      pop.style.left = `${Math.max(16, (vw - pop.offsetWidth) / 2)}px`;
      pop.style.top = `${Math.max(16, (vh - pop.offsetHeight) / 2)}px`;
      return;
    }
    const r = element.getBoundingClientRect();
    const box = { left: Math.max(0, r.left - pad), top: Math.max(0, r.top - pad),
                  right: Math.min(vw, r.right + pad), bottom: Math.min(vh, r.bottom + pad) };
    Object.assign(shades.top.style, { left: "0", top: "0", width: `${vw}px`, height: `${box.top}px` });
    Object.assign(shades.bottom.style, { left: "0", top: `${box.bottom}px`, width: `${vw}px`, height: `${vh - box.bottom}px` });
    Object.assign(shades.left.style, { left: "0", top: `${box.top}px`, width: `${box.left}px`, height: `${box.bottom - box.top}px` });
    Object.assign(shades.right.style, { left: `${box.right}px`, top: `${box.top}px`, width: `${vw - box.right}px`, height: `${box.bottom - box.top}px` });
    Object.assign(ring.style, { display: "block", left: `${box.left}px`, top: `${box.top}px`,
                                width: `${box.right - box.left}px`, height: `${box.bottom - box.top}px` });
    const w = pop.offsetWidth, h = pop.offsetHeight;
    // Élément de la barre du haut : bulle en dessous, comme un menu ; ailleurs, à côté de préférence.
    const inTopbar = Boolean(element.closest("#topbar"));
    let placement = inTopbar && box.bottom + gap + h <= vh - 12 ? "bottom"
      : box.right + gap + w <= vw - 12 ? "right" : box.left - gap - w >= 12 ? "left"
      : box.bottom + gap + h <= vh - 12 ? "bottom" : "top";
    let left, top;
    if (placement === "right" || placement === "left") {
      left = placement === "right" ? box.right + gap : box.left - gap - w;
      top = Math.min(Math.max(12, (box.top + box.bottom) / 2 - h / 2), vh - h - 12);
    } else {
      top = placement === "bottom" ? box.bottom + gap : box.top - gap - h;
      left = Math.min(Math.max(12, (box.left + box.right) / 2 - w / 2), vw - w - 12);
    }
    if (vw < 640) {
      // Petit écran : bulle pleine largeur, en bas si la cible laisse la place, sinon en haut (jamais sur la cible).
      placement = "bottom-sheet";
      left = 12;
      const below = vh - box.bottom, above = box.top;
      top = below >= h + gap || below >= above ? Math.max(12, vh - h - 12) : 12;
    }
    pop.dataset.place = placement;
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
    const arrow = layer.querySelector(".tour-arrow");
    if (placement === "right" || placement === "left") {
      arrow.style.top = `${Math.min(Math.max(14, (box.top + box.bottom) / 2 - top - 7), h - 28)}px`; arrow.style.left = "";
    } else {
      arrow.style.left = `${Math.min(Math.max(14, (box.left + box.right) / 2 - left - 7), w - 28)}px`; arrow.style.top = "";
    }
  }

  function go(next) {
    index = Math.max(0, Math.min(STEPS.length - 1, next));
    render();
  }

  function onKey(event) {
    if (!layer) return;
    if (event.key === "Escape") { event.stopPropagation(); close(); }
    if (event.key === "ArrowRight") { event.preventDefault(); go(index + 1); }
    if (event.key === "ArrowLeft") { event.preventDefault(); go(index - 1); }
    if (event.key === "Tab") {  // focus gardé dans la bulle
      const items = [...layer.querySelectorAll(".tour-pop button")];
      const position = items.indexOf(document.activeElement);
      event.preventDefault();
      items[(position + (event.shiftKey ? -1 : 1) + items.length) % items.length]?.focus();
    }
  }

  function close() {
    markSeen();
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", place);
    window.removeEventListener("scroll", place, true);
    layer?.remove();
    layer = null;
    if (window.innerWidth <= 980) document.body.classList.remove("side-open");
    window.dispatchEvent(new Event("jobradar:guide-closed"));
  }

  function open(start = 0) {
    if (!$("#dialog").hidden) closeDialog();
    if (!layer) build();
    index = start;
    render();
  }

  document.getElementById("guideNav")?.addEventListener("click", () => open(0));
  window.JobRadarGuide = Object.freeze({ open, get pending() { return !seen(); } });
  (window.JobRadarAuth?.ready() || Promise.resolve()).then(() => { if (!seen()) setTimeout(() => open(0), 400); });
})();
