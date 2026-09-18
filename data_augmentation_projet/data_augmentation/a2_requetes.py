# -*- coding: utf-8 -*-
"""
A2 — FORMULATION DES REQUETES DE RECHERCHE

Entree  : out/motscles_candidats.json (produit par A1)
Sortie  : out/requetes.json

Principe : une requete = un GABARIT + un TERME issu de A1.
Aucun texte libre n'est genere : le mode par defaut est "template",
100% deterministe, sans LLM. Chaque requete conserve :
  - terme_id  : le terme de A1 dont elle derive
  - template  : le gabarit exact utilise

=> On peut donc, pour n'importe quelle requete, remonter au terme, puis
   du terme remonter aux enregistrements du corpus. Aucune requete ne
   peut porter sur un sujet absent du corpus scrape.

La sortie porte aussi l'empreinte A1 : si le corpus change, le chainage
A1 -> A2 devient verifiable (et une incoherence est detectable).
"""

from collections import defaultdict

from . import config
from .common import (
    get_logger, lire_json, ecrire_json, id_stable, sha256_objet,
    parametres_effectifs, enregistrer_manifeste, now_iso, nettoyer,
)

log = get_logger("A2")

# Champs qu'un terme A1 doit obligatoirement porter pour etre exploitable.
_CHAMPS_TERME_REQUIS = ("id", "terme", "type")


def valider_gabarits() -> None:
    """Verifie au demarrage que chaque gabarit est utilisable (echec immediat sinon)."""
    groupes = list(config.A2_TEMPLATES.items()) + [("<defaut>", config.A2_TEMPLATE_DEFAUT)]
    for type_terme, gabarits in groupes:
        if not gabarits:
            raise ValueError(f"A2 : aucun gabarit defini pour le type '{type_terme}'.")
        for gabarit in gabarits:
            if "{terme}" not in gabarit:
                raise ValueError(
                    f"A2 : le gabarit '{gabarit}' (type '{type_terme}') "
                    "ne contient pas {terme} — il produirait la meme requete "
                    "pour tous les termes."
                )
            try:
                gabarit.format(terme="test")
            except (KeyError, IndexError) as exc:
                raise ValueError(
                    f"A2 : le gabarit '{gabarit}' (type '{type_terme}') "
                    f"contient un champ inconnu : {exc}"
                ) from None


def gabarits_pour(type_terme: str) -> list[str]:
    """Retourne les gabarits du type, ou les gabarits par defaut si le type est inconnu."""
    return config.A2_TEMPLATES.get(type_terme, config.A2_TEMPLATE_DEFAUT)


def terme_exploitable(terme: dict) -> bool:
    """Ecarte un terme A1 incomplet ou vide plutot que de produire une requete bancale."""
    if not isinstance(terme, dict):
        return False
    for champ in _CHAMPS_TERME_REQUIS:
        if not terme.get(champ):
            return False
    return bool(nettoyer(terme["terme"]))


def construire_requetes(termes: list[dict]) -> tuple[list[dict], dict]:
    """Croise chaque terme retenu avec ses gabarits ; retourne les requetes et les stats."""
    requetes = []
    vus = set()
    stats = {
        "termes_lus": len(termes),
        "termes_invalides": 0,
        "termes_hors_perimetre": 0,
        "termes_utilises": 0,
        "types_sans_gabarit_dedie": defaultdict(int),
        "doublons_ecartes": 0,
    }

    for terme in termes:
        if not terme_exploitable(terme):
            stats["termes_invalides"] += 1
            continue
        if terme["type"] not in config.A2_TYPES_INCLUS:
            stats["termes_hors_perimetre"] += 1
            continue

        libelle = nettoyer(terme["terme"])
        if terme["type"] not in config.A2_TEMPLATES:
            stats["types_sans_gabarit_dedie"][terme["type"]] += 1

        produites = 0
        for gabarit in gabarits_pour(terme["type"]):
            texte = nettoyer(gabarit.format(terme=libelle))
            if not texte:
                continue
            empreinte = texte.casefold()
            if empreinte in vus:
                stats["doublons_ecartes"] += 1
                continue
            vus.add(empreinte)
            requetes.append({
                "id": id_stable(texte),
                "requete": texte,
                "terme_id": terme["id"],
                "terme": libelle,
                "type_terme": terme["type"],
                "template": gabarit,
                "mode": config.A2_MODE,
            })
            produites += 1

        if produites:
            stats["termes_utilises"] += 1

    # Tri stable : type, terme, puis texte de la requete
    requetes.sort(key=lambda r: (r["type_terme"], r["terme"].casefold(), r["requete"]))

    if config.A2_MAX_REQUETES and len(requetes) > config.A2_MAX_REQUETES:
        log.info(
            f"A2_MAX_REQUETES={config.A2_MAX_REQUETES} — "
            f"{len(requetes) - config.A2_MAX_REQUETES} requetes tronquees"
        )
        requetes = requetes[:config.A2_MAX_REQUETES]

    stats["types_sans_gabarit_dedie"] = dict(sorted(stats["types_sans_gabarit_dedie"].items()))
    return requetes, stats


