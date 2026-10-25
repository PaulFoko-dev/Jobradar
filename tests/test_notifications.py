"""Tests des notifications : alertes d'offres, rappels, déduplication et API."""
import subprocess
from datetime import timedelta

import pytest
from conftest import FIXED_NOW

from jobradar import ai, db, notifications, pipeline

REAL_NOTIFY = notifications.notify


@pytest.fixture
def desktop(monkeypatch):
    """Capture les notifications de bureau au lieu de les afficher."""
    messages: list[str] = []

    def capture(message: str, title: str = "JobRadar") -> bool:
        messages.append(message)
        return True

    monkeypatch.setattr(notifications, "notify", capture)
    return messages


def _analyzed(oid: int, search_id: int, **fields: object) -> dict:
    return {"id": oid, "search_id": search_id, "title": f"Stage {oid}", "company": "Acme", "score": 90,
            "verdict": "Très bon", "in_zone": True, "match_reasons": ["a", "b", "c", "d"]} | fields


def _kinds() -> list[tuple[str, str]]:
    return [(n["kind"], n["status"]) for n in notifications.list_notifications()]


# ---------------------------------------------------------------- alertes offres

def test_seuils_des_alertes(make_offer, search_id) -> None:
    ids = [make_offer(company=c) for c in ("Acme", "Acme", "Acme", "Exclue", "Acme")]
    db.set_company_rule("Exclue", "excluded")
    created = notifications.alert_high_score_offers([
        _analyzed(ids[0], search_id, score=85),
        _analyzed(ids[1], search_id, score=84),
        _analyzed(ids[2], search_id, in_zone=False),
        _analyzed(ids[3], search_id, company="Exclue"),
        _analyzed(ids[4], search_id, in_zone=None),
    ])
    assert [n["payload"]["offer_id"] for n in created] == [ids[0], ids[4]]
    assert created[0]["kind"] == "high_score_offer"
    assert created[0]["payload"] == {"title": f"Stage {ids[0]}", "company": "Acme", "score": 85,
                                     "verdict": "Très bon", "reasons": ["a", "b", "c"], "search_id": search_id,
                                     "offer_id": ids[0]}


def test_alerte_dedupliquee_par_offre(make_offer, search_id) -> None:
    oid = make_offer()
    assert len(notifications.alert_high_score_offers([_analyzed(oid, search_id)])) == 1
    assert notifications.alert_high_score_offers([_analyzed(oid, search_id, score=99)]) == []
    assert len(notifications.list_notifications()) == 1


def test_seuil_configurable(make_offer, search_id, write_config) -> None:
    write_config(alerts={"min_score": 95})
    assert notifications.alert_high_score_offers([_analyzed(make_offer(), search_id, score=94)]) == []


def test_suppression_de_loffre_supprime_son_alerte(make_offer, search_id) -> None:
    oid = make_offer()
    notifications.alert_high_score_offers([_analyzed(oid, search_id)])
    db.delete_offer(oid)
    assert notifications.list_notifications() == []


def test_texte_de_bureau_agrege() -> None:
    created = [{"payload": {"company": c, "title": "t", "score": s}}
               for c, s in (("Beta", 88), ("Intrinsec", 93), (None, 90), ("Delta", 86))]
    assert notifications.offers_desktop_text(created) == "4 offres très pertinentes : Intrinsec (93), t (90), Beta (88)…"
    assert notifications.offers_desktop_text(created[:1]) == "1 offre très pertinente : Beta (88)"


def test_pipeline_une_seule_notification_de_bureau(make_offer, search_id, monkeypatch, desktop) -> None:
    fortes = [make_offer(company="Intrinsec", title="Stage pentest"), make_offer(company="Beta", title="Stage SOC")]
    faible = make_offer(company="Gamma", title="Stage GRC")
    scores = {fortes[0]: 93, fortes[1]: 88, faible: 50}

    def faux_scoring(_crit: dict, batch: list[dict]) -> list[dict]:
        return [{"id": o["id"], "score": scores[o["id"]], "in_zone": True, "verdict": "v",
                 "match_reasons": ["r"], "application_deadline": ""} for o in batch]

    monkeypatch.setattr(ai, "score_offers", faux_scoring)
    pipeline.run_search(search_id, score_only=True)

    assert desktop == ["2 offres très pertinentes : Intrinsec (93), Beta (88)"]
    notes = notifications.list_notifications()
    assert sorted(n["offer_id"] for n in notes) == sorted(fortes)
    assert all(n["delivered_at"] for n in notes)

    pipeline.run_search(search_id, score_only=True)  # rien de nouveau à analyser
    assert len(desktop) == 1


