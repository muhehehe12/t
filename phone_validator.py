"""
phone_validator.py — Validare STRICTA a telefoanelor extrase
==============================================================
Logica defensiva pentru a evita telefoane gresite/random in DB.
Telefoane invalide (anunturi cu coduri postale, preturi, ID-uri
de produs confundate cu numere de telefon) creeaza:
  1. Consum inutil Groq (genereaza site pentru numar fals)
  2. Deploy GitHub Pages cu site care nu duce nicaieri
  3. Numere care nu vor primi WhatsApp -> "delivered" gresit

VALIDARI APLICATE:
  - Format exact (lungime, prefix tara, prefix mobil)
  - Lista prefixe operatori REALI per tara (date stabile, verificabile
    public la ANCOM Romania / Ofcom UK)
  - Excludem secvente "obviously fake" (toate aceleasi cifre, etc)
"""
from __future__ import annotations

import re


# ═══════════════════════════════════════════════════════════
# ROMANIA — numere mobile +40 7XX XXX XXX
# Prefixele dupa "+40 7" sunt repartizate pe operatori (ANCOM):
#   Vodafone:  72, 73
#   Orange:    74, 75
#   Telekom:   76
#   Digi:      77
#   RCS-RDS:   78
# Telefoanele incep cu 07 in Romania, dar in format +40 e:
#   +40 7XX = numar mobil valid
# Format complet: +40 + 9 cifre (incepe cu 7) = 11 cifre total
# ═══════════════════════════════════════════════════════════

_RO_VALID_MOBILE_PREFIXES = {
    "72", "73",      # Vodafone
    "74", "75",      # Orange
    "76",            # Telekom
    "77",            # Digi
    "78",            # RCS-RDS / alte MVNOs
    "70", "71", "79" # rezervate, dar uneori folosite — acceptam defensiv
}


def is_valid_ro_mobile(phone: str) -> bool:
    """
    True doar daca telefonul e STRICT un mobil RO valid.
    Refuza:
      - Numere fixe (021..., 023..., 026...)
      - Numere prea scurte/lungi
      - Numere cu prefixe necunoscute
      - Secvente "obvious fake" (toate cifrele identice)
    """
    digits = re.sub(r"[^\d]", "", phone)

    # Trebuie sa fie exact 11 cifre incepand cu 40 si urmate de 7
    if len(digits) != 11:
        return False
    if not digits.startswith("407"):
        return False

    # Cifrele 4-5 (dupa "+40 7") trebuie sa fie prefix de operator real
    prefix = digits[2:4]  # ex: "73" pentru "+40 73x xxx xxx"
    if prefix not in _RO_VALID_MOBILE_PREFIXES:
        return False

    # Refuza numere "obvious fake":
    # Toate cifrele dupa prefix sunt aceleasi (ex: +40 723 333 333)
    rest = digits[4:]  # 7 cifre dupa "+40 7XX"
    if len(set(rest)) == 1:
        return False

    # Secvente prea simple (123456789, 987654321)
    if rest in ("1234567", "7654321", "0000000", "1111111"):
        return False

    return True


def normalize_ro_phone(raw: str) -> str | None:
    """
    Incearca sa normalizeze un sir cu telefon la format +40XXXXXXXXX.
    Returneaza None daca nu e mobil RO valid dupa normalizare.
    """
    if not raw:
        return None

    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None

    # Daca incepe cu 0, scoatem 0-ul initial (format national RO: 0723...)
    if digits.startswith("0"):
        digits = digits[1:]

    # Daca acum incepe cu 40, adaugam doar "+"
    # Daca incepe cu 7 (fara 40), adaugam "40"
    if digits.startswith("40"):
        normalized = "+" + digits
    elif digits.startswith("7") and len(digits) == 9:
        normalized = "+40" + digits
    else:
        return None  # format nerecunoscut

    if is_valid_ro_mobile(normalized):
        return normalized
    return None


# ═══════════════════════════════════════════════════════════
# UK — numere mobile +44 7XXX XXX XXX
# Format complet: +44 + 10 cifre (incepe cu 7) = 12 cifre total
# Numerele fixe UK incep cu alte cifre (1, 2, 3) si NU le acceptam
# pentru ca nu pot primi WhatsApp.
# Ofcom: orice numar 7 incepe cu 7 e mobil, cu rare exceptii (74XX,
# 76XX, 77XX, 78XX, 79XX sunt mobile principale).
# ═══════════════════════════════════════════════════════════

_UK_VALID_MOBILE_FIRST_DIGIT = "7"  # toate mobile UK incep cu 7 dupa prefix


def is_valid_uk_mobile(phone: str) -> bool:
    """True doar daca e mobil UK valid (+44 7XXX XXX XXX)."""
    digits = re.sub(r"[^\d]", "", phone)

    if len(digits) != 12:
        return False
    if not digits.startswith("44"):
        return False
    if digits[2] != _UK_VALID_MOBILE_FIRST_DIGIT:
        return False

    # Verifica zona mobile (nu numar special 7000, premium 70-71 etc)
    # Zona mobile reala UK: 7400-7999 (in format scurt)
    zone = digits[3:4]  # cifra 4 din "+44 7X..."
    if zone not in ("4", "5", "6", "7", "8", "9"):
        return False

    # Refuza obvious fake
    rest = digits[3:]  # 9 cifre dupa "+44 7"
    if len(set(rest)) == 1:
        return False
    if rest in ("123456789", "987654321", "000000000"):
        return False

    return True


def normalize_uk_phone(raw: str) -> str | None:
    """Normalizeaza la +44XXXXXXXXXX sau None daca invalid."""
    if not raw:
        return None

    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None

    # 0 initial = format national UK (07XXX...)
    if digits.startswith("0"):
        digits = digits[1:]

    if digits.startswith("44"):
        normalized = "+" + digits
    elif digits.startswith("7") and len(digits) == 10:
        normalized = "+44" + digits
    else:
        return None

    if is_valid_uk_mobile(normalized):
        return normalized
    return None
