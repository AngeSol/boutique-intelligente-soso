"""Stockage Cloudflare KV (palier 3). Même interface que JsonFileStore.

`kv` est le binding KV du Worker (self.env.BOUTIQUE_KV). Ce module n'importe
rien de spécifique à Cloudflare : on peut le tester avec un faux KV.

Clés utilisées :
  catalogue          JSON du catalogue (stock compris)
  livraison          JSON des tarifs de livraison
  commandes          JSON de la liste des commandes
  hist:<chatId>      historique de conversation d'un client (TTL 7 jours)
  vu:<idMessage>     anti-doublon des webhooks (TTL 2 jours)
"""
from __future__ import annotations

import asyncio
import json


class KVStore:
    def __init__(self, kv, defauts: dict | None = None):
        self.kv = kv
        self.defauts = defauts or {}  # données de départ si la clé n'existe pas encore

    async def _get_json(self, key: str, default):
        valeur = await self.kv.get(key)
        if not isinstance(valeur, str):  # None / null JavaScript → clé absente
            return self.defauts.get(key, default)
        return json.loads(valeur)

    async def _put_json(self, key: str, data) -> None:
        texte = json.dumps(data, ensure_ascii=False)
        for essai in range(3):  # KV limite à 1 écriture/seconde par clé (sinon 429)
            try:
                await self.kv.put(key, texte)
                return
            except Exception:  # noqa: BLE001 (erreur JavaScript remontée par le runtime)
                if essai == 2:
                    raise
                await asyncio.sleep(1.1)

    async def get_catalogue(self) -> dict:
        return await self._get_json("catalogue", {"devise": "XOF", "produits": []})

    async def save_catalogue(self, catalogue: dict) -> None:
        await self._put_json("catalogue", catalogue)

    async def get_livraison(self) -> dict:
        return await self._get_json("livraison", {"zones": {}})

    async def list_orders(self) -> list[dict]:
        return await self._get_json("commandes", [])

    async def get_order(self, order_id: str) -> dict | None:
        return next((o for o in await self.list_orders() if o["id"] == order_id), None)

    async def save_order(self, order: dict) -> None:
        orders = [o for o in await self.list_orders() if o["id"] != order["id"]] + [order]
        await self._put_json("commandes", orders)

    async def kv_get(self, key: str) -> str | None:
        valeur = await self.kv.get(key)
        return valeur if isinstance(valeur, str) else None

    async def kv_put(self, key: str, value: str, ttl: int | None = None) -> None:
        if ttl:
            await self.kv.put(key, value, expirationTtl=max(60, int(ttl)))  # TTL minimum KV : 60 s
        else:
            await self.kv.put(key, value)
