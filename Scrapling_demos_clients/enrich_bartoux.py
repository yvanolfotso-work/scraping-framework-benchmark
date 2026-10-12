#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_bartoux.py  --  ETAPE 2 du pipeline Galeries Bartoux
------------------------------------------------------------------
Entrees (produites par scrapling_bartoux.py, dans le meme dossier) :
    catalogue.json, artistes.json, artistes/<slug>/<image>.jpg

Traitement, pour chaque oeuvre :
    1. ouverture de l'image ORIGINALE (une seule fois, en memoire)
    2. extraction des couleurs dominantes sur l'original (aucune perte)
    3. conversion en WebP (lossy haute qualite ou lossless, au choix)
    4. ajout de la structure "ai_metadata" dans le catalogue

Sorties (dossier separe, les fichiers sources ne sont JAMAIS modifies) :
    output_final/
        catalogue.json          <- catalogue final (local_image -> .webp + ai_metadata)
        artistes.json           <- copie telle quelle
        images/<slug>/<x>.webp  <- images optimisees
        rapport_traitement.json <- statistiques + echecs
        enrich_bartoux.log

Outils (100 % gratuits / open source) : Pillow (+ libwebp integre), python-dotenv (optionnel)

Installation :
    pip install "Pillow>=10.0" python-dotenv

Usage :
    python enrich_bartoux.py                 # traitement normal (reprise automatique)
    python enrich_bartoux.py --limit 10      # test rapide sur 10 oeuvres
    python enrich_bartoux.py --force         # retraite tout
    python enrich_bartoux.py --workers 4

