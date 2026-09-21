# -*- coding: utf-8 -*-
"""
A3 — RECHERCHE EXTERNE (Wikipedia + Tavily)

Entree  : out/requetes.json (produit par A2)
Sortie  : out/resultats_bruts.json  (+ out/raw/*.json : reponses brutes)

AUCUN LLM N'INTERVIENT DANS CE MODULE.
Tout ce qui sort d'ici est le contenu renvoye par un serveur distant,
jamais du texte genere par nous. Pour Tavily, la reponse "synthetisee"
(include_answer) est explicitement desactivee : on ne garde que les
resultats (titre, url, extrait de la page).

Preuve d'origine conservee pour chaque appel :
  - url_appelee    : l'URL exacte (cle d'API jamais dans l'URL)
  - methode / corps_requete : pour les appels POST (Tavily), le corps
                     JSON envoye (il ne contient jamais de cle d'API)
  - http_status    : le code de reponse
  - recu_le        : horodatage UTC
  - sha256_reponse : empreinte du corps de la reponse
  - fichier_brut   : chemin de la reponse brute sauvegardee sur disque

=> N'importe qui peut rejouer l'appel (URL, ou URL + corps pour un POST),
   recalculer le sha256 et verifier que le contenu n'a pas ete fabrique.

Fournisseurs :
  - wikipedia  : API MediaWiki publique, sans cle
  - tavily     : API Tavily Search (cle requise, TAVILY_API_KEY dans .env)
  - google_cse : Google Programmable Search (conserve mais ferme pour ce
                 projet : erreur 403, ne pas l'activer)
"""

import time
import json
from urllib.parse import urlencode, quote, urlparse

import requests

from . import config
from .common import (
    get_logger, lire_json, ecrire_json, id_stable, sha256_texte,
    sha256_objet, nettoyer, parametres_effectifs, enregistrer_manifeste,
    now_iso,
)

log = get_logger("A3")

# Codes HTTP transitoires : on reessaie. Les autres sont definitifs.
_STATUTS_REESSAYABLES = (429, 500, 502, 503, 504)

# Codes signalant un probleme de configuration/credential/quota : inutile
# d'insister (401/403 : cle refusee ; 432/433 : quota Tavily epuise).
_STATUTS_FATALS = (401, 403, 432, 433)

_SESSION = requests.Session()
_DERNIER_APPEL = 0.0


def _attendre_cadence() -> None:
    """Respecte A3_DELAI_ENTRE_REQUETES entre deux appels reseau reels."""
    global _DERNIER_APPEL
    reste = config.A3_DELAI_ENTRE_REQUETES - (time.monotonic() - _DERNIER_APPEL)
    if reste > 0:
        time.sleep(reste)
    _DERNIER_APPEL = time.monotonic()


def appel_http(url: str, params: dict, headers: dict,
               json_corps: dict | None = None) -> tuple[requests.Response | None, str | None]:
    """Retourne (reponse, erreur). Reessaie avec backoff sur erreurs transitoires.

    GET par defaut ; POST avec un corps JSON si json_corps est fourni.
    """
    derniere_erreur = None
    for tentative in range(1, config.A3_MAX_RETRIES + 1):
        _attendre_cadence()
        try:
            if json_corps is None:
                resp = _SESSION.get(url, params=params, headers=headers,
                                    timeout=config.A3_TIMEOUT)
            else:
                resp = _SESSION.post(url, params=params, json=json_corps,
                                     headers=headers, timeout=config.A3_TIMEOUT)
            if resp.status_code == 200:
                return resp, None
            if resp.status_code in _STATUTS_REESSAYABLES and tentative < config.A3_MAX_RETRIES:
                derniere_erreur = f"HTTP {resp.status_code}"
                attente = config.A3_BACKOFF ** tentative
                log.warning(f"    {derniere_erreur} — nouvelle tentative dans {attente:.1f}s")
                time.sleep(attente)
                continue
            return resp, f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            derniere_erreur = str(e)
            if tentative < config.A3_MAX_RETRIES:
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
    """Ecrit la reponse brute sur disque et retourne son chemin relatif."""
    if not config.A3_SAUVER_BRUT:
        return None
    config.RAW_DIR.mkdir(parents=True, exist_ok=True)
    chemin = config.RAW_DIR / f"{prefixe}_{requete_id}.json"
    chemin.write_text(contenu, encoding="utf-8")
    return str(chemin.relative_to(config.BASE_DIR))


# ----------------------------------------------------------------------
# Fournisseur : Wikipedia (API MediaWiki, sans cle)
# ----------------------------------------------------------------------

