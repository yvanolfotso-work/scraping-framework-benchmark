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
import os
import shutil
import logging
import requests
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from pathlib import Path


# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

BASE_URL = "https://www.galeries-bartoux.com"
ARTISTS_URL = "https://www.galeries-bartoux.com/artistes/"

CATALOGUE_FILE = "catalogue.json"
BACKUP_DIR = "backups"
LOG_FILE = "scraping_bartoux.log"
IMAGES_ROOT = "artistes"          # dossier principal des images

MIN_RATIO_SECURITE = 0.35

# Délais (plus longs = moins de timeouts)
SLEEP_BETWEEN_ARTISTS = 3.5
SLEEP_BETWEEN_ARTWORKS = 2.8
PAGE_TIMEOUT = 60000              # 60 secondes

MAX_ARTISTS = None                # mets 5 pour tester rapidement
MAX_ARTWORKS_PER_ARTIST = None


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
    """Transforme un texte en nom de fichier/dossier sûr"""
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
    """Extrait les dimensions depuis le slug de l'URL (très fiable sur Bartoux)"""
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
    """Transforme '120 x 180 cm' → '120x180cm' pour le nom de fichier"""
    if not dimensions:
        return None
    dims = dimensions.lower()
    dims = re.sub(r"[^\d]+", "x", dims)          # tout ce qui n'est pas chiffre → x
    dims = re.sub(r"x+", "x", dims).strip("x")
    if dims:
        return f"{dims}cm"
    return None


def download_image(img_url: str, save_path: Path) -> bool:
    """Télécharge une image et la sauvegarde"""
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
        log.warning(f"      Échec téléchargement image {img_url} : {e}")
    return False


# ----------------------------------------------------------------------
# 1. ARTISTES
# ----------------------------------------------------------------------

def get_artists() -> list[dict]:
    log.info(f"Chargement page artistes : {ARTISTS_URL}")
    try:
        page = StealthyFetcher.fetch(
            ARTISTS_URL,
            headless=True,
            network_idle=True,
            timeout=PAGE_TIMEOUT
        )
    except Exception as e:
        log.error(f"Échec chargement page artistes : {e}")
        return []

    time.sleep(2)

    artists = []
    links = page.css('a[href*="/artistes/"]')
    seen = set()

    for a in links:
        href = a.css("::attr(href)").get()
        name = clean_text(a.css("::text").get() or a.get_all_text())
        if not href or not name:
            continue
        full = absolute_url(href)
        if not full or full.rstrip("/") == ARTISTS_URL.rstrip("/") or full in seen:
            continue
        if "/artistes/" in full and full.count("/") >= 4:
            seen.add(full)
            artists.append({"name": name, "url": full, "slug": slugify(name)})

    log.info(f"Artistes trouvés : {len(artists)}")
    return artists


# ----------------------------------------------------------------------
# 2. ŒUVRES D'UN ARTISTE
# ----------------------------------------------------------------------

def get_artworks_from_artist(artist: dict) -> list[dict]:
    log.info(f"  → Artiste : {artist['name']}")
    try:
        page = StealthyFetcher.fetch(
            artist["url"],
            headless=True,
            network_idle=True,
            timeout=PAGE_TIMEOUT
        )
    except Exception as e:
        log.error(f"    Échec page artiste {artist['name']} : {e}")
        return []

    time.sleep(SLEEP_BETWEEN_ARTISTS)

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
                "artist_url": artist["url"]
            })

    log.info(f"    Œuvres candidates : {len(artworks)}")
    return artworks


