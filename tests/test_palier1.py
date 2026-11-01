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
from boutique.tools import Toolbox

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture
def store(tmp_path):
    # toujours le stock INITIAL : les tests ne dépendent pas des commandes réelles passées en local
    shutil.copy(DATA / "catalogue_initial.json", tmp_path / "catalogue.json")
    shutil.copy(DATA / "livraison.json", tmp_path / "livraison.json")
    return JsonFileStore(tmp_path)


def fx_transport(xof_cad=0.00229, er_api_ok=True):
    def handler(request: httpx.Request):
        if "open.er-api.com" in request.url.host:
            if not er_api_ok:
                return httpx.Response(503)
            return httpx.Response(200, json={"result": "success", "time_last_update_utc": "test",
                                             "rates": {"CAD": xof_cad, "EUR": 1 / 655.957}})
        if "frankfurter" in request.url.host:
            return httpx.Response(200, json={"date": "2026-10-29", "rates": {"CAD": 1.502}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ outils
def test_chercher_produit(store):
    r = run(Toolbox(store).chercher_produit("wax vlisco bleu"))
    assert r["resultats"][0]["id"] == "P001"
    assert r["resultats"][0]["prix_xof"] == 25000


def test_rupture_propose_alternatives(store):
    r = run(Toolbox(store).verifier_stock("P002", 1))
    assert r["disponible"] is False
    assert r["alternatives"] and all(a["stock_disponible"] >= 1 for a in r["alternatives"])


def test_livraison_total(store):
    r = run(Toolbox(store).calculer_livraison("Abomey-Calavi", [{"produit_id": "P001", "quantite": 2}]))
    assert r["sous_total_articles_xof"] == 50000
    assert r["frais_livraison_xof"] == 1500
    assert r["total_xof"] == 51500  # total = articles + livraison


def test_livraison_zone_inconnue(store):
    r = run(Toolbox(store).calculer_livraison("Tombouctou", [{"produit_id": "P001", "quantite": 1}]))
    assert "erreur" in r and "cotonou" in r["zones_disponibles"]


def test_supplement_gros_volume(store):
    r = run(Toolbox(store).calculer_livraison("cotonou", [{"produit_id": "P003", "quantite": 7}]))
    assert r["frais_livraison_xof"] == 1000 + 2 * 300


def test_conversion_api_principale(store):
    r = run(Toolbox(store, fx_transport()).convertir_devise(51500, "CAD"))
    assert r["source_taux"] == "open.er-api.com"
    assert r["montant_converti"] == round(51500 * 0.00229, 2)


def test_conversion_repli_frankfurter(store):
    r = run(Toolbox(store, fx_transport(er_api_ok=False)).convertir_devise(655957, "CAD"))
    assert "frankfurter" in r["source_taux"]
    assert r["montant_converti"] == pytest.approx(1000 * 1.502, rel=1e-6)


def test_commande_refusee_sans_confirmation(store):
    r = run(Toolbox(store).creer_commande("Ada", "+229 97000000", "cotonou",
                                          [{"produit_id": "P001", "quantite": 1}], False))
    assert "erreur" in r


def test_commande_en_attente_gerante(store):
    tb = Toolbox(store)
    run(tb.verifier_stock("P001", 1))
    r = run(tb.creer_commande("Ada", "+229 97000000", "cotonou", [{"produit_id": "P001", "quantite": 1}], True))
    assert r["statut"] == "en_attente_validation_gerante" and r["total_xof"] == 26000
    assert len(run(store.list_orders())) == 1


def test_commande_rupture_refusee(store):
    r = run(Toolbox(store).creer_commande("Ada", "1", "cotonou", [{"produit_id": "A003", "quantite": 1}], True))
    assert "erreur" in r


# ------------------------------------------------------------------ évaluation
def test_detecte_chiffre_invente():
    outils = [{"total_xof": 51500}]
    assert chiffres_non_sources("Total : *51 500 FCFA*", outils) == []
    assert chiffres_non_sources("Total : *49 000 FCFA*", outils) == [49000.0]
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


def test_agent_enchaine_plusieurs_outils(store):
    script = [
        LLMResponse({"role": "assistant", "content": None, "tool_calls": [appel(1, "chercher_produit", {"requete": "vlisco bleu"})]}, {"prompt_tokens": 100, "completion_tokens": 10}),
        LLMResponse({"role": "assistant", "content": None, "tool_calls": [appel(2, "verifier_stock", {"produit_id": "P001", "quantite": 2})]}),
        LLMResponse({"role": "assistant", "content": None, "tool_calls": [appel(3, "calculer_livraison", {"zone": "abomey-calavi", "articles": [{"produit_id": "P001", "quantite": 2}]})]}),
        LLMResponse({"role": "assistant", "content": None, "tool_calls": [appel(4, "convertir_devise", {"montant": 51500, "devise_cible": "CAD"})]}),
        LLMResponse({"role": "assistant", "content": "Oui ✅ Total *51 500 FCFA* (≈ 117,94 $ CA). Je valide ?"}),
    ]
    agent = AgentVendeur(FakeLLM(script), Toolbox(store, fx_transport()), max_steps=6)
    rep = run(agent.repondre([{"role": "user", "content": "2 vlisco bleu pour Calavi, combien en CAD ?"}]))
    assert rep.trace.outils_appeles == ["chercher_produit", "verifier_stock", "calculer_livraison", "convertir_devise"]
    assert "51 500" in rep.texte
    assert rep.trace.resume()["tokens_entree"] == 100


def test_agent_corrige_un_chiffre_invente(store):
    script = [
        LLMResponse({"role": "assistant", "content": "Le pagne coûte 19 000 FCFA."}),  # inventé !
        LLMResponse({"role": "assistant", "content": None, "tool_calls": [appel(1, "chercher_produit", {"requete": "woodin"})]}),
        LLMResponse({"role": "assistant", "content": "Le pagne Woodin coûte *15 000 FCFA*."}),
    ]
    fake = FakeLLM(script)
    rep = run(AgentVendeur(fake, Toolbox(store)).repondre([{"role": "user", "content": "prix woodin ?"}]))
    assert "15 000" in rep.texte
    assert any(e["type"] == "evaluation" and "❌" in e["verdict"] for e in rep.trace.etapes)


def test_agent_limite_etapes(store):
    boucle = [LLMResponse({"role": "assistant", "content": None, "tool_calls": [appel(i, "chercher_produit", {"requete": "x"})]}) for i in range(10)]
    rep = run(AgentVendeur(FakeLLM(boucle), Toolbox(store), max_steps=3).repondre([{"role": "user", "content": "?"}]))
    assert rep.trace.etapes[-1]["type"] == "limite_atteinte"
    assert len(rep.trace.outils_appeles) == 3


# ------------------------------------------------------------------ client HTTP RodiumAI
def test_client_rodium_format_openai():
    vu = {}
    def handler(request):
        vu["url"] = str(request.url)
        vu["auth"] = request.headers["authorization"]
        vu["body"] = json.loads(request.content)
        return httpx.Response(200, json={"model": "openai/gpt-4o-mini", "usage": {"prompt_tokens": 5},
                                         "choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    c = RodiumClient("rd_sk_test", "https://api.rodiumai.io/v1", "openai/gpt-4o-mini",
                     transport=httpx.MockTransport(handler))
    r = run(c.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "f"}}]))
    assert vu["url"] == "https://api.rodiumai.io/v1/chat/completions"
    assert vu["auth"] == "Bearer rd_sk_test"
    assert vu["body"]["tool_choice"] == "auto"
    assert r.message["content"] == "ok"


def test_commande_refusee_si_produit_devine(store):
    """Bug réel du 31/10 : au 2e message, l'agent a deviné P001 au lieu du Woodin (P004)."""
    tb = Toolbox(store)
    run(tb.verifier_stock("P004", 1))       # message 1 : le Woodin est vérifié
    tb.nouveau_tour()                       # message 2 : « Oui je valide »
    r = run(tb.creer_commande("Ada", "1", "cotonou", [{"produit_id": "P001", "quantite": 1}], True))
    assert "non vérifiés" in r["erreur"]
    assert run(store.list_orders()) == []
    run(tb.verifier_stock("P004", 1))       # l'agent revérifie le bon produit
    r = run(tb.creer_commande("Ada", "1", "cotonou", [{"produit_id": "P004", "quantite": 1}], True))
    assert r["total_xof"] == 16000
