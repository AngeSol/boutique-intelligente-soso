"""Tests du palier 1 (sans clé API : LLM et API de change simulés).

Lancer : python -m pytest -q
"""
import asyncio
import json
import shutil
from pathlib import Path

import httpx
import pytest

from boutique.agent import AgentVendeur, chiffres_non_sources
from boutique.llm import LLMResponse, RodiumClient
from boutique.store import JsonFileStore
from boutique.tools import SCHEMAS, Toolbox

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture
def store(tmp_path):
    # toujours le stock INITIAL : les tests ne dépendent pas des commandes réelles passées en local
    shutil.copy(DATA / "catalogue_initial.json", tmp_path / "catalogue.json")
    shutil.copy(DATA / "livraison.json", tmp_path / "livraison.json")
    return JsonFileStore(tmp_path)


TAUX = {"EUR": 1 / 655.957, "USD": 0.00177, "NGN": 2.71, "CAD": 0.00238}


def fx_transport(er_api_ok=True):
    def handler(request: httpx.Request):
        if "open.er-api.com" in request.url.host:
            if not er_api_ok:
                return httpx.Response(503)
            return httpx.Response(200, json={"result": "success", "time_last_update_utc": "test", "rates": TAUX})
        if "frankfurter" in request.url.host:
            return httpx.Response(200, json={"date": "2026-11-01", "rates": {"USD": 1.16}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ noms et messages imposés
def test_noms_des_outils_conformes_au_sujet():
    assert [s["function"]["name"] for s in SCHEMAS] == [
        "chercher_produits", "verifier_stock", "frais_livraison", "convertir_devise", "creer_commande"]


def test_chercher_produits(store):
    r = run(Toolbox(store).chercher_produits("Vous avez des pagnes ?"))
    assert r["resultats"][0] == {"id": "P01", "nom": "Pagne wax 6 yards", "prix_xof": 12000, "en_stock": True}
    r = run(Toolbox(store).chercher_produits("beurre de karité"))
    assert r["resultats"][0]["id"] == "P06"


def test_chercher_produits_erreur(store):
    r = run(Toolbox(store).chercher_produits("ordinateur"))
    assert r["erreur"] == "aucun produit pour ordinateur"


def test_verifier_stock_et_reference_inconnue(store):
    r = run(Toolbox(store).verifier_stock("p05", 2))
    assert r["disponible"] and r["stock_disponible"] == 5
    assert run(Toolbox(store).verifier_stock("P99"))["erreur"] == "référence inconnue"


def test_rupture_propose_une_alternative_proche(store):
    r = run(Toolbox(store).verifier_stock("P03", 1))  # sac en raphia : stock 0
    assert r["disponible"] is False and r["prix_xof"] == 7500
    assert r["alternatives"][0]["id"] == "P04"         # sandales en cuir : même catégorie, prix proche


def test_frais_livraison(store):
    r = run(Toolbox(store).frais_livraison("Fidjrosse"))
    assert r == {"zone": "Fidjrossè", "frais_livraison_xof": 1500, "delai": "24 à 48 h"}


def test_frais_livraison_zone_non_desservie(store):
    r = run(Toolbox(store).frais_livraison("Lomé"))
    assert r["erreur"] == "zone non desservie"
    assert r["zones_desservies"] == ["Cotonou", "Akpakpa", "Fidjrossè", "Calavi", "Porto-Novo", "Ouidah"]


def test_total_scenario_afi(store):
    """2 × 22 000 + 2 000 = 46 000 FCFA (le sac en raphia est en rupture, donc exclu)."""
    r = run(Toolbox(store).frais_livraison("Calavi", [{"produit_id": "P05", "quantite": 2}]))
    assert r["total_xof"] == 46000


def test_total_scenario_koffi(store):
    """3 × 3 500 + 3 000 = 13 500 FCFA."""
    r = run(Toolbox(store).frais_livraison("Porto-Novo", [{"produit_id": "P06", "quantite": 3}]))
    assert r["total_xof"] == 13500


def test_conversion_api_reelle(store):
    r = run(Toolbox(store, fx_transport()).convertir_devise(13500, "NGN"))
    assert r["source_taux"] == "open.er-api.com" and r["montant_converti"] == round(13500 * 2.71, 2)


def test_conversion_repli_frankfurter(store):
    r = run(Toolbox(store, fx_transport(er_api_ok=False)).convertir_devise(655957, "USD"))
    assert "frankfurter" in r["source_taux"] and r["montant_converti"] == pytest.approx(1160, rel=1e-6)


def test_devise_non_prise_en_charge(store):
    assert run(Toolbox(store, fx_transport()).convertir_devise(1000, "XYZ"))["erreur"] == "devise non prise en charge"
    assert run(Toolbox(store, fx_transport()).convertir_devise(1000, "euros"))["erreur"] == "devise non prise en charge"


def test_commande_refusee_sans_confirmation(store):
    r = run(Toolbox(store).creer_commande("Afi", "Calavi", [{"produit_id": "P05", "quantite": 1}], False))
    assert "erreur" in r and run(store.list_orders()) == []


def test_commande_stock_insuffisant(store):
    tb = Toolbox(store)
    run(tb.verifier_stock("P03", 1))
    r = run(tb.creer_commande("Afi", "Calavi", [{"produit_id": "P03", "quantite": 1}], True))
    assert r["erreur"] == "stock insuffisant pour Sac en raphia"


def test_commande_numero_court_et_attente(store):
    tb = Toolbox(store)
    run(tb.verifier_stock("P05", 2))
    r = run(tb.creer_commande("Afi", "Calavi", [{"produit_id": "P05", "quantite": 2}], True))
    assert r["commande_id"] == "1" and r["statut"] == "en_attente_validation_gerante" and r["total_xof"] == 46000
    run(tb.verifier_stock("P06", 1))
    assert run(tb.creer_commande("Koffi", "Cotonou", [{"produit_id": "P06", "quantite": 1}], True))["commande_id"] == "2"


def test_commande_refusee_si_reference_devinee(store):
    """Bug réel : au 2e message, l'agent avait deviné une référence au lieu de la vérifier."""
    tb = Toolbox(store)
    run(tb.verifier_stock("P05", 1))   # message 1 : la robe est vérifiée
    tb.nouveau_tour()                  # message 2 : « oui je confirme »
    r = run(tb.creer_commande("Afi", "Calavi", [{"produit_id": "P01", "quantite": 1}], True))
    assert "non vérifiée" in r["erreur"] and run(store.list_orders()) == []


# ------------------------------------------------------------------ évaluation
def test_detecte_chiffre_invente():
    outils = [{"total_xof": 46000}]
    assert chiffres_non_sources("Total : *46 000 FCFA*", outils) == []
    assert chiffres_non_sources("Total : *44 000 FCFA*", outils) == [44000.0]
    assert chiffres_non_sources("Je note le +229 97 12 34 56", [], ["mon numéro +229 97 12 34 56"]) == []


# ------------------------------------------------------------------ boucle d'agent
class FakeLLM:
    """Rejoue une suite de réponses, comme le ferait le vrai LLM."""
    def __init__(self, script):
        self.script = list(script)
        self.recu = []

    async def chat(self, messages, tools=None, **kw):
        self.recu.append(messages)
        return self.script.pop(0)


def appel(i, nom, args):
    return {"id": f"c{i}", "type": "function", "function": {"name": nom, "arguments": json.dumps(args)}}


def outil(i, nom, args, usage=None):
    return LLMResponse({"role": "assistant", "content": None, "tool_calls": [appel(i, nom, args)]}, usage or {})


def texte(t):
    return LLMResponse({"role": "assistant", "content": t})


def test_test1_pagnes_en_euros(store):
    """Test 1 du sujet : chercher_produits puis convertir_devise, dans cet ordre."""
    script = [outil(1, "chercher_produits", {"requete": "pagnes"}, {"prompt_tokens": 100, "completion_tokens": 10}),
              outil(2, "convertir_devise", {"montant": 12000, "devise_cible": "EUR"}),
              texte("Oui 😊 Le *Pagne wax 6 yards* coûte *12 000 FCFA*, soit ≈ 18,29 €. Je vous en réserve ?")]
    rep = run(AgentVendeur(FakeLLM(script), Toolbox(store, fx_transport())).repondre(
        [{"role": "user", "content": "Vous avez des pagnes ? Combien ça fait en euros pour ma sœur à Paris ?"}]))
    assert rep.trace.outils_appeles == ["chercher_produits", "convertir_devise"]
    assert "12 000" in rep.texte and rep.trace.resume()["tokens_entree"] == 100


def test_test2_lome_non_desservie(store):
    script = [outil(1, "frais_livraison", {"zone": "Lomé"}),
              texte("Désolée, nous ne livrons pas à Lomé. Zones desservies : Cotonou, Akpakpa, Fidjrossè, "
                    "Calavi, Porto-Novo et Ouidah 🙏")]
    rep = run(AgentVendeur(FakeLLM(script), Toolbox(store)).repondre([{"role": "user", "content": "Livrez-moi à Lomé."}]))
    assert rep.trace.etapes[0]["resultat"]["erreur"] == "zone non desservie"
    assert "Calavi" in rep.texte


def test_agent_corrige_un_chiffre_invente(store):
    script = [texte("La robe coûte 20 000 FCFA."),                        # inventé !
              outil(1, "chercher_produits", {"requete": "robe"}),
              texte("La *Robe en wax* coûte *22 000 FCFA*.")]
    rep = run(AgentVendeur(FakeLLM(script), Toolbox(store)).repondre([{"role": "user", "content": "prix robe ?"}]))
    assert "22 000" in rep.texte
    assert any(e["type"] == "evaluation" and "❌" in e["verdict"] for e in rep.trace.etapes)


def test_agent_limite_etapes(store):
    boucle = [outil(i, "chercher_produits", {"requete": "pagne"}) for i in range(12)]
    rep = run(AgentVendeur(FakeLLM(boucle), Toolbox(store), max_steps=3).repondre([{"role": "user", "content": "?"}]))
    assert rep.trace.etapes[-1]["type"] == "limite_atteinte" and len(rep.trace.outils_appeles) == 3


def test_max_steps_par_defaut_entre_8_et_10(store):
    assert 8 <= AgentVendeur(None, Toolbox(store)).max_steps <= 10


# ------------------------------------------------------------------ client HTTP RodiumAI
def test_client_rodium_format_openai():
    vu = {}

    def handler(request):
        vu.update(url=str(request.url), auth=request.headers["authorization"], body=json.loads(request.content))
        return httpx.Response(200, json={"model": "openai/gpt-4o-mini", "usage": {"prompt_tokens": 5},
                                         "choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    c = RodiumClient("rd_sk_test", "https://api.rodiumai.io/v1", "openai/gpt-4o-mini",
                     transport=httpx.MockTransport(handler))
    r = run(c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "f"}}]))
    assert vu["url"] == "https://api.rodiumai.io/v1/chat/completions" and vu["auth"] == "Bearer rd_sk_test"
    assert vu["body"]["tool_choice"] == "auto" and r.message["content"] == "ok"