Config optionnelle via .env (aucune cle API n'est necessaire pour cette etape) :
    INPUT_DIR=.   OUTPUT_DIR=output_final   WEBP_MODE=lossy   WEBP_QUALITY=92
------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # python-dotenv optionnel
    pass


# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

INPUT_DIR = Path(os.getenv("INPUT_DIR", BASE_DIR)).resolve()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output_final")).resolve()

CATALOGUE_FILE = "catalogue.json"
ARTISTS_FILE = "artistes.json"
IMAGES_ROOT = "artistes"            # dossier des images sources (cote scraper)
OUTPUT_IMAGES = "images"            # sous-dossier des images WebP en sortie
REPORT_FILE = "rapport_traitement.json"
LOG_FILE = "enrich_bartoux.log"

# --- WebP ---
# "lossy"    : qualite 92 = visuellement identique, ~25-40 % plus leger qu'un JPEG
# "lossless" : pixel-perfect, MAIS souvent PLUS LOURD que le JPEG d'origine
#              (un JPEG est deja compresse avec perte).
WEBP_MODE = os.getenv("WEBP_MODE", "lossy").lower()
WEBP_QUALITY = int(os.getenv("WEBP_QUALITY", "92"))
WEBP_METHOD = 6                     # 0-6 : 6 = meilleure compression (plus lent)

# --- Couleurs ---
PALETTE_SIZE = 5                    # nombre de couleurs retournees
RAW_QUANT_COLORS = 12               # quantification brute avant fusion
MERGE_DISTANCE = 38                 # fusion des couleurs trop proches (distance RGB)
ANALYSIS_SIZE = 256                 # image reduite pour l'analyse (rapide, stable)
MIN_PERCENT = 1.0                   # ignore les couleurs < 1 % de l'image

# --- Robustesse ---
IO_RETRIES = 3                      # relectures si le fichier est verrouille / incomplet
IO_RETRY_DELAY = 1.5                # secondes, double a chaque essai
Image.MAX_IMAGE_PIXELS = 300_000_000

EMPTY_AI_METADATA = {
    "colors": [],
    "subjects": [],
    "style": [],
    "mood": [],
    "visual_description": "",
}

# Palette de noms de couleurs (francais) pour rendre les couleurs exploitables
COLOR_NAMES = {
    "noir": (0, 0, 0), "gris fonce": (70, 70, 70), "gris": (128, 128, 128),
    "gris clair": (200, 200, 200), "blanc": (250, 250, 250),
    "rouge": (200, 30, 30), "bordeaux": (110, 20, 40), "orange": (240, 130, 30),
    "jaune": (240, 210, 40), "dore": (200, 160, 50), "vert": (50, 150, 60),
    "vert fonce": (20, 80, 40), "turquoise": (40, 170, 170),
    "bleu clair": (130, 190, 235), "bleu": (40, 80, 200),
    "bleu marine": (20, 30, 90), "violet": (120, 50, 160),
    "rose": (240, 130, 170), "marron": (110, 70, 40), "beige": (225, 205, 170),
    "creme": (245, 235, 210), "terracotta": (190, 100, 70),
}


# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(OUTPUT_DIR / LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("enrich_bartoux")


log = logging.getLogger("enrich_bartoux")


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

def load_json(path: Path) -> dict:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Lecture impossible de {path} : {e}")
    return {}


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def to_posix(path: Path) -> str:
    return str(path).replace("\\", "/")


def human_size(n: float) -> str:
    for unit in ("o", "Ko", "Mo", "Go"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} To"


# ----------------------------------------------------------------------
# EXTRACTION DES COULEURS
# ----------------------------------------------------------------------

def _distance(a: tuple, b: tuple) -> float:
    """Distance RGB ponderee (approximation perceptuelle 'redmean')."""
    rm = (a[0] + b[0]) / 2
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return (((512 + rm) * dr * dr) / 256 + 4 * dg * dg + ((767 - rm) * db * db) / 256) ** 0.5


def nearest_color_name(rgb: tuple) -> str:
    return min(COLOR_NAMES, key=lambda n: _distance(rgb, COLOR_NAMES[n]))


def extract_colors(img: Image.Image) -> list[dict]:
    """
    Couleurs dominantes d'une image : quantification Pillow (median cut) sur une
    version reduite, puis fusion des teintes trop proches.
    Retourne [{"hex", "rgb", "name", "percent"}, ...] triees par dominance.
    """
    work = img
    if work.mode in ("RGBA", "LA", "P"):
        work = work.convert("RGBA")
        bg = Image.new("RGBA", work.size, (255, 255, 255, 255))
        work = Image.alpha_composite(bg, work)
    work = work.convert("RGB")
    work.thumbnail((ANALYSIS_SIZE, ANALYSIS_SIZE), Image.Resampling.LANCZOS)

    quant = work.quantize(
        colors=RAW_QUANT_COLORS,
        method=Image.Quantize.MEDIANCUT,
        dither=Image.Dither.NONE,
    )
    palette = quant.getpalette() or []
    counts = quant.getcolors() or []
    total = sum(c for c, _ in counts) or 1

    raw = []
    for count, idx in sorted(counts, reverse=True):
        rgb = tuple(palette[idx * 3: idx * 3 + 3])
        if len(rgb) == 3:
            raw.append([rgb, count])

    # Fusion des couleurs proches (la plus dominante absorbe les voisines)
    merged: list[list] = []
    for rgb, count in raw:
        for m in merged:
            if _distance(m[0], rgb) < MERGE_DISTANCE:
                m[1] += count
                break
        else:
            merged.append([rgb, count])

    merged.sort(key=lambda m: m[1], reverse=True)
    result = []
    for rgb, count in merged[:PALETTE_SIZE]:
        pct = round(100 * count / total, 1)
        if pct < MIN_PERCENT:
            continue
        result.append({
            "hex": "#{:02x}{:02x}{:02x}".format(*rgb),
            "rgb": list(rgb),
            "name": nearest_color_name(rgb),
            "percent": pct,
        })
    return result


# ----------------------------------------------------------------------
# TRAITEMENT D'UNE IMAGE (execute dans un process worker)
# ----------------------------------------------------------------------

def _open_image(src: Path) -> Image.Image:
    """Ouvre + charge l'image avec retry (fichier verrouille / ecriture en cours)."""
    last_err: Exception | None = None
    for attempt in range(1, IO_RETRIES + 1):
        try:
            with Image.open(src) as im:
                im.load()
                return ImageOps.exif_transpose(im)
        except (OSError, UnidentifiedImageError, ValueError) as e:
            last_err = e
            if attempt < IO_RETRIES:
                time.sleep(IO_RETRY_DELAY * (2 ** (attempt - 1)))
    raise RuntimeError(f"Lecture impossible apres {IO_RETRIES} essais : {last_err}")


def _save_webp(img: Image.Image, dst: Path, mode: str, quality: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".webp.part")
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

    params = {"method": WEBP_METHOD}
    icc = img.info.get("icc_profile")
    if icc:
        params["icc_profile"] = icc
    if mode == "lossless":
        params.update(lossless=True, quality=100, exact=False)
    else:
        params.update(lossless=False, quality=quality)

    img.save(tmp, format="WEBP", **params)
    os.replace(tmp, dst)


def process_one(task: dict) -> dict:
    """Convertit + analyse une image. Ne leve jamais : renvoie un statut."""
    pid = task["id"]
    src, dst = Path(task["src"]), Path(task["dst"])
    result = {"id": pid, "status": "fail", "colors": [], "bytes_in": 0, "bytes_out": 0, "error": None}
    try:
        if not src.exists() or src.stat().st_size == 0:
            result.update(status="missing", error=f"Fichier source introuvable : {src}")
            return result

        result["bytes_in"] = src.stat().st_size
        img = _open_image(src)

        # 1) couleurs sur l'original (avant toute conversion)
        result["colors"] = extract_colors(img)

        # 2) conversion WebP (un .webp existant est simplement copie : pas de re-compression)
        if src.suffix.lower() == ".webp":
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        else:
            _save_webp(img, dst, task["mode"], task["quality"])

        result["bytes_out"] = dst.stat().st_size
        result["status"] = "ok"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        for leftover in (dst.with_suffix(".webp.part"),):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
    return result


# ----------------------------------------------------------------------
# PIPELINE
# ----------------------------------------------------------------------

def build_destination(local_image: str) -> Path:
    """artistes/<slug>/<x>.jpg  ->  output_final/images/<slug>/<x>.webp"""
    rel = Path(local_image)
    if rel.parts and rel.parts[0] == IMAGES_ROOT:
        rel = Path(*rel.parts[1:])
    return OUTPUT_DIR / OUTPUT_IMAGES / rel.with_suffix(".webp")


def merge_ai_metadata(previous: dict | None, colors: list[dict]) -> dict:
    """Conserve les champs IA deja remplis (futures etapes), met a jour les couleurs."""
    ai = dict(EMPTY_AI_METADATA)
    if previous:
        ai.update({k: v for k, v in previous.items() if k in EMPTY_AI_METADATA})
    ai["colors"] = colors
    return ai


def run(limit: int | None, force: bool, workers: int) -> None:
    catalogue_in = load_json(INPUT_DIR / CATALOGUE_FILE)
    if not catalogue_in:
        log.error(f"{INPUT_DIR / CATALOGUE_FILE} introuvable ou vide. Lance d'abord scrapling_bartoux.py.")
        return

    previous_out = load_json(OUTPUT_DIR / CATALOGUE_FILE)
    catalogue_out: dict = {}
    tasks: list[dict] = []
    stats = {"total": len(catalogue_in), "converted": 0, "reused": 0, "missing": 0,
             "failed": 0, "bytes_in": 0, "bytes_out": 0}
    failures: list[dict] = []

    items = list(catalogue_in.items())
    if limit:
        items = items[:limit]

    for pid, entry in items:
        entry = dict(entry)
        prev = previous_out.get(pid, {})
        local = entry.get("local_image")

        if not local:
            entry["ai_metadata"] = merge_ai_metadata(prev.get("ai_metadata"), [])
            catalogue_out[pid] = entry
            stats["missing"] += 1
            continue

        dst = build_destination(local)
        prev_colors = (prev.get("ai_metadata") or {}).get("colors")

        # Reprise : webp deja produit + couleurs deja calculees -> on saute
        if not force and dst.exists() and dst.stat().st_size > 0 and prev_colors:
            entry["local_image"] = to_posix(dst.relative_to(OUTPUT_DIR))
            entry["ai_metadata"] = merge_ai_metadata(prev.get("ai_metadata"), prev_colors)
            catalogue_out[pid] = entry
            stats["reused"] += 1
            continue

        catalogue_out[pid] = entry          # complete apres traitement
        tasks.append({
            "id": pid, "src": str(INPUT_DIR / local), "dst": str(dst),
            "mode": WEBP_MODE, "quality": WEBP_QUALITY,
        })

    log.info(f"Oeuvres: {len(items)} | a traiter: {len(tasks)} | deja faites: {stats['reused']} "
             f"| sans image: {stats['missing']} | mode WebP: {WEBP_MODE} (q={WEBP_QUALITY})")

    def finalize(res: dict) -> None:
        pid = res["id"]
        entry = catalogue_out[pid]
        prev_ai = (previous_out.get(pid) or {}).get("ai_metadata")
        if res["status"] == "ok":
            dst = build_destination(entry["local_image"])
            entry["local_image"] = to_posix(dst.relative_to(OUTPUT_DIR))
            entry["ai_metadata"] = merge_ai_metadata(prev_ai, res["colors"])
            stats["converted"] += 1
            stats["bytes_in"] += res["bytes_in"]
            stats["bytes_out"] += res["bytes_out"]
        else:
            # On garde l'ancien chemin pour pouvoir retenter, structure ai_metadata vide
            entry["ai_metadata"] = merge_ai_metadata(prev_ai, [])
            key = "missing" if res["status"] == "missing" else "failed"
            stats[key] += 1
            failures.append({"id": pid, "title": entry.get("title"),
                             "status": res["status"], "error": res["error"]})
            log.warning(f"  {res['status'].upper()} {entry.get('title')} : {res['error']}")

    try:
        if tasks:
            workers = max(1, workers)
            done = 0
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(process_one, t) for t in tasks]
                for fut in as_completed(futures):
                    finalize(fut.result())
                    done += 1
                    if done % 25 == 0 or done == len(tasks):
                        log.info(f"  Progression : {done}/{len(tasks)}")
    except KeyboardInterrupt:
        log.warning("Interruption : sauvegarde de l'etat actuel (reprise possible).")
    finally:
        # Sorties : toujours ecrites, meme en cas d'interruption
        save_json(OUTPUT_DIR / CATALOGUE_FILE, catalogue_out)
        artists_src = INPUT_DIR / ARTISTS_FILE
        if artists_src.exists():
            shutil.copy2(artists_src, OUTPUT_DIR / ARTISTS_FILE)

        saved = stats["bytes_in"] - stats["bytes_out"]
        ratio = (100 * saved / stats["bytes_in"]) if stats["bytes_in"] else 0
        report = {
            "date": datetime.now(timezone.utc).isoformat(),
            "webp_mode": WEBP_MODE, "webp_quality": WEBP_QUALITY,
            "stats": stats, "gain_percent": round(ratio, 1), "failures": failures,
        }
        save_json(OUTPUT_DIR / REPORT_FILE, report)

        log.info("===== RESUME =====")
        log.info(f"Converties : {stats['converted']} | reutilisees : {stats['reused']} "
                 f"| sans image : {stats['missing']} | echecs : {stats['failed']}")
        if stats["bytes_in"]:
            log.info(f"Poids : {human_size(stats['bytes_in'])} -> {human_size(stats['bytes_out'])} "
                     f"({ratio:+.1f} % de gain)")
        log.info(f"Sortie : {OUTPUT_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Etape 2 : WebP + couleurs + ai_metadata")
    parser.add_argument("--limit", type=int, default=None, help="Traiter seulement N oeuvres (test)")
    parser.add_argument("--force", action="store_true", help="Retraiter meme ce qui existe deja")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = parser.parse_args()

    setup_logging()
    log.info("=" * 60)
    log.info("Demarrage enrichissement Bartoux (WebP + couleurs + ai_metadata)")
    log.info("=" * 60)
    run(args.limit, args.force, args.workers)


if __name__ == "__main__":
    main()