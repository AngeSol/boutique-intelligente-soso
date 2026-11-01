"""Les 5 outils de l'agent vendeur.

Règle d'or : TOUS les chiffres que l'agent donne au client viennent d'ici
(prix, stock, frais, totaux, taux de change). Le LLM ne calcule rien lui-même.
"""
from __future__ import annotations

import json
import secrets
import unicodedata
from datetime import datetime, timezone

import httpx

from .commandes import EN_ATTENTE, VALIDEE, refuser_commande, valider_commande
from .store import JsonFileStore

XOF_PAR_EUR = 655.957  # parité fixe FCFA (UEMOA) / euro


def _norm(text: str) -> str:
    """minuscules + sans accents, pour comparer 'Lomé' et 'lome'."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return text.lower().strip()


# --------------------------------------------------------------------------
# Schémas (format OpenAI « tools ») : c'est ce que le LLM voit
# --------------------------------------------------------------------------
SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "chercher_produit",
            "description": "Cherche des produits dans le catalogue par mots-clés (nom, catégorie, couleur). "
                           "Renvoie id, nom, prix en XOF, unité et disponibilité. À utiliser avant tout prix.",
            "parameters": {
                "type": "object",
                "properties": {
                    "requete": {"type": "string", "description": "Mots-clés, ex. 'wax bleu', 'sac', 'kanvo'."},
                    "categorie": {"type": "string", "enum": ["pagne", "accessoire"],
                                  "description": "Filtre optionnel."},
                },
                "required": ["requete"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verifier_stock",
            "description": "Vérifie si une quantité d'un produit est disponible. En cas de rupture, "
                           "renvoie des alternatives en stock de la même catégorie.",
            "parameters": {
                "type": "object",
                "properties": {
                    "produit_id": {"type": "string", "description": "Identifiant, ex. 'P001'."},
                    "quantite": {"type": "integer", "minimum": 1, "default": 1},
                },
                "required": ["produit_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculer_livraison",
            "description": "Calcule les frais de livraison vers une zone ET le total de la commande "
                           "(total = articles + livraison). Utilise toujours ce total, ne calcule jamais toi-même.",
            "parameters": {
                "type": "object",
                "properties": {
                    "zone": {"type": "string", "description": "Ville ou 'retrait boutique dantokpa'."},
                    "articles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "produit_id": {"type": "string"},
                                "quantite": {"type": "integer", "minimum": 1},
                            },
                            "required": ["produit_id", "quantite"],
                        },
                    },
                },
                "required": ["zone", "articles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convertir_devise",
            "description": "Convertit un montant (XOF par défaut) dans une autre devise au taux du jour (API réelle). "
                           "Ex. pour un client en euros (EUR), dollars canadiens (CAD), nairas (NGN).",
            "parameters": {
                "type": "object",
                "properties": {
                    "montant": {"type": "number"},
                    "devise_cible": {"type": "string", "description": "Code ISO 4217, ex. EUR, USD, CAD, NGN."},
                    "devise_source": {"type": "string", "default": "XOF"},
                },
                "required": ["montant", "devise_cible"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "creer_commande",
            "description": "Enregistre une commande. N'appeler QUE si le client a explicitement confirmé "
                           "le récapitulatif (articles, zone, total). La commande part ensuite en validation "
                           "auprès de la gérante : elle n'est pas définitive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "client_nom": {"type": "string"},
                    "client_telephone": {"type": "string"},
                    "zone": {"type": "string"},
                    "articles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "produit_id": {"type": "string"},
                                "quantite": {"type": "integer", "minimum": 1},
                            },
                            "required": ["produit_id", "quantite"],
                        },
                    },
                    "confirmation_client": {
                        "type": "boolean",
                        "description": "true uniquement si le client a dit oui au récapitulatif.",
                    },
                },
                "required": ["client_nom", "client_telephone", "zone", "articles", "confirmation_client"],
            },
        },
    },
]


class Toolbox:
    """
    validateur : async (commande) -> (accord: bool, motif: str). Validation humaine
                 IMMÉDIATE (palier 2, terminal). Si absent, la commande reste en attente.
    notifier   : async (commande) -> None. Prévient la gérante (palier 3, WhatsApp),
                 qui validera plus tard par message.
    contexte   : infos fiables venant du canal, pas du LLM (ex. numéro WhatsApp réel).
    """

    def __init__(self, store: JsonFileStore, http_transport: httpx.AsyncBaseTransport | None = None,
                 validateur=None, notifier=None, contexte: dict | None = None):
        self.store = store
        self.http_transport = http_transport  # injectable pour les tests
        self.validateur = validateur
        self.notifier = notifier
        self.contexte = contexte or {}
        self.ids_verifies: set[str] = set()  # produits vérifiés (verifier_stock) pendant CE message

    def nouveau_tour(self) -> None:
        """Appelé à chaque nouveau message client : les identifiants des messages précédents
        ne comptent plus (l'historique ne garde que le texte, pas les résultats d'outils)."""
        self.ids_verifies = set()

    # ------------------------------------------------------------------ 1
    async def chercher_produit(self, requete: str, categorie: str | None = None) -> dict:
        cat = await self.store.get_catalogue()
        mots = [m for m in _norm(requete).replace(",", " ").split() if len(m) > 1]
        resultats = []
        for p in cat["produits"]:
            if categorie and p["categorie"] != categorie:
                continue
            texte = _norm(" ".join([p["nom"], p["categorie"], *p["couleurs"]]))
            score = sum(1 for m in mots if m in texte)
            if score or not mots:
                resultats.append((score, p))
        resultats.sort(key=lambda x: -x[0])
        produits = [
            {"id": p["id"], "nom": p["nom"], "prix_xof": p["prix"], "unite": p["unite"],
             "couleurs": p["couleurs"], "disponible": p["stock"] > 0}
            for _, p in resultats[:5]
        ]
        if not produits:
            return {"resultats": [], "message": "Aucun produit trouvé.",
                    "categories_disponibles": sorted({p["categorie"] for p in cat["produits"]})}
        return {"resultats": produits}

    # ------------------------------------------------------------------ 2
    async def verifier_stock(self, produit_id: str, quantite: int = 1) -> dict:
        cat = await self.store.get_catalogue()
        prod = next((p for p in cat["produits"] if p["id"] == produit_id), None)
        if prod is None:
            return {"erreur": f"Produit {produit_id} inconnu. Utilise chercher_produit d'abord."}
        self.ids_verifies.add(produit_id)
        ok = prod["stock"] >= quantite
        out = {"produit_id": produit_id, "nom": prod["nom"], "quantite_demandee": quantite,
               "stock_disponible": prod["stock"], "disponible": ok}
        if not ok:
            alts = [p for p in cat["produits"]
                    if p["categorie"] == prod["categorie"] and p["id"] != produit_id and p["stock"] >= quantite]
            alts.sort(key=lambda p: abs(p["prix"] - prod["prix"]))  # prix le plus proche d'abord
            out["alternatives"] = [{"id": p["id"], "nom": p["nom"], "prix_xof": p["prix"],
                                    "stock_disponible": p["stock"]} for p in alts[:3]]
        return out

    # ------------------------------------------------------------------ 3
    async def calculer_livraison(self, zone: str, articles: list[dict]) -> dict:
        tarifs = await self.store.get_livraison()
        zones = {_norm(k): (k, v) for k, v in tarifs["zones"].items()}
        z = zones.get(_norm(zone))
        if z is None:
            return {"erreur": f"Zone '{zone}' non desservie.",
                    "zones_disponibles": list(tarifs["zones"].keys())}
        nom_zone, info = z

        cat = await self.store.get_catalogue()
        index = {p["id"]: p for p in cat["produits"]}
        lignes, sous_total, nb_articles = [], 0, 0
        for a in articles:
            p = index.get(a["produit_id"])
            if p is None:
                return {"erreur": f"Produit {a['produit_id']} inconnu."}
            q = int(a["quantite"])
            lignes.append({"produit_id": p["id"], "nom": p["nom"], "quantite": q,
                           "prix_unitaire_xof": p["prix"], "montant_xof": p["prix"] * q})
            sous_total += p["prix"] * q
            nb_articles += q

        frais = info["frais"]
        seuil = tarifs.get("supplement_par_article_au_dela_de", 999)
        supplement = max(0, nb_articles - seuil) * tarifs.get("supplement_par_article", 0)
        livraison = frais + supplement
        return {
            "zone": nom_zone, "delai": info["delai"], "lignes": lignes,
            "sous_total_articles_xof": sous_total,
            "frais_livraison_xof": livraison,
            "total_xof": sous_total + livraison,
        }

    # ------------------------------------------------------------------ 4
    async def convertir_devise(self, montant: float, devise_cible: str, devise_source: str = "XOF") -> dict:
        src, dst = devise_source.upper().strip(), devise_cible.upper().strip()
        if src == dst:
            return {"montant_source": montant, "devise_source": src, "montant_converti": montant,
                    "devise_cible": dst, "taux": 1.0, "source_taux": "identique"}
        async with httpx.AsyncClient(timeout=10, transport=self.http_transport) as client:
            # API principale : open.er-api.com (gratuite, sans clé, gère le XOF)
            try:
                r = await client.get(f"https://open.er-api.com/v6/latest/{src}")
                data = r.json()
                if data.get("result") == "success" and dst in data.get("rates", {}):
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
                    taux_base = 1.0
                    date = None
                else:
                    r = await client.get(f"https://api.frankfurter.app/latest?from={base}&to={cible}")
                    data = r.json()
                    taux_base = float(data["rates"][cible])
                    date = data.get("date")
                converti = montant_base * taux_base
                if dst == "XOF":
                    converti *= XOF_PAR_EUR
                return {"montant_source": montant, "devise_source": src,
                        "montant_converti": round(converti, 2), "devise_cible": dst,
                        "taux": round(converti / montant, 8) if montant else None,
                        "date_taux": date, "source_taux": "frankfurter.app (BCE) + parité XOF/EUR"}
            except (httpx.HTTPError, ValueError, KeyError):
                return {"erreur": f"Conversion {src}->{dst} indisponible pour le moment."}

    # ------------------------------------------------------------------ 5
    async def creer_commande(self, client_nom: str, client_telephone: str, zone: str,
                             articles: list[dict], confirmation_client: bool) -> dict:
        if not confirmation_client:
            return {"erreur": "Le client n'a pas confirmé. Présente-lui le récapitulatif et attends son « oui »."}
        non_verifies = [a["produit_id"] for a in articles if a["produit_id"] not in self.ids_verifies]
        if non_verifies:  # garde-fou : interdit de deviner un identifiant
            return {"erreur": f"Produit(s) {non_verifies} non vérifiés pendant cet échange. Relis le récapitulatif "
                              "validé par le client, retrouve le bon produit avec chercher_produit, vérifie-le "
                              "avec verifier_stock, puis rappelle creer_commande."}
        for a in articles:  # le stock est revérifié au moment de commander
            s = await self.verifier_stock(a["produit_id"], int(a["quantite"]))
            if "erreur" in s or not s["disponible"]:
                return {"erreur": "Stock insuffisant au moment de commander.", "detail": s}
        devis = await self.calculer_livraison(zone, articles)
        if "erreur" in devis:
            return devis
        client = {"nom": client_nom, "telephone": self.contexte.get("telephone") or client_telephone}
        if self.contexte.get("chat_id"):
            client["chat_id"] = self.contexte["chat_id"]  # pour prévenir le client après décision
        commande = {
            "id": f"CMD-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(2).upper()}",
            "cree_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "client": client,
            **devis,
            "statut": EN_ATTENTE,
        }
        await self.store.save_order(commande)

        # Palier 2 : la gérante décide tout de suite (terminal)
        if self.validateur is not None:
            accord, motif = await self.validateur(commande)
            res = (await valider_commande(self.store, commande["id"]) if accord
                   else await refuser_commande(self.store, commande["id"], motif))
            final = res.get("commande", commande)
            return {"commande_id": final["id"], "statut": final["statut"], "total_xof": final["total_xof"],
                    "motif": final.get("motif"),
                    "message": "Commande VALIDÉE par la gérante." if final["statut"] == VALIDEE
                    else "Commande REFUSÉE par la gérante."}

        # Palier 3 : la gérante est prévenue et validera plus tard sur WhatsApp
        if self.notifier is not None:
            await self.notifier(commande)
        return {"commande_id": commande["id"], "statut": commande["statut"], "total_xof": commande["total_xof"],
                "message": "Commande enregistrée, en attente de validation par la gérante. "
                           "Le client sera prévenu dès sa décision."}

    # ------------------------------------------------------------------ dispatch
    async def run(self, name: str, arguments: str | dict) -> dict:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except json.JSONDecodeError:
            return {"erreur": f"Arguments JSON invalides pour {name}."}
        fn = getattr(self, name, None) if name in {s["function"]["name"] for s in SCHEMAS} else None
        if fn is None:
            return {"erreur": f"Outil inconnu : {name}"}
        try:
            return await fn(**args)
        except TypeError as e:
            return {"erreur": f"Paramètres invalides pour {name} : {e}"}
