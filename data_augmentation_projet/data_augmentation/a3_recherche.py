# -*- coding: utf-8 -*-
"""
A3 — RECHERCHE EXTERNE (Wikipedia + Google)
===========================================

Entree  : out/requetes.json (produit par A2)
Sortie  : out/resultats_bruts.json  (+ out/raw/*.json : reponses brutes)

AUCUN LLM N'INTERVIENT DANS CE MODULE.
C'est la garantie demandee : tout ce qui sort d'ici est le contenu
renvoye par un serveur distant, jamais du texte genere.

Preuve d'origine conservee pour chaque appel :
  - url_appelee   : l'URL exacte (cle d'API masquee)
  - http_status   : le code de reponse
  - recu_le       : horodatage UTC
  - sha256_reponse: empreinte du corps de la reponse
  - fichier_brut  : chemin de la reponse brute sauvegardee sur disque

=> N'importe qui peut rejouer l'URL, recalculer le sha256 et verifier
   que le contenu n'a pas ete fabrique.

Fournisseurs (APIs officielles, pas de scraping de pages de resultats) :
  - wikipedia  : API MediaWiki publique, sans cle
  - google_cse : Google Programmable Search JSON API (cle + CX requis)
"""

import time
import json
from urllib.parse import urlencode

import requests

from . import config
from .common import (
    get_logger, lire_json, ecrire_json, id_stable, sha256_texte,
    sha256_objet, nettoyer, parametres_effectifs, enregistrer_manifeste,
    now_iso,
)

log = get_logger("A3")


# ----------------------------------------------------------------------
# Appel HTTP avec retries
# ----------------------------------------------------------------------

