"""
chatbot_server.py — Backend chatbot pentru site-urile clientilor
===================================================================
Site-urile sunt fisiere statice pe GitHub Pages — orice JS de pe
pagina e vizibil public. NU putem pune cheia Groq in JavaScript
(oricine ar putea sa o fure din "View Source").

Acest server tine cheia Groq SECRETA pe server-ul tau, iar
widget-ul de chat din site vorbeste cu acest server, nu direct
cu Groq.

Flow:
  Vizitator pe site -> widget chat -> POST /chat/{job_id}
  -> server citeste din DB datele firmei (nume, nisa, descriere)
  -> construieste prompt + apeleaza Groq -> returneaza raspunsul

RATE LIMITING: fara protectie, oricine ar putea sa bombardeze
endpoint-ul si sa-ti consume cota Groq pe TOATE site-urile, nu
doar pe unul. Limitam per-IP (sliding window simplu, in memorie —
suficient pentru volumul tau, nu necesita Redis).

RULARE:
    pip install fastapi uvicorn --break-system-packages
    python chatbot_server.py
    # sau in productie: uvicorn chatbot_server:app --host 127.0.0.1 --port 8420

EXPUNERE HTTPS: vezi instructiunile Caddy din raspunsul asociat —
serverul asta ruleaza pe localhost, Caddy face proxy HTTPS catre el.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import AsyncSessionLocal, Job
from groq_rotator import get_rotator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chatbot_server")

app = FastAPI(title="Hybrid King Chatbot Backend")

# ── CORS: in productie, restrange la domeniile tale GitHub Pages ──
# Momentan permisiv (*.github.io) — daca vrei mai strict, inlocuieste
# cu lista explicita de domenii.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.github\.io",
    allow_methods=["POST"],
    allow_headers=["*"],
)

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_MODEL = "llama-3.1-8b-instant"  # model rapid, suficient pentru FAQ simplu

# ── Rate limiting per IP — sliding window in memorie ──────────────
_RATE_LIMIT_MAX = 15        # max 15 mesaje
_RATE_LIMIT_WINDOW = 60     # per 60 secunde
_request_log: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(client_ip: str) -> bool:
    """Returneaza True daca cererea e permisa, False daca a depasit limita."""
    now = time.monotonic()
    log = _request_log[client_ip]

    # Scoate cererile mai vechi de fereastra
    while log and now - log[0] > _RATE_LIMIT_WINDOW:
        log.popleft()

    if len(log) >= _RATE_LIMIT_MAX:
        return False

    log.append(now)
    return True


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


def _build_system_prompt(job: Job) -> str:
    """Construieste promptul de sistem din datele reale ale firmei."""
    desc = (job.description or "").strip()[:500]
    lang_instr = {
        "Czech": "Odpovidej VZDY v cestine.",
        "Romanian": "Raspunde INTOTDEAUNA in limba romana.",
    }.get(job.language, "Always answer in English.")

    return (
        f"Jsi asistent na webu firmy '{job.business_name}' "
        f"({job.niche}). {lang_instr}\n\n"
        f"Informace o firme:\n{desc or '(zadny popis)'}\n"
        f"Telefon firmy: {job.phone_number}\n\n"
        "PRAVIDLA:\n"
        "- Odpovidej strucne (max 3 vety).\n"
        "- NIKDY nevymyslej ceny, terminy ani zaruky, ktere nejsou "
        "v informaci o firme.\n"
        "- Pokud nevis odpoved, doporuc kontaktovat firmu primo "
        f"na telefon {job.phone_number} nebo WhatsApp.\n"
        "- Bud zdvorily a profesionalni.\n"
        "- Nikdy se nevydavej za cloveka, jsi AI asistent.\n"
    )


async def _call_groq_chat(system_prompt: str, user_message: str) -> str:
    rotator = get_rotator()

    for attempt in range(rotator.count):
        key = rotator.current
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                _GROQ_URL,
                json={
                    "model": _MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message[:500]},
                    ],
                    "temperature": 0.4,
                    "max_tokens": 200,
                },
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )

            if r.status_code == 429:
                logger.warning("Groq 429 pe slot %d — rotesc cheia", attempt)
                rotator.rotate()
                await asyncio.sleep(1)
                continue

            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()

    raise RuntimeError("Toate cheile Groq sunt rate-limited momentan")


@app.post("/chat/{job_id}", response_model=ChatResponse)
async def chat(job_id: int, payload: ChatRequest, request: Request) -> ChatResponse:
    client_ip = request.client.host if request.client else "unknown"

    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Prea multe mesaje. Asteapta un minut.",
        )

    if not payload.message or len(payload.message.strip()) < 1:
        raise HTTPException(status_code=400, detail="Mesaj gol.")

    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Site invalid.")

    if not job.chatbot_enabled:
        raise HTTPException(status_code=403, detail="Chatbot dezactivat pentru acest site.")

    system_prompt = _build_system_prompt(job)

    try:
        reply = await _call_groq_chat(system_prompt, payload.message)
    except Exception as exc:
        logger.error("Eroare Groq pentru job %d: %s", job_id, exc)
        fallback = {
            "Czech": f"Omlouvame se, momentalne nemohu odpovedet. "
                     f"Zavolejte prosim na {job.phone_number}.",
            "Romanian": f"Ne pare rau, nu pot raspunde momentan. "
                        f"Va rugam sunati la {job.phone_number}.",
        }.get(job.language, f"Sorry, please call {job.phone_number}.")
        return ChatResponse(reply=fallback)

    return ChatResponse(reply=reply)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8420)
