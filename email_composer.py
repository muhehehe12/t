"""
email_composer.py — Compune email B2B personalizat (NU trimite automat)
==========================================================================
Genereaza email-uri B2B personalizate pentru lead-urile UK Companies House
care au:
  - email gasit (prin email_extractor)
  - site web deja generat si deployed (prin orchestrator)

FILOSOFIA DE BAZA: la fel ca WhatsApp-ul tau RO. Tu trimiti manual,
sistemul iti pregateste drafturile. Nu iei ban de pe domain-ul tau de
email pentru ca tu controlezi volumul si poti customiza fiecare draft
inainte de trimitere.

Output:
  - Fisier CSV: outreach_uk_b2b.csv cu: company | email | subject | body | site_demo
  - Pentru fiecare lead, draft incepe cu un detaliu UNIC (nume firma, 
    locatie, SIC) — NU template identic copy-paste.

Daca configurezi SMTP in .env, comanda `python email_composer.py --send`
trimite efectiv. La inceput RECOMANDAT modul "draft only" (default).
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import random
import re
import sys
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
from sqlalchemy import select

from config import settings
from database import AsyncSessionLocal, Job, JobStatus, init_db

logger = logging.getLogger(__name__)

_DRAFTS_CSV = Path("outreach_uk_b2b.csv")

# Variante de SUBJECT - randomizate per email ca sa nu para mass-template
_SUBJECT_VARIANTS = [
    "Quick website concept for {name}",
    "Made a site preview for {name}",
    "{name} — small idea worth a look",
    "Free website mockup for {name}",
    "Built a demo for {name} this morning",
]


def _industry_label(sic_or_niche: str) -> str:
    """Construction (SIC 41202) -> 'residential construction'"""
    if "41201" in sic_or_niche:
        return "commercial construction"
    if "41202" in sic_or_niche:
        return "residential construction"
    if "41100" in sic_or_niche:
        return "building development"
    if "43210" in sic_or_niche:
        return "electrical installation"
    if "43220" in sic_or_niche:
        return "plumbing and heating"
    if "43320" in sic_or_niche:
        return "joinery"
    return "construction"


def _compose_email(job: Job) -> tuple[str, str]:
    """
    Returneaza (subject, body) personalizat pentru un job B2B.
    NU template identic — variaza pe SIC, nume firma, oras.
    """
    name = job.business_name
    industry = _industry_label(job.niche or "")
    demo_url = job.vercel_url or "(demo se genereaza dupa contact)"

    # Extrage oras din descriere daca posibil
    city = ""
    desc = job.description or ""
    for c in ["London", "Manchester", "Birmingham", "Glasgow", "Leeds",
              "Liverpool", "Bristol", "Edinburgh", "Cardiff", "Belfast"]:
        if c in desc:
            city = c
            break

    subject = random.choice(_SUBJECT_VARIANTS).format(name=name)

    location_part = f" based in {city}" if city else ""

    body = f"""Hi,

I noticed {name}{location_part} working in {industry} and put together a quick website concept that I thought you might find interesting:

{demo_url}

It's a free demo - no obligation. I build sites specifically for {industry} firms in the UK, focused on:

- Mobile-first design that converts visitors into enquiries
- Built around your services and location
- Fully owned by you - no monthly subscriptions

If it's something you'd like to discuss, just reply to this email. If not, no worries - delete and ignore.

Best regards,
{settings.smtp_from_name or 'Web Agency'}

---
P.S. If you'd prefer not to hear from me, just reply with "no thanks" and I won't contact you again.
"""
    return subject, body


def _write_draft_csv(jobs: list[Job]) -> None:
    """Salveaza drafturi intr-un CSV pentru tine sa le revizuiesti
    si sa le trimiti manual din clientul tau de email."""
    write_header = not _DRAFTS_CSV.exists()

    with open(_DRAFTS_CSV, "a", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "timestamp", "job_id", "company_name", "email",
                "demo_url", "subject", "body",
            ])

        for job in jobs:
            subject, body = _compose_email(job)
            writer.writerow([
                datetime.now().isoformat(),
                job.id,
                job.business_name,
                job.email or "",
                job.vercel_url or "",
                subject,
                body,
            ])


async def _send_email_smtp(to_email: str, subject: str, body: str) -> bool:
    """Trimite email prin SMTP configurat in .env. Returneaza True daca succes."""
    if not settings.smtp_host or not settings.smtp_from_email:
        logger.error("SMTP neconfigurat — nu pot trimite")
        return False

    msg = EmailMessage()
    msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=settings.smtp_password,
            start_tls=True,
            timeout=30,
        )
        return True
    except Exception as exc:
        logger.error("SMTP eroare pentru %s: %s", to_email, exc)
        return False


async def compose_b2b_drafts(send: bool = False, limit: int = 20) -> None:
    """
    Compune drafturi pentru job-uri B2B care:
    - au is_b2b=True
    - au email completat
    - au vercel_url (site demo deja deployed)
    - status DEPLOYED si NU SENT inca

    Daca send=True si SMTP e configurat, trimite efectiv si marcheaza SENT.
    Daca send=False (default), doar salveaza CSV cu drafturi.

    Limita pe rulare: ca sa NU trimitem 1000 emailuri intr-o ora =>
    spam folder garantat. 20/rulare e sustenabil.
    """
    await init_db()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Job).where(
                Job.is_b2b.is_(True),
                Job.email.isnot(None),
                Job.vercel_url.isnot(None),
                Job.status == JobStatus.DEPLOYED,
            ).limit(limit)
        )
        jobs = list(result.scalars().all())

    if not jobs:
        logger.info("Niciun job B2B pregatit (cu email + site demo).")
        return

    logger.info("B2B drafts: %d job-uri pregatite", len(jobs))

    if send and settings.smtp_host:
        # Mod trimitere efectiva — cu pauze mari intre email-uri
        sent_ok = 0
        for job in jobs:
            subject, body = _compose_email(job)
            ok = await _send_email_smtp(job.email, subject, body)
            if ok:
                sent_ok += 1
                # Marcheaza ca SENT
                async with AsyncSessionLocal() as session:
                    db_job = await session.get(Job, job.id)
                    if db_job:
                        db_job.status = JobStatus.SENT
                        await session.commit()
                logger.info("EMAIL SENT: %s -> %s", job.business_name[:40], job.email)
            # 60s intre emailuri ca sa fim safe pe reputatie SMTP
            await asyncio.sleep(60)

        logger.info("DONE. Sent %d/%d", sent_ok, len(jobs))
    else:
        # Mod draft — salveaza in CSV pentru trimitere manuala
        _write_draft_csv(jobs)
        logger.info(
            "DONE. %d drafturi salvate in %s. Trimite manual.",
            len(jobs), _DRAFTS_CSV.resolve(),
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true",
                        help="Trimite efectiv via SMTP (default: doar drafturi)")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    asyncio.run(compose_b2b_drafts(send=args.send, limit=args.limit))


if __name__ == "__main__":
    main()
