"""Les 5 outils de l'agent vendeur (noms et messages d'erreur imposés par le sujet).

| Outil              | Rôle                                              | En cas d'erreur                       |
|--------------------|---------------------------------------------------|---------------------------------------|
| chercher_produits  | trouver des produits par mot-clé                  | « aucun produit pour X »              |
| verifier_stock     | donner le stock d'une référence                   | « référence inconnue »                |
| frais_livraison    | frais et délai pour une zone (+ total si articles)| « zone non desservie » + zones        |
| convertir_devise   | FCFA → EUR, USD, NGN… via une API de taux réelle  | « devise non prise en charge »        |
| creer_commande     | enregistrer une commande (action irréversible)    | « stock insuffisant pour X »          |

Règle d'or : TOUS les chiffres que l'agent donne au client viennent d'ici.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone

import httpx

from .commandes import EN_ATTENTE, VALIDEE, refuser_commande, valider_commande
from .store import JsonFileStore

XOF_PAR_EUR = 655.957  # parité fixe FCFA (UEMOA) / euro
_MOTS_VIDES = {"des", "les", "une", "pour", "avec", "vous", "avez", "est", "combien", "prix", "fait",
               "quel", "quelle", "quels", "veux", "voudrais", "cherche", "dans", "sur", "pas"}


def _norm(text: str) -> str:
    """minuscules, sans accents, tirets → espaces : 'Fidjrossè' == 'fidjrosse', 'Porto-Novo' == 'porto novo'."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[\s\-_]+", " ", text.lower()).strip()


def _racine(mot: str) -> str:
    """Singulier approximatif : 'pagnes' → 'pagne', 'robes' → 'robe', 'pots' → 'pot'."""
    return mot[:-1] if len(mot) > 3 and mot[-1] in "sx" else mot


def _article_schema() -> dict:
    return {"type": "array", "items": {"type": "object", "properties": {
        "produit_id": {"type": "string", "description": "Référence, ex. 'P05'."},
        "quantite": {"type": "integer", "minimum": 1}}, "required": ["produit_id", "quantite"]}}


# --------------------------------------------------------------------------
# Schémas (format OpenAI « tools ») : c'est ce que le LLM voit
# --------------------------------------------------------------------------
SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "chercher_produits",
        "description": "Trouve des produits du catalogue par mot-clé (ex. 'pagne', 'robe', 'karité'). "
                       "À utiliser dès que le client parle d'un produit ou demande un prix : renvoie la "
                       "référence, le nom, le prix en FCFA et si le produit est en stock.",
        "parameters": {"type": "object", "properties": {
            "requete": {"type": "string", "description": "Mot-clé du client, ex. 'sac en raphia'."}},
            "required": ["requete"]}}},
    {"type": "function", "function": {
        "name": "verifier_stock",
        "description": "Donne le stock d'une référence pour une quantité. À utiliser avant de promettre une "
                       "quantité ou d'enregistrer une commande. En cas de rupture, renvoie des alternatives proches.",
        "parameters": {"type": "object", "properties": {
            "produit_id": {"type": "string", "description": "Référence, ex. 'P05'."},
            "quantite": {"type": "integer", "minimum": 1, "default": 1}},
            "required": ["produit_id"]}}},
    {"type": "function", "function": {
        "name": "frais_livraison",
        "description": "Donne les frais et le délai de livraison pour une zone. Si tu passes aussi les articles, "
                       "renvoie le TOTAL (articles + livraison) : annonce toujours ce total, ne le calcule jamais toi-même.",
        "parameters": {"type": "object", "properties": {
            "zone": {"type": "string", "description": "Quartier ou ville, ex. 'Calavi'."},
            "articles": _article_schema()},
            "required": ["zone"]}}},
    {"type": "function", "function": {
        "name": "convertir_devise",
        "description": "Convertit un montant en FCFA vers une autre devise (EUR, USD, NGN, CAD…) au taux réel du "
                       "jour. À utiliser quand le client veut un prix dans sa devise.",
        "parameters": {"type": "object", "properties": {
            "montant": {"type": "number", "description": "Montant en FCFA."},
            "devise_cible": {"type": "string", "description": "Code ISO 4217, ex. EUR, USD, NGN."}},
            "required": ["montant", "devise_cible"]}}},
    {"type": "function", "function": {
        "name": "creer_commande",
        "description": "Enregistre une commande (ACTION IRRÉVERSIBLE). À appeler UNIQUEMENT après l'accord explicite "
                       "du client sur le récapitulatif (articles, zone, total). La commande est ensuite soumise "
                       "à la validation d'Afi, la gérante.",
        "parameters": {"type": "object", "properties": {
            "client_nom": {"type": "string"},
            "client_telephone": {"type": "string"},
            "zone": {"type": "string"},
            "articles": _article_schema(),
            "confirmation_client": {"type": "boolean",
                                    "description": "true uniquement si le client a dit oui au récapitulatif."}},
            "required": ["client_nom", "zone", "articles", "confirmation_client"]}}},
]


