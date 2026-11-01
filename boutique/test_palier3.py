"""Tests du palier 3 : stockage KV, service WhatsApp, garde-fous, Worker (runtime simulé)."""
import asyncio
import json
import sys
import types
from pathlib import Path

import httpx

from boutique.config import Settings
from boutique.llm import LLMResponse
from boutique.store_kv import KVStore
from boutique.whatsapp import ServiceWhatsApp
from tests.test_palier1 import DATA, FakeLLM, appel, run, store  # noqa: F401 (fixture)

CLIENT = "22990000001@c.us"
GERANTE = "22997000000@c.us"


class FakeKV:
    def __init__(self):
        self.d, self.ttl = {}, {}

    async def get(self, key):
        return self.d.get(key)

    async def put(self, key, value, expirationTtl=None):
        self.d[key] = value
        self.ttl[key] = expirationTtl


class FakeGreen:
    def __init__(self):
        self.envoyes = []

    async def envoyer(self, chat_id, message):
        self.envoyes.append((chat_id, message))
        return {"idMessage": "x"}

    def pour(self, chat):
        return [m for c, m in self.envoyes if c == chat]


def wh(chat, txt, mid):
    return {"typeWebhook": "incomingMessageReceived", "idMessage": mid,
            "senderData": {"chatId": chat, "senderName": "Test"},
            "messageData": {"typeMessage": "textMessage", "textMessageData": {"textMessage": txt}}}


def texte(t):
    return LLMResponse({"role": "assistant", "content": t})


def outil(i, nom, args):
    return LLMResponse({"role": "assistant", "content": None, "tool_calls": [appel(i, nom, args)]})


def settings():
    return Settings.from_env({"RODIUM_API_KEY": "x", "GERANTE_CHAT_ID": GERANTE, "JUGE_ACTIF": "0"})


def service(store, script, **kw):
    green = FakeGreen()
    return ServiceWhatsApp(store, green, settings(), FakeLLM(script), **kw), green


# ------------------------------------------------------------------ KV
def test_kvstore_defauts_et_ecriture():
    catalogue = json.loads((DATA / "catalogue_initial.json").read_text(encoding="utf-8"))
    kv = FakeKV()
    st = KVStore(kv, defauts={"catalogue": catalogue})
    assert len(run(st.get_catalogue())["produits"]) == 10       # données par défaut si KV vide
    run(st.save_order({"id": "CMD-1", "statut": "x"}))
    assert run(st.get_order("CMD-1"))["statut"] == "x"
    run(st.kv_put("vu:1", "1", ttl=10))
    assert kv.ttl["vu:1"] == 60                                  # TTL minimum de Cloudflare KV


# ------------------------------------------------------------------ service WhatsApp
def test_client_recoit_une_reponse_et_historique(store):
    svc, green = service(store, [outil(1, "chercher_produit", {"requete": "woodin"}),
                                 texte("Le *Pagne Woodin « Soleil »* est à *15 000 FCFA* 😊")])
    assert run(svc.traiter(wh(CLIENT, "prix du woodin ?", "M1"))) == "repondu"
    assert "15 000" in green.pour(CLIENT)[0]
    hist = json.loads(run(store.kv_get(f"hist:{CLIENT}")))
    assert [m["role"] for m in hist] == ["user", "assistant"]


def test_doublon_ignore(store):
    svc, green = service(store, [texte("Bonjour 😊"), texte("ne doit pas servir")])
    assert run(svc.traiter(wh(CLIENT, "bonjour", "M1"))) == "repondu"
    assert run(svc.traiter(wh(CLIENT, "bonjour", "M1"))) == "doublon"
    assert len(green.envoyes) == 1


def test_groupe_et_autres_webhooks_ignores(store):
    svc, green = service(store, [])
    assert run(svc.traiter(wh("123-456@g.us", "salut", "M1"))) == "ignore:groupe"
    assert run(svc.traiter({"typeWebhook": "outgoingMessageStatus"})) == "ignore:type"
    assert green.envoyes == []


def test_parcours_complet_commande_puis_validation_gerante(store):
    commande = {"client_nom": "Koffi", "client_telephone": "000 faux numéro", "zone": "cotonou",
                "articles": [{"produit_id": "P004", "quantite": 1}], "confirmation_client": True}
    svc, green = service(store, [outil(1, "verifier_stock", {"produit_id": "P004", "quantite": 1}),
                                 outil(2, "creer_commande", commande),
                                 texte("Merci Koffi ✅ Votre commande est transmise à la gérante.")])
    run(svc.traiter(wh(CLIENT, "Oui je valide, Koffi", "M1")))

    cmd = run(store.list_orders())[0]
    assert cmd["statut"] == "en_attente_validation_gerante"
    assert cmd["client"]["telephone"] == "+22990000001"            # vrai numéro WhatsApp, pas celui du LLM
    assert f"OK {cmd['id']}" in green.pour(GERANTE)[0]            # la gérante a reçu le récapitulatif

    assert run(svc.traiter(wh(GERANTE, f"ok {cmd['id'].lower()}", "M2"))) == "gerante:validee"
    assert run(store.get_order(cmd["id"]))["statut"] == "validee"
    produits = run(store.get_catalogue())["produits"]
    assert next(p for p in produits if p["id"] == "P004")["stock"] == 11  # 12 - 1
    assert "validée" in green.pour(CLIENT)[-1]                     # le client est prévenu
    hist = json.loads(run(store.kv_get(f"hist:{CLIENT}")))
    assert "validee" in hist[-1]["memo"]                           # Awa connaît la décision


