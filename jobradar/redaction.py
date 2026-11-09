"""Assainissement des pages enregistrées : aucune donnée du candidat ni secret de session ne doit survivre.

Les DOM conservés servent au corpus de rejeu du robot (tests/fixtures/apply) : ils gardent la structure
(balises, libellés, options, attributs d'accessibilité) et perdent tout ce qui a été saisi ou qui
identifie la session (valeurs, champs cachés, jetons, scripts, paramètres d'URL).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup, Comment

MASK = "▒"
# Attributs qui portent une saisie, un état coché ou un identifiant de session.
VALUE_ATTRIBUTES = {"value", "checked", "selected", "data-value", "data-initial-value", "defaultvalue"}
URL_ATTRIBUTES = {"href", "src", "action", "formaction", "data-href", "data-url", "poster"}
SECRET_NAME = re.compile(r"csrf|xsrf|token|nonce|session|auth|captcha|recaptcha|secret|signature", re.I)
SECRET_ATTRIBUTE = re.compile(r"^(?:data-)?(?:csrf|xsrf|token|nonce|sitekey|session)", re.I)
DROPPED_TAGS = ("script", "noscript", "template", "object", "embed", "canvas", "svg", "video", "audio")
MIN_PRIVATE_LENGTH = 3


def strip_url(url: str) -> str:
    """Garde schéma, hôte et chemin ; supprime requête et fragment (jetons, identifiants de suivi)."""
    if not url or url.startswith(("data:", "javascript:", "mailto:", "tel:")):
        return "" if url.startswith(("data:", "javascript:")) else url.split(":", 1)[0] + ":"
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _private_pattern(private_values: Iterable[str]) -> re.Pattern | None:
    variants = set()
    for value in private_values:
        text = " ".join(str(value or "").split())
        if len(text) < MIN_PRIVATE_LENGTH:
            continue
        variants.add(text)
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 6:  # téléphone sous une autre présentation : 06 01 02… / +33 6 01…
            variants.add(r"\D{0,3}".join(digits[-9:]))
    if not variants:
        return None
    escaped = sorted((re.escape(v) if "\\D" not in v else v for v in variants), key=len, reverse=True)
    return re.compile("|".join(escaped), re.I)


def redact_text(text: str, private_values: Iterable[str]) -> str:
    pattern = _private_pattern(private_values)
    return pattern.sub(MASK, text) if pattern else text


def sanitize_html(html: str, private_values: Iterable[str] = ()) -> str:
    """DOM rejouable sans valeur saisie, champ caché, script, jeton ni donnée privée connue."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(DROPPED_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda node: isinstance(node, Comment)):
        comment.extract()
    for tag in soup.find_all("input"):
        kind = (tag.get("type") or "").lower()
        if kind == "hidden" or SECRET_NAME.search(f"{tag.get('name', '')} {tag.get('id', '')}"):
            tag.decompose()
    for tag in soup.find_all("meta"):
        if SECRET_NAME.search(f"{tag.get('name', '')} {tag.get('property', '')}"):
            tag.decompose()
    for tag in soup.find_all("textarea"):
        tag.clear()
    for tag in soup.find_all(True):
        for attribute in list(tag.attrs):
            name = attribute.lower()
            if name in VALUE_ATTRIBUTES or SECRET_ATTRIBUTE.match(name) or name.startswith("on"):
                del tag.attrs[attribute]
            elif name in URL_ATTRIBUTES and isinstance(tag.attrs[attribute], str):
                tag.attrs[attribute] = strip_url(tag.attrs[attribute])
            elif name == "srcset":
                del tag.attrs[attribute]
    pattern = _private_pattern(private_values)
    if pattern:
        for node in soup.find_all(string=True):
            if pattern.search(node):
                node.replace_with(pattern.sub(MASK, str(node)))
        for tag in soup.find_all(True):
            for attribute, value in list(tag.attrs.items()):
                if isinstance(value, str) and pattern.search(value):
                    tag.attrs[attribute] = pattern.sub(MASK, value)
    return str(soup)
