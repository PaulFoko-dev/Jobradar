"""Capture locale et sûre des pages d'offres d'emploi."""

from __future__ import annotations

import base64
import html
import logging
import os
import re
import sys
import tempfile
import threading
import time
import unicodedata
import zlib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from jobradar.workflow import DEFAULT_TIMEZONE, validate_http_url

_LOGGER = logging.getLogger(__name__)

CaptureLevel = Literal["page", "page_navigateur_dedie", "reconstituee", "indisponible"]
_EngineName = Literal["chromium-playwright", "chrome", "msedge"]

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {"User-Agent": _USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7"}
_VIEWPORT = {"width": 1_280, "height": 900}
# Deux pixels d'image par pixel CSS : un corps de 16 px occupe 32 px dans le PNG et reste lisible au zoom.
_DEVICE_SCALE_FACTOR = 2
# Hauteur CSS maximale d'un PNG d'un seul tenant (32 000 px d'image à DSF 2). Au-delà, Chromium cesse de dessiner le
# texte sans erreur (mesuré : texte absent sous ~51 500 px d'image) ; la page n'est alors gardée qu'en PDF.
_MAX_HEIGHT_PX = 16_000
_MAX_WIDTH_PX = 2_000
# Défilement pour le chargement paresseux : le PDF couvre la page entière, même au-delà de la limite du PNG.
_SCROLL_STEP_PX = 850
_MAX_SCROLL_STEPS = 60
# Part minimale des mots visibles de la page retrouvés dans le texte du PDF.
_MIN_PDF_COVERAGE = 0.9
_MIN_TEXT_CHARS = 200
# Au-delà de cette longueur, un mot comme « captcha » vient d'un pied de page (reCAPTCHA), pas d'un mur.
_WALL_MAX_TEXT_CHARS = 2_500
_MAX_ONLINE_BYTES = 2_000_000
# Statuts souvent renvoyés par un mur de connexion ou un contrôle anti-robot plutôt que par une offre absente.
_BLOCKING_STATUSES = frozenset({401, 403, 429, 503})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_WALL_URL_MARKERS = ("authwall", "/checkpoint/", "/cdn-cgi/challenge")
# Motifs cherchés dans le texte d'une page courte ; « captcha » seul y est trop fréquent (mention reCAPTCHA).
_WALL_PATTERNS = (
    "just a moment",
    "checking your browser",
    "cloudflare ray id",
    "verify you are human",
    "verifiez que vous etes humain",
    "confirmez que vous etes humain",
    "complete the captcha",
    "resoudre le captcha",
    "resolvez le captcha",
    "access denied",
    "acces refuse",
    "sign in to view",
    "connectez-vous pour voir",
)
_WALL_TITLE_PATTERNS = (*_WALL_PATTERNS, "captcha", "un instant", "attention required")
_EXPIRED_PATTERNS = (
    "offre n'est plus disponible",
    "cette offre a expire",
    "cette annonce a expire",
    "annonce n'est plus disponible",
    "offre n'est plus en ligne",
    "this job is no longer available",
    "this position is no longer available",
    "no longer accepting applications",
    "job has expired",
)
_REJECT_SELECTORS = (
    "#onetrust-reject-all-handler",
    "#didomi-notice-disagree-button",
    "#axeptio_btn_dismiss",
    "#CybotCookiebotDialogBodyButtonDecline",
    ".cc-deny",
    '[data-testid*="reject" i]',
    'button[id*="reject" i]',
)
_REJECT_NAMES = re.compile(
    r"^\s*(tout refuser|refuser tout|refuser|je refuse|continuer sans accepter|reject all|reject|decline all|decline"
    r"|only necessary|necessary only|use necessary cookies only|accept only necessary|refuse all)\b",
    re.IGNORECASE,
)
_CONSENT_CONTAINERS = (
    '[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i], '
    '[id*="didomi" i], [id*="axeptio" i], [aria-label*="cookie" i]'
)
_CLOSE_NAMES = re.compile(
    r"^\s*(fermer|close|dismiss|ok|j'ai compris|compris|got it|plus tard|not now|non merci|no thanks|×|✕|x)\s*$",
    re.IGNORECASE,
)
_DIALOGS = '[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open]'
_EXPAND_NAMES = re.compile(
    r"^\s*(voir plus|lire la suite|afficher plus|afficher la suite|show more|read more|see more)\s*$", re.IGNORECASE
)
# Masque les surcouches fixes restantes (fenêtres d'inscription, fonds de modale, bandeaux de consentement)
# sans toucher aux éléments qui portent l'essentiel du texte, puis rétablit le défilement.
_HIDE_OVERLAYS_JS = """() => {
    const area = innerWidth * innerHeight;
    const total = (document.body?.innerText || '').length || 1;
    const consent = /cookie|consent|didomi|axeptio|onetrust|gdpr|rgpd|credential_picker/i;
    let hidden = 0;
    for (const element of document.querySelectorAll('body *')) {
        if (getComputedStyle(element).position !== 'fixed') continue;
        const box = element.getBoundingClientRect();
        const marker = `${element.id} ${element.className} ${element.getAttribute('aria-label') || ''}`;
        const large = box.width * box.height >= 0.25 * area;
        if ((large || consent.test(marker)) && (element.innerText || '').length < 0.5 * total) {
            element.style.setProperty('display', 'none', 'important');
            hidden += 1;
        }
    }
    if (hidden) {
        for (const root of [document.documentElement, document.body]) {
            root?.style.setProperty('overflow', 'visible', 'important');
        }
    }
    return hidden;
}"""
# À l'impression, un élément fixe se répète sur chaque page et masque le texte : il est ancré en haut de page.
_UNFIX_FOR_PRINT_JS = """() => {
    for (const element of document.querySelectorAll('body *')) {
        if (getComputedStyle(element).position === 'fixed') {
            element.style.setProperty('position', 'absolute', 'important');
        }
    }
}"""

_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class CaptureResult:
    level: CaptureLevel
    pdf_path: Path | None
    png_path: Path | None
    error: str | None
    captured_at_utc: str


@dataclass(frozen=True)
class _Engine:
    name: _EngineName
    channel: str | None


_ENGINES = (_Engine("chromium-playwright", None), _Engine("chrome", "chrome"), _Engine("msedge", "msedge"))
_ENGINE_LABELS = {
    "chromium-playwright": "Chromium fourni par Playwright est utilisable.",
    "chrome": "Google Chrome est utilisable par Playwright.",
    "msedge": "Microsoft Edge est utilisable par Playwright.",
}


class _CaptureFailure(RuntimeError):
    def __init__(self, message: str, *, blocked: bool = False) -> None:
        super().__init__(message)
        self.blocked = blocked


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized.casefold()).split())


