// Connexion locale (identifiant + mot de passe) et barre supérieure : avatar, nom, menu du compte.
(() => {
  let resolveReady;
  const readyPromise = new Promise(resolve => (resolveReady = resolve));
  let state = { account: false, authenticated: false, username: null };
  let avatarId = "";  // avatar choisi dans « Mon profil » ; sinon les initiales

  async function call(path, body, method = "POST") {
    const response = await fetch(path, { method, headers: { "Content-Type": "application/json" },
                                         body: body ? JSON.stringify(body) : undefined });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || response.statusText);
    return data;
  }

  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const EYE = `<svg viewBox="0 0 24 24" class="ic stroke" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>`;
  const EYE_OFF = `<svg viewBox="0 0 24 24" class="ic stroke" aria-hidden="true"><path d="M3 3l18 18"/><path d="M10.6 5.1A10.4 10.4 0 0 1 12 5c6.5 0 10 7 10 7a17.6 17.6 0 0 1-3.2 4.1"/><path d="M6.6 6.6C3.8 8.4 2 12 2 12s3.5 7 10 7a9.9 9.9 0 0 0 5.4-1.6"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>`;

  // Champ mot de passe avec un bouton œil pour afficher ou masquer la saisie.
  function passwordField(label, name, autocomplete, extra = "") {
    return `<label class="field"><span>${label}</span><span class="auth-password">
      <input name="${name}" type="password" autocomplete="${autocomplete}" required ${extra}>
      <button type="button" class="pw-toggle" data-pw-toggle aria-label="Afficher le mot de passe" aria-pressed="false"
        title="Afficher le mot de passe">${EYE}</button></span></label>`;
  }

  function bindPasswordToggles(root) {
    root.querySelectorAll("[data-pw-toggle]").forEach(button => (button.onclick = () => {
      const input = button.parentElement.querySelector("input");
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      button.innerHTML = show ? EYE_OFF : EYE;
      const label = show ? "Masquer le mot de passe" : "Afficher le mot de passe";
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
      button.setAttribute("aria-pressed", String(show));
      input.focus();
    }));
  }

  function initials(name) {
    const parts = String(name || "?").split(/[._@\s-]+/).filter(Boolean);
    return ((parts[0] || "?")[0] + (parts[1]?.[0] || "")).toUpperCase();
  }

  // Remplit les pastilles du compte : image de l'avatar, ou initiales si aucun avatar ou si l'image manque.
  function paintAvatar() {
    const name = state.username || "Invité";
    document.querySelectorAll("#accountSlot .avatar").forEach(span => {
      span.textContent = "";
      span.classList.toggle("has-img", Boolean(avatarId));
      if (!avatarId) { span.textContent = initials(name); return; }
      const image = new Image();
      image.alt = "";
      image.src = `/static/img/avatars/${avatarId}.webp`;
      image.onerror = () => { avatarId = ""; paintAvatar(); };
      span.appendChild(image);
    });
  }

  function setAvatar(id) {
    avatarId = /^[a-z0-9-]{1,40}$/.test(String(id || "")) ? id : "";
    paintAvatar();
  }

  // Profil illisible (coffre verrouillé, première ouverture) : les initiales restent affichées.
  async function loadAvatar() {
    try { setAvatar((await call("/api/profile", null, "GET")).avatar); } catch { /* initiales */ }
  }

  function screen(mode, message = "") {
    const setup = mode === "setup";
    let root = document.getElementById("authScreen");
    if (!root) {
      root = document.createElement("div");
      root.id = "authScreen";
      root.className = "auth-screen";
      document.body.appendChild(root);
    }
    document.body.classList.add("auth-locked");
    root.innerHTML = `<form class="auth-card" novalidate>
      <img src="/static/img/favicon.png" alt="" class="auth-logo" width="56" height="56">
      <h1>${setup ? "Crée ton compte JobRadar" : "Connexion à JobRadar"}</h1>
      <p class="muted">${setup ? "Ce compte protège l'accès à JobRadar sur cet ordinateur. Il reste local : rien n'est envoyé en ligne."
        : "Saisis ton identifiant et ton mot de passe."}</p>
      <label class="field"><span>Identifiant</span><input name="username" autocomplete="username" required minlength="3"
        maxlength="64" autofocus></label>
      ${passwordField("Mot de passe", "password", setup ? "new-password" : "current-password", `minlength="${setup ? 8 : 1}"`)}
      ${setup ? `${passwordField("Confirmer le mot de passe", "confirm", "new-password")}
        <p class="muted small">Au moins 8 caractères. Il n'existe pas de récupération en ligne : choisis un mot de passe dont tu te souviendras.</p>` : ""}
      <p class="auth-error" role="alert">${escape(message)}</p>
      <button class="btn primary block" type="submit">${setup ? "Créer mon compte" : "Se connecter"}</button>
    </form>`;
    const form = root.querySelector("form");
    bindPasswordToggles(root);
    form.onsubmit = async event => {
      event.preventDefault();
      const { username, password } = form.elements;
      const error = root.querySelector(".auth-error");
      if (setup && password.value !== form.elements.confirm.value) { error.textContent = "Les deux mots de passe diffèrent."; return; }
      form.querySelector("[type=submit]").disabled = true;
      try {
        state = await call(setup ? "/api/auth/setup" : "/api/auth/login", { username: username.value.trim(), password: password.value });
        unlock();
      } catch (failure) {
        error.textContent = failure.message;
        form.querySelector("[type=submit]").disabled = false;
      }
    };
    setTimeout(() => form.elements.username.focus(), 50);
  }

  function unlock() {
    document.getElementById("authScreen")?.remove();
    document.body.classList.remove("auth-locked");
    renderTopbar();
    loadAvatar();
    resolveReady(state);
  }

  function renderTopbar() {
    const slot = document.getElementById("accountSlot");
    if (!slot) return;
    const name = state.username || "Invité";
    slot.innerHTML = `<div class="account-menu">
        <button class="account-button" type="button" aria-haspopup="menu" aria-expanded="false" id="accountButton">
          <span class="avatar" aria-hidden="true">${escape(initials(name))}</span>
          <span class="account-name">${escape(name)}</span>
          <svg viewBox="0 0 24 24" class="ic stroke chevron" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>
        </button>
        <div class="account-dropdown" role="menu" hidden id="accountDropdown">
          <div class="account-head"><span class="avatar lg">${escape(initials(name))}</span><div><b>${escape(name)}</b>
            <small class="muted">${state.account ? "Compte local JobRadar" : "Aucun compte : accès libre sur cet ordinateur"}</small></div></div>
          <button role="menuitem" type="button" data-menu="profile">Mon profil</button>
          <button role="menuitem" type="button" data-menu="settings">Réglages</button>
          <button role="menuitem" type="button" data-menu="google">Connecter un compte Google</button>
          <button role="menuitem" type="button" data-menu="guide">Guide d'utilisation</button>
          <hr>
          ${state.account ? `<button role="menuitem" type="button" data-menu="password">Changer le mot de passe</button>
            <button role="menuitem" type="button" data-menu="logout" class="danger">Se déconnecter</button>`
            : `<button role="menuitem" type="button" data-menu="setup">Créer un compte (protéger l'accès)</button>`}
        </div>
      </div>`;
    const button = document.getElementById("accountButton");
    const menu = document.getElementById("accountDropdown");
    const toggle = open => { menu.hidden = !open; button.setAttribute("aria-expanded", String(open)); };
    button.onclick = event => { event.stopPropagation(); toggle(menu.hidden); if (!menu.hidden) menu.querySelector("button")?.focus(); };
    document.addEventListener("click", event => { if (!event.target.closest?.(".account-menu")) toggle(false); });
    menu.addEventListener("keydown", event => {
      const items = [...menu.querySelectorAll("button")];
      const index = items.indexOf(document.activeElement);
      if (event.key === "Escape") { toggle(false); button.focus(); }
      if (event.key === "ArrowDown") { event.preventDefault(); items[(index + 1) % items.length].focus(); }
      if (event.key === "ArrowUp") { event.preventDefault(); items[(index - 1 + items.length) % items.length].focus(); }
    });
    menu.querySelectorAll("[data-menu]").forEach(item => (item.onclick = () => { toggle(false); action(item.dataset.menu); }));
    paintAvatar();
  }

  async function action(name) {
    if (name === "profile") document.getElementById("profileNav")?.click();
    if (name === "today") document.getElementById("todayNav")?.click();
    if (name === "settings") document.getElementById("settingsNav")?.click();
    if (name === "google") window.JobRadarGoogle?.open();
    if (name === "guide") window.JobRadarGuide?.open();
    if (name === "setup") screen("setup");
    if (name === "logout") {
      await call("/api/auth/logout").catch(() => {});
      location.reload();
    }
    if (name === "password") changePassword();
  }

  function changePassword() {
    openDialog(`<form class="m-pad" id="pwForm"><h2 id="dialogTitle">Changer le mot de passe</h2>
      ${passwordField("Mot de passe actuel", "current", "current-password")}
      ${passwordField("Nouveau mot de passe (8 caractères minimum)", "next", "new-password", 'minlength="8"')}
      <p class="muted small">Les autres sessions ouvertes seront déconnectées.</p><p class="auth-error" role="alert"></p></form>
      <div class="m-foot"><button class="btn" type="button" data-pw-cancel>Annuler</button><button class="btn primary" type="button" data-pw-save>Enregistrer</button></div>`);
    bindPasswordToggles(document.getElementById("pwForm"));
    document.querySelector("[data-pw-cancel]").onclick = closeDialog;
    document.querySelector("[data-pw-save]").onclick = async () => {
      const form = document.getElementById("pwForm");
      try {
        await call("/api/auth/password", { current: form.elements.current.value, new: form.elements.next.value }, "PUT");
        closeDialog();
        toast("Mot de passe modifié");
      } catch (failure) { form.querySelector(".auth-error").textContent = failure.message; }
    };
  }

  async function start() {
    try { state = await call("/api/auth/status", null, "GET"); } catch { state = { account: false, authenticated: false }; }
    if (!state.account) {
      // Premier lancement : JobRadar propose de créer le compte (accès libre tant qu'il n'existe pas).
      try { if (localStorage.getItem("jobradar-compte-plus-tard") === "1") return unlock(); } catch {}
      screen("setup");
      const later = document.createElement("button");
      later.type = "button"; later.className = "btn ghost block"; later.textContent = "Plus tard";
      later.onclick = () => { try { localStorage.setItem("jobradar-compte-plus-tard", "1"); } catch {} unlock(); };
      document.querySelector(".auth-card").appendChild(later);
    } else if (!state.authenticated) {
      screen("login");
    } else {
      unlock();
    }
  }

  window.JobRadarAuth = Object.freeze({
    ready: () => readyPromise,
    require: message => screen("login", message || "Ta session a expiré : reconnecte-toi."),
    setAvatar,
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
