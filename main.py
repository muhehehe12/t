"""
main.py — Hybrid King Entry Point (Manual WhatsApp Mode)
=========================================================
Suporta doua piete independente:
    python main.py --market cz   # Cehia: bazos.cz
    python main.py --market ro   # Romania: olx.ro

Fiecare piata = DB separat + CSV separat + log separat.
WhatsApp auto-send = DEZACTIVAT. Trimiti tu manual.
"""
import argparse
import os
import sys

# ══ 1. Parse market INAINTE de orice alt import ══════════════
def _parse_market() -> str:
    p = argparse.ArgumentParser(
        description="Hybrid King — Lead scraper + site generator",
        add_help=False,
    )
    p.add_argument(
        "--market", choices=["ro", "uk", "uk_b2b"], default="ro",
        help="Piata: ro (olx) | uk (gumtree, consumer) | uk_b2b (companies house, B2B)"
    )
    args, _ = p.parse_known_args()
    return args.market

MARKET = _parse_market()

# ══ 2. Set env vars INAINTE de import config/database ════════
if "DATABASE_URL" not in os.environ:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./hybridking_{MARKET}.db"
os.environ["MARKET"] = MARKET   # folosit de orchestrator._is_mobile + _build_message

# ══ 3. Acum importam restul ══════════════════════════════════
import asyncio
import csv
import logging
import re
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select, update

from config import settings
from database import AsyncSessionLocal, Job, JobStatus, init_db
from orchestrator import (
    _build_message,
    _is_mobile,
    _is_quality_lead,
    loop_deployer,
)

# ── Importa scraper-ul corect pentru piata ────────────────────
if MARKET == "ro":
    # Piata RO: DOAR OLX (Publi24 si Okazii eliminate complet —
    # dadeau telefoane gresite/random, nu meritau efortul)
    from scraper_ro import run_scraper_ro as _run_scraper
elif MARKET == "uk":
    # Piata UK consumer: doar Gumtree (singura sursa confirmata cu meseriasi
    # + telefoane vizibile). Vezi nota din scraper_gumtree.py despre
    # riscuri: WhatsApp ~48% penetrare in UK, Gumtree filtreaza unele
    # telefoane automat.
    from scraper_gumtree import run_scraper_uk as _run_scraper
elif MARKET == "uk_b2b":
    # Piata UK B2B: Companies House -> firme construction registrate.
    # Pipeline: scrape firme -> enrichment email -> generare demo
    # -> drafturi email B2B (NU WhatsApp).
    from scraper_companies_house import run_scraper_companies_house as _run_ch
    from email_extractor import enrich_b2b_jobs

    async def _run_scraper(total: int = 200) -> None:
        """Pipeline UK B2B: scrape firme + enrichment email."""
        # Pas 1: scrape firme din Companies House
        await _run_ch(total=total)
        # Pas 2: enrichment email (vizita site firma, extract email)
        await enrich_b2b_jobs(limit=total)
else:
    raise ValueError(
        f"MARKET='{MARKET}' invalid — accept doar 'ro', 'uk' sau 'uk_b2b'."
    )

# ── Constante per piata ───────────────────────────────────────
_OUTREACH_CSV  = Path(f"outreach_{MARKET}.csv")
_LOG_FILE      = Path(f"logs/hybridking_{MARKET}.log")
_MARKET_LABEL  = {
    "ro":     "RO - olx.ro (consumer/meseriasi)",
    "uk":     "UK - gumtree.com (consumer/tradespeople)",
    "uk_b2b": "UK B2B - Companies House (construction)",
}.get(MARKET, MARKET.upper())
_MARKET_FLAG   = MARKET.upper()

logger = logging.getLogger("hybridking.main")

# IDs deja notificate in aceasta sesiune
_notified_ids: set[int] = set()


# ═══════════════════════════════════════════════════════════
# CSV logger
# ═══════════════════════════════════════════════════════════

def _ensure_csv() -> None:
    if not _OUTREACH_CSV.exists():
        with open(_OUTREACH_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "piata", "job_id", "firma", "telefon", "nisa",
                "website", "wa_link", "mesaj_scurt", "notificat_la",
            ])


