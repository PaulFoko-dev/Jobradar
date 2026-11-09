"""Lecture locale de la boîte de réception (IMAP) : réponses des recruteurs et accusés de réception.

Rien n'est envoyé à l'IA : chaque message récent est rapproché d'une candidature envoyée (nom ou domaine de
l'entreprise) puis classé par règles (accusé de réception, refus, entretien). Le statut n'est jamais changé seul :
JobRadar crée une notification qui le **propose**. Seule exception, sûre : un accusé de réception arrivé après
l'envoi d'une candidature dont la confirmation n'avait pas été reconnue confirme cet envoi (aucun doublon possible).
"""
from __future__ import annotations

import email
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

from . import config, db, mailer
from .sources.common import norm

DEFAULT = {"enabled": False, "imap_host": "", "days": 30}
RULES = (
    ("refus", ("malheureusement", "ne pas donner suite", "pas retenue", "pas retenu", "ne correspond pas",
               "unfortunately", "not moving forward", "will not be moving", "decided to pursue other candidates",
               "regret to inform", "autre candidat")),
    ("entretien", ("entretien", "interview", "echange telephonique", "rencontrer", "disponibilites pour",
                   "schedule a call", "next step", "prochaine etape")),
    ("accuse", ("bien recu votre candidature", "avons bien recu", "accuse de reception", "candidature a bien ete",
                "thank you for applying", "we have received your application", "application received",
                "merci pour votre candidature", "thanks for applying")),
)
PROPOSED_STATUS = {"refus": "refuse", "entretien": "entretien_rh"}
MAX_MESSAGES = 200


def settings() -> dict:
    section = config.load_config().get("inbox")
    return DEFAULT | (section if isinstance(section, dict) else {})


def _header(value: str | None) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except (ValueError, LookupError):
        return value or ""


def _body(message: email.message.Message) -> str:
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_type() in {"text/plain", "text/html"} and not part.get_filename():
            try:
                text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            except (AttributeError, LookupError):
                continue
            return re.sub(r"<[^>]+>", " ", text)[:20_000]
    return ""


def classify(subject: str, body: str) -> str | None:
    """Refus avant entretien avant accusé : un refus poli contient souvent « merci pour votre candidature »."""
    text = norm(f"{subject} {body}")
    for kind, words in RULES:
        if any(f" {norm(word)} " in f" {text} " for word in words):
            return kind
    return None


def _company_key(name: str | None) -> str:
    words = [word for word in norm(name).split() if word not in {"groupe", "group", "sa", "sas", "france", "the"}]
    return " ".join(words[:2])


def match_application(sender: str, subject: str, body: str, applications: list[dict]) -> dict | None:
    """Candidature envoyée dont l'entreprise apparaît dans l'expéditeur, l'objet ou le début du message."""
    name, address = parseaddr(sender)
    domain = norm(address.split("@")[-1].split(".")[-2] if "@" in address and "." in address else "")
    haystack = f" {norm(name)} {domain} {norm(subject)} {norm(body[:3000])} "
    found = [application for application in applications
             if (key := _company_key(application.get("company"))) and len(key) >= 3 and f" {key} " in haystack]
    return found[0] if len(found) == 1 else None  # ambigu : rien plutôt qu'une erreur


def _connect():
    conf = mailer.email_config()
    password, _ = mailer.password_source()
    host = settings()["imap_host"] or re.sub(r"^smtp\.", "imap.", conf["smtp_host"])
    user = conf["username"] or conf["from"]
    if not host or not user or not password:
        raise RuntimeError("Boîte de réception non configurée : renseigne d'abord l'e-mail dans les Réglages.")
    client = imaplib.IMAP4_SSL(host, timeout=30)
    client.login(user, password)
    return client


def fetch(days: int) -> list[dict]:
    client = _connect()
    try:
        client.select("INBOX", readonly=True)  # lecture seule : aucun message marqué comme lu
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        _status, data = client.search(None, f'(SINCE "{since}")')
        messages = []
        for number in (data[0].split() if data and data[0] else [])[-MAX_MESSAGES:]:
            _status, raw = client.fetch(number, "(BODY.PEEK[])")
            if not raw or not isinstance(raw[0], tuple):
                continue
            message = email.message_from_bytes(raw[0][1])
            try:
                received = parsedate_to_datetime(message.get("Date"))
            except (TypeError, ValueError):
                received = None
            messages.append({"id": _header(message.get("Message-ID")) or number.decode(),
                             "from": _header(message.get("From")), "subject": _header(message.get("Subject")),
                             "body": _body(message), "received": received})
        return messages
    finally:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def scan(messages: list[dict] | None = None) -> dict:
    """Rapproche les messages récents des candidatures envoyées ; propose des statuts, confirme les accusés."""
    from . import application_flow, notifications

    conf = settings()
    if messages is None:
        if not conf["enabled"]:
            return {"enabled": False}
        messages = fetch(int(conf["days"]))
    applications = [application for application in db.list_applications()
                    if application.get("status") not in {"consultee", "abandon"} or application.get("applied_at_utc")]
    pending = {attempt["application_id"]: attempt for attempt in db.list_unconfirmed_submissions()}
    proposals, confirmed = 0, 0
    for message in messages:
        kind = classify(message["subject"], message["body"])
        if kind is None:
            continue
        application = match_application(message["from"], message["subject"], message["body"], applications)
        if application is None:
            continue
        attempt = pending.get(application["id"])
        if kind == "accuse" and attempt is not None:
            application_flow.confirm_assisted_sent(attempt["id"])
            confirmed += 1
            continue
        status = PROPOSED_STATUS.get(kind)
        if status and application.get("status") != status:
            created = notifications.response_detected(application, status, message["subject"],
                                                      dedupe=f"reponse:{application['id']}:{message['id']}")
            proposals += int(bool(created))
    return {"enabled": True, "messages": len(messages), "proposals": proposals, "confirmed": confirmed}


CODE = re.compile(r"(?<![\d-])(\d{6}|\d{4}|\d{8})(?![\d-])")
LINK = re.compile(r"https?://[^\s\"'<>]+")
VERIFY_WORDS = ("verif", "verify", "confirm", "activate", "activation", "valid", "token", "otp")


def extract_verification(message: dict, domains: set[str]) -> dict | None:
    """Code ou lien de vérification d'un message envoyé par le site de candidature (lecture locale)."""
    sender = parseaddr(message["from"])[1].lower()
    body = message["body"]
    sender_ok = any(sender.endswith(domain) for domain in domains if domain)
    links = [link.rstrip(".,);") for link in LINK.findall(body)
             if any(word in link.lower() for word in VERIFY_WORDS)
             and any(domain in link.lower() for domain in domains if domain)]
    text = norm(f"{message['subject']} {body}")
    code = None
    if any(word in text for word in ("code", "verification", "otp", "one time")):
        found = CODE.findall(re.sub(r"https?://\S+", " ", f"{message['subject']} {body}"))
        code = found[0] if found else None
    if not sender_ok and not links:
        return None
    return {"code": code, "link": links[0] if links else None} if (code or links) else None


def find_verification(domains: set[str], since: datetime) -> dict | None:
    """Dernier code ou lien de vérification reçu depuis ``since`` d'un des domaines du site ; None sinon."""
    if not settings()["enabled"]:
        return None
    for message in reversed(fetch(1)):
        received = message.get("received")
        if received is not None and received.astimezone(timezone.utc) < since:
            continue
        found = extract_verification(message, domains)
        if found:
            return found
    return None