def appel_http(url: str, params: dict, headers: dict) -> tuple[requests.Response | None, str | None]:
    """Retourne (reponse, erreur). Reessaie avec backoff exponentiel."""
    derniere_erreur = None
    for tentative in range(1, config.A3_MAX_RETRIES + 1):
        try:
            resp = requests.get(
                url, params=params, headers=headers,
                timeout=config.A3_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp, None
            if resp.status_code in (429, 500, 502, 503, 504):
                derniere_erreur = f"HTTP {resp.status_code}"
                attente = config.A3_BACKOFF ** tentative
                log.warning(f"    {derniere_erreur} — nouvelle tentative dans {attente:.1f}s")
                time.sleep(attente)
                continue
            return resp, f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            derniere_erreur = str(e)
            attente = config.A3_BACKOFF ** tentative
            log.warning(f"    Erreur reseau ({e}) — nouvelle tentative dans {attente:.1f}s")
            time.sleep(attente)
    return None, derniere_erreur


def url_masquee(url: str, params: dict) -> str:
    """URL reconstruite pour la tracabilite, avec la cle d'API masquee."""
    sains = {k: ("***MASQUE***" if k in ("key", "api_key") else v)
             for k, v in params.items()}
    return f"{url}?{urlencode(sains)}"


def sauver_brut(prefixe: str, requete_id: str, contenu: str) -> str | None:
    if not config.A3_SAUVER_BRUT:
        return None
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    chemin = config.RAW_DIR / f"{prefixe}_{requete_id}.json"
    with open(chemin, "w", encoding="utf-8") as f:
        f.write(contenu)
    return str(chemin.relative_to(config.BASE_DIR))


# ----------------------------------------------------------------------
# Fournisseur : Wikipedia (API MediaWiki, sans cle)
# ----------------------------------------------------------------------

def chercher_wikipedia(requete: dict) -> dict:
    url = config.WIKIPEDIA_API.format(lang=config.A3_LANG)
    params = {
        "action": "query",
        "list": "search",
        "srsearch": requete["requete"],
        "srlimit": config.A3_MAX_RESULTATS_PAR_REQUETE,
        "format": "json",
        "utf8": 1,
    }
    headers = {"User-Agent": config.WIKIPEDIA_UA}

    resp, erreur = appel_http(url, params, headers)
    preuve = {
        "fournisseur": "wikipedia",
        "url_appelee": url_masquee(url, params),
        "recu_le": now_iso(),
        "http_status": resp.status_code if resp is not None else None,
        "erreur": erreur,
    }

    if resp is None or resp.status_code != 200:
        return {"preuve": preuve, "resultats": []}

    corps = resp.text
    preuve["sha256_reponse"] = sha256_texte(corps)
    preuve["fichier_brut"] = sauver_brut("wikipedia", requete["id"], corps)

    try:
        data = json.loads(corps)
    except json.JSONDecodeError as e:
        preuve["erreur"] = f"JSON invalide : {e}"
        return {"preuve": preuve, "resultats": []}

    resultats = []
    for i, item in enumerate(data.get("query", {}).get("search", [])):
        titre = nettoyer(item.get("title"))
        if not titre:
            continue
        # Wikipedia renvoie un extrait avec des balises <span> de surlignage
        extrait = nettoyer(
            str(item.get("snippet", ""))
            .replace('<span class="searchmatch">', "")
            .replace("</span>", "")
        )
        page_url = (
            f"https://{config.A3_LANG}.wikipedia.org/wiki/"
            + titre.replace(" ", "_")
        )
        resultats.append({
            "position": i + 1,
            "titre": titre,
            "url": page_url,
            "extrait": extrait,
            "pageid": item.get("pageid"),
        })
    return {"preuve": preuve, "resultats": resultats}


# ----------------------------------------------------------------------
# Fournisseur : Google Programmable Search (API officielle)
# ----------------------------------------------------------------------

def chercher_google_cse(requete: dict) -> dict:
    if not config.GOOGLE_API_KEY or not config.GOOGLE_CSE_ID:
        return {
            "preuve": {
                "fournisseur": "google_cse",
                "erreur": "GOOGLE_API_KEY / GOOGLE_CSE_ID absents "
                          "(voir .env.example) — fournisseur ignore",
                "recu_le": now_iso(),
                "http_status": None,
            },
            "resultats": [],
        }

    url = config.GOOGLE_CSE_ENDPOINT
    params = {
        "key": config.GOOGLE_API_KEY,
        "cx": config.GOOGLE_CSE_ID,
        "q": requete["requete"],
        "num": min(config.A3_MAX_RESULTATS_PAR_REQUETE, 10),  # max 10 cote API
        "hl": config.A3_LANG,
        "lr": f"lang_{config.A3_LANG}",
    }
    headers = {"Accept": "application/json"}

    resp, erreur = appel_http(url, params, headers)
    preuve = {
        "fournisseur": "google_cse",
        "url_appelee": url_masquee(url, params),
        "recu_le": now_iso(),
        "http_status": resp.status_code if resp is not None else None,
        "erreur": erreur,
    }

    if resp is None or resp.status_code != 200:
        if resp is not None:
            preuve["corps_erreur"] = resp.text[:500]
        return {"preuve": preuve, "resultats": []}

    corps = resp.text
    preuve["sha256_reponse"] = sha256_texte(corps)
    preuve["fichier_brut"] = sauver_brut("google", requete["id"], corps)

    try:
        data = json.loads(corps)
    except json.JSONDecodeError as e:
        preuve["erreur"] = f"JSON invalide : {e}"
        return {"preuve": preuve, "resultats": []}

    resultats = []
    for i, item in enumerate(data.get("items", [])):
        lien = nettoyer(item.get("link"))
        if not lien:
            continue
        resultats.append({
            "position": i + 1,
            "titre": nettoyer(item.get("title")),
            "url": lien,
            "extrait": nettoyer(item.get("snippet")),
            "site": nettoyer(item.get("displayLink")),
        })
    return {"preuve": preuve, "resultats": resultats}


FOURNISSEURS = {
    "wikipedia": chercher_wikipedia,
    "google_cse": chercher_google_cse,
}


# ----------------------------------------------------------------------
# Boucle principale
# ----------------------------------------------------------------------

def executer(limite: int | None = None, providers: list[str] | None = None) -> dict:
    log.info("=" * 60)
    log.info("A3 — Recherche externe (aucun LLM dans cette etape)")

    a2 = lire_json(config.A2_OUT)
    requetes = a2.get("requetes", [])
    log.info(f"Requetes en entree : {len(requetes)}")

    if limite:
        requetes = requetes[:limite]
        log.info(f"MODE TEST — limite a {len(requetes)} requetes")

    actifs = providers or config.A3_PROVIDERS
    inconnus = [p for p in actifs if p not in FOURNISSEURS]
    if inconnus:
        raise ValueError(f"Fournisseur(s) inconnu(s) : {inconnus}")
    log.info(f"Fournisseurs : {', '.join(actifs)}")

    enregistrements = []
    stats = {p: {"ok": 0, "echec": 0, "resultats": 0} for p in actifs}

    for idx, requete in enumerate(requetes, 1):
        log.info(f"[{idx}/{len(requetes)}] {requete['requete']}")
        for nom in actifs:
            reponse = FOURNISSEURS[nom](requete)
            preuve = reponse["preuve"]
            resultats = reponse["resultats"]

            if preuve.get("erreur"):
                stats[nom]["echec"] += 1
                log.warning(f"    {nom:<11} : ECHEC — {preuve['erreur']}")
            else:
                stats[nom]["ok"] += 1
                stats[nom]["resultats"] += len(resultats)
                log.info(f"    {nom:<11} : {len(resultats)} resultat(s)")

            for r in resultats:
                enregistrements.append({
                    "id": id_stable(nom, requete["id"], r["url"]),
                    # --- liaison remontante vers le corpus scrape ---
                    "requete_id": requete["id"],
                    "requete": requete["requete"],
                    "terme_id": requete["terme_id"],
                    "terme": requete["terme"],
                    "type_terme": requete["type_terme"],
                    "template": requete["template"],
                    # --- contenu renvoye par le serveur ---
                    "fournisseur": nom,
                    "position": r.get("position"),
                    "titre": r.get("titre"),
                    "url": r.get("url"),
                    "extrait": r.get("extrait"),
                    # --- preuve d'origine ---
                    "provenance": {
                        "origine": "web",
                        "url_appelee": preuve.get("url_appelee"),
                        "http_status": preuve.get("http_status"),
                        "recu_le": preuve.get("recu_le"),
                        "sha256_reponse": preuve.get("sha256_reponse"),
                        "fichier_brut": preuve.get("fichier_brut"),
                    },
                })

            time.sleep(config.A3_DELAI_ENTRE_REQUETES)

    enregistrements.sort(key=lambda e: (e["terme"].lower(), e["fournisseur"],
                                        e["position"] or 0, e["url"] or ""))

    log.info("----- RESUME -----")
    for nom in actifs:
        s = stats[nom]
        log.info(f"  {nom:<11} : {s['ok']} appel(s) OK, {s['echec']} echec(s), "
                 f"{s['resultats']} resultat(s)")
    log.info(f"  Total enregistrements : {len(enregistrements)}")

    sortie = {
        "meta": {
            "etape": "A3",
            "genere_le": now_iso(),
            "source": str(config.A2_OUT),
            "empreinte_requetes_a2": a2.get("meta", {}).get("empreinte_requetes"),
            "fournisseurs": actifs,
            "statistiques": stats,
            "nb_resultats": len(enregistrements),
            "llm_utilise": False,
            "parametres": parametres_effectifs([
                "A3_PROVIDERS", "A3_MAX_RESULTATS_PAR_REQUETE", "A3_LANG",
                "A3_DELAI_ENTRE_REQUETES", "A3_TIMEOUT", "A3_MAX_RETRIES",
                "A3_BACKOFF", "A3_SAUVER_BRUT",
            ]),
        },
        "resultats": enregistrements,
    }
    sortie["meta"]["empreinte_resultats"] = sha256_objet(
        [{k: v for k, v in e.items() if k != "provenance"} for e in enregistrements]
    )

    ecrire_json(config.A3_OUT, sortie)
    log.info(f"Ecrit -> {config.A3_OUT}")
    if config.A3_SAUVER_BRUT:
        log.info(f"Reponses brutes -> {config.RAW_DIR}/")

    enregistrer_manifeste("A3", {
        "sortie": str(config.A3_OUT),
        "nb_resultats": len(enregistrements),
        "statistiques": stats,
        "empreinte_resultats": sortie["meta"]["empreinte_resultats"],
    })
    return sortie
