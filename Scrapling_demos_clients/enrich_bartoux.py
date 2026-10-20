#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
enrich_bartoux.py  --  ETAPE 2 (version finale) du pipeline Galeries Bartoux
------------------------------------------------------------------
Entrees (produites par scrapling_bartoux.py, dans le meme dossier) :
    catalogue.json, artistes.json, artistes/<slug>/<image>.jpg

Pour chaque oeuvre :
    1. conversion JPEG/JPG -> WebP        (SAUTEE si le .webp existe deja)
    2. mesure sur l'image (SAUTEE si deja remplie) :
         color      couleurs dominantes (hex, rgb, nom, pourcentage)
         tonality   temperature / brightness / intensity
         format     orientation / size
    3. ecriture dans ai_metadata

Les champs themes, style, material, recognizable_elements sont laisses
vides : ils sont remplis par l'etape 3 (modele de vision).

IDEMPOTENT : relancer le script ne reconvertit rien. Il ne fait que
completer ce qui manque (ex : couleurs, tonality, format).
Les sources ne sont JAMAIS modifiees ; tout est ecrit dans output_final/.

Sorties :
    output_final/
        catalogue.json            catalogue final
        artistes.json             copie
        images/<slug>/<x>.webp    images optimisees
        rapport_traitement.json   statistiques + echecs
        enrich_bartoux.log

Aucune cle API necessaire. Outils open source : Pillow (libwebp integre).

Installation :   pip install "Pillow>=10.0" python-dotenv

Usage :
    python enrich_bartoux.py                 # normal (reprise automatique)
    python enrich_bartoux.py --limit 10      # test sur 10 oeuvres
    python enrich_bartoux.py --force         # recalcule tout (webp + mesures)
    python enrich_bartoux.py --workers 4

.env optionnel :  INPUT_DIR  OUTPUT_DIR  WEBP_MODE (lossy|lossless)  WEBP_QUALITY
------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import re
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
except ImportError:
    pass


# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = Path(os.getenv("INPUT_DIR", BASE_DIR)).resolve()
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output_final")).resolve()

CATALOGUE_FILE = "catalogue.json"
ARTISTS_FILE = "artistes.json"
IMAGES_ROOT = "artistes"
OUTPUT_IMAGES = "images"
REPORT_FILE = "rapport_traitement.json"
LOG_FILE = "enrich_bartoux.log"

# --- WebP ---
# lossy    : qualite 92, visuellement identique, nettement plus leger
# lossless : pixel-perfect mais souvent PLUS LOURD que le JPEG d'origine
WEBP_MODE = os.getenv("WEBP_MODE", "lossy").lower()
WEBP_QUALITY = int(os.getenv("WEBP_QUALITY", "92"))
WEBP_METHOD = 6

# --- Couleurs ---
PALETTE_SIZE = 5
RAW_QUANT_COLORS = 12
MERGE_DISTANCE = 38
ANALYSIS_SIZE = 256
MIN_PERCENT = 1.0

# --- Tonalite (seuils ajustables) ---
TONE_SIZE = 128                 # image reduite pour l'analyse
TEMP_MIN_SAT = 40               # (0-255) en dessous : pixel gris, sans temperature
TEMP_MIN_VAL = 40               # (0-255) en dessous : pixel trop sombre
TEMP_MIN_COVERAGE = 0.03        # sous ce poids de pixels colores -> "neutre"
TEMP_DOMINANCE = 0.60           # part requise pour "chaude" ou "froide"
BRIGHT_DARK_MAX = 85            # luminance moyenne < 85  -> "sombre"
BRIGHT_LIGHT_MIN = 170          # luminance moyenne > 170 -> "claire"
INTENSITY_VIVID_MIN = 110       # saturation moyenne > 110 -> "vive"
INTENSITY_MUTED_MIN = 55        # saturation moyenne > 55  -> "moderee", sinon "terne"

