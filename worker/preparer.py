"""Prépare le dossier du Worker avant `pywrangler dev` ou `pywrangler deploy`.

- copie le paquet ../boutique dans src/boutique (le même code que les paliers 1 et 2) ;
- génère src/donnees_defaut.py avec le catalogue et les tarifs initiaux.

    cd worker
    python preparer.py
"""
import json
import shutil
from pathlib import Path

ICI = Path(__file__).resolve().parent
RACINE = ICI.parent
SRC = ICI / "src"

cible = SRC / "boutique"
if cible.exists():
    shutil.rmtree(cible)
shutil.copytree(RACINE / "boutique", cible, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

catalogue = json.loads((RACINE / "data" / "catalogue_initial.json").read_text(encoding="utf-8"))
livraison = json.loads((RACINE / "data" / "livraison.json").read_text(encoding="utf-8"))
(SRC / "donnees_defaut.py").write_text(
    '"""Généré par worker/preparer.py : ne pas modifier à la main."""\n'
    f"CATALOGUE = {catalogue!r}\n\nLIVRAISON = {livraison!r}\n", encoding="utf-8")

print(f"✅ Copié boutique/ → {cible.relative_to(RACINE)} et généré src/donnees_defaut.py "
      f"({len(catalogue['produits'])} produits)")
