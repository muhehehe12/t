#!/usr/bin/env python3
"""
subscriptions.py — Gestionare abonamente clienti (manual, fara plati automate)
================================================================================
Tine evidenta clientilor care au acceptat upsell-ul de abonament lunar
(hosting+update-uri / mentenanta), si te avertizeaza cand le vine plata.

NU proceseaza plati automat — tu trimiti manual reminder-ul pe WhatsApp
si confirmi cand a platit. Filozofia e identica cu restul sistemului:
control manual, AI/automatizare doar la partea de generare/tracking.

Usage:
    python subscriptions.py add --id 42 --price 15 --currency EUR
        Marcheaza Job #42 ca abonat, 15 EUR/luna, prima plata azi

    python subscriptions.py list
        Arata toti abonatii activi + cand le vine urmatoarea plata

    python subscriptions.py due [--days 3]
        Arata cui ii vine plata in urmatoarele N zile (default 3)
        — astea sunt cele pe care trebuie sa le contactezi

    python subscriptions.py paid --id 42
        Confirma plata primita, avanseaza next_billing_date cu o luna

    python subscriptions.py cancel --id 42
        Dezactiveaza abonamentul (is_subscriber=False)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from database import AsyncSessionLocal, Job, init_db


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def cmd_package(job_id: int, package_type: str, price: float, currency: str) -> None:
    """Marcheaza ce pachet a luat clientul la vanzarea initiala
    (premium/landing/standard) — separat de abonamentul recurent."""
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if not job:
            print(f"  Job #{job_id} nu exista.")
            return

        job.package_type = package_type
        job.package_price = price
        job.package_currency = currency.upper()
        await session.commit()

        print(f"\n  ✅  Pachet inregistrat:")
        print(f"      Firma   : {job.business_name}")
        print(f"      Pachet  : {package_type}")
        print(f"      Pret    : {price} {currency.upper()}\n")


async def cmd_add(job_id: int, price: float, currency: str, notes: str | None) -> None:
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if not job:
            print(f"  Job #{job_id} nu exista.")
            return

        now = _now()
        job.is_subscriber = True
        job.subscription_price = price
        job.subscription_currency = currency.upper()
        job.subscription_started_at = now
        job.next_billing_date = now + timedelta(days=30)
        job.last_payment_at = now  # prima plata = azi, la activare
        if notes:
            job.subscription_notes = notes

        await session.commit()

        print(f"\n  ✅  Abonament activat:")
        print(f"      Firma   : {job.business_name}")
        print(f"      Telefon : {job.phone_number}")
        print(f"      Pret    : {price} {currency.upper()}/luna")
        print(f"      Urmatoarea plata: {job.next_billing_date.strftime('%Y-%m-%d')}\n")


async def cmd_list() -> None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Job).where(Job.is_subscriber.is_(True))
            .order_by(Job.next_billing_date)
        )
        subs = result.scalars().all()

    if not subs:
        print("\n  Niciun abonat activ momentan.\n")
        return

    total_mrr: dict[str, float] = {}
    print(f"\n  {'='*70}")
    print(f"  ABONATI ACTIVI ({len(subs)})")
    print(f"  {'='*70}")

    for job in subs:
        currency = job.subscription_currency or "?"
        price = job.subscription_price or 0
        total_mrr[currency] = total_mrr.get(currency, 0) + price

        next_bill = (job.next_billing_date.strftime('%Y-%m-%d')
                     if job.next_billing_date else "—")
        days_left = (
            (job.next_billing_date - _now()).days
            if job.next_billing_date else None
        )
        flag = ""
        if days_left is not None:
            if days_left < 0:
                flag = "  ⚠️  INTARZIATA"
            elif days_left <= 3:
                flag = "  🔔  SCADENTA APROAPE"

        print(f"\n  #{job.id}  {job.business_name}")
        print(f"      Tel: {job.phone_number}")
        print(f"      {price} {currency}/luna  |  Urm. plata: {next_bill}{flag}")
        if job.subscription_notes:
            print(f"      Note: {job.subscription_notes}")

    print(f"\n  {'-'*70}")
    print(f"  MRR total: " + ", ".join(
        f"{v:.2f} {k}" for k, v in total_mrr.items()
    ))
    print(f"  {'='*70}\n")


async def cmd_due(days: int) -> None:
    cutoff = _now() + timedelta(days=days)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Job).where(
                Job.is_subscriber.is_(True),
                Job.next_billing_date <= cutoff,
            ).order_by(Job.next_billing_date)
        )
        due_jobs = result.scalars().all()

    if not due_jobs:
        print(f"\n  Niciun abonat cu plata scadenta in urmatoarele {days} zile.\n")
        return

    print(f"\n  {'='*70}")
    print(f"  PLATI SCADENTE — urmatoarele {days} zile (contacteaza-i manual)")
    print(f"  {'='*70}")

    for job in due_jobs:
        days_left = (job.next_billing_date - _now()).days
        status_txt = (f"INTARZIATA cu {-days_left} zile" if days_left < 0
                      else f"scade in {days_left} zile" if days_left > 0
                      else "SCADE AZI")
        wa_link = f"https://wa.me/{job.phone_number.lstrip('+')}"

        print(f"\n  #{job.id}  {job.business_name}  —  {status_txt}")
        print(f"      Tel     : {job.phone_number}")
        print(f"      Suma    : {job.subscription_price} {job.subscription_currency}")
        print(f"      WhatsApp: {wa_link}")

    print(f"\n  {'='*70}\n")


async def cmd_paid(job_id: int) -> None:
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if not job or not job.is_subscriber:
            print(f"  Job #{job_id} nu e abonat activ.")
            return

        now = _now()
        job.last_payment_at = now
        # Avanseaza cu o luna de la data scadenta (nu de la azi),
        # ca sa nu se acumuleze drift daca plateste cu cateva zile intarziere
        base = job.next_billing_date or now
        job.next_billing_date = base + timedelta(days=30)

        await session.commit()

        print(f"\n  ✅  Plata confirmata pentru {job.business_name}")
        print(f"      Urmatoarea plata: {job.next_billing_date.strftime('%Y-%m-%d')}\n")


async def cmd_gmb_checklist() -> None:
    """
    Checklist lunar pentru abonatii GMB/SEO local — un livrabil
    concret recurent, ca sa nu fie "platesc pentru nimic vizibil".
    NU automatizeaza GMB-ul (API-ul cere verificare business),
    e ghid manual de executat pentru fiecare abonat activ.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Job).where(Job.is_subscriber.is_(True))
        )
        subs = result.scalars().all()

    if not subs:
        print("\n  Niciun abonat activ — niciun checklist de generat.\n")
        return

    print(f"\n  {'='*70}")
    print(f"  CHECKLIST LUNAR GMB/SEO — {len(subs)} abonati")
    print(f"  {'='*70}")
    print("""
  Pentru FIECARE client de mai jos, in fiecare luna:
    [ ] 1. Postare noua pe profilul Google Business (oferta/update)
    [ ] 2. Adauga 1 poza noua (din site sau client)
    [ ] 3. Verifica recenzii noi -> raspunde sau trimite draft clientului
    [ ] 4. Verifica NAP (Nume/Adresa/Telefon) consistent cu site-ul
    [ ] 5. Trimite client un mesaj scurt cu "raportul lunii"
""")

    for job in subs:
        print(f"  → #{job.id}  {job.business_name}  ({job.phone_number})")

    print(f"\n  {'='*70}\n")


