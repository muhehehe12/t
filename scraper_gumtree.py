"""
scraper_gumtree.py — Gumtree.com UK Lead Scraper
=====================================================
Singura sursa UK CONFIRMATA live cu:
  - meseriasi reali postand anunturi (nu doar marketplace produse)
  - telefoane vizibile in anunturi (cel putin in unele)

ATENTIE — RISCURI REALE confirmate prin cautare:
  1. Gumtree filtreaza activ unele telefoane si le inlocuieste cu
     "[Phone number removed]" — rata de extractie va fi MAI MICA
     decat pe OLX/Publi24. Asta nu e bug, e protectie anti-scraping.
  2. UK are penetrare WhatsApp de doar ~48% (mult sub Europa Est),
     deci canalul tau principal e structural mai slab pe aceasta
     piata. Lead-urile gasite vor avea rata de raspuns mai mica.

URL-uri CONFIRMATE prin cautare live:
  /business-services/  (categoria principala servicii)
  /business-services/london/, /business-services/manchester/ etc
  /uk/london, /uk/manchester (pagini oras)
  Format URL anunt: /p/[slug]/[id] sau /[category]/[city]/...
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup, Tag
from sqlalchemy import select

from database import AsyncSessionLocal, Job, JobStatus, init_db
from http_resilience import CircuitBreaker, fetch_with_retry

logger = logging.getLogger(__name__)

_GUMTREE_BASE = "https://www.gumtree.com"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}

# Telefoane UK: format +44 7xxx xxx xxx (mobile) sau 020 xxxx xxxx (London landline)
# Acceptam DOAR mobile pt outreach WhatsApp (numere fixe = nu raspund pe WA)
_PHONE_UK_MOBILE_RE = re.compile(
    r"(?:\+?44[\s\-]?|0)"
    r"7\d{3}"
    r"[\s\-]?\d{3}"
    r"[\s\-]?\d{3}"
)

# Detecteaza tag-ul de cenzura Gumtree
_PHONE_REMOVED_RE = re.compile(r"\[Phone\s+number\s+removed\]", re.IGNORECASE)


def _find_phone_uk(text: str) -> str | None:
    """
    Cauta nr UK mobile valid in text. Foloseste validatorul strict
    din phone_validator.py — refuza numere fake, prefixe necunoscute,
    numere fixe (care nu pot primi WhatsApp).
    """
    from phone_validator import normalize_uk_phone
    # Daca textul contine marcaj de cenzura Gumtree, e probabil ca
    # telefonul real e ascuns — totusi cautam, uneori e vizibil partial
    for m in _PHONE_UK_MOBILE_RE.finditer(text):
        candidate = normalize_uk_phone(m.group(0))
        if candidate:
            return candidate
    return None


@dataclass
class Lead:
    business_name: str
    phone_number: str
    niche: str
    url: str
    description: str = ""
    is_promoted: bool = False


# ================================================================
# 20 cele mai mari orase UK
# Pattern URL CONFIRMAT prin cautare: /business-services/{city-slug}/
# Slug-uri folosesc lowercase + hyphen (manchester, london, leeds, etc)
# ================================================================

_CITIES_UK: list[str] = [
    "london",
    "manchester",
    "birmingham",
    "glasgow",
    "leeds",
    "liverpool",
    "newcastle",
    "sheffield",
    "bristol",
    "edinburgh",
    "nottingham",
    "cardiff",
    "belfast",
    "coventry",
    "leicester",
    "sunderland",
    "brighton",
    "southampton",
    "portsmouth",
    "york",
]


# ================================================================
# 30 nise — meserii populare in UK
# CONFIRMAT: categoria /business-services/ contine sub-categorii
# precum: clothing-services, building-services, cleaning-services
# Pentru cautari directe folosim slug-ul subcategoriei + q={termen}
# ================================================================

_NICHES_UK: list[tuple[str, list[str]]] = [
    ("Painters and decorators",   ["painter", "decorator"]),
    ("Plumbers",                  ["plumber", "boiler repair"]),
    ("Electricians",              ["electrician", "electrical services"]),
    ("Builders",                  ["builder", "building services"]),
    ("Cleaners",                  ["cleaner", "cleaning services"]),
    ("Gardeners",                 ["gardener", "garden services"]),
    ("Movers and removals",       ["removals", "man and van"]),
    ("Carpenters",                ["carpenter", "joiner"]),
    ("Tilers",                    ["tiler", "tiling"]),
    ("Plasterers",                ["plasterer", "plastering"]),
    ("Roofers",                   ["roofer", "roofing"]),
    ("Locksmiths",                ["locksmith"]),
    ("Handymen",                  ["handyman", "odd jobs"]),
    ("Appliance repair",          ["appliance repair", "washing machine repair"]),
    ("Heating engineers",         ["gas engineer", "heating engineer"]),
    ("Car mechanics",             ["mobile mechanic", "car repair"]),
    ("Demolition",                ["demolition", "site clearance"]),
    ("Photographers",             ["wedding photographer", "event photographer"]),
    ("Caterers",                  ["catering", "wedding catering"]),
    ("Drain unblocking",          ["drain unblocking", "drainage"]),
    ("Solar panel installers",    ["solar panel installation"]),
    ("Beauticians",               ["mobile beautician", "mobile nails"]),
    ("Massage therapists",        ["mobile massage", "sports massage"]),
    ("Tutors",                    ["maths tutor", "english tutor"]),
    ("Phone repair",              ["phone repair", "iphone repair"]),
    ("Hairdressers",              ["mobile hairdresser"]),
    ("Pet services",              ["dog walker", "mobile dog grooming"]),
    ("Carpet cleaning",           ["carpet cleaning"]),
    ("IT repair",                 ["computer repair", "laptop repair"]),
    ("Pest control",              ["pest control"]),
]


# ================================================================
# Parse listing URLs din pagina de search
# ================================================================

# Anunturile Gumtree au URL care contine /p/ urmat de un slug si ID:
# Ex: /p/painters/painter-decorator-london/12345
_LISTING_RE = re.compile(r"^/p/[a-z0-9\-_/]+/\d+/?$", re.IGNORECASE)


def _get_listing_urls(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    results: list[str] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        if not isinstance(a, Tag):
            continue
        href = str(a.get("href", "")).split("?")[0].split("#")[0]
        if not href.startswith("/"):
            continue
        if not _LISTING_RE.match(href):
            continue
        url = _GUMTREE_BASE + href
        if url in seen:
            continue
        seen.add(url)
        results.append(url)

    return results[:25]


# ================================================================
# Parse detail page
# ================================================================

def _parse_detail(html: str) -> tuple[str, str | None, str, bool]:
    """Returneaza (title, phone_or_None, description, is_promoted)."""
    soup = BeautifulSoup(html, "lxml")

    title = "Local Tradesperson"
    h1 = soup.find("h1")
    if h1 and isinstance(h1, Tag):
        t = h1.get_text(strip=True)
        if t and len(t) > 3:
            title = t

    if title == "Local Tradesperson":
        og = soup.find("meta", {"property": "og:title"})
        if og and isinstance(og, Tag):
            c = og.get("content", "")
            if c:
                title = str(c).strip()

    # Telefon — tel: link sau scan in text
    phone: str | None = None
    for a in soup.find_all("a", href=True):
        href = str(a.get("href", ""))
        if href.startswith("tel:"):
            p = _find_phone_uk(href.replace("tel:", ""))
            if p:
                phone = p
                break

    if not phone:
        # Cauta in textul complet — pe Gumtree, in anunturile cu telefon
        # vizibil, apare in descrierea anuntului
        phone = _find_phone_uk(soup.get_text(" ", strip=True))

    # Descriere — cauta zone tipice
    description = ""
    for cls_kw in ["description", "ad-description", "listing-description"]:
        el = soup.find(attrs={"class": lambda c: c and any(
            cls_kw in cl.lower() for cl in c)})
        if el and isinstance(el, Tag):
            t = el.get_text(" ", strip=True)
            if t and len(t) > 20:
                description = t
                break

    if not description:
        description = soup.get_text(" ", strip=True)[:1500]

    # Promoted detection — Gumtree marcheaza anunturile "Featured"
    is_promoted = bool(
        re.search(r"\b(featured|urgent|top|sponsored)\b", html, re.IGNORECASE)
    )

    return title, phone, description.strip()[:1500], is_promoted


# ================================================================
# Scrape o nisa pe toate orasele — DOAR anunturile promovate
# ================================================================

async def _scrape_niche_uk(
    client: httpx.AsyncClient,
    niche: str,
    terms: list[str],
    seen_phones: set[str],
    breaker: CircuitBreaker,
) -> list[Lead]:
    """Cauta DOAR anunturi promovate (Featured/Top) in 20 orase."""
    leads: list[Lead] = []

    for city in _CITIES_UK:
        if breaker.is_open():
            logger.warning("[UK:%s] circuit deschis", niche)
            break

        for term in terms:
            term_slug = term.replace(" ", "+")
            # URL cautare: /search?search_category=services&search_location={city}&q={term}
            url = (f"{_GUMTREE_BASE}/business-services/{city}/"
                   f"srpsearch+{term_slug}")

            r = await fetch_with_retry(
                client, url, headers=_HEADERS,
                breaker=breaker, max_retries=3,
            )
            if r is None:
                continue

            listing_urls = _get_listing_urls(r.text)
            if not listing_urls:
                await asyncio.sleep(1.5)
                continue

            logger.info(
                "[UK:%s] oras='%s' term='%s' -> %d URL-uri",
                niche, city, term, len(listing_urls),
            )

            for listing_url in listing_urls:
                dr = await fetch_with_retry(
                    client, listing_url, headers=_HEADERS,
                    breaker=breaker, max_retries=2,
                )
                if dr is None:
                    continue

                title, phone, description, is_promoted = _parse_detail(dr.text)

                # FILTRARE STRICTA: doar anunturi promovate
                if not is_promoted:
                    continue

                if not phone or phone in seen_phones:
                    continue

                seen_phones.add(phone)
                leads.append(Lead(
                    business_name=title,
                    phone_number=phone,
                    niche=niche,
                    url=listing_url,
                    description=description,
                    is_promoted=True,
                ))
                logger.info(
                    "LEAD_UK_TOP [%s] oras='%s': '%s' -> %s",
                    niche[:20], city, title[:40], phone,
                )

                await asyncio.sleep(1.8)

            await asyncio.sleep(2.0)

    return leads


# ================================================================
# Insert in DB
# ================================================================

async def _insert_uk(leads: list[Lead]) -> int:
    n = 0
    async with AsyncSessionLocal() as session:
        for lead in leads:
            exists = await session.scalar(
                select(Job).where(Job.phone_number == lead.phone_number)
            )
            if exists:
                continue
            session.add(Job(
                business_name=lead.business_name,
                phone_number=lead.phone_number,
                niche=lead.niche,
                description=lead.description,
                language="English",
                status=JobStatus.SCRAPED,
            ))
            n += 1
        await session.commit()
    return n


# ================================================================
# Main
# ================================================================

async def run_scraper_uk(total: int = 200) -> None:
    await init_db()
    logger.info("Gumtree UK scraper: %d nise x %d orase, DOAR promovate",
                len(_NICHES_UK), len(_CITIES_UK))

    grand = 0
    seen_phones: set[str] = set()
    breaker = CircuitBreaker("gumtree.com", threshold=5, cooldown_sec=300)

    async with httpx.AsyncClient(
        http2=False, follow_redirects=True, timeout=30.0,
    ) as client:
        for niche, terms in _NICHES_UK:
            logger.info("=== GUMTREE UK: %s (DOAR promovate) ===", niche)
            leads = await _scrape_niche_uk(
                client, niche, terms, seen_phones, breaker,
            )

            local_seen: set[str] = set()
            unique = [l for l in leads
                      if not (l.phone_number in local_seen
                              or local_seen.add(l.phone_number))]  # type: ignore

            inserted = await _insert_uk(unique)
            grand += inserted
            logger.info("GUMTREE UK %s [PROMOVATE]: gasit=%d inserat=%d",
                        niche, len(unique), inserted)

            if breaker.is_open():
                logger.warning("Gumtree UK: circuit deschis, opresc devreme")
                break

            await asyncio.sleep(4.0)

    logger.info("DONE GUMTREE UK. Total lead-uri noi: %d", grand)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(run_scraper_uk(total=200))
