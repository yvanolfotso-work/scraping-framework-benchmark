#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
artiste_transform_json.py
------------------------------------------------------------------
Prend en entrée le fichier produit par scrapling_bartoux.py
(data_transform/input/artistes.json), et appelle un LLM (Groq, gratuit)
pour produire, pour CHAQUE artiste ayant une bio :

  - shortBio  : résumé condensé de la bio (2-3 phrases max)
  - univers   : description courte du style / de l'univers artistique
  - motsCles  : liste de mots-clés (4 à 6) qui caractérisent l'artiste
  - couleurs  : liste de couleurs si elles sont clairement identifiables
                dans la bio, sinon liste vide (jamais inventée)

Robustesse :
  - Réponse LLM validée strictement en JSON ; en cas d'échec de
    parsing, jusqu'à 3 tentatives avec un prompt plus strict.
  - Si le LLM échoue malgré tout, la fiche est quand même écrite
    avec des valeurs de secours (shortBio = bio tronquée, univers="",
    motsCles=[], couleurs=[]) et un flag `aCurer` pour repérage manuel
    (même convention que artiste_transform_json.py côté œuvres).
  - Reprise incrémentale : un artiste déjà enrichi (même bio, donc
    même hash) n'est PAS renvoyé au LLM, sauf --force. Ça évite de
    payer/consommer des appels API inutiles à chaque relance.
  - Une erreur sur un artiste n'interrompt jamais le traitement des
    autres (try/except par artiste + sauvegarde incrémentale).

Entrée  : data_transform/input/artistes.json
Sortie  : data_transform/output/artistes_enrichis.json

Pré-requis :
    pip install groq
    Variable d'environnement GROQ_API_KEY définie (clé gratuite Groq).

Usage :
    python artiste_transform_json.py
    python artiste_transform_json.py --input artistes.json --output artistes_enrichis.json
    python artiste_transform_json.py --force          # tout regénérer
    python artiste_transform_json.py --sleep 2         # espacer les appels API

------------------------------------------------------------------
FIX (voir revue) :
  Le fichier artistes.json produit par scrapling_bartoux.py a la forme :
      {
        "abc123hash": {
          "id": "...",
          "content_hash": "...",
          "status": "active",
          "data": { "name": ..., "url": ..., "bio": ..., "slug": ..., "category": ... },
          "history": []
        }
      }
  Les champs name/url/bio sont donc sous entree["data"], pas au niveau
  racine de chaque entrée. La fonction enrichir_tous() lit maintenant
  entree.get("data", entree) pour rester compatible avec les deux formats.
------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from dotenv import load_dotenv

try:
    from groq import Groq
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Le package 'groq' est introuvable. Installe-le avec :\n"
        "    pip install groq\n"
        f"(erreur d'import d'origine : {exc})"
    )

# ── Chemins / configuration ────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR.parent / ".env")

INPUT_FILE = BASE_DIR / "input" / "artistes.json"
OUTPUT_FILE = BASE_DIR / "output" / "artistes_enrichis.json"

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

MAX_TENTATIVES_LLM = 3
DELAI_ENTRE_APPELS_DEFAUT = 1.2  # secondes, pour rester sous les limites gratuites de Groq

SHORTBIO_MAX_CHARS = 260
UNIVERS_MAX_CHARS = 320

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("artiste_transform_json")


PROMPT_SYSTEME = """Tu es un assistant qui rédige des fiches artistes pour une galerie d'art.
Tu réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte avant ou après,
sans balises markdown (pas de ```), sur une seule ligne ou formaté, peu importe,
mais ce doit être un JSON strictement valide et RIEN d'autre.

Le JSON doit avoir exactement ces clés :
{
  "shortBio": string,   // résumé condensé de la biographie, 2 à 3 phrases, en français, au plus 260 caractères
  "univers": string,    // description courte du style / de l'univers artistique (thèmes, matières, sujets récurrents), au plus 320 caractères
  "motsCles": [string], // 4 à 6 mots-clés courts (1 à 3 mots chacun) qui caractérisent l'artiste
  "couleurs": [string]  // couleurs dominantes SI elles sont clairement mentionnées ou évidentes dans la bio (ex: bronze pour une sculpture en bronze). Si aucune couleur ne peut être déduite raisonnablement, renvoie une liste vide [].
}

Règles importantes :
- N'invente AUCUN fait qui n'est pas présent ou clairement déductible du texte fourni.
- Si tu ne peux pas déduire les couleurs, renvoie couleurs: [] plutôt que d'inventer.
- Le texte doit rester en français, sobre et factuel, dans le ton d'une galerie d'art.
"""


def construire_prompt_utilisateur(nom: str, bio: str) -> str:
    return (
        f"Nom de l'artiste : {nom}\n\n"
        f"Biographie source :\n\"\"\"\n{bio}\n\"\"\"\n\n"
        "Génère le JSON demandé pour cet artiste, à partir uniquement de ce texte."
    )