async def cmd_packages() -> None:
    """Raport pe tipuri de pachete vandute (premium/landing/standard)."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Job))
        jobs = result.scalars().all()

    counts: dict[str, int] = {}
    revenue: dict[str, float] = {}

    for job in jobs:
        pkg = job.package_type or "standard"
        counts[pkg] = counts.get(pkg, 0) + 1
        if job.package_price:
            revenue[pkg] = revenue.get(pkg, 0) + job.package_price

    print(f"\n  {'='*70}")
    print(f"  RAPORT PACHETE VANDUTE")
    print(f"  {'='*70}")
    for pkg, count in sorted(counts.items(), key=lambda x: -x[1]):
        rev = revenue.get(pkg, 0)
        print(f"  {pkg:<12} : {count:>3} clienti   |   {rev:.2f} total")
    print(f"  {'='*70}\n")


async def cmd_chatbot(job_id: int, enable: bool) -> None:
    """Activeaza/dezactiveaza chatbot pentru un job. NU face redeploy
    automat — ruleaza separat `python redeploy.py --id N` dupa asta."""
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if not job:
            print(f"  Job #{job_id} nu exista.")
            return

        job.chatbot_enabled = enable
        await session.commit()

        action = "activat" if enable else "dezactivat"
        print(f"\n  ✅  Chatbot {action} pentru {job.business_name}")
        if enable:
            print(f"      ⚠️  Ruleaza acum: python redeploy.py --id {job.id}")
            print(f"      (ca sa apara efectiv pe site-ul deja live)\n")
        else:
            print()


async def cmd_contacted(job_id: int) -> None:
    """Marcheaza ca am contactat lead-ul azi. Programeaza follow-up auto."""
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if not job:
            print(f"  Job #{job_id} nu exista.")
            return

        now = _now()
        job.contacted_at = now
        job.followup_count = (job.followup_count or 0) + 1

        # Programeaza follow-up dupa N zile: 3, 7, 14
        followup_days = [3, 7, 14]
        idx = min(job.followup_count - 1, len(followup_days) - 1)
        job.next_followup_at = now + timedelta(days=followup_days[idx])

        await session.commit()

        print(f"\n  ✅  Marcat ca contactat #{job.followup_count}: {job.business_name}")
        print(f"      Urmatoarea recontactare: {job.next_followup_at.strftime('%Y-%m-%d')}\n")


async def cmd_replied(job_id: int, interested: bool = False) -> None:
    """Marcheaza ca lead-ul a raspuns. Optional: marcheaza ca interesat."""
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if not job:
            print(f"  Job #{job_id} nu exista.")
            return

        job.replied_at = _now()
        job.interested = interested
        # Cand a raspuns, nu mai urmarim auto-followup
        job.next_followup_at = None
        await session.commit()

        flag = "INTERESAT" if interested else "raspuns generic"
        print(f"\n  📩  {job.business_name}: {flag}\n")


async def cmd_sold(job_id: int, amount: float, currency: str) -> None:
    """Marcheaza vanzare incheiata. Inregistreaza suma."""
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if not job:
            print(f"  Job #{job_id} nu exista.")
            return

        job.sold = True
        job.interested = True
        job.sale_amount = amount
        job.sale_currency = currency.upper()
        if not job.package_price:
            job.package_price = amount
            job.package_currency = currency.upper()
        await session.commit()

        print(f"\n  💰  VANZARE INCHEIATA: {job.business_name}")
        print(f"      Suma: {amount} {currency.upper()}\n")


async def cmd_followups_due() -> None:
    """Lista lead-uri care necesita follow-up azi."""
    now = _now()
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Job).where(
                Job.next_followup_at.isnot(None),
                Job.next_followup_at <= now,
                Job.sold.is_(False),
                Job.interested.is_(False),
            ).order_by(Job.next_followup_at)
        )
        jobs = list(result.scalars().all())

    if not jobs:
        print("\n  Niciun follow-up scadent azi.\n")
        return

    print(f"\n  {'='*70}")
    print(f"  FOLLOW-UPS SCADENTE — {len(jobs)} lead-uri")
    print(f"  {'='*70}\n")

    for job in jobs:
        wa_link = f"https://wa.me/{job.phone_number.lstrip('+')}"
        days_ago = (now - job.contacted_at).days if job.contacted_at else "?"

        print(f"  #{job.id}  {job.business_name}")
        print(f"      Contactat acum {days_ago} zile  |  Tentativa #{job.followup_count}")
        print(f"      Tel: {job.phone_number}  |  WA: {wa_link}")
        print(f"      Scor: {job.lead_score}  |  Site: {job.vercel_url or '-'}\n")


async def cmd_crm_stats() -> None:
    """Statistici CRM: rata contactare, raspuns, conversie."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Job))
        all_jobs = list(result.scalars().all())

    total = len(all_jobs)
    if total == 0:
        print("\n  Niciun job in DB.\n")
        return

    deployed = sum(1 for j in all_jobs if j.vercel_url)
    contacted = sum(1 for j in all_jobs if j.contacted_at)
    replied = sum(1 for j in all_jobs if j.replied_at)
    interested = sum(1 for j in all_jobs if j.interested)
    sold = sum(1 for j in all_jobs if j.sold)

    # Total venituri pe valuta
    revenue: dict[str, float] = {}
    for j in all_jobs:
        if j.sold and j.sale_amount and j.sale_currency:
            revenue[j.sale_currency] = revenue.get(j.sale_currency, 0) + j.sale_amount

    print(f"\n  {'='*60}")
    print(f"  CRM STATS")
    print(f"  {'='*60}")
    print(f"  Total lead-uri:       {total}")
    print(f"  Site-uri deployed:    {deployed}  ({100*deployed/total:.1f}%)")
    print(f"  Contactati:           {contacted}  ({100*contacted/max(1,deployed):.1f}% din deployed)")
    print(f"  Au raspuns:           {replied}  ({100*replied/max(1,contacted):.1f}% din contactati)")
    print(f"  Interesati:           {interested}")
    print(f"  VANDUTI:              {sold}  ({100*sold/max(1,contacted):.1f}% din contactati)")
    print(f"  {'-'*60}")
    if revenue:
        for cur, amt in revenue.items():
            print(f"  Venit total {cur}:        {amt:.2f}")
    print(f"  {'='*60}\n")