def chercher_wikipedia(requete: dict) -> dict:
    """Interroge l'API de recherche MediaWiki et normalise les resultats."""
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
        extrait = nettoyer(
            str(item.get("snippet", ""))
            .replace('<span class="searchmatch">', "")
            .replace("</span>", "")
        )
        page_url = (
            f"https://{config.A3_LANG}.wikipedia.org/wiki/"
            + quote(titre.replace(" ", "_"), safe="_(),!$-")
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
# Fournisseur : Tavily (API de recherche web, POST + cle dans l'en-tete)
# ----------------------------------------------------------------------

def chercher_tavily(requete: dict) -> dict:
    """Interroge l'API Tavily Search et normalise les resultats."""
    if not config.TAVILY_API_KEY:
        return {
            "preuve": {
                "fournisseur": "tavily",
                "erreur": "TAVILY_API_KEY absente (voir .env) — fournisseur ignore",
                "recu_le": now_iso(),
                "http_status": None,
            },
            "resultats": [],
        }

    url = config.TAVILY_ENDPOINT
    corps_requete = {
        "query": requete["requete"],
        "search_depth": config.TAVILY_SEARCH_DEPTH,
        "max_results": min(config.A3_MAX_RESULTATS_PAR_REQUETE, 20),
        "include_answer": False,        # jamais de reponse synthetisee
        "include_raw_content": False,
        "include_images": False,
    }
    if config.TAVILY_EXCLURE_DOMAINES:
        corps_requete["exclude_domains"] = list(config.TAVILY_EXCLURE_DOMAINES)

    # La cle est dans l'en-tete Authorization : elle n'apparait jamais
    # dans l'URL, ni dans corps_requete, ni dans les fichiers de preuve.
    headers = {
        "Authorization": f"Bearer {config.TAVILY_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    resp, erreur = appel_http(url, {}, headers, json_corps=corps_requete)
    preuve = {
        "fournisseur": "tavily",
        "methode": "POST",
        "url_appelee": url,
        "corps_requete": corps_requete,
        "recu_le": now_iso(),
        "http_status": resp.status_code if resp is not None else None,
        "erreur": erreur,
    }

    if resp is None or resp.status_code != 200:
        if resp is not None:
            preuve["corps_erreur"] = resp.text[:500]
            # Tavily renvoie la cause dans detail (texte) ou detail.error
            try:
                detail = json.loads(resp.text).get("detail")
                if isinstance(detail, dict):
                    detail = detail.get("error")
                if detail:
                    preuve["erreur"] = f"{preuve['erreur']} — {nettoyer(str(detail))}"
            except (json.JSONDecodeError, AttributeError):
                pass
        return {"preuve": preuve, "resultats": []}

    corps = resp.text
    preuve["sha256_reponse"] = sha256_texte(corps)
    preuve["fichier_brut"] = sauver_brut("tavily", requete["id"], corps)

    try:
        data = json.loads(corps)
    except json.JSONDecodeError as e:
        preuve["erreur"] = f"JSON invalide : {e}"
        return {"preuve": preuve, "resultats": []}

    resultats = []
    for i, item in enumerate(data.get("results") or []):
        lien = nettoyer(item.get("url"))
        if not lien:
            continue
        resultats.append({
            "position": i + 1,
            "titre": nettoyer(item.get("title")),
            "url": lien,
            "extrait": nettoyer(item.get("content")),
            "site": nettoyer(urlparse(lien).netloc),
        })
    return {"preuve": preuve, "resultats": resultats}


# ----------------------------------------------------------------------
# Fournisseur : Google Programmable Search (conserve, ferme pour ce projet)
# ----------------------------------------------------------------------

def chercher_google_cse(requete: dict) -> dict:
    """Interroge l'API Programmable Search et normalise les resultats."""
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
        "num": min(config.A3_MAX_RESULTATS_PAR_REQUETE, 10),
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
            # L'API renvoie la cause exacte du refus dans error.message
            try:
                message = json.loads(resp.text).get("error", {}).get("message")
                if message:
                    preuve["erreur"] = f"{preuve['erreur']} — {nettoyer(message)}"
            except (json.JSONDecodeError, AttributeError):
                pass
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
    "tavily": chercher_tavily,
    "google_cse": chercher_google_cse,
}


def est_echec_fatal(preuve: dict) -> bool:
    """Vrai si l'echec vient de la configuration du fournisseur, pas du reseau."""
    return preuve.get("http_status") in _STATUTS_FATALS


# ----------------------------------------------------------------------
# Boucle principale
# ----------------------------------------------------------------------

def executer(limite: int | None = None, providers: list[str] | None = None) -> dict:
    """Execute chaque requete A2 sur chaque fournisseur actif et ecrit les resultats."""
    log.info("=" * 60)
    log.info("A3 — Recherche externe (aucun LLM dans cette etape)")

    if not config.A2_OUT.exists():
        raise FileNotFoundError(
            f"Sortie A2 introuvable : {config.A2_OUT}. Lance d'abord 'run.py a2'."
        )

    a2 = lire_json(config.A2_OUT)
    requetes = a2.get("requetes", [])
    if not requetes:
        raise ValueError(f"Sortie A2 vide ou illisible : {config.A2_OUT}")
    log.info(f"Requetes en entree : {len(requetes)}")

    if limite:
        requetes = requetes[:limite]
        log.info(f"MODE TEST — limite a {len(requetes)} requetes")

    actifs = list(providers or config.A3_PROVIDERS)
    inconnus = [p for p in actifs if p not in FOURNISSEURS]
    if inconnus:
        raise ValueError(f"Fournisseur(s) inconnu(s) : {inconnus}")
    log.info(f"Fournisseurs : {', '.join(actifs)}")

    enregistrements = []
    stats = {p: {"ok": 0, "echec": 0, "resultats": 0, "desactive": False} for p in actifs}
    echecs_consecutifs = {p: 0 for p in actifs}
    suspendus = set()

    for idx, requete in enumerate(requetes, 1):
        log.info(f"[{idx}/{len(requetes)}] {requete['requete']}")
        for nom in actifs:
            if nom in suspendus:
                continue

            reponse = FOURNISSEURS[nom](requete)
            preuve = reponse["preuve"]
            resultats = reponse["resultats"]

            if preuve.get("erreur"):
                stats[nom]["echec"] += 1
                log.warning(f"    {nom:<11} : ECHEC — {preuve['erreur']}")
                if est_echec_fatal(preuve):
                    echecs_consecutifs[nom] += 1
                    if echecs_consecutifs[nom] >= config.A3_ECHECS_FATALS_MAX:
                        suspendus.add(nom)
                        stats[nom]["desactive"] = True
                        log.error(
                            f"    {nom} suspendu pour ce run apres "
                            f"{echecs_consecutifs[nom]} echecs fatals consecutifs "
                            "(cle d'API, restrictions ou quota a verifier)."
                        )
            else:
                echecs_consecutifs[nom] = 0
                stats[nom]["ok"] += 1
                stats[nom]["resultats"] += len(resultats)
                log.info(f"    {nom:<11} : {len(resultats)} resultat(s)")

            for r in resultats:
                provenance = {
                    "origine": "web",
                    "url_appelee": preuve.get("url_appelee"),
                    "http_status": preuve.get("http_status"),
                    "recu_le": preuve.get("recu_le"),
                    "sha256_reponse": preuve.get("sha256_reponse"),
                    "fichier_brut": preuve.get("fichier_brut"),
                }
                # Appels POST (Tavily) : on garde de quoi rejouer l'appel.
                if preuve.get("corps_requete") is not None:
                    provenance["methode"] = preuve.get("methode")
                    provenance["corps_requete"] = preuve.get("corps_requete")

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
                    "provenance": provenance,
                })

        if suspendus and suspendus.issuperset(actifs):
            log.error("Tous les fournisseurs sont suspendus — arret de la boucle.")
            break

    enregistrements.sort(key=lambda e: ((e["terme"] or "").casefold(), e["fournisseur"],
                                        e["position"] or 0, e["url"] or ""))

    log.info("----- RESUME -----")
    for nom in actifs:
        s = stats[nom]
        suffixe = " [SUSPENDU]" if s["desactive"] else ""
        log.info(f"  {nom:<11} : {s['ok']} appel(s) OK, {s['echec']} echec(s), "
                 f"{s['resultats']} resultat(s){suffixe}")
    log.info(f"  Total enregistrements : {len(enregistrements)}")

    sortie = {
        "meta": {
            "etape": "A3",
            "genere_le": now_iso(),
            "source": str(config.A2_OUT),
            "empreinte_requetes_a2": a2.get("meta", {}).get("empreinte_requetes"),
            "fournisseurs": actifs,
            "fournisseurs_suspendus": sorted(suspendus),
            "statistiques": stats,
            "nb_resultats": len(enregistrements),
            "llm_utilise": False,
            "parametres": parametres_effectifs([
                "A3_PROVIDERS", "A3_MAX_RESULTATS_PAR_REQUETE", "A3_LANG",
                "A3_DELAI_ENTRE_REQUETES", "A3_TIMEOUT", "A3_MAX_RETRIES",
                "A3_BACKOFF", "A3_SAUVER_BRUT", "A3_ECHECS_FATALS_MAX",
                "TAVILY_SEARCH_DEPTH",
            ]),
        },
        "resultats": enregistrements,
    }
    sortie["meta"]["empreinte_resultats"] = sha256_objet(
        [{k: v for k, v in e.items() if k != "provenance"} for e in enregistrements]
    )

    ecrire_json(config.A3_OUT, sortie)
    log.info(f"Save Dans -> {config.A3_OUT}")
    if config.A3_SAUVER_BRUT:
        log.info(f"Reponses brutes -> {config.RAW_DIR}/")

    enregistrer_manifeste("A3", {
        "sortie": str(config.A3_OUT),
        "nb_resultats": len(enregistrements),
        "statistiques": stats,
        "fournisseurs_suspendus": sorted(suspendus),
        "empreinte_resultats": sortie["meta"]["empreinte_resultats"],
    })
    return sortie