@dataclass
class ArtisteEnrichi:
    id: str
    name: str
    url: Optional[str]
    bio: Optional[str]
    shortBio: str = ""
    univers: str = ""
    motsCles: list[str] = field(default_factory=list)
    couleurs: list[str] = field(default_factory=list)
    aCurer: list[str] = field(default_factory=list)
    bioHash: str = ""
    enrichedAt: str = ""


# ── Client LLM (Groq) ───────────────────────────────────────────────
_client: Optional[Groq] = None


def obtenir_client() -> Groq:
    global _client
    if _client is None:
        if not GROQ_API_KEY:
            raise SystemExit(
                "GROQ_API_KEY n'est pas définie. Sur PowerShell :\n"
                '    $env:GROQ_API_KEY = "ta_cle_ici"\n'
                "puis relance le script."
            )
        _client = Groq(api_key=GROQ_API_KEY)
    return _client


def extraire_json(texte: str) -> Optional[dict]:
    """Essaie de parser du JSON même si le modèle a ajouté du texte autour."""
    texte = texte.strip()
    # Retire d'éventuelles balises de code
    texte = re.sub(r"^```(json)?", "", texte.strip(), flags=re.IGNORECASE).strip()
    texte = re.sub(r"```$", "", texte.strip()).strip()

    try:
        return json.loads(texte)
    except json.JSONDecodeError:
        pass

    # Fallback : on cherche le plus grand bloc {...} dans la réponse
    match = re.search(r"\{.*\}", texte, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def valider_et_nettoyer(data: dict) -> Optional[dict]:
    if not isinstance(data, dict):
        return None

    short_bio = data.get("shortBio")
    univers = data.get("univers")
    mots_cles = data.get("motsCles")
    couleurs = data.get("couleurs")

    if not isinstance(short_bio, str) or not short_bio.strip():
        return None
    if not isinstance(univers, str):
        univers = ""
    if not isinstance(mots_cles, list):
        mots_cles = []
    if not isinstance(couleurs, list):
        couleurs = []

    mots_cles = [str(m).strip() for m in mots_cles if str(m).strip()][:6]
    couleurs = [str(c).strip() for c in couleurs if str(c).strip()][:5]

    return {
        "shortBio": short_bio.strip()[:SHORTBIO_MAX_CHARS],
        "univers": univers.strip()[:UNIVERS_MAX_CHARS],
        "motsCles": mots_cles,
        "couleurs": couleurs,
    }


def appeler_llm(nom: str, bio: str) -> Optional[dict]:
    client = obtenir_client()
    prompt_utilisateur = construire_prompt_utilisateur(nom, bio)

    derniere_erreur: Optional[str] = None

    for tentative in range(1, MAX_TENTATIVES_LLM + 1):
        messages = [
            {"role": "system", "content": PROMPT_SYSTEME},
            {"role": "user", "content": prompt_utilisateur},
        ]
        if tentative > 1:
            messages.append({
                "role": "user",
                "content": (
                    "Ta réponse précédente n'était pas un JSON valide "
                    f"(erreur: {derniere_erreur}). Réponds cette fois UNIQUEMENT "
                    "avec le JSON demandé, rien d'autre, pas de ```."
                ),
            })

        try:
            reponse = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.4,
                max_tokens=600,
            )
            contenu = reponse.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            derniere_erreur = str(exc)
            log.warning("Appel Groq échoué pour '%s' (tentative %d/%d) : %s", nom, tentative, MAX_TENTATIVES_LLM, exc)
            time.sleep(1.5 * tentative)
            continue

        data = extraire_json(contenu)
        if data is None:
            derniere_erreur = "réponse non parsable en JSON"
            log.warning("JSON invalide pour '%s' (tentative %d/%d)", nom, tentative, MAX_TENTATIVES_LLM)
            continue

        valide = valider_et_nettoyer(data)
        if valide is None:
            derniere_erreur = "clés/JSON incomplet"
            log.warning("JSON incomplet pour '%s' (tentative %d/%d)", nom, tentative, MAX_TENTATIVES_LLM)
            continue

        return valide

    log.error("❌ Échec définitif de l'enrichissement LLM pour '%s' : %s", nom, derniere_erreur)
    return None


