"""
scraper_ro.py — OLX Romania Lead Scraper
==========================================
Structura identica cu scraper_b2c.py dar pentru olx.ro.

FLOW:
  Browse categorii OLX servicii -> colecteaza URL-uri listing
  -> viziteaza pagina de detaliu -> extrage titlu + telefon
  -> incearca OLX API /limited-phones/ ca fallback
  -> insereaza in DB cu language="Romanian"
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup, Tag
from sqlalchemy import select

from database import AsyncSessionLocal, Job, JobStatus, init_db

logger = logging.getLogger(__name__)

_OLX_BASE = "https://www.olx.ro"

# Categorii OLX servicii (URL paths directe)
_OLX_SERVICE_PATHS: list[str] = [
    "/servicii/reparatii/",
    "/servicii/constructii/",
    "/servicii/curatenie/",
    "/servicii/instalatii-sanitare/",
    "/servicii/electrice/",
    "/servicii/gradinarit-agricultura/",
    "/servicii/transport-mutari/",
    "/servicii/tamplarie-geamuri/",
]

# Pattern URL listing OLX Romania (doua formate posibile)
_LISTING_RE = re.compile(
    r"/(?:oferta|d/oferta)/[a-zA-Z0-9\-_]+-ID[a-zA-Z0-9]+\.html"
    r"|/(?:oferta|d/oferta)/[a-zA-Z0-9\-_]+-\d+\.html"
    r"|/(?:oferta|d/oferta)/[^\"'\s?#]+"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
    "DNT": "1",
}

# ================================================================
# Telefoane Romania: +40 7xx xxx xxx (11 cifre cu prefix)
# ================================================================

_PHONE_RE_RO = re.compile(
    r"(?:\+?40[\s\-]?)?"           # prefix optional +40 sau 40
    r"0?"                           # 0 optional inainte de 7
    r"7[0-9]{2}"                    # 7xx
    r"[\s\-\.]?"
    r"[0-9]{3}"
    r"[\s\-\.]?"
    r"[0-9]{3}"
)


def _norm_ro(raw: str) -> str:
    """Normalizeaza la +40xxxxxxxxx"""
    d = re.sub(r"[^\d]", "", raw).lstrip("0")
    if d.startswith("40"):
        pass
    elif d.startswith("7"):
        d = "40" + d
    else:
        d = "40" + d
    return "+" + d


def _valid_ro(phone: str) -> bool:
    """Valideaza numar mobil roman: +40 7xx xxx xxx"""
    d = re.sub(r"[^\d]", "", phone)
    return len(d) == 11 and d.startswith("40") and d[2] == "7"


def _find_phone_ro(text: str) -> str | None:
    """
    Cauta in text un nr de telefon mobil RO valid. Foloseste
    validatorul strict din phone_validator.py — refuza numere
    cu prefixe necunoscute, secvente fake, sau format incorect.
    """
    from phone_validator import normalize_ro_phone
    for m in _PHONE_RE_RO.finditer(text):
        candidate = normalize_ro_phone(m.group(0))
        if candidate:
            return candidate
    return None


# ================================================================
# Lead dataclass
# ================================================================

@dataclass
class Lead:
    business_name: str
    phone_number: str
    niche: str
    url: str
    seller_name: str | None = None
    description: str = ""
    is_promoted: bool = False


# ================================================================
# Toate 42 judete RO + Bucuresti -> slug judet pentru OLX
# Pattern URL CONFIRMAT prin cautare reala:
#   /servicii-afaceri-colaborari/{judet}-judet/q-{termen}/
# Slug-urile sunt nume judet, lowercase, fara diacritice.
# 2 confirmate live (brasov-judet, bihor-judet).
# Restul = aceeasi conventie consistenta, incredere ridicata.
# Bucuresti = NEVERIFICAT (nu are judet propriu-zis).
# ================================================================

_CITIES_RO_OLX: dict[str, str] = {
    # Top 20 cele mai mari orase RO (impact maxim, volum gestionabil)
    # Folosesc judet-slug ca pana acum
    "Bucuresti":         "bucuresti",        # CONFIRMAT (URL fara sufix -judet)
    "Cluj-Napoca":       "cluj",
    "Timisoara":         "timis",
    "Iasi":              "iasi",
    "Constanta":         "constanta",
    "Craiova":           "dolj",
    "Brasov":            "brasov",            # CONFIRMAT
    "Galati":            "galati",
    "Ploiesti":          "prahova",
    "Oradea":            "bihor",             # CONFIRMAT
    "Braila":            "braila",
    "Arad":              "arad",
    "Pitesti":           "arges",
    "Sibiu":             "sibiu",
    "Bacau":             "bacau",
    "Targu Mures":       "mures",
    "Baia Mare":         "maramures",
    "Buzau":             "buzau",
    "Botosani":          "botosani",
    "Satu Mare":         "satu-mare",
}


# ================================================================
# Niches Romania — cautare prin termeni in categoria Servicii
# Categoria reala OLX.ro: /servicii-afaceri-colaborari/
# (NU /servicii/... — acea cale NU exista, de-aici erorile 404)
# Pattern verificat: /servicii-afaceri-colaborari/q-{termen}/
# ================================================================

_NICHES_RO: list[tuple[str, list[str]]] = [
    # Nise existente
    ("Zugravit si vopsit",
        ["zugrav", "vopsitorie", "zugraveli"]),
    ("Instalatii sanitare",
        ["instalator", "instalatii sanitare", "instalatii termice"]),
    ("Electrician",
        ["electrician", "instalatii electrice"]),
    ("Constructii si renovari",
        ["constructii", "renovari", "amenajari interioare"]),
    ("Servicii curatenie",
        ["curatenie", "firma curatenie"]),
    ("Gradinarit",
        ["gradinar", "intretinere gradina", "tuns gazon"]),
    ("Mutari si transport",
        ["mutari", "transport marfa", "relocari"]),
    ("Tamplarie si acoperisuri",
        ["tamplar", "acoperisuri", "dulgherie"]),

    # Nise noi adaugate (12 noi -> total 30)
    ("Faianta si gresie",
        ["faiantar", "montaj faianta", "montaj gresie"]),
    ("Parchet si rigips",
        ["parchet", "montaj parchet", "rigips", "gips carton"]),
    ("Termopane si usi",
        ["termopane", "montaj termopane", "usi metalice"]),
    ("Lacatuserie",
        ["lacatus", "lacatuserie", "deblocari yale"]),
    ("Reparatii electrocasnice",
        ["reparatii frigidere", "reparatii masini spalat", "service electrocasnice"]),
    ("Aer conditionat",
        ["montaj aer conditionat", "service aer conditionat", "climatizare"]),
    ("Service auto",
        ["reparatii auto", "service auto", "tinichigerie"]),
    ("Demolari si excavatii",
        ["demolari", "excavatii", "miniexcavator"]),
    ("Fotograf si video",
        ["fotograf nunta", "filmare evenimente"]),
    ("Catering si food",
        ["catering", "catering evenimente"]),

    # Nise suplimentare (total 30)
    ("Curatare canalizari",
        ["desfundare canalizare", "vidanjare", "destupare wc"]),
    ("Solar si fotovoltaice",
        ["panouri fotovoltaice", "instalare panouri", "solar"]),
    ("Cosmetica si frumusete",
        ["cosmetician", "manichiura domiciliu", "epilare"]),
    ("Masaj si terapii",
        ["masaj terapeutic", "kinetoterapeut", "fizioterapie"]),
    ("Cursuri si meditatii",
        ["meditatii matematica", "cursuri", "lectii particulare"]),
    ("Reparatii telefoane",
        ["reparatii telefoane", "service mobil"]),
    ("Frizerie si coafor",
        ["frizer domiciliu", "coafor"]),
    ("Veterinar si pets",
        ["veterinar domiciliu", "tuns animale", "dresaj caini"]),
    ("Curatatorie chimica",
        ["curatatorie haine", "curatat covoare"]),
    ("Service IT",
        ["reparatii calculatoare", "service laptop", "instalare windows"]),
    ("Dezinsectie",
        ["dezinsectie", "deratizare", "dezinfectie"]),
    ("Sticla si oglinzi",
        ["geamuri sticla", "montaj oglinzi"]),
]


def _slugify_query(term: str) -> str:
    """Transforma un termen de cautare in slug pentru URL OLX.
    Ex: 'instalatii sanitare' -> 'instalatii-sanitare'"""
    s = term.strip().lower()
    s = re.sub(r"[^\w\s\-]", "", s)
    s = re.sub(r"\s+", "-", s)
    return s


# ================================================================
# Extrage URL-uri listing de pe pagina categorie OLX
# ================================================================

def _get_listing_urls(html: str, base_url: str = "") -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    results: list[str] = []
    seen: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        if not isinstance(a_tag, Tag):
            continue
        href = str(a_tag.get("href", ""))

        # Curata query params si anchore
        href_clean = href.split("?")[0].split("#")[0]

        if not _LISTING_RE.search(href_clean):
            continue

        if href_clean.startswith("http"):
            url = href_clean
        elif href_clean.startswith("/"):
            url = _OLX_BASE + href_clean
        else:
            continue

        # Asigura ca e OLX Romania
        if "olx.ro" not in url:
            continue

        # Exclude pagini de cautare/categorii
        if any(p in url for p in ["/oferte/", "/oferta-ta/", "/cont/"]):
            continue

        if url in seen:
            continue

        seen.add(url)
        results.append(url)

    logger.info("Gasit %d URL-uri listing pe pagina", len(results))
    return results


# ================================================================
# Extrage titlu + telefon din pagina listing OLX
# ================================================================

def _parse_olx_detail(html: str) -> tuple[str, str | None, str | None, str, bool]:
    """
    Parseaza pagina de detaliu OLX Romania.
    Returneaza (titlu, telefon_sau_None, seller_name, description, is_promoted).
    """
    soup = BeautifulSoup(html, "lxml")

    # ── Titlu ─────────────────────────────────────────────
    title = "Prestator servicii"

    h1 = soup.find("h1")
    if h1 and isinstance(h1, Tag):
        t = h1.get_text(strip=True)
        if t and len(t) > 3:
            title = t

    if title == "Prestator servicii":
        og_title = soup.find("meta", {"property": "og:title"})
        if og_title and isinstance(og_title, Tag):
            c = og_title.get("content", "")
            if c:
                title = str(c).strip()

    # ── Telefon — Metoda 1: tel: link ─────────────────────
    phone: str | None = None

    for a in soup.find_all("a", href=True):
        href = str(a.get("href", ""))
        if href.startswith("tel:"):
            raw = href.replace("tel:", "").strip()
            p = _find_phone_ro(raw)
            if p:
                phone = p
                break

    # ── Telefon — Metoda 2: __NEXT_DATA__ JSON (Next.js) ──
    seller_name: str | None = None
    description: str = ""

    nd_tag = soup.find("script", id="__NEXT_DATA__")
    ad_data: dict = {}
    if nd_tag and isinstance(nd_tag, Tag):
        try:
            nd_str = nd_tag.string or ""
            if not phone:
                p = _find_phone_ro(nd_str)
                if p:
                    phone = p
            data = json.loads(nd_str)
            ad_data = (data.get("props", {})
                            .get("pageProps", {})
                            .get("ad", {}))
        except (json.JSONDecodeError, AttributeError, KeyError):
            ad_data = {}

    if ad_data and not phone:
        for field in ["phone", "phoneNumber", "contact_phone",
                      "seller_phone", "user_phone"]:
            val = ad_data.get(field, "")
            if val:
                p = _find_phone_ro(str(val))
                if p:
                    phone = p
                    break

    # Nume vanzator — NEVERIFICAT live, incercam cele mai
    # probabile chei JSON pe baza convetiilor OLX/Next.js.
    if ad_data:
        user_obj = ad_data.get("user") or ad_data.get("seller") or {}
        if isinstance(user_obj, dict):
            seller_name = (
                user_obj.get("name")
                or user_obj.get("displayName")
                or user_obj.get("username")
            )
        if not seller_name:
            seller_name = ad_data.get("sellerName") or ad_data.get("user_name")

        # Descriere anunt
        description = str(
            ad_data.get("description")
            or ad_data.get("body")
            or ""
        ).strip()

    # ── Telefon — Metoda 3: data-phone / data-cy atribute ─
    if not phone:
        for attr in ["data-phone", "data-cy"]:
            for el in soup.find_all(attrs={attr: True}):
                if not isinstance(el, Tag):
                    continue
                raw = str(el.get(attr, ""))
                p = _find_phone_ro(raw)
                if p:
                    phone = p
                    break
            if phone:
                break

    # ── Telefon — Metoda 4: keywords in text ──────────────
    if not phone:
        full = soup.get_text(" ", strip=True)
        for kw in ["telefon", "tel.", "tel:", "mobil", "apeleaza",
                   "suna", "contact"]:
            idx = full.lower().find(kw)
            if idx >= 0:
                snippet = full[max(0, idx - 5): idx + 80]
                p = _find_phone_ro(snippet)
                if p:
                    phone = p
                    break

    # ── Telefon — Metoda 5: scan complet pagina ───────────
    if not phone:
        phone = _find_phone_ro(soup.get_text(" ", strip=True))

    # ── Descriere fallback: daca nu am gasit-o in JSON,
    # folosim textul complet al paginii, trunchiat ──────────
    if not description:
        description = soup.get_text(" ", strip=True)[:1500]

    # ── Anunt promovat — CONFIRMAT prin documentatia oficiala
    # OLX: anunturile promovate au textul "Anunt Promovat"
    # evidentiat cu alta culoare in lista de anunturi.
    is_promoted = bool(re.search(r"anun[tţ]\s*promovat", html, re.IGNORECASE))

    return title, phone, seller_name, description.strip()[:1500], is_promoted


# ================================================================
# OLX Romania API /limited-phones/ (fallback)
# ================================================================

async def _fetch_phone_olx_api(
    client: httpx.AsyncClient,
    listing_url: str,
) -> str | None:
    """
    Incearca OLX API sa obtina telefonul.
    Format URL listing: /oferta/{slug}-ID{offer_id}.html
                     sau /oferta/{slug}-{id}.html
    """
    # Extrage ID-ul ofertei din URL
    m = (
        re.search(r"-ID([A-Za-z0-9]+)\.html", listing_url)
        or re.search(r"-(\d{5,})(?:\.html)?$", listing_url)
    )
    if not m:
        return None

    offer_id = m.group(1)

    try:
        r = await client.get(
            f"{_OLX_BASE}/api/v1/offers/{offer_id}/limited-phones/",
            headers={
                **_HEADERS,
                "Accept": "application/json",
                "Referer": listing_url,
            },
            timeout=10.0,
        )
        if r.is_success:
            data = r.json()
            phones_list = data.get("data", [])
            if isinstance(phones_list, list) and phones_list:
                raw = str(phones_list[0].get("phone", ""))
                p = _find_phone_ro(raw)
                if p:
                    logger.debug("OLX API phone OK: %s", p)
                    return p
    except Exception as exc:
        logger.debug("OLX API /limited-phones/ eroare %s: %s",
                     offer_id, exc)

    return None


# ================================================================
# Scrape o nisa
# ================================================================

async def _scrape_niche_ro(
    client: httpx.AsyncClient,
    niche: str,
    terms: list[str],
    target: int,
) -> list[Lead]:
    leads: list[Lead] = []
    seen_phones: set[str] = set()
    seen_urls: set[str] = set()

    for term in terms:
        if len(leads) >= target:
            break

        slug = _slugify_query(term)
        search_url = f"{_OLX_BASE}/servicii-afaceri-colaborari/q-{slug}/"

        # Colecteaza URL-uri din mai multe pagini de rezultate
        for page in range(1, 6):  # pana la 5 pagini per termen
            if len(seen_urls) >= target * 4:
                break

            params: dict[str, str] = {}
            if page > 1:
                params["page"] = str(page)

            try:
                r = await client.get(
                    search_url,
                    params=params,
                    headers=_HEADERS,
                    timeout=20.0,
                    follow_redirects=True,
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                logger.error("Eroare cautare OLX term='%s' pagina %d: %s",
                             term, page, exc)
                break

            page_urls = _get_listing_urls(r.text)
            if not page_urls:
                logger.info("Nicio listare term='%s' pagina %d", term, page)
                break

            new_urls = [u for u in page_urls if u not in seen_urls]
            seen_urls.update(new_urls)
            logger.info("nisa='%s' term='%s' pagina=%d -> %d URL-uri noi",
                        niche, term, page, len(new_urls))

            await asyncio.sleep(2.5)

        await asyncio.sleep(1.5)

    logger.info("nisa='%s': %d URL-uri de vizitat", niche, len(seen_urls))

    # ── Statistici diagnostic pe nisa ──────────────────────
    visited_count = 0
    no_phone_count = 0
    api_recovered_count = 0

    # Viziteaza fiecare listing
    for url in list(seen_urls):
        if len(leads) >= target:
            break

        try:
            dr = await client.get(
                url,
                headers=_HEADERS,
                timeout=15.0,
                follow_redirects=True,
            )
            dr.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("Eroare detaliu OLX %s: %s", url, exc)
            await asyncio.sleep(1.0)
            continue

        title, phone, seller_name, description, is_promoted = _parse_olx_detail(dr.text)
        visited_count += 1

        # DIAGNOSTIC: loghez ce-am gasit ca sa pot diagnostica de ce
        # rata de extractie e 0 in unele zile. Daca title e gol sau
        # phone e None pe TOATE URL-urile, structura OLX s-a schimbat.
        logger.debug("PARSE [%s]: title=%r phone=%r promoted=%s",
                     url.rsplit('/', 2)[-2] if '/' in url else 'unknown',
                     title[:40] if title else None,
                     phone, is_promoted)

        # Fallback: incearca API daca nu am gasit telefon in HTML
        if not phone:
            phone = await _fetch_phone_olx_api(client, url)
            if phone:
                api_recovered_count += 1
                logger.info("API fallback a recuperat telefon: %s pentru %s",
                            phone, url[:80])
            else:
                no_phone_count += 1

        if phone and phone not in seen_phones:
            seen_phones.add(phone)
            leads.append(Lead(
                business_name=title,
                phone_number=phone,
                niche=niche,
                url=url,
                seller_name=seller_name,
                description=description,
                is_promoted=is_promoted,
            ))
            logger.info("LEAD_RO [%s]: '%s' -> %s%s",
                        niche[:20], title[:40], phone,
                        " [PROMOVAT]" if is_promoted else "")
        else:
            if not phone:
                logger.debug("Niciun telefon: %s", url)

        await asyncio.sleep(1.5)

    # ── Sumar diagnostic la finalul nisei ──────────────────
    # Asta arata IMEDIAT daca:
    # - visited=49, leads=0, no_phone=49 -> OLX a schimbat structura
    # - visited=49, leads=0, api_recovered=15 -> validatorul taie prea mult
    # - visited=49, leads=5, no_phone=44 -> normal, multe anunturi nu au tel
    logger.info(
        "DIAG [%s]: vizitate=%d -> lead-uri=%d, fara_telefon=%d, api_recuperat=%d",
        niche, visited_count, len(leads), no_phone_count, api_recovered_count,
    )

    return leads[:target]


# ================================================================
# NOU: cautare pe 15 orase (judete) — DOAR anunturi promovate
# Functionalitatea existenta (_scrape_niche_ro) ramane neschimbata;
# asta e un pas ADITIONAL, separat.
# Pattern CONFIRMAT: /servicii-afaceri-colaborari/{judet}-judet/q-{termen}/
# ATENTIE: OLX are protectie anti-bot reala (confirmat printr-un
# mesaj de blocare gasit live) — volum mai mare = risc mai mare
# de blocare. Daca apar erori repetate, redu frecventa/numarul
# de orase verificate per rulare.
# ================================================================

async def _scrape_niche_cities_promoted_ro(
    client: httpx.AsyncClient,
    niche: str,
    terms: list[str],
    seen_phones_global: set[str],
) -> list[Lead]:
    promoted_leads: list[Lead] = []

    for city_name, judet_slug in _CITIES_RO_OLX.items():
        for term in terms:
            slug = _slugify_query(term)
            # CONFIRMAT prin cautare live:
            # - Judetele folosesc /{judet}-judet/q-{slug}/
            # - Bucuresti foloseste /{bucuresti}/q-{slug}/ (FARA -judet)
            #   pentru ca nu e judet propriu-zis, e unitate administrativa separata
            if judet_slug == "bucuresti":
                url = (f"{_OLX_BASE}/servicii-afaceri-colaborari/"
                       f"{judet_slug}/q-{slug}/")
            else:
                url = (f"{_OLX_BASE}/servicii-afaceri-colaborari/"
                       f"{judet_slug}-judet/q-{slug}/")

            try:
                r = await client.get(
                    url, headers=_HEADERS, timeout=20.0,
                    follow_redirects=True,
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                logger.error(
                    "[%s] Eroare oras='%s' term='%s': %s",
                    niche, city_name, term, exc,
                )
                await asyncio.sleep(2.0)
                continue

            listing_urls = _get_listing_urls(r.text)
            if not listing_urls:
                await asyncio.sleep(1.5)
                continue

            logger.info(
                "[%s] oras='%s' term='%s' -> %d URL-uri de verificat (promovat?)",
                niche, city_name, term, len(listing_urls),
            )

            for listing_url in listing_urls:
                try:
                    dr = await client.get(
                        listing_url, headers=_HEADERS, timeout=15.0,
                        follow_redirects=True,
                    )
                    dr.raise_for_status()
                except httpx.HTTPError:
                    await asyncio.sleep(1.0)
                    continue

                title, phone, seller_name, description, is_promoted = \
                    _parse_olx_detail(dr.text)

                if not is_promoted:
                    continue  # filtrare stricta — doar promovate

                if not phone:
                    phone = await _fetch_phone_olx_api(client, listing_url)

                if not phone or phone in seen_phones_global:
                    continue

                seen_phones_global.add(phone)
                promoted_leads.append(Lead(
                    business_name=title,
                    phone_number=phone,
                    niche=niche,
                    url=listing_url,
                    seller_name=seller_name,
                    description=description,
                    is_promoted=True,
                ))
                logger.info(
                    "LEAD_TOP_RO [%s] oras='%s': '%s' -> %s",
                    niche[:20], city_name, title[:40], phone,
                )

                await asyncio.sleep(1.5)

            await asyncio.sleep(2.0)

    return promoted_leads


# ================================================================
# Insert in DB
# ================================================================

async def _insert_ro(leads: list[Lead]) -> int:
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
                seller_name=lead.seller_name,
                phone_number=lead.phone_number,
                niche=lead.niche,
                description=lead.description,
                language="Romanian",
                status=JobStatus.SCRAPED,
            ))
            n += 1
        await session.commit()
    return n


# ================================================================
# Main
# ================================================================

async def run_scraper_ro(total: int = 200) -> None:
    await init_db()
    per_niche = max(5, total // len(_NICHES_RO))
    logger.info("Scraper RO: %d nise x %d = %d target",
                len(_NICHES_RO), per_niche, total)

    grand = 0
    seen_phones_global: set[str] = set()

    async with httpx.AsyncClient(
        http2=False,
        follow_redirects=True,
        timeout=30.0,
    ) as client:
        for niche, terms in _NICHES_RO:
            logger.info("=== RO: %s ===", niche)
            leads = await _scrape_niche_ro(client, niche, terms, per_niche)

            # De-duplicare
            seen: set[str] = set()
            unique = [l for l in leads
                      if not (l.phone_number in seen
                              or seen.add(l.phone_number))]  # type: ignore

            inserted = await _insert_ro(unique)
            grand += inserted
            logger.info("RO %s: gasit=%d inserat=%d",
                        niche, len(unique), inserted)

            # NOU: cautare suplimentara pe 15 orase, DOAR promovate
            logger.info("=== RO: %s (15 orase, DOAR promovate) ===", niche)
            for l in unique:
                seen_phones_global.add(l.phone_number)
            promoted = await _scrape_niche_cities_promoted_ro(
                client, niche, terms, seen_phones_global,
            )
            inserted_promoted = await _insert_ro(promoted)
            grand += inserted_promoted
            logger.info("RO %s [PROMOVAT pe orase]: gasit=%d inserat=%d",
                        niche, len(promoted), inserted_promoted)

            await asyncio.sleep(4.0)

    logger.info("DONE RO. Total lead-uri noi: %d", grand)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(run_scraper_ro(total=200))