# ----------------------------------------------------------------------
# 3. DÉTAIL + TÉLÉCHARGEMENT IMAGE
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
        log.warning(f"      Échec page œuvre {url} : {e}")
        return None

    time.sleep(SLEEP_BETWEEN_ARTWORKS)

    # Détection des fausses pages (redirection 301 vers la page artiste)
    current_url = str(getattr(page, "url", url))
    if current_url.rstrip("/") == item["artist_url"].rstrip("/"):
        log.warning(f"      Redirection vers page artiste → ignoré : {url}")
        return None

    # --- Titre ---
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

    # --- Dimensions / Technique / Année ---
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
            # remettre un format lisible
            dimensions = dimensions.replace("x", " x ").replace("cm", " cm")

    if not dimensions:
        dim_fallback = re.search(r"(\d+[\d\s.,xX×\-]*\s*cm)", text, re.I)
        if dim_fallback:
            dimensions = clean_text(dim_fallback.group(1))

    tech_match = re.search(r"(?:Technique|Matériau|Medium)\s*[:\n]*\s*([^\n]{3,140})", text, re.I)
    if tech_match:
        medium = clean_text(tech_match.group(1))

    year_match = re.search(r"(?:Année|Year)\s*[:\n]*\s*(\d{4})", text, re.I)
    if year_match:
        year = year_match.group(1)

    # --- Image ---
    image = (
        page.css("img.wp-post-image::attr(src)").get()
        or page.css(".oeuvre-image img::attr(src)").get()
        or page.css("article img::attr(src)").get()
        or page.css("figure img::attr(src)").get()
        or page.css("img::attr(src)").get()
    )
    image = absolute_url(image) if image else None

    # --- Description ---
    description = None
    for p in page.css("p::text")[:6]:
        t = clean_text(p)
        if t and len(t) > 50 and "cookie" not in t.lower():
            description = t
            break

    # --- Téléchargement de l'image (nom avec dimensions si présentes) ---
    local_image_path = None
    if image:
        artist_slug = item["artist_slug"]
        artwork_slug = slugify(title or url.rstrip("/").split("/")[-1])

        # Ajout des dimensions dans le nom de fichier
        dims_for_file = normalize_dimensions_for_filename(dimensions)
        if dims_for_file:
            filename_base = f"{artwork_slug}-{dims_for_file}"
        else:
            filename_base = artwork_slug

        ext = Path(urlparse(image).path).suffix.lower()
        if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
            ext = ".jpg"

        save_path = Path(IMAGES_ROOT) / artist_slug / f"{filename_base}{ext}"

        if download_image(image, save_path):
            local_image_path = str(save_path).replace("\\", "/")
            log.info(f"      📷 Image sauvée → {local_image_path}")
        else:
            log.warning(f"      Image non téléchargée : {image}")

    data = {
        "artist": artist,
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
            filter(None, [artist, title, dimensions, medium, year])
        )
    }

    if not title or not artist:
        log.warning(f"      Œuvre incomplète ignorée : {url}")
        return None

    return {
        "natural_id": url,
        "data": data
    }


# ----------------------------------------------------------------------
# 4. SCRAPING COMPLET
# ----------------------------------------------------------------------

def scrape_all() -> list:
    artists = get_artists()
    if MAX_ARTISTS:
        artists = artists[:MAX_ARTISTS]

    all_items = []
    for artist in artists:
        artworks = get_artworks_from_artist(artist)
        if MAX_ARTWORKS_PER_ARTIST:
            artworks = artworks[:MAX_ARTWORKS_PER_ARTIST]

        for aw in artworks:
            detail = scrape_artwork_detail(aw)
            if detail:
                all_items.append(detail)
                d = detail["data"]
                log.info(f"      ✓ {d.get('title')} | {d.get('dimensions')}")

    log.info(f"Total œuvres valides extraites : {len(all_items)}")
    return all_items


# ----------------------------------------------------------------------
# 5. ID + HASH + CATALOGUE
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
    log.info(f"Backup créé : {backup_path}")


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
            log.info(f"[NOUVEAU]   {data.get('artist')} – {data.get('title')}")
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
                log.info(f"[CHANGÉ]    {data.get('title')} → {changes}")
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
# 6. MAIN
# ----------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("Démarrage scraping Galeries Bartoux + images avec dimensions")
    log.info("=" * 60)

    Path(IMAGES_ROOT).mkdir(exist_ok=True)

    scraped = scrape_all()

    if not scraped:
        log.error("Aucune œuvre valide extraite. Arrêt sans toucher au catalogue.")
        return

    catalogue = load_catalogue()

    if catalogue:
        nb_actifs_avant = sum(1 for e in catalogue.values() if e["status"] == "active")
        if nb_actifs_avant > 0:
            ratio = len(scraped) / nb_actifs_avant
            if ratio < MIN_RATIO_SECURITE:
                log.error(
                    f"Scraping suspect : {len(scraped)} œuvres trouvées "
                    f"contre {nb_actifs_avant} attendues (ratio {ratio:.0%}). "
                    f"Catalogue NON modifié."
                )
                return

    backup_catalogue()
    catalogue = merge_catalogue(catalogue, scraped)
    save_catalogue(catalogue)

    total = len(catalogue)
    active = sum(1 for e in catalogue.values() if e["status"] == "active")
    orphans = sum(1 for e in catalogue.values() if e["status"] == "orphan")
    with_image = sum(1 for e in catalogue.values() if e["data"].get("local_image"))

    log.info("===== RÉSUMÉ =====")
    log.info(f"Total catalogue     : {total}")
    log.info(f"Actifs              : {active}")
    log.info(f"Orphelins           : {orphans}")
    log.info(f"Avec image locale   : {with_image}")
    log.info(f"Dossier images      : {IMAGES_ROOT}/")
    log.info(f"Fichier             : {CATALOGUE_FILE}")
    log.info("Terminé.")


if __name__ == "__main__":
    main()