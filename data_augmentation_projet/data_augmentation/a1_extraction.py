# -*- coding: utf-8 -*-
"""
A1 — EXTRACTION DES MOTS-CLES CANDIDATS
=======================================

Entree  : artistes.json + catalogue.json (produits par le scraper Bartoux)
Sortie  : out/motscles_candidats.json

Garanties :
  1. DETERMINISTE — memes fichiers d'entree + memes parametres
     => fichier de sortie identique a l'octet pres (tri stable, hash verifie).
  2. AUCUNE INVENTION — un terme ne peut exister en sortie que s'il a ete
     lu dans le corpus. Chaque terme porte la liste des enregistrements
     et des champs d'ou il provient.

Deux familles de termes :
  - STRUCTURES : valeur d'un champ identifie (nom d'artiste, categorie,
    titre d'oeuvre, technique). Gardes meme vus une seule fois.
  - TEXTE LIBRE : n-grammes extraits des bios et descriptions, filtres
    par mots vides, longueur et frequence minimale.
"""

from collections import defaultdict

from . import config
from .common import (
    get_logger, lire_json, ecrire_json, nettoyer, cle_normalisee,
    tokeniser, ngrams, iter_enregistrements, id_stable, sha256_objet,
    parametres_effectifs, enregistrer_manifeste, now_iso,
)

log = get_logger("A1")


# ----------------------------------------------------------------------
# Accumulateur de termes
# ----------------------------------------------------------------------

class Accumulateur:
    """Regroupe les occurrences par cle normalisee, en gardant les variantes."""

    def __init__(self):
        # cle -> {"types": {...}, "formes": {forme: n}, "occurrences": [...], "total": n}
        self.termes = defaultdict(
            lambda: {"types": defaultdict(int), "formes": defaultdict(int),
                     "occurrences": [], "total": 0}
        )

    def ajouter(self, forme: str, type_terme: str, fichier: str,
                record_id: str, champ: str) -> None:
        forme = nettoyer(forme)
        if not forme:
            return
        cle = cle_normalisee(forme)
        if not cle:
            return
        e = self.termes[cle]
        e["types"][type_terme] += 1
        e["formes"][forme] += 1
        e["total"] += 1
        if len(e["occurrences"]) < config.A1_MAX_OCCURRENCES_TRACEES:
            e["occurrences"].append(
                {"fichier": fichier, "record_id": record_id, "champ": champ}
            )


# ----------------------------------------------------------------------
# Filtres
# ----------------------------------------------------------------------

def terme_texte_valide(cle: str) -> bool:
    """Filtre applique UNIQUEMENT aux n-grammes issus du texte libre."""
    if not cle or cle in config.A1_BLACKLIST:
        return False
    tokens = cle.split()
    # tout n-gramme dont un token est un mot vide est rejete
    for tok in tokens:
        if tok in config.A1_STOPWORDS:
            return False
        if len(tok) < config.A1_LONGUEUR_MIN_TOKEN:
            return False
    # rejette les suites purement numeriques (dimensions, annees isolees)
    if all(tok.isdigit() for tok in tokens):
        return False
    return True


def terme_structure_valide(cle: str) -> bool:
    if not cle or cle in config.A1_BLACKLIST:
        return False
    if len(cle) < config.A1_LONGUEUR_MIN_TOKEN:
        return False
    return True


# ----------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------

def extraire_structures(catalogues: dict, acc: Accumulateur) -> int:
    n = 0
    for fichier, champ, type_terme in config.A1_CHAMPS_STRUCTURES:
        catalogue = catalogues.get(fichier)
        if not catalogue:
            continue
        for record_id, data in iter_enregistrements(catalogue):
            valeur = data.get(champ)
            if not valeur:
                continue
            acc.ajouter(valeur, type_terme, fichier, record_id, champ)
            n += 1
    log.info(f"Champs structures : {n} occurrences collectees")
    return n


def extraire_texte_libre(catalogues: dict, acc: Accumulateur) -> int:
    n = 0
    for fichier, champ in config.A1_CHAMPS_TEXTE:
        catalogue = catalogues.get(fichier)
        if not catalogue:
            continue
        for record_id, data in iter_enregistrements(catalogue):
            texte = data.get(champ)
            if not texte:
                continue
            tokens = tokeniser(texte)
            for taille in range(config.A1_NGRAM_MIN, config.A1_NGRAM_MAX + 1):
                for gram in ngrams(tokens, taille):
                    if not terme_texte_valide(gram):
                        continue
                    acc.ajouter(gram, "concept", fichier, record_id, champ)
                    n += 1
    log.info(f"Texte libre : {n} occurrences de n-grammes collectees")
    return n