def _contains_any(normalized_text: str, patterns: tuple[str, ...]) -> bool:
    return any(_normalize_text(pattern) in normalized_text for pattern in patterns)


def _looks_like_wall(url: str, title: str, text: str) -> bool:
    """Mur de connexion ou anti-robot : URL caractéristique, titre, ou page courte qui en parle."""
    if any(marker in url.casefold() for marker in _WALL_URL_MARKERS):
        return True
    if _contains_any(_normalize_text(title), _WALL_TITLE_PATTERNS):
        return True
    return len(text) <= _WALL_MAX_TEXT_CHARS and _contains_any(_normalize_text(text), _WALL_PATTERNS)


def _remaining_ms(deadline: float, *, ceiling_ms: int | None = None) -> int:
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining < 1:
        raise _CaptureFailure("Le délai global de capture est dépassé.")
    return min(remaining, ceiling_ms) if ceiling_ms is not None else remaining


def _close_quietly(target: Browser | BrowserContext | None) -> None:
    if target is not None:
        try:
            target.close()
        except Exception:
            pass


def _launch_first_engine(playwright: Playwright, *, timeout_ms: int) -> tuple[Browser, _Engine] | None:
    """Lance le premier moteur disponible en headless (Chromium Playwright, puis Chrome, puis Edge)."""
    for engine in _ENGINES:
        options: dict[str, Any] = {"headless": True, "timeout": timeout_ms}
        if engine.channel is not None:
            options["channel"] = engine.channel
        try:
            return playwright.chromium.launch(**options), engine
        except Exception:
            continue
    return None


def browser_status() -> dict:
    """Décrit le premier moteur Chromium réellement lançable."""
    try:
        with sync_playwright() as playwright:
            launched = _launch_first_engine(playwright, timeout_ms=15_000)
            if launched is not None:
                _close_quietly(launched[0])
    except Exception:
        launched = None
    if launched is None:
        return {
            "available": False,
            "engine": None,
            "detail": "Aucun Chromium Playwright, Chrome ou Edge utilisable n'a été trouvé.",
        }
    engine = launched[1]
    return {"available": True, "engine": engine.name, "detail": _ENGINE_LABELS[engine.name]}


def _context_options() -> dict[str, Any]:
    return {
        "accept_downloads": False,
        "permissions": [],
        "service_workers": "block",
        "user_agent": _USER_AGENT,
        "viewport": _VIEWPORT,
        "device_scale_factor": _DEVICE_SCALE_FACTOR,
    }


def _guard_context(context: BrowserContext, page: Page) -> None:
    """Retire toute permission et ferme toute fenêtre ouverte par la page : seule `page` reste active."""
    context.clear_permissions()

    def close_extra(extra: Page) -> None:
        if extra is not page:
            try:
                extra.close()
            except Exception:
                pass

    context.on("page", close_extra)
    for existing in context.pages:
        close_extra(existing)


def _offline_page(browser: Browser, deadline: float) -> Page:
    """Page sans JavaScript ni accès réseau, pour imprimer un contenu produit par JobRadar."""
    context = browser.new_context(**(_context_options() | {"java_script_enabled": False}))
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    _guard_context(context, page)
    page.set_default_timeout(_remaining_ms(deadline, ceiling_ms=15_000))
    return page


def _click_first_visible(locators: list[Any], deadline: float) -> bool:
    for locator in locators:
        try:
            for index in range(min(locator.count(), 4)):
                button = locator.nth(index)
                if button.is_visible():
                    button.click(timeout=_remaining_ms(deadline, ceiling_ms=1_500))
                    return True
        except _CaptureFailure:
            raise
        except Exception:
            continue
    return False


def _dismiss_cookie_banner(page: Page, deadline: float) -> None:
    """Refuse les cookies quand un bouton le permet, sinon ferme le bandeau de consentement."""
    reject: list[Any] = []
    close: list[Any] = []
    for frame in page.frames[:10]:
        consent = frame.locator(_CONSENT_CONTAINERS)
        reject.extend(frame.locator(selector) for selector in _REJECT_SELECTORS)
        reject.append(frame.get_by_role("button", name=_REJECT_NAMES))
        # Un lien peut quitter la page : il n'est cliqué qu'à l'intérieur d'un bandeau de consentement.
        reject.append(consent.get_by_role("link", name=_REJECT_NAMES))
        close.append(consent.get_by_role("button", name=_CLOSE_NAMES))
        close.append(consent.locator('[aria-label*="fermer" i], [aria-label*="close" i]'))
    if _click_first_visible(reject, deadline) or _click_first_visible(close, deadline):
        page.wait_for_timeout(min(500, _remaining_ms(deadline)))


def _dismiss_overlays(page: Page, deadline: float) -> None:
    """Ferme les fenêtres modales (inscription, alerte…) puis masque les surcouches fixes restantes."""
    dialogs = page.locator(_DIALOGS)
    closers = [
        dialogs.locator('[aria-label*="fermer" i], [aria-label*="close" i]'),
        dialogs.get_by_role("button", name=_CLOSE_NAMES),
    ]
    if _click_first_visible(closers, deadline):
        page.wait_for_timeout(min(300, _remaining_ms(deadline)))
    try:
        page.keyboard.press("Escape")
        page.evaluate(_HIDE_OVERLAYS_JS)
    except Exception:
        pass


