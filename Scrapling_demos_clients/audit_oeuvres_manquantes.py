#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_oeuvres_manquantes.py
------------------------------------------------------------------
Audit Galeries Bartoux : identifie les oeuvres presentes sur le site
mais ABSENTES de catalogue.json (non scrapees), avec leurs infos.

Principe :
  1. Recharge la liste des artistes du site (get_artists)
  2. Pour chaque artiste, relit sa page et collecte TOUS les liens d'oeuvres
     (+ titre et miniature visibles sur la liste)
  3. Compare avec catalogue.json  ->  oeuvres manquantes
  4. Pour chaque oeuvre manquante, visite sa page et recupere :
     titre, description, dimensions, image, et diagnostique la cause
     (page inaccessible, redirection, titre absent, simple oubli...)

A placer dans le MEME dossier que scrapling_bartoux.py (il en reutilise
les fonctions : fetch_page, get_artists, etc.).

Sorties (dossier audit_manquantes/) :
    oeuvres_manquantes.json       detail complet par oeuvre
    oeuvres_manquantes.csv        meme chose, pour Excel
    resume_par_artiste.csv        attendues / scrapees / manquantes par artiste
    artistes_manquants.json       artistes du site absents de artistes.json
                                  + artistes dont la page est inaccessible
    audit.log

Usage :
    python audit_oeuvres_manquantes.py
    python audit_oeuvres_manquantes.py --limit-artists 5     # test
    python audit_oeuvres_manquantes.py --no-detail           # sans visiter les pages manquantes
------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag

# Reutilise le scraper existant (patch browserforge inclus a l'import)
import scrapling_bartoux as sb

BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "audit_manquantes"
OUT_DIR.mkdir(exist_ok=True)

CATALOGUE_CANDIDATES = [BASE_DIR / "output_final" / "catalogue.json", BASE_DIR / "catalogue.json"]
ARTISTS_CANDIDATES = [BASE_DIR / "output_final" / "artistes.json", BASE_DIR / "artistes.json"]

SLEEP_DETAIL = 2.0
SUSPECT_PATTERNS = ("/page/", "/feed", "?", "/comments")

logging.getLogger().handlers.clear()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(OUT_DIR / "audit.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
    force=True,
)
log = logging.getLogger("audit_bartoux")


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

def norm_url(url: str | None) -> str:
    if not url:
        return ""
    return urldefrag(url)[0].rstrip("/").lower()


def load_first(paths: list[Path]) -> tuple[dict, Path | None]:
    for p in paths:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f), p
            except Exception as e:
                log.error(f"Lecture impossible {p} : {e}")
    return {}, None


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def save_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:   # utf-8-sig : accents OK dans Excel
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore", delimiter=";")
        w.writeheader()
        w.writerows(rows)


def slug_to_title(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1].replace("-", " ").upper()
    return re.sub(r"\d+\s*[xX×]\s*\d+.*", "", slug).strip()


# ----------------------------------------------------------------------
# 1. LIENS D'OEUVRES SUR LA PAGE ARTISTE
# ----------------------------------------------------------------------

def collect_listing(artist: dict, retries: int, timeout: int) -> dict | None:
    """Retourne {url_normalisee: info} ou None si la page artiste est inaccessible."""
    page = sb.fetch_page(artist["url"], retries=retries, base_timeout=timeout)
    if page is None:
        return None
    time.sleep(1.5)

    prefix = norm_url(artist["url"]) + "/"
    found: dict[str, dict] = {}

    for a in page.css('a[href*="/artistes/"]'):
        href = a.css("::attr(href)").get()
        if not href:
            continue
        full = urldefrag(sb.absolute_url(href) or "")[0]
        n = norm_url(full)
        if not n or n == norm_url(artist["url"]) or not (n + "/").startswith(prefix):
            continue

        title = sb.clean_text(a.css("::text").get() or a.get_all_text())
        img = (a.css("img::attr(src)").get() or a.css("img::attr(data-src)").get()
               or a.css("img::attr(data-lazy-src)").get())
        img = sb.absolute_url(img) if img else None

        cur = found.setdefault(n, {"url": full, "title_listing": None, "image_listing": None})
        cur["title_listing"] = cur["title_listing"] or title
        cur["image_listing"] = cur["image_listing"] or img

    return found