# ------------------------------------------------------------------ règles ajoutées après les vrais tests
def test_chercher_produits_donne_l_alternative_en_cas_de_rupture(store):
    r = run(Toolbox(store).chercher_produits("sac en raphia"))["resultats"][0]
    assert r["rupture"] and [a["nom"] for a in r["alternatives"]] == ["Sandales en cuir"]  # même catégorie


def test_regle_livraison_et_rupture():
    from boutique.evaluation import evaluer_regles
    # test 2 réel : l'agent n'avait appelé aucun outil pour Lomé
    ev = evaluer_regles("Je vais vérifier les frais pour Lomé.", [], ["Livrez-moi à Lomé."])
    assert not ev["ok"] and "frais_livraison" in ev["problemes"][0]
    # test 3 réel : rupture signalée mais sans alternative
    res = [{"outil": "chercher_produits", "resultat": {"resultats": [
        {"nom": "Sac en raphia", "prix_xof": 7500, "rupture": True,
         "alternatives": [{"nom": "Sandales en cuir", "prix_xof": 9000}]}]}}]
    ev = evaluer_regles("Le sac est à 7 500 FCFA mais en rupture.", res, ["Combien le sac en raphia ?"])
    assert not ev["ok"] and "Sandales en cuir" in ev["problemes"][0]
    # test 3 réel (2e essai) : alternative proposée mais prix du sac oublié
    ev = evaluer_regles("Le sac est en rupture. Alternative : sandales en cuir à 9 000 FCFA.", res,
                        ["Combien le sac en raphia ?"])
    assert not ev["ok"] and "7500" in ev["problemes"][0]
    assert evaluer_regles("Le sac (7 500 FCFA) est en rupture ; les sandales en cuir sont à 9 000 FCFA.",
                          res, ["Combien le sac en raphia ?"])["ok"]


def test_agent_corrige_test2_lome(store):
    script = [texte("Je vais vérifier les frais pour Lomé. Quels produits voulez-vous ?"),   # sans outil
              outil(1, "frais_livraison", {"zone": "Lomé"}),
              texte("Désolée, Lomé n'est pas desservie. Nous livrons à Cotonou, Akpakpa, Fidjrossè, Calavi, "
                    "Porto-Novo et Ouidah 🙏")]
    rep = run(AgentVendeur(FakeLLM(script), Toolbox(store)).repondre([{"role": "user", "content": "Livrez-moi à Lomé."}]))
    assert rep.trace.outils_appeles == ["frais_livraison"] and "Calavi" in rep.texte
