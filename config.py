"""
config.py — Hybrid King Settings
==================================
Reads from .env file via pydantic-settings.

IMPORTANT: pydantic-settings incarca .env DOAR in obiectul `settings`,
NU si in os.environ. Dar main.py, groq_rotator.py si orchestrator.py
(MARKET) folosesc os.getenv() direct. De-aceea incarcam .env explicit
aici, o singura data, ca toate variabilele sa fie vizibile peste tot.
"""
from __future__ import annotations

from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# override=False: nu suprascrie variabile deja setate manual
# (ex: MARKET / DATABASE_URL, pe care main.py le seteaza inainte
# de acest import).
load_dotenv(override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Database ──────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./hybridking.db"

    # ── Groq AI ───────────────────────────────────────────
    # O singura cheie (legacy) SAU lista separata cu virgula:
    groq_api_key: str = ""
    groq_api_keys: str = ""   # Ex: "gsk_key1,gsk_key2,gsk_key3"

    # ── GitHub Pages deployment (inlocuieste Vercel) ──────
    github_token: str = ""
    # coredigital-cz e cont PERSONAL, nu organizatie GitHub reala.
    # Codul cade automat pe /user/repos daca detecteaza asta (404
    # la /orgs/.../repos), dar e mai curat sa fie corect din start.
    github_org: str = "coredigital-cz"

    # ── Chatbot backend (vezi chatbot_server.py) ──────────
    # URL public HTTPS catre serverul de chatbot (Caddy reverse
    # proxy). Lasa gol pentru a dezactiva chatbot-ul complet.
    # Ex: https://chat.domeniultau.com
    chatbot_api_url: str = ""

    # ── UK Companies House (pipeline B2B) ──────────────────
    # Cheie API gratuita (cer pe https://developer.company-information.service.gov.uk/)
    # Folosita pentru cautare firme dupa SIC code + zona geografica.
    # Lasa gol pentru a dezactiva pipeline-ul B2B UK.
    companies_house_api_key: str = ""

    # ── SMTP (pentru email outreach B2B UK) ────────────────
    # Pentru trimitere email B2B. Daca lasi smtp_host gol, sistemul
    # compune email-ul si il pune intr-un fisier "draft" pentru tine
    # sa-l trimiti manual (recomandat la inceput, ca sa eviti spam folder).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "Web Agency"
    smtp_from_email: str = ""

    # ── Vercel (PASTRAT pentru compatibilitate, NEFOLOSIT) ─
    vercel_token: str = ""
    vercel_team_id: str = ""

    # ── WhatsApp / WAHA (NOT used — manual mode) ─────────
    waha_api_url: str = "http://localhost:3000"
    daily_limit: int = 35

    # ── City default ──────────────────────────────────────
    default_city: str = "Praha"

    @field_validator("groq_api_key", "vercel_token", "github_token", mode="before")
    @classmethod
    def strip_quotes(cls, v: str) -> str:
        return str(v).strip().strip('"').strip("'")

    @property
    def vercel_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.vercel_token}",
            "Content-Type": "application/json",
        }


settings = Settings()
