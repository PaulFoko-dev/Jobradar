"""Collecteurs d'offres. Chaque source expose `collect(criteria, cfg, known_urls, log) -> list[dict]`.

Une offre normalisée contient : title, company, location, contract, date_posted (YYYY-MM-DD),
description (texte brut), url, source ; et facultativement published_at, l'instant de publication brut
de la source (retenu seulement s'il porte un fuseau horaire : aucun fuseau n'est jamais deviné).
"""
from . import arbeitnow, ats, francetravail, groups, hellowork, jobspy_src, labonnealternance, remoteok, wttj

SOURCES = {
    "indeed": lambda *a: jobspy_src.collect("indeed", *a),
    "linkedin": lambda *a: jobspy_src.collect("linkedin", *a),
    "hellowork": hellowork.collect,
    "welcometothejungle": wttj.collect,
    "francetravail": francetravail.collect,
    "labonnealternance": labonnealternance.collect,
    "arbeitnow": arbeitnow.collect,
    "remoteok": remoteok.collect,
    "ats": ats.collect,
    "grands_groupes": groups.collect,
}
