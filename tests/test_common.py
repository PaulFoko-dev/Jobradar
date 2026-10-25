"""Tests des utilitaires partagés par les collecteurs."""

from jobradar.sources.common import html_to_text, matched_keywords, matches_any, norm, strip_contract_words


def test_norm_normalise_accents_et_ponctuation() -> None:
    assert norm("Ingénieur·e Cybersécurité (H/F)") == "ingenieur e cybersecurite h f"
    assert norm(None) == ""


def test_mots_cles_courts_respectent_les_limites_de_mot() -> None:
    assert matches_any("Analyste SOC junior", ["SOC"])
    assert not matches_any("Associate consultant", ["SOC"])


def test_mots_cles_longs_acceptent_les_variantes_suffixees() -> None:
    assert matched_keywords("Pentester une application", ["pentest", "réseau"]) == {"pentest"}


def test_retrait_du_type_de_contrat_conserve_une_requete_utile() -> None:
    assert strip_contract_words("stage pentest") == "pentest"
    assert strip_contract_words("stage") == "stage"


def test_conversion_html_supprime_les_balises_et_espaces_superflus() -> None:
    assert html_to_text("<p>Première ligne</p><p>Deuxième ligne</p>") == "Première ligne\nDeuxième ligne"
    assert html_to_text(None) == ""