def test_bureau_desactive(make_offer, search_id, write_config, desktop) -> None:
    write_config(alerts={"desktop": False})
    created = notifications.alert_high_score_offers([_analyzed(make_offer(), search_id)])
    assert notifications.deliver_desktop(created, "x") is False
    assert desktop == []


def test_intervention_de_candidature_est_dedupliquee_et_actionnable(data_dir, desktop) -> None:
    application = db.create_application({"title": "Stage SOC", "company": "Acme"})
    document = db.create_document(application["id"], "cv", 1, {"kind": "cv"}, {},
                                  {"readability": 100, "ready": True}, [])
    document = db.set_document_status(document["id"], "pret")
    attempt = db.create_application_attempt(
        application["id"], mode="assisted", status="queued", cv_document_id=document["id"],
        letter_document_id=None, target_url="https://example.test", provider="generic",
    )
    intervention = {"kind": "missing_fields", "message": "Une réponse est requise.", "fields": []}

    first = notifications.application_intervention(attempt, intervention)
    second = notifications.application_intervention(attempt, intervention)

    assert first is not None and second is None
    assert first["kind"] == "application_intervention"
    assert first["payload"]["application_id"] == application["id"]


def test_confirmation_de_candidature_est_dedupliquee(data_dir, desktop) -> None:
    application = db.create_application({"title": "Stage SOC", "company": "Acme"})
    document = db.create_document(application["id"], "cv", 1, {"kind": "cv"}, {},
                                  {"readability": 100, "ready": True}, [])
    document = db.set_document_status(document["id"], "pret")
    attempt = db.create_application_attempt(
        application["id"], mode="assisted", status="queued", cv_document_id=document["id"],
        letter_document_id=None, target_url="https://example.test", provider="generic",
    )
    first = notifications.application_submitted(attempt)
    second = notifications.application_submitted(attempt)
    assert first is not None and second is None
    assert first["kind"] == "application_submitted"


# ---------------------------------------------------------------- rappels

def _sent(clock, days_ago: int) -> dict:
    clock(FIXED_NOW - timedelta(days=days_ago))
    app = db.create_application({"title": "Stage SOC", "company": "Acme", "status": "envoyee"})
    clock(FIXED_NOW)
    return app


def test_rappel_de_relance_idempotent_puis_retire(clock) -> None:
    app = _sent(clock, 11)
    _sent(clock, 3)
    created = notifications.refresh_notifications()
    assert [(n["kind"], n["payload"]["application_id"]) for n in created] == [("follow_up_due", app["id"])]
    assert notifications.refresh_notifications() == []
    with db.connect() as c:
        key = c.execute("SELECT dedupe_key FROM notifications").fetchone()[0]
    assert key == f"followup:{app['id']}:2026-10-19"

    db.mark_follow_up_sent(app["id"])
    notifications.refresh_notifications()
    assert _kinds() == [("follow_up_due", "dismissed")]


def test_rappel_de_relance_reporte_recree_a_la_nouvelle_date(clock) -> None:
    app = _sent(clock, 11)
    notifications.refresh_notifications()
    db.snooze_follow_up(app["id"], 2)
    assert notifications.refresh_notifications() == []
    clock(FIXED_NOW + timedelta(days=2))
    created = notifications.refresh_notifications()
    assert len(created) == 1
    with db.connect() as c:
        keys = sorted(r[0] for r in c.execute("SELECT dedupe_key FROM notifications"))
    assert keys == [f"followup:{app['id']}:2026-10-19", f"followup:{app['id']}:2026-10-22"]