def _append_csv(job: Job, msg: str) -> None:
    clean = re.sub(r"[^\d]", "", job.phone_number)
    wa    = f"https://wa.me/{clean}"
    # Prima linie a mesajului ca preview
    preview = msg.split("\n\n")[0].replace("\n", " ")[:80]
    with open(_OUTREACH_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            MARKET.upper(),
            job.id,
            job.business_name,
            job.phone_number,
            job.niche,
            job.vercel_url,
            wa,
            preview,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ])


def _detect_source(job: Job) -> str:
    """Detecteaza sursa lead-ului din URL salvat in descriere."""
    desc = (job.description or "").lower()
    if "olx.ro" in desc:
        return "OLX.ro"
    if "gumtree" in desc:
        return "Gumtree.com"
    return "?"


# ═══════════════════════════════════════════════════════════
# Notifier loop
# ═══════════════════════════════════════════════════════════

async def loop_notifier() -> None:
    """
    Scaneaza DB la fiecare 30s pentru joburi DEPLOYED noi.
    Format imbunatatit: contor zi curenta, sursa lead-ului,
    layout consistent, prioritate vizuala pentru date critice.
    Salveaza in CSV.
    """
    logger.info("[%s] Notifier pornit (manual WhatsApp mode)", _MARKET_FLAG)
    _ensure_csv()

    today_key = datetime.now().strftime("%Y-%m-%d")
    today_count = 0

    while True:
        # Reset contor cand se schimba ziua
        current_day = datetime.now().strftime("%Y-%m-%d")
        if current_day != today_key:
            today_key = current_day
            today_count = 0
            logger.info("[%s] Zi noua (%s) — contor resetat",
                        _MARKET_FLAG, today_key)

        try:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Job).where(
                        Job.status == JobStatus.DEPLOYED,
                        Job.vercel_url.isnot(None),
                    )
                )
                jobs = result.scalars().all()

            for job in jobs:
                if job.id in _notified_ids:
                    continue

                # Filtreaza numere fixe si lead-uri de calitate slaba
                if not _is_mobile(job.phone_number):
                    _notified_ids.add(job.id)
                    continue
                if not _is_quality_lead(job.business_name):
                    _notified_ids.add(job.id)
                    continue

                _notified_ids.add(job.id)
                today_count += 1
                msg      = _build_message(job)
                clean    = re.sub(r"[^\d]", "", job.phone_number)
                wa_link  = f"https://wa.me/{clean}"
                ts       = datetime.now().strftime("%H:%M:%S")

                # Detecteaza sursa lead-ului din URL-ul anuntului
                # original (daca exista) sau din pattern firma
                source = _detect_source(job)

                _append_csv(job, msg)

                # ── Output formatat — Unicode box drawing ─────────
                w = 62
                top    = "┏" + "━" * (w - 2) + "┓"
                mid    = "┣" + "━" * (w - 2) + "┫"
                bot    = "┗" + "━" * (w - 2) + "┛"
                blank  = "┃" + " " * (w - 2) + "┃"

                def line(content: str, indent: int = 1) -> str:
                    pad = w - 2 - indent
                    return "┃" + " " * indent + content[:pad].ljust(pad) + "┃"

                print(f"\n{top}")
                hdr = (f"#{job.id}  •  [{_MARKET_FLAG}]  •  {source}"
                       f"  •  {ts}  •  Azi: {today_count}")
                print(line(hdr))
                print(mid)
                print(line(f"Firma   : {job.business_name}"))
                print(line(f"Telefon : {job.phone_number}"))
                print(line(f"Nisa    : {job.niche}"))
                if job.seller_name:
                    print(line(f"Vanzator: {job.seller_name}"))
                print(blank)
                print(line(f"Website : {job.vercel_url}"))
                print(line(f"WhatsApp: {wa_link}"))
                print(mid)
                print(line("MESAJ DE TRIMIS:"))
                print(blank)
                for msg_line in msg.split("\n"):
                    # Hard wrap pentru fiecare linie a mesajului
                    while len(msg_line) > w - 4:
                        print(line(msg_line[:w - 4], indent=2))
                        msg_line = msg_line[w - 4:]
                    print(line(msg_line, indent=2))
                print(bot)
                print()

                logger.info(
                    "[%s Job %d] NOTIFIED (#%d azi): '%s' | %s | %s",
                    _MARKET_FLAG, job.id, today_count,
                    job.business_name[:35], job.phone_number, job.vercel_url,
                )

        except Exception as exc:
            logger.error("[%s] Notifier eroare: %s", _MARKET_FLAG, exc, exc_info=True)

        await asyncio.sleep(30)