# Sections repliées en <details> (« L'entreprise », « Avantages »…) ; les menus d'en-tête et de pied restent fermés.
_OPEN_DETAILS_JS = """() => {
    let opened = 0;
    for (const details of document.querySelectorAll('details:not([open])')) {
        if (details.closest('header, nav, footer, [role="navigation"], [role="banner"], [role="menu"]')) continue;
        details.open = true;
        opened += 1;
    }
    return opened;
}"""


def _expand_collapsed(page: Page, deadline: float) -> None:
    """Déplie les descriptions tronquées (« Voir plus ») ; seuls des boutons sont cliqués, jamais des liens."""
    try:
        page.evaluate(_OPEN_DETAILS_JS)
    except Exception:
        pass
    buttons = page.get_by_role("button", name=_EXPAND_NAMES)
    try:
        for index in range(min(buttons.count(), 5)):
            button = buttons.nth(index)
            if button.is_visible():
                button.click(timeout=_remaining_ms(deadline, ceiling_ms=1_500))
    except _CaptureFailure:
        raise
    except Exception:
        return


def _document_size(page: Page) -> tuple[int, int]:
    dimensions = page.evaluate(
        """() => ({
            width: Math.max(document.documentElement?.scrollWidth || 0, document.body?.scrollWidth || 0, innerWidth),
            height: Math.max(document.documentElement?.scrollHeight || 0, document.body?.scrollHeight || 0, innerHeight)
        })"""
    )
    return int(dimensions["width"]), int(dimensions["height"])


def _scroll_page(page: Page, deadline: float) -> None:
    """Fait défiler jusqu'en bas par paliers pour déclencher le chargement paresseux, puis revient en haut."""
    position = 0
    previous_height = -1
    for _ in range(_MAX_SCROLL_STEPS):
        height = _document_size(page)[1]
        if position >= height and height == previous_height:
            break
        position = min(position + _SCROLL_STEP_PX, height)
        page.evaluate("position => window.scrollTo(0, position)", position)
        page.wait_for_timeout(min(200, _remaining_ms(deadline)))
        previous_height = height
    page.evaluate("() => window.scrollTo(0, 0)")


def _page_state(page: Page) -> tuple[str, str, str]:
    """URL, titre et texte visibles ; vides si la page est en cours de navigation."""
    try:
        return page.url, page.title(), page.locator("body").inner_text(timeout=3_000)
    except Exception:
        return page.url, "", ""


def _wait_past_wall(page: Page, deadline: float, *, max_wait_ms: int) -> None:
    """Laisse le temps à un contrôle anti-robot transitoire (Cloudflare…) de se résoudre."""
    limit = time.monotonic() + max_wait_ms / 1000
    while _looks_like_wall(*_page_state(page)) and time.monotonic() < limit:
        page.wait_for_timeout(min(1_000, _remaining_ms(deadline)))


def _validate_page(page: Page) -> None:
    url, title, text = _page_state(page)
    if _looks_like_wall(url, title, text):
        raise _CaptureFailure("La page d'origine présente un mur de connexion ou un contrôle anti-robot.", blocked=True)
    if len("".join(text.split())) < _MIN_TEXT_CHARS:
        raise _CaptureFailure("La page d'origine est vide ou contient trop peu de texte.")


def _png_dimensions(png: bytes) -> tuple[int, int] | None:
    if not png.startswith(_PNG_SIGNATURE) or len(png) < 24 or png[12:16] != b"IHDR":
        return None
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


def _screenshot(page: Page) -> bytes | None:
    """PNG net de toute la hauteur du document, ou None s'il ne peut pas être complet (jamais d'image partielle)."""
    scale = float(page.evaluate("() => devicePixelRatio"))
    for _ in range(2):
        width, height = _document_size(page)
        if height > _MAX_HEIGHT_PX:
            return None
        width = max(1, min(width, _MAX_WIDTH_PX))
        # full_page est indispensable : sans lui, le découpage est limité à la fenêtre visible.
        png = page.screenshot(
            type="png", full_page=True, animations="disabled", clip={"x": 0, "y": 0, "width": width, "height": height}
        )
        if _png_dimensions(png) != (round(width * scale), round(height * scale)):
            return None
        # Un document qui grandit pendant la capture (contenu tardif) serait tronqué : nouvel essai, puis abandon.
        if _document_size(page)[1] <= height:
            return png
    return None


def _screenshot_segments(page: Page) -> list[bytes]:
    """Page trop haute pour un seul PNG fiable : tranches successives, chacune sous la limite mesurée."""
    width, height = _document_size(page)
    width = max(1, min(width, _MAX_WIDTH_PX))
    return [
        page.screenshot(
            type="png",
            full_page=True,
            animations="disabled",
            clip={"x": 0, "y": top, "width": width, "height": min(_MAX_HEIGHT_PX, height - top)},
        )
        for top in range(0, height, _MAX_HEIGHT_PX)
    ]


# Texte visible du document, y compris dans les composants web (shadow DOM), que innerText ignore. Les fenêtres de
# consentement en sont exclues : elles ne font pas partie de l'offre et sont masquées avant l'impression.
_VISIBLE_TEXT_JS = """() => {
    const skipped = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'svg']);
    const consent = /cookie|consent|didomi|axeptio|onetrust|gdpr|rgpd|credential_picker/i;
    const parts = [];
    const visit = node => {
        if (node.nodeType === Node.TEXT_NODE) {
            const parent = node.parentElement;
            if (parent && node.textContent.trim() && parent.checkVisibility({visibilityProperty: true})) {
                parts.push(node.textContent);
            }
            return;
        }
        if (node.nodeType === Node.ELEMENT_NODE) {
            if (skipped.has(node.tagName)) return;
            if (consent.test(`${node.tagName} ${node.id} ${node.getAttribute('class') || ''}`)) return;
            if (node.tagName === 'SLOT') {
                const assigned = node.assignedNodes({flatten: true});
                (assigned.length ? assigned : [...node.childNodes]).forEach(visit);
                return;
            }
            if (node.shadowRoot) {
                node.shadowRoot.childNodes.forEach(visit);
                return;
            }
        }
        node.childNodes.forEach(visit);
    };
    if (document.body) visit(document.body);
    return parts.join(' ');
}"""

