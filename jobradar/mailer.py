"""Alertes par e-mail : réglages, mot de passe SMTP, construction du récapitulatif et envoi chiffré.

Le mot de passe SMTP n'est jamais écrit dans ``config.json``. Il est lu dans
l'ordre : variable d'environnement ``JOBRADAR_SMTP_PASSWORD``, trousseau du
système (bibliothèque ``keyring``), puis ``data/secrets.json`` (droits 0600),
qui n'est écrit que si le trousseau est indisponible. Il n'apparaît ni dans les
réponses de l'API, ni dans les journaux, ni dans les messages d'erreur.

L'envoi passe toujours par TLS (STARTTLS ou SSL, certificat vérifié) ; le
contenu des offres, non fiable, est échappé dans la partie HTML et seuls des
liens http(s) sont insérés. Aucune image distante (ni logo, ni pixel de suivi).
"""
import html
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from urllib.parse import urlsplit

from . import config, workflow

ENV_PASSWORD = "JOBRADAR_SMTP_PASSWORD"
KEYRING_SERVICE = "JobRadar"
KEYRING_ACCOUNT = "smtp"
KEYRING_TIMEOUT_S = 10  # un trousseau verrouillé peut attendre une saisie : on ne l'attend pas indéfiniment
SMTP_TIMEOUT_S = 20
TEST_INTERVAL_S = 10
SECTION_LIMIT = 10  # éléments détaillés par section ; les suivants sont seulement comptés
APP_URL = "http://127.0.0.1:8765"
KINDS = ("high_score_offer", "follow_up_due", "interview_reminder")
SECURITIES = ("starttls", "ssl")

NAVY, RADAR, MINT = "#0B1F3A", "#087CFA", "#00D99B"


class SendError(RuntimeError):
    """Échec d'envoi, avec un message en français sans aucun secret."""


class RateLimited(RuntimeError):
    """Essai d'envoi demandé trop tôt après le précédent."""


class SecretUnavailable(RuntimeError):
    """Trousseau absent, verrouillé ou qui ne répond pas."""


# ---------------------------------------------------------------- réglages

_ADDRESS = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def is_address(value: str) -> bool:
    """Adresse simple « nom@domaine.tld », sans nom affiché ni caractère de contrôle."""
    name, addr = parseaddr(value)
    return bool(value) and not name and addr == value and len(value) <= 254 and bool(_ADDRESS.fullmatch(value))


def _text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} : texte attendu.")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{label} : {limit} caractères au maximum.")
    if _CONTROL.search(value):
        raise ValueError(f"{label} : caractères de contrôle interdits.")
    return value


def _address(value: object, label: str) -> str:
    value = _text(value, label, 254)
    if value and not is_address(value):
        raise ValueError(f"{label} invalide : « {value} ». Indique une adresse seule, comme prenom.nom@exemple.fr.")
    return value


def validate_email_config(data: dict) -> dict:
    """Réglages e-mail complets et normalisés ; lève ValueError avec un message en français."""
    unknown = set(data) - set(config.DEFAULT_CONFIG["email"])
    if unknown:
        raise ValueError(f"Réglage e-mail inconnu : {', '.join(sorted(unknown))}.")
    merged = json.loads(json.dumps(config.DEFAULT_CONFIG["email"])) | data
    enabled = merged["enabled"]
    if not isinstance(enabled, bool):
        raise ValueError("Activation : booléen attendu.")
    host = _text(merged["smtp_host"], "Serveur SMTP", 253).lower()
    if host and not _HOST.fullmatch(host):
        raise ValueError("Serveur SMTP invalide : indique un nom d'hôte comme smtp.gmail.com (sans http:// ni port).")
    port = merged["smtp_port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Port SMTP invalide : nombre entier entre 1 et 65535 attendu.")
    if merged["security"] not in SECURITIES:
        raise ValueError("Sécurité invalide : « starttls » ou « ssl » attendu.")
    kinds = merged["kinds"]
    if not isinstance(kinds, list) or any(k not in KINDS for k in kinds):
        raise ValueError(f"Types d'alerte invalides : sous-ensemble de {', '.join(KINDS)} attendu.")
    return {
        "enabled": enabled,
        "to": _address(merged["to"], "Adresse de réception"),
        "from": _address(merged["from"], "Adresse d'expédition"),
        "smtp_host": host,
        "smtp_port": port,
        "security": merged["security"],
        "username": _text(merged["username"], "Identifiant", 320),
        "kinds": [k for k in KINDS if k in kinds],
    }


