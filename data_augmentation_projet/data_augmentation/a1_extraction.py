# -*- coding: utf-8 -*-


import re
from collections import defaultdict

from . import config
from .common import (
    get_logger, lire_json, ecrire_json, nettoyer, cle_normalisee,
    tokeniser, ngrams, iter_enregistrements, id_stable, sha256_objet,
    parametres_effectifs, enregistrer_manifeste, now_iso,
)

log = get_logger("A1")

_RE_DIMENSION = re.compile(config.A1_REGEX_DIMENSION, re.IGNORECASE)
_RE_SCHEMA = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_RE_EXTENSION = re.compile(r"\.(jpg|jpeg|png|webp|gif|html?)$", re.IGNORECASE)


# ----------------------------------------------------------------------
# Accumulateur de termes
# ----------------------------------------------------------------------

class Accumulateur:
    """Regroupe les occurrences par cle normalisee, en gardant les variantes."""

    def __init__(self):
        self.termes = defaultdict(
            lambda: {"types": defaultdict(int), "formes": defaultdict(int),
                     "occurrences": [], "total": 0}
        )

    def ajouter(self, forme: str, type_terme: str, fichier: str,
                record_id: str, champ: str) -> None:
        """Enregistre une occurrence brute (forme telle que lue, sans filtrage)."""
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
    """Filtre applique UNIQUEMENT aux n-grammes issus de la bio (texte libre)."""
    if not cle or cle in config.A1_BLACKLIST:
        return False
    tokens = cle.split()
    for tok in tokens:
        if tok in config.A1_STOPWORDS:
            return False
        if len(tok) < config.A1_LONGUEUR_MIN_TOKEN:
            return False
    if all(tok.isdigit() for tok in tokens):
        return False
    return True


def terme_structure_valide(cle: str) -> bool:
    """Filtre applique aux termes structures (name, category, parse d'URL)."""
    if not cle or cle in config.A1_BLACKLIST:
        return False
    if len(cle) < config.A1_LONGUEUR_MIN_TOKEN:
        return False
    return True


# ----------------------------------------------------------------------
# Parsing de l'URL produit (catalogue)
# ----------------------------------------------------------------------

def _segments_url(url: str) -> list[str]:
    """Decoupe une URL en segments de chemin, sans schema, domaine, query ni ancre."""
    if not url:
        return []
    url = url.split("?")[0].split("#")[0]
    url = _RE_SCHEMA.sub("", url)
    parties = [p for p in url.split("/") if p]
    return parties[1:] if parties else []


def _lisible(valeur: str) -> str:
    """Transforme un slug (tirets/underscores) en texte lisible, espaces normalises."""
    return re.sub(r"\s+", " ", valeur.replace("-", " ").replace("_", " ")).strip()


def parser_url_produit(url: str) -> dict:
    """
    Reconstruit (artiste, titre, dimension) depuis l'URL d'une fiche produit.
    Aucune invention : chaine vide si l'information n'est pas dans l'URL.
    """
    segments = _segments_url(url)
    if not segments:
        return {"artiste": "", "titre": "", "dimension": ""}

    artiste = ""
    for i, seg in enumerate(segments[:-1]):
        if seg.lower() in config.A1_URL_MARQUEURS_ARTISTE and i + 1 < len(segments) - 1:
            artiste = segments[i + 1]
            break

    slug = _RE_EXTENSION.sub("", segments[-1])

    dimension = ""
    m = _RE_DIMENSION.search(slug)
    if m:
        dimension = f"{m.group(1)}x{m.group(2)}cm"
        slug = slug[:m.start()] + slug[m.end():]

    blocs = [b for b in slug.split("_") if b]
    if blocs and artiste and _lisible(blocs[0]).lower() == _lisible(artiste).lower():
        blocs = blocs[1:]

    return {
        "artiste": _lisible(artiste),
        "titre": _lisible(" ".join(blocs)),
        "dimension": dimension,
    }


# ----------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------

def extraire_structures(catalogues: dict, acc: Accumulateur) -> int:
    """Collecte les champs structures declares dans config.A1_CHAMPS_STRUCTURES."""
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


def extraire_urls_catalogue(catalogues: dict, acc: Accumulateur) -> int:
    """Parse l'URL de chaque produit du catalogue (artiste / titre_oeuvre / dimension)."""
    n = 0
    catalogue = catalogues.get("catalogue")
    if not catalogue:
        return 0
    champ = config.A1_CHAMP_URL_CATALOGUE
    sans_url = 0
    for record_id, data in iter_enregistrements(catalogue):
        url = data.get(champ)
        if not url:
            sans_url += 1
            continue
        infos = parser_url_produit(url)
        for cle_info, type_terme in (
            ("artiste", "artiste"),
            ("titre", "titre_oeuvre"),
            ("dimension", "dimension"),
        ):
            if infos[cle_info]:
                acc.ajouter(infos[cle_info], type_terme, "catalogue", record_id, champ)
                n += 1
    if sans_url:
        log.info(f"Catalogue : {sans_url} enregistrements sans champ '{champ}' (ignores)")
    log.info(f"URLs catalogue : {n} occurrences collectees")
    return n


def extraire_texte_libre(catalogues: dict, acc: Accumulateur) -> int:
    """Extrait les n-grammes des champs texte libre (la bio artiste)."""
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
    """Type retenu : priorite aux types structures sur 'concept', puis ordre alpha stable."""
    structures = {t: n for t, n in types.items() if t != "concept"}
    candidats = structures or types
    return sorted(candidats.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def forme_canonique(formes: dict) -> str:
    """Forme d'origine la plus frequente ; egalite -> ordre alphabetique."""
    return sorted(formes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def construire_sortie(acc: Accumulateur) -> list[dict]:
    """Applique les seuils par type et produit la liste finale, triee de facon stable."""
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
    """Charge les sources, extrait, filtre, ecrit le JSON de sortie et le manifeste."""
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
    extraire_urls_catalogue(catalogues, acc)
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
                "A1_CHAMPS_STRUCTURES", "A1_CHAMPS_TEXTE",
                "A1_CHAMP_URL_CATALOGUE", "A1_URL_MARQUEURS_ARTISTE",
                "A1_REGEX_DIMENSION", "A1_NGRAM_MIN", "A1_NGRAM_MAX",
                "A1_FREQ_MIN", "A1_LONGUEUR_MIN_TOKEN", "A1_TOP_N_PAR_TYPE",
                "A1_GARDER_STRUCTURES_FREQ_1", "A1_MAX_OCCURRENCES_TRACEES",
            ]),
        },
        "termes": termes,
    }

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