def type_dominant(types: dict) -> str:
    """
    Type retenu pour un terme.

    Regle de priorite : un terme vu au moins une fois dans un CHAMP
    STRUCTURE est un terme structure, meme s'il apparait plus souvent
    dans du texte libre. Sans cette regle, un titre d'oeuvre cite dans
    une description (ex. "Montre Molle") basculait en "concept", puis
    etait elimine par le seuil de frequence des n-grammes.

    A egalite entre deux types structures : ordre alphabetique (stable).
    """
    structures = {t: n for t, n in types.items() if t != "concept"}
    candidats = structures or types
    return sorted(candidats.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def forme_canonique(formes: dict) -> str:
    """Forme d'origine la plus frequente ; egalite -> ordre alphabetique."""
    return sorted(formes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def construire_sortie(acc: Accumulateur) -> list[dict]:
    termes = []
    for cle, e in acc.termes.items():
        t_type = type_dominant(e["types"])
        est_structure = t_type != "concept"

        if est_structure:
            if not terme_structure_valide(cle):
                continue
            if not config.A1_GARDER_STRUCTURES_FREQ_1 and e["total"] < config.A1_FREQ_MIN:
                continue
        else:
            if e["total"] < config.A1_FREQ_MIN:
                continue

        termes.append({
            "id": id_stable(cle, t_type),
            "terme": forme_canonique(e["formes"]),
            "cle": cle,
            "type": t_type,
            "frequence": e["total"],
            "source_terme": "structure" if est_structure else "texte_libre",
            "variantes": sorted(e["formes"].keys()),
            "origine": {
                "fichiers": sorted({o["fichier"] for o in e["occurrences"]}),
                "champs": sorted({o["champ"] for o in e["occurrences"]}),
                "occurrences": e["occurrences"],
                "occurrences_tronquees": e["total"] > len(e["occurrences"]),
            },
        })

    # Tri stable : type, puis frequence decroissante, puis cle
    termes.sort(key=lambda t: (t["type"], -t["frequence"], t["cle"]))

    if config.A1_TOP_N_PAR_TYPE:
        par_type = defaultdict(list)
        for t in termes:
            if len(par_type[t["type"]]) < config.A1_TOP_N_PAR_TYPE:
                par_type[t["type"]].append(t)
        termes = [t for typ in sorted(par_type) for t in par_type[typ]]

    return termes


# ----------------------------------------------------------------------
# Point d'entree
# ----------------------------------------------------------------------

def executer(limite: int | None = None) -> dict:
    log.info("=" * 60)
    log.info("A1 — Extraction des mots-cles candidats")

    catalogues = {
        "artistes": lire_json(config.ARTISTES_JSON),
        "catalogue": lire_json(config.CATALOGUE_JSON),
    }
    log.info(f"artistes.json  : {len(catalogues['artistes'])} enregistrements")
    log.info(f"catalogue.json : {len(catalogues['catalogue'])} enregistrements")

    acc = Accumulateur()
    extraire_structures(catalogues, acc)
    extraire_texte_libre(catalogues, acc)

    termes = construire_sortie(acc)
    log.info(f"Termes retenus : {len(termes)}")

    if limite:
        termes = termes[:limite]
        log.info(f"MODE TEST — limite a {len(termes)} termes")

    repartition = defaultdict(int)
    for t in termes:
        repartition[t["type"]] += 1
    for typ in sorted(repartition):
        log.info(f"  {typ:<14} : {repartition[typ]}")

    sortie = {
        "meta": {
            "etape": "A1",
            "genere_le": now_iso(),
            "sources": {
                "artistes_json": str(config.ARTISTES_JSON),
                "catalogue_json": str(config.CATALOGUE_JSON),
            },
            "nb_termes": len(termes),
            "repartition_par_type": dict(sorted(repartition.items())),
            "parametres": parametres_effectifs([
                "A1_CHAMPS_STRUCTURES", "A1_CHAMPS_TEXTE", "A1_NGRAM_MIN",
                "A1_NGRAM_MAX", "A1_FREQ_MIN", "A1_LONGUEUR_MIN_TOKEN",
                "A1_TOP_N_PAR_TYPE", "A1_GARDER_STRUCTURES_FREQ_1",
                "A1_MAX_OCCURRENCES_TRACEES",
            ]),
        },
        "termes": termes,
    }

    # Empreinte du contenu utile (hors horodatage) : preuve de reproductibilite
    sortie["meta"]["empreinte_termes"] = sha256_objet(termes)

    ecrire_json(config.A1_OUT, sortie)
    log.info(f"Ecrit -> {config.A1_OUT}")
    log.info(f"Empreinte termes : {sortie['meta']['empreinte_termes']}")

    enregistrer_manifeste("A1", {
        "sortie": str(config.A1_OUT),
        "nb_termes": len(termes),
        "empreinte_termes": sortie["meta"]["empreinte_termes"],
    })
    return sortie