_PDF_OBJECT = re.compile(rb"(\d+)\s+\d+\s+obj\b(.*?)\bendobj", re.DOTALL)
_PDF_TOKEN = re.compile(
    rb"<[0-9A-Fa-f\s]*>|\((?:\\.|[^\\()])*\)|/[^\s/<>\[\]()]+|\[|\]|[-+]?(?:\d+\.?\d*|\.\d+)|[A-Za-z'\"*]+"
)
_PDF_FONT_NAMES = re.compile(rb"/Font\s*<<(.*?)>>", re.DOTALL)
_PDF_REFERENCE = re.compile(rb"/([^\s/<>\[\]()]+)\s+(\d+)\s+\d+\s+R")

# Table ToUnicode d'une police : code de glyphe -> texte, et largeur des codes en octets.
_PdfCmap = tuple[dict[int, str], int]


def _pdf_utf16(hex_digits: bytes) -> str:
    raw = bytes.fromhex(hex_digits.decode("ascii"))
    return raw.decode("utf-16-be", errors="ignore")


def _pdf_cmap(data: bytes) -> _PdfCmap:
    """Table ToUnicode (code de glyphe -> texte) et largeur des codes en octets."""
    codespace = re.search(rb"begincodespacerange\s*<([0-9A-Fa-f]+)>", data)
    width = max(1, len(codespace.group(1)) // 2) if codespace else 2
    mapping: dict[int, str] = {}
    for block in re.findall(rb"beginbfchar(.*?)endbfchar", data, re.DOTALL):
        for source, target in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]*)>", block):
            mapping[int(source, 16)] = _pdf_utf16(target)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", data, re.DOTALL):
        ranges = re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<[0-9A-Fa-f]*>|\[[^\]]*\])", block)
        for low_hex, high_hex, target in ranges:
            low, high = int(low_hex, 16), min(int(high_hex, 16), int(low_hex, 16) + 0xFFFF)
            if target.startswith(b"["):
                for offset, item in enumerate(re.findall(rb"<([0-9A-Fa-f]*)>", target)):
                    mapping[low + offset] = _pdf_utf16(item)
            elif len(target) > 2:
                start = bytes.fromhex(target[1:-1].decode("ascii"))
                first = int.from_bytes(start, "big")
                for code in range(low, high + 1):
                    value = (first + code - low).to_bytes(len(start), "big")
                    mapping[code] = value.decode("utf-16-be", errors="ignore")
    return mapping, width


def _pdf_string(token: bytes, cmap: _PdfCmap | None) -> str:
    if token.startswith(b"<"):
        digits = re.sub(rb"\s+", b"", token[1:-1])
        raw = bytes.fromhex((digits + b"0" * (len(digits) % 2)).decode("ascii"))
    else:
        raw = re.sub(rb"\\([()\\])", rb"\1", token[1:-1])
    if cmap is None:
        return raw.decode("latin-1")
    mapping, width = cmap
    return "".join(mapping.get(int.from_bytes(raw[i : i + width], "big"), "") for i in range(0, len(raw), width))


# Opérateurs qui commencent une nouvelle ligne de texte (Td/TD seulement avec un déplacement vertical).
_PDF_NEW_LINE = frozenset({b"Tm", b"T*", b"'", b'"', b"ET"})


def _pdf_stream_lines(data: bytes, font_for: Callable[[bytes], _PdfCmap | None]) -> list[str]:
    """Lignes de texte d'un flux de contenu : Tf choisit la police, Tj/TJ/'/" affichent du texte."""
    lines: list[str] = []
    line: list[str] = []
    operands: list[bytes] = []
    font: _PdfCmap | None = None
    for token in _PDF_TOKEN.findall(data):
        if not (token[:1].isalpha() or token in (b"'", b'"')):
            operands.append(token)
            continue
        if token in _PDF_NEW_LINE or (token in (b"Td", b"TD") and operands and float(operands[-1]) != 0):
            if line:
                lines.append("".join(line))
            line = []
        if token == b"Tf" and len(operands) >= 2:
            font = font_for(operands[-2][1:])
        elif token in (b"Tj", b"'", b'"') and operands:
            line.append(_pdf_string(operands[-1], font))
        elif token == b"TJ":
            for operand in operands:
                if operand[:1] in (b"<", b"("):
                    line.append(_pdf_string(operand, font))
                elif operand not in (b"[", b"]") and float(operand) < -200:
                    line.append(" ")
        operands = []
    if line:
        lines.append("".join(line))
    return lines


