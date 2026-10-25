"""Tests des règles métier de candidature, sans horloge ni service externe."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from jobradar.workflow import (
    DeadlineState,
    days_left,
    deadline_status,
    follow_up_status,
    is_urgent,
    normalize_company,
    priority,
    validate_http_url,
    validate_timezone,
)

PARIS = ZoneInfo("Europe/Paris")


def test_normalisation_entreprise_conserve_la_forme_juridique() -> None:
    assert normalize_company("  ACME\u00a0ＳＡＳ  France  ") == "acme sas france"
    assert normalize_company("Straße GmbH") == "strasse gmbh"


def test_deadline_date_seule_reste_valide_toute_la_journee() -> None:
    now = datetime(2026, 10, 22, 23, 59, tzinfo=PARIS)
    decision = deadline_status(date(2026, 10, 22), now=now)
    assert decision.state is DeadlineState.AUJOURDHUI
    assert decision.due_at == datetime(2026, 10, 22, 23, 59, 59, 999999, tzinfo=PARIS)


def test_deadline_instant_expire_a_la_seconde_precise() -> None:
    now = datetime(2026, 10, 22, 12, 0, 1, tzinfo=PARIS)
    decision = deadline_status(datetime(2026, 10, 22, 12, 0, tzinfo=PARIS), now=now)
    assert decision.state is DeadlineState.EXPIREE


def test_deadline_refuse_un_instant_sans_fuseau() -> None:
    with pytest.raises(ValueError, match="fuseau horaire"):
        deadline_status(datetime(2026, 10, 22), now=datetime(2026, 10, 20, tzinfo=PARIS))


def test_priorite_suit_les_criteres_dans_un_ordre_stable() -> None:
    now = datetime(2026, 10, 20, 9, tzinfo=PARIS)
    commun = dict(
        excluded=False,
        deadline=date(2026, 10, 25),
        favorite=False,
        score=80,
        published_at=date(2026, 10, 19),
        now=now,
    )
    favorite = priority(item_id=2, **(commun | {"favorite": True}))
    standard = priority(item_id=1, **commun)
    exclue = priority(item_id=3, **(commun | {"excluded": True, "favorite": True, "score": 100}))

    assert favorite.key < standard.key < exclue.key
    assert "Entreprise favorite." in favorite.reasons
    assert exclue.reasons[0] == "Offre exclue."


def _decision(item_id: int, deadline: date | None, score: int | None, published_at: date | None = None,
              favorite: bool = False, **extra: object):
    return priority(
        item_id=item_id,
        excluded=False,
        deadline=deadline,
        favorite=favorite,
        score=score,
        published_at=published_at,
        now=datetime(2026, 10, 20, 9, tzinfo=PARIS),
        **extra,
    )


def test_priorite_deadlines_urgentes_triees_par_echeance_puis_score_fraicheur_identifiant() -> None:
    urgente = _decision(9, date(2026, 10, 21), 10, date(2026, 10, 1))
    mieux_notee = _decision(8, date(2026, 10, 25), 90, date(2026, 10, 1))
    plus_fraiche = _decision(7, date(2026, 10, 25), 90, date(2026, 10, 19))
    departage_id = _decision(6, date(2026, 10, 25), 90, date(2026, 10, 19))

    assert urgente.key < mieux_notee.key
    assert plus_fraiche.key < mieux_notee.key
    assert departage_id.key < plus_fraiche.key


def test_priorite_une_deadline_lointaine_ne_donne_aucun_avantage() -> None:
    lointaine = _decision(1, date(2026, 12, 1), 60)
    sans_deadline = _decision(2, None, 90)
    meme_score_sans_deadline = _decision(3, None, 60)

    assert sans_deadline.key < lointaine.key
    # À score égal, la date lointaine est ignorée : seul l'identifiant départage.
    assert lointaine.key < meme_score_sans_deadline.key
    assert "Pas de date limite urgente." in lointaine.reasons


def test_priorite_deadline_urgente_passe_avant_favorite_et_meilleur_score() -> None:
    urgente = _decision(5, date(2026, 10, 27), 40)
    favorite = _decision(1, None, 99, favorite=True)
    assert urgente.key < favorite.key
    assert "Date limite urgente." in urgente.reasons


def test_priorite_hors_urgence_favorite_puis_score() -> None:
    favorite = _decision(5, date(2026, 11, 30), 50, favorite=True)
    mieux_notee = _decision(1, None, 95)
    assert favorite.key < mieux_notee.key


def test_priorite_seuil_durgence_parametrable() -> None:
    a_sept_jours = _decision(1, date(2026, 10, 27), 10)
    a_huit_jours = _decision(2, date(2026, 10, 28), 10)
    sans_deadline = _decision(3, None, 90)
    assert a_sept_jours.key < sans_deadline.key
    assert sans_deadline.key < a_huit_jours.key

    elargi = _decision(2, date(2026, 10, 28), 10, urgent_days=10)
    assert elargi.key < sans_deadline.key
    restreint = _decision(1, date(2026, 10, 27), 10, urgent_days=0)
    assert sans_deadline.key < restreint.key


def test_priorite_aujourdhui_est_toujours_urgente() -> None:
    aujourdhui = _decision(1, date(2026, 10, 20), 0, urgent_days=0)
    sans_deadline = _decision(2, None, 100)
    assert aujourdhui.key < sans_deadline.key


def test_priorite_refuse_un_seuil_negatif() -> None:
    with pytest.raises(ValueError, match="délai d'urgence"):
        _decision(1, None, 10, urgent_days=-1)


def test_priorite_expiree_et_exclue_en_fin_de_liste() -> None:
    now = datetime(2026, 10, 20, 9, tzinfo=PARIS)
    base = dict(favorite=False, published_at=None, now=now)
    faible = priority(item_id=1, excluded=False, deadline=None, score=5, **base)
    expiree = priority(item_id=2, excluded=False, deadline=date(2026, 10, 19), score=100, **base)
    exclue = priority(item_id=3, excluded=True, deadline=date(2026, 10, 21), score=100, **base)
    assert faible.key < expiree.key < exclue.key


def test_jours_restants_et_urgence() -> None:
    now = datetime(2026, 10, 20, 9, tzinfo=PARIS)
    decision = deadline_status(date(2026, 10, 27), now=now)
    assert days_left(decision, now=now) == 7
    assert is_urgent(decision, now=now)
    assert not is_urgent(decision, now=now, urgent_days=6)
    expiree = deadline_status(date(2026, 10, 18), now=now)
    assert days_left(expiree, now=now) == -2
    assert not is_urgent(expiree, now=now)
    absente = deadline_status(None, now=now)
    assert days_left(absente, now=now) is None


def test_offre_expiree_passe_apres_une_offre_sans_deadline() -> None:
    now = datetime(2026, 10, 20, 9, tzinfo=PARIS)
    base = dict(excluded=False, favorite=False, score=50, published_at=None, now=now)
    sans_deadline = priority(item_id=1, deadline=None, **base)
    expiree = priority(item_id=2, deadline=date(2026, 10, 19), **base)
    assert sans_deadline.key < expiree.key


def test_relance_compte_dix_jours_calendaires_malgre_le_changement_dheure() -> None:
    sent_at = datetime(2026, 3, 20, 9, 30, tzinfo=PARIS)
    now = datetime(2026, 3, 30, 9, 30, tzinfo=PARIS)
    decision = follow_up_status(sent_at=sent_at, status="envoyee", now=now)

    assert decision.due_at == datetime(2026, 3, 30, 9, 30, tzinfo=PARIS)
    assert decision.is_due
    assert decision.due_at.utcoffset() != sent_at.utcoffset()


@pytest.mark.parametrize("status", ["consultee", "relance", "entretien_rh", "retenu", "refuse", "abandon"])
def test_statuts_non_eligibles_ne_declenchent_pas_de_relance(status: str) -> None:
    decision = follow_up_status(
        sent_at=datetime(2026, 10, 1, 10, tzinfo=PARIS),
        status=status,
        now=datetime(2026, 10, 22, 10, tzinfo=PARIS),
    )
    assert decision.due_at is None
    assert not decision.is_due


@pytest.mark.parametrize("name", ["Europe/Paris", "UTC", "America/Montreal"])
def test_validation_fuseau_iana(name: str) -> None:
    assert validate_timezone(name).key == name


def test_validation_fuseau_inconnu() -> None:
    with pytest.raises(ValueError, match="inconnu"):
        validate_timezone("Europe/Inconnue")


@pytest.mark.parametrize(
    "url",
    ["https://example.test/offre?id=1", "http://localhost:8765/path", "https://[::1]:8443/offre"],
)
def test_validation_url_http(url: str) -> None:
    assert validate_http_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.test/offre",
        "https://",
        "https://user:secret@example.test",
        " https://example.test",
        "https://example.test/ligne\nnouvelle",
        "https://example.test:99999",
    ],
)
def test_validation_url_refuse_les_valeurs_dangereuses(url: str) -> None:
    with pytest.raises(ValueError):
        validate_http_url(url)
