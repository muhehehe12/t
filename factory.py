"""
factory.py — Hybrid King Website Generator v4
===============================================
Simplified for reliability. Only 24 Groq-filled placeholders
(down from 40+). Stock photos mapped by niche (no AI guessing).

AI: Groq API (free, 14,400 req/day, no credit card)
Model: llama-3.3-70b-versatile
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

import aiofiles
import httpx
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_MODEL = "llama-3.3-70b-versatile"
_REQUEST_DELAY = 4.0
_last_request_time: float = 0.0


class WebsiteOutput(BaseModel):
    index_html: str
    style_css: str = ""


# ═══════════════════════════════════════════════════════════
# Unsplash photos mapped by niche keyword
# Reliable — no AI guessing needed
# ═══════════════════════════════════════════════════════════

_NICHE_PHOTOS: dict[str, str] = {
    # Construction / building
    "staveb":    "photo-1504307651254-35680f356dfd",
    "zedni":     "photo-1504307651254-35680f356dfd",
    "rekonstruk":"photo-1581094794329-c8112a89af12",
    # Painting
    "malir":     "photo-1562259929-b4e1fd3aef09",
    "malov":     "photo-1562259929-b4e1fd3aef09",
    "lakyr":     "photo-1562259929-b4e1fd3aef09",
    "nater":     "photo-1562259929-b4e1fd3aef09",
    # Plumbing / heating
    "instal":    "photo-1585704032915-c3400ca199e7",
    "vodoin":    "photo-1585704032915-c3400ca199e7",
    "topen":     "photo-1585704032915-c3400ca199e7",
    "voda":      "photo-1585704032915-c3400ca199e7",
    # Electrical
    "elektr":    "photo-1621905251918-48416bd8575a",
    "elektro":   "photo-1621905251918-48416bd8575a",
    # Cleaning
    "uklid":     "photo-1581578731548-c64695cc6952",
    "cisten":    "photo-1581578731548-c64695cc6952",
    # Gardening / landscaping
    "zahrad":    "photo-1416879595882-3373a0480b5b",
    "sekani":    "photo-1416879595882-3373a0480b5b",
    # Moving
    "stehov":    "photo-1600518464441-9154a4dea21b",
    # Carpentry / roofing
    "tesar":     "photo-1588854337236-6889d631faa8",
    "pokryvac":  "photo-1588854337236-6889d631faa8",
    "strech":    "photo-1588854337236-6889d631faa8",
    # Handyman
    "hodinov":   "photo-1581578731548-c64695cc6952",
    "manzel":    "photo-1581578731548-c64695cc6952",
    "oprav":     "photo-1581578731548-c64695cc6952",
    # Tiling / flooring
    "obklad":    "photo-1584622650111-993a426fbf0a",
    "dlazb":     "photo-1584622650111-993a426fbf0a",
    "podlah":    "photo-1584622650111-993a426fbf0a",
    # Welding / metal
    "svar":      "photo-1504328345606-18bbc8c9d7d1",
    "zamec":     "photo-1504328345606-18bbc8c9d7d1",
    "kov":       "photo-1504328345606-18bbc8c9d7d1",
    # HVAC / heat pumps
    "klimat":    "photo-1631545806609-05172b622742",
    "cerpad":    "photo-1631545806609-05172b622742",
    "reviz":     "photo-1631545806609-05172b622742",
    # Default
    "default":   "photo-1504307651254-35680f356dfd",
}


def _pick_photo(business_name: str, niche: str) -> str:
    """Pick Unsplash photo ID based on niche/name keywords."""
    combined = (business_name + " " + niche).lower()
    for keyword, photo_id in _NICHE_PHOTOS.items():
        if keyword in combined:
            return photo_id
    return _NICHE_PHOTOS["default"]


# ═══════════════════════════════════════════════════════════
# Groq placeholders — only 24 keys (was 40+)
# ═══════════════════════════════════════════════════════════

_PLACEHOLDERS = [
    "MOTTO",
    "COLOR", "ACCENT",
    "STAT_1", "STAT_1_LABEL",
    "STAT_2", "STAT_2_LABEL",
    "STAT_3", "STAT_3_LABEL",
    "SERVICES_TITLE", "SERVICES_SUB",
    "SVC_1_TITLE", "SVC_1_DESC",
    "SVC_2_TITLE", "SVC_2_DESC",
    "SVC_3_TITLE", "SVC_3_DESC",
    "CTA_TEXT",
]


# ═══════════════════════════════════════════════════════════
# DEFENSIVE LAYER — sanitizare + fallback-uri per limba
# Toate sunt valori sigure, fara emoji, fara caractere ciudate,
# care arata profesional chiar daca Groq returneaza tampenii.
# ═══════════════════════════════════════════════════════════

# Defaults per placeholder pe limba — folosite cand Groq da
# gol, prea scurt, prea lung, sau text invalid.
_DEFAULTS: dict[str, dict[str, str]] = {
    "Czech": {
        "MOTTO":          "Kvalita a spolehlivost na prvnim miste.",
        "COLOR":          "#1F3A5F",
        "ACCENT":         "#C9822A",
        "STAT_1":         "10+", "STAT_1_LABEL": "Let zkusenosti",
        "STAT_2":         "100%", "STAT_2_LABEL": "Spokojeni klienti",
        "STAT_3":         "24h",  "STAT_3_LABEL": "Doba odezvy",
        "SERVICES_TITLE": "Nabidka sluzeb",
        "SERVICES_SUB":   "Profesionalni resseni pro vase potreby",
        "SVC_1_TITLE":    "Konzultace zdarma",
        "SVC_1_DESC":     "Nezavazna konzultace a kalkulace ceny pred zacatkem prace.",
        "SVC_2_TITLE":    "Realizace na klic",
        "SVC_2_DESC":     "Komplexni provedeni zakazky od zacatku do konce.",
        "SVC_3_TITLE":    "Zaruka kvality",
        "SVC_3_DESC":     "Pouzivame overene postupy a kvalitni materialy.",
        "CTA_TEXT":       "Napiste nam na WhatsApp a domluvime detaily.",
    },
    "Romanian": {
        "MOTTO":          "Calitate si seriozitate la fiecare proiect.",
        "COLOR":          "#1F3A5F",
        "ACCENT":         "#C9822A",
        "STAT_1":         "10+", "STAT_1_LABEL": "Ani experienta",
        "STAT_2":         "100%", "STAT_2_LABEL": "Clienti multumiti",
        "STAT_3":         "24h",  "STAT_3_LABEL": "Timp de raspuns",
        "SERVICES_TITLE": "Oferta de servicii",
        "SERVICES_SUB":   "Solutii profesionale pentru nevoile dumneavoastra",
        "SVC_1_TITLE":    "Consultanta gratuita",
        "SVC_1_DESC":     "Consultanta gratuita si estimarea pretului inainte de inceperea lucrarii.",
        "SVC_2_TITLE":    "Lucrari la cheie",
        "SVC_2_DESC":     "Realizare completa a proiectului de la inceput pana la final.",
        "SVC_3_TITLE":    "Garantie",
        "SVC_3_DESC":     "Folosim metode verificate si materiale de calitate.",
        "CTA_TEXT":       "Scrieti-ne pe WhatsApp si stabilim detaliile.",
    },
    "English": {
        "MOTTO":          "Quality and reliability you can trust.",
        "COLOR":          "#1F3A5F",
        "ACCENT":         "#C9822A",
        "STAT_1":         "10+", "STAT_1_LABEL": "Years of experience",
        "STAT_2":         "100%", "STAT_2_LABEL": "Satisfied clients",
        "STAT_3":         "24h",  "STAT_3_LABEL": "Response time",
        "SERVICES_TITLE": "Our services",
        "SERVICES_SUB":   "Professional solutions for your needs",
        "SVC_1_TITLE":    "Free consultation",
        "SVC_1_DESC":     "Free consultation and price estimate before starting work.",
        "SVC_2_TITLE":    "Complete service",
        "SVC_2_DESC":     "Full project execution from start to finish.",
        "SVC_3_TITLE":    "Quality guarantee",
        "SVC_3_DESC":     "We use proven methods and quality materials.",
        "CTA_TEXT":       "Message us on WhatsApp to arrange details.",
    },
}

# Limite per camp — sub minim sau peste maxim => folosesc default.
# Acestea sunt LIMITE DE AFISARE, nu de continut. Sub minim =
# probabil gol/eroare, peste maxim = strica layout-ul.
_FIELD_LIMITS: dict[str, tuple[int, int]] = {
    "MOTTO":          (10, 80),
    "STAT_1":         (1, 12), "STAT_1_LABEL": (3, 28),
    "STAT_2":         (1, 12), "STAT_2_LABEL": (3, 28),
    "STAT_3":         (1, 12), "STAT_3_LABEL": (3, 28),
    "SERVICES_TITLE": (5, 60),
    "SERVICES_SUB":   (10, 120),
    "SVC_1_TITLE":    (3, 40), "SVC_1_DESC": (15, 180),
    "SVC_2_TITLE":    (3, 40), "SVC_2_DESC": (15, 180),
    "SVC_3_TITLE":    (3, 40), "SVC_3_DESC": (15, 180),
    "CTA_TEXT":       (10, 140),
}

# Pattern hex color valid (3, 6 sau 8 cifre)
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{3}([0-9A-Fa-f]{3}([0-9A-Fa-f]{2})?)?$")

# Pattern: caracter de control sau caracter non-printable
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Pattern emoji larg (categoriile Unicode emoji)
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF"     # emoticoane, simboluri, transport, etc
    "\U00002600-\U000027BF"      # dingbats, simboluri diverse
    "\U0001F900-\U0001F9FF"      # supplemental symbols
    "\u200d\u20e3\ufe0f]+",      # ZWJ + variation selectors
)


def _clean_text(s: str) -> str:
    """Sanitizeaza un string: scoate emoji, caractere de control,
    HTML basic, whitespace excesiv. NU schimba continutul lingvistic."""
    if not isinstance(s, str):
        return ""
    # Scoate emoji
    s = _EMOJI_RE.sub("", s)
    # Scoate caractere de control
    s = _CONTROL_RE.sub("", s)
    # Scoate < si > ca masura defensiva (XSS, layout-break)
    s = s.replace("<", "").replace(">", "")
    # Colapseaza whitespace multiplu -> un spatiu
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _validate_field(value: str, key: str, default: str) -> str:
    """Valideaza un camp text si cade pe default daca e invalid.
    Validari: lungime, caractere admise."""
    cleaned = _clean_text(value)

    if not cleaned:
        return default

    # Verifica lungimea conform _FIELD_LIMITS
    limits = _FIELD_LIMITS.get(key)
    if limits:
        lo, hi = limits
        if len(cleaned) < lo:
            logger.debug("Camp '%s' prea scurt (%d < %d): fallback",
                         key, len(cleaned), lo)
            return default
        if len(cleaned) > hi:
            # Trunchiez in loc sa cad pe default — pastreaza continutul
            # cat mai mult, dar evita layout broken
            cleaned = cleaned[:hi].rsplit(" ", 1)[0].rstrip(",.;:") + "."
            logger.debug("Camp '%s' trunchiat la %d char", key, len(cleaned))

    return cleaned


def _validate_hex(value: str, default: str) -> str:
    """Returneaza culoarea hex daca e valida, altfel default."""
    if not isinstance(value, str):
        return default
    v = value.strip()
    if _HEX_RE.match(v):
        return v
    logger.debug("Culoare invalida '%s', folosesc default '%s'", v, default)
    return default


def _clean_business_name(name: str, language: str) -> str:
    """
    Curata numele firmei venit din scraper inainte de afisare.
    Scoate telefoane vizibile, ALL CAPS extreme, caractere ciudate,
    trunchiaza la lungime rezonabila pentru titlu.
    """
    cleaned = _clean_text(name)

    # Scoate telefoane vizibile in titlu (ex: "Zugrav 0723123456")
    cleaned = re.sub(
        r"(?:\+?\d{2,3}[\s\-]?)?\d{3,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4}",
        "", cleaned,
    ).strip()

    # Daca e in ALL CAPS si are > 15 caractere, foloseste Title Case
    if cleaned.isupper() and len(cleaned) > 15:
        cleaned = cleaned.title()

    # Scoate semne de exclamare/interogare repetate (!!!, ???)
    cleaned = re.sub(r"[!?]{2,}", "", cleaned)

    # Trunchiaza la 60 caractere (limita umana pentru un H1)
    if len(cleaned) > 60:
        cleaned = cleaned[:60].rsplit(" ", 1)[0]

    # Daca a ramas gol dupa toate sanitizarile, fallback generic
    if not cleaned or len(cleaned) < 3:
        cleaned = ("Profesionalni sluzby" if language == "Czech"
                   else "Servicii profesionale" if language == "Romanian"
                   else "Professional services")

    return cleaned


def _sanitize_all_placeholders(
    raw: dict, language: str,
) -> dict[str, str]:
    """
    Verifica si curata TOATE placeholder-ele Groq inainte sa
    intre in template. Garantie: nu va exista niciodata camp gol,
    text cu emoji, hex invalid, sau text care strica layout-ul.
    """
    defaults = _DEFAULTS.get(language, _DEFAULTS["English"])
    result: dict[str, str] = {}

    for key in _PLACEHOLDERS:
        default_val = defaults[key]
        raw_val = raw.get(key, "")

        # Culorile au validare speciala (hex pattern)
        if key in ("COLOR", "ACCENT"):
            result[key] = _validate_hex(str(raw_val), default_val)
            continue

        # Restul - text general, cu limite de lungime per camp
        result[key] = _validate_field(str(raw_val), key, default_val)

    return result


# ═══════════════════════════════════════════════════════════
# System prompts — simple, clear instructions
# ═══════════════════════════════════════════════════════════

_SYSTEM_CZECH = (
    "Jsi copywriter pro ceske remeslniky. Vyplnis JSON pro web stranky.\n\n"
    "PRAVIDLA:\n"
    "- MOTTO: 1 veta, max 8 slov, inspirativni motto firmy. Napr: 'Kvalita a spolehlivost na prvnim miste.'\n"
    "- COLOR: hlavni hex barva pro obor (tmavsi). Napr: #1A5276 (instalater), #C0392B (stavba), #196F3D (zahrada)\n"
    "- ACCENT: vyrazna hex barva pro tlacitka. Napr: #E67E22, #F39C12, #2ECC71\n"
    "- STAT cisla: realisticka. Napr: '150+', '12 let', '100%'\n"
    "- SVC: 3 sluzby s krátkym popisem (1-2 vety)\n"
    "- CTA_TEXT: 1 veta motivace napsat na WhatsApp. Napr: 'Napiste nam a domluvime se.'\n"
    "- ZAKAZANO: emoji, specialni znaky, emotikony\n"
    "- Vrat POUZE JSON, nic jineho.\n"
)

_SYSTEM_ENGLISH = (
    "You are a copywriter for local tradespeople. Fill JSON for a website.\n\n"
    "RULES:\n"
    "- MOTTO: 1 sentence, max 8 words, inspirational business motto. E.g: 'Quality and reliability you can trust.'\n"
    "- COLOR: dark industry hex. E.g: #1A5276 (plumber), #C0392B (builder), #196F3D (gardener)\n"
    "- ACCENT: bright button hex. E.g: #E67E22, #F39C12, #2ECC71\n"
    "- STAT numbers: realistic. E.g: '150+', '12 years', '100%'\n"
    "- SVC: 3 services with short description (1-2 sentences)\n"
    "- CTA_TEXT: 1 sentence motivating to write on WhatsApp. E.g: 'Message us and we will arrange everything.'\n"
    "- FORBIDDEN: emoji, emoticons, special characters\n"
    "- Return ONLY JSON.\n"
)

_SYSTEM_ROMANIAN = (
    "Esti copywriter pentru meseriasi. Completezi JSON pentru un site web.\n\n"
    "REGULI:\n"
    "- MOTTO: 1 propozitie, max 8 cuvinte, motto inspirational. Ex: 'Calitate si seriozitate la fiecare proiect.'\n"
    "- COLOR: culoare hex industrie (inchisa). Ex: #1A5276 (instalator), #C0392B (constructor)\n"
    "- ACCENT: culoare hex butoane (vie). Ex: #E67E22, #F39C12\n"
    "- STAT cifre: realiste. Ex: '150+', '12 ani', '100%'\n"
    "- SVC: 3 servicii cu descriere scurta\n"
    "- CTA_TEXT: 1 propozitie motivanta sa scrie pe WhatsApp\n"
    "- INTERZIS: emoji, emoticoane, caractere speciale\n"
    "- Returneaza DOAR JSON.\n"
)

_SYSTEMS = {
    "Czech":    _SYSTEM_CZECH,
    "English":  _SYSTEM_ENGLISH,
    "Romanian": _SYSTEM_ROMANIAN,
}


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

def _extract_json(text: str) -> str:
    """Extract first complete JSON object from text."""
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\n?```\s*$", "", text, flags=re.MULTILINE).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON in response")
    depth, in_str, escape = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise ValueError("Unterminated JSON")


def _strip_emoji(text: str) -> str:
    return re.sub(
        "[\U00010000-\U0010FFFF\u2600-\u26FF\u2700-\u27BF]",
        "", text
    ).strip()


async def _load_template(language: str) -> str:
    base = Path(__file__).parent / "templates"
    mapping = {
        "English":  "uk_tradesman.html",   # UK alb-curat sans-serif
        "Romanian": "rom_tradesman.html",  # RO premium
    }
    # Czech a fost eliminat complet din sistem — orice valoare
    # necunoscuta cade pe Romanian ca fallback sigur, NU pe Czech
    # (template-ul czech_tradesman.html nu mai exista pe disc).
    template_file = mapping.get(language, "rom_tradesman.html")
    path = base / template_file
    async with aiofiles.open(path, encoding="utf-8") as fh:
        return await fh.read()


# ═══════════════════════════════════════════════════════════
# Curatare titlu — Romania
# Anunturile de pe OLX/Publi24 vin uneori cu sufixe de platforma
# in titlu (ex: "Electrician Bucuresti - OLX.ro"). Le stergem
# obligatoriu inainte de publicare, conform cerintei.
# ═══════════════════════════════════════════════════════════

_PLATFORM_SUFFIXES_RO = [
    r"\s*[-–|•]\s*OLX\.ro\s*$",
    r"\s*[-–|•]\s*OLX\s*$",
    r"\s*\(OLX\.ro\)\s*$",
    r"\s*[-–|•]\s*Publi24\.ro\s*$",
    r"\s*[-–|•]\s*Publi24\s*$",
    r"\s*\(Publi24\.ro\)\s*$",
    r"\s*[-–|•]\s*anun[tţ]uri\s*$",
    r"^\s*OLX\.ro\s*[-–|•]\s*",
    r"^\s*Publi24\.ro\s*[-–|•]\s*",
]


def _clean_romanian_title(name: str) -> str:
    """Sterge sufixele de platforma (OLX.ro, Publi24.ro) din titlu."""
    cleaned = name
    for pattern in _PLATFORM_SUFFIXES_RO:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" -–|•")
    return cleaned if cleaned else name


def _format_description_section(description: str, language: str) -> str:
    """
    Formateaza descrierea originala a anuntului intr-o sectiune
    HTML curata, fara emoji, cu paragrafe separate. Daca nu exista
    descriere, sectiunea intreaga e omisa (string gol).
    """
    text = (description or "").strip()
    if len(text) < 15:
        return ""

    # Scapa HTML, taie la o lungime rezonabila pentru afisare
    import html as html_lib
    safe = html_lib.escape(text[:900]).strip()

    heading = {
        "Czech":    "O nasich sluzbach",
        "Romanian": "Despre serviciile noastre",
        "English":  "About our services",
    }.get(language, "O nasich sluzbach")

    return (
        '<section class="about reveal">\n'
        '  <div class="about-inner">\n'
        f'    <h2>{heading}</h2>\n'
        f'    <p class="about-text">{safe}</p>\n'
        '  </div>\n'
        '</section>'
    )


def _build_prompt(
    name: str, niche: str, lang: str, phone: str, city: str,
    description: str = "",
) -> str:
    keys = "\n".join(f'  "{k}": "..."' for k in _PLACEHOLDERS)
    desc_block = (
        f"Descrierea originala a anuntului (foloseste-o ca sursa de "
        f"inspiratie reala, nu o copia textual):\n{description[:600]}\n\n"
        if description else ""
    )
    return (
        f"Business: {name}\n"
        f"Niche: {niche}\n"
        f"City: {city}\n"
        f"Language: {lang}\n\n"
        f"{desc_block}"
        f"Return JSON with EXACTLY these keys:\n"
        f"{{\n{keys}\n}}\n\n"
        f"No emoji. All text in {lang}. Hex colors only."
    )


async def _call_groq(system: str, prompt: str) -> str:
    """Call Groq API with rate limiting + automatic key rotation on 429."""
    global _last_request_time
    from groq_rotator import get_rotator

    rotator = get_rotator()

    # Incearca fiecare cheie disponibila
    for attempt in range(rotator.count):
        now = time.monotonic()
        elapsed = now - _last_request_time
        if elapsed < _REQUEST_DELAY:
            await asyncio.sleep(_REQUEST_DELAY - elapsed)
        _last_request_time = time.monotonic()

        key = rotator.current
        if not key:
            raise RuntimeError("GROQ_API_KEY(S) nu sunt setate in .env")

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                _GROQ_URL,
                json={
                    "model": _MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user",   "content": prompt},
                    ],
                    "temperature": 0.7,
                    "max_tokens": 1500,
                    "response_format": {"type": "json_object"},
                },
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )

            if r.status_code == 429:
                logger.warning(
                    "Groq 429 (rate limit) pe cheie slot=%d — rotesc...",
                    attempt,
                )
                rotator.rotate()
                await asyncio.sleep(3)
                continue

            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]

    # Toate cheile au 429 — asteapta si ridica exceptie pt retry extern
    raise RuntimeError(
        "429: Toate cheile Groq au atins rate limit. "
        f"Stare rotator: {rotator.status()}"
    )


# ═══════════════════════════════════════════════════════════
# Main generator
# ═══════════════════════════════════════════════════════════

def _build_chatbot_widget(job_id: int, language: str, accent: str) -> str:
    """
    Widget de chat injectat doar pentru clientii cu chatbot_enabled=True.
    Vorbeste cu chatbot_server.py (backend separat, vezi acel fisier),
    NU contine nicio cheie API — sigur de pus in HTML public.
    """
    if not settings.chatbot_api_url:
        return ""  # backend neconfigurat — widget omis complet

    placeholder = {
        "Czech":    "Napiste zpravu...",
        "Romanian": "Scrieti un mesaj...",
    }.get(language, "Type a message...")

    greeting = {
        "Czech":    "Dobry den! Jak vam mohu pomoci?",
        "Romanian": "Buna ziua! Cum va pot ajuta?",
    }.get(language, "Hello! How can I help you?")

    return f'''
<div id="hk-chat-widget" style="position:fixed;bottom:90px;right:1.5rem;z-index:998;font-family:Inter,sans-serif;">
  <button id="hk-chat-toggle" style="width:58px;height:58px;border-radius:50%;background:{accent};border:none;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,0.3);display:flex;align-items:center;justify-content:center;">
    <svg width="26" height="26" viewBox="0 0 24 24" fill="white"><path d="M12 2C6.48 2 2 6.48 2 12c0 1.54.36 3 1 4.3L2 22l5.7-1c1.3.64 2.76 1 4.3 1 5.52 0 10-4.48 10-10S17.52 2 12 2z"/></svg>
  </button>
  <div id="hk-chat-panel" style="display:none;position:absolute;bottom:70px;right:0;width:300px;max-height:420px;background:#fff;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,0.25);overflow:hidden;flex-direction:column;">
    <div style="background:{accent};color:#fff;padding:0.9rem 1rem;font-weight:700;font-size:0.9rem;">{greeting}</div>
    <div id="hk-chat-msgs" style="flex:1;overflow-y:auto;padding:0.8rem;max-height:280px;font-size:0.85rem;line-height:1.5;"></div>
    <div style="display:flex;border-top:1px solid #eee;">
      <input id="hk-chat-input" type="text" placeholder="{placeholder}" style="flex:1;border:none;padding:0.7rem;font-size:0.85rem;outline:none;">
      <button id="hk-chat-send" style="background:{accent};color:#fff;border:none;padding:0 1rem;cursor:pointer;font-weight:700;">→</button>
    </div>
  </div>
</div>
<script>
(function() {{
  var JOB_ID = {job_id};
  var API = "{settings.chatbot_api_url}";
  var panel = document.getElementById('hk-chat-panel');
  var toggle = document.getElementById('hk-chat-toggle');
  var msgs = document.getElementById('hk-chat-msgs');
  var input = document.getElementById('hk-chat-input');
  var send = document.getElementById('hk-chat-send');

  toggle.addEventListener('click', function() {{
    panel.style.display = panel.style.display === 'none' ? 'flex' : 'none';
  }});

  function addMsg(text, isUser) {{
    var div = document.createElement('div');
    div.style.margin = '0.4rem 0';
    div.style.textAlign = isUser ? 'right' : 'left';
    var bubble = document.createElement('span');
    bubble.style.display = 'inline-block';
    bubble.style.padding = '0.5rem 0.8rem';
    bubble.style.borderRadius = '10px';
    bubble.style.maxWidth = '85%';
    bubble.style.background = isUser ? '{accent}' : '#f0f0f0';
    bubble.style.color = isUser ? '#fff' : '#222';
    bubble.textContent = text;
    div.appendChild(bubble);
    msgs.appendChild(div);
    msgs.scrollTop = msgs.scrollHeight;
  }}

  async function sendMsg() {{
    var text = input.value.trim();
    if (!text) return;
    addMsg(text, true);
    input.value = '';
    try {{
      var res = await fetch(API + '/chat/' + JOB_ID, {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{message: text}})
      }});
      var data = await res.json();
      addMsg(data.reply || '...', false);
    }} catch (e) {{
      addMsg('Connection error.', false);
    }}
  }}

  send.addEventListener('click', sendMsg);
  input.addEventListener('keypress', function(e) {{
    if (e.key === 'Enter') sendMsg();
  }});
}})();
</script>
'''


async def generate_website(
    business_name: str,
    niche: str,
    language: str = "Romanian",
    phone: str = "",
    city: str = "",
    description: str = "",
    job_id: int = 0,
    chatbot_enabled: bool = False,
) -> WebsiteOutput:
    """Generate website from template + Groq JSON."""

    # Curatare titlu — obligatoriu pentru Romania (sterge sufixe
    # de platforma precum "- OLX.ro" sau "- Publi24.ro")
    if language == "Romanian":
        business_name = _clean_romanian_title(business_name)

    # Defaults
    city = city.strip() or (
        "Praha" if language == "Czech"
        else "Bucuresti" if language == "Romanian"
        else "London"
    )
    phone_raw = phone.strip() or "+420 000 000 000"
    phone_href = re.sub(r"[^\d+]", "", phone_raw)
    phone_digits = re.sub(r"[^\d]", "", phone_raw)

    # Pick stock photo by niche
    photo_id = _pick_photo(business_name, niche)

    # Load template (selectat automat pe limba/locatie)
    template = await _load_template(language)

    # Sectiune descriere — formatata, fara emoji, sau goala
    description_section = _format_description_section(description, language)

    # Call Groq — cu strategie defensiva: niciodata exceptie spre exterior.
    # Daca Groq esueaza complet, folosim 100% defaults — site arata profesional
    # chiar daca AI-ul nu raspunde deloc.
    system = _SYSTEMS.get(language, _SYSTEM_CZECH)
    prompt = _build_prompt(business_name, niche, language, phone_raw, city,
                            description=description)

    raw_groq_response: dict = {}

    for attempt in range(1, 4):
        try:
            raw = await _call_groq(system, prompt)
            data = json.loads(_extract_json(raw))

            # Verifica cate chei sunt prezente (nu obligam sa fie toate)
            present = [k for k in _PLACEHOLDERS if k in data]
            missing = [k for k in _PLACEHOLDERS if k not in data]
            if missing:
                logger.info(
                    "Groq attempt %d: lipsesc %d chei %s — voi folosi "
                    "defaults pentru ele", attempt, len(missing), missing,
                )

            # Pastreaza ce avem (chiar partial); validarea + sanitizarea
            # se face dupa in _sanitize_all_placeholders, care umple golurile
            raw_groq_response = data
            logger.info(
                "Groq OK pentru '%s' (%s) attempt %d (%d/%d chei prezente)",
                business_name, language, attempt,
                len(present), len(_PLACEHOLDERS),
            )
            break

        except (ValueError, json.JSONDecodeError, KeyError,
                httpx.HTTPError) as exc:
            logger.warning(
                "Attempt %d/3 pentru '%s': %s",
                attempt, business_name, exc,
            )
            if "429" in str(exc):
                logger.info("Groq rate limit — astept 65s...")
                await asyncio.sleep(65)
            else:
                await asyncio.sleep(4 * attempt)
    else:
        # Toate 3 incercari au esuat — NU mai aruncam exceptie, mergem cu defaults
        logger.error(
            "Groq a esuat 3x pentru '%s' — generez site cu defaults pentru "
            "limba '%s'. Site-ul va arata profesional dar generic.",
            business_name, language,
        )
        raw_groq_response = {}

    # ── DEFENSIVE LAYER: sanitizeaza si umple toate placeholderele ───
    # Garantie absoluta: nicio cheie goala, niciun text cu emoji,
    # niciun hex invalid, nimic care sa strice layout-ul.
    placeholders = _sanitize_all_placeholders(raw_groq_response, language)

    # ── Replace all placeholders ─────────────────────────

    chatbot_widget = (
        _build_chatbot_widget(job_id, language, placeholders["ACCENT"])
        if chatbot_enabled and job_id else ""
    )

    # Static fields (from scraper data, not Groq) — si BUSINESS_NAME e
    # CURATAT defensiv inainte de injectare in HTML
    clean_business_name = _clean_business_name(business_name, language)
    static = {
        "BUSINESS_NAME":       clean_business_name,
        "CITY":                _clean_text(city) or city,
        "PHONE_RAW":           phone_href,
        "PHONE_DISPLAY":       phone_raw,
        "PHONE_DIGITS":        phone_digits,
        "PHOTO_ID":            photo_id,
        "DESCRIPTION_SECTION": description_section,
        "CHATBOT_WIDGET":      chatbot_widget,
    }

    html = template
    for k, v in static.items():
        html = html.replace("{{" + k + "}}", v)
    for k, v in placeholders.items():
        html = html.replace("{{" + k + "}}", v)

    # Verificare finala absoluta: NICIUN placeholder nu trebuie sa ramana
    # nereplacat (ar arata pe site ca text crud "{{XXX}}"). Daca totusi
    # se intampla (bug in cod, nu in Groq), il inlocuim cu sir gol —
    # mai bine spatiu gol decat "{{TAGLINE}}" vizibil pe site.
    remaining = re.findall(r"\{\{[A-Z_0-9]+\}\}", html)
    if remaining:
        logger.error(
            "BUG: placeholdere nereplacate dupa toata sanitizarea: %s — "
            "le sterg ca sa nu apara pe site", remaining,
        )
        for ph in set(remaining):
            html = html.replace(ph, "")

    logger.info(
        "Site ready: '%s' | %d chars | from Groq: %d/%d chei",
        clean_business_name, len(html),
        len(raw_groq_response) if raw_groq_response else 0,
        len(_PLACEHOLDERS),
    )
    return WebsiteOutput(index_html=html, style_css="")


# ═══════════════════════════════════════════════════════════
# Demo
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    async def _demo():
        r = await generate_website(
            business_name="Novak Instalaterske prace",
            niche="Instalaterske prace",
            language="Czech",
            phone="+420 721 234 567",
            city="Praha",
        )
        print(f"HTML: {len(r.index_html):,} chars")
        Path("demo.html").write_text(r.index_html, encoding="utf-8")
        print("Saved: demo.html")

    asyncio.run(_demo())