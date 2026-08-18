#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrapling_bartoux.py
------------------------------------------------------------------
Scraper Galeries Bartoux, en DEUX catalogues separes :

  - artistes.json    : un enregistrement par artiste (nom, slug,
                        categorie Peintre/Sculpteur/Design, bio
                        REELLE scrapee).
  - catalogue.json    : un enregistrement par oeuvre, avec juste
                        une LIAISON vers l'artiste (artist_id,
                        artist_name, artist_slug, artist_category)
                        -> pas de bio dupliquee dans chaque oeuvre.

L'artist_id est un hash stable de l'URL de l'artiste : c'est la
cle de correspondance entre les deux fichiers.

Fix bio : deux methodes de recuperation en cascade (selecteur CSS
precis, puis decoupage du texte brut autour du marqueur
"Biographie") pour rester robuste si la structure HTML varie
legerement d'un artiste a l'autre.

Independant du repertoire d'execution (os.chdir sur le dossier du
script). Detection de changement par hash sur les deux catalogues.

Usage :
    python scrapling_bartoux.py

------------------------------------------------------------------
FIX (voir revue) :
  Dans process_artist_page(), le filtre des liens d'oeuvres ne
  vérifiait que "/artistes/" dans l'URL + un nombre de "/" supérieur
  à celui de la page artiste. Ça laissait passer des liens vers
  D'AUTRES fiches artistes (ex: bloc "artistes similaires") dès que
  leur URL était plus longue. Le filtre vérifie maintenant que le
  lien est bien un sous-chemin strict de l'URL de l'artiste en cours
  (full.startswith(artist_url + "/")).
