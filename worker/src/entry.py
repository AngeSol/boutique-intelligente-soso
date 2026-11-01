"""Cloudflare Python Worker : le vendeur WhatsApp en production (palier 3).

Routes :
  GET  /                  état du service (sans secret)
  POST /webhook           webhook Green API (en-tête Authorization: Bearer <WEBHOOK_TOKEN>)
  POST /admin/init        (Bearer WEBHOOK_TOKEN) charge le catalogue et les tarifs initiaux dans KV
  GET  /admin/commandes   (Bearer WEBHOOK_TOKEN) liste des commandes, pour vérifier / faire des captures

Le webhook répond 200 IMMÉDIATEMENT ; le travail (LLM, outils, envoi WhatsApp) se fait
ensuite en arrière-plan avec ctx.waitUntil, sinon Green API croirait à un échec et renverrait le message.
"""
import hmac
import json
from urllib.parse import parse_qs, urlparse

from workers import Response, WorkerEntrypoint

from boutique.config import Settings
from boutique.greenapi import GreenAPI
from boutique.llm import RodiumClient
from boutique.store_kv import KVStore
from boutique.whatsapp import ServiceWhatsApp
from donnees_defaut import CATALOGUE, LIVRAISON


class EnvAdapter:
    """Permet à Settings.from_env de lire self.env (variables + secrets Cloudflare)."""

    def __init__(self, env):
        self.env = env

    def get(self, nom, defaut=None):
        try:
            v = getattr(self.env, nom)
        except AttributeError:
            return defaut
        if v is None or str(v) in ("undefined", "null"):
            return defaut
        return str(v)


class Default(WorkerEntrypoint):
    def _settings(self) -> Settings:
        return Settings.from_env(EnvAdapter(self.env))

    def _store(self) -> KVStore:
        return KVStore(self.env.BOUTIQUE_KV, defauts={"catalogue": CATALOGUE, "livraison": LIVRAISON})

    @staticmethod
    def _autorise(request, s: Settings) -> bool:
        if not s.webhook_token:
            return False
        recu = request.headers.get("Authorization") or ""
        if hmac.compare_digest(recu.strip(), f"Bearer {s.webhook_token}"):
            return True
        jeton = parse_qs(urlparse(str(request.url)).query).get("token", [""])[0]  # repli : ?token=...
        return hmac.compare_digest(jeton, s.webhook_token)

    async def fetch(self, request):
        s = self._settings()
        chemin = urlparse(str(request.url)).path.rstrip("/") or "/"
        methode = request.method

        if methode == "GET" and chemin == "/":
            return Response.json({
                "service": "boutique-intelligente", "ok": True, "modele": s.model,
                "juge_actif": s.juge_actif,
                "secrets_presents": {"RODIUM_API_KEY": bool(s.api_key), "GREEN_API_TOKEN": bool(s.green_api_token),
                                     "WEBHOOK_TOKEN": bool(s.webhook_token), "GERANTE_CHAT_ID": bool(s.gerante_chat_id)},
            })

        if methode == "POST" and chemin == "/webhook":
            if not self._autorise(request, s):
                return Response("unauthorized", status=401)
            try:
                body = await request.json()
            except Exception:  # noqa: BLE001
                return Response("bad json", status=400)
            service = ServiceWhatsApp(
                store=self._store(),
                green=GreenAPI(s.green_api_url, s.green_id_instance, s.green_api_token),
                settings=s,
                llm=RodiumClient(s.api_key, s.base_url, s.model),
            )
            self.ctx.waitUntil(service.traiter_sans_erreur(body))  # travail en arrière-plan
            return Response.json({"recu": True})  # réponse immédiate à Green API

        if chemin == "/admin/init" and methode == "POST":
            if not self._autorise(request, s):
                return Response("unauthorized", status=401)
            store = self._store()
            await store.save_catalogue(CATALOGUE)
            await store.kv_put("livraison", json.dumps(LIVRAISON, ensure_ascii=False))
            return Response.json({"ok": True, "produits": len(CATALOGUE["produits"])})

        if chemin == "/admin/commandes" and methode == "GET":
            if not self._autorise(request, s):
                return Response("unauthorized", status=401)
            return Response.json(await self._store().list_orders())

        return Response("not found", status=404)
