"""
email_extractor.py — Extrage email de pe site-ul firmei (UK B2B pipeline)
==========================================================================
Lead-urile din Companies House au doar nume + adresa. NU au telefon/email
(nu exista in registru). Acest modul:

  1. Cauta site-ul web al firmei prin Google search (sau direct prin
     suggested-by-name approach)
  2. Daca gaseste site, viziteaza pagina "Contact" / "About" / homepage
  3. Extrage email-uri B2B (filtreaza emailuri personale, social, etc)
  4. Updateaza Job-ul in DB cu email + company_website

Limitari ONESTE:
  - Multe firme mici n-au site web -> skip
  - Multe site-uri au email-uri obfuscate (img/JS) -> nu prind toate
  - NU folosim Google Search API platit. Cautam direct prin "name + city"
    pe DuckDuckGo HTML (gratuit, fara cheie), apoi vizitam top rezultat.
    Asta inseamna rata de succes 30-50%, nu 100%. E normal.

NU trimite emailuri. Doar populeaza DB cu email pentru pasul urmator
(email_composer.py).
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from urllib.parse import urlparse, quote_plus

import httpx
from bs4 import BeautifulSoup, Tag
from sqlalchemy import select

from database import AsyncSessionLocal, Job, JobStatus, init_db
from http_resilience import CircuitBreaker, fetch_with_retry

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
}

# Regex email — clasic, dar exclude obvious-spam patterns
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# Email-uri pe care le ignoram (placeholders, exemple, sistemice)
_EMAIL_BLACKLIST_PARTS = [
    "example.com", "yourdomain", "domain.com",
    "noreply", "no-reply", "donotreply",
    "wordpress.com", "wixsite.com", "weebly.com",
    "sentry.io", "googleapis", "gstatic", "cloudfront",
    "@2x", "@3x",  # image hash artefacts
]


async def _search_duckduckgo_site(
    client: httpx.AsyncClient,
    company_name: str,
    location: str,
    breaker: CircuitBreaker,
) -> str | None:
    """
    Cauta site-ul firmei pe DuckDuckGo HTML (fara cheie API).
    Returneaza primul URL ne-social care pare a fi site oficial.
    """
    query = quote_plus(f"{company_name} {location}")
    url = f"https://html.duckduckgo.com/html/?q={query}"

    r = await fetch_with_retry(
        client, url, headers=_HEADERS, breaker=breaker, max_retries=2,
    )
    if r is None:
        return None

    soup = BeautifulSoup(r.text, "lxml")

    # DuckDuckGo HTML: rezultate au class="result__url" sau href in <a class="result__a">
    for a in soup.find_all("a", class_="result__a", href=True):
        if not isinstance(a, Tag):
            continue
        href = str(a.get("href", ""))

        # DuckDuckGo wrappuie URL-urile - extragem URL real
        if "uddg=" in href:
            from urllib.parse import unquote, parse_qs
            try:
                params = parse_qs(urlparse(href).query)
                real_url = params.get("uddg", [None])[0]
                if real_url:
                    href = unquote(real_url)
            except Exception:
                continue

        # Filtru: nu vrem social media, directoare, etc.
        domain = urlparse(href).netloc.lower()
        skip_domains = [
            "facebook.com", "linkedin.com", "twitter.com", "x.com",
            "instagram.com", "youtube.com",
            "companieshouse.gov.uk", "endole.co.uk", "find-and-update",
            "yell.com", "checkatrade.com", "trustpilot.com",
            "yelp.com", "google.com", "wikipedia.org",
            "duckduckgo.com",
        ]
        if any(skip in domain for skip in skip_domains):
            continue

        # Are .co.uk sau .uk sau .com - probabil site real
        if not domain:
            continue

        return f"https://{domain}" if not href.startswith("http") else href

    return None


def _extract_emails_from_html(html: str, company_domain: str) -> list[str]:
    """
    Gaseste email-uri in HTML. Prioritizeaza email-uri pe domeniul
    firmei (info@firma.co.uk) vs email-uri generice (gmail).
    """
    found = set()
    for match in _EMAIL_RE.finditer(html):
        email = match.group(0).lower().strip()

        # Skip obvious-junk
        if any(part in email for part in _EMAIL_BLACKLIST_PARTS):
            continue
        if len(email) > 100:  # email-uri lungi suspect = encoding garbage
            continue

        found.add(email)

    if not found:
        return []

    # Sortare: pe domeniul firmei primii, restul dupa
    on_domain = [e for e in found if company_domain in e]
    off_domain = [e for e in found if company_domain not in e]
    return on_domain + off_domain


async def _find_email_for_company(
    client: httpx.AsyncClient,
    company_name: str,
    location: str,
    breaker: CircuitBreaker,
) -> tuple[str | None, str | None]:
    """
    Returneaza (email, website_url) sau (None, None) daca esueaza.
    """
    # Pas 1: gaseste site
    site = await _search_duckduckgo_site(client, company_name, location, breaker)
    if not site:
        return None, None

    domain = urlparse(site).netloc.lower().replace("www.", "")
    logger.info("[email] '%s' -> site candidat: %s", company_name[:40], site)

    # Pas 2: viziteaza homepage
    r = await fetch_with_retry(
        client, site, headers=_HEADERS, breaker=breaker, max_retries=2,
    )
    if r is None:
        return None, site

    emails = _extract_emails_from_html(r.text, domain)

    # Pas 3: daca nu pe homepage, cauta link "contact"
    if not emails:
        soup = BeautifulSoup(r.text, "lxml")
        contact_link = None
        for a in soup.find_all("a", href=True):
            if not isinstance(a, Tag):
                continue
            href = str(a.get("href", ""))
            text = a.get_text(strip=True).lower()
            if "contact" in href.lower() or "contact" in text:
                # Construieste URL absolut
                if href.startswith("http"):
                    contact_link = href
                elif href.startswith("/"):
                    contact_link = site.rstrip("/") + href
                else:
                    contact_link = site.rstrip("/") + "/" + href
                break

        if contact_link:
            cr = await fetch_with_retry(
                client, contact_link, headers=_HEADERS,
                breaker=breaker, max_retries=2,
            )
            if cr is not None:
                emails = _extract_emails_from_html(cr.text, domain)

    if not emails:
        return None, site

    return emails[0], site


async def enrich_b2b_jobs(limit: int = 100) -> None:
    """
    Itereaza prin Job-urile B2B fara email si incearca enrichment.
    Limitat la `limit` per rulare ca sa nu lovesti rate limit-uri.
    """
    await init_db()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Job).where(
                Job.is_b2b.is_(True),
                Job.email.is_(None),
                Job.status == JobStatus.SCRAPED,
            ).limit(limit)
        )
        jobs = result.scalars().all()

    if not jobs:
        logger.info("Niciun Job B2B fara email — nimic de enriched.")
        return

    logger.info("Email enrichment: %d job-uri B2B de procesat", len(jobs))

    breaker = CircuitBreaker("ddg-html", threshold=8, cooldown_sec=300)

    found_count = 0
    async with httpx.AsyncClient(
        http2=False, follow_redirects=True, timeout=20.0,
    ) as client:
        for job in jobs:
            if breaker.is_open():
                logger.warning("Circuit deschis, opresc enrichment")
                break

            # Extrage location din adresa
            location = ""
            desc = job.description or ""
            for city in ["London", "Manchester", "Birmingham", "Glasgow",
                         "Leeds", "Liverpool", "Bristol", "Edinburgh"]:
                if city in desc:
                    location = city
                    break

            email, website = await _find_email_for_company(
                client, job.business_name, location or "UK", breaker,
            )

            # Update DB
            async with AsyncSessionLocal() as session:
                db_job = await session.get(Job, job.id)
                if db_job:
                    if email:
                        db_job.email = email
                        found_count += 1
                    if website:
                        db_job.company_website = website
                    await session.commit()

            if email:
                logger.info("[email] FOUND: %s -> %s", job.business_name[:40], email)
            else:
                logger.info("[email] not found pentru: %s", job.business_name[:40])

            # Rate limit DDG (sunt agresivi cu HTML scraping)
            await asyncio.sleep(3.5)

    logger.info("DONE enrichment. Email gasit pe %d/%d job-uri", found_count, len(jobs))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(enrich_b2b_jobs(limit=100))