# ----------------------------------------------------------------------
# 2. DETAIL D'UNE OEUVRE MANQUANTE + DIAGNOSTIC
# ----------------------------------------------------------------------

def extract_description(page) -> str | None:
    for sel in ("div.The_Content section[itemprop='text'] p", "div.The_Content p",
                "section[itemprop='text'] p", "article p"):
        try:
            parts = []
            for el in page.css(sel):
                t = sb.clean_text(" ".join(el.css("::text").getall()))
                if t and len(t) > 25 and "cookie" not in t.lower():
                    parts.append(t)
            if parts:
                return "\n\n".join(parts)
        except Exception:
            continue
    meta = sb.clean_text(page.css('meta[name="description"]::attr(content)').get())
    return meta if meta and len(meta) > 40 else None


def inspect_missing(rec: dict) -> dict:
    """Visite la page de l'oeuvre et complete rec. Diagnostique la cause."""
    page = sb.fetch_page(rec["url"], retries=sb.SECOND_PASS_RETRIES, base_timeout=sb.SECOND_PASS_TIMEOUT)
    if page is None:
        rec["cause"] = "page_inaccessible"
        return rec
    time.sleep(SLEEP_DETAIL)

    current = norm_url(str(getattr(page, "url", rec["url"])))
    if current == norm_url(rec["artist_url"]):
        rec["cause"] = "redirection_vers_page_artiste"
        return rec

    title = sb.clean_text(page.css("h1::text").get() or page.css("h1 *::text").get())
    if title and ("intervention" in title.lower() or len(title) > 90):
        title = None
    rec["title"] = title or rec.get("title_listing") or slug_to_title(rec["url"])

    text = page.get_all_text() or ""
    m = re.search(r"(?:Dimensions|Taille)\s*[:\n]*\s*([^\n]{3,90})", text, re.I)
    dims = sb.clean_text(m.group(1)) if m else sb.extract_dimensions_from_url(rec["url"])
    rec["dimensions"] = dims

    img = (page.css("img.wp-post-image::attr(src)").get() or page.css("article img::attr(src)").get()
           or page.css("figure img::attr(src)").get() or page.css("img::attr(src)").get())
    rec["image"] = sb.absolute_url(img) if img else rec.get("image_listing")
    rec["description"] = extract_description(page)

    if not title:
        rec["cause"] = "titre_absent_sur_la_page"
    elif not rec["image"]:
        rec["cause"] = "image_absente"
    else:
        rec["cause"] = "page_ok_non_scrapee"      # oubli pur : recuperable telle quelle
    return rec


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Audit des oeuvres Bartoux non scrapees")
    ap.add_argument("--limit-artists", type=int, default=None)
    ap.add_argument("--no-detail", action="store_true", help="Ne pas visiter les pages des oeuvres manquantes")
    args = ap.parse_args()

    catalogue, cat_path = load_first(CATALOGUE_CANDIDATES)
    artistes_json, _ = load_first(ARTISTS_CANDIDATES)
    if not catalogue:
        sys.exit("catalogue.json introuvable (ni output_final/ ni dossier courant).")
    log.info(f"Catalogue : {cat_path} ({len(catalogue)} oeuvres)")

    known_urls = {norm_url(e.get("url")) for e in catalogue.values()}
    scraped_by_artist: dict[str, int] = {}
    for e in catalogue.values():
        scraped_by_artist[e.get("artist_id")] = scraped_by_artist.get(e.get("artist_id"), 0) + 1

    artists = sb.get_artists()
    if not artists:
        sys.exit("Liste des artistes inaccessible.")
    if args.limit_artists:
        artists = artists[: args.limit_artists]

    missing: list[dict] = []
    summary: list[dict] = []
    artists_unreachable: list[dict] = []
    total_site = 0

    try:
        for i, artist in enumerate(artists, 1):
            log.info(f"[{i}/{len(artists)}] {artist['name']} [{artist['category']}]")
            listing = collect_listing(artist, sb.MAX_RETRIES, sb.PAGE_TIMEOUT)
            if listing is None:
                listing = collect_listing(artist, sb.SECOND_PASS_RETRIES, sb.SECOND_PASS_TIMEOUT)
            if listing is None:
                log.error(f"  Page artiste inaccessible : {artist['url']}")
                artists_unreachable.append({k: artist[k] for k in ("id", "name", "url", "category")})
                continue

            total_site += len(listing)
            miss = [v for n, v in listing.items() if n not in known_urls]
            log.info(f"  site={len(listing)} scrapees={scraped_by_artist.get(artist['id'], 0)} manquantes={len(miss)}")

            summary.append({
                "artiste": artist["name"], "categorie": artist["category"], "url_artiste": artist["url"],
                "liens_sur_site": len(listing), "dans_catalogue": scraped_by_artist.get(artist["id"], 0),
                "manquantes": len(miss),
            })

            for v in miss:
                rec = {
                    "artist_id": artist["id"], "artist_name": artist["name"],
                    "artist_category": artist["category"], "artist_url": artist["url"],
                    "url": v["url"], "title": v["title_listing"] or slug_to_title(v["url"]),
                    "title_listing": v["title_listing"], "image_listing": v["image_listing"],
                    "image": v["image_listing"], "dimensions": None, "description": None,
                    "suspect_non_oeuvre": any(p in v["url"] for p in SUSPECT_PATTERNS),
                    "cause": "non_visite",
                }
                if not args.no_detail:
                    log.info(f"    -> detail : {v['url']}")
                    inspect_missing(rec)
                missing.append(rec)

            # sauvegarde progressive
            save_json(OUT_DIR / "oeuvres_manquantes.json", missing)
    except KeyboardInterrupt:
        log.warning("Interruption : sauvegarde de l'etat actuel.")
    finally:
        fields = ["artist_name", "artist_category", "title", "dimensions", "description",
                  "url", "image", "artist_url", "cause", "suspect_non_oeuvre"]
        save_json(OUT_DIR / "oeuvres_manquantes.json", missing)
        save_csv(OUT_DIR / "oeuvres_manquantes.csv", missing, fields)
        save_csv(OUT_DIR / "resume_par_artiste.csv", summary,
                 ["artiste", "categorie", "url_artiste", "liens_sur_site", "dans_catalogue", "manquantes"])

        site_artist_ids = {a["id"] for a in artists}
        missing_artists = [{"id": a["id"], "name": a["name"], "url": a["url"], "category": a["category"]}
                           for a in artists if a["id"] not in artistes_json] if artistes_json else []
        save_json(OUT_DIR / "artistes_manquants.json", {
            "absents_de_artistes_json": missing_artists,
            "page_inaccessible": artists_unreachable,
        })

        causes: dict[str, int] = {}
        for r in missing:
            causes[r["cause"]] = causes.get(r["cause"], 0) + 1
        log.info("===== RESUME =====")
        log.info(f"Date                    : {datetime.now(timezone.utc).isoformat()}")
        log.info(f"Liens d'oeuvres sur site: {total_site}")
        log.info(f"Dans catalogue.json     : {len(catalogue)}")
        log.info(f"OEUVRES MANQUANTES      : {len(missing)}")
        for c, n in sorted(causes.items(), key=lambda x: -x[1]):
            log.info(f"   {c:35s}: {n}")
        log.info(f"Artistes absents de artistes.json : {len(missing_artists)}")
        log.info(f"Artistes a page inaccessible      : {len(artists_unreachable)}")
        log.info(f"Rapports -> {OUT_DIR}")


if __name__ == "__main__":
    main()