# --- Format ---
SQUARE_TOLERANCE = 0.08         # ratio l/h entre 0.92 et 1.08 -> "carre"
SIZE_SMALL_MAX_CM = 50          # plus grand cote < 50 cm   -> "petit"
SIZE_LARGE_MIN_CM = 100         # plus grand cote > 100 cm  -> "grand"

# --- Robustesse ---
IO_RETRIES = 3
IO_RETRY_DELAY = 1.5
Image.MAX_IMAGE_PIXELS = 300_000_000

# Structure finale de ai_metadata (valeurs en francais, champs en anglais)
EMPTY_AI_METADATA = {
    "color": [],
    "tonality": {"temperature": "", "brightness": "", "intensity": ""},
    "format": {"orientation": "", "size": ""},
    "themes": [],
    "style": [],
    "material": [],
    "recognizable_elements": [],
}
AI_LIST_FIELDS = ("themes", "style", "material", "recognizable_elements")

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
# LOGGING / HELPERS
# ----------------------------------------------------------------------

def setup_logging() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(OUTPUT_DIR / LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


log = logging.getLogger("enrich_bartoux")


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
# STRUCTURE ai_metadata (migration + completude)
# ----------------------------------------------------------------------

def normalize_ai(prev: dict | None) -> dict:
    """
    Reconstruit ai_metadata avec EXACTEMENT la structure finale.
    - conserve ce qui est deja rempli (y compris les champs de l'etape 3)
    - migre l'ancien champ "colors" vers "color"
    - supprime les anciens champs (subjects, mood, visual_description)
    """
    prev = prev if isinstance(prev, dict) else {}
    ai = copy.deepcopy(EMPTY_AI_METADATA)

    colors = prev.get("color") or prev.get("colors")
    if isinstance(colors, list):
        ai["color"] = colors
    for key in ("tonality", "format"):
        src = prev.get(key)
        if isinstance(src, dict):
            for sub in ai[key]:
                if isinstance(src.get(sub), str):
                    ai[key][sub] = src[sub]
    for key in AI_LIST_FIELDS:
        if isinstance(prev.get(key), list):
            ai[key] = prev[key]
    return ai


def analysis_missing(ai: dict, dimensions: str | None) -> bool:
    """True si une mesure (color / tonality / format) manque encore."""
    if not ai["color"]:
        return True
    if any(not v for v in ai["tonality"].values()):
        return True
    if not ai["format"]["orientation"]:
        return True
    if not ai["format"]["size"] and parse_dimensions(dimensions):
        return True
    return False


def merge_analysis(ai: dict, analysis: dict, force: bool) -> None:
    """Remplit uniquement ce qui est vide (ou tout avec --force)."""
    if force or not ai["color"]:
        ai["color"] = analysis["color"]
    for key in ("tonality", "format"):
        for sub, value in analysis[key].items():
            if value and (force or not ai[key][sub]):
                ai[key][sub] = value


# ----------------------------------------------------------------------
# MESURES : COULEURS
# ----------------------------------------------------------------------

def _distance(a: tuple, b: tuple) -> float:
    rm = (a[0] + b[0]) / 2
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return (((512 + rm) * dr * dr) / 256 + 4 * dg * dg + ((767 - rm) * db * db) / 256) ** 0.5


def nearest_color_name(rgb: tuple) -> str:
    return min(COLOR_NAMES, key=lambda n: _distance(rgb, COLOR_NAMES[n]))


def to_rgb(img: Image.Image) -> Image.Image:
    """RGB sur fond blanc (gere transparence et modes exotiques)."""
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return img.convert("RGB")


def extract_colors(rgb_img: Image.Image) -> list[dict]:
    work = rgb_img.copy()
    work.thumbnail((ANALYSIS_SIZE, ANALYSIS_SIZE), Image.Resampling.LANCZOS)
    quant = work.quantize(colors=RAW_QUANT_COLORS, method=Image.Quantize.MEDIANCUT,
                          dither=Image.Dither.NONE)
    palette = quant.getpalette() or []
    counts = quant.getcolors() or []
    total = sum(c for c, _ in counts) or 1

    raw = []
    for count, idx in sorted(counts, reverse=True):
        rgb = tuple(palette[idx * 3: idx * 3 + 3])
        if len(rgb) == 3:
            raw.append([rgb, count])

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
        result.append({"hex": "#{:02x}{:02x}{:02x}".format(*rgb), "rgb": list(rgb),
                       "name": nearest_color_name(rgb), "percent": pct})
    return result


# ----------------------------------------------------------------------
# MESURES : TONALITE + FORMAT
# ----------------------------------------------------------------------

def analyze_tonality(rgb_img: Image.Image) -> dict:
    """temperature (chaude/froide/neutre), brightness (sombre/moyenne/claire),
    intensity (vive/moderee/terne), mesurees sur les pixels."""
    small = rgb_img.copy()
    small.thumbnail((TONE_SIZE, TONE_SIZE), Image.Resampling.BILINEAR)
    hsv_pixels = list(small.convert("HSV").getdata())
    n = len(hsv_pixels) or 1
    lum = sum(small.convert("L").getdata()) / n
    sat = sum(p[1] for p in hsv_pixels) / n

    warm = cool = 0.0
    for h, s, v in hsv_pixels:
        if s < TEMP_MIN_SAT or v < TEMP_MIN_VAL:
            continue
        weight = (s / 255) * (v / 255)
        deg = h * 360 / 255
        if deg < 70 or deg >= 330:          # rouges, oranges, jaunes
            warm += weight
        elif 150 <= deg <= 270:             # cyans, bleus
            cool += weight

    total = warm + cool
    if total / n < TEMP_MIN_COVERAGE:
        temperature = "neutre"
    elif warm / total >= TEMP_DOMINANCE:
        temperature = "chaude"
    elif cool / total >= TEMP_DOMINANCE:
        temperature = "froide"
    else:
        temperature = "neutre"

    if lum < BRIGHT_DARK_MAX:
        brightness = "sombre"
    elif lum > BRIGHT_LIGHT_MIN:
        brightness = "claire"
    else:
        brightness = "moyenne"

    if sat > INTENSITY_VIVID_MIN:
        intensity = "vive"
    elif sat > INTENSITY_MUTED_MIN:
        intensity = "moderee"
    else:
        intensity = "terne"

    return {"temperature": temperature, "brightness": brightness, "intensity": intensity}


def parse_dimensions(text: str | None) -> list[float]:
    """'150 x 150 cm' -> [150.0, 150.0] (cm). Gere aussi 3 dimensions et mm."""
    if not text:
        return []
    nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", text)][:3]
    if re.search(r"\bmm\b", text, re.I):
        nums = [x / 10 for x in nums]
    return nums


def compute_orientation(width: int, height: int) -> str:
    if height <= 0:
        return ""
    ratio = width / height
    if abs(ratio - 1) <= SQUARE_TOLERANCE:
        return "carre"
    return "paysage" if ratio > 1 else "portrait"


def compute_size(dimensions: str | None) -> str:
    nums = parse_dimensions(dimensions)
    if not nums:
        return ""
    longest = max(nums)
    if longest < SIZE_SMALL_MAX_CM:
        return "petit"
    if longest > SIZE_LARGE_MIN_CM:
        return "grand"
    return "moyen"


def analyze_image(img: Image.Image, dimensions: str | None) -> dict:
    rgb = to_rgb(img)
    return {
        "color": extract_colors(rgb),
        "tonality": analyze_tonality(rgb),
        "format": {"orientation": compute_orientation(*img.size),
                   "size": compute_size(dimensions)},
    }


# ----------------------------------------------------------------------
# TRAITEMENT D'UNE IMAGE (process worker)
# ----------------------------------------------------------------------

def _open_image(path: Path) -> Image.Image:
    last_err: Exception | None = None
    for attempt in range(1, IO_RETRIES + 1):
        try:
            with Image.open(path) as im:
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
    """Convertit (si besoin) et analyse (si besoin). Ne leve jamais."""
    src, dst = Path(task["src"]), Path(task["dst"])
    res = {"id": task["id"], "status": "fail", "analysis": None, "converted": False,
           "bytes_in": 0, "bytes_out": 0, "error": None}
    try:
        src_ok = src.exists() and src.stat().st_size > 0
        dst_ok = dst.exists() and dst.stat().st_size > 0

        if task["need_convert"] and not src_ok:
            res.update(status="missing", error=f"Source introuvable : {src}")
            return res
        # analyse sur l'original si present (plus fidele), sinon sur le webp
        read_path = src if src_ok else (dst if dst_ok else None)
        if read_path is None:
            res.update(status="missing", error=f"Aucune image : {src}")
            return res

        img = _open_image(read_path)

        if task["need_analysis"]:
            res["analysis"] = analyze_image(img, task["dimensions"])

        if task["need_convert"]:
            if src.suffix.lower() == ".webp":
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            else:
                _save_webp(img, dst, task["mode"], task["quality"])
            res["converted"] = True
            res["bytes_in"] = src.stat().st_size
            res["bytes_out"] = dst.stat().st_size

        res["status"] = "ok"
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        try:
            dst.with_suffix(".webp.part").unlink(missing_ok=True)
        except OSError:
            pass
    return res


# ----------------------------------------------------------------------
# PIPELINE
# ----------------------------------------------------------------------

def build_destination(local_image: str) -> Path:
    """artistes/<slug>/<x>.jpg  ->  output_final/images/<slug>/<x>.webp"""
    rel = Path(local_image)
    if rel.parts and rel.parts[0] == IMAGES_ROOT:
        rel = Path(*rel.parts[1:])
    return OUTPUT_DIR / OUTPUT_IMAGES / rel.with_suffix(".webp")


def webp_exists(dst: Path) -> bool:
    return dst.exists() and dst.stat().st_size > 0


def run(limit: int | None, force: bool, workers: int) -> None:
    catalogue_in = load_json(INPUT_DIR / CATALOGUE_FILE)
    if not catalogue_in:
        log.error(f"{INPUT_DIR / CATALOGUE_FILE} introuvable ou vide. Lance d'abord scrapling_bartoux.py.")
        return

    previous_out = load_json(OUTPUT_DIR / CATALOGUE_FILE)
    catalogue_out: dict = {}
    tasks: dict[str, dict] = {}
    stats = {"total": len(catalogue_in), "converted": 0, "analyzed": 0, "reused": 0,
             "missing": 0, "failed": 0, "bytes_in": 0, "bytes_out": 0}
    failures: list[dict] = []

    for idx, (pid, entry) in enumerate(catalogue_in.items()):
        entry = dict(entry)
        prev = previous_out.get(pid) or {}
        ai = normalize_ai(prev.get("ai_metadata"))

        # --limit : le reste du catalogue est conserve tel quel (jamais tronque)
        if limit and idx >= limit:
            if prev:
                keep = dict(prev)
                keep["ai_metadata"] = normalize_ai(prev.get("ai_metadata"))
                catalogue_out[pid] = keep
            else:
                entry["ai_metadata"] = ai
                catalogue_out[pid] = entry
            continue

        local = entry.get("local_image")
        if not local:
            entry["ai_metadata"] = ai
            catalogue_out[pid] = entry
            stats["missing"] += 1
            continue

        dst = build_destination(local)
        need_convert = force or not webp_exists(dst)
        need_analysis = force or analysis_missing(ai, entry.get("dimensions"))

        if not need_convert and not need_analysis:
            entry["local_image"] = to_posix(dst.relative_to(OUTPUT_DIR))
            entry["ai_metadata"] = ai
            catalogue_out[pid] = entry
            stats["reused"] += 1
            continue

        entry["ai_metadata"] = ai
        catalogue_out[pid] = entry
        tasks[pid] = {
            "id": pid, "src": str(INPUT_DIR / local), "dst": str(dst),
            "need_convert": need_convert, "need_analysis": need_analysis,
            "dimensions": entry.get("dimensions"),
            "mode": WEBP_MODE, "quality": WEBP_QUALITY,
        }

    n_conv = sum(1 for t in tasks.values() if t["need_convert"])
    n_ana = sum(1 for t in tasks.values() if t["need_analysis"])
    log.info(f"Oeuvres: {len(catalogue_in)} | a traiter: {len(tasks)} "
             f"(conversion: {n_conv}, mesures: {n_ana}) | deja completes: {stats['reused']} "
             f"| sans image: {stats['missing']} | WebP: {WEBP_MODE} q={WEBP_QUALITY}")

    def finalize(res: dict) -> None:
        pid = res["id"]
        entry = catalogue_out[pid]
        dst = Path(tasks[pid]["dst"])
        if webp_exists(dst):
            entry["local_image"] = to_posix(dst.relative_to(OUTPUT_DIR))
        if res["status"] == "ok":
            if res["analysis"]:
                merge_analysis(entry["ai_metadata"], res["analysis"], force)
                stats["analyzed"] += 1
            if res["converted"]:
                stats["converted"] += 1
                stats["bytes_in"] += res["bytes_in"]
                stats["bytes_out"] += res["bytes_out"]
        else:
            key = "missing" if res["status"] == "missing" else "failed"
            stats[key] += 1
            failures.append({"id": pid, "title": entry.get("title"),
                             "status": res["status"], "error": res["error"]})
            log.warning(f"  {res['status'].upper()} {entry.get('title')} : {res['error']}")

    try:
        if tasks:
            done = 0
            with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
                futures = [pool.submit(process_one, t) for t in tasks.values()]
                for fut in as_completed(futures):
                    finalize(fut.result())
                    done += 1
                    if done % 25 == 0 or done == len(tasks):
                        log.info(f"  Progression : {done}/{len(tasks)}")
    except KeyboardInterrupt:
        log.warning("Interruption : sauvegarde de l'etat actuel (reprise possible).")
    finally:
        save_json(OUTPUT_DIR / CATALOGUE_FILE, catalogue_out)
        artists_src = INPUT_DIR / ARTISTS_FILE
        if artists_src.exists():
            shutil.copy2(artists_src, OUTPUT_DIR / ARTISTS_FILE)

        saved = stats["bytes_in"] - stats["bytes_out"]
        ratio = (100 * saved / stats["bytes_in"]) if stats["bytes_in"] else 0
        save_json(OUTPUT_DIR / REPORT_FILE, {
            "date": datetime.now(timezone.utc).isoformat(),
            "webp_mode": WEBP_MODE, "webp_quality": WEBP_QUALITY,
            "stats": stats, "gain_percent": round(ratio, 1), "failures": failures,
        })

        log.info("===== RESUME =====")
        log.info(f"Converties : {stats['converted']} | mesurees : {stats['analyzed']} "
                 f"| deja completes : {stats['reused']} | sans image : {stats['missing']} "
                 f"| echecs : {stats['failed']}")
        if stats["bytes_in"]:
            log.info(f"Poids converti : {human_size(stats['bytes_in'])} -> "
                     f"{human_size(stats['bytes_out'])} ({ratio:+.1f} % de gain)")
        log.info(f"Sortie : {OUTPUT_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Etape 2 : WebP + color/tonality/format")
    parser.add_argument("--limit", type=int, default=None, help="Traiter seulement N oeuvres (test)")
    parser.add_argument("--force", action="store_true", help="Tout recalculer (webp + mesures)")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = parser.parse_args()

    setup_logging()
    log.info("=" * 60)
    log.info("Demarrage enrichissement Bartoux - etape 2 (WebP + mesures)")
    log.info("=" * 60)
    run(args.limit, args.force, args.workers)


if __name__ == "__main__":
    main()