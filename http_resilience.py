"""
http_resilience.py — Helpers comuni pentru toate scrapere-le
================================================================
Implementeaza:
  - retry cu backoff exponential pe erori temporare (5xx, 429, timeout)
  - circuit breaker: dupa N erori consecutive de la o sursa,
    sare automat peste restul cererilor catre acea sursa
  - logging structurat al ce-a esuat si de ce
"""
from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
# Circuit breaker per sursa
# ════════════════════════════════════════════════════════════

@dataclass
class CircuitBreaker:
    """
    Daca o sursa esueaza de N ori la rand, deschide circuitul
    si urmatoarele apeluri esueaza imediat (fara cerere reala),
    pana cand cooldown-ul trece. Asta protejeaza scraperul sa
    nu continue sa loveasca o sursa blocata/picata.
    """
    source_name: str
    threshold: int = 5         # cate erori consecutive declanseaza
    cooldown_sec: float = 300  # 5 minute pauza dupa deschidere
    failures: int = 0
    opened_at: float = 0.0

    def record_success(self) -> None:
        if self.failures > 0:
            logger.info("[%s] circuit reset (succes dupa %d esecuri)",
                        self.source_name, self.failures)
        self.failures = 0
        self.opened_at = 0.0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold and self.opened_at == 0.0:
            self.opened_at = asyncio.get_event_loop().time()
            logger.error(
                "[%s] CIRCUIT DESCHIS dupa %d esecuri consecutive — "
                "pauza %ds inainte de retry",
                self.source_name, self.failures, int(self.cooldown_sec),
            )

    def is_open(self) -> bool:
        if self.opened_at == 0.0:
            return False
        elapsed = asyncio.get_event_loop().time() - self.opened_at
        if elapsed >= self.cooldown_sec:
            logger.info("[%s] cooldown expirat, incerc reconectare",
                        self.source_name)
            self.opened_at = 0.0
            self.failures = 0
            return False
        return True


# ════════════════════════════════════════════════════════════
# Fetch cu retry + backoff exponential
# ════════════════════════════════════════════════════════════

# Status codes care merita retry (probabil temporar)
_RETRY_STATUS = {429, 500, 502, 503, 504}


async def fetch_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict | None = None,
    timeout: float = 20.0,
    max_retries: int = 3,
    base_delay: float = 1.5,
    breaker: CircuitBreaker | None = None,
    params: dict | None = None,
) -> httpx.Response | None:
    """
    Face GET cu retry pe erori temporare. Returneaza None daca
    toate retry-urile esueaza sau daca circuit breaker-ul e
    deschis.

    Backoff: base_delay * (2^attempt) + jitter random.
    Asta evita "thundering herd" — daca server-ul revine,
    cererile retry-uite nu pleaca toate in aceeasi milisecunda.
    """
    if breaker and breaker.is_open():
        logger.debug("[%s] cerere SARITA (circuit deschis): %s",
                     breaker.source_name, url[:80])
        return None

    last_exc: Exception | None = None

    for attempt in range(max_retries):
        try:
            r = await client.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
                follow_redirects=True,
            )

            if r.status_code in _RETRY_STATUS:
                # 429/5xx — eroare temporara, merita retry
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1.0)
                logger.warning(
                    "HTTP %d la %s — astept %.1fs (incercare %d/%d)",
                    r.status_code, url[:80], delay, attempt + 1, max_retries,
                )
                if breaker:
                    breaker.record_failure()
                await asyncio.sleep(delay)
                continue

            if r.status_code == 404:
                # Pagina nu exista — nu retrying, dar nici eroare grava
                logger.debug("HTTP 404 la %s — sar peste", url[:80])
                return None

            r.raise_for_status()  # 4xx altele (403 etc) -> exceptie

            if breaker:
                breaker.record_success()
            return r

        except (httpx.TimeoutException, httpx.NetworkError,
                httpx.RemoteProtocolError) as exc:
            last_exc = exc
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1.0)
            logger.warning(
                "Eroare retea la %s: %s — astept %.1fs (incercare %d/%d)",
                url[:80], type(exc).__name__, delay,
                attempt + 1, max_retries,
            )
            if breaker:
                breaker.record_failure()
            await asyncio.sleep(delay)

        except httpx.HTTPStatusError as exc:
            # 403, 401, etc — nu retry pe astea, sunt definitive
            logger.error("HTTP %d (definitiv) la %s",
                         exc.response.status_code, url[:80])
            if breaker:
                breaker.record_failure()
            return None

    logger.error("Toate %d incercari au esuat pentru %s (ultima: %s)",
                 max_retries, url[:80],
                 type(last_exc).__name__ if last_exc else "?")
    return None