def _pdf_text(pdf: bytes) -> str:
    """Texte d'un PDF imprimé par Chromium (polices à table ToUnicode), sans dépendance externe."""
    heads: dict[int, bytes] = {}
    raw_streams: dict[int, bytes] = {}
    for match in _PDF_OBJECT.finditer(pdf):
        number, body = int(match.group(1)), match.group(2)
        start = body.find(b"stream")
        heads[number] = body if start < 0 else body[:start]
        if start >= 0:
            data = body[start + 6 :]
            data = data[2:] if data.startswith(b"\r\n") else data[1:] if data.startswith(b"\n") else data
            raw_streams[number] = data[: data.rfind(b"endstream")]

    def stream(number: int) -> bytes | None:
        """Flux décompressé à la demande : les images et fichiers de police ne sont jamais décodés."""
        data = raw_streams.get(number)
        if data is not None and b"/FlateDecode" in heads[number]:
            try:
                return zlib.decompressobj().decompress(data)
            except zlib.error:
                return None
        return data

    def resolve(head: bytes, key: bytes) -> list[int]:
        found = re.search(rb"/" + key + rb"\s*(\[[^\]]*\]|\d+\s+\d+\s+R)", head)
        return [int(ref) for ref in re.findall(rb"(\d+)\s+\d+\s+R", found.group(1))] if found else []

    cmaps: dict[int, _PdfCmap] = {}
    for number, head in heads.items():
        refs = resolve(head, b"ToUnicode")
        data = stream(refs[0]) if b"/Font" in head and refs else None
        if data is not None:
            cmaps[number] = _pdf_cmap(data)

    def font_names(head: bytes) -> dict[bytes, int]:
        resources = resolve(head, b"Resources")
        if resources:
            head = heads.get(resources[0], b"")
        names: dict[bytes, int] = {}
        for block in _PDF_FONT_NAMES.findall(head):
            names.update((name, int(ref)) for name, ref in _PDF_REFERENCE.findall(block))
        return names

    # Chaque flux de contenu est lu avec les polices de sa page ou de son XObject ; à défaut, celles du document.
    every_font: dict[bytes, int] = {}
    owners: dict[int, dict[bytes, int]] = {}
    for number, head in heads.items():
        names = font_names(head)
        every_font.update(names)
        for content in resolve(head, b"Contents"):
            owners[content] = names
        if b"/Form" in head and number in raw_streams:
            owners[number] = names

    lines: list[str] = []
    for number in sorted(owners):
        data = stream(number)
        if data is not None and (b"Tj" in data or b"TJ" in data):
            lines.extend(_pdf_stream_lines(data, lambda name, names=owners[number]: cmaps.get(
                names.get(name, every_font.get(name, -1)))))
    return "\n".join(lines)


def _words(text: str) -> Counter[str]:
    return Counter(word for word in _normalize_text(text).split() if len(word) > 1)


def _text_coverage(reference: str, extracted: str) -> float | None:
    """Part des mots de la référence (avec leurs répétitions) retrouvés dans le texte extrait ; None sans référence."""
    expected = _words(reference)
    total = sum(expected.values())
    if not total:
        return None
    found = _words(extracted)
    return sum(min(count, found[word]) for word, count in expected.items()) / total


def _print_pdf(page: Page) -> tuple[bytes, str | None]:
    """PDF de la page entière avec les styles d'écran, et un avertissement si son texte est incomplet."""
    try:
        reference = str(page.evaluate(_VISIBLE_TEXT_JS))
    except Exception:
        reference = ""
    # Les feuilles d'impression des sites masquent souvent une partie de l'offre : le rendu écran est imprimé.
    page.emulate_media(media="screen")
    page.evaluate(_UNFIX_FOR_PRINT_JS)
    pdf = page.pdf(format="A4", print_background=True)
    try:
        coverage = _text_coverage(reference, _pdf_text(pdf))
    except Exception as exc:
        _LOGGER.warning("Texte du PDF non vérifiable : %s", exc)
        return pdf, "Le texte du PDF n'a pas pu être vérifié."
    if coverage is not None and coverage < _MIN_PDF_COVERAGE:
        return pdf, (
            f"Le PDF ne contient que {coverage:.0%} du texte visible de la page "
            f"(seuil {_MIN_PDF_COVERAGE:.0%}) ; une partie de l'offre peut manquer."
        )
    return pdf, None


@dataclass(frozen=True)
class _PageCapture:
    pdf: bytes | None
    png: bytes | None
    segments: tuple[bytes, ...]
    notes: tuple[str, ...]


_PNG_DROPPED = (
    "Page trop haute pour une image nette et complète : seul le PDF de la page complète est conservé, sans PNG."
)


def _capture_page(page: Page, url: str, *, deadline: float, headless: bool) -> _PageCapture:
    """Charge, nettoie et capture la page ; le PDF n'est produit qu'en headless."""
    page.set_default_timeout(_remaining_ms(deadline, ceiling_ms=12_000))
    document_statuses: list[int] = []

    def record_document(response: Any) -> None:
        try:
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                document_statuses.append(response.status)
        except Exception:
            pass

    page.on("response", record_document)
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=_remaining_ms(deadline, ceiling_ms=15_000))
        status = response.status if response is not None else 200
        if status >= 400 and status not in _BLOCKING_STATUSES:
            raise _CaptureFailure(f"La page d'origine répond avec le statut HTTP {status}.")
        try:
            page.wait_for_load_state("networkidle", timeout=_remaining_ms(deadline, ceiling_ms=4_000))
        except _CaptureFailure:
            raise
        except Exception:
            pass
        _wait_past_wall(page, deadline, max_wait_ms=6_000 if headless else 30_000)
        # Un contrôle anti-robot résolu recharge le document : seul le dernier statut compte.
        status = document_statuses[-1] if document_statuses else status
        if status in _BLOCKING_STATUSES:
            raise _CaptureFailure(f"La page d'origine refuse l'accès (statut HTTP {status}).", blocked=True)
        if status >= 400:
            raise _CaptureFailure(f"La page d'origine répond avec le statut HTTP {status}.")
        _dismiss_cookie_banner(page, deadline)
        _dismiss_overlays(page, deadline)
        _expand_collapsed(page, deadline)
        _scroll_page(page, deadline)
        # Certains bandeaux et fenêtres n'apparaissent qu'après un délai ou au défilement.
        _dismiss_cookie_banner(page, deadline)
        _dismiss_overlays(page, deadline)
        _validate_page(page)
        png = _screenshot(page)
        notes = () if png is not None else (_PNG_DROPPED,)
        if not headless:
            # Sans page.pdf(), le PDF sera fait d'images : en tranches fiables si la page est trop haute.
            segments = (png,) if png is not None else tuple(_screenshot_segments(page))
            return _PageCapture(None, png, segments, notes)
        pdf, warning = _print_pdf(page)
        return _PageCapture(pdf, png, (), notes + ((warning,) if warning else ()))
    except _CaptureFailure:
        raise
    except Exception as exc:
        _LOGGER.warning("Échec de capture : %s", exc)
        raise _CaptureFailure("Le navigateur n'a pas pu charger ou imprimer la page d'origine.") from exc