def valeurs_de_secours(bio: str) -> dict:
    """Utilisé seulement si le LLM échoue après toutes les tentatives."""
    resume = re.sub(r"\s+", " ", bio).strip()
    if len(resume) > SHORTBIO_MAX_CHARS:
        resume = resume[: SHORTBIO_MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return {"shortBio": resume, "univers": "", "motsCles": [], "couleurs": []}


def hash_bio(bio: str) -> str:
    return hashlib.sha256(bio.encode("utf-8")).hexdigest()[:16]


# ── Boucle principale ────────────────────────────────────────────
def enrichir_tous(input_path: Path, output_path: Path, force: bool, sleep_s: float, limit: Optional[int]) -> None:
    if not input_path.exists():
        log.error("Fichier d'entrée introuvable : %s", input_path)
        raise SystemExit(1)

    brut: dict[str, dict[str, Any]] = json.loads(input_path.read_text(encoding="utf-8"))
    log.info("%d artistes lus depuis %s", len(brut), input_path)

    # Reprise incrémentale
    resultats: dict[str, dict] = {}
    if output_path.exists() and not force:
        try:
            resultats = json.loads(output_path.read_text(encoding="utf-8"))
            log.info("Fichier de sortie existant chargé (%d artistes déjà enrichis).", len(resultats))
        except Exception:
            resultats = {}

    slugs = list(brut.keys())
    if limit:
        slugs = slugs[:limit]

    n_traites = 0
    n_llm_ok = 0
    n_llm_echecs = 0
    n_sans_bio = 0
    n_ignores_cache = 0

    for i, slug in enumerate(slugs, start=1):
        entree = brut[slug]

        # --- FIX ---
        # Le fichier artistes.json (sortie de merge_catalogue dans
        # scrapling_bartoux.py) a la forme :
        #   { "id":..., "content_hash":..., "status":..., "data": {...}, "history": [] }
        # Les champs utiles sont donc sous entree["data"], pas au niveau racine.
        # Le fallback "entree" (si "data" absent) garde la compatibilité avec
        # un éventuel format plat.
        d = entree.get("data", entree) if isinstance(entree, dict) else {}

        nom = d.get("name") or slug
        url = d.get("url")
        bio = d.get("bio")

        log.info("[%d/%d] %s", i, len(slugs), nom)

        if not bio or not bio.strip():
            n_sans_bio += 1
            log.warning("⚠️  Pas de bio pour '%s' — fiche créée vide, à compléter manuellement.", nom)
            resultats[slug] = asdict(ArtisteEnrichi(
                id=slug, name=nom, url=url, bio=None,
                shortBio="", univers="", motsCles=[], couleurs=[],
                aCurer=["bio", "shortBio", "univers", "motsCles", "couleurs"],
                bioHash="", enrichedAt=datetime.now().isoformat(timespec="seconds"),
            ))
            continue

        empreinte = hash_bio(bio)
        deja = resultats.get(slug)
        if not force and deja and deja.get("bioHash") == empreinte and deja.get("shortBio"):
            n_ignores_cache += 1
            log.info("↩️  '%s' déjà enrichi (bio inchangée) — ignoré (utilise --force pour regénérer).", nom)
            continue

        try:
            enrichissement = appeler_llm(nom, bio)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001 - un artiste en échec ne doit pas stopper le batch
            log.error("Erreur inattendue pendant l'enrichissement de '%s' : %s", nom, exc)
            enrichissement = None

        a_curer: list[str] = []
        if enrichissement is None:
            enrichissement = valeurs_de_secours(bio)
            a_curer = ["shortBio", "univers", "motsCles", "couleurs"]
            n_llm_echecs += 1
        else:
            n_llm_ok += 1
            if not enrichissement["couleurs"]:
                a_curer.append("couleurs")  # pas forcément une erreur, mais à vérifier

        resultats[slug] = asdict(ArtisteEnrichi(
            id=slug, name=nom, url=url, bio=bio,
            shortBio=enrichissement["shortBio"],
            univers=enrichissement["univers"],
            motsCles=enrichissement["motsCles"],
            couleurs=enrichissement["couleurs"],
            aCurer=a_curer,
            bioHash=empreinte,
            enrichedAt=datetime.now().isoformat(timespec="seconds"),
        ))

        n_traites += 1

        # Sauvegarde incrémentale : résiste aux interruptions / rate limits
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(resultats, ensure_ascii=False, indent=2), encoding="utf-8")

        time.sleep(sleep_s)

    log.info("✅ Terminé.")
    log.info("   %d artistes traités par le LLM cette exécution", n_traites)
    log.info("   %d enrichissements LLM réussis", n_llm_ok)
    log.info("   %d enrichissements LLM en échec (valeurs de secours utilisées)", n_llm_echecs)
    log.info("   %d artistes ignorés car déjà enrichis (cache)", n_ignores_cache)
    log.info("   %d artistes sans bio source", n_sans_bio)
    log.info("📄 Résultat écrit dans %s", output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrichit artistes.json via l'API Groq (shortBio, univers, motsCles, couleurs).")
    parser.add_argument("--input", type=Path, default=INPUT_FILE)
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE)
    parser.add_argument("--force", action="store_true", help="Regénère même les artistes déjà enrichis")
    parser.add_argument("--sleep", type=float, default=DELAI_ENTRE_APPELS_DEFAUT, help="Délai (s) entre deux appels LLM")
    parser.add_argument("--limit", type=int, default=None, help="Limiter à N artistes (pour tester)")
    args = parser.parse_args()

    enrichir_tous(
        input_path=args.input, output_path=args.output,
        force=args.force, sleep_s=args.sleep, limit=args.limit,
    )


if __name__ == "__main__":
    main()