def email_config(cfg: dict | None = None) -> dict:
    """Réglages e-mail en vigueur ; une section invalide désactive les alertes (avec un avertissement)."""
    cfg = config.load_config() if cfg is None else cfg
    section = cfg.get("email")
    try:
        if not isinstance(section, dict):
            raise ValueError("objet JSON attendu")
        return validate_email_config(section)
    except ValueError as e:
        print(f"config.json : réglage « email » invalide ({e}), alertes par e-mail désactivées.", file=sys.stderr)
        return validate_email_config({})


def sender(conf: dict) -> str:
    """Adresse d'expédition : « from », ou l'identifiant s'il s'agit d'une adresse."""
    return conf["from"] or (conf["username"] if is_address(conf["username"]) else "")


def check_ready(conf: dict, password_available: bool) -> None:
    """Vérifie qu'un envoi est possible ; lève ValueError en listant ce qui manque."""
    missing = [label for label, ok in (
        ("l'adresse de réception", conf["to"]),
        ("le serveur SMTP", conf["smtp_host"]),
        ("l'identifiant", conf["username"]),
        ("le mot de passe", password_available),
    ) if not ok]
    if missing:
        listed = ", ".join(missing[:-1]) + (" et " if len(missing) > 1 else "") + missing[-1]
        raise ValueError(f"Renseigne {listed} pour envoyer des alertes par e-mail.")
    if not sender(conf):
        raise ValueError("Indique l'adresse d'expédition : ton identifiant n'est pas une adresse e-mail.")


# ---------------------------------------------------------------- mot de passe

def _secrets_path():
    return config.DATA_DIR / "secrets.json"


def _backend():
    """Trousseau utilisable, ou None (bibliothèque absente, aucun service, backend « fail » ou « null »)."""
    try:
        import keyring

        backend = keyring.get_keyring()
        return backend if backend.priority > 0 else None
    except Exception:
        return None


def keyring_available() -> bool:
    return _backend() is not None


def _keyring_call(method: str, *args: str):
    """Appel au trousseau dans un fil séparé, abandonné après KEYRING_TIMEOUT_S secondes."""
    backend = _backend()
    if backend is None:
        raise SecretUnavailable("trousseau du système indisponible")
    outcome: dict = {}

    def run() -> None:
        try:
            outcome["value"] = getattr(backend, method)(*args)
        except Exception as e:  # PasswordDeleteError, KeyringLocked, erreurs D-Bus…
            outcome["error"] = e

    worker = threading.Thread(target=run, name="jobradar-keyring", daemon=True)
    worker.start()
    worker.join(KEYRING_TIMEOUT_S)
    if worker.is_alive():
        raise SecretUnavailable("le trousseau ne répond pas (verrouillé ?)")
    if "error" in outcome:
        error = outcome["error"]
        if type(error).__name__ == "PasswordDeleteError":
            return None
        raise SecretUnavailable(f"trousseau inaccessible ({type(error).__name__})")
    return outcome.get("value")