# PDF, PNG complet ou None, remarques à reporter dans `error` sans faire échouer la capture.
_Captured = tuple[bytes, bytes | None, tuple[str, ...]]


def _capture_ephemeral(browser: Browser, url: str, *, deadline: float) -> _Captured:
    """Niveau 1 : contexte vierge, sans cookie ni session."""
    context: BrowserContext | None = None
    try:
        context = browser.new_context(**_context_options())
        page = context.new_page()
        _guard_context(context, page)
        captured = _capture_page(page, url, deadline=deadline, headless=True)
        if captured.pdf is None:
            raise _CaptureFailure("Le navigateur n'a pas produit le PDF attendu.")
        return captured.pdf, captured.png, captured.notes
    except _CaptureFailure:
        raise
    except Exception as exc:
        _LOGGER.warning("Échec de capture : %s", exc)
        raise _CaptureFailure("Le navigateur sans session n'a pas pu capturer la page d'origine.") from exc
    finally:
        _close_quietly(context)


def _display_available() -> bool:
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _profile_lock(profile_dir: Path) -> threading.Lock:
    """Chromium refuse d'ouvrir deux fois le même profil : les captures dédiées sont sérialisées."""
    key = str(profile_dir.resolve())
    with _PROFILE_LOCKS_GUARD:
        return _PROFILE_LOCKS.setdefault(key, threading.Lock())


def _png_to_pdf(browser: Browser, images: tuple[bytes, ...], *, deadline: float) -> bytes:
    """Imprime hors ligne une ou plusieurs tranches PNG, à la suite ; Chromium les répartit sur les pages A4."""
    page = _offline_page(browser, deadline)
    try:
        markup = "".join(
            f"<img alt='Capture de l’offre' src='data:image/png;base64,{base64.b64encode(png).decode('ascii')}'>"
            for png in images
        )
        page.set_content(
            "<!doctype html><meta charset='utf-8'><style>"
            "@page{size:A4;margin:8mm}html,body{margin:0}img{display:block;width:100%;height:auto}"
            f"</style>{markup}",
            wait_until="load",
        )
        return page.pdf(format="A4", print_background=True)
    finally:
        _close_quietly(page.context)


def _capture_persistent(
    playwright: Playwright,
    engine: _Engine,
    browser: Browser,
    url: str,
    profile_dir: Path,
    *,
    headless: bool,
    deadline: float,
) -> _Captured:
    """Niveau 2 : profil dédié et persistant, headless ou fenêtre visible."""
    lock = _profile_lock(profile_dir)
    if not lock.acquire(timeout=_remaining_ms(deadline) / 1000):
        raise _CaptureFailure("Le navigateur dédié est occupé par une autre capture.")
    context: BrowserContext | None = None
    try:
        options = _context_options() | {"headless": headless, "timeout": _remaining_ms(deadline, ceiling_ms=15_000)}
        if engine.channel is not None:
            options["channel"] = engine.channel
        context = playwright.chromium.launch_persistent_context(profile_dir, **options)
        page = context.pages[0] if context.pages else context.new_page()
        _guard_context(context, page)
        captured = _capture_page(page, url, deadline=deadline, headless=headless)
        pdf = captured.pdf
        if pdf is None:
            # page.pdf() n'existe qu'en headless : l'image de la fenêtre visible est imprimée hors ligne.
            # La conversion garde quelques secondes même si la fenêtre visible a consommé tout le délai.
            pdf = _png_to_pdf(browser, captured.segments, deadline=max(deadline, time.monotonic() + 10))
        return pdf, captured.png, captured.notes
    except _CaptureFailure:
        raise
    except Exception as exc:
        _LOGGER.warning("Échec de capture : %s", exc)
        raise _CaptureFailure("Le navigateur dédié n'a pas pu capturer la page d'origine.") from exc
    finally:
        _close_quietly(context)
        lock.release()


