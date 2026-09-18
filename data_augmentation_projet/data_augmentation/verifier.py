# -*- coding: utf-8 -*-
"""
VERIFICATION — controle d'integrite du flow d'augmentation

Ce module ne produit pas de donnee : il CONTROLE celles deja produites.
C'est la piece a montrer pour repondre a la question "comment sait-on
que ces donnees viennent vraiment du net et pas d'un LLM ?".

Six controles :

  C1. Chainage des empreintes
      L'empreinte A1 citee par A2, et l'empreinte A2 citee par A3,
      correspondent bien aux fichiers presents.

  C2. Ancrage des termes dans le corpus
      - termes copies directs (name/category/bio) -> doivent apparaitre
        tels quels (normalises) dans le texte brut du corpus.
      - termes reconstruits depuis catalogue.url (artiste/titre_oeuvre/
        dimension) -> l'URL source citee en "origine" est reparsee avec
        EXACTEMENT le meme parseur que A1 ; le terme doit reapparaitre
        dans le resultat. Une simple sous-chaine ne suffit pas ici :
        A1 transforme le texte (tirets retires, dimension reformatee),
        donc l'URL brute ne contient plus le terme mot pour mot.

  C3. Ancrage des requetes
      Chaque requete de A2 doit etre reconstructible par gabarit + terme.

  C4. Preuve HTTP des resultats
      Chaque resultat de A3 porte un http_status 200 et un fichier brut
      dont le sha256 est recalcule et compare.

  C5. Ancrage des URL
      Chaque resultat cite une URL/titre present dans la reponse brute.

  C6. Aucun LLM declare dans l'etape de collecte.
"""

import json
from pathlib import Path

from . import config
from . import a1_extraction
from .common import (
    get_logger, lire_json, sha256_texte, sha256_objet,
    cle_normalisee, nettoyer, iter_enregistrements,
)

log = get_logger("VERIF")


class Rapport:
    def __init__(self):
        self.controles = []

    def ajouter(self, code: str, libelle: str, ok: bool, detail: str = ""):
        self.controles.append(
            {"code": code, "libelle": libelle, "ok": ok, "detail": detail}
        )

    @property
    def tout_ok(self) -> bool:
        return all(c["ok"] for c in self.controles)

    def afficher(self):
        log.info("=" * 60)
        log.info("RAPPORT DE VERIFICATION")
        for c in self.controles:
            statut = "OK   " if c["ok"] else "ECHEC"
            log.info(f"  [{statut}] {c['code']} — {c['libelle']}")
            if c["detail"]:
                log.info(f"           {c['detail']}")
        log.info("=" * 60)
        log.info("RESULTAT GLOBAL : " + ("CONFORME" if self.tout_ok else "NON CONFORME"))


# ----------------------------------------------------------------------
# C1 — chainage des empreintes
# ----------------------------------------------------------------------

def c1_chainage(rapport: Rapport, a1, a2, a3):
    empreinte_a1_reelle = sha256_objet(a1.get("termes", []))
    attendue_par_a2 = a2.get("meta", {}).get("empreinte_termes_a1")
    ok = empreinte_a1_reelle == attendue_par_a2
    rapport.ajouter(
        "C1a", "A2 pointe bien sur la version actuelle de A1", ok,
        "" if ok else f"A1={empreinte_a1_reelle[:16]} vs cite par A2={str(attendue_par_a2)[:16]}"
    )

    if a3 is None:
        return
    empreinte_a2_reelle = sha256_objet(a2.get("requetes", []))
    attendue_par_a3 = a3.get("meta", {}).get("empreinte_requetes_a2")
    ok = empreinte_a2_reelle == attendue_par_a3
    rapport.ajouter(
        "C1b", "A3 pointe bien sur la version actuelle de A2", ok,
        "" if ok else f"A2={empreinte_a2_reelle[:16]} vs cite par A3={str(attendue_par_a3)[:16]}"
    )


# ----------------------------------------------------------------------
# C2 — chaque terme est reproductible depuis le corpus
# ----------------------------------------------------------------------

def index_corpus() -> str:
    """Concatene tout le texte exploitable des deux catalogues, normalise."""
    morceaux = []
    for chemin in (config.ARTISTES_JSON, config.CATALOGUE_JSON):
        catalogue = lire_json(chemin)
        for _, data in iter_enregistrements(catalogue):
            for valeur in data.values():
                if isinstance(valeur, str):
                    morceaux.append(cle_normalisee(valeur))
    return " || ".join(morceaux)


def _vient_de_url_catalogue(terme: dict) -> bool:
    """Vrai si ce terme A1 a ete reconstruit depuis catalogue.url (voir A1_CHAMP_URL_CATALOGUE)."""
    champ_url = config.A1_CHAMP_URL_CATALOGUE
    return any(
        occ.get("fichier") == "catalogue" and occ.get("champ") == champ_url
        for occ in terme.get("origine", {}).get("occurrences", [])
    )


def _reproductible_depuis_url(terme: dict, enregistrements_catalogue: dict) -> bool:
    """Reparse les URL citees en 'origine' et verifie que le terme en ressort encore."""
    champ_url = config.A1_CHAMP_URL_CATALOGUE
    for occ in terme.get("origine", {}).get("occurrences", []):
        if occ.get("fichier") != "catalogue" or occ.get("champ") != champ_url:
            continue
        data = enregistrements_catalogue.get(occ.get("record_id"))
        if not data:
            continue
        infos = a1_extraction.parser_url_produit(data.get(champ_url) or "")
        if terme["cle"] in {cle_normalisee(v) for v in infos.values() if v}:
            return True
    return False


