"""
lead_scoring.py — Lead scoring engine pentru filtrare premium
================================================================
Scor 0-15 pe lead. Doar scor >=7 trece la deployment automat.
Lead-urile sub 7 sunt salvate dar marcate SKIP_LOW_SCORE — le poti
revizui manual ulterior daca vrei.

SEMNALELE SUNT REGEX-BASED, nu AI — ruleaza local, instant, fara
sa consume Groq tokens. AI-ul ramane pentru generare site, nu scoring.

CATEGORII DE SCOR:

POSITIVE (+):
  +5  Niche prioritare sezonier (air conditioning vara, etc — TOP PRIORITY)
  +3  Semnal "firma profesionista" (echipa, ani experienta, flota)
  +2  Anunt cu telefon vizibil direct (nu prin API fallback)
  +2  Descriere lunga (>200 char = anunt serios)
  +2  Promovat / "Featured" pe platforma
  +1  Multiple servicii listate
  +1  Adresa fizica mentionata (nu doar "lucrez la domiciliu")

NEGATIVE (-):
  -3  Semnal "lucrator strain" (anunturi de joburi in Germania/UK etc)
  -3  Spam comercial / ALL CAPS extreme
  -2  Anunt foarte scurt (<50 char = leftover/test)
  -2  Numere multiple in descriere (anunt vechi multifolosit)
  -1  Lipsa descriere completa
"""
from __future__ import annotations

import re
from datetime import datetime
from dataclasses import dataclass

# ═══════════════════════════════════════════════════════════
# Semnale "firma profesionista" — confirma ca NU e meserias
# individual, ci o firma cu echipa
# ═══════════════════════════════════════════════════════════

# Romana
_PRO_SIGNALS_RO = [
    # Echipa
    r"echip[aă]\s+(?:de\s+)?\d+",        # "echipa de 5", "echipa 8"
    r"\d+\s+(?:angaja|muncit|special)",   # "5 angajati", "10 muncitori"
    r"firm[aă]\s+(?:cu|de)\s+\d+",        # "firma cu 10 ani"
    # Experienta
    r"\d{1,2}\s+ani\s+(?:experienta|experienţă)",
    r"peste\s+\d+\s+ani",
    r"din\s+(?:anul\s+)?(?:19|20)\d{2}",  # "din 2010", "din anul 2015"
    # Flota / dotari
    r"flot[aă]\s+(?:proprie|de)",
    r"utilaje\s+(?:proprii|moderne)",
    r"echipamente\s+profesionale",
    # Acreditari
    r"autoriza[tţ]i?\s+(?:ANRE|MDRT|RAR)",
    r"certifica[tţ]i?\s+ISO",
    # Servicii la cheie
    r"servicii\s+la\s+cheie",
    r"lucr[aă]ri\s+complet",
]

# Engleza (UK)
_PRO_SIGNALS_EN = [
    # Team
    r"team\s+of\s+\d+",
    r"\d+\s+(?:staff|employees|workers)",
    r"established\s+(?:in\s+)?(?:19|20)\d{2}",
    # Experience
    r"\d{1,2}\s*\+?\s*years?\s+(?:experience|in\s+business|established)",
    r"over\s+\d+\s+years",
    r"since\s+(?:19|20)\d{2}",
    # Fleet/equipment
    r"fleet\s+of",
    r"own\s+(?:vehicles|fleet|equipment)",
    r"fully\s+equipped",
    # Credentials
    r"(?:gas\s+safe|niceic|fmb|fensa|trustmark)\s+(?:registered|approved|certified)",
    r"city\s+(?:and|&)\s+guilds",
    r"public\s+liability\s+insur",
    # Service scope
    r"commercial\s+and\s+(?:residential|domestic)",
    r"nationwide\s+(?:service|coverage)",
]

# ═══════════════════════════════════════════════════════════
# Anti-semnale — penalizeaza lead-uri proaste
# ═══════════════════════════════════════════════════════════

_BAD_SIGNALS_RO = [
    r"(?:in|catre|pentru)\s+(?:germania|italia|spania|anglia|uk|olanda|franta|austria)",
    r"loc(?:uri)?\s+de\s+munca",
    r"angajam\s+(?:in|catre)",
    r"salariu\s+(?:de\s+)?(?:\d|atractiv)",
    r"cazare\s+(?:asigurata|inclus)",
    r"plecam\s+(?:in|catre|spre)",
]

_BAD_SIGNALS_EN = [
    r"work\s+abroad",
    r"jobs?\s+in\s+(?:germany|netherlands|spain|italy)",
    r"recruiting\s+for",
    r"hiring\s+for\s+overseas",
]

# ═══════════════════════════════════════════════════════════
# Sezonalitate — boost pentru nise cu cerere mare pe sezon
# CHIAR ACUM (iunie 2026 = vara): air conditioning TOP priority
# ═══════════════════════════════════════════════════════════

_SUMMER_HOT_NICHES = [
    r"aer\s+condi[tţ]ionat",
    r"climatizare",
    r"montaj\s+ac\b",
    r"air\s+conditioning",
    r"\baircon\b",
    r"hvac",
    r"ventila[tţ]ie",
    r"piscin[ae]",
    r"swimming\s+pool",
]

