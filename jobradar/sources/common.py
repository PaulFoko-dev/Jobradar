import random
import re
import time
import unicodedata
from functools import lru_cache

from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

CONTRACT_WORDS = re.compile(
    r"\b(stage|stagiaire|internship|intern|alternance|alternant|apprentissage|cdi|cdd|freelance|"
    r"professionnalisation|vie)\b",
    re.I,
)


def polite_pause(base: float = 1.5):
    """Pause aléatoire entre deux requêtes vers un même site."""
    time.sleep(base + random.random() * base)


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def strip_contract_words(query: str) -> str:
    """'stage pentest' -> 'pentest' (pour les sites qui filtrent le contrat à part)."""
    return re.sub(r"\s+", " ", CONTRACT_WORDS.sub("", query)).strip() or query


def norm(s: str | None) -> str:
    """Minuscules, sans accents ni ponctuation : « Ingénieur·e Cybersécurité (H/F) » -> « ingenieur e cybersecurite h f »."""
    s = unicodedata.normalize("NFKD", (s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9+#]+", " ", s).strip()


@lru_cache(maxsize=4096)
def _kw_pattern(keyword: str) -> re.Pattern | None:
    k = norm(keyword)
    if not k:
        return None
    # Début de mot obligatoire ; fin de mot aussi pour les sigles courts (« soc » ne doit pas trouver « associate »),
    # mais pas pour les mots longs (« pentest » trouve « pentester »).
    end = r"(?![a-z0-9])" if len(k) <= 4 else ""
    return re.compile(r"(?<![a-z0-9])" + re.escape(k) + end)


def matched_keywords(text: str, keywords: list[str]) -> set[str]:
    t = norm(text)
    return {k for k in keywords if k and (p := _kw_pattern(k)) and p.search(t)}


def matches_any(text: str, keywords: list[str]) -> bool:
    t = norm(text)
    return any((p := _kw_pattern(k)) and p.search(t) for k in keywords if k)
