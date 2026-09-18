# -*- coding: utf-8 -*-
"""
A2 — FORMULATION DES REQUETES DE RECHERCHE
==========================================

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
"""

from . import config
from .common import (
    get_logger, lire_json, ecrire_json, id_stable, sha256_objet,
    parametres_effectifs, enregistrer_manifeste, now_iso, nettoyer,
)

log = get_logger("A2")


def gabarits_pour(type_terme: str) -> list[str]:
    return config.A2_TEMPLATES.get(type_terme, config.A2_TEMPLATE_DEFAUT)


def construire_requetes(termes: list[dict]) -> list[dict]:
    requetes = []
    vus = set()

    for terme in termes:
        if terme["type"] not in config.A2_TYPES_INCLUS:
            continue
        libelle = nettoyer(terme["terme"])
        if not libelle:
            continue

        for gabarit in gabarits_pour(terme["type"]):
            texte = nettoyer(gabarit.format(terme=libelle))
            if not texte or texte.lower() in vus:
                continue
            vus.add(texte.lower())
            requetes.append({
                "id": id_stable(texte),
                "requete": texte,
                "terme_id": terme["id"],
                "terme": libelle,
                "type_terme": terme["type"],
                "template": gabarit,
                "mode": config.A2_MODE,
            })

    # Tri stable : type, terme, puis texte de la requete
    requetes.sort(key=lambda r: (r["type_terme"], r["terme"].lower(), r["requete"]))

    if config.A2_MAX_REQUETES:
        requetes = requetes[:config.A2_MAX_REQUETES]

    return requetes


def executer(limite: int | None = None) -> dict:
    log.info("=" * 60)
    log.info("A2 — Formulation des requetes")

    if config.A2_MODE != "template":
        raise NotImplementedError(
            f"A2_MODE='{config.A2_MODE}' non supporte. "
            "Seul le mode 'template' (deterministe, sans LLM) est actif."
        )

    a1 = lire_json(config.A1_OUT)
    termes = a1.get("termes", [])
    log.info(f"Termes en entree : {len(termes)}")

    if limite:
        termes = termes[:limite]
        log.info(f"MODE TEST — limite a {len(termes)} termes")

    requetes = construire_requetes(termes)
    log.info(f"Requetes generees : {len(requetes)}")
    for r in requetes[:5]:
        log.info(f"  ex. [{r['type_terme']}] {r['requete']}")

    sortie = {
        "meta": {
            "etape": "A2",
            "genere_le": now_iso(),
            "source": str(config.A1_OUT),
            "empreinte_termes_a1": a1.get("meta", {}).get("empreinte_termes"),
            "nb_requetes": len(requetes),
            "parametres": parametres_effectifs([
                "A2_MODE", "A2_TEMPLATES", "A2_TEMPLATE_DEFAUT",
                "A2_MAX_REQUETES", "A2_TYPES_INCLUS",
            ]),
        },
        "requetes": requetes,
    }
    sortie["meta"]["empreinte_requetes"] = sha256_objet(requetes)

    ecrire_json(config.A2_OUT, sortie)
    log.info(f"Ecrit -> {config.A2_OUT}")

    enregistrer_manifeste("A2", {
        "sortie": str(config.A2_OUT),
        "nb_requetes": len(requetes),
        "empreinte_requetes": sortie["meta"]["empreinte_requetes"],
    })
    return sortie