def test_rappel_dentretien_dans_les_36_heures(clock) -> None:
    app = db.create_application({"title": "Stage SOC", "company": "Acme"})
    proche = db.create_interview(app["id"], {"starts_at": (FIXED_NOW + timedelta(hours=35)).isoformat()})
    db.create_interview(app["id"], {"starts_at": (FIXED_NOW + timedelta(hours=37)).isoformat()})
    db.create_interview(app["id"], {"starts_at": (FIXED_NOW - timedelta(hours=1)).isoformat()})

    created = notifications.refresh_notifications()
    assert [n["payload"]["interview_id"] for n in created] == [proche["id"]]
    assert created[0]["payload"]["starts_at"] == "2026-10-21T19:00:00Z"
    assert notifications.refresh_notifications() == []

    db.cancel_interview(proche["id"])
    notifications.refresh_notifications()
    assert _kinds() == [("interview_reminder", "dismissed")]


def test_rappels_de_bureau_une_seule_fois(clock, desktop) -> None:
    _sent(clock, 11)
    _sent(clock, 12)
    app = db.create_application({"title": "Stage", "company": "Acme"})
    db.create_interview(app["id"], {"starts_at": (FIXED_NOW + timedelta(hours=2)).isoformat()})
    notifications.refresh_notifications()

    assert notifications.deliver_pending_reminders() is True
    assert desktop == ["Rappels : 2 relances à faire, 1 entretien dans les 36 h"]
    assert notifications.deliver_pending_reminders() is False
    assert len(desktop) == 1


# ---------------------------------------------------------------- API

def test_api_notifications(client, clock, make_offer, search_id) -> None:
    oid = make_offer()
    notifications.alert_high_score_offers([_analyzed(oid, search_id)])
    _sent(clock, 11)

    listed = client.get("/api/notifications").json()  # crée aussi le rappel de relance
    assert [n["kind"] for n in listed] == ["follow_up_due", "high_score_offer"]
    assert set(listed[0]) == {"id", "kind", "status", "offer_id", "payload", "created_at", "delivered_at"}

    dismissed = client.post(f"/api/notifications/{listed[1]['id']}/dismiss").json()
    assert dismissed["status"] == "dismissed"
    assert [n["status"] for n in client.get("/api/notifications").json()] == ["pending", "dismissed"]
    assert client.post("/api/notifications/999/dismiss").status_code == 404

    assert client.post("/api/notifications/dismiss-all").json() == {"dismissed": 1}
    assert {n["status"] for n in client.get("/api/notifications").json()} == {"dismissed"}


def test_api_notifications_limitee_a_50_non_lues_dabord(client, make_offer, search_id) -> None:
    analyzed = [_analyzed(make_offer(), search_id) for _ in range(55)]
    notifications.alert_high_score_offers(analyzed)
    with db.connect() as c:
        c.execute("UPDATE notifications SET status='dismissed' WHERE offer_id=?", (analyzed[-1]["id"],))
    listed = client.get("/api/notifications").json()
    assert len(listed) == 50
    assert all(n["status"] == "pending" for n in listed)


# ---------------------------------------------------------------- notification de bureau

def test_notify_passe_le_texte_en_argument_sans_bloquer(monkeypatch) -> None:
    lancements = []

    class FauxProcessus:
        def __init__(self, cmd, **kwargs):
            lancements.append((cmd, kwargs))

    monkeypatch.setattr(notifications.subprocess, "Popen", FauxProcessus)
    monkeypatch.setattr(notifications.sys, "platform", "darwin")
    message = 'Offre "spéciale" & do shell script "rm"'
    assert REAL_NOTIFY(message) is True
    cmd, kwargs = lancements[0]
    assert cmd[-1] == message
    assert all(message not in part for part in cmd[:-1])
    assert kwargs["stdout"] is subprocess.DEVNULL


def test_notify_sans_outil_ou_plateforme_inconnue(monkeypatch) -> None:
    def absent(*_a, **_k):
        raise FileNotFoundError("notify-send")

    monkeypatch.setattr(notifications.subprocess, "Popen", absent)
    monkeypatch.setattr(notifications.sys, "platform", "linux")
    assert REAL_NOTIFY("x") is False
    monkeypatch.setattr(notifications.sys, "platform", "win32")
    assert REAL_NOTIFY("x") is False