_WINTER_HOT_NICHES = [
    r"central[aă]\s+termic",
    r"centrale?\s+pe\s+gaz",
    r"deszapeziri",
    r"boiler\s+(?:repair|installation)",
    r"heating\s+(?:engineer|service)",
    r"chimney",
]


def _current_season_niches() -> tuple[list[str], list[str]]:
    """Returneaza (boost_active, boost_inactive) bazat pe luna curenta.
    Iun-Aug = vara (AC boost), Dec-Feb = iarna (heating boost),
    in lunile de tranzitie ambele sunt active dar mai slab."""
    month = datetime.now().month
    if month in (6, 7, 8):
        return (_SUMMER_HOT_NICHES, _WINTER_HOT_NICHES)
    if month in (12, 1, 2):
        return (_WINTER_HOT_NICHES, _SUMMER_HOT_NICHES)
    # Tranzitie (mar-mai, sep-nov): boost mai slab pe ambele
    return (_SUMMER_HOT_NICHES + _WINTER_HOT_NICHES, [])


# ═══════════════════════════════════════════════════════════
# Main scoring function
# ═══════════════════════════════════════════════════════════

@dataclass
class ScoringResult:
    score: int
    reasons: list[str]

    def reasons_str(self) -> str:
        """Returneaza motivele concatenate, trunchiat la 500 char DB limit."""
        return " | ".join(self.reasons)[:500]


def score_lead(
    business_name: str,
    niche: str,
    description: str = "",
    language: str = "Romanian",
    is_promoted: bool = False,
    phone_via_api_fallback: bool = False,
) -> ScoringResult:
    """
    Calculeaza scorul 0-15 pentru un lead pe baza semnalelor regex.
    Returneaza ScoringResult cu scorul si motivele.
    """
    score = 0
    reasons: list[str] = []

    # Text complet pentru analiza
    full_text = f"{business_name} {niche} {description}".lower()

    # Alege seturile de pattern pe limba
    if language == "Romanian":
        pro_signals = _PRO_SIGNALS_RO
        bad_signals = _BAD_SIGNALS_RO
    else:  # English (UK)
        pro_signals = _PRO_SIGNALS_EN
        bad_signals = _BAD_SIGNALS_EN

    # ── BOOST SEZONIER (TOP PRIORITY per cererea utilizatorului) ──
    boost_active, boost_inactive = _current_season_niches()
    season_match = False
    for pattern in boost_active:
        if re.search(pattern, full_text, re.IGNORECASE):
            score += 5
            reasons.append(f"sezon-hot:{pattern[:25]}")
            season_match = True
            break

    if not season_match:
        # Boost mai slab pentru nise de sezon opus (cerere reziduala)
        for pattern in boost_inactive:
            if re.search(pattern, full_text, re.IGNORECASE):
                score += 2
                reasons.append(f"sezon-mid:{pattern[:25]}")
                break

    # ── SEMNAL FIRMA PROFESIONISTA ──
    pro_matches = []
    for pattern in pro_signals:
        if re.search(pattern, full_text, re.IGNORECASE):
            pro_matches.append(pattern[:30])

    if pro_matches:
        # +3 pentru primul semnal, +1 pentru fiecare suplimentar (max +5)
        bonus = min(3 + len(pro_matches) - 1, 5)
        score += bonus
        reasons.append(f"pro-signals:{len(pro_matches)}({bonus})")

    # ── PROMOVAT pe platforma ──
    if is_promoted:
        score += 2
        reasons.append("promovat")

    # ── DESCRIERE LUNGA (anunt serios, nu test) ──
    desc_len = len(description.strip())
    if desc_len > 200:
        score += 2
        reasons.append(f"desc-lunga({desc_len})")
    elif desc_len < 50:
        score -= 2
        reasons.append(f"desc-scurta({desc_len})")

    # ── TELEFON DIRECT (nu prin fallback API) ──
    # Telefonul gasit direct in HTML e mai sigur ca cel din API fallback
    if not phone_via_api_fallback:
        score += 2
        reasons.append("phone-direct")

    # ── ANTI-SEMNALE — penalizeaza ──
    for pattern in bad_signals:
        if re.search(pattern, full_text, re.IGNORECASE):
            score -= 3
            reasons.append(f"bad:{pattern[:25]}")
            break  # un singur bad-signal e suficient pentru penalty

    # ── ALL CAPS extreme = anunt spam-like ──
    if business_name == business_name.upper() and len(business_name) > 30:
        score -= 3
        reasons.append("ALL-CAPS")

    # ── Semne multiple !!! sau ??? ──
    if re.search(r"[!?]{3,}", business_name + " " + description):
        score -= 2
        reasons.append("punctuatie-spam")

    return ScoringResult(score=max(0, score), reasons=reasons)


# Pragul de scor pentru deployment automat
# Lead-uri cu scor < THRESHOLD sunt skipped (status SKIP_LOW_SCORE)
SCORE_THRESHOLD_DEPLOY = 7