def executer(limite: int | None = None) -> dict:
    """Lit la sortie A1, genere les requetes, ecrit le JSON de sortie et le manifeste."""
    log.info("=" * 60)
    log.info("A2 — Formulation des requetes")

    if config.A2_MODE != "template":
        raise NotImplementedError(
            f"A2_MODE='{config.A2_MODE}' non supporte. "
            "Seul le mode 'template' (deterministe, sans LLM) est actif."
        )

    valider_gabarits()

    if not config.A1_OUT.exists():
        raise FileNotFoundError(
            f"Sortie A1 introuvable : {config.A1_OUT}. Lance d'abord 'run.py a1'."
        )

    a1 = lire_json(config.A1_OUT)
    termes = a1.get("termes", [])
    if not termes:
        raise ValueError(f"Sortie A1 vide ou illisible : {config.A1_OUT}")
    log.info(f"Termes en entree : {len(termes)}")

    if limite:
        termes = termes[:limite]
        log.info(f"MODE TEST — limite a {len(termes)} termes")

    requetes, stats = construire_requetes(termes)

    log.info(f"Termes hors perimetre (A2_TYPES_INCLUS) : {stats['termes_hors_perimetre']}")
    if stats["termes_invalides"]:
        log.info(f"Termes ecartes (incomplets) : {stats['termes_invalides']}")
    for type_terme, n in stats["types_sans_gabarit_dedie"].items():
        log.info(f"Type '{type_terme}' sans gabarit dedie : {n} termes -> gabarit par defaut")
    if stats["doublons_ecartes"]:
        log.info(f"Requetes dupliquees ecartees : {stats['doublons_ecartes']}")
    log.info(f"Requetes generees : {len(requetes)}")

    repartition = defaultdict(int)
    for r in requetes:
        repartition[r["type_terme"]] += 1
    for type_terme in sorted(repartition):
        log.info(f"  {type_terme:<14} : {repartition[type_terme]}")

    for r in requetes[:5]:
        log.info(f"  ex. [{r['type_terme']}] {r['requete']}")

    sortie = {
        "meta": {
            "etape": "A2",
            "genere_le": now_iso(),
            "source": str(config.A1_OUT),
            "empreinte_termes_a1": a1.get("meta", {}).get("empreinte_termes"),
            "nb_requetes": len(requetes),
            "repartition_par_type": dict(sorted(repartition.items())),
            "statistiques": stats,
            "parametres": parametres_effectifs([
                "A2_MODE", "A2_TEMPLATES", "A2_TEMPLATE_DEFAUT",
                "A2_MAX_REQUETES", "A2_TYPES_INCLUS",
            ]),
        },
        "requetes": requetes,
    }
    sortie["meta"]["empreinte_requetes"] = sha256_objet(requetes)

    ecrire_json(config.A2_OUT, sortie)
    log.info(f"Save Dans -> {config.A2_OUT}")
    log.info(f"Empreinte requetes : {sortie['meta']['empreinte_requetes']}")

    enregistrer_manifeste("A2", {
        "sortie": str(config.A2_OUT),
        "nb_requetes": len(requetes),
        "empreinte_requetes": sortie["meta"]["empreinte_requetes"],
        "empreinte_termes_a1": sortie["meta"]["empreinte_termes_a1"],
    })
    return sortie