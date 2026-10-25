"""Tests des alertes par e-mail : envoi SMTP simulé, récapitulatif, secret, API et migration 9.

Aucun e-mail réel n'est envoyé : ``smtplib.SMTP`` et ``SMTP_SSL`` sont
remplacés par des doubles, et le trousseau est un trousseau en mémoire.
"""
import json
import os
import smtplib
import socket
import sqlite3
import ssl
import stat
import sys
from datetime import timedelta

import keyring
import pytest
from conftest import FIXED_NOW, MemoryKeyring
from keyring.backends import fail

from jobradar import ai, cli, config, db, mailer, notifications, pipeline
from jobradar.migrations import LATEST_SCHEMA_VERSION, MIGRATIONS, migrate

PASSWORD = "s3cret-Mot2passe"
EMAIL = {
    "enabled": True,
    "to": "moi@exemple.fr",
    "from": "",
    "smtp_host": "smtp.exemple.fr",
    "smtp_port": 587,
    "security": "starttls",
    "username": "compte@exemple.fr",
    "kinds": ["high_score_offer", "follow_up_due", "interview_reminder"],
}


class FakeSMTP:
    """Double de smtplib.SMTP : enregistre les appels ; ``fail_at`` fait échouer une étape."""

    instances: list["FakeSMTP"] = []
    fail_at: dict[str, BaseException] = {}

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[tuple] = []
        self.sent: list[tuple] = []
        FakeSMTP.instances.append(self)
        self._step("connect")

    def _step(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name in FakeSMTP.fail_at:
            raise FakeSMTP.fail_at[name]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.calls.append(("quit",))

    def ehlo(self):
        self._step("ehlo")

    def starttls(self, context=None):
        self._step("starttls", context)

    def login(self, user, password):
        self._step("login", user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self._step("send")
        self.sent.append((msg, from_addr, to_addrs))


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    FakeSMTP.fail_at = {}
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTPSSL)
    return FakeSMTP


@pytest.fixture
def configure(data_dir, isolated_mail):
    """Écrit la section « email » (valide par défaut) et range le mot de passe dans le trousseau simulé."""
    def write(password: str | None = PASSWORD, **overrides: object) -> None:
        config.save_config_section("email", EMAIL | overrides)
        if password:
            isolated_mail.set_password(mailer.KEYRING_SERVICE, mailer.KEYRING_ACCOUNT, password)

    return write


def _sent_messages() -> list:
    return [s[0] for inst in FakeSMTP.instances for s in inst.sent]


def _parts(msg) -> tuple[str, str]:
    return (msg.get_body(("plain",)).get_content(), msg.get_body(("html",)).get_content())


def _offer_alert(make_offer, search_id, **fields) -> dict:
    oid = make_offer(**{k: v for k, v in fields.items() if k in ("title", "company", "url")})
    analyzed = {"id": oid, "search_id": search_id, "title": fields.get("title", f"Stage {oid}"),
                "company": fields.get("company", "Acme"), "score": fields.get("score", 90),
                "verdict": "Très bon profil", "in_zone": True,
                "match_reasons": ["Pentest", "Île-de-France", "Bac+5", "Quatrième raison"]}
    return notifications.alert_high_score_offers([analyzed])[0]


def _email_rows() -> list[sqlite3.Row]:
    with db.connect() as c:
        return c.execute("SELECT * FROM notifications ORDER BY id").fetchall()


# ---------------------------------------------------------------- transport SMTP

def test_starttls_contexte_verifie_authentification_et_entetes(configure, smtp) -> None:
    configure()
    conf = mailer.email_config()
    msg = mailer.build_message(conf, "Objet", "texte", "<p>html</p>")
    mailer.send(msg, conf, PASSWORD)

    (server,) = smtp.instances
    assert type(server) is FakeSMTP
    assert (server.host, server.port, server.timeout) == ("smtp.exemple.fr", 587, 20)
    names = [c[0] for c in server.calls]
    assert names == ["connect", "ehlo", "starttls", "ehlo", "login", "send", "quit"]
    context = server.calls[2][1]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert server.calls[4] == ("login", "compte@exemple.fr", PASSWORD)
    sent, from_addr, to_addrs = server.sent[0]
    assert (from_addr, to_addrs) == ("compte@exemple.fr", ["moi@exemple.fr"])
    assert sent["From"] == "JobRadar <compte@exemple.fr>"
    assert sent["To"] == "moi@exemple.fr"
    assert sent["Subject"] == "Objet"
    assert sent["Date"]
    assert sent["Message-ID"].endswith("@jobradar.local>")
    assert sent["Auto-Submitted"] == "auto-generated"
    assert _parts(sent) == ("texte\n", "<p>html</p>\n")


def test_ssl_direct_sans_starttls_et_adresse_dexpedition(configure, smtp) -> None:
    configure(smtp_port=465, security="ssl", **{"from": "alertes@exemple.fr"})
    conf = mailer.email_config()
    mailer.send(mailer.build_message(conf, "o", "t"), conf, PASSWORD)
    (server,) = smtp.instances
    assert type(server) is FakeSMTPSSL
    assert isinstance(server.context, ssl.SSLContext) and server.context.verify_mode == ssl.CERT_REQUIRED
    assert "starttls" not in [c[0] for c in server.calls]
    assert server.sent[0][1] == "alertes@exemple.fr"


def test_pas_denvoi_en_clair_si_starttls_absent(configure, smtp) -> None:
    configure()
    smtp.fail_at = {"starttls": smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")}
    conf = mailer.email_config()
    with pytest.raises(mailer.SendError, match="aucun envoi en clair"):
        mailer.send(mailer.build_message(conf, "o", "t"), conf, PASSWORD)
    assert [c[0] for c in smtp.instances[0].calls] == ["connect", "ehlo", "starttls", "quit"]


@pytest.mark.parametrize(("error", "expected"), [
    (smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted"), "Authentification refusée"),
    (socket.gaierror(-2, "Name or service not known"), "Serveur SMTP introuvable : « smtp.exemple.fr »"),
    (TimeoutError("timed out"), "Délai dépassé (20 s)"),
    (ConnectionRefusedError(111, "Connection refused"), "Connexion refusée par smtp.exemple.fr:587"),
    (ssl.SSLCertVerificationError(1, "certificate verify failed"), "Certificat du serveur invalide"),
    (smtplib.SMTPRecipientsRefused({"moi@exemple.fr": (550, b"no")}), "Adresse de réception refusée"),
])
def test_erreurs_traduites(configure, smtp, error, expected) -> None:
    configure()
    smtp.fail_at = {"connect" if not isinstance(error, smtplib.SMTPAuthenticationError) else "login": error}
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        smtp.fail_at = {"send": error}
    conf = mailer.email_config()
    with pytest.raises(mailer.SendError) as info:
        mailer.send(mailer.build_message(conf, "o", "t"), conf, PASSWORD)
    assert expected in str(info.value)


def test_mot_de_passe_masque_meme_si_le_serveur_le_renvoie(configure, smtp) -> None:
    configure()
    smtp.fail_at = {"login": smtplib.SMTPAuthenticationError(535, f"bad credentials {PASSWORD}".encode())}
    conf = mailer.email_config()
    with pytest.raises(mailer.SendError) as info:
        mailer.send(mailer.build_message(conf, "o", "t"), conf, PASSWORD)
    assert PASSWORD not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


# ---------------------------------------------------------------- récapitulatif

def _sent_since(clock, days: int) -> dict:
    clock(FIXED_NOW - timedelta(days=days))
    app = db.create_application({"title": "Stage SOC", "company": "Beta", "status": "envoyee"})
    clock(FIXED_NOW)
    return app


def test_recapitulatif_regroupe_echappe_et_filtre_les_liens(configure, smtp, clock, make_offer, search_id) -> None:
    configure()
    _offer_alert(make_offer, search_id, title='<script>alert("x")</script> & Co', company="Evil <b>Corp</b>",
                 url="javascript:alert(1)", score=88)
    _offer_alert(make_offer, search_id, title="Stage pentest", company="Intrinsec", score=93,
                 url="https://exemple.test/offre?id=1&src=jr")
    _sent_since(clock, 11)
    app = db.create_application({"title": "Stage", "company": "Gamma"})
    db.create_interview(app["id"], {"starts_at": (FIXED_NOW + timedelta(hours=30)).isoformat(),
                                    "location": "Paris <15e>", "notes": "NOTE PRIVÉE"})
    notifications.refresh_notifications()

    assert notifications.deliver_email_digest() == {"sent": 4, "error": None}
    (msg,) = _sent_messages()
    assert msg["Subject"] == "JobRadar — 2 offres très pertinentes · 1 relance · 1 entretien demain"
    text, body = _parts(msg)

    # Sections dans l'ordre, offres triées par score, trois raisons au plus.
    assert text.index("OFFRES TRÈS PERTINENTES") < text.index("RELANCES À FAIRE") < text.index("ENTRETIENS À VENIR")
    assert text.index("Stage pentest") < text.index("<script>")
    assert "Quatrième raison" not in text and "Quatrième raison" not in body
    assert "Stage SOC — Beta · envoyée le 9 octobre 2026" in text
    assert "mercredi 21 octobre à 16:00 · Paris <15e>" in text
    assert "Ouvrir JobRadar : http://127.0.0.1:8765 (sur ton PC)" in text
    assert "décoche « Activer »" in text

    # HTML : texte échappé, uniquement des liens http(s), aucune image ni donnée privée.
    assert "<script>" not in body and "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; Co" in body
    assert "Evil &lt;b&gt;Corp&lt;/b&gt;" in body and "Paris &lt;15e&gt;" in body
    assert "javascript:" not in body and "javascript:" not in text
    assert 'href="https://exemple.test/offre?id=1&amp;src=jr"' in body
    hrefs = [h.split('"')[0] for h in body.split('href="')[1:]]
    assert hrefs and all(h.startswith(("https://", "http://")) for h in hrefs)
    assert "<img" not in body.lower() and "url(" not in body
    assert "NOTE PRIVÉE" not in text and "NOTE PRIVÉE" not in body
    for color in ("#0B1F3A", "#087CFA", "#00D99B"):
        assert color in body

    assert all(r["emailed_at_utc"] == "2026-10-20T08:00:00Z" for r in _email_rows())
    assert notifications.deliver_email_digest() == {"sent": 0, "error": None}
    assert len(smtp.instances) == 1
    assert mailer.read_status() == {"last_sent_at": "2026-10-20T08:00:00+00:00", "last_error": None}


def test_echec_retente_puis_abandon_apres_trois_essais(configure, smtp, make_offer, search_id) -> None:
    configure()
    _offer_alert(make_offer, search_id)
    smtp.fail_at = {"login": smtplib.SMTPAuthenticationError(535, b"no")}

    for attempt in (1, 2, 3):
        result = notifications.deliver_email_digest()
        assert result["sent"] == 0 and result["error"].startswith("Authentification refusée")
        (row,) = _email_rows()
        assert (row["email_attempts"], row["emailed_at_utc"]) == (attempt, None)
        assert row["email_error"].startswith("Authentification refusée")
    assert notifications.deliver_email_digest() == {"sent": 0, "error": None}
    assert len(smtp.instances) == 3
    assert mailer.read_status()["last_error"].startswith("Authentification refusée")


def test_reessai_reussi_apres_un_echec(configure, smtp, make_offer, search_id) -> None:
    configure()
    _offer_alert(make_offer, search_id)
    smtp.fail_at = {"connect": TimeoutError()}
    assert notifications.deliver_email_digest()["error"].startswith("Délai dépassé")
    smtp.fail_at = {}
    assert notifications.deliver_email_digest() == {"sent": 1, "error": None}
    (row,) = _email_rows()
    assert (row["email_attempts"], row["email_error"]) == (1, None)
    assert row["emailed_at_utc"]
    assert mailer.read_status()["last_error"] is None


def test_filtre_kinds_et_notifications_lues(configure, smtp, clock, make_offer, search_id) -> None:
    configure(kinds=["follow_up_due"])
    _offer_alert(make_offer, search_id)
    _sent_since(clock, 11)
    _sent_since(clock, 12)
    notifications.refresh_notifications()
    lue = next(n for n in notifications.list_notifications() if n["kind"] == "follow_up_due")
    notifications.dismiss(lue["id"])

    assert notifications.deliver_email_digest()["sent"] == 1
    (msg,) = _sent_messages()
    assert msg["Subject"] == "JobRadar — 1 relance"
    emailed = {r["kind"]: r["emailed_at_utc"] for r in _email_rows() if r["status"] == "pending"}
    assert emailed["high_score_offer"] is None and emailed["follow_up_due"]


def test_desactive_ou_sans_mot_de_passe(configure, smtp, make_offer, search_id) -> None:
    configure(enabled=False, password=None)
    _offer_alert(make_offer, search_id)
    assert notifications.deliver_email_digest() == {"sent": 0, "error": None}

    configure(password=None)  # activé, mais trousseau vide (timer sans accès au trousseau)
    result = notifications.deliver_email_digest()
    assert result["error"].startswith("Mot de passe SMTP introuvable")
    assert smtp.instances == []
    (row,) = _email_rows()
    assert row["email_attempts"] == 0  # un réglage incomplet ne consomme pas de tentative


def test_section_limitee_a_dix_elements(configure, smtp, make_offer, search_id) -> None:
    configure()
    for _ in range(12):
        _offer_alert(make_offer, search_id)
    assert notifications.deliver_email_digest()["sent"] == 12
    text, body = _parts(_sent_messages()[0])
    assert "… et 2 autres dans JobRadar." in body
    assert text.count("\n- ") == 10


def test_pipeline_envoie_un_recapitulatif_journalise(configure, smtp, make_offer, search_id, monkeypatch) -> None:
    configure()
    oid = make_offer(company="Intrinsec", title="Stage pentest")
    monkeypatch.setattr(ai, "score_offers", lambda _c, batch: [
        {"id": o["id"], "score": 93, "in_zone": True, "verdict": "v", "match_reasons": ["r"],
         "application_deadline": ""} for o in batch])
    pipeline.run_search(search_id, score_only=True)
    assert len(_sent_messages()) == 1
    assert "E-mail récapitulatif envoyé (1 alerte(s))" in "\n".join(pipeline.STATE["log"])
    assert [r["offer_id"] for r in _email_rows()] == [oid]


def test_pipeline_echec_denvoi_non_bloquant(configure, smtp, make_offer, search_id, monkeypatch) -> None:
    configure()
    make_offer()
    monkeypatch.setattr(ai, "score_offers", lambda _c, batch: [
        {"id": o["id"], "score": 93, "in_zone": True, "verdict": "v", "match_reasons": [],
         "application_deadline": ""} for o in batch])
    smtp.fail_at = {"connect": socket.gaierror(-2, "x")}
    stats = pipeline.run_search(search_id, score_only=True)
    assert stats["scored"] == 1
    log = "\n".join(pipeline.STATE["log"])
    assert "E-mail non envoyé : Serveur SMTP introuvable" in log and PASSWORD not in log


def test_cli_refresh_un_seul_email_pour_plusieurs_recherches(configure, smtp, make_offer, search_id,
                                                             monkeypatch, capsys) -> None:
    configure()
    second = db.save_search({"name": "Autre", "criteria": {}})
    runs = []

    def fake_run(sid, score_only=False, *, send_email=True):
        runs.append((sid, send_email))
        _offer_alert(make_offer, search_id)
        return {"new": 0}

    monkeypatch.setattr(pipeline, "run_search", fake_run)
    monkeypatch.setattr(sys, "argv", ["jobradar", "refresh"])
    cli.main()
    assert sorted(runs) == sorted([(search_id, False), (second, False)])
    assert len(_sent_messages()) == 1
    assert "E-mail récapitulatif envoyé (2 alerte(s))" in capsys.readouterr().out


# ---------------------------------------------------------------- secret

class LockedKeyring(MemoryKeyring):
    """Trousseau présent mais verrouillé (cas du timer systemd sans session ouverte)."""

    def get_password(self, service, username):
        raise keyring.errors.KeyringLocked("locked")

    set_password = delete_password = get_password


def test_ordre_env_puis_trousseau_puis_fichier(data_dir, isolated_mail, monkeypatch) -> None:
    assert mailer.password_source() == (None, None)
    (data_dir / "secrets.json").write_text(json.dumps({"smtp_password": "fichier"}), encoding="utf-8")
    assert mailer.password_source() == ("fichier", "file")
    isolated_mail.set_password(mailer.KEYRING_SERVICE, mailer.KEYRING_ACCOUNT, "trousseau")
    assert mailer.password_source() == ("trousseau", "keyring")
    monkeypatch.setenv(mailer.ENV_PASSWORD, "variable")
    assert mailer.password_source() == ("variable", "env")


def test_trousseau_disponible_et_ancien_repli_efface(data_dir, isolated_mail) -> None:
    secrets = data_dir / "secrets.json"
    secrets.write_text(json.dumps({"smtp_password": "ancien", "autre": 1}), encoding="utf-8")
    assert mailer.store_password("nouveau") == "keyring"
    assert isolated_mail.store == {(mailer.KEYRING_SERVICE, mailer.KEYRING_ACCOUNT): "nouveau"}
    assert json.loads(secrets.read_text(encoding="utf-8")) == {"autre": 1}
    mailer.clear_password()
    assert isolated_mail.store == {}
    assert mailer.password_source() == (None, None)


@pytest.mark.parametrize("backend", [fail.Keyring, LockedKeyring], ids=["absent", "verrouille"])
def test_repli_fichier_si_trousseau_indisponible(data_dir, backend) -> None:
    keyring.set_keyring(backend())
    assert mailer.keyring_available() is (backend is LockedKeyring)
    assert mailer.store_password("repli") == "file"
    secrets = data_dir / "secrets.json"
    assert json.loads(secrets.read_text(encoding="utf-8")) == {"smtp_password": "repli"}
    if os.name == "posix":
        assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
    assert mailer.password_source() == ("repli", "file")
    mailer.clear_password()
    assert not secrets.exists()


def test_trousseau_qui_ne_repond_pas(data_dir, isolated_mail, monkeypatch) -> None:
    import threading

    release = threading.Event()
    monkeypatch.setattr(mailer, "KEYRING_TIMEOUT_S", 0.05)
    monkeypatch.setattr(isolated_mail, "get_password", lambda *_a: release.wait(5))
    try:
        assert mailer.password_source() == (None, None)
    finally:
        release.set()


# ---------------------------------------------------------------- API des réglages

VIEW_KEYS = set(EMAIL) | {"password_set", "password_storage", "keyring_available", "last_error", "last_sent_at"}


def test_api_lecture_par_defaut(client) -> None:
    view = client.get("/api/settings/email").json()
    assert set(view) == VIEW_KEYS
    assert view["enabled"] is False and view["to"] == "" and view["username"] == ""
    assert (view["password_set"], view["password_storage"], view["keyring_available"]) == (False, None, True)
    assert (view["last_error"], view["last_sent_at"]) == (None, None)


@pytest.mark.parametrize(("body", "message"), [
    ({"to": "pas-une-adresse"}, "Adresse de réception invalide"),
    ({"to": "Moi <moi@exemple.fr>"}, "Adresse de réception invalide"),
    ({"to": "moi@exemple.fr\r\nBcc: x@y.fr"}, "caractères de contrôle"),
    ({"from": "a@b"}, "Adresse d'expédition invalide"),
    ({"smtp_port": 0}, "smtp_port : doit être supérieur ou égal à 1"),
    ({"smtp_port": 70000}, "smtp_port : doit être inférieur ou égal à 65535"),
    ({"smtp_port": "587"}, "smtp_port : nombre entier attendu"),
    ({"security": "tls"}, "security : valeur non autorisée"),
    ({"smtp_host": "https://smtp.gmail.com"}, "Serveur SMTP invalide"),
    ({"kinds": ["sms"]}, "kinds.0 : valeur non autorisée"),
    ({"enabled": "oui"}, "enabled : booléen attendu"),
    ({"debug": True}, "debug : champ inconnu"),
])
def test_api_validation(client, body, message) -> None:
    res = client.put("/api/settings/email", json=body)
    assert res.status_code == 400
    assert message in res.json()["detail"]
    assert config.load_config()["email"] == config.DEFAULT_CONFIG["email"]  # rien n'a été écrit


def test_api_activation_refusee_sans_mot_de_passe(client) -> None:
    res = client.put("/api/settings/email", json={"enabled": True, "to": "moi@exemple.fr"})
    assert res.status_code == 400
    assert res.json()["detail"] == ("Renseigne le serveur SMTP, l'identifiant et le mot de passe pour envoyer "
                                    "des alertes par e-mail.")
    res = client.put("/api/settings/email", json=EMAIL | {"username": "login-sans-arobase"})
    assert res.status_code == 400 and "le mot de passe" in res.json()["detail"]
    res = client.put("/api/settings/email", json=EMAIL | {"username": "login", "password": PASSWORD})
    assert res.json()["detail"].startswith("Indique l'adresse d'expédition")
    assert client.get("/api/settings/email").json()["password_set"] is False  # rien enregistré après un refus


def test_api_enregistrement_fusionne_sans_mot_de_passe(client, write_config, isolated_mail, capsys) -> None:
    write_config(timezone="Europe/Paris", francetravail={"client_id": "garde-moi", "client_secret": ""})
    res = client.put("/api/settings/email", json=EMAIL | {"password": PASSWORD, "smtp_host": " SMTP.Exemple.FR "})
    assert res.status_code == 200
    view = res.json()
    assert set(view) == VIEW_KEYS
    assert view["smtp_host"] == "smtp.exemple.fr" and view["enabled"] is True
    assert (view["password_set"], view["password_storage"]) == (True, "keyring")

    raw = config.CONFIG_PATH.read_text(encoding="utf-8")
    saved = json.loads(raw)
    assert saved["francetravail"]["client_id"] == "garde-moi" and saved["timezone"] == "Europe/Paris"
    assert saved["email"] == EMAIL
    assert PASSWORD not in raw
    assert PASSWORD not in client.get("/api/settings/email").text
    assert PASSWORD not in json.dumps(view)
    assert isolated_mail.store == {(mailer.KEYRING_SERVICE, mailer.KEYRING_ACCOUNT): PASSWORD}

    # Chaîne vide = inchangé ; mise à jour partielle.
    view = client.put("/api/settings/email", json={"password": "", "smtp_port": 465, "security": "ssl"}).json()
    assert view["password_set"] and view["smtp_port"] == 465 and view["to"] == "moi@exemple.fr"
    assert isolated_mail.store[(mailer.KEYRING_SERVICE, mailer.KEYRING_ACCOUNT)] == PASSWORD

    # Suppression impossible tant que les alertes sont actives…
    assert client.put("/api/settings/email", json={"password": None}).status_code == 400
    # … puis possible (null explicite ou clear_password) une fois désactivées.
    view = client.put("/api/settings/email", json={"enabled": False, "password": None}).json()
    assert view["password_set"] is False and isolated_mail.store == {}
    client.put("/api/settings/email", json={"password": "autre"})
    assert client.put("/api/settings/email", json={"clear_password": True}).json()["password_set"] is False
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err


def test_api_repli_fichier_signale(client, data_dir) -> None:
    keyring.set_keyring(fail.Keyring())
    view = client.put("/api/settings/email", json=EMAIL | {"password": PASSWORD}).json()
    assert (view["password_storage"], view["keyring_available"]) == ("file", False)
    assert PASSWORD not in config.CONFIG_PATH.read_text(encoding="utf-8")
    assert PASSWORD in (data_dir / "secrets.json").read_text(encoding="utf-8")


def test_api_variable_denvironnement(client, monkeypatch) -> None:
    monkeypatch.setenv(mailer.ENV_PASSWORD, PASSWORD)
    view = client.put("/api/settings/email", json=EMAIL).json()
    assert (view["enabled"], view["password_storage"]) == (True, "env")
    # Effacer le mot de passe enregistré n'empêche pas l'activation : la variable reste disponible.
    assert client.put("/api/settings/email", json={"clear_password": True}).status_code == 200


def test_api_email_de_test(client, configure, smtp) -> None:
    configure(enabled=False)
    assert client.post("/api/settings/email/test").json() == {"ok": True}
    (msg,) = _sent_messages()
    assert msg["Subject"] == "JobRadar — e-mail de test"
    assert msg["To"] == "moi@exemple.fr"
    assert client.get("/api/settings/email").json()["last_sent_at"] == "2026-10-20T08:00:00+00:00"

    res = client.post("/api/settings/email/test")
    assert res.status_code == 429 and "Patiente encore" in res.json()["detail"]
    assert len(smtp.instances) == 1


def test_api_email_de_test_erreur_traduite(client, configure, smtp, monkeypatch) -> None:
    configure()
    smtp.fail_at = {"login": smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")}
    res = client.post("/api/settings/email/test")
    assert res.status_code == 502
    assert res.json()["detail"].startswith("Authentification refusée : identifiant ou mot de passe incorrect.")
    assert "535 5.7.8 Username and Password not accepted" in res.json()["detail"]
    assert client.get("/api/settings/email").json()["last_error"] == res.json()["detail"]
    assert PASSWORD not in res.text


def test_api_email_de_test_reglages_incomplets(client, smtp) -> None:
    res = client.post("/api/settings/email/test")
    assert res.status_code == 400 and res.json()["detail"].startswith("Renseigne l'adresse de réception")
    assert smtp.instances == []


def test_config_email_invalide_desactive(write_config, capsys) -> None:
    write_config(email=EMAIL | {"smtp_port": "abc"})
    assert mailer.email_config()["enabled"] is False
    assert "alertes par e-mail désactivées" in capsys.readouterr().err


# ---------------------------------------------------------------- migration 9

def test_migration_9_colonnes_email(tmp_path) -> None:
    assert LATEST_SCHEMA_VERSION >= 9
    with sqlite3.connect(tmp_path / "v8.db") as conn:
        conn.execute("BEGIN")
        for _version, step in MIGRATIONS[:8]:
            step(conn)
        conn.execute("PRAGMA user_version = 8")
        conn.execute("""INSERT INTO notifications(kind, dedupe_key, payload_json, created_at_utc)
                        VALUES ('follow_up_due', 'k', '{}', '2026-10-01T00:00:00Z')""")
        conn.commit()

        migrate(conn)
        migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(notifications)")}
        assert {"emailed_at_utc", "email_attempts", "email_error"} <= set(columns)
        assert columns["email_attempts"][3] == 1  # NOT NULL
        row = conn.execute("SELECT emailed_at_utc, email_attempts, email_error FROM notifications").fetchone()
        assert row == (None, 0, None)