def _string_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple | set):
        return "\n".join(_string_value(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(f"{key} : {_string_value(item)}" for key, item in value.items())
    return str(value)


def _escaped(value: object) -> str:
    return html.escape(_string_value(value), quote=True)


def _analysis_list(analysis: dict[str, Any], key: str) -> str:
    value = analysis.get(key)
    if not value:
        return ""
    items = value if isinstance(value, list | tuple) else [value]
    return "".join(f"<li>{_escaped(item)}</li>" for item in items)


def _human_date(captured_at: str) -> str:
    """« 2026-10-23T06:44:32Z » -> « 23/10/2026 à 08:44 » (heure de Paris) pour le bandeau lisible."""
    try:
        moment = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        return moment.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).strftime("%d/%m/%Y à %H:%M")
    except (ValueError, ZoneInfoNotFoundError):
        return captured_at


def _reconstructed_html(offer: dict, reason: str, captured_at: str) -> str:
    analysis_value = offer.get("analysis")
    analysis = analysis_value if isinstance(analysis_value, dict) else {}
    source_url = _string_value(offer.get("url"))
    try:
        safe_href = html.escape(validate_http_url(source_url), quote=True) if source_url else ""
    except ValueError:
        safe_href = ""
    source_display = _escaped(source_url) or "Non renseigné"
    source_markup = f'<a href="{safe_href}">{source_display}</a>' if safe_href else source_display
    summary = analysis.get("summary")
    deadline = analysis.get("application_deadline") or offer.get("application_deadline")
    analysis_sections = ""
    if summary:
        analysis_sections += f"<h2>Résumé JobRadar</h2><div class='text'>{_escaped(summary)}</div>"
    missions = _analysis_list(analysis, "missions")
    if missions:
        analysis_sections += f"<h2>Missions</h2><ul>{missions}</ul>"
    profile = _analysis_list(analysis, "profile")
    if profile:
        analysis_sections += f"<h2>Profil recherché</h2><ul>{profile}</ul>"

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<style>
@page {{ size: A4; margin: 17mm; }}
* {{ box-sizing: border-box; }}
body {{ color: #0B1F3A; font-family: system-ui, -apple-system, "Segoe UI", sans-serif; font-size: 10.5pt;
       line-height: 1.5; margin: 0; overflow-wrap: anywhere; }}
.banner {{ background: #EAF4FF; border-left: 5px solid #087CFA; margin-bottom: 22px; padding: 12px 14px; }}
h1 {{ color: #0B1F3A; font-size: 23pt; line-height: 1.15; margin: 0 0 8px; }}
h2 {{ border-bottom: 2px solid #087CFA; color: #0B1F3A; font-size: 14pt; margin: 24px 0 10px; padding-bottom: 4px; }}
.company {{ color: #087CFA; font-size: 14pt; font-weight: 700; margin-bottom: 14px; }}
.meta {{ border-collapse: collapse; margin: 8px 0 18px; width: 100%; }}
.meta th {{ color: #0B1F3A; text-align: left; vertical-align: top; width: 28%; }}
.meta td, .meta th {{ border-bottom: 1px solid #D8E2EF; padding: 5px 3px; }}
.text {{ white-space: pre-wrap; }}
a {{ color: #087CFA; }}
li {{ margin-bottom: 5px; }}
</style>
</head>
<body>
<div class="banner">Copie reconstituée par JobRadar le {_escaped(_human_date(captured_at))} — la page d'origine
n'était pas accessible ({_escaped(reason.rstrip(". "))}).</div>
<h1>{_escaped(offer.get("title")) or "Offre sans intitulé"}</h1>
<div class="company">{_escaped(offer.get("company")) or "Entreprise non renseignée"}</div>
<table class="meta">
<tr><th>Lieu</th><td>{_escaped(offer.get("location")) or "Non renseigné"}</td></tr>
<tr><th>Contrat</th><td>{_escaped(offer.get("contract")) or "Non renseigné"}</td></tr>
<tr><th>Date de publication</th><td>{_escaped(offer.get("date_posted")) or "Non renseignée"}</td></tr>
<tr><th>Date limite</th><td>{_escaped(deadline) or "Non renseignée"}</td></tr>
<tr><th>Lien d'origine</th><td>{source_markup}</td></tr>
</table>
<h2>Description complète</h2>
<div class="text">{_escaped(offer.get("description")) or "Description non disponible."}</div>
{analysis_sections}
</body>
</html>"""


def _render_reconstructed(browser: Browser, offer: dict, reason: str, captured_at: str, *, deadline: float) -> bytes:
    """Niveau 3 : contenu de l'offre échappé, imprimé sans JavaScript ni requête réseau."""
    page = _offline_page(browser, deadline)
    try:
        page.set_content(_reconstructed_html(offer, reason, captured_at), wait_until="load")
        return page.pdf(format="A4", print_background=True)
    finally:
        _close_quietly(page.context)


def _temporary_bytes(content: bytes, destination: Path) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _write_outputs(
    dest_dir: Path, *, pdf: bytes, png: bytes | None = None, drop_stale_png: bool = False
) -> tuple[Path, Path | None]:
    """Écrit les fichiers à côté de leur destination puis les substitue atomiquement.

    Avec `drop_stale_png`, un ancien offre.png est retiré quand la nouvelle capture n'en a pas : il ne
    correspondrait plus au PDF (aucune image partielle ou périmée n'est laissée à côté de la page complète).
    """
    if not pdf.startswith(b"%PDF"):
        raise _CaptureFailure("Le navigateur a produit un PDF invalide.")
    if png is not None and not png.startswith(_PNG_SIGNATURE):
        raise _CaptureFailure("Le navigateur a produit une image PNG invalide.")
    pdf_path = dest_dir / "offre.pdf"
    png_path = dest_dir / "offre.png" if png is not None else None
    temporaries: list[tuple[Path, Path]] = []
    try:
        temporaries.append((_temporary_bytes(pdf, pdf_path), pdf_path))
        if png is not None and png_path is not None:
            temporaries.append((_temporary_bytes(png, png_path), png_path))
        for temporary, destination in temporaries:
            os.replace(temporary, destination)
        if png is None and drop_stale_png:
            (dest_dir / "offre.png").unlink(missing_ok=True)
        return pdf_path, png_path
    except OSError as exc:
        raise _CaptureFailure("Les fichiers de capture ne peuvent pas être écrits.") from exc
    finally:
        for temporary, _ in temporaries:
            temporary.unlink(missing_ok=True)


def _failure_reason(failures: list[str]) -> str:
    unique = list(dict.fromkeys(failures))
    return " ".join(unique) if unique else "La page d'origine n'était pas accessible."


def _capture_levels(
    playwright: Playwright,
    *,
    url: str | None,
    dest_dir: Path,
    offer: dict,
    browser_profile_dir: Path,
    allow_headed: bool,
    failures: list[str],
    captured_at: str,
    page_deadline: float,
    final_deadline: float,
) -> CaptureResult:
    launched = _launch_first_engine(playwright, timeout_ms=_remaining_ms(final_deadline, ceiling_ms=15_000))
    if launched is None:
        return CaptureResult(
            "indisponible", None, None, "Aucun Chromium Playwright, Chrome ou Edge utilisable n'est disponible.",
            captured_at,
        )
    browser, engine = launched

    def attempt(level: CaptureLevel, capture: Callable[[], _Captured]) -> CaptureResult | bool:
        """Résultat si la capture réussit, sinon indique si l'échec vient d'un mur."""
        try:
            pdf, png, notes = capture()
            pdf_path, png_path = _write_outputs(dest_dir, pdf=pdf, png=png, drop_stale_png=True)
        except _CaptureFailure as exc:
            failures.append(str(exc))
            return exc.blocked
        remarks = [*failures, *notes]
        return CaptureResult(level, pdf_path, png_path, _failure_reason(remarks) if remarks else None, captured_at)

    def dedicated(target: str, headless: bool) -> _Captured:
        return _capture_persistent(
            playwright, engine, browser, target, browser_profile_dir, headless=headless, deadline=page_deadline
        )

    try:
        if url is not None:
            outcome = attempt("page", lambda: _capture_ephemeral(browser, url, deadline=page_deadline))
            if isinstance(outcome, CaptureResult):
                return outcome
            outcome = attempt("page_navigateur_dedie", lambda: dedicated(url, True))
            # La fenêtre visible ne sert qu'à franchir un mur, pas une page vide ou absente.
            if outcome is True and allow_headed and _display_available():
                outcome = attempt("page_navigateur_dedie", lambda: dedicated(url, False))
            if isinstance(outcome, CaptureResult):
                return outcome

        reason = _failure_reason(failures)
        rendered_offer = dict(offer)
        if url and not rendered_offer.get("url"):
            rendered_offer["url"] = url
        # La copie reconstituée garde toujours un minimum de temps, même si les niveaux précédents ont tout consommé.
        deadline = max(final_deadline, time.monotonic() + 5)
        try:
            pdf = _render_reconstructed(browser, rendered_offer, reason, captured_at, deadline=deadline)
            pdf_path, _ = _write_outputs(dest_dir, pdf=pdf)
        except Exception as exc:
            _LOGGER.warning("Échec de la copie reconstituée : %s", exc)
            return CaptureResult(
                "indisponible", None, None, f"{reason} La copie reconstituée n'a pas pu être produite.", captured_at
            )
        return CaptureResult("reconstituee", pdf_path, None, reason, captured_at)
    finally:
        _close_quietly(browser)


def capture_offer(
    *,
    url: str | None,
    dest_dir: Path,
    offer: dict,
    browser_profile_dir: Path,
    timeout_s: int = 45,
    allow_headed: bool = True,
) -> CaptureResult:
    """Capture une offre avec repli progressif, sans propager les échecs d'exécution."""
    if not isinstance(dest_dir, Path) or not dest_dir.is_absolute():
        raise ValueError("dest_dir doit être un chemin absolu.")
    if not isinstance(browser_profile_dir, Path):
        raise TypeError("browser_profile_dir doit être un Path.")
    if not isinstance(offer, dict):
        raise TypeError("offer doit être un dictionnaire.")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s < 1:
        raise ValueError("timeout_s doit être un entier positif.")
    if not isinstance(allow_headed, bool):
        raise TypeError("allow_headed doit être un booléen.")

    captured_at = _utc_now()
    start = time.monotonic()
    final_deadline = start + timeout_s
    # Une part du délai est réservée à la copie reconstituée.
    page_deadline = final_deadline - min(15.0, timeout_s / 3)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        browser_profile_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return CaptureResult("indisponible", None, None, "Les dossiers de capture ne peuvent pas être créés.", captured_at)

    failures: list[str] = []
    valid_url: str | None = None
    if url is None:
        failures.append("Aucune URL d'origine n'est renseignée.")
    else:
        try:
            valid_url = validate_http_url(url)
        except (TypeError, ValueError):
            failures.append("L'URL d'origine n'est pas une URL HTTP(S) valide.")

    try:
        # Instance Playwright propre à l'appel : sa sortie arrête le pilote et tout navigateur encore ouvert.
        with sync_playwright() as playwright:
            return _capture_levels(
                playwright,
                url=valid_url,
                dest_dir=dest_dir,
                offer=offer,
                browser_profile_dir=browser_profile_dir,
                allow_headed=allow_headed,
                failures=failures,
                captured_at=captured_at,
                page_deadline=page_deadline,
                final_deadline=final_deadline,
            )
    except Exception as exc:
        _LOGGER.warning("Moteur de capture indisponible : %s", exc)
        return CaptureResult(
            "indisponible",
            None,
            None,
            "Le moteur de capture n'a pas pu démarrer ; les captures existantes sont conservées.",
            captured_at,
        )


def _visible_text(markup: str) -> tuple[str, str]:
    """Titre et texte approximatif d'une page HTML, sans scripts ni styles (souvent porteurs de faux motifs)."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", markup, re.IGNORECASE | re.DOTALL)
    title = html.unescape(title_match.group(1)) if title_match else ""
    body = re.sub(r"<(script|style|noscript|template|svg)\b.*?</\1\s*>", " ", markup, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<!--.*?-->|<[^>]+>", " ", body, flags=re.DOTALL)
    return title, " ".join(html.unescape(body).split())


def is_offer_online(url: str, *, timeout_s: int = 20) -> bool | None:
    """True en ligne, False retirée, None indéterminé (erreur réseau, blocage)."""
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int) or timeout_s < 1:
        raise ValueError("timeout_s doit être un entier positif.")
    try:
        validated_url = validate_http_url(url)
    except ValueError:
        return None
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout_s, headers=_HEADERS) as client:
            with client.stream("GET", validated_url) as response:
                if response.status_code in {404, 410}:
                    return False
                if not 200 <= response.status_code < 300:
                    return None
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) >= _MAX_ONLINE_BYTES:
                        break
                markup = bytes(raw).decode(response.encoding or "utf-8", errors="replace")
                final_url = str(response.url)
    except httpx.HTTPError:
        return None

    title, text = _visible_text(markup)
    if _contains_any(_normalize_text(f"{title} {text}"), _EXPIRED_PATTERNS):
        return False
    if _looks_like_wall(final_url, title, text):
        return None
    return True
