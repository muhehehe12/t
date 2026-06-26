"""
github_deploy.py — Deploy site-uri pe GitHub Pages
=====================================================
Inlocuieste complet Vercel. Pentru fiecare client:
  1. Creeaza repo nou in organizatia/contul configurat (GITHUB_ORG)
  2. Incarca index.html (si optional alte fisiere)
  3. Activeaza GitHub Pages pentru acel repo
  4. Returneaza URL-ul live: https://{org}.github.io/{repo}/

Respecta rate limiting GitHub cu sleep intre apeluri API si
gestioneaza cazul in care un repo cu acel nume exista deja.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re

import httpx

from config import settings

logger = logging.getLogger(__name__)

_GH_API = "https://api.github.com"

# Pauza intre operatiuni API consecutive — respecta rate limits
# GitHub (5000 req/h pentru token autentificat, dar creare repo +
# Pages activation au limite mai stricte de bursting).
_SLEEP_BETWEEN_CALLS = 2.0
_SLEEP_AFTER_REPO_CREATE = 3.0
_SLEEP_AFTER_PAGES_ENABLE = 2.0


def _gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def slugify_repo_name(business_name: str, job_id: int, market: str) -> str:
    """
    Construieste un nume de repo valid pentru GitHub din numele firmei.
    Ex: 'Novak Instalaterske prace' + id=42 + cz -> 'novak-instalaterske-cz-42'
    Sufixul cu job_id garanteaza unicitate chiar daca numele se repeta.
    """
    base = business_name.encode("ascii", errors="ignore").decode("ascii")
    base = re.sub(r"[^\w\-]", "-", base.lower().strip())
    base = re.sub(r"-{2,}", "-", base).strip("-")
    base = base[:40] or "site"
    return f"{base}-{market}-{job_id}"


async def _check_repo_exists(
    http: httpx.AsyncClient, owner: str, repo: str,
) -> bool:
    r = await http.get(
        f"{_GH_API}/repos/{owner}/{repo}",
        headers=_gh_headers(),
        timeout=15.0,
    )
    return r.status_code == 200


async def _create_repo(
    http: httpx.AsyncClient, org: str, repo: str,
) -> str:
    """
    Creeaza un repo nou in organizatie/cont. Daca exista deja,
    NU arunca eroare — il refoloseste (util la retry-uri dupa
    un job care a picat la pasul de upload).
    Returneaza numele real al repo-ului folosit.

    IMPORTANT: /orgs/{org}/repos functioneaza DOAR pentru
    organizatii GitHub reale, nu pentru conturi personale.
    Daca GITHUB_ORG e setat gresit cu un username personal,
    GitHub raspunde 404 — caz in care cadem automat pe
    /user/repos (functioneaza identic pentru cont personal).
    """
    url = f"{_GH_API}/orgs/{org}/repos" if org else f"{_GH_API}/user/repos"

    r = await http.post(
        url,
        headers=_gh_headers(),
        json={
            "name": repo,
            "description": "Site generat automat — Hybrid King",
            "private": False,
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False,
            "auto_init": True,  # creeaza un README initial (necesar
                                  # pentru ca repo-ul sa aiba o ramura
                                  # implicita inainte de upload fisiere)
        },
        timeout=20.0,
    )

    if r.status_code == 404 and org:
        # org nu e o organizatie GitHub reala — probabil e de fapt
        # username-ul personal. Reincercam pe /user/repos.
        logger.warning(
            "GITHUB_ORG='%s' nu e o organizatie GitHub valida "
            "(404 la /orgs/%s/repos). Incerc /user/repos — "
            "verifica daca '%s' e de fapt contul tau personal, "
            "caz in care poti lasa GITHUB_ORG gol in .env.",
            org, org, org,
        )
        r = await http.post(
            f"{_GH_API}/user/repos",
            headers=_gh_headers(),
            json={
                "name": repo,
                "description": "Site generat automat — Hybrid King",
                "private": False,
                "has_issues": False,
                "has_projects": False,
                "has_wiki": False,
                "auto_init": True,
            },
            timeout=20.0,
        )

    if r.status_code == 201:
        logger.info("Repo creat: %s/%s", org or "(personal)", repo)
        return repo

    if r.status_code == 422:
        # "name already exists on this account" — repo exista deja.
        body = r.text.lower()
        if "already exists" in body or "name already exists" in body:
            logger.warning(
                "Repo '%s' exista deja — il refolosesc.", repo,
            )
            return repo
        raise RuntimeError(f"GitHub 422 la creare repo '{repo}': {r.text[:300]}")

    if r.status_code == 403:
        raise RuntimeError(
            f"GitHub 403 (permisiuni insuficiente sau rate limit) "
            f"la creare repo '{repo}': {r.text[:300]}"
        )

    raise RuntimeError(f"GitHub creare repo {r.status_code}: {r.text[:300]}")


async def _get_file_sha(
    http: httpx.AsyncClient, owner: str, repo: str, path: str,
) -> str | None:
    """Daca fisierul exista deja in repo, GitHub cere SHA-ul lui
    pentru update (PUT). Returneaza None daca fisierul nu exista."""
    r = await http.get(
        f"{_GH_API}/repos/{owner}/{repo}/contents/{path}",
        headers=_gh_headers(),
        timeout=15.0,
    )
    if r.status_code == 200:
        return r.json().get("sha")
    return None


async def _upload_file(
    http: httpx.AsyncClient,
    owner: str,
    repo: str,
    path: str,
    content: str,
    message: str = "Adauga site generat automat",
) -> None:
    """Creeaza/actualizeaza un fisier in repo via Contents API."""
    existing_sha = await _get_file_sha(http, owner, repo, path)

    payload: dict = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }
    if existing_sha:
        payload["sha"] = existing_sha  # necesar pentru update, nu creare

    r = await http.put(
        f"{_GH_API}/repos/{owner}/{repo}/contents/{path}",
        headers=_gh_headers(),
        json=payload,
        timeout=30.0,
    )

    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"GitHub upload '{path}' in '{repo}' a dat {r.status_code}: "
            f"{r.text[:300]}"
        )

    logger.info("Fisier incarcat: %s/%s/%s", owner, repo, path)


async def _enable_pages(
    http: httpx.AsyncClient, owner: str, repo: str,
) -> None:
    """
    Activeaza GitHub Pages pentru repo, servind din branch-ul
    principal ('main' sau 'master'), folder radacina ('/').
    Daca Pages e deja activat (409), e tratat ca succes.
    """
    # Detecteaza branch-ul implicit (poate fi 'main' sau 'master')
    r_repo = await http.get(
        f"{_GH_API}/repos/{owner}/{repo}",
        headers=_gh_headers(),
        timeout=15.0,
    )
    default_branch = "main"
    if r_repo.status_code == 200:
        default_branch = r_repo.json().get("default_branch", "main")

    r = await http.post(
        f"{_GH_API}/repos/{owner}/{repo}/pages",
        headers=_gh_headers(),
        json={
            "source": {"branch": default_branch, "path": "/"},
        },
        timeout=20.0,
    )

    if r.status_code in (201, 204):
        logger.info("GitHub Pages activat pentru %s/%s (branch=%s)",
                    owner, repo, default_branch)
        return

    if r.status_code == 409:
        # Pages deja activat — normal la retry-uri
        logger.info("GitHub Pages era deja activat pentru %s/%s",
                    owner, repo)
        return

    raise RuntimeError(
        f"GitHub Pages activare {r.status_code} pentru '{repo}': "
        f"{r.text[:300]}"
    )


# ═══════════════════════════════════════════════════════════
# Functia principala — apelata din orchestrator.py
# ═══════════════════════════════════════════════════════════

async def deploy_to_github_pages(
    http: httpx.AsyncClient,
    business_name: str,
    job_id: int,
    market: str,
    index_html: str,
) -> tuple[str, str]:
    """
    Creeaza repo + incarca site + activeaza Pages.
    Returneaza (url_live, nume_repo).

    market: 'cz' sau 'ro' — folosit doar pentru numele repo-ului,
    nu afecteaza logica de deploy.
    """
    if not settings.github_token:
        raise RuntimeError(
            "GITHUB_TOKEN nu este setat in .env — "
            "necesar pentru deploy pe GitHub Pages."
        )

    org = settings.github_org.strip()
    owner = org or "(personal)"  # doar pentru loguri

    repo = slugify_repo_name(business_name, job_id, market)

    # ── Pas 1: creare repo ────────────────────────────────
    repo = await _create_repo(http, org, repo)
    await asyncio.sleep(_SLEEP_AFTER_REPO_CREATE)

    # Determina owner real pentru apelurile urmatoare (orgs vs user)
    real_owner = org
    if not real_owner:
        r_user = await http.get(
            f"{_GH_API}/user", headers=_gh_headers(), timeout=15.0,
        )
        real_owner = r_user.json().get("login", "")

    # ── Pas 2: upload index.html ───────────────────────────
    await _upload_file(http, real_owner, repo, "index.html", index_html)
    await asyncio.sleep(_SLEEP_BETWEEN_CALLS)

    # ── Pas 3: activare GitHub Pages ───────────────────────
    await _enable_pages(http, real_owner, repo)
    await asyncio.sleep(_SLEEP_AFTER_PAGES_ENABLE)

    url = f"https://{real_owner}.github.io/{repo}/"
    logger.info("Deploy complet: %s -> %s", business_name, url)
    return url, repo
