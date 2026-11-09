"""Mémoire de parcours par site : un champ déjà associé à une clé du profil n'est plus redemandé à l'IA.

Après un remplissage vérifié, chaque champ résolu par le superviseur IA (``ia-superviseur:<clé>``) est mémorisé pour le
domaine du formulaire sous la forme « libellé replié -> clé de profil ». Aucune valeur n'est enregistrée ; le fichier
vit sous ``data/profil`` et est donc chiffré au repos quand le coffre est actif. À la visite suivante, les champs en
attente dont le libellé est connu sont remplis depuis le profil avant tout appel à l'IA (plus rapide, plus stable).
"""
from __future__ import annotations

import json
import threading
from urllib.parse import urlsplit

from . import config, private_io, profile
from .application_adapters import _fold

_LOCK = threading.Lock()
MAX_FIELDS_PER_SITE = 200


def _path():
    return config.DATA_DIR / "profil" / "parcours.json"


def domain(url: str) -> str:
    return (urlsplit(url or "").hostname or "").removeprefix("www.")


def _load() -> dict:
    try:
        data = json.loads(private_io.read_text(_path()))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def lookup(url: str, label: str) -> str | None:
    return _load().get(domain(url), {}).get(_fold(label))


def remember(url: str, resolved: dict[str, str]) -> int:
    """``resolved`` : libellé -> clé de profil, pour des champs remplis puis relus avec succès."""
    site = domain(url)
    if not site or not resolved:
        return 0
    with _LOCK:
        data = _load()
        fields = data.setdefault(site, {})
        before = dict(fields)
        fields.update({_fold(label): key for label, key in resolved.items() if _fold(label) and key})
        if len(fields) > MAX_FIELDS_PER_SITE:
            data[site] = dict(list(fields.items())[-MAX_FIELDS_PER_SITE:])
        if data.get(site) == before:
            return 0
        profile.atomic_write(_path(), json.dumps(data, ensure_ascii=False, indent=1).encode())
        return len(resolved)


def apply(url: str, plan: dict, values: dict[str, str]) -> tuple[dict, list[str]]:
    """Remplit depuis le profil les champs en attente déjà connus pour ce site ; renvoie (plan, libellés remplis)."""
    known = _load().get(domain(url), {})
    if not known:
        return plan, []
    accepted, labels = [], []
    remaining = []
    for item in plan["interventions"]:
        key = known.get(_fold(item.get("label")))
        if key and key in values and item.get("kind") != "sensitive" and item.get("input_kind") not in {
                "select", "radio", "listbox", "file"}:
            accepted.append({"selector": item["selector"], "value": values[key], "kind": item.get("input_kind") or "text",
                             "confidence": 1.0, "source": f"parcours:{key}", "label": item["label"]})
            labels.append(item["label"])
        else:
            remaining.append(item)
    if not accepted:
        return plan, []
    return {**plan, "values": [*plan["values"], *accepted], "interventions": remaining,
            "ready": not remaining}, labels
