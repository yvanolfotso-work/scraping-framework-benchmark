#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrap_bartoux.py
------------------------------------------------------------------
Scraper complet Galeries Bartoux :
  - Parcourt les 3 sections (#peintres, #sculpteurs, #design) de
    la page /artistes/ pour recuperer chaque artiste ET sa
    categorie (Peintre / Sculpteur / Design).
  - Pour chaque artiste, recupere sa VRAIE biographie (scrapee
    directement depuis sa page, section "Biographie" -> aucune
    generation automatique/fake, contrairement a transform_json.py
    qui ne doit plus etre necessaire pour ce site).
  - Parcourt ensuite les oeuvres de chaque artiste, telecharge les
    images (nommees avec les dimensions quand disponibles).
  - Detection de changement par hash (id/contenu) -> NOUVEAU /
    CHANGE / INCHANGE / ORPHELIN, comme le reste du pipeline.

Independant du repertoire d'execution : le script se place
toujours dans son propre dossier au demarrage (os.chdir), donc
catalogue.json / artistes/ / backups/ / logs sont toujours crees
au meme endroit, peu importe d'ou tu lances `python scrap_bartoux.py`.

Usage :
    python scrap_bartoux.py
------------------------------------------------------------------
"""

# --- Toujours travailler depuis le dossier du script ---
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
# --- FIN ---

# --- PATCH browserforge Windows ---
import browserforge.headers.generator as bf_gen


def _fake_generate(self, **kwargs):
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


bf_gen.HeaderGenerator.generate = _fake_generate
# --- FIN PATCH ---

from scrapling.fetchers import StealthyFetcher
import json
import re
import time
import hashlib
import shutil
import logging
import requests
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse


# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

BASE_URL = "https://www.galeries-bartoux.com"
ARTISTS_URL = "https://www.galeries-bartoux.com/artistes/"

CATALOGUE_FILE = "catalogue.json"
BACKUP_DIR = "backups"
LOG_FILE = "scraping_bartoux.log"
IMAGES_ROOT = "artistes"

MIN_RATIO_SECURITE = 0.35

SLEEP_BETWEEN_ARTISTS = 3.5
SLEEP_BETWEEN_ARTWORKS = 2.8
PAGE_TIMEOUT = 60000

MAX_ARTISTS = None                # mets 5 pour tester rapidement
MAX_ARTWORKS_PER_ARTIST = None

# Correspondance id de section HTML -> categorie lisible
CATEGORY_LABELS = {
    "peintres": "Peintre",
    "sculpteurs": "Sculpteur",
    "design": "Design",
}


# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# HELPERS
# ----------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_text(txt):
    if not txt:
        return None
    return re.sub(r"\s+", " ", str(txt)).strip() or None


def absolute_url(href: str) -> str | None:
    if not href:
        return None
    return urljoin(BASE_URL, href)


def slugify(text: str) -> str:
    if not text:
        return "unknown"
    text = text.lower().strip()
    text = re.sub(r"[àáâãäå]", "a", text)
    text = re.sub(r"[èéêë]", "e", text)
    text = re.sub(r"[ìíîï]", "i", text)
    text = re.sub(r"[òóôõö]", "o", text)
    text = re.sub(r"[ùúûü]", "u", text)
    text = re.sub(r"[ç]", "c", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:90] or "unknown"


def extract_dimensions_from_url(url: str) -> str | None:
    match = re.search(
        r"(\d+[\s\-xX×]+\d+(?:[\s\-xX×]+\d+)?)\s*-?\s*cm",
        url, re.IGNORECASE
    )
    if match:
        dims = match.group(1)
        dims = re.sub(r"[\-xX×]+", "x", dims)
        dims = re.sub(r"\s+", "", dims).strip()
        return f"{dims}cm"
    return None


def normalize_dimensions_for_filename(dimensions: str | None) -> str | None:
    if not dimensions:
        return None
    dims = dimensions.lower()
    dims = re.sub(r"[^\d]+", "x", dims)
    dims = re.sub(r"x+", "x", dims).strip("x")
    if dims:
        return f"{dims}cm"
    return None


def download_image(img_url: str, save_path: Path) -> bool:
    if not img_url:
        return False
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": BASE_URL + "/",
        }
        resp = requests.get(img_url, headers=headers, timeout=25, stream=True)
        if resp.status_code == 200 and "image" in resp.headers.get("content-type", ""):
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            return True
    except Exception as e:
        log.warning(f"      Echec telechargement image {img_url} : {e}")
    return False


# ----------------------------------------------------------------------
# 1. ARTISTES (avec categorie Peintre / Sculpteur / Design)
# ----------------------------------------------------------------------

def get_artists() -> list[dict]:
    """
    Parcourt les 3 blocs <div class="Liste_Artistes" id="peintres|sculpteurs|design">
    de la page /artistes/ pour recuperer chaque artiste avec sa
    vraie categorie (pas de devinette : c'est la section HTML qui
    le dit explicitement).
    """
    log.info(f"Chargement page artistes : {ARTISTS_URL}")
    try:
        page = StealthyFetcher.fetch(
            ARTISTS_URL,
            headless=True,
            network_idle=True,
            timeout=PAGE_TIMEOUT
        )
    except Exception as e:
        log.error(f"Echec chargement page artistes : {e}")
        return []

    time.sleep(2)

    artists = []
    seen_urls = set()

    sections = page.css("div.Liste_Artistes")
    log.info(f"Sections trouvees : {len(sections)}")

    for section in sections:
        section_id = section.css("::attr(id)").get() or ""
        category = CATEGORY_LABELS.get(section_id, section_id or "Inconnu")

        articles = section.css("article")
        for art in articles:
            link = art.css("div.CA_Titre a")
            href = link.css("::attr(href)").get()
            name = clean_text(link.css("::text").get())

            if not href or not name:
                continue

            full = absolute_url(href)
            if not full or full in seen_urls:
                continue

            seen_urls.add(full)
            artists.append({
                "name": name,
                "url": full,
                "slug": slugify(name),
                "category": category,
            })

        log.info(f"  {section_id} ({category}) : {len(articles)} artistes")

    log.info(f"Total artistes trouves : {len(artists)}")
    return artists


# ----------------------------------------------------------------------
# 2. BIO REELLE DE L'ARTISTE (scrapee, pas generee)
# ----------------------------------------------------------------------

def extract_artist_bio(page) -> str | None:
    """
    Recupere la vraie biographie depuis la section 'Biographie' de
    la page artiste : <section itemprop="text"><p>...</p>...</section>
    """
    paragraphs = page.css("section[itemprop='text'] p::text").getall()
    if not paragraphs:
        # fallback plus large si la structure varie legerement
        paragraphs = page.css("div.The_Content p::text").getall()

    cleaned = [clean_text(p) for p in paragraphs]
    cleaned = [p for p in cleaned if p and "cookie" not in p.lower()]

    if not cleaned:
        return None

    return "\n\n".join(cleaned)


# ----------------------------------------------------------------------
# 3. OEUVRES D'UN ARTISTE + BIO (une seule visite de la page artiste)
# ----------------------------------------------------------------------

def get_artworks_from_artist(artist: dict) -> tuple[list[dict], str | None]:
    log.info(f"  -> Artiste : {artist['name']} [{artist['category']}]")
    try:
        page = StealthyFetcher.fetch(
            artist["url"],
            headless=True,
            network_idle=True,
            timeout=PAGE_TIMEOUT
        )
    except Exception as e:
        log.error(f"    Echec page artiste {artist['name']} : {e}")
        return [], None

    time.sleep(SLEEP_BETWEEN_ARTISTS)

    # Bio reelle, recuperee sur cette meme page (pas de fetch en plus)
    bio = extract_artist_bio(page)
    if bio:
        log.info(f"    Bio recuperee ({len(bio)} caracteres)")
    else:
        log.warning(f"    Aucune bio trouvee pour {artist['name']}")

    artworks = []
    links = page.css('a[href*="/artistes/"]')
    seen = set()

    for a in links:
        href = a.css("::attr(href)").get()
        title = clean_text(a.css("::text").get() or a.get_all_text())
        if not href:
            continue
        full = absolute_url(href)
        if (full and full != artist["url"]
                and "/artistes/" in full
                and full.count("/") > artist["url"].count("/")
                and full not in seen):
            seen.add(full)
            artworks.append({
                "title_guess": title,
                "url": full,
                "artist_name": artist["name"],
                "artist_slug": artist["slug"],
                "artist_url": artist["url"],
                "artist_category": artist["category"],
                "artist_bio": bio,
            })

    log.info(f"    Oeuvres candidates : {len(artworks)}")
    return artworks, bio


# ----------------------------------------------------------------------
# 4. DETAIL D'UNE OEUVRE + TELECHARGEMENT IMAGE
# ----------------------------------------------------------------------

def scrape_artwork_detail(item: dict) -> dict | None:
    url = item["url"]
    try:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            network_idle=True,
            timeout=PAGE_TIMEOUT
        )
    except Exception as e:
        log.warning(f"      Echec page oeuvre {url} : {e}")
        return None

    time.sleep(SLEEP_BETWEEN_ARTWORKS)

    current_url = str(getattr(page, "url", url))
    if current_url.rstrip("/") == item["artist_url"].rstrip("/"):
        log.warning(f"      Redirection vers page artiste -> ignore : {url}")
        return None

    title = (
        page.css("h1::text").get()
        or page.css("h1 *::text").get()
        or item.get("title_guess")
    )
    title = clean_text(title)

    if title and ("intervention" in title.lower() or len(title) > 90):
        title = None

    if not title:
        slug = url.rstrip("/").split("/")[-1]
        title = slug.replace("-", " ").upper()
        title = re.sub(r"\d+\s*[xX×]\s*\d+.*", "", title).strip()

    artist = item.get("artist_name")

    text = page.get_all_text() or ""

    dimensions = None
    medium = None
    year = None

    dim_match = re.search(r"(?:Dimensions|Taille)\s*[:\n]*\s*([^\n]{3,90})", text, re.I)
    if dim_match:
        dimensions = clean_text(dim_match.group(1))

    if not dimensions:
        dimensions = extract_dimensions_from_url(url)
        if dimensions:
            dimensions = dimensions.replace("x", " x ").replace("cm", " cm")

    if not dimensions:
        dim_fallback = re.search(r"(\d+[\d\s.,xX×\-]*\s*cm)", text, re.I)
        if dim_fallback:
            dimensions = clean_text(dim_fallback.group(1))

    tech_match = re.search(r"(?:Technique|Materiau|Medium)\s*[:\n]*\s*([^\n]{3,140})", text, re.I)
    if tech_match:
        medium = clean_text(tech_match.group(1))

    year_match = re.search(r"(?:Annee|Year)\s*[:\n]*\s*(\d{4})", text, re.I)
    if year_match:
        year = year_match.group(1)

    image = (
        page.css("img.wp-post-image::attr(src)").get()
        or page.css(".oeuvre-image img::attr(src)").get()
        or page.css("article img::attr(src)").get()
        or page.css("figure img::attr(src)").get()
        or page.css("img::attr(src)").get()
    )
    image = absolute_url(image) if image else None

    description = None
    for p in page.css("p::text")[:6]:
        t = clean_text(p)
        if t and len(t) > 50 and "cookie" not in t.lower():
            description = t
            break

    local_image_path = None
    if image:
        artist_slug = item["artist_slug"]
        artwork_slug = slugify(title or url.rstrip("/").split("/")[-1])

        dims_for_file = normalize_dimensions_for_filename(dimensions)
        filename_base = f"{artwork_slug}-{dims_for_file}" if dims_for_file else artwork_slug

        ext = Path(urlparse(image).path).suffix.lower()
        if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
            ext = ".jpg"

        save_path = Path(IMAGES_ROOT) / artist_slug / f"{filename_base}{ext}"

        if download_image(image, save_path):
            local_image_path = str(save_path).replace("\\", "/")
            log.info(f"      Image sauvee -> {local_image_path}")
        else:
            log.warning(f"      Image non telechargee : {image}")

    data = {
        "artist": artist,
        "artist_category": item.get("artist_category"),
        "artist_bio": item.get("artist_bio"),
        "title": title,
        "dimensions": dimensions,
        "medium": medium,
        "year": year,
        "image": image,
        "local_image": local_image_path,
        "url": url,
        "gallery": "Galeries Bartoux",
        "description": description,
        "searchable_text": " - ".join(
            filter(None, [artist, item.get("artist_category"), title, dimensions, medium, year])
        )
    }

    if not title or not artist:
        log.warning(f"      Oeuvre incomplete ignoree : {url}")
        return None

    return {
        "natural_id": url,
        "data": data
    }


# ----------------------------------------------------------------------
# 5. SCRAPING COMPLET
# ----------------------------------------------------------------------

def scrape_all() -> list:
    artists = get_artists()
    if MAX_ARTISTS:
        artists = artists[:MAX_ARTISTS]

    all_items = []
    for artist in artists:
        artworks, _bio = get_artworks_from_artist(artist)
        if MAX_ARTWORKS_PER_ARTIST:
            artworks = artworks[:MAX_ARTWORKS_PER_ARTIST]

        for aw in artworks:
            detail = scrape_artwork_detail(aw)
            if detail:
                all_items.append(detail)
                d = detail["data"]
                log.info(f"      OK {d.get('title')} | {d.get('dimensions')}")

    log.info(f"Total oeuvres valides extraites : {len(all_items)}")
    return all_items


# ----------------------------------------------------------------------
# 6. ID + HASH + CATALOGUE (logique inchangee, id base sur l'URL)
# ----------------------------------------------------------------------

def make_id(item: dict) -> str:
    natural_id = item.get("natural_id")
    if natural_id:
        base = f"url:{natural_id}"
    else:
        d = item["data"]
        base = f"name:{d.get('artist')}|{d.get('title')}|{d.get('dimensions')}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def make_content_hash(data: dict) -> str:
    payload_data = {k: v for k, v in data.items() if k != "local_image"}
    payload = json.dumps(payload_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_catalogue() -> dict:
    if os.path.exists(CATALOGUE_FILE):
        with open(CATALOGUE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def backup_catalogue():
    if not os.path.exists(CATALOGUE_FILE):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"catalogue_{stamp}.json")
    shutil.copy2(CATALOGUE_FILE, backup_path)
    log.info(f"Backup cree : {backup_path}")


def save_catalogue(catalogue: dict):
    tmp_path = CATALOGUE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(catalogue, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CATALOGUE_FILE)


def diff_fields(old_data: dict, new_data: dict) -> dict:
    changes = {}
    for key in new_data:
        if key in ("searchable_text", "local_image"):
            continue
        old_val = old_data.get(key)
        new_val = new_data.get(key)
        if old_val != new_val:
            changes[key] = {"avant": old_val, "apres": new_val}
    return changes


def merge_catalogue(catalogue: dict, scraped_items: list) -> dict:
    timestamp = now_iso()
    seen_ids = set()

    for item in scraped_items:
        pid = make_id(item)
        data = item["data"]
        chash = make_content_hash(data)
        seen_ids.add(pid)

        if pid not in catalogue:
            catalogue[pid] = {
                "id": pid,
                "source_url": data.get("url"),
                "content_hash": chash,
                "first_seen": timestamp,
                "last_checked": timestamp,
                "last_changed": timestamp,
                "status": "active",
                "data": data,
                "history": []
            }
            log.info(f"[NOUVEAU]   {data.get('artist')} ({data.get('artist_category')}) - {data.get('title')}")
        else:
            entry = catalogue[pid]
            entry["last_checked"] = timestamp
            entry["source_url"] = data.get("url")
            entry["status"] = "active"

            if entry["content_hash"] != chash:
                changes = diff_fields(entry["data"], data)
                entry.setdefault("history", []).append({
                    "date": timestamp,
                    "changes": changes
                })
                entry["content_hash"] = chash
                entry["data"] = data
                entry["last_changed"] = timestamp
                log.info(f"[CHANGE]    {data.get('title')} -> {changes}")
            else:
                if data.get("local_image"):
                    entry["data"]["local_image"] = data["local_image"]

    for pid, entry in catalogue.items():
        if pid not in seen_ids and entry["status"] != "orphan":
            entry["status"] = "orphan"
            entry["last_checked"] = timestamp
            log.info(f"[ORPHELIN]  {entry['data'].get('title')}")

    return catalogue


# ----------------------------------------------------------------------
# 7. MAIN
# ----------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("Demarrage scraping Galeries Bartoux (categorie + bio reelle)")
    log.info("=" * 60)

    Path(IMAGES_ROOT).mkdir(exist_ok=True)

    scraped = scrape_all()

    if not scraped:
        log.error("Aucune oeuvre valide extraite. Arret sans toucher au catalogue.")
        return

    catalogue = load_catalogue()

    if catalogue:
        nb_actifs_avant = sum(1 for e in catalogue.values() if e["status"] == "active")
        if nb_actifs_avant > 0:
            ratio = len(scraped) / nb_actifs_avant
            if ratio < MIN_RATIO_SECURITE:
                log.error(
                    f"Scraping suspect : {len(scraped)} oeuvres trouvees "
                    f"contre {nb_actifs_avant} attendues (ratio {ratio:.0%}). "
                    f"Catalogue NON modifie."
                )
                return

    backup_catalogue()
    catalogue = merge_catalogue(catalogue, scraped)
    save_catalogue(catalogue)

    total = len(catalogue)
    active = sum(1 for e in catalogue.values() if e["status"] == "active")
    orphans = sum(1 for e in catalogue.values() if e["status"] == "orphan")
    with_image = sum(1 for e in catalogue.values() if e["data"].get("local_image"))
    with_bio = sum(1 for e in catalogue.values() if e["data"].get("artist_bio"))

    by_category = {}
    for e in catalogue.values():
        cat = e["data"].get("artist_category") or "Inconnu"
        by_category[cat] = by_category.get(cat, 0) + 1

    log.info("===== RESUME =====")
    log.info(f"Total catalogue     : {total}")
    log.info(f"Actifs              : {active}")
    log.info(f"Orphelins           : {orphans}")
    log.info(f"Avec image locale   : {with_image}")
    log.info(f"Avec bio reelle     : {with_bio}")
    for cat, n in by_category.items():
        log.info(f"  - {cat:<12} : {n}")
    log.info(f"Dossier images      : {IMAGES_ROOT}/")
    log.info(f"Fichier             : {CATALOGUE_FILE}")
    log.info("Termine.")


if __name__ == "__main__":
    main()