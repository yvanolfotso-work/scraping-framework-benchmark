#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
artiste_transform_json.py
------------------------------------------------------------------
Enrichit data_transform/input/artistes.json via un LLM
(shortBio, univers, motsCles + eval_motCles scorés, couleurs).

Provider configurable via .env :
    LLM_PROVIDER=groq|anthropic|openai

Pré-requis :
    pip install groq anthropic openai python-dotenv

Usage :
    python artiste_transform_json.py
    python artiste_transform_json.py --force
    python artiste_transform_json.py --score-min 0.7 --limit 5
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

# ── Chemins / configuration ────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")

INPUT_FILE = BASE_DIR / "input" / "artistes.json"
OUTPUT_FILE = BASE_DIR / "output" / "artistes_enrichis.json"

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq").strip().lower()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

MAX_TENTATIVES_LLM = 3
DELAI_ENTRE_APPELS_DEFAUT = 1.2

SHORTBIO_MAX_CHARS = 260
UNIVERS_MAX_CHARS = 320

MOTCLE_SCORE_MIN_DEFAUT = 0.6
MOTCLE_MIN_COUNT = 4
MOTCLE_MAX_COUNT = 6

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
  "motsCles": [         // 4 à 6 mots-clés courts (1 à 3 mots chacun) qui caractérisent l'artiste
    {
      "mot": string,    // le mot-clé lui-même
      "score": number   // TON auto-évaluation de la pertinence de ce mot-clé, de 0.0 à 1.0 :
                         //   - 1.0 = le mot-clé est explicitement présent ou directement et
                         //           sans ambiguïté déductible du texte fourni
                         //   - 0.5 = déduction plausible mais un peu générique ou indirecte
                         //   - 0.0 = tu n'es pas sûr, le mot-clé est un remplissage générique
                         // Sois un évaluateur honnête et sévère : n'attribue pas un score élevé
                         // par complaisance, un mot-clé vague ou passe-partout (ex: "artiste",
                         // "créatif", "talentueux") doit recevoir un score bas.
    }
  ],
  "couleurs": [string]  // couleurs dominantes SI elles sont clairement mentionnées ou évidentes dans la bio (ex: bronze pour une sculpture en bronze). Si aucune couleur ne peut être déduite raisonnablement, renvoie une liste vide [].
}

Règles importantes :
- N'invente AUCUN fait qui n'est pas présent ou clairement déductible du texte fourni.
- Si tu ne peux pas déduire les couleurs, renvoie couleurs: [] plutôt que d'inventer.
- Chaque mot-clé doit être spécifique à cet artiste et à ce texte, pas un terme générique
  applicable à n'importe quel artiste de galerie.
- Le texte doit rester en français, sobre et factuel, dans le ton d'une galerie d'art.

