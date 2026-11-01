"""Configuration (variables d'environnement, fichier .env en local, secrets sur Cloudflare)."""
from __future__ import annotations

import os
from dataclasses import dataclass

try:  # python-dotenv est optionnel (absent sur Cloudflare Workers)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


@dataclass
class Settings:
    api_key: str
    base_url: str
    model: str
    max_steps: int
    juge_actif: bool
    juge_model: str
    prix_entree_1m: float       # USD par million de tokens d'entrée (pour le tableau de comparaison)
    prix_sortie_1m: float
    rodi_par_usd: float         # conversion USD → RODI (crédits RodiumAI)
    green_api_url: str
    green_id_instance: str
    green_api_token: str
    gerante_chat_id: str
    webhook_token: str

    @classmethod
    def from_env(cls, env=None) -> "Settings":
        """env : dict, os.environ, ou tout objet avec .get(nom, défaut)."""
        env = env if env is not None else os.environ

        def g(nom, defaut=""):
            v = env.get(nom, defaut)
            return defaut if v is None else str(v)

        model = g("LLM_MODEL", "openai/gpt-4o-mini")
        return cls(
            api_key=g("RODIUM_API_KEY"),
            base_url=g("RODIUM_BASE_URL", "https://api.rodiumai.io/v1").rstrip("/"),
            model=model,
            max_steps=int(g("MAX_STEPS", "8")),
            juge_actif=g("JUGE_ACTIF", "1") not in ("0", "false", "non", ""),
            juge_model=g("JUGE_MODEL") or model,
            prix_entree_1m=float(g("PRIX_ENTREE_1M_USD", "0.15")),
            prix_sortie_1m=float(g("PRIX_SORTIE_1M_USD", "0.60")),
            rodi_par_usd=float(g("RODI_PAR_USD", "757.6")),
            green_api_url=g("GREEN_API_URL", "https://api.green-api.com").rstrip("/"),
            green_id_instance=g("GREEN_ID_INSTANCE"),
            green_api_token=g("GREEN_API_TOKEN"),
            gerante_chat_id=g("GERANTE_CHAT_ID"),
            webhook_token=g("WEBHOOK_TOKEN"),
        )