------------------------------------------------------------------
"""

from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)

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

ARTISTS_FILE = "artistes.json"
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

CATEGORY_LABELS = {
    "peintres": "Peintre",
    "sculpteurs": "Sculpteur",
    "design": "Design",
}

# Marqueurs textuels utilises pour couper la bio en fallback texte brut
BIO_END_MARKERS = [
    "Plus d'info sur cet artiste",
    "Plus d’info sur cet artiste",
    "Vidéo",
    "Voir toutes les œuvres",
    "Contactez-nous sur WhatsApp",
    "J'aimerais recevoir",
    "J’aimerais recevoir",
]


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
# HELPERS GENERIQUES
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


def make_stable_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


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
    return f"{dims}cm" if dims else None


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
# 1. LISTE DES ARTISTES (avec categorie)
# ----------------------------------------------------------------------

def get_artists() -> list[dict]:
    log.info(f"Chargement page artistes : {ARTISTS_URL}")
    try:
        page = StealthyFetcher.fetch(
            ARTISTS_URL, headless=True, network_idle=True, timeout=PAGE_TIMEOUT
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
                "id": make_stable_id(full),
                "name": name,
                "url": full,
                "slug": slugify(name),
                "category": category,
            })

        log.info(f"  {section_id} ({category}) : {len(articles)} artistes")

    log.info(f"Total artistes trouves : {len(artists)}")
    return artists


# ----------------------------------------------------------------------
# 2. BIO REELLE (deux methodes en cascade, robuste)
# ----------------------------------------------------------------------


def extract_artist_bio(page) -> str | None:
    """
    Récupère la bio réelle d'un artiste.
    Plusieurs méthodes en cascade pour rester robuste face aux variations HTML.
    """

    def _texts_from_selector(selector: str) -> list[str]:
        """Retourne une liste de paragraphes propres depuis un sélecteur CSS."""
        results = []
        try:
            elements = page.css(selector)
            if not elements:
                return []
            for el in elements:
                # 1) texte via ::text (tous les nœuds texte)
                parts = el.css("::text").getall() if hasattr(el, "css") else []
                if parts:
                    t = clean_text(" ".join(parts))
                else:
                    # 2) fallback get_all_text / text
                    raw = None
                    if hasattr(el, "get_all_text"):
                        raw = el.get_all_text()
                    elif hasattr(el, "text"):
                        raw = el.text
                    t = clean_text(raw)
                if t and len(t) > 25 and "cookie" not in t.lower():
                    results.append(t)
        except Exception as e:
            log.debug(f"Sélecteur bio échoué ({selector}) : {e}")
        return results

    # --- Méthode 1 : sélecteurs CSS ciblés (du plus précis au plus large) ---
    css_candidates = [
        "div.The_Content section[itemprop='text'] p",
        "div.The_Content section p",
        "section[itemprop='text'] p",
        "div.The_Content p",
        ".The_Content p",
    ]
    for sel in css_candidates:
        paragraphs = _texts_from_selector(sel)
        if paragraphs:
            bio = "\n\n".join(paragraphs)
            if len(bio) > 60:
                return bio

    # --- Méthode 2 : XPath (souvent plus fiable que CSS + ::text) ---
    try:
        xpath_paragraphs = page.xpath(
            "//div[contains(@class,'The_Content')]//section[@itemprop='text']//p"
            " | //div[contains(@class,'The_Content')]//p"
            " | //section[@itemprop='text']//p"
        )
        cleaned = []
        for node in xpath_paragraphs:
            t = clean_text(node.get_all_text() if hasattr(node, "get_all_text") else node.xpath("string()").get())
            if t and len(t) > 25 and "cookie" not in t.lower():
                cleaned.append(t)
        if cleaned:
            bio = "\n\n".join(cleaned)
            if len(bio) > 60:
                return bio
    except Exception as e:
        log.debug(f"XPath bio échoué : {e}")

    # --- Méthode 3 : texte brut de la page, découpe autour de "Biographie" ---
    full_text = ""
    try:
        full_text = page.get_all_text() or ""
    except Exception:
        pass

    if "Biographie" in full_text:
        after = full_text.split("Biographie", 1)[1]
        # On retire le nom de l'artiste s'il est juste après le titre
        # (souvent "Biographie\nAL FRENO\nÀ l'issue...")
        lines = [ln.strip() for ln in after.splitlines() if ln.strip()]
        # saute les lignes très courtes (titre / nom)
        start_idx = 0
        for i, ln in enumerate(lines[:4]):
            if len(ln) < 40 and not ln.endswith("."):
                start_idx = i + 1
            else:
                break
        after = "\n".join(lines[start_idx:])

        for marker in BIO_END_MARKERS:
            if marker in after:
                after = after.split(marker, 1)[0]
                break
        # coupe aussi sur le formulaire / WhatsApp
        for extra in ["Nom*", "Prénom*", "Contactez-nous sur WhatsApp", "Plus d'info sur cet artiste"]:
            if extra in after:
                after = after.split(extra, 1)[0]
        bio = clean_text(after)
        if bio and len(bio) > 80:
            return bio

    # --- Méthode 4 : meta description (début de bio) ---
    try:
        meta = page.css('meta[name="description"]::attr(content)').get()
        meta = clean_text(meta)
        if meta and len(meta) > 80:
            return meta
    except Exception:
        pass

    # --- Méthode 5 : JSON-LD (schema.org) ---
    try:
        for script in page.css('script[type="application/ld+json"]::text').getall():
            if not script:
                continue
            data = json.loads(script)
            graph = data.get("@graph", [data] if isinstance(data, dict) else [])
            for item in graph:
                if not isinstance(item, dict):
                    continue
                desc = item.get("description")
                if desc and len(str(desc)) > 80:
                    return clean_text(desc)
    except Exception:
        pass

    return None

# ----------------------------------------------------------------------
# 3. PAGE ARTISTE : bio + liste des liens d'oeuvres (une seule visite)
# ----------------------------------------------------------------------

def process_artist_page(artist: dict) -> tuple[dict, list[dict]]:
    """
    Retourne (artist_record_pour_artistes_json, liste_liens_oeuvres)
    en ne visitant la page artiste qu'une seule fois.
    """
    log.info(f"  -> Artiste : {artist['name']} [{artist['category']}]")
    try:
        page = StealthyFetcher.fetch(
            artist["url"], headless=True, network_idle=True, timeout=PAGE_TIMEOUT
        )
    except Exception as e:
        log.error(f"    Echec page artiste {artist['name']} : {e}")
        return None, []

    time.sleep(SLEEP_BETWEEN_ARTISTS)

    bio = extract_artist_bio(page)
    if bio:
        log.info(f"    Bio recuperee ({len(bio)} caracteres)")
    else:
        log.warning(f"    Aucune bio trouvee pour {artist['name']} (les deux methodes ont echoue)")

    artist_data = {
        "name": artist["name"],
        "slug": artist["slug"],
        "category": artist["category"],
        "url": artist["url"],
        "bio": bio,
        "gallery": "Galeries Bartoux",
    }
    artist_record = {"id": artist["id"], "data": artist_data}

    # --- FIX ---
    # Liens d'oeuvres, extraits de la meme page deja chargee.
    # On ne garde que les liens qui sont reellement des sous-pages de
    # CET artiste : full doit commencer par son URL + "/".
    # L'ancien test ("/artistes/" in full + full.count("/") > artist["url"].count("/"))
    # laissait passer des liens vers D'AUTRES fiches artistes (ex: bloc
    # "artistes similaires" en bas de page) des que leur URL etait plus
    # longue que celle de l'artiste en cours -> pollution du catalogue
    # d'oeuvres avec des liens qui ne sont pas des oeuvres.
    artwork_links = []
    seen = set()
    artist_url_prefix = artist["url"].rstrip("/") + "/"

    for a in page.css('a[href*="/artistes/"]'):
        href = a.css("::attr(href)").get()
        title = clean_text(a.css("::text").get() or a.get_all_text())
        if not href:
            continue
        full = absolute_url(href)

        if (full
                and full != artist["url"]
                and full.startswith(artist_url_prefix)
                and full not in seen):
            seen.add(full)
            artwork_links.append({
                "title_guess": title,
                "url": full,
                "artist_id": artist["id"],
                "artist_name": artist["name"],
                "artist_slug": artist["slug"],
                "artist_category": artist["category"],
                "artist_url": artist["url"],
            })

    log.info(f"    Oeuvres candidates : {len(artwork_links)}")
    return artist_record, artwork_links


# ----------------------------------------------------------------------
# 4. DETAIL D'UNE OEUVRE + IMAGE (liaison via artist_id, pas de bio ici)
# ----------------------------------------------------------------------

def scrape_artwork_detail(item: dict) -> dict | None:
    url = item["url"]
    try:
        page = StealthyFetcher.fetch(
            url, headless=True, network_idle=True, timeout=PAGE_TIMEOUT
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
        artwork_slug = slugify(title or url.rstrip("/").split("/")[-1])
        dims_for_file = normalize_dimensions_for_filename(dimensions)
        filename_base = f"{artwork_slug}-{dims_for_file}" if dims_for_file else artwork_slug
        ext = Path(urlparse(image).path).suffix.lower()
        if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
            ext = ".jpg"
        save_path = Path(IMAGES_ROOT) / item["artist_slug"] / f"{filename_base}{ext}"
        if download_image(image, save_path):
            local_image_path = str(save_path).replace("\\", "/")
            log.info(f"      Image sauvee -> {local_image_path}")
        else:
            log.warning(f"      Image non telechargee : {image}")

    # --- Liaison vers l'artiste : juste la reference, pas la bio ---
    data = {
        "artist_id": item["artist_id"],
        "artist_name": item["artist_name"],
        "artist_slug": item["artist_slug"],
        "artist_category": item["artist_category"],
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
            filter(None, [item["artist_name"], item["artist_category"], title, dimensions, medium, year])
        )
    }

    if not title or not item["artist_name"]:
        log.warning(f"      Oeuvre incomplete ignoree : {url}")
        return None

    return {"id": make_stable_id(url), "data": data}


# ----------------------------------------------------------------------
# 5. SCRAPING COMPLET
# ----------------------------------------------------------------------

def scrape_all() -> tuple[list, list]:
    artists = get_artists()
    if MAX_ARTISTS:
        artists = artists[:MAX_ARTISTS]

    artist_records = []
    artwork_records = []

    for artist in artists:
        artist_record, artwork_links = process_artist_page(artist)
        if artist_record:
            artist_records.append(artist_record)

        if MAX_ARTWORKS_PER_ARTIST:
            artwork_links = artwork_links[:MAX_ARTWORKS_PER_ARTIST]

        for link in artwork_links:
            detail = scrape_artwork_detail(link)
            if detail:
                artwork_records.append(detail)
                d = detail["data"]
                log.info(f"      OK {d.get('title')} | {d.get('dimensions')}")

    log.info(f"Total artistes valides : {len(artist_records)}")
    log.info(f"Total oeuvres valides  : {len(artwork_records)}")
    return artist_records, artwork_records


# ----------------------------------------------------------------------
# 6. CATALOGUE GENERIQUE (hash + historique), reutilise pour les deux
# ----------------------------------------------------------------------

def make_content_hash(data: dict, exclude: set = frozenset({"local_image"})) -> str:
    payload = {k: v for k, v in data.items() if k not in exclude}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def backup_file(path: str, label: str):
    if not os.path.exists(path):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"{label}_{stamp}.json")
    shutil.copy2(path, backup_path)
    log.info(f"Backup cree : {backup_path}")


def save_json(path: str, data: dict):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def diff_fields(old_data: dict, new_data: dict, exclude: set = frozenset({"searchable_text", "local_image"})) -> dict:
    changes = {}
    for key in new_data:
        if key in exclude:
            continue
        old_val = old_data.get(key)
        new_val = new_data.get(key)
        if old_val != new_val:
            changes[key] = {"avant": old_val, "apres": new_val}
    return changes


def merge_catalogue(catalogue: dict, items: list, label: str) -> dict:
    """
    items : liste de {"id":..., "data": {...}}
    Detection NOUVEAU / CHANGE / ORPHELIN, identique pour artistes et oeuvres.
    """
    timestamp = now_iso()
    seen_ids = set()

    for item in items:
        pid = item["id"]
        data = item["data"]
        chash = make_content_hash(data)
        seen_ids.add(pid)

        if pid not in catalogue:
            catalogue[pid] = {
                "id": pid,
                "content_hash": chash,
                "first_seen": timestamp,
                "last_checked": timestamp,
                "last_changed": timestamp,
                "status": "active",
                "data": data,
                "history": []
            }
            log.info(f"[NOUVEAU:{label}] {data.get('name') or data.get('title')}")
        else:
            entry = catalogue[pid]
            entry["last_checked"] = timestamp
            entry["status"] = "active"

            if entry["content_hash"] != chash:
                changes = diff_fields(entry["data"], data)
                entry.setdefault("history", []).append({"date": timestamp, "changes": changes})
                entry["content_hash"] = chash
                entry["data"] = data
                entry["last_changed"] = timestamp
                log.info(f"[CHANGE:{label}]  {data.get('name') or data.get('title')} -> {changes}")
            else:
                if data.get("local_image"):
                    entry["data"]["local_image"] = data["local_image"]

    for pid, entry in catalogue.items():
        if pid not in seen_ids and entry["status"] != "orphan":
            entry["status"] = "orphan"
            entry["last_checked"] = timestamp
            log.info(f"[ORPHELIN:{label}] {entry['data'].get('name') or entry['data'].get('title')}")

    return catalogue


def ratio_ok(catalogue: dict, nb_new: int) -> bool:
    if not catalogue:
        return True
    nb_actifs_avant = sum(1 for e in catalogue.values() if e["status"] == "active")
    if nb_actifs_avant == 0:
        return True
    return (nb_new / nb_actifs_avant) >= MIN_RATIO_SECURITE


# ----------------------------------------------------------------------
# 7. MAIN
# ----------------------------------------------------------------------

def main():
    log.info("=" * 60)
    log.info("Demarrage scraping Galeries Bartoux (artistes.json + catalogue.json)")
    log.info("=" * 60)

    Path(IMAGES_ROOT).mkdir(exist_ok=True)

    artist_records, artwork_records = scrape_all()

    if not artist_records and not artwork_records:
        log.error("Rien d'extrait. Arret sans toucher aux fichiers.")
        return

    # --- Catalogue artistes ---
    artistes_catalogue = load_json(ARTISTS_FILE)
    if artist_records:
        if not ratio_ok(artistes_catalogue, len(artist_records)):
            log.error("Scraping ARTISTES suspect (trop peu de resultats). artistes.json NON modifie.")
        else:
            backup_file(ARTISTS_FILE, "artistes")
            artistes_catalogue = merge_catalogue(artistes_catalogue, artist_records, "artiste")
            save_json(ARTISTS_FILE, artistes_catalogue)

    # --- Catalogue oeuvres ---
    oeuvres_catalogue = load_json(CATALOGUE_FILE)
    if artwork_records:
        if not ratio_ok(oeuvres_catalogue, len(artwork_records)):
            log.error("Scraping OEUVRES suspect (trop peu de resultats). catalogue.json NON modifie.")
        else:
            backup_file(CATALOGUE_FILE, "catalogue")
            oeuvres_catalogue = merge_catalogue(oeuvres_catalogue, artwork_records, "oeuvre")
            save_json(CATALOGUE_FILE, oeuvres_catalogue)

    # --- Resume ---
    total_artistes = len(artistes_catalogue)
    actifs_artistes = sum(1 for e in artistes_catalogue.values() if e["status"] == "active")
    avec_bio = sum(1 for e in artistes_catalogue.values() if e["data"].get("bio"))

    total_oeuvres = len(oeuvres_catalogue)
    actifs_oeuvres = sum(1 for e in oeuvres_catalogue.values() if e["status"] == "active")
    avec_image = sum(1 for e in oeuvres_catalogue.values() if e["data"].get("local_image"))

    log.info("===== RESUME =====")
    log.info(f"[Artistes]  total={total_artistes} actifs={actifs_artistes} avec_bio={avec_bio} -> {ARTISTS_FILE}")
    log.info(f"[Oeuvres]   total={total_oeuvres} actifs={actifs_oeuvres} avec_image={avec_image} -> {CATALOGUE_FILE}")
    log.info(f"Dossier images : {IMAGES_ROOT}/")
    log.info("Termine.")


if __name__ == "__main__":
    main()