def _read_file() -> dict:
    try:
        data = json.loads(_secrets_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_file(data: dict) -> None:
    """Écrit secrets.json avec des droits 0600 (dès la création, quand l'OS le permet)."""
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def password_source() -> tuple[str | None, str | None]:
    """(mot de passe, provenance) : « env », « keyring », « file », ou (None, None)."""
    env = os.environ.get(ENV_PASSWORD, "")
    if env:
        return env, "env"
    try:
        stored = _keyring_call("get_password", KEYRING_SERVICE, KEYRING_ACCOUNT)
    except SecretUnavailable:
        stored = None
    if stored:
        return stored, "keyring"
    stored = _read_file().get("smtp_password")
    if isinstance(stored, str) and stored:
        return stored, "file"
    return None, None


def store_password(password: str) -> str:
    """Enregistre le mot de passe dans le trousseau, ou à défaut dans secrets.json ; renvoie l'emplacement."""
    try:
        _keyring_call("set_password", KEYRING_SERVICE, KEYRING_ACCOUNT, password)
    except SecretUnavailable:
        _write_file(_read_file() | {"smtp_password": password})
        return "file"
    _forget_file_password()  # pas de copie en clair laissée par un ancien repli
    return "keyring"


def _forget_file_password() -> None:
    data = _read_file()
    if "smtp_password" in data:
        data.pop("smtp_password")
        if data:
            _write_file(data)
        else:
            _secrets_path().unlink(missing_ok=True)


def clear_password() -> None:
    """Supprime le mot de passe enregistré (trousseau et repli) ; la variable d'environnement reste maître."""
    try:
        _keyring_call("delete_password", KEYRING_SERVICE, KEYRING_ACCOUNT)
    except SecretUnavailable:
        pass
    _forget_file_password()


# ---------------------------------------------------------------- état du dernier envoi

def _status_path():
    return config.DATA_DIR / "email_status.json"


def read_status() -> dict:
    try:
        data = json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return {"last_sent_at": data.get("last_sent_at"), "last_error": data.get("last_error")}


def record_status(error: str | None, at: datetime) -> None:
    """Mémorise le résultat du dernier envoi (réussi : date ; échoué : message d'erreur)."""
    data = read_status()
    if error is None:
        data = {"last_sent_at": at.isoformat(timespec="seconds"), "last_error": None}
    else:
        data["last_error"] = error
    path = _status_path()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- API des réglages

def settings_view() -> dict:
    """Réglages pour l'interface : jamais le mot de passe, seulement sa présence et sa provenance."""
    _, storage = password_source()
    return email_config() | {
        "password_set": storage is not None,
        "password_storage": storage,
        "keyring_available": keyring_available(),
    } | read_status()


def update_settings(fields: dict, password: str | None = None, clear: bool = False) -> dict:
    """Applique une mise à jour partielle des réglages ; ``password`` vide ou None = inchangé.

    Tout est validé avant la moindre écriture : un refus ne modifie ni le
    mot de passe enregistré ni config.json.
    """
    conf = validate_email_config(email_config() | fields)
    if password is not None and (not isinstance(password, str) or len(password) > 1024
                                 or any(c in password for c in "\r\n\x00")):
        raise ValueError("Mot de passe invalide (1024 caractères au maximum, sans retour à la ligne).")
    if conf["enabled"]:
        if password:
            available = True
        elif clear:
            available = bool(os.environ.get(ENV_PASSWORD))
        else:
            available = password_source()[1] is not None
        check_ready(conf, available)
    if clear:
        clear_password()
    if password:
        store_password(password)
    config.save_config_section("email", conf)
    return settings_view()


# ---------------------------------------------------------------- envoi

def _reply(e: smtplib.SMTPResponseException) -> str:
    text = e.smtp_error.decode("utf-8", "replace") if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
    text = " ".join(text.split())[:200]
    return f" (réponse du serveur : {e.smtp_code} {text})" if text else f" (code {e.smtp_code})"


def explain(error: BaseException, conf: dict) -> str:
    """Traduit une erreur d'envoi en message français exploitable."""
    where = f"{conf['smtp_host']}:{conf['smtp_port']}"
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return ("Authentification refusée : identifiant ou mot de passe incorrect. Gmail exige un mot de passe "
                "d'application ; Outlook peut refuser le mot de passe classique." + _reply(error))
    if isinstance(error, smtplib.SMTPNotSupportedError):
        return ("Le serveur ne propose pas STARTTLS : rien n'a été envoyé (aucun envoi en clair). "
                "Essaie la sécurité SSL sur le port 465.")
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "Adresse de réception refusée par le serveur."
    if isinstance(error, smtplib.SMTPSenderRefused):
        return ("Adresse d'expédition refusée : la plupart des fournisseurs exigent l'adresse du compte."
                + _reply(error))
    if isinstance(error, smtplib.SMTPServerDisconnected):
        return (f"Connexion fermée par {where} : vérifie que la sécurité correspond au port "
                "(STARTTLS : 587, SSL : 465).")
    if isinstance(error, smtplib.SMTPResponseException):
        return "Le serveur SMTP a refusé l'envoi" + _reply(error) + "."
    if isinstance(error, smtplib.SMTPException):
        return f"Erreur SMTP : {type(error).__name__}."
    if isinstance(error, ssl.SSLCertVerificationError):
        reason = getattr(error, "verify_message", None) or "vérification impossible"
        return f"Certificat du serveur invalide ({reason}) : envoi annulé pour ta sécurité."
    if isinstance(error, ssl.SSLError):
        return ("Échec de la négociation TLS : vérifie que la sécurité correspond au port "
                "(STARTTLS : 587, SSL : 465).")
    if isinstance(error, socket.gaierror):
        return f"Serveur SMTP introuvable : « {conf['smtp_host']} ». Vérifie son nom et ta connexion Internet."
    if isinstance(error, TimeoutError | socket.timeout):
        return (f"Délai dépassé ({SMTP_TIMEOUT_S} s) en contactant {where} : vérifie le port, la sécurité "
                "et ta connexion (pare-feu, réseau qui bloque le SMTP).")
    if isinstance(error, ConnectionRefusedError):
        return f"Connexion refusée par {where} : vérifie le port et la sécurité."
    if isinstance(error, OSError):
        return f"Erreur réseau en contactant {where} : {error.strerror or type(error).__name__}."
    return f"Envoi impossible : {type(error).__name__}."


def send(msg: EmailMessage, conf: dict, password: str) -> None:
    """Envoie ``msg`` via TLS ; lève SendError avec un message traduit, sans le mot de passe."""
    context = ssl.create_default_context()
    host, port = conf["smtp_host"], conf["smtp_port"]
    try:
        if conf["security"] == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S)
        with server:
            if conf["security"] != "ssl":
                server.ehlo()
                server.starttls(context=context)  # SMTPNotSupportedError si absent : jamais de repli en clair
                server.ehlo()
            server.login(conf["username"], password)
            server.send_message(msg, from_addr=sender(conf), to_addrs=[conf["to"]])
    except Exception as e:
        message = explain(e, conf)
        raise SendError(message.replace(password, "•••") if password else message) from None


