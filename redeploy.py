#!/usr/bin/env python3
"""
redeploy.py — Regenereaza si redeploy-eaza site-ul unui job EXISTENT
=======================================================================
Folosit cand un client deja activ accepta un upsell (ex: chatbot)
si vrei sa-i actualizezi site-ul deja live, FARA sa creezi un repo
nou — acelasi link pe care l-a primit deja pe WhatsApp continua
sa functioneze, doar continutul se actualizeaza.

Functioneaza pentru ca:
  - github_deploy.py detecteaza daca repo-ul exista deja (il refoloseste)
  - _upload_file detecteaza daca fisierul exista deja (il actualizeaza
    via SHA, nu da eroare de duplicat)

Usage:
    python redeploy.py --id 42
        Regenereaza site-ul pentru job #42 cu datele curente din DB
        (inclusiv chatbot_enabled, daca a fost activat).

    python redeploy.py --id 42 --market ro
        Specifica explicit piata (cz/ro) — default cz daca nu e dat.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

from database import AsyncSessionLocal, Job
from factory import generate_website
from github_deploy import deploy_to_github_pages


async def redeploy(job_id: int, market: str) -> None:
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)

    if not job:
        print(f"  Job #{job_id} nu exista.")
        return

    print(f"\n  Regenerare site pentru: {job.business_name}")
    print(f"  Chatbot activ: {job.chatbot_enabled}")

    site = await generate_website(
        business_name=job.business_name,
        niche=job.niche,
        language=job.language,
        phone=job.phone_number,
        description=job.description or "",
        job_id=job.id,
        chatbot_enabled=job.chatbot_enabled,
    )

    async with httpx.AsyncClient(timeout=30.0) as http:
        url, repo_name = await deploy_to_github_pages(
            http,
            business_name=job.business_name,
            job_id=job.id,
            market=market,
            index_html=site.index_html,
        )

    print(f"\n  ✅  Redeploy complet")
    print(f"      URL : {url}")
    print(f"      Repo: {repo_name}\n")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Redeploy site existent")
    parser.add_argument("--id", type=int, required=True)
    parser.add_argument("--market", default="cz", choices=["cz", "ro"])
    args = parser.parse_args()

    # GITHUB_TOKEN trebuie sa fie deja in .env (citit via config.py)
    if not os.getenv("GITHUB_TOKEN"):
        from config import settings
        if not settings.github_token:
            print("  GITHUB_TOKEN lipseste din .env — nu pot face deploy.")
            sys.exit(1)

    await redeploy(args.id, args.market)


if __name__ == "__main__":
    asyncio.run(main())