class Toolbox:
    """
    validateur : async (commande) -> (accord: bool, motif: str). Validation humaine IMMÉDIATE
                 (palier 2, terminal). Un refus est renvoyé à l'agent comme une observation.
    notifier   : async (commande) -> None. Prévient Afi sur WhatsApp (palier 3) ; elle validera par message.
    contexte   : infos fiables venant du canal, pas du LLM (ex. numéro WhatsApp réel).
    """

    def __init__(self, store: JsonFileStore, http_transport: httpx.AsyncBaseTransport | None = None,
                 validateur=None, notifier=None, contexte: dict | None = None):
        self.store = store
        self.http_transport = http_transport  # injectable pour les tests
        self.validateur = validateur
        self.notifier = notifier
        self.contexte = contexte or {}
        self.ids_verifies: set[str] = set()  # références vérifiées (verifier_stock) pendant CE message
        self.recap_confirme: dict | None = None
        self.ids_vus: set[str] = set()          # références renvoyées par un outil pendant CE message
        self.exiger_references_vues = False     # équipe : le logisticien ne peut pas deviner une référence

    def nouveau_tour(self) -> None:
        """Appelé à chaque nouveau message client : les vérifications précédentes ne comptent plus."""
        self.ids_verifies = set()
        self.ids_vus = set()
        self.recap_confirme = None  # {produit_id: quantité} quand le client vient de dire « oui » au récapitulatif

    async def _index(self) -> dict:
        return {p["id"]: p for p in (await self.store.get_catalogue())["produits"]}

    @staticmethod
    def _alternatives(prod: dict, index: dict, quantite: int = 1) -> list[dict]:
        """Alternative proche à un produit en rupture : même catégorie d'abord, puis prix le plus proche."""
        alts = [p for p in index.values() if p["id"] != prod["id"] and p["stock"] >= max(1, quantite)]
        meme_categorie = [p for p in alts if p["categorie"] == prod["categorie"]]
        alts = meme_categorie or alts  # une alternative « proche » : même catégorie si elle existe
        alts.sort(key=lambda p: abs(p["prix"] - prod["prix"]))
        return [{"id": p["id"], "nom": p["nom"], "prix_xof": p["prix"], "stock_disponible": p["stock"]}
                for p in alts[:2]]

    # ------------------------------------------------------------------ 1
    async def chercher_produits(self, requete: str, categorie: str | None = None) -> dict:
        produits = (await self.store.get_catalogue())["produits"]
        mots = [_racine(m) for m in _norm(requete).split() if len(m) > 2 and m not in _MOTS_VIDES]
        resultats = []
        for p in produits:
            texte = {_racine(m) for m in _norm(" ".join([p["nom"], p["categorie"], *p.get("mots_cles", [])])).split()}
            score = sum(1 for m in mots if m in texte)
            if score or not mots:
                resultats.append((score, p))
        if not resultats:
            return {"erreur": f"aucun produit pour {requete}",
                    "produits_du_catalogue": [p["nom"] for p in produits]}
        resultats.sort(key=lambda x: -x[0])
        meilleur = resultats[0][0]
        index = {p["id"]: p for p in produits}
        sortie = []
        for s, p in resultats:
            if s != meilleur or len(sortie) == 5:
                continue
            ligne = {"id": p["id"], "nom": p["nom"], "prix_xof": p["prix"], "en_stock": p["stock"] > 0}
            if p["stock"] <= 0:  # rupture : l'alternative est fournie tout de suite
                ligne["rupture"] = True
                ligne["alternatives"] = self._alternatives(p, index)
            sortie.append(ligne)
            self.ids_vus |= {ligne["id"], *(a["id"] for a in ligne.get("alternatives", []))}
        return {"resultats": sortie}

    # ------------------------------------------------------------------ 2
    async def verifier_stock(self, produit_id: str, quantite: int = 1) -> dict:
        ref = str(produit_id).strip().upper()
        index = await self._index()
        prod = index.get(ref)
        if prod is None:
            return {"erreur": "référence inconnue", "reference": produit_id}
        self.ids_verifies.add(ref)
        self.ids_vus.add(ref)
        quantite = int(quantite)
        ok = prod["stock"] >= quantite
        out = {"produit_id": ref, "nom": prod["nom"], "prix_xof": prod["prix"], "quantite_demandee": quantite,
               "stock_disponible": prod["stock"], "disponible": ok}
        if not ok:
            out["alternatives"] = self._alternatives(prod, index, quantite)
        return out

    # ------------------------------------------------------------------ 3
    async def frais_livraison(self, zone: str, articles: list[dict] | None = None) -> dict:
        tarifs = await self.store.get_livraison()
        zones = {_norm(k): k for k in tarifs["zones"]}
        zones.update({_norm(a): k for a, k in tarifs.get("alias", {}).items()})
        nom_zone = zones.get(_norm(zone))
        if nom_zone is None:
            return {"erreur": "zone non desservie", "zone_demandee": zone,
                    "zones_desservies": list(tarifs["zones"].keys())}
        frais = tarifs["zones"][nom_zone]
        out = {"zone": nom_zone, "frais_livraison_xof": frais, "delai": tarifs.get("delai", "24 à 48 h")}
        if not articles:
            return out
        if self.exiger_references_vues:  # garde-fou équipe (bug réel du 01/11 : R01, S01 puis P01/P02 devinés)
            inconnues = [str(a.get("produit_id", "")).strip().upper() for a in articles
                         if str(a.get("produit_id", "")).strip().upper() not in self.ids_vus]
            if inconnues:
                return {"erreur": f"référence(s) {inconnues} jamais renvoyée(s) par le vendeur pendant cet échange : "
                                  "ne devine pas ; demande au Gérant les références exactes (ex. P05)",
                        "references_connues": sorted(self.ids_vus)}

        index = await self._index()
        lignes, sous_total, indisponibles = [], 0, []
        for a in articles:
            p = index.get(str(a["produit_id"]).strip().upper())
            if p is None:
                return {"erreur": "référence inconnue", "reference": a["produit_id"]}
            q = int(a["quantite"])
            if p["stock"] < q:  # article en rupture : JAMAIS compté dans le total
                indisponibles.append({"produit_id": p["id"], "nom": p["nom"], "prix_xof": p["prix"],
                                      "quantite_demandee": q, "stock_disponible": p["stock"], "rupture": True,
                                      "alternatives": self._alternatives(p, index, q)})
                continue
            lignes.append({"produit_id": p["id"], "nom": p["nom"], "quantite": q,
                           "prix_unitaire_xof": p["prix"], "montant_xof": p["prix"] * q})
            sous_total += p["prix"] * q
        out = {**out, "lignes": lignes, "sous_total_articles_xof": sous_total, "total_xof": sous_total + frais}
        if indisponibles:
            out["articles_non_disponibles"] = indisponibles
            out["note"] = ("total calculé SANS les articles en rupture ; signale la rupture et propose "
                           "l'alternative au client")
        return out

    # ------------------------------------------------------------------ 4
    async def convertir_devise(self, montant: float, devise_cible: str, devise_source: str = "XOF") -> dict:
        src = (devise_source or "XOF").upper().strip().replace("FCFA", "XOF")
        dst = str(devise_cible).upper().strip()
        if not re.fullmatch(r"[A-Z]{3}", dst):
            return {"erreur": "devise non prise en charge", "devise": devise_cible}
        if src == dst:
            return {"montant_source": montant, "devise_source": src, "montant_converti": montant,
                    "devise_cible": dst, "taux": 1.0, "source_taux": "identique"}
        async with httpx.AsyncClient(timeout=10, transport=self.http_transport) as client:
            # API principale : open.er-api.com (gratuite, sans clé, gère le XOF et le NGN)
            try:
                r = await client.get(f"https://open.er-api.com/v6/latest/{src}")
                data = r.json()
                if data.get("result") == "success":
                    if dst not in data.get("rates", {}):
                        return {"erreur": "devise non prise en charge", "devise": dst}
                    taux = float(data["rates"][dst])
                    return {"montant_source": montant, "devise_source": src,
                            "montant_converti": round(montant * taux, 2), "devise_cible": dst,
                            "taux": taux, "date_taux": data.get("time_last_update_utc"),
                            "source_taux": "open.er-api.com"}
            except (httpx.HTTPError, ValueError):
                pass
            # Repli : Frankfurter (BCE) via l'euro, grâce à la parité fixe XOF/EUR
            try:
                base = "EUR" if src == "XOF" else src
                cible = "EUR" if dst == "XOF" else dst
                montant_base = montant / XOF_PAR_EUR if src == "XOF" else montant
                if base == cible:
                    taux_base, date = 1.0, None
                else:
                    r = await client.get(f"https://api.frankfurter.app/latest?from={base}&to={cible}")
                    data = r.json()
                    if cible not in data.get("rates", {}):
                        return {"erreur": "devise non prise en charge", "devise": dst}
                    taux_base, date = float(data["rates"][cible]), data.get("date")
                converti = montant_base * taux_base
                if dst == "XOF":
                    converti *= XOF_PAR_EUR
                return {"montant_source": montant, "devise_source": src,
                        "montant_converti": round(converti, 2), "devise_cible": dst,
                        "taux": round(converti / montant, 8) if montant else None,
                        "date_taux": date, "source_taux": "frankfurter.app (BCE) + parité XOF/EUR"}
            except (httpx.HTTPError, ValueError, KeyError):
                return {"erreur": "taux de change indisponible pour le moment, réessayer plus tard"}

    # ------------------------------------------------------------------ 5
    async def creer_commande(self, client_nom: str, zone: str, articles: list[dict],
                             confirmation_client: bool, client_telephone: str = "") -> dict:
        if not confirmation_client:
            return {"erreur": "le client n'a pas confirmé : présente le récapitulatif et attends son « oui »"}
        articles = [{"produit_id": str(a["produit_id"]).strip().upper(), "quantite": int(a["quantite"])}
                    for a in articles]
        non_verifies = [a["produit_id"] for a in articles if a["produit_id"] not in self.ids_verifies]
        if non_verifies:  # garde-fou : interdit de deviner une référence
            return {"erreur": f"référence(s) {non_verifies} non vérifiée(s) pendant cet échange : retrouve le "
                              "produit avec chercher_produits, vérifie-le avec verifier_stock, puis réessaie"}
        if self.recap_confirme:  # garde-fou : un « oui » confirme EXACTEMENT le récapitulatif
            demande = {}
            for a in articles:
                demande[a["produit_id"]] = demande.get(a["produit_id"], 0) + a["quantite"]
            if demande != self.recap_confirme:
                attendu = ", ".join(f"{q} × {pid}" for pid, q in self.recap_confirme.items())
                return {"erreur": f"articles différents du récapitulatif que le client a confirmé ({attendu}). "
                                  "Commande exactement ces articles, sans ajouter l'alternative ni deviner "
                                  "une référence"}
        index = await self._index()
        for a in articles:  # le stock est revérifié au moment de commander
            p = index.get(a["produit_id"])
            if p is None:
                return {"erreur": "référence inconnue", "reference": a["produit_id"]}
            if p["stock"] < a["quantite"]:
                return {"erreur": f"stock insuffisant pour {p['nom']}", "stock_disponible": p["stock"]}
        devis = await self.frais_livraison(zone, articles)
        if "erreur" in devis:
            return devis

        client = {"nom": client_nom, "telephone": self.contexte.get("telephone") or client_telephone}
        if self.contexte.get("chat_id"):
            client["chat_id"] = self.contexte["chat_id"]  # pour prévenir le client après la décision
        numeros = [int(o["id"]) for o in await self.store.list_orders() if str(o["id"]).isdigit()]
        commande = {"id": str(max(numeros, default=0) + 1),  # numéro court : « OUI 12 / NON 12 »
                    "cree_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "client": client, **devis, "statut": EN_ATTENTE}
        await self.store.save_order(commande)

        # Palier 2 : Afi décide tout de suite dans le terminal ; un refus est une observation pour l'agent
        if self.validateur is not None:
            accord, motif = await self.validateur(commande)
            res = (await valider_commande(self.store, commande["id"]) if accord
                   else await refuser_commande(self.store, commande["id"], motif))
            final = res.get("commande", commande)
            return {"commande_id": final["id"], "statut": final["statut"], "zone": final["zone"],
                    "lignes": final["lignes"], "sous_total_articles_xof": final["sous_total_articles_xof"],
                    "frais_livraison_xof": final["frais_livraison_xof"],
                    "total_xof": final["total_xof"], "motif": final.get("motif"),
                    "message": "Commande VALIDÉE par Afi." if final["statut"] == VALIDEE
                    else "Commande REFUSÉE par Afi : préviens le client poliment, rien n'est enregistré."}

        # Palier 3 : Afi est prévenue sur WhatsApp et validera plus tard (OUI n / NON n)
        if self.notifier is not None:
            await self.notifier(commande)
        return {"commande_id": commande["id"], "statut": commande["statut"], "zone": commande["zone"],
                "lignes": commande["lignes"], "sous_total_articles_xof": commande["sous_total_articles_xof"],
                "frais_livraison_xof": commande["frais_livraison_xof"],
                "total_xof": commande["total_xof"],
                "message": "Commande transmise à Afi pour validation. Le client sera prévenu de sa décision."}

    # ------------------------------------------------------------------ dispatch
    async def run(self, name: str, arguments: str | dict) -> dict:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except json.JSONDecodeError:
            return {"erreur": f"arguments JSON invalides pour {name}"}
        fn = getattr(self, name, None) if name in {s["function"]["name"] for s in SCHEMAS} else None
        if fn is None:
            return {"erreur": f"outil inconnu : {name}"}
        try:
            return await fn(**args)
        except (TypeError, KeyError, ValueError) as e:
            return {"erreur": f"paramètres invalides pour {name} : {e}"}