def build_message(conf: dict, subject: str, text: str, html_body: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr(("JobRadar", sender(conf)))
    msg["To"] = conf["to"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="jobradar.local")
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    if html_body is not None:
        msg.add_alternative(html_body, subtype="html")
    return msg


_test_lock = threading.Lock()
_last_test = -float("inf")


def send_test(now: datetime) -> None:
    """E-mail de test avec les réglages enregistrés ; au plus un essai toutes les 10 s."""
    global _last_test
    conf = email_config()
    password, _ = password_source()
    check_ready(conf, bool(password))
    with _test_lock:
        wait = TEST_INTERVAL_S - (time.monotonic() - _last_test)
        if wait > 0:
            raise RateLimited(f"Patiente encore {int(wait) + 1} s avant un nouvel essai.")
        _last_test = time.monotonic()
    text = ("Bonne nouvelle : JobRadar arrive à t'écrire.\n\n"
            "Tu recevras ici un récapitulatif après chaque actualisation, s'il y a du nouveau "
            "(offres très pertinentes, relances à faire, entretiens à venir).\n\n" + _footer_text())
    body = _layout("E-mail de test", "<p style=\"margin:0 0 12px\">Bonne nouvelle : JobRadar arrive à t'écrire.</p>"
                   "<p style=\"margin:0\">Tu recevras ici un récapitulatif après chaque actualisation, s'il y a "
                   "du nouveau (offres très pertinentes, relances à faire, entretiens à venir).</p>")
    try:
        send(build_message(conf, "JobRadar — e-mail de test", text, body), conf, password)
    except SendError as e:
        record_status(str(e), now)
        raise
    record_status(None, now)


# ---------------------------------------------------------------- récapitulatif

_DAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MONTHS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
           "novembre", "décembre")


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _day(iso: str | None, zone) -> str:
    at = _parse(iso)
    if not at:
        return ""
    local = at.astimezone(zone)
    return f"{local.day} {_MONTHS[local.month - 1]} {local.year}"


def _when(iso: str | None, zone) -> str:
    at = _parse(iso)
    if not at:
        return ""
    local = at.astimezone(zone)
    return f"{_DAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]} à {local:%H:%M}"


