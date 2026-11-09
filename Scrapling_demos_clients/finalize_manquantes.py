#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
finalize_manquantes.py -- dernier script : oeuvres manquantes -> format production
------------------------------------------------------------------
Entree  : output_manquantes/catalogue_manquantes.json
          output_manquantes/artistes/<slug>/*.jpg
Sortie  : output_manquantes_final/
              catalogue.json          meme format que la production
              artistes/<slug>/*.webp  local_image = "artistes/<slug>/<x>.webp"
              rapport_final.json
              finalize_manquantes.log

Etapes : 1) JPEG -> WebP + color/tonality/format   (enrich_bartoux)
         2) themes/style/material/elements via IA  (enrich_ai_bartoux)

NE MODIFIE JAMAIS output_final/ (lecture seule : doublons + bios artistes).

Usage :
    python finalize_manquantes.py --skip-ai      # WebP + mesures seulement
    python finalize_manquantes.py --limit 2      # test sur 2 oeuvres
    python finalize_manquantes.py                # tout
    python finalize_manquantes.py --force        # tout refaire
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag

import enrich_bartoux as eb
import enrich_ai_bartoux as ea

BASE_DIR = Path(__file__).resolve().parent

SRC_CATALOGUE = BASE_DIR / "output_manquantes" / "catalogue_manquantes.json"
PROD_DIR = BASE_DIR / "output_final"                 # lecture seule
OUT_DIR = BASE_DIR / "output_manquantes_final"
OUT_CATALOGUE = OUT_DIR / "catalogue.json"
OUT_IMAGES = OUT_DIR / "artistes"                    # <-- artistes, pas images
OUT_REPORT = OUT_DIR / "rapport_final.json"
LOG_FILE = OUT_DIR / "finalize_manquantes.log"

log = logging.getLogger("finalize")


def setup_logging() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def norm_url(u: str | None) -> str:
    return urldefrag(u or "")[0].rstrip("/").lower()


def webp_destination(local_image: str) -> Path:
    """output_manquantes/artistes/<slug>/<x>.jpg
       -> output_manquantes_final/artistes/<slug>/<x>.webp"""
    p = Path(local_image)
    return OUT_IMAGES / p.parts[-2] / (p.stem + ".webp")


def save_all(catalogue: dict) -> None:
    eb.save_json(OUT_CATALOGUE, catalogue)


def main() -> None:
    ap = argparse.ArgumentParser(description="Finalisation des oeuvres manquantes")
    ap.add_argument("--limit", type=int, default=None, help="Traiter seulement N oeuvres (test)")
    ap.add_argument("--force", action="store_true", help="Tout refaire (webp + mesures + IA)")
    ap.add_argument("--skip-ai", action="store_true", help="Ne faire que WebP + mesures")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--model", default=ea.DEFAULT_MODEL)
    args = ap.parse_args()

    setup_logging()
    log.info("=" * 60)
    log.info("Finalisation des oeuvres manquantes -> format production")
    log.info("=" * 60)

    if not SRC_CATALOGUE.exists():
        sys.exit(f"{SRC_CATALOGUE} introuvable.")
    source = eb.load_json(SRC_CATALOGUE)

    # Production en lecture seule : doublons + bios
    prod_cat = eb.load_json(PROD_DIR / "catalogue.json")
    prod_artists = eb.load_json(PROD_DIR / "artistes.json")
    if not prod_cat:
        log.warning("output_final/catalogue.json introuvable : verification des doublons impossible.")
    prod_ids = set(prod_cat.keys())
    prod_urls = {norm_url(e.get("url")) for e in prod_cat.values()}

    # noms de fichiers WebP deja presents en production (risque d'ecrasement)
    prod_images: set[str] = set()
    for sub in ("images", "artistes"):
        d = PROD_DIR / sub
        if d.exists():
            prod_images |= {p.name for p in d.rglob("*.webp")}

    # Reprise
    previous = eb.load_json(OUT_CATALOGUE)
    catalogue: dict = {}
    stats = {"source": len(source), "duplicates": 0, "no_image": 0,
             "converted": 0, "measured": 0, "ai_ok": 0, "ai_failed": 0, "name_clash": 0}
    problems: list[dict] = []
    tasks: dict[str, dict] = {}

    for idx, (pid, entry) in enumerate(source.items()):
        if args.limit and idx >= args.limit:
            break
        if pid in prod_ids or norm_url(entry.get("url")) in prod_urls:
            stats["duplicates"] += 1
            log.warning(f"[DOUBLON PRODUCTION] ignoree : {entry.get('title')}")
            continue
        if prod_artists and entry.get("artist_id") not in prod_artists:
            log.warning(f"[ARTISTE] artist_id {entry.get('artist_id')} absent de "
                        f"output_final/artistes.json ({entry.get('artist_name')})")

        prev = previous.get(pid) or {}
        new = {k: entry.get(k) for k in ("artist_id", "artist_name", "artist_slug",
                                         "artist_category", "title", "dimensions",
                                         "image", "local_image", "url")}
        new["ai_metadata"] = eb.normalize_ai(prev.get("ai_metadata"))
        catalogue[pid] = new

        local = entry.get("local_image")
        src = BASE_DIR / local if local else None
        if not src or not src.exists():
            stats["no_image"] += 1
            problems.append({"id": pid, "title": entry.get("title"),
                             "problem": "image source absente"})
            log.warning(f"[SANS IMAGE] {entry.get('title')}")
            continue

        dst = webp_destination(local)
        if dst.name in prod_images:
            stats["name_clash"] += 1
            log.warning(f"[NOM IDENTIQUE EN PRODUCTION] {dst.name} : risque d'ecrasement")
            problems.append({"id": pid, "title": entry.get("title"),
                             "problem": f"fichier {dst.name} existe deja en production"})

        need_convert = args.force or not eb.webp_exists(dst)
        need_analysis = args.force or eb.analysis_missing(new["ai_metadata"], new.get("dimensions"))
        if need_convert or need_analysis:
            tasks[pid] = {"id": pid, "src": str(src), "dst": str(dst),
                          "need_convert": need_convert, "need_analysis": need_analysis,
                          "dimensions": new.get("dimensions"),
                          "mode": eb.WEBP_MODE, "quality": eb.WEBP_QUALITY}
        if eb.webp_exists(dst):
            # ex : artistes/al-freno/la-bamba-150x150cm.webp
            new["local_image"] = eb.to_posix(dst.relative_to(OUT_DIR))

    log.info(f"Oeuvres a integrer : {len(catalogue)} | WebP/mesures a faire : {len(tasks)}")

    try:
        # ---------- ETAPE 1 : WebP + couleurs / tonalite / format ----------
        if tasks:
            with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futs = [pool.submit(eb.process_one, t) for t in tasks.values()]
                for fut in as_completed(futs):
                    res = fut.result()
                    entry = catalogue[res["id"]]
                    dst = Path(tasks[res["id"]]["dst"])
                    if eb.webp_exists(dst):
                        entry["local_image"] = eb.to_posix(dst.relative_to(OUT_DIR))
                    if res["status"] == "ok":
                        if res["analysis"]:
                            eb.merge_analysis(entry["ai_metadata"], res["analysis"], args.force)
                            stats["measured"] += 1
                        if res["converted"]:
                            stats["converted"] += 1
                        log.info(f"  [WEBP+MESURES] {entry['title']}")
                    else:
                        problems.append({"id": res["id"], "title": entry["title"],
                                         "problem": res["error"]})
                        log.warning(f"  ECHEC {entry['title']} : {res['error']}")
            save_all(catalogue)

        # ---------- ETAPE 2 : IA ----------
        if not args.skip_ai:
            api_key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
            if not api_key:
                log.error("ANTHROPIC_API_KEY absente (.env) : etape IA ignoree. "
                          "Relance le script apres l'avoir ajoutee.")
            else:
                client = ea.anthropic.Anthropic(api_key=api_key,
                                                max_retries=ea.SDK_INTERNAL_RETRIES)
                ai_tasks = []
                for pid, entry in catalogue.items():
                    ai = entry["ai_metadata"]
                    done = all(ai[f] for f in ea.REQUIRED_FIELDS)
                    local = entry.get("local_image") or ""
                    img = OUT_DIR / local
                    if (done and not args.force) or not local or not img.exists():
                        continue
                    ai_tasks.append({
                        "id": pid, "title": entry["title"], "image_path": str(img),
                        "context": ea.build_context(
                            entry, prod_artists.get(entry.get("artist_id"), {})),
                    })
                log.info(f"IA : {len(ai_tasks)} oeuvre(s) a analyser | modele {args.model}")

                try:
                    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                        futs = {pool.submit(ea.analyse_one, t, client, args.model,
                                            ea.MAX_ATTEMPTS, ea.REQUEST_TIMEOUT): t
                                for t in ai_tasks}
                        for fut in as_completed(futs):
                            res = fut.result()
                            entry = catalogue[res["id"]]
                            if res["status"] == "ok":
                                for f in ea.AI_LIST_FIELDS:
                                    if args.force or not entry["ai_metadata"][f]:
                                        entry["ai_metadata"][f] = res["result"][f]
                                stats["ai_ok"] += 1
                                log.info(f"  [IA OK] {entry['title']} | "
                                         f"{', '.join(entry['ai_metadata']['style'])} | "
                                         f"{', '.join(entry['ai_metadata']['themes'][:4])}")
                            else:
                                stats["ai_failed"] += 1
                                problems.append({"id": res["id"], "title": entry["title"],
                                                 "problem": res["error"]})
                                log.warning(f"  [IA ECHEC] {entry['title']} : {res['error']}")
                            if (stats["ai_ok"] + stats["ai_failed"]) % 5 == 0:
                                save_all(catalogue)
                except ea.FatalError as e:
                    log.error(f"ARRET IA : {e}")
    except KeyboardInterrupt:
        log.warning("Interruption : sauvegarde de l'etat actuel (reprise possible).")
    finally:
        save_all(catalogue)

        # ---------- Verification finale ----------
        incomplete = []
        for pid, e in catalogue.items():
            ai = e["ai_metadata"]
            lp = e.get("local_image") or ""
            img_ok = lp.startswith("artistes/") and lp.endswith(".webp") \
                and (OUT_DIR / lp).exists()
            ai_ok = all(ai[f] for f in ea.REQUIRED_FIELDS) or args.skip_ai
            if not (img_ok and ai["color"] and all(ai["tonality"].values())
                    and ai["format"]["orientation"] and ai_ok):
                incomplete.append(e["title"])
        eb.save_json(OUT_REPORT, {
            "date": datetime.now(timezone.utc).isoformat(), "model": args.model,
            "stats": stats, "total": len(catalogue),
            "incomplete": incomplete, "problems": problems,
        })
        log.info("===== RESUME =====")
        for k, v in stats.items():
            log.info(f"{k:12s}: {v}")
        log.info(f"Total dans catalogue final : {len(catalogue)}")
        log.info(f"Oeuvres incompletes        : {len(incomplete)} {incomplete}")
        log.info(f"Sortie : {OUT_DIR}")


if __name__ == "__main__":
    main()