"""Stockage des données de la boutique (palier 1-2 : fichiers JSON en local).

Toutes les méthodes sont asynchrones et l'interface est identique à celle de
KVStore (store_kv.py, palier 3, Cloudflare KV). Les outils et les agents ne
savent pas où sont rangées les données : on change de stockage sans les toucher.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path


class JsonFileStore:
    def __init__(self, data_dir: str | Path = "data"):
        self.dir = Path(data_dir)
        self._lock = asyncio.Lock()

    # ----------------------------------------------------------- utilitaires
    def _read(self, name: str, default):
        path = self.dir / name
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, name: str, data) -> None:
        path = self.dir / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)  # écriture atomique : jamais de fichier à moitié écrit

    # ----------------------------------------------------------- catalogue / livraison
    async def get_catalogue(self) -> dict:
        return self._read("catalogue.json", {"devise": "XOF", "produits": []})

    async def save_catalogue(self, catalogue: dict) -> None:
        async with self._lock:
            self._write("catalogue.json", catalogue)

    async def get_livraison(self) -> dict:
        return self._read("livraison.json", {"zones": {}})

    # ----------------------------------------------------------- commandes
    async def list_orders(self) -> list[dict]:
        return self._read("commandes.json", [])

    async def get_order(self, order_id: str) -> dict | None:
        return next((o for o in await self.list_orders() if o["id"] == order_id), None)

    async def save_order(self, order: dict) -> None:
        async with self._lock:
            orders = self._read("commandes.json", [])
            orders = [o for o in orders if o["id"] != order["id"]] + [order]
            self._write("commandes.json", orders)

    # ----------------------------------------------------------- petit clé-valeur
    # (historique des conversations, anti-doublon) : imite Cloudflare KV en local
    async def kv_get(self, key: str) -> str | None:
        etat = self._read("etat.json", {})
        item = etat.get(key)
        if item is None or (item["expire"] and item["expire"] < time.time()):
            return None
        return item["valeur"]

    async def kv_put(self, key: str, value: str, ttl: int | None = None) -> None:
        async with self._lock:
            etat = self._read("etat.json", {})
            maintenant = time.time()
            etat = {k: v for k, v in etat.items() if not v["expire"] or v["expire"] > maintenant}
            etat[key] = {"valeur": value, "expire": maintenant + ttl if ttl else None}
            self._write("etat.json", etat)