def safe_link(url: object) -> str | None:
    """Lien http(s) absolu sans espace ni caractère de contrôle ; sinon None."""
    if not isinstance(url, str) or not url or _CONTROL.search(url) or " " in url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme.lower() in ("http", "https") and parts.netloc else None


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def digest_subject(sections: dict[str, list[dict]], zone, now: datetime) -> str:
    """« JobRadar — 3 offres très pertinentes · 1 relance · 1 entretien demain »."""
    parts = []
    if offers := sections.get("high_score_offer"):
        parts.append(_plural(len(offers), "offre très pertinente", "offres très pertinentes"))
    if follows := sections.get("follow_up_due"):
        parts.append(_plural(len(follows), "relance", "relances"))
    if interviews := sections.get("interview_reminder"):
        today = now.astimezone(zone).date()
        days = {(_parse(i.get("starts_at")) or now).astimezone(zone).date() for i in interviews}
        when = ("aujourd'hui" if days == {today} else "demain" if days == {today + timedelta(days=1)}
                else "à venir")
        parts.append(_plural(len(interviews), "entretien", "entretiens") + f" {when}")
    return "JobRadar — " + " · ".join(parts)


def _footer_text() -> str:
    return (f"Ouvrir JobRadar : {APP_URL} (sur ton PC)\n"
            "Pour ne plus recevoir ces e-mails : JobRadar → Réglages → Alertes par e-mail, "
            "puis décoche « Activer ».")


