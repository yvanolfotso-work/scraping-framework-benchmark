#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
integrate_manquantes.py
------------------------------------------------------------------
Etape 2 de l'audit Galeries Bartoux : construit un catalogue SEPARE
des oeuvres manquantes, a partir de audit_manquantes/oeuvres_manquantes.json.

IMPORTANT : ce script NE MODIFIE JAMAIS catalogue.json ni artistes.json
ni le dossier d'images existant. Il ne les LIT que pour ignorer ce qui
est deja scrape.

Tout est ecrit dans un dossier a part :

    output_manquantes/
        catalogue_manquantes.json   meme format que catalogue.json
                                    (memes 9 champs, meme id sha256 de l'URL)
        artistes/<slug-artiste>/    images des oeuvres manquantes (dossier separe)
        rapport_rejetees.json       oeuvres non integrees + raison
        rapport_sans_image.json     oeuvres ajoutees mais image non telechargee
        integrate_manquantes.log

Rien n'est melange avec la production : le champ local_image vaut
"output_manquantes/artistes/<slug>/<fichier>", ce qui permet de voir d'un coup
d'oeil les oeuvres qui n'avaient pas ete scrapees. Tu decides ensuite toi-meme,
a la main, ce que tu integres a ton catalogue.

Reprise : on peut relancer a tout moment. Les oeuvres deja faites sont
ignorees ; celles dont l'image a echoue sont retentees.

A placer dans le MEME dossier que scrapling_bartoux.py.

Usage :
    python integrate_manquantes.py --dry-run    # simulation, rien n'est ecrit
    python integrate_manquantes.py
------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from urllib.parse import urldefrag, urlparse

import scrapling_bartoux as sb

BASE_DIR = Path(__file__).resolve().parent
AUDIT = BASE_DIR / "audit_manquantes" / "oeuvres_manquantes.json"

CATALOGUE_CANDIDATES = [BASE_DIR / "output_final" / "catalogue.json", BASE_DIR / "catalogue.json"]

OUT_DIR = BASE_DIR / "output_manquantes"
OUT_CATALOGUE = OUT_DIR / "catalogue_manquantes.json"
OUT_IMAGES = OUT_DIR / "artistes"
OUT_REJECTED = OUT_DIR / "rapport_rejetees.json"
OUT_NO_IMAGE = OUT_DIR / "rapport_sans_image.json"

ACCEPTED_CAUSES = {"page_ok_non_scrapee"}
SAVE_EVERY = 20

OUT_DIR.mkdir(exist_ok=True)

logging.getLogger().handlers.clear()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(OUT_DIR / "integrate_manquantes.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("integrate")


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

def norm_url(u: str | None) -> str:
    return urldefrag(u or "")[0].rstrip("/").lower()


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_existing_catalogue() -> tuple[dict, Path | None]:
    """Catalogue principal, en LECTURE SEULE."""
    for p in CATALOGUE_CANDIDATES:
        if p.exists():
            return load_json(p), p
    return {}, None


def pretty_dimensions(dims: str | None) -> str | None:
    """'150x150cm' -> '150 x 150 cm' (format du scraper)."""
    if dims and re.fullmatch(r"[\dxX.,]+cm", dims.strip(), re.I):
        return dims.lower().replace("x", " x ").replace("cm", " cm").strip()
    return dims


def image_path_for(rec: dict, title: str, dims: str | None, image_url: str) -> Path:
    slug = sb.slugify(rec["artist_name"])
    base = sb.slugify(title)
    d = sb.normalize_dimensions_for_filename(dims)
    name = f"{base}-{d}" if d else base
    ext = Path(urlparse(image_url).path).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    return OUT_IMAGES / slug / f"{name}{ext}"


def rel(path: Path) -> str:
    """Chemin relatif au dossier du script : output_manquantes/artistes/<slug>/<fichier>.
    Le prefixe output_manquantes/ permet de reconnaitre tout de suite les oeuvres
    qui n'avaient pas ete scrapees, sans rien melanger avec la production."""
    return str(path.relative_to(BASE_DIR)).replace("\\", "/")


def try_download(rec: dict, title: str, dims: str | None) -> tuple[str | None, str | None]:
    """Essaie l'image de la page, puis la miniature de la liste.
    Retourne (url_image_utilisee, chemin_local) ou (url, None) si echec."""
    candidates = []
    for key in ("image", "image_listing"):
        u = rec.get(key)
        if u and u not in candidates:
            candidates.append(u)
    if not candidates:
        return None, None

    for url in candidates:
        path = image_path_for(rec, title, dims, url)
        if path.exists() and path.stat().st_size > 0:
            return url, rel(path)
        if sb.download_image(url, path):
            return url, rel(path)
    return candidates[0], None


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Catalogue separe des oeuvres manquantes Bartoux")
    ap.add_argument("--dry-run", action="store_true", help="Simulation : rien n'est ecrit ni telecharge")
    args = ap.parse_args()

    if not AUDIT.exists():
        sys.exit(f"{AUDIT} introuvable. Lance d'abord audit_oeuvres_manquantes.py")
    missing = load_json(AUDIT)

    main_catalogue, main_path = load_existing_catalogue()
    main_urls = {norm_url(e.get("url")) for e in main_catalogue.values()}
    main_ids = set(main_catalogue.keys())
    log.info(f"Catalogue principal (lecture seule) : {main_path} ({len(main_catalogue)} oeuvres)")
    log.info(f"Audit : {len(missing)} oeuvres manquantes")

    # Reprise : on recharge le catalogue des manquantes s'il existe deja
    new_catalogue: dict = load_json(OUT_CATALOGUE) if OUT_CATALOGUE.exists() else {}
    if new_catalogue:
        log.info(f"Reprise : {len(new_catalogue)} oeuvres deja dans {OUT_CATALOGUE.name}")

    stats = {"added": 0, "retried_image": 0, "already_main": 0, "already_done": 0,
             "rejected": 0, "no_image": 0}
    rejected: list[dict] = []
    no_image: list[dict] = []

    try:
        for rec in missing:
            url = urldefrag(rec["url"])[0]
            pid = sb.make_stable_id(url)

            # Deja dans le catalogue principal : on ignore
            if pid in main_ids or norm_url(url) in main_urls:
                stats["already_main"] += 1
                continue

            # Deja traitee dans le catalogue des manquantes
            if pid in new_catalogue:
                entry = new_catalogue[pid]
                has_file = entry.get("local_image") and (BASE_DIR / entry["local_image"]).exists()
                if has_file or args.dry_run:
                    stats["already_done"] += 1
                    continue
                # image manquante : on retente seulement l'image
                if not args.dry_run:
                    used, local = try_download(rec, entry["title"], entry.get("dimensions"))
                    if local:
                        entry["image"], entry["local_image"] = used, local
                        stats["retried_image"] += 1
                        log.info(f"[IMAGE RECUPEREE] {entry['title']}")
                    else:
                        no_image.append({"title": entry["title"], "url": url, "image": used})
                        stats["no_image"] += 1
                continue

            # Filtre qualite
            if (rec.get("cause") not in ACCEPTED_CAUSES
                    or rec.get("suspect_non_oeuvre")
                    or not rec.get("title")):
                stats["rejected"] += 1
                rejected.append({"url": url, "artist_name": rec.get("artist_name"),
                                 "title": rec.get("title"), "cause": rec.get("cause"),
                                 "suspect_non_oeuvre": rec.get("suspect_non_oeuvre")})
                log.warning(f"[REJETE] {rec.get('cause')} | {url}")
                continue

            title = rec["title"]
            dims = pretty_dimensions(rec.get("dimensions"))
            slug = sb.slugify(rec["artist_name"])

            image_used, local = rec.get("image"), None
            if not args.dry_run:
                image_used, local = try_download(rec, title, dims)
                if not local:
                    stats["no_image"] += 1
                    no_image.append({"title": title, "url": url, "image": image_used})
                    log.warning(f"[SANS IMAGE] {title} | {url}")

            # Meme structure que catalogue.json : 9 champs, rien d'autre
            new_catalogue[pid] = {
                "artist_id": rec["artist_id"],
                "artist_name": rec["artist_name"],
                "artist_slug": slug,
                "artist_category": rec["artist_category"],
                "title": title,
                "dimensions": dims,
                "image": image_used,
                "local_image": local,
                "url": url,
            }
            stats["added"] += 1
            log.info(f"[AJOUT] {rec['artist_name']} | {title} | {dims} | {local}")

            if not args.dry_run and stats["added"] % SAVE_EVERY == 0:
                save_json(OUT_CATALOGUE, new_catalogue)
    except KeyboardInterrupt:
        log.warning("Interruption : sauvegarde de l'etat actuel.")
    finally:
        if not args.dry_run:
            save_json(OUT_CATALOGUE, new_catalogue)
            save_json(OUT_REJECTED, rejected)
            save_json(OUT_NO_IMAGE, no_image)
        log.info("===== RESUME =====" + (" (DRY RUN)" if args.dry_run else ""))
        log.info(f"Nouvelles oeuvres ajoutees      : {stats['added']}")
        log.info(f"Images recuperees a la reprise  : {stats['retried_image']}")
        log.info(f"Deja dans catalogue principal   : {stats['already_main']}")
        log.info(f"Deja traitees (reprise)         : {stats['already_done']}")
        log.info(f"Rejetees                        : {stats['rejected']}")
        log.info(f"Sans image                      : {stats['no_image']}")
        log.info(f"Total catalogue_manquantes.json : {len(new_catalogue)} oeuvres")
        log.info(f"Dossier de sortie               : {OUT_DIR}")


if __name__ == "__main__":
    main()