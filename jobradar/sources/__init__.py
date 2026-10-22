"""Collecteurs d'offres. Chaque source expose `collect(criteria, cfg, known_urls, log) -> list[dict]`.

Une offre normalisée contient : title, company, location, contract, date_posted (YYYY-MM-DD),
description (texte brut), url, source.
"""
from . import ats, francetravail, hellowork, jobspy_src, wttj

SOURCES = {
    "indeed": lambda *a: jobspy_src.collect("indeed", *a),
    "linkedin": lambda *a: jobspy_src.collect("linkedin", *a),
    "hellowork": hellowork.collect,
    "welcometothejungle": wttj.collect,
    "francetravail": francetravail.collect,
    "ats": ats.collect,
}
