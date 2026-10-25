"""Tests du filtrage et de la détection de doublons."""

from datetime import date, timedelta

import pytest

from jobradar.matching import (
    UNKNOWN_CONTRACT,
    DedupIndex,
    company_key,
    dedup_key,
    priority,
    relevance,
    scoring_order,
    similar,
    title_tokens,
)


@pytest.fixture
def criteres_stage() -> dict:
    return {
        "titles": ["stage cybersécurité"],
        "include_keywords": ["cybersécurité", "pentest", "SOC"],
        "exclude_keywords": ["senior"],
        "contract_types": ["stage"],
        "posted_within_days": 30,
    }


def offre(**changements: object) -> dict:
    base = {
        "title": "Stage analyste cybersécurité",
        "company": "Exemple SAS France",
        "description": "Convention de stage en cybersécurité au sein du SOC. " * 8,
        "contract": "stage",
        "date_posted": date.today().isoformat(),
    }
    return base | changements


def test_relevance_accepte_une_offre_pertinente(criteres_stage: dict) -> None:
    assert relevance(offre(), criteres_stage) is None


@pytest.mark.parametrize(
    ("changements", "raison"),
    [
        ({"title": "Consultant cybersécurité senior"}, "mot exclu dans l'intitulé"),
        ({"title": "Stage marketing", "description": "Convention de stage en communication. " * 8}, "hors domaine"),
        (
            {"date_posted": (date.today() - timedelta(days=46)).isoformat()},
            "trop ancienne",
        ),
    ],
)
def test_relevance_explique_les_exclusions(criteres_stage: dict, changements: dict, raison: str) -> None:
    assert relevance(offre(**changements), criteres_stage) == raison


def test_relevance_signale_un_contrat_indeterminable(criteres_stage: dict) -> None:
    candidat = offre(title="Analyste cybersécurité", contract=None, description="SOC et pentest")
    assert relevance(candidat, criteres_stage) == UNKNOWN_CONTRACT


def test_priority_favorise_un_stage_explicitement_cible(criteres_stage: dict) -> None:
    cible = offre(title="Stage pentest cybersécurité")
    vague = offre(title="Consultant", description="cybersécurité pentest SOC")
    assert priority(cible, criteres_stage) < priority(vague, criteres_stage)


def test_normalisation_des_cles_de_dedoublonnage() -> None:
    assert company_key("Exemple SAS France") == "exemple"
    assert title_tokens("Stage Ingénieur(e) Réseau Sécurité H/F") == frozenset(
        {"ingenieur", "reseau", "securite"}
    )
    assert dedup_key(offre()).startswith("exemple|")


def test_similar_exige_assez_de_mots_pour_un_rapprochement_approximatif() -> None:
    assert similar(frozenset({"cyber"}), frozenset({"cyber"}))
    assert not similar(frozenset({"cyber", "soc"}), frozenset({"cyber", "audit"}))
    assert similar(
        frozenset({"ingenieur", "reseau", "securite", "junior"}),
        frozenset({"ingenieur", "reseau", "securite", "junior", "dsi"}),
    )


def test_index_retrouve_un_doublon_de_la_meme_entreprise() -> None:
    index = DedupIndex()
    initiale = offre(title="Stage Ingénieur Réseau Sécurité", company="Exemple SAS")
    index.add(initiale, "cle-source", oid=42)

    doublon = offre(title="Stage Ingénieur(e) Réseau Sécurité H/F", company="Exemple France")
    assert index.find(doublon) == ("cle-source", 42)


def test_index_retrouve_un_intitule_long_identique_sur_un_autre_site() -> None:
    index = DedupIndex()
    initiale = offre(title="Stage analyste sécurité systèmes réseaux", company="Entreprise A")
    index.add(initiale, "cle-originale", oid=7)

    republication = offre(title="Stage analyste sécurité systèmes réseaux", company="Jobboard B")
    assert index.find(republication) == ("cle-originale", 7)



def test_les_offres_les_plus_recentes_sont_analysees_en_premier(criteres_stage: dict) -> None:
    pertinente_ancienne = {"title": "Stage analyste SOC", "description": "", "date_posted": "2026-09-01"}
    vague_recente = {"title": "Chargé de mission", "description": "", "date_posted": "2026-10-20"}
    sans_date = {"title": "Stage SOC", "description": "", "first_seen": "2026-10-10T08:00:00+00:00"}
    ordre = sorted([pertinente_ancienne, sans_date, vague_recente], key=lambda o: scoring_order(o, criteres_stage))
    assert ordre == [vague_recente, sans_date, pertinente_ancienne]


def test_la_pertinence_departage_les_offres_du_meme_jour(criteres_stage: dict) -> None:
    cible = {"title": "Stage analyste SOC", "description": "", "date_posted": "2026-10-20"}
    vague = {"title": "Chargé de mission", "description": "", "date_posted": "2026-10-20"}
    assert scoring_order(cible, criteres_stage) < scoring_order(vague, criteres_stage)