def test_un_client_ne_peut_pas_valider(store):
    run(store.save_order({"id": "CMD-X", "statut": "en_attente_validation_gerante", "client": {"nom": "a"},
                          "lignes": [], "total_xof": 1}))
    svc, green = service(store, [texte("Seule la gérante peut valider une commande 🙏")])
    assert run(svc.traiter(wh(CLIENT, "OK CMD-X", "M1"))) == "repondu"   # traité comme un message client
    assert run(store.get_order("CMD-X"))["statut"] == "en_attente_validation_gerante"


def test_gerante_refuse(store):
    run(store.save_order({"id": "CMD-Y", "statut": "en_attente_validation_gerante",
                          "client": {"nom": "a", "chat_id": CLIENT}, "lignes": [], "total_xof": 1,
                          "zone": "cotonou", "delai": "24 h"}))
    svc, green = service(store, [])
    assert run(svc.traiter(wh(GERANTE, "NON CMD-Y rupture", "M1"))) == "gerante:refusee"
    assert run(store.get_order("CMD-Y"))["motif"] == "rupture"
    assert "n'a pas pu être validée" in green.pour(CLIENT)[0]


def test_limite_de_messages(store):
    svc, green = service(store, [texte("a"), texte("b")], limite_par_heure=2)
    statuts = [run(svc.traiter(wh(CLIENT, f"msg {i}", f"M{i}"))) for i in range(4)]
    assert statuts == ["repondu", "repondu", "limite_client", "limite_client"]
    assert len(green.pour(CLIENT)) == 3  # 2 réponses + 1 seul avertissement


# ------------------------------------------------------------------ Worker (runtime Cloudflare simulé)
class FakeResponse:
    def __init__(self, body="", status=200):
        self.body, self.status = body, status

    @classmethod
    def json(cls, data, status=200):
        return cls(json.dumps(data), status)


def charger_worker(tmp_path):
    sys.modules["workers"] = types.SimpleNamespace(Response=FakeResponse, WorkerEntrypoint=object)
    (tmp_path / "donnees_defaut.py").write_text(
        f"CATALOGUE = {json.loads((DATA / 'catalogue_initial.json').read_text(encoding='utf-8'))!r}\n"
        f"LIVRAISON = {json.loads((DATA / 'livraison.json').read_text(encoding='utf-8'))!r}\n", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "entry_test", Path(__file__).resolve().parent.parent / "worker" / "src" / "entry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRequest:
    def __init__(self, method, url, headers=None, body=None):
        self.method, self.url, self.headers, self._body = method, url, headers or {}, body

    async def json(self):
        return self._body


def test_worker_routes(tmp_path):
    mod = charger_worker(tmp_path)
    w = mod.Default()
    taches = []
    w.env = types.SimpleNamespace(BOUTIQUE_KV=FakeKV(), WEBHOOK_TOKEN="secret", RODIUM_API_KEY="k",
                                  GREEN_API_TOKEN="t", GREEN_ID_INSTANCE="1", GERANTE_CHAT_ID=GERANTE,
                                  JUGE_ACTIF="0")
    w.ctx = types.SimpleNamespace(waitUntil=taches.append)

    r = run(w.fetch(FakeRequest("GET", "https://x.workers.dev/")))
    assert json.loads(r.body)["secrets_presents"]["RODIUM_API_KEY"] is True

    r = run(w.fetch(FakeRequest("POST", "https://x.workers.dev/webhook", {"Authorization": "Bearer faux"}, {})))
    assert r.status == 401 and taches == []

    r = run(w.fetch(FakeRequest("POST", "https://x.workers.dev/webhook", {"Authorization": "Bearer secret"},
                                {"typeWebhook": "stateInstanceChanged"})))
    assert r.status == 200 and len(taches) == 1          # réponse immédiate, travail en arrière-plan
    assert run(taches[0]) == "ignore:type"

    r = run(w.fetch(FakeRequest("POST", "https://x.workers.dev/admin/init", {"Authorization": "Bearer secret"})))
    assert json.loads(r.body)["produits"] == 10
    assert "catalogue" in w.env.BOUTIQUE_KV.d
