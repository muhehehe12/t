"""
scraper_companies_house.py — UK B2B Lead Source (Companies House API)
==========================================================================
Sursa OFICIALA pentru firme UK active, prin API-ul gratuit Companies House.

Vs. Gumtree (consumer / meseriasi individuali pentru WhatsApp pitch),
acest scraper produce LEAD-URI B2B:
  - Firme inregistrate, active
  - Filtru pe SIC code (industrie) + zona geografica
  - Companies de dimensiune medie-mare (10+ angajati de obicei)

IMPORTANT — limitari oficiale CONFIRMATE prin documentatia API:
  1. API-ul NU returneaza telefoane sau email-uri (nu exista in registru).
     Avem doar: nume firma, adresa, status, SIC, data inregistrarii.
  2. Pentru contact, trebuie pas urmator: vizita site web firma + 
     extragere email din "Contact us" (vezi email_extractor.py).
  3. API-ul are rate limit: 600 cereri / 5 minute pe cheie gratuita
     (~2 cereri/secunda sustinut).

SIC codes pre-configurati: Construction (incepem cu astea, cum ai cerut).
  - 41201 Construction of commercial buildings
  - 41202 Construction of domestic buildings
  - 41100 Development of building projects
  - 43210 Electrical installation
  - 43220 Plumbing, heating, air-conditioning installation
  - 43320 Joinery installation
  - 43999 Other specialised construction activities n.e.c.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import sys
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from config import settings
from database import AsyncSessionLocal, Job, JobStatus, init_db
from http_resilience import CircuitBreaker, fetch_with_retry

logger = logging.getLogger(__name__)

_CH_BASE = "https://api.company-information.service.gov.uk"

# SIC codes pentru CONSTRUCTION (cum ai cerut explicit)
# Verificate live prin documentatia Companies House SIC 2007.
_SIC_CONSTRUCTION = [
    "41100",  # Development of building projects
    "41201",  # Construction of commercial buildings
    "41202",  # Construction of domestic buildings
    "43210",  # Electrical installation
    "43220",  # Plumbing, heating, AC installation
    "43320",  # Joinery installation
    "43999",  # Other specialised construction activities
]

# Top 20 zone UK pentru construction (orase + regiuni cu activitate mare)
# Companies House foloseste 'location' = postcode prefix sau nume oras.
# CONFIRMAT: API-ul accepta "location" ca string in advanced search.
_UK_LOCATIONS = [
    "London", "Manchester", "Birmingham", "Glasgow", "Leeds",
    "Liverpool", "Newcastle", "Sheffield", "Bristol", "Edinburgh",
    "Nottingham", "Cardiff", "Belfast", "Coventry", "Leicester",
    "Sunderland", "Brighton", "Southampton", "Portsmouth", "York",
]


@dataclass
class B2BLead:
    company_name: str
    company_number: str
    sic_code: str
    address: str
    postcode: str
    incorporated_date: str


def _ch_auth_header(api_key: str) -> dict:
    """Companies House API foloseste HTTP Basic Auth cu API key ca user."""
    token = base64.b64encode(f"{api_key}:".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def _search_by_sic_location(
    client: httpx.AsyncClient,
    sic_code: str,
    location: str,
    breaker: CircuitBreaker,
) -> list[B2BLead]:
    """
    Cauta firme active intr-un SIC code + locatie folosind
    Advanced Search API endpoint.
    """
    leads: list[B2BLead] = []
    api_key = settings.companies_house_api_key

    if not api_key:
        logger.error("COMPANIES_HOUSE_API_KEY lipseste — adauga in .env")
        return []

    # Endpoint Advanced Search:
    # /advanced-search/companies?sic_codes={code}&location={loc}&company_status=active
    url = f"{_CH_BASE}/advanced-search/companies"
    params = {
        "sic_codes": sic_code,
        "location": location,
        "company_status": "active",
        "size": "50",   # 50 rezultate per pagina
        "start_index": "0",
    }

    r = await fetch_with_retry(
        client, url,
        headers=_ch_auth_header(api_key),
        params=params,
        breaker=breaker,
        max_retries=3,
    )
    if r is None:
        return []

    try:
        data = r.json()
    except Exception as exc:
        logger.error("JSON parse error CH API: %s", exc)
        return []

    items = data.get("items", [])
    logger.info("[CH] SIC=%s location=%s -> %d firme", sic_code, location, len(items))

    for item in items:
        name = item.get("company_name", "").strip()
        crn = item.get("company_number", "").strip()
        if not name or not crn:
            continue

        addr_obj = item.get("registered_office_address", {}) or {}
        addr_parts = [
            addr_obj.get("address_line_1"),
            addr_obj.get("address_line_2"),
            addr_obj.get("locality"),
            addr_obj.get("region"),
        ]
        addr = ", ".join(p for p in addr_parts if p)
        postcode = addr_obj.get("postal_code", "")

        leads.append(B2BLead(
            company_name=name,
            company_number=crn,
            sic_code=sic_code,
            address=addr,
            postcode=postcode,
            incorporated_date=item.get("date_of_creation", ""),
        ))

    return leads


async def _insert_b2b(leads: list[B2BLead]) -> int:
    """Insereaza ca Jobs cu is_b2b=True. Phone e placeholder."""
    n = 0
    async with AsyncSessionLocal() as session:
        for lead in leads:
            # Check daca firma e deja in DB (dupa company_number, nu phone)
            exists = await session.scalar(
                select(Job).where(Job.company_number == lead.company_number)
            )
            if exists:
                continue

            # Phone e placeholder unic pe baza CRN (constraint UNIQUE)
            placeholder_phone = f"+44000{lead.company_number}"

            session.add(Job(
                business_name=lead.company_name,
                phone_number=placeholder_phone,
                niche=f"Construction (SIC {lead.sic_code})",
                description=f"UK Companies House lead. Inregistrata {lead.incorporated_date}. "
                            f"Adresa: {lead.address}",
                language="English",
                company_number=lead.company_number,
                is_b2b=True,
                status=JobStatus.SCRAPED,
            ))
            n += 1
        await session.commit()
    return n


async def run_scraper_companies_house(total: int = 200) -> None:
    await init_db()

    if not settings.companies_house_api_key:
        logger.error(
            "COMPANIES_HOUSE_API_KEY lipseste — obtine cheie gratuita "
            "de pe https://developer.company-information.service.gov.uk/ "
            "si adauga in .env"
        )
        return

    logger.info(
        "Companies House scraper: %d SIC codes x %d locatii (construction UK)",
        len(_SIC_CONSTRUCTION), len(_UK_LOCATIONS),
    )

    grand = 0
    breaker = CircuitBreaker("companies-house", threshold=5, cooldown_sec=300)

    async with httpx.AsyncClient(
        http2=False, follow_redirects=True, timeout=30.0,
    ) as client:
        for sic in _SIC_CONSTRUCTION:
            for location in _UK_LOCATIONS:
                if breaker.is_open():
                    logger.warning("CH: circuit deschis, opresc")
                    break

                leads = await _search_by_sic_location(
                    client, sic, location, breaker,
                )
                inserted = await _insert_b2b(leads)
                grand += inserted

                if inserted > 0:
                    logger.info(
                        "CH SIC=%s loc=%s: gasit=%d inserat=%d (total=%d)",
                        sic, location, len(leads), inserted, grand,
                    )

                # Rate limit: 600 req / 5 min = max 2/sec
                # Pauza 0.6s = ~1.6 req/sec, sustenabil
                await asyncio.sleep(0.6)

                if grand >= total:
                    logger.info("Target %d atins, opresc", total)
                    break
            if grand >= total or breaker.is_open():
                break

    logger.info("DONE Companies House. Total lead-uri B2B noi: %d", grand)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(run_scraper_companies_house(total=200))