def _layout(title: str, content: str) -> str:
    """Gabarit HTML sobre : styles en ligne, lisible sans images ni feuille de style externe."""
    e = html.escape
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title></head>
<body style="margin:0;padding:0;background:#F6F9FC;color:{NAVY};font-family:'Work Sans',Segoe UI,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.5">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F6F9FC"><tr><td align="center" style="padding:20px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#FFFFFF;border:1px solid #E2E8F0;border-radius:12px;border-collapse:separate;overflow:hidden">
<tr><td style="background:{NAVY};padding:18px 24px;border-bottom:4px solid {MINT}">
<div style="color:#FFFFFF;font-size:20px;font-weight:700;letter-spacing:-0.02em">JobRadar</div>
<div style="color:#8EA3BF;font-size:13px">{e(title)}</div></td></tr>
<tr><td style="padding:20px 24px">{content}</td></tr>
<tr><td style="padding:16px 24px;background:#EEF3F8;color:#64748B;font-size:12.5px">
<p style="margin:0 0 6px">Ouvrir JobRadar : <a href="{APP_URL}" style="color:{RADAR}">{APP_URL}</a> (sur ton PC)</p>
<p style="margin:0">Pour ne plus recevoir ces e-mails : JobRadar → Réglages → Alertes par e-mail, puis décoche « Activer ».</p>
</td></tr></table></td></tr></table></body></html>"""


def _section_html(title: str, rows: list[str], hidden: int) -> str:
    more = (f'<p style="margin:4px 0 0;color:#64748B;font-size:13px">… et {hidden} autre{"s" if hidden > 1 else ""} '
            "dans JobRadar.</p>" if hidden else "")
    return (f'<h2 style="margin:0 0 10px;font-size:16px;color:{NAVY};border-left:3px solid {RADAR};'
            f'padding-left:8px">{html.escape(title)}</h2>' + "".join(rows) + more
            + '<div style="height:18px"></div>')


def _card(inner: str) -> str:
    return f'<div style="border:1px solid #E2E8F0;border-radius:10px;padding:12px 14px;margin:0 0 10px">{inner}</div>'


def _who(item: dict) -> str:
    title, company = item.get("title") or "", item.get("company") or ""
    # Le titre par défaut d'un entretien contient déjà l'entreprise : pas de doublon.
    return title if company and company in title else " — ".join(x for x in (title, company) if x)


def digest(sections: dict[str, list[dict]], zone, now: datetime) -> tuple[str, str, str]:
    """(objet, texte brut, HTML) du récapitulatif ; sections dans l'ordre offres, relances, entretiens."""
    e = html.escape
    text, parts = [], []
    if offers := sections.get("high_score_offer"):
        shown = sorted(offers, key=lambda o: -(o.get("score") or 0))[:SECTION_LIMIT]
        text.append("OFFRES TRÈS PERTINENTES")
        rows = []
        for o in shown:
            link = safe_link(o.get("url"))
            reasons = [str(r) for r in (o.get("reasons") or [])[:3]]
            text.append(f"- {o.get('title') or ''}" + (f" — {o['company']}" if o.get("company") else "")
                        + f" · score {o.get('score')}")
            if o.get("verdict"):
                text.append(f"  {o['verdict']}")
            text.extend(f"  • {r}" for r in reasons)
            if link:
                text.append(f"  {link}")
            title = e(o.get("title") or "Offre")
            heading = (f'<a href="{e(link)}" style="color:{RADAR};text-decoration:none">{title}</a>' if link else title)
            rows.append(_card(
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
                f'<td style="font-weight:600;font-size:15px">{heading}</td>'
                f'<td align="right" valign="top" style="width:56px"><span style="display:inline-block;'
                f'background:#E6FBF4;color:#047857;border-radius:99px;padding:2px 10px;font-weight:700;'
                f'font-size:13px">{e(str(o.get("score")))}</span></td></tr></table>'
                + (f'<div style="color:#64748B;font-size:13.5px">{e(o["company"])}</div>' if o.get("company") else "")
                + (f'<div style="margin-top:6px;font-weight:600;font-size:13.5px">{e(o["verdict"])}</div>'
                   if o.get("verdict") else "")
                + (('<ul style="margin:6px 0 0;padding-left:18px;font-size:13.5px">'
                    + "".join(f"<li>{e(r)}</li>" for r in reasons) + "</ul>") if reasons else "")
                + (f'<div style="margin-top:8px"><a href="{e(link)}" style="color:{RADAR};font-weight:600;'
                   f'font-size:13.5px">Voir l\'offre →</a></div>' if link else "")
            ))
        parts.append(_section_html("Offres très pertinentes", rows, len(offers) - len(shown)))
        text.append("")
    if follows := sections.get("follow_up_due"):
        shown = follows[:SECTION_LIMIT]
        text.append("RELANCES À FAIRE")
        rows = []
        for a in shown:
            sent = _day(a.get("applied_at"), zone)
            text.append(f"- {_who(a)}" + (f" · envoyée le {sent}" if sent else ""))
            rows.append(_card(f'<div style="font-weight:600">{e(_who(a))}</div>'
                              + (f'<div style="color:#64748B;font-size:13.5px">Candidature envoyée le {e(sent)}</div>'
                                 if sent else "")))
        parts.append(_section_html("Relances à faire", rows, len(follows) - len(shown)))
        text.append("")
    if interviews := sections.get("interview_reminder"):
        shown = sorted(interviews, key=lambda i: i.get("starts_at") or "")[:SECTION_LIMIT]
        text.append("ENTRETIENS À VENIR")
        rows = []
        for i in shown:
            when = _when(i.get("starts_at"), zone)
            text.append(f"- {_who(i)} · {when}" + (f" · {i['location']}" if i.get("location") else ""))
            rows.append(_card(f'<div style="font-weight:600">{e(_who(i))}</div>'
                              f'<div style="font-size:13.5px;color:{RADAR};font-weight:600">{e(when.capitalize())}</div>'
                              + (f'<div style="color:#64748B;font-size:13.5px">{e(i["location"])}</div>'
                                 if i.get("location") else "")))
        parts.append(_section_html("Entretiens à venir", rows, len(interviews) - len(shown)))
        text.append("")
    subject = digest_subject(sections, zone, now)
    plain = "\n".join(text) + "\n" + _footer_text() + "\n"
    return subject, plain, _layout("Tes alertes du " + _day(now.isoformat(), zone), "".join(parts))


def send_digest(conf: dict, sections: dict[str, list[dict]], now: datetime) -> None:
    """Construit et envoie le récapitulatif ; lève SendError (ou ValueError si rien n'est configuré)."""
    password, _ = password_source()
    if not password:
        raise ValueError("Mot de passe SMTP introuvable : trousseau verrouillé ou inaccessible ? "
                         "Enregistre-le à nouveau dans Réglages ou définis JOBRADAR_SMTP_PASSWORD.")
    check_ready(conf, True)
    zone = workflow.validate_timezone(config.settings().timezone)
    subject, text, body = digest(sections, zone, now)
    send(build_message(conf, subject, text, body), conf, password)