Note : ce format {mot, score} est uniquement pour TA réponse à toi (le LLM) — le score sert
en aval à filtrer les mots-clés peu pertinents. N'y pense pas autrement, réponds juste avec
ce format demandé.
"""


def construire_prompt_utilisateur(nom: str, bio: str) -> str:
    return (
        f"Nom de l'artiste : {nom}\n\n"
        f"Biographie source :\n\"\"\"\n{bio}\n\"\"\"\n\n"
        "Génère le JSON demandé pour cet artiste, à partir uniquement de ce texte. "
        "N'oublie pas d'auto-évaluer honnêtement le score de chaque mot-clé."
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
    eval_motCles: list[dict] = field(default_factory=list)
    couleurs: list[str] = field(default_factory=list)
    aCurer: list[str] = field(default_factory=list)
    bioHash: str = ""
    enrichedAt: str = ""


# ── Clients LLM ─────────────────────────────────────────────────────

_groq_client = None
_anthropic_client = None
_openai_client = None


def _obtenir_client_groq():
    global _groq_client
    if _groq_client is None:
        try:
            from groq import Groq
        except ImportError as exc:
            raise SystemExit(
                "Package 'groq' manquant. Installe-le avec : pip install groq\n"
                f"(erreur : {exc})"
            )
        if not GROQ_API_KEY:
            raise SystemExit(
                "GROQ_API_KEY n'est pas définie.\n"
                '  PowerShell : $env:GROQ_API_KEY = "ta_cle"\n'
                "  ou dans le fichier .env"
            )
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


def _obtenir_client_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        try:
            import anthropic
        except ImportError as exc:
            raise SystemExit(
                "Package 'anthropic' manquant. Installe-le avec : pip install anthropic\n"
                f"(erreur : {exc})"
            )
        if not ANTHROPIC_API_KEY:
            raise SystemExit(
                "ANTHROPIC_API_KEY n'est pas définie.\n"
                "Ajoute-la dans le fichier .env"
            )
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


def _obtenir_client_openai():
    global _openai_client
    if _openai_client is None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise SystemExit(
                "Package 'openai' manquant. Installe-le avec : pip install openai\n"
                f"(erreur : {exc})"
            )
        if not OPENAI_API_KEY:
            raise SystemExit(
                "OPENAI_API_KEY n'est pas définie.\n"
                "Ajoute-la dans le fichier .env"
            )
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


def _appeler_provider(messages: list[dict]) -> str:
    """Envoie les messages au provider configuré et retourne le contenu texte."""
    if LLM_PROVIDER == "groq":
        client = _obtenir_client_groq()
        # Certains modèles Groq supportent reasoning_format ; on le tente,
        # sinon on retombe sur un appel classique.
        try:
            reponse = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.4,
                max_tokens=2000,
                reasoning_format="parsed",
            )
        except TypeError:
            reponse = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                temperature=0.4,
                max_tokens=2000,
            )
        return reponse.choices[0].message.content or ""

    if LLM_PROVIDER == "anthropic":
        client = _obtenir_client_anthropic()
        # Anthropic sépare system / messages
        system = ""
        user_messages = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            else:
                user_messages.append({"role": m["role"], "content": m["content"]})
        reponse = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            temperature=0.4,
            system=system,
            messages=user_messages,
        )
        # Contenu texte
        parts = [block.text for block in reponse.content if hasattr(block, "text")]
        return "\n".join(parts)

    if LLM_PROVIDER == "openai":
        client = _obtenir_client_openai()
        reponse = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.4,
            max_tokens=2000,
        )
        return reponse.choices[0].message.content or ""

    raise SystemExit(
        f"LLM_PROVIDER inconnu : '{LLM_PROVIDER}'. "
        "Valeurs acceptées : groq | anthropic | openai"
    )


def extraire_json(texte: str) -> Optional[dict]:
    texte = texte.strip()
    texte = re.sub(r"^```(json)?", "", texte.strip(), flags=re.IGNORECASE).strip()
    texte = re.sub(r"```$", "", texte.strip()).strip()

    try:
        return json.loads(texte)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", texte, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _normaliser_score(valeur: Any) -> Optional[float]:
    try:
        score = float(valeur)
    except (TypeError, ValueError):
        return None
    if score != score:  # NaN
        return None
    if score > 1.0:
        if score <= 10.0:
            score = score / 10.0
        elif score <= 100.0:
            score = score / 100.0
        else:
            return None
    return max(0.0, min(1.0, score))


def valider_et_nettoyer_motscles(mots_cles_brut: Any) -> tuple[list[dict], bool]:
    if not isinstance(mots_cles_brut, list):
        return [], True

    normalises: list[dict] = []
    vus: set[str] = set()
    format_suspect = False

    for item in mots_cles_brut:
        mot: Optional[str] = None
        score: Optional[float] = None

        if isinstance(item, dict):
            mot_brut = item.get("mot") or item.get("keyword") or item.get("value")
            mot = str(mot_brut).strip() if mot_brut else None
            score = _normaliser_score(item.get("score"))
            if score is None:
                format_suspect = True
        elif isinstance(item, str):
            mot = item.strip()
            score = None
            format_suspect = True
        else:
            format_suspect = True
            continue

        if not mot:
            format_suspect = True
            continue

        cle_dedup = mot.lower()
        if cle_dedup in vus:
            continue
        vus.add(cle_dedup)

        normalises.append({"mot": mot, "score": score if score is not None else 0.0})

    normalises.sort(key=lambda x: x["score"], reverse=True)
    return normalises, format_suspect


def valider_et_nettoyer(data: dict) -> Optional[dict]:
    if not isinstance(data, dict):
        return None

    short_bio = data.get("shortBio")
    univers = data.get("univers")
    couleurs = data.get("couleurs")

    if not isinstance(short_bio, str) or not short_bio.strip():
        return None
    if not isinstance(univers, str):
        univers = ""
    if not isinstance(couleurs, list):
        couleurs = []

    candidats_motscles, motscles_format_suspect = valider_et_nettoyer_motscles(
        data.get("motsCles")
    )
    couleurs = [str(c).strip() for c in couleurs if str(c).strip()][:5]

    return {
        "shortBio": short_bio.strip()[:SHORTBIO_MAX_CHARS],
        "univers": univers.strip()[:UNIVERS_MAX_CHARS],
        "candidats_motsCles": candidats_motscles,
        "motsCles_format_suspect": motscles_format_suspect,
        "couleurs": couleurs,
    }


def appeler_llm(nom: str, bio: str) -> Optional[dict]:
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
                    "Ta réponse précédente n'était pas exploitable "
                    f"(erreur: {derniere_erreur}). Réponds cette fois UNIQUEMENT "
                    "avec le JSON demandé, rien d'autre, pas de ```. Vérifie bien que "
                    "chaque élément de motsCles est un objet {\"mot\": ..., \"score\": ...} "
                    "avec un score numérique entre 0.0 et 1.0."
                ),
            })

        try:
            contenu = _appeler_provider(messages)
        except SystemExit:
            raise
        except Exception as exc:
            derniere_erreur = str(exc)
            log.warning(
                "Appel %s échoué pour '%s' (tentative %d/%d) : %s",
                LLM_PROVIDER, nom, tentative, MAX_TENTATIVES_LLM, exc,
            )
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

        if not valide["candidats_motsCles"] and tentative < MAX_TENTATIVES_LLM:
            derniere_erreur = "aucun mot-clé exploitable"
            log.warning("Aucun mot-clé exploitable pour '%s' (tentative %d/%d)", nom, tentative, MAX_TENTATIVES_LLM)
            continue

        return valide

    log.error("❌ Échec définitif de l'enrichissement LLM pour '%s' : %s", nom, derniere_erreur)
    return None


def valeurs_de_secours(bio: str) -> dict:
    resume = re.sub(r"\s+", " ", bio).strip()
    if len(resume) > SHORTBIO_MAX_CHARS:
        resume = resume[: SHORTBIO_MAX_CHARS - 1].rsplit(" ", 1)[0] + "…"
    return {
        "shortBio": resume,
        "univers": "",
        "candidats_motsCles": [],
        "motsCles_format_suspect": True,
        "couleurs": [],
    }


def hash_bio(bio: str) -> str:
    return hashlib.sha256(bio.encode("utf-8")).hexdigest()[:16]


def fiche_est_au_format_courant(fiche: Optional[dict]) -> bool:
    if not fiche:
        return False
    mots_cles = fiche.get("motsCles")
    if not isinstance(mots_cles, list):
        return False
    if not all(isinstance(m, str) for m in mots_cles):
        return False
    eval_mots_cles = fiche.get("eval_motCles")
    if not isinstance(eval_mots_cles, list):
        return False
    for item in eval_mots_cles:
        if not isinstance(item, dict) or "mot" not in item or "score" not in item:
            return False
    return True


def appliquer_seuil_pertinence(
    candidats_motscles: list[dict], score_min: float
) -> tuple[list[dict], bool]:
    retenus = [m for m in candidats_motscles if m["score"] >= score_min][:MOTCLE_MAX_COUNT]
    return retenus, len(retenus) < MOTCLE_MIN_COUNT


# ── Boucle principale ────────────────────────────────────────────

def enrichir_tous(
    input_path: Path,
    output_path: Path,
    force: bool,
    sleep_s: float,
    limit: Optional[int],
    score_min: float,
) -> None:
    if not input_path.exists():
        log.error("Fichier d'entrée introuvable : %s", input_path)
        raise SystemExit(1)

    brut: dict[str, dict[str, Any]] = json.loads(input_path.read_text(encoding="utf-8"))
    log.info("%d artistes lus depuis %s", len(brut), input_path)
    log.info("Provider LLM : %s", LLM_PROVIDER)

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
    n_migres_vers_v3 = 0

    for i, slug in enumerate(slugs, start=1):
        entree = brut[slug]
        d = entree.get("data", entree) if isinstance(entree, dict) else {}

        nom = d.get("name") or slug
        url = d.get("url")
        bio = d.get("bio")

        log.info("[%d/%d] %s", i, len(slugs), nom)

        if not bio or not bio.strip():
            n_sans_bio += 1
            log.warning("⚠️  Pas de bio pour '%s' — fiche créée vide.", nom)
            resultats[slug] = asdict(ArtisteEnrichi(
                id=slug, name=nom, url=url, bio=None,
                shortBio="", univers="", motsCles=[], eval_motCles=[], couleurs=[],
                aCurer=["bio", "shortBio", "univers", "motsCles", "couleurs"],
                bioHash="", enrichedAt=datetime.now().isoformat(timespec="seconds"),
            ))
            continue

        empreinte = hash_bio(bio)
        deja = resultats.get(slug)
        deja_a_jour = (
            not force
            and deja is not None
            and deja.get("bioHash") == empreinte
            and deja.get("shortBio")
            and fiche_est_au_format_courant(deja)
        )
        if deja_a_jour:
            n_ignores_cache += 1
            log.info("↩️  '%s' déjà enrichi — ignoré (utilise --force pour regénérer).", nom)
            continue

        if deja is not None and not force and deja.get("bioHash") == empreinte and not fiche_est_au_format_courant(deja):
            n_migres_vers_v3 += 1
            log.info("🔁 '%s' ancien format → migration v3.", nom)

        try:
            enrichissement = appeler_llm(nom, bio)
        except SystemExit:
            raise
        except Exception as exc:
            log.error("Erreur inattendue pour '%s' : %s", nom, exc)
            enrichissement = None

        a_curer: list[str] = []
        if enrichissement is None:
            enrichissement = valeurs_de_secours(bio)
            a_curer = ["shortBio", "univers", "motsCles", "couleurs"]
            n_llm_echecs += 1
        else:
            n_llm_ok += 1
            if not enrichissement["couleurs"]:
                a_curer.append("couleurs")
            if enrichissement.get("motsCles_format_suspect"):
                a_curer.append("motsCles")

        candidats_retenus, sous_le_minimum = appliquer_seuil_pertinence(
            enrichissement["candidats_motsCles"], score_min
        )
        if sous_le_minimum and "motsCles" not in a_curer:
            a_curer.append("motsCles")

        motscles_finales = [c["mot"] for c in candidats_retenus]
        eval_motcles = enrichissement["candidats_motsCles"]

        resultats[slug] = asdict(ArtisteEnrichi(
            id=slug, name=nom, url=url, bio=bio,
            shortBio=enrichissement["shortBio"],
            univers=enrichissement["univers"],
            motsCles=motscles_finales,
            eval_motCles=eval_motcles,
            couleurs=enrichissement["couleurs"],
            aCurer=a_curer,
            bioHash=empreinte,
            enrichedAt=datetime.now().isoformat(timespec="seconds"),
        ))

        n_traites += 1
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(resultats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        time.sleep(sleep_s)

    log.info("✅ Terminé.")
    log.info("   Provider : %s", LLM_PROVIDER)
    log.info("   %d artistes traités par le LLM cette exécution", n_traites)
    log.info("   dont %d migrés depuis un ancien format vers v3", n_migres_vers_v3)
    log.info("   %d enrichissements LLM réussis", n_llm_ok)
    log.info("   %d enrichissements LLM en échec (valeurs de secours)", n_llm_echecs)
    log.info("   %d artistes ignorés (cache à jour)", n_ignores_cache)
    log.info("   %d artistes sans bio source", n_sans_bio)
    log.info("   seuil pertinence mots-clés : %.2f", score_min)
    log.info("📄 Résultat écrit dans %s", output_path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrichit artistes.json via LLM (Groq / Anthropic / OpenAI)."
    )
    parser.add_argument("--input", type=Path, default=INPUT_FILE)
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--sleep", type=float, default=DELAI_ENTRE_APPELS_DEFAUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--score-min", type=float, default=MOTCLE_SCORE_MIN_DEFAUT,
        help=f"Score minimum pour retenir un mot-clé (défaut : {MOTCLE_SCORE_MIN_DEFAUT})",
    )
    args = parser.parse_args()

    if not (0.0 <= args.score_min <= 1.0):
        raise SystemExit("--score-min doit être compris entre 0.0 et 1.0")

    if LLM_PROVIDER not in ("groq", "anthropic", "openai"):
        raise SystemExit(
            f"LLM_PROVIDER='{LLM_PROVIDER}' invalide. "
            "Valeurs acceptées dans .env : groq | anthropic | openai"
        )

    enrichir_tous(
        input_path=args.input,
        output_path=args.output,
        force=args.force,
        sleep_s=args.sleep,
        limit=args.limit,
        score_min=args.score_min,
    )


if __name__ == "__main__":
    main()