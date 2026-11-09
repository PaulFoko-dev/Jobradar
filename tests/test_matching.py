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


def test_index_rapproche_deux_noms_d_une_meme_entreprise() -> None:
    index = DedupIndex()
    index.add(offre(title="Stage Ingénieur - cybersécurité applicative (H/F)", company="HGH Systèmes Infrarouges",
                    location="Igny, A8, France"), "hgh", oid=32)

    assert index.find(offre(title="Stage Ingénieur - cybersécurité applicative (H/F)",
                            company="HGH Infrared Systems", location="Igny, Île-de-France, France")) == ("hgh", 32)
    # Même racine mais autre poste, ou autre ville : offres distinctes.
    assert index.find(offre(title="Stage Ingénieur - sécurité réseau (H/F)", company="HGH Infrared Systems",
                            location="Igny")) is None
    assert index.find(offre(title="Stage Ingénieur - cybersécurité applicative (H/F)",
                            company="HGH Infrared Systems", location="Lyon")) is None


def test_un_premier_mot_generique_ne_rapproche_pas_deux_entreprises() -> None:
    index = DedupIndex()
    index.add(offre(title="Stage analyste SOC", company="Crédit Agricole", location="Paris"), "ca", oid=1)
    assert index.find(offre(title="Stage analyste SOC", company="Crédit Mutuel", location="Paris")) is None


def test_index_retrouve_l_offre_d_origine_republiee_par_un_site_relais() -> None:
    index = DedupIndex()
    index.add(offre(title="Stage - Audits techniques et tests d'intrusion", company="Wavestone"), "wave", oid=9)

    relais = offre(title="Stage - Audits techniques et tests d'intrusion (H/F)", company="Groupe Talents Handicap")
    assert index.find(relais) == ("wave", 9)
    # Sans site relais, deux entreprises différentes avec un intitulé court restent distinctes.
    index.add(offre(title="Stage analyste SOC junior", company="Entreprise A"), "a", oid=1)
    assert index.find(offre(title="Stage analyste SOC junior", company="Entreprise B")) is None



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


def test_offers_leading_to_the_same_application_page_are_one_offer() -> None:
    from jobradar.matching import target_key

    index = DedupIndex()
    index.add(offre(title="Stagiaire Pentester / Security Researcher", company="Patrowl",
                    url="https://www.welcometothejungle.com/fr/companies/patrowl/jobs/stagiaire-pentester_paris?utm=x"),
              "patrowl", oid=63)
    renamed = offre(title="Stage sécurité offensive", company="Patrowl SAS",
                    url="https://example-board.fr/o/1", links=[{"source": "candidature directe", "apply": True,
                    "url": "https://welcometothejungle.com/fr/companies/patrowl/jobs/stagiaire-pentester_paris/"}])
    assert index.find(renamed) == ("patrowl", 63)
    # Identifiant dans les paramètres : deux offres Indeed distinctes ne sont jamais confondues.
    assert target_key("https://fr.indeed.com/viewjob?jk=1&from=x") != target_key("https://fr.indeed.com/viewjob?jk=2")
    assert target_key("https://careers.example.com/job/Apply?jobId=7&utm_source=a") == \
        target_key("https://careers.example.com/job/Apply/?utm_source=b&jobId=7")
