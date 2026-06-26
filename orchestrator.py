
"""
Hybrid King — Orchestrator v4
==============================
Production-grade automated pipeline.

Loop A — Deployer (runs continuously):
  SCRAPED -> quality filter -> Groq generates site
          -> Vercel direct upload -> DEPLOYED
  Rate limit: 15s between generations (avoids Groq 429)

Loop B — WhatsApp Outreach (anti-ban optimized):
  DEPLOYED -> mobile check -> business hours check
           -> typing simulation (6-10s) -> human-like message
           -> OUTREACH_SENT -> random delay 4-8 min
  Hard cap: 35 messages/day (conservative for anti-ban)
  Hours: 08:00-19:00 Czech time only

Anti-ban measures:
  - 35/day hard limit (below WhatsApp detection threshold)
  - 4-8 minute random gaps (never uniform)
  - Business hours only (08:00-19:00 CET)
  - Typing simulation 6-10 seconds
  - 5 randomized message variants (never same text)
  - Mobile-only filter (skip landlines)
  - Lead quality filter (skip job ads, product sales)
  - Handles WAHA 503 (auto-restart) gracefully

Pricing: 199 EUR one-time (4990 CZK)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
import urllib.parse
from datetime import date, datetime, time as dtime, timezone, timedelta
from textwrap import dedent

import httpx
from sqlalchemy import func, select, update

from config import settings
from database import AsyncSessionLocal, Job, JobStatus, init_db
from factory import generate_website
from github_deploy import deploy_to_github_pages

# ═══════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════

logger = logging.getLogger("hybridking")

_GH_PAGES_NOTE = (
    "Vercel a fost inlocuit complet cu GitHub Pages — "
    "vezi github_deploy.py"
)


# ═══════════════════════════════════════════════════════════
# Lead quality filter
# Filters out job ads, product sales, recruitment agencies
# ═══════════════════════════════════════════════════════════

_SKIP_UPPER = [
    # German/foreign job offers
    "NĚMECKO", "NEMECKO", "RAKOUS", "POLSK", "SLOVINSK",
    "MNICHOV", "DÜSSELDORF", "BERLIN", "HAMBURG",
    "SACHSEN", "BAYERN", "DEUTSCHLAND",
    # Recruitment phrases
    "HLEDÁME PARTU", "HLEDAME PARTU",
    "HLEDÁME PRACOVNÍK", "HLEDAME PRACOVNIK",
    "PRACOVNÍ NABÍDKA", "PRACOVNI NABIDKA",
    "BRIGÁD", "BRIGAD",
    "NÁSTUP IHNED", "NASTUP IHNED",
    "NABÍDKA PRÁCE", "NABIDKA PRACE",
    # Wages / job offers
    "EUR/HOD", "EURO/HOD", "KČ/HOD", "KC/HOD",
    "OD 27€", "OD 22€", "OD 24€", "OD 96 000",
    "OD 102", "PLAT OD",
    # Product sales (not services)
    "HILTI", "ROTHENBERGER", "MAKITA", "BOSCH ",
    "DEWALT", "PARKSIDE", "STIHL",
    "PRODÁM", "PRODAM", "PRODEJ ",
    "KOUPÍM", "KOUPIM",
    "V ZÁRUCE", "V ZARUCE", "ZÁNOVNÍ", "ZANOVNI",
    "KORUNKY", "PŘÍSLUŠENSTV",
    # Agencies / guarantors
    "ODPOVĚDNÝ ZÁSTUPCE", "ODPOVEDNY ZASTUPCE",
    "GARANT ŽIVNOST", "GARANT ZIVNOST",
    "PERSONÁLNÍ AGENTUR", "PERSONALNI AGENTUR",
    "PRACOVNÍ AGENTUR", "PRACOVNI AGENTUR",
    # Buyers / seekers
    "HLEDÁM KE KOUPI", "HLEDAM KE KOUPI",
    "SPOLUPRÁCE PRO FIRMY",
    # Shop workers
    "PRODAVAČ", "PRODAVAC", "PRODAVAČK", "PRODAVACK",
    "V OBCHODĚ", "V OBCHODE",
]


def _is_quality_lead(name: str) -> bool:
    """True if the listing looks like a real local tradesperson.
    Filtru AGRESIV pentru a evita consum inutil de Groq pe lead-uri
    care nu sunt clienti reali (anunturi joburi, internationale, spam)."""
    upper = name.upper()

    for phrase in _SKIP_UPPER:
        if phrase in upper:
            return False

    # Skip if contains emoji arrows/warnings (usually job ads)
    if any(ch in name for ch in ['➡', '⚡', '⚠', '🇦🇹', '🇩🇪', '🇮🇹', '🇪🇸', '🇬🇧', '👨', '👷', '🔥', '💯']):
        return False

    # Skip ALL CAPS titles longer than 50 chars (usually spam/job ads)
    if name == name.upper() and len(name) > 50:
        return False

    # ── Filtre suplimentare RO: anunturi job-uri in strainatate ──
    # Tradespeople RO autentici NU contin aceste cuvinte in titlu.
    market = os.getenv("MARKET", "cz").lower()
    if market == "ro":
        lower = name.lower()

        # Joburi in strainatate (anunturi de recrutare, nu meseriasi locali)
        foreign_keywords = [
            "germania", "italia", "spania", "anglia", "uk", "olanda",
            "franta", "belgia", "austria", "norvegia", "irlanda",
            "germany", "italy", "spain", "england", "france",
            "abroad", "strainatate", "in strainatate",
            "angajam", "angajez", "caut soferi", "caut muncitor",
            "loc de munca", "locuri de munca", "recrutare",
            "salariu", "salar de", "cazare asigurata",
        ]
        for kw in foreign_keywords:
            if kw in lower:
                return False

        # Romgleza / titluri lipsite de sens (probabil traduceri auto sau spam)
        if len(re.findall(r"[a-z]", name)) < 3:  # mai putin de 3 litere = clar spam
            return False

        # Titluri prea scurte (sub 5 caractere) sau prea lungi (peste 100)
        if len(name.strip()) < 5 or len(name.strip()) > 100:
            return False

        # Anunturi care contin doar majuscule + cifre + semne (gen "OFERTA!!! 1000 EUR")
        if re.match(r"^[A-Z0-9\s\!\?\.,\-_]+$", name) and len(name) > 20:
            return False

        # Mai mult de 3 semne de exclamare/intrebare consecutive
        if re.search(r"[!?]{3,}", name):
            return False

        # Cuvinte cheie spam comercial (anunturi de vanzari directe, nu servicii)
        spam_keywords = [
            "vand urgent", "vand ieftin", "vand stoc",
            "oferim credite", "credite rapide", "imprumut rapid",
            "loterie", "castig", "ai castigat",
            "click aici", "vezi aici",
        ]
        for kw in spam_keywords:
            if kw in lower:
                return False

    return True


# ═══════════════════════════════════════════════════════════
# Mobile number check — market-aware (RO sau UK)
# Set env var MARKET=ro|uk inainte de pornire
# ═══════════════════════════════════════════════════════════

def _is_mobile(phone: str) -> bool:
    """Verifica daca numarul e mobil, in functie de piata activa."""
    digits = re.sub(r"[^\d]", "", phone)
    market = os.getenv("MARKET", "ro").lower()
    if market == "uk":
        # UK mobile: +44 7XXX XXX XXX  (12 cifre: 44 + 10)
        return (
            len(digits) == 12
            and digits.startswith("44")
            and digits[2] == "7"
        )
    # Default: Romania +40 7xx xxx xxx  (11 cifre: 40 + 9)
    return (
        len(digits) == 11
        and digits.startswith("40")
        and digits[2] == "7"
    )


# Alias backward-compat (folosit intern)
_is_czech_mobile = _is_mobile  # numele e legacy, dar functia detecteaza piata curenta


# ═══════════════════════════════════════════════════════════
# Business hours (08:00-19:00 Czech time)
# ═══════════════════════════════════════════════════════════

def _czech_hour() -> int:
    """Current hour in Czech Republic (UTC+2 summer / UTC+1 winter)."""
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    offset = 2 if 3 <= month <= 10 else 1  # simplified DST
    return (utc_now + timedelta(hours=offset)).hour


def _is_business_hours() -> bool:
    h = _czech_hour()
    return 8 <= h < 19


# ═══════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════

def _slugify(text: str, max_len: int = 36) -> str:
    text = text.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^\w\-]", "-", text.lower().strip())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:max_len] or "site"


# Vercel a fost inlocuit complet cu GitHub Pages.
# Vezi github_deploy.py: deploy_to_github_pages()


# ═══════════════════════════════════════════════════════════
# Loop A — Deployer
# ═══════════════════════════════════════════════════════════

async def loop_deployer() -> None:
    logger.info("═══ DEPLOYER LOOP STARTED ═══")

    async with httpx.AsyncClient(http2=False, timeout=60.0) as http:
        while True:
            try:
                # Fetch up to 3 SCRAPED jobs
                async with AsyncSessionLocal() as session:
                    result = await session.execute(
                        select(Job)
                        .where(Job.status == JobStatus.SCRAPED)
                        .limit(3)
                        .with_for_update(skip_locked=True)
                    )
                    jobs = result.scalars().all()

                if not jobs:
                    logger.debug("No SCRAPED jobs. Sleeping 90s.")
                    await asyncio.sleep(90)
                    continue

                for job in jobs:
                    # ── Quality filter ─────────────────────────
                    if not _is_quality_lead(job.business_name):
                        logger.info(
                            "[Job %d] SKIP (bad lead): '%s'",
                            job.id, job.business_name[:60],
                        )
                        async with AsyncSessionLocal() as session:
                            await session.execute(
                                update(Job).where(Job.id == job.id)
                                .values(status=JobStatus.FAILED)
                            )
                            await session.commit()
                        continue

                    # ── Lead scoring (filtru premium) ──────────
                    # Calculeaza scor 0-15 pe semnale calitate firma.
                    # Lead-uri cu scor sub THRESHOLD sunt FAILED — nu
                    # consum Groq pe ele. Skipam B2B (au logica separata).
                    if not job.is_b2b:
                        from lead_scoring import score_lead, SCORE_THRESHOLD_DEPLOY
                        result = score_lead(
                            business_name=job.business_name,
                            niche=job.niche or "",
                            description=job.description or "",
                            language=job.language,
                        )

                        # Salveaza scorul in DB indiferent de prag
                        async with AsyncSessionLocal() as session:
                            await session.execute(
                                update(Job).where(Job.id == job.id)
                                .values(
                                    lead_score=result.score,
                                    score_reasons=result.reasons_str(),
                                )
                            )
                            await session.commit()

                        if result.score < SCORE_THRESHOLD_DEPLOY:
                            logger.info(
                                "[Job %d] SKIP (low score %d/%d): '%s' [%s]",
                                job.id, result.score, SCORE_THRESHOLD_DEPLOY,
                                job.business_name[:40],
                                ", ".join(result.reasons[:3]),
                            )
                            async with AsyncSessionLocal() as session:
                                await session.execute(
                                    update(Job).where(Job.id == job.id)
                                    .values(status=JobStatus.FAILED)
                                )
                                await session.commit()
                            continue
                        else:
                            logger.info(
                                "[Job %d] SCORE OK (%d/%d): '%s' [%s]",
                                job.id, result.score, SCORE_THRESHOLD_DEPLOY,
                                job.business_name[:40],
                                ", ".join(result.reasons[:3]),
                            )

                    # ── Mobile filter ──────────────────────────
                    if not _is_czech_mobile(job.phone_number):
                        logger.info(
                            "[Job %d] SKIP (landline): %s",
                            job.id, job.phone_number,
                        )
                        async with AsyncSessionLocal() as session:
                            await session.execute(
                                update(Job).where(Job.id == job.id)
                                .values(status=JobStatus.FAILED)
                            )
                            await session.commit()
                        continue

                    # ── Mark GENERATING ────────────────────────
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            update(Job).where(Job.id == job.id)
                            .values(status=JobStatus.GENERATING)
                        )
                        await session.commit()

                    try:
                        logger.info(
                            "[Job %d] Generating site: '%s' (%s)...",
                            job.id, job.business_name[:50], job.language,
                        )

                        site = await generate_website(
                            business_name=job.business_name,
                            niche=job.niche,
                            language=job.language,
                            phone=job.phone_number,
                            description=job.description or "",
                            job_id=job.id,
                            chatbot_enabled=job.chatbot_enabled,
                        )

                        market = os.getenv("MARKET", "cz").lower()

                        url, repo_name = await deploy_to_github_pages(
                            http,
                            business_name=job.business_name,
                            job_id=job.id,
                            market=market,
                            index_html=site.index_html,
                        )

                        async with AsyncSessionLocal() as session:
                            await session.execute(
                                update(Job).where(Job.id == job.id)
                                .values(
                                    status=JobStatus.DEPLOYED,
                                    vercel_url=url,       # acum contine URL-ul GitHub Pages
                                    github_repo=repo_name,
                                )
                            )
                            await session.commit()

                        logger.info(
                            "[Job %d] ✓ DEPLOYED -> %s (repo=%s)",
                            job.id, url, repo_name,
                        )

                    except Exception as exc:
                        err = str(exc)
                        logger.error(
                            "[Job %d] ✗ Deploy failed: %s",
                            job.id, err[:150],
                        )

                        # 429 = rate limit -> revert to SCRAPED for retry
                        new_status = (
                            JobStatus.SCRAPED if "429" in err
                            else JobStatus.FAILED
                        )
                        async with AsyncSessionLocal() as session:
                            await session.execute(
                                update(Job).where(Job.id == job.id)
                                .values(status=new_status)
                            )
                            await session.commit()

                        if "429" in err:
                            logger.info("  Groq rate limit — cooling 70s...")
                            await asyncio.sleep(70)

                    # 15s between each generation (prevents Groq 429)
                    await asyncio.sleep(15)

            except Exception as exc:
                logger.error("Deployer crash: %s", exc, exc_info=True)
                await asyncio.sleep(30)


# ═══════════════════════════════════════════════════════════
# WhatsApp — message builder
# 5 human-sounding variants, randomly picked each time
# Pricing: 4990 CZK / 199 EUR one-time
# ═══════════════════════════════════════════════════════════

def _build_message(job: Job) -> str:
    """Construieste mesaj pitch adaptat pietei (RO / UK). Varianta random."""
    name = job.business_name
    url  = job.vercel_url
    market = os.getenv("MARKET", "ro").lower()

    if market == "ro":
        variants = [
            # RO Variant 1 — prietenos
            f"Buna ziua,\n\n"
            f"am gasit anuntul dvs. si m-am gandit ca v-ar prinde bine "
            f"un site propriu. Am pregatit o demonstratie:\n\n"
            f"{url}\n\n"
            f"Site complet gata, inclusiv modificari, "
            f"199 EUR o singura data — fara abonament lunar.\n\n"
            f"Spuneti-mi daca va intereseaza.\n\n"
            f"O zi buna",

            # RO Variant 2 — profesional, concis
            f"Buna ziua,\n\n"
            f"creez site-uri pentru meseriasi si am pregatit "
            f"o mostra direct pentru *{name}*:\n\n"
            f"{url}\n\n"
            f"Site profesional, 199 EUR, fara costuri lunare.\n\n"
            f"Raspundeti aici daca vreti sa discutam.\n\n"
            f"Cu stima",

            # RO Variant 3 — orientat pe valoare
            f"Buna ziua,\n\n"
            f"am observat ca *{name}* nu are inca un site web. "
            f"Am creat o mostra gratuita:\n\n"
            f"{url}\n\n"
            f"Include:\n"
            f"- Design mobil\n"
            f"- Datele si serviciile dvs.\n"
            f"- Vizibil pe Google\n\n"
            f"Pret: 199 EUR o singura data. Fara alte costuri.\n\n"
            f"Astept raspunsul dvs.",

            # RO Variant 4 — intrebare deschizatoare
            f"Buna ziua,\n\n"
            f"sunteti interesat de un site web pentru *{name}*?\n\n"
            f"Am pregatit deja o mostra — vedeti:\n"
            f"{url}\n\n"
            f"199 EUR, fara abonament. Il adaptez dupa dorintele dvs.\n\n"
            f"Dati-mi un semn daca va place.",

            # RO Variant 5 — scurt si direct
            f"Buna ziua,\n\n"
            f"am creat un site pentru firma dvs. — "
            f"vedeti aici:\n\n"
            f"{url}\n\n"
            f"199 EUR o singura data, modificarile incluse.\n\n"
            f"Scrieti-mi daca doriti.",
        ]
    elif market == "uk":
        # UK pitch in English. Pret £150 one-time (echivalent ~199 EUR,
        # punct de pret accesibil pe piata UK pentru un site simplu).
        variants = [
            # UK Variant 1 — friendly, direct
            f"Hi there,\n\n"
            f"I came across your ad and thought you might benefit from "
            f"having your own website. I've put together a quick demo:\n\n"
            f"{url}\n\n"
            f"Full website, mobile-friendly, ready to go — £150 one-time, "
            f"no monthly fees.\n\n"
            f"Let me know if you'd like to chat.\n\n"
            f"Cheers",

            # UK Variant 2 — professional, concise
            f"Hello,\n\n"
            f"I build websites for tradespeople. Made a sample directly "
            f"for *{name}*:\n\n"
            f"{url}\n\n"
            f"Complete site, £150, no subscription.\n\n"
            f"Just reply here if interested.\n\n"
            f"Kind regards",

            # UK Variant 3 — value-oriented
            f"Hi,\n\n"
            f"Noticed *{name}* doesn't have a website yet. "
            f"Built a free sample to show you:\n\n"
            f"{url}\n\n"
            f"Includes:\n"
            f"- Mobile-ready design\n"
            f"- Your details and services\n"
            f"- Google-friendly setup\n\n"
            f"£150 one-time. No ongoing costs.\n\n"
            f"Let me know your thoughts.",

            # UK Variant 4 — opening question
            f"Hi there,\n\n"
            f"Interested in a website for *{name}*?\n\n"
            f"Already made a demo — have a look:\n"
            f"{url}\n\n"
            f"£150, no subscription. I'll adjust it to your needs.\n\n"
            f"Drop me a message if you like it.",

            # UK Variant 5 — short and direct
            f"Hi,\n\n"
            f"Made you a website — take a look:\n\n"
            f"{url}\n\n"
            f"£150 one-time, edits included.\n\n"
            f"Get in touch if you want it.",
        ]
    else:
        # Default fallback — un singur mesaj generic in engleza
        # (nu ar trebui sa ajunga aici daca MARKET e setat corect)
        variants = [
            f"Hi,\n\nI made a sample website for your business:\n\n"
            f"{url}\n\nLet me know if you're interested.",
        ]

    return random.choice(variants)


# ═══════════════════════════════════════════════════════════
# WhatsApp — send helpers
# ═══════════════════════════════════════════════════════════

async def _get_screenshot_url(http: httpx.AsyncClient, site_url: str) -> str:
    """Obține un URL valid cu screenshot-ul site-ului generat via Microlink API."""
    logger.info("  Generating screenshot for %s...", site_url)
    
    encoded_url = urllib.parse.quote(site_url)
    api_endpoint = f"https://api.microlink.io/?url={encoded_url}&screenshot=true&meta=false"
    
    try:
        r = await http.get(api_endpoint, timeout=30.0)
        if r.is_success:
            data = r.json()
            screenshot_url = data.get("data", {}).get("screenshot", {}).get("url")
            if screenshot_url:
                logger.info("  ✓ Screenshot generated successfully: %s", screenshot_url)
                return screenshot_url
    except Exception as e:
        logger.warning("  ✗ Failed to generate screenshot for %s: %s", site_url, e)
    
    return ""


async def _wa_typing(http: httpx.AsyncClient, phone: str, ms: int) -> None:
    """Non-fatal typing indicator."""
    clean = re.sub(r"[^\d]", "", phone)
    try:
        await http.post(
            f"{settings.waha_api_url}/sendTyping",
            json={"phone": clean, "duration": ms},
            timeout=10.0,
        )
    except Exception:
        pass  # always non-fatal


async def _wa_send(
    http: httpx.AsyncClient, phone: str, text: str, image_url: str = ""
) -> tuple[bool, str]:
    """
    Send message (and optionally an image URL).
    Returns (success, reason).
    Raises RuntimeError on 503 (WAHA restarting).
    """
    clean = re.sub(r"[^\d]", "", phone)

    payload = {
        "phone": clean,
        "message": text
    }

    if image_url:
        payload["mediaUrl"] = image_url

    r = await http.post(
        f"{settings.waha_api_url}/send",
        json=payload,
        timeout=25.0,
    )

    if r.status_code == 503:
        data = r.json()
        raise RuntimeError(f"WAHA_RESTARTING:{data.get('retry_after', 30)}")

    if r.is_success:
        data = r.json()
        if data.get("success", True) is False:
            return False, data.get("reason", "not_on_whatsapp")
        return True, ""

    # 500 — check if it's "not on WhatsApp" vs real error
    body = r.text[:300]
    if any(kw in body.lower() for kw in ["no lid", "not registered", "invalid"]):
        return False, "not_on_whatsapp"

    raise RuntimeError(f"WAHA {r.status_code}: {body}")


# ═══════════════════════════════════════════════════════════
# Loop B — WhatsApp scheduler
# ═══════════════════════════════════════════════════════════

async def _count_sent_today() -> int:
    today = datetime.combine(date.today(), dtime.min).replace(tzinfo=timezone.utc)
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            select(func.count(Job.id)).where(
                Job.status == JobStatus.OUTREACH_SENT,
                Job.updated_at >= today,
            )
        )
        return r.scalar_one() or 0


async def loop_whatsapp() -> None:
    logger.info("═══ WHATSAPP LOOP STARTED ═══")
    daily_limit = min(getattr(settings, 'daily_limit', 35), 35)

    async with httpx.AsyncClient(timeout=30.0) as http:
        while True:
            try:
                # ── Daily limit ────────────────────────────
                sent = await _count_sent_today()
                if sent >= daily_limit:
                    now = datetime.now()
                    secs = 86400 - now.hour * 3600 - now.minute * 60 - now.second + 120
                    logger.info(
                        "Daily limit %d/%d reached. Sleeping %ds.",
                        sent, daily_limit, secs,
                    )
                    await asyncio.sleep(secs)
                    continue

                # ── Business hours ─────────────────────────
                if not _is_business_hours():
                    czech_h = _czech_hour()
                    logger.info(
                        "Outside business hours (Czech: %d:00). "
                        "Sleeping 30min.", czech_h,
                    )
                    await asyncio.sleep(1800)
                    continue

                # ── Fetch next DEPLOYED job ────────────────
                async with AsyncSessionLocal() as session:
                    r = await session.execute(
                        select(Job)
                        .where(
                            Job.status == JobStatus.DEPLOYED,
                            Job.vercel_url.isnot(None),
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                    job = r.scalar_one_or_none()

                if not job:
                    logger.debug("No DEPLOYED jobs. Sleeping 120s.")
                    await asyncio.sleep(120)
                    continue

                # ── Double-check filters ───────────────────
                skip_reason = None
                if not _is_czech_mobile(job.phone_number):
                    skip_reason = f"landline {job.phone_number}"
                elif not _is_quality_lead(job.business_name):
                    skip_reason = f"bad lead '{job.business_name[:40]}'"

                if skip_reason:
                    logger.info("[Job %d] SKIP: %s", job.id, skip_reason)
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            update(Job).where(Job.id == job.id)
                            .values(status=JobStatus.FAILED)
                        )
                        await session.commit()
                    continue

                # ── Lock immediately ───────────────────────
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(Job).where(Job.id == job.id)
                        .values(status=JobStatus.OUTREACH_SENT)
                    )
                    await session.commit()

                try:
                    # Curăță numărul de telefon (elimină '+' și eventualele spații)
                    clean_phone = job.phone_number.replace("+", "").replace(" ", "")

                    # Generare Screenshot automată din link-ul de Vercel existent în DB
                    screenshot_url = ""
                    if job.vercel_url:
                        logger.info("[Job %d] Waiting for Vercel page render stability...", job.id)
                        await asyncio.sleep(4.0)  # scurt delay de siguranță pentru randare CSS/imagini
                        screenshot_url = await _get_screenshot_url(http, job.vercel_url)

                    # Step 1: typing (6-10s human simulation)
                    typing_ms = random.randint(6000, 10000)
                    logger.info(
                        "[Job %d] Typing to %s for %.1fs...",
                        job.id, clean_phone, typing_ms / 1000,
                    )
                    await _wa_typing(http, clean_phone, typing_ms)
                    await asyncio.sleep(typing_ms / 1000)

                    # Step 2: send randomly picked message variant (text + screenshot)
                    msg = _build_message(job)
                    ok, reason = await _wa_send(http, clean_phone, msg, screenshot_url)

                    if ok:
                        logger.info(
                            "[Job %d] ✓ SENT to %s — daily %d/%d",
                            job.id, clean_phone,
                            sent + 1, daily_limit,
                        )
                    else:
                        logger.info(
                            "[Job %d] ✗ %s not on WhatsApp (%s)",
                            job.id, clean_phone, reason,
                        )
                        async with AsyncSessionLocal() as session:
                            await session.execute(
                                update(Job).where(Job.id == job.id)
                                .values(status=JobStatus.FAILED)
                            )
                            await session.commit()
                        # Small pause then continue immediately
                        await asyncio.sleep(5)
                        continue

                except RuntimeError as exc:
                    err = str(exc)
                    if err.startswith("WAHA_RESTARTING:"):
                        wait = int(err.split(":")[1])
                        logger.warning(
                            "[Job %d] WAHA restarting. Revert DEPLOYED, wait %ds.",
                            job.id, wait,
                        )
                        async with AsyncSessionLocal() as session:
                            await session.execute(
                                update(Job).where(Job.id == job.id)
                                .values(status=JobStatus.DEPLOYED)
                            )
                            await session.commit()
                        await asyncio.sleep(wait)
                        continue
                    raise

                except Exception as exc:
                    logger.error(
                        "[Job %d] WhatsApp error: %s",
                        job.id, exc,
                    )
                    # Revert to DEPLOYED for retry
                    async with AsyncSessionLocal() as session:
                        await session.execute(
                            update(Job).where(Job.id == job.id)
                            .values(status=JobStatus.DEPLOYED)
                        )
                        await session.commit()
                    await asyncio.sleep(30)
                    continue

                # ── Anti-ban random delay 4-8 minutes ─────
                delay = random.randint(240, 480)
                logger.info(
                    "  Next message in %d min %d sec.",
                    delay // 60, delay % 60,
                )
                await asyncio.sleep(delay)

            except Exception as exc:
                logger.error("Scheduler crash: %s", exc, exc_info=True)
                await asyncio.sleep(30)


# ═══════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════

async def main() -> None:
    await init_db()

    # Reset stuck GENERATING jobs
    async with AsyncSessionLocal() as session:
        stuck = await session.execute(
            update(Job)
            .where(Job.status == JobStatus.GENERATING)
            .values(status=JobStatus.SCRAPED)
        )
        if stuck.rowcount:
            logger.info("Reset %d stuck GENERATING -> SCRAPED", stuck.rowcount)
        await session.commit()

    # Report current state
    async with AsyncSessionLocal() as session:
        for status in [JobStatus.SCRAPED, JobStatus.DEPLOYED,
                       JobStatus.OUTREACH_SENT, JobStatus.FAILED]:
            r = await session.execute(
                select(func.count(Job.id)).where(Job.status == status)
            )
            count = r.scalar_one() or 0
            logger.info("  DB: %s = %d", status.value, count)

    logger.info("")
    logger.info("╔════════════════════════════════════════╗")
    logger.info("║  Hybrid King v4 — Starting 2 loops     ║")
    logger.info("║  Deployer: generates + publishes sites  ║")
    logger.info("║  WhatsApp: sends 35/day, 08-19 Czech    ║")
    logger.info("╚════════════════════════════════════════╝")
    logger.info("")

    results = await asyncio.gather(
        loop_deployer(),
        loop_whatsapp(),
        return_exceptions=True,
    )
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            logger.critical("Loop %d crashed: %s", i, res, exc_info=res)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(
                "/home/%USER/hybridking/logs/orchestrator.log",
                encoding="utf-8",
            ),
        ],
    )
    asyncio.run(main())