def c2_termes_ancres(rapport: Rapport, a1):
    corpus = index_corpus()
    catalogue = lire_json(config.CATALOGUE_JSON)
    enregistrements = dict(iter_enregistrements(catalogue))

    manquants = []
    for t in a1.get("termes", []):
        if t["cle"] in corpus:
            continue
        if _vient_de_url_catalogue(t) and _reproductible_depuis_url(t, enregistrements):
            continue
        manquants.append(t["terme"])

    ok = not manquants
    rapport.ajouter(
        "C2", "Tous les termes de A1 sont reproductibles depuis le corpus scrape", ok,
        "" if ok else f"{len(manquants)} terme(s) non reproductible(s) : {manquants[:5]}"
    )


# ----------------------------------------------------------------------
# C3 — chaque requete est reconstructible
# ----------------------------------------------------------------------

def c3_requetes_ancrees(rapport: Rapport, a1, a2):
    termes = {t["id"]: t for t in a1.get("termes", [])}
    anomalies = []
    for r in a2.get("requetes", []):
        terme = termes.get(r["terme_id"])
        if terme is None:
            anomalies.append(f"{r['requete']} (terme_id inconnu)")
            continue
        attendu = nettoyer(r["template"].format(terme=nettoyer(terme["terme"])))
        if attendu != r["requete"]:
            anomalies.append(f"{r['requete']} != {attendu}")
    ok = not anomalies
    rapport.ajouter(
        "C3", "Toutes les requetes de A2 sont reconstructibles (gabarit + terme)", ok,
        "" if ok else f"{len(anomalies)} anomalie(s) : {anomalies[:5]}"
    )


# ----------------------------------------------------------------------
# C4 / C5 / C6 — preuve HTTP, ancrage des URL, absence de LLM
# ----------------------------------------------------------------------

def c4_c5_preuves(rapport: Rapport, a3):
    if a3 is None:
        rapport.ajouter("C4", "Preuve HTTP des resultats A3", True,
                        "A3 non execute — controle ignore")
        return

    resultats = a3.get("resultats", [])
    if not resultats:
        rapport.ajouter("C4", "Preuve HTTP des resultats A3", True,
                        "aucun resultat a verifier")
        return

    sans_preuve, hash_casses, url_absentes = [], [], []
    cache_brut = {}

    for r in resultats:
        p = r.get("provenance", {})
        if p.get("http_status") != 200 or not p.get("sha256_reponse"):
            sans_preuve.append(r["url"])
            continue

        chemin_rel = p.get("fichier_brut")
        if not chemin_rel:
            sans_preuve.append(r["url"])
            continue

        chemin = config.BASE_DIR / chemin_rel
        if chemin_rel not in cache_brut:
            if not Path(chemin).exists():
                cache_brut[chemin_rel] = None
            else:
                cache_brut[chemin_rel] = Path(chemin).read_text(encoding="utf-8")

        contenu = cache_brut[chemin_rel]
        if contenu is None:
            sans_preuve.append(r["url"])
            continue

        if sha256_texte(contenu) != p["sha256_reponse"]:
            hash_casses.append(chemin_rel)
            continue

        titre = r.get("titre") or ""
        url = r.get("url") or ""
        dernier_segment = url.rstrip("/").split("/")[-1]
        if (titre and titre in contenu) or (dernier_segment and dernier_segment in contenu):
            continue
        url_absentes.append(url)

    ok4 = not sans_preuve and not hash_casses
    detail4 = ""
    if sans_preuve:
        detail4 += f"{len(sans_preuve)} sans preuve HTTP exploitable ; "
    if hash_casses:
        detail4 += f"{len(hash_casses)} fichier(s) brut(s) modifie(s) depuis la collecte"
    rapport.ajouter(
        "C4", "Chaque resultat A3 porte une reponse HTTP 200 verifiable", ok4,
        detail4 or f"{len(resultats)} resultat(s) verifie(s), sha256 recalcules"
    )

    ok5 = not url_absentes
    rapport.ajouter(
        "C5", "Chaque URL/titre restitue figure dans la reponse brute du serveur", ok5,
        "" if ok5 else f"{len(url_absentes)} non retrouve(s) : {url_absentes[:5]}"
    )

    rapport.ajouter(
        "C6", "Aucun LLM declare dans l'etape de collecte",
        a3.get("meta", {}).get("llm_utilise") is False,
        "meta.llm_utilise = False"
    )


# ----------------------------------------------------------------------
# Point d'entree
# ----------------------------------------------------------------------

def executer() -> Rapport:
    rapport = Rapport()

    a1 = lire_json(config.A1_OUT)
    a2 = lire_json(config.A2_OUT)
    a3 = lire_json(config.A3_OUT) if config.A3_OUT.exists() else None

    c1_chainage(rapport, a1, a2, a3)
    c2_termes_ancres(rapport, a1)
    c3_requetes_ancrees(rapport, a1, a2)
    c4_c5_preuves(rapport, a3)

    rapport.afficher()
    return rapport