async def cmd_cancel(job_id: int) -> None:
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if not job:
            print(f"  Job #{job_id} nu exista.")
            return

        job.is_subscriber = False
        await session.commit()

        print(f"\n  Abonament dezactivat pentru {job.business_name}\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Gestionare abonamente clienti")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Activeaza abonament pentru un job")
    p_add.add_argument("--id", type=int, required=True)
    p_add.add_argument("--price", type=float, required=True)
    p_add.add_argument("--currency", default="EUR")
    p_add.add_argument("--notes", default=None)

    p_pkg = sub.add_parser("package", help="Inregistreaza pachetul vandut (premium/landing/standard)")
    p_pkg.add_argument("--id", type=int, required=True)
    p_pkg.add_argument("--type", choices=["premium", "landing", "standard"], required=True)
    p_pkg.add_argument("--price", type=float, required=True)
    p_pkg.add_argument("--currency", default="RON")

    sub.add_parser("packages", help="Raport pe tipuri de pachete vandute")
    sub.add_parser("gmb-checklist", help="Checklist lunar pentru abonati GMB/SEO")

    p_bot = sub.add_parser("chatbot", help="Activeaza/dezactiveaza chatbot pe site")
    p_bot.add_argument("--id", type=int, required=True)
    p_bot.add_argument("--off", action="store_true", help="Dezactiveaza (default: activeaza)")

    # ── CRM commands ────────────────────────────────────────
    p_c = sub.add_parser("contacted", help="Marcheaza ca am contactat lead-ul azi")
    p_c.add_argument("--id", type=int, required=True)

    p_r = sub.add_parser("replied", help="Marcheaza ca a raspuns lead-ul")
    p_r.add_argument("--id", type=int, required=True)
    p_r.add_argument("--interested", action="store_true",
                     help="Marcheaza ca interesat (raspuns pozitiv)")

    p_s = sub.add_parser("sold", help="Marcheaza vanzare incheiata")
    p_s.add_argument("--id", type=int, required=True)
    p_s.add_argument("--amount", type=float, required=True)
    p_s.add_argument("--currency", default="RON")

    sub.add_parser("followups", help="Lista follow-ups scadente azi")
    sub.add_parser("crm-stats", help="Statistici CRM: contactati/raspuns/vanduti")

    sub.add_parser("list", help="Lista abonati activi + MRR")

    p_due = sub.add_parser("due", help="Plati scadente in urmatoarele N zile")
    p_due.add_argument("--days", type=int, default=3)

    p_paid = sub.add_parser("paid", help="Confirma plata primita")
    p_paid.add_argument("--id", type=int, required=True)

    p_cancel = sub.add_parser("cancel", help="Dezactiveaza abonament")
    p_cancel.add_argument("--id", type=int, required=True)

    args = parser.parse_args()
    await init_db()

    if args.command == "add":
        await cmd_add(args.id, args.price, args.currency, args.notes)
    elif args.command == "package":
        await cmd_package(args.id, args.type, args.price, args.currency)
    elif args.command == "packages":
        await cmd_packages()
    elif args.command == "gmb-checklist":
        await cmd_gmb_checklist()
    elif args.command == "chatbot":
        await cmd_chatbot(args.id, enable=not args.off)
    elif args.command == "contacted":
        await cmd_contacted(args.id)
    elif args.command == "replied":
        await cmd_replied(args.id, interested=args.interested)
    elif args.command == "sold":
        await cmd_sold(args.id, args.amount, args.currency)
    elif args.command == "followups":
        await cmd_followups_due()
    elif args.command == "crm-stats":
        await cmd_crm_stats()
    elif args.command == "list":
        await cmd_list()
    elif args.command == "due":
        await cmd_due(args.days)
    elif args.command == "paid":
        await cmd_paid(args.id)
    elif args.command == "cancel":
        await cmd_cancel(args.id)


if __name__ == "__main__":
    asyncio.run(main())
