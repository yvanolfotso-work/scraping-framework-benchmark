# -*- coding: utf-8 -*-
"""
common.py — briques partagees par A1, A2 et A3.

Tout ce qui touche au determinisme (tri, hash, ecriture JSON) et a la
tracabilite (provenance, empreintes) est centralise ici.
"""

import json
import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import config


# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

def get_logger(nom: str) -> logging.Logger:
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(nom)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    fh = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


# ----------------------------------------------------------------------
# TEMPS / HASH
# ----------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_texte(texte: str) -> str:
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


def sha256_octets(donnees: bytes) -> str:
    return hashlib.sha256(donnees).hexdigest()


def sha256_objet(obj) -> str:
    """Empreinte stable d'une structure Python (cles triees)."""
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return sha256_texte(payload)


def id_stable(*parties: str) -> str:
    """Identifiant court et reproductible a partir de plusieurs chaines."""
    return hashlib.sha256("||".join(parties).encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------
# JSON — lecture / ecriture deterministe
# ----------------------------------------------------------------------

def lire_json(chemin: Path) -> dict:
    chemin = Path(chemin)
    if not chemin.exists():
        raise FileNotFoundError(
            f"Fichier introuvable : {chemin}\n"
            f"Verifie SCRAPING_DIR dans config.py ou dans ton .env."
        )
    with open(chemin, "r", encoding="utf-8") as f:
        return json.load(f)


def ecrire_json(chemin: Path, donnees) -> None:
    """
    Ecriture atomique et deterministe :
      - cles triees  -> meme entree = meme octets
      - fichier .tmp puis remplacement  -> pas de fichier a moitie ecrit
    """
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    tmp = chemin.with_suffix(chemin.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            donnees, f,
            ensure_ascii=config.JSON_ENSURE_ASCII,
            indent=config.JSON_INDENT,
            sort_keys=True,
        )
        f.write("\n")
    tmp.replace(chemin)


# ----------------------------------------------------------------------
# NORMALISATION DE TEXTE
# ----------------------------------------------------------------------

_RE_ESPACES = re.compile(r"\s+")
_RE_NON_ALPHANUM = re.compile(r"[^a-z0-9\s'\-]")


def nettoyer(txt) -> str:
    if txt is None:
        return ""
    return _RE_ESPACES.sub(" ", str(txt)).strip()


def sans_accents(txt: str) -> str:
    nfkd = unicodedata.normalize("NFKD", txt)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def cle_normalisee(terme: str) -> str:
    """
    Cle de regroupement : minuscules, sans accents, ponctuation retiree.
    Sert a fusionner "Salvador Dalí" et "salvador dali" en un seul terme,
    SANS jamais perdre la forme d'origine (conservee a part).
    """
    t = sans_accents(nettoyer(terme).lower())
    t = _RE_NON_ALPHANUM.sub(" ", t)
    return _RE_ESPACES.sub(" ", t).strip()


def tokeniser(texte: str) -> list[str]:
    """
    Decoupe en tokens normalises, dans l'ordre d'apparition.

    L'apostrophe est traitee comme un separateur : "l'huile" donne
    ["l", "huile"]. Sans cela, "l'huile" restait un token unique que
    la liste de mots vides ne pouvait pas nettoyer, et on retrouvait
    des candidats du type "l'huile" au lieu de "huile".
    """
    t = sans_accents(nettoyer(texte).lower())
    t = t.replace("'", " ").replace("\u2019", " ")
    t = _RE_NON_ALPHANUM.sub(" ", t)
    return [tok for tok in t.split() if tok]


def ngrams(tokens: list[str], n: int) -> list[str]:
    if n <= 0 or len(tokens) < n:
        return []
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


# ----------------------------------------------------------------------
# NAVIGATION DANS LES CATALOGUES DU SCRAPER
# ----------------------------------------------------------------------

def iter_enregistrements(catalogue: dict, actifs_seulement: bool = True):
    """
    Les catalogues du scraper ont la forme :
        { "<id>": {"id":..., "status":..., "data": {...}, ...}, ... }
    On itere sur (id, data) en respectant un ordre stable (tri par id).
    """
    for pid in sorted(catalogue.keys()):
        entree = catalogue[pid]
        if not isinstance(entree, dict):
            continue
        if actifs_seulement and entree.get("status") not in (None, "active"):
            continue
        data = entree.get("data")
        if isinstance(data, dict):
            yield pid, data


# ----------------------------------------------------------------------
# MANIFESTE DE RUN (tracabilite des parametres)
# ----------------------------------------------------------------------

def parametres_effectifs(noms: list[str]) -> dict:
    """Capture la valeur des parametres de config utilises par une etape."""
    out = {}
    for nom in noms:
        val = getattr(config, nom, None)
        if isinstance(val, (set, frozenset)):
            val = sorted(val)
        elif isinstance(val, Path):
            val = str(val)
        out[nom] = val
    return out


def enregistrer_manifeste(etape: str, infos: dict) -> None:
    """Ajoute/remplace l'entree d'une etape dans out/manifest.json."""
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifeste = {}
    if config.MANIFEST.exists():
        try:
            with open(config.MANIFEST, "r", encoding="utf-8") as f:
                manifeste = json.load(f)
        except Exception:
            manifeste = {}
    manifeste[etape] = {"horodatage": now_iso(), **infos}
    ecrire_json(config.MANIFEST, manifeste)