# ═══════════════════════════════════════════════════════════
# Status printer (la fiecare 5 min)
# ═══════════════════════════════════════════════════════════

async def loop_status() -> None:
    await asyncio.sleep(60)
    while True:
        try:
            async with AsyncSessionLocal() as session:
                counts: dict[str, int] = {}
                for st in JobStatus:
                    r = await session.execute(
                        select(func.count(Job.id)).where(Job.status == st)
                    )
                    counts[st.value] = r.scalar_one() or 0
            ts = datetime.now().strftime("%H:%M")
            summary = " | ".join(
                f"{k}={v}" for k, v in counts.items() if v > 0
            )
            print(f"\n  [{_MARKET_FLAG}] {ts}  {summary}\n")
        except Exception as exc:
            logger.error("Status loop eroare: %s", exc)
        await asyncio.sleep(300)


# ═══════════════════════════════════════════════════════════
# Validare configuratie
# ═══════════════════════════════════════════════════════════

def _validate_config() -> list[str]:
    errors = []
    groq_ok = bool(
        os.getenv("GROQ_API_KEYS")
        or os.getenv("GROQ_API_KEY")
        or settings.groq_api_keys
        or settings.groq_api_key
    )
    if not groq_ok:
        errors.append("GROQ_API_KEYS lipseste din .env")
    github_ok = bool(os.getenv("GITHUB_TOKEN") or settings.github_token)
    if not github_ok:
        errors.append("GITHUB_TOKEN lipseste din .env")
    return errors


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════

async def main() -> None:
    errors = _validate_config()

    print(f"""
╔══════════════════════════════════════════════════════════╗
║      HYBRID KING v4 — {_MARKET_LABEL:<35}║
╠══════════════════════════════════════════════════════════╣
║  1. Scraper  → lead-uri noi                             ║
║  2. Deployer → genereaza site Groq + publica Vercel     ║
║  3. Notifier → afiseaza date + mesaj WA pentru tine     ║
║                                                          ║
║  WhatsApp auto-send = DEZACTIVAT (trimiti manual)       ║
╚══════════════════════════════════════════════════════════╝
""")

    if errors:
        print("❌  ERORI CONFIGURATIE:")
        for e in errors:
            print(f"    • {e}")
        print("\n    Editeaza .env si reporneaste.\n")
        sys.exit(1)

    print(f"  Market      : {_MARKET_LABEL}")
    print(f"  Database    : hybridking_{MARKET}.db")
    print(f"  CSV output  : {_OUTREACH_CSV.resolve()}")
    print(f"  Log         : {_LOG_FILE.resolve()}")
    print()

    await init_db()

    # Reseteaza joburi blocate din sesiuni anterioare
    async with AsyncSessionLocal() as session:
        stuck = await session.execute(
            update(Job)
            .where(Job.status == JobStatus.GENERATING)
            .values(status=JobStatus.SCRAPED)
        )
        if stuck.rowcount:
            logger.info("[%s] Reset %d joburi GENERATING->SCRAPED",
                        _MARKET_FLAG, stuck.rowcount)
        await session.commit()

    logger.info("[%s] Pornesc Scraper + Deployer + Notifier...", _MARKET_FLAG)

    results = await asyncio.gather(
        _run_scraper(total=200),
        loop_deployer(),
        loop_notifier(),
        loop_status(),
        return_exceptions=True,
    )

    names = ["scraper", "deployer", "notifier", "status"]
    for i, res in enumerate(results):
        if isinstance(res, Exception):
            logger.critical("[%s] Task '%s' crashed: %s",
                            _MARKET_FLAG, names[i], res, exc_info=res)


# ═══════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    os.makedirs("sites", exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(_LOG_FILE), encoding="utf-8"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n\n  Oprit manual [{_MARKET_FLAG}]. Lead-urile gata sunt in {_OUTREACH_CSV}\n")
