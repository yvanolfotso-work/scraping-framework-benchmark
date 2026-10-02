#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrapling_bartoux.py
------------------------------------------------------------------
Scraper Galeries Bartoux -> DEUX fichiers JSON :

  artistes.json  : { id: {id, name, url, bio, type} }
  catalogue.json : { id_oeuvre: {artist_id, artist_name, artist_slug,
                                 artist_category, title, dimensions,
                                 image, local_image, url} }

Le HASH de detection de changement est conserve mais N'EST PLUS ecrit
dans artistes.json / catalogue.json : il vit dans un fichier interne
separe (hashes_internes.json).

Robustesse :
  - timeout eleve + augmente a chaque tentative
  - retry avec backoff exponentiel sur TOUTES les requetes
  - seconde passe automatique sur tout ce qui a echoue
  - une bio deja connue n'est jamais ecrasee par une bio vide
  - FRESH_START = True : repart de zero, aucune migration d'anciens fichiers

Usage :
    python scrapling_bartoux.py
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
import random
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
HASH_FILE = "hashes_internes.json"      # hash hors des JSON de sortie
BACKUP_DIR = "backups"
LOG_FILE = "scraping_bartoux.log"
IMAGES_ROOT = "artistes"

MIN_RATIO_SECURITE = 0.35

# True = repart de zero : les anciens JSON et hashes sont ignores (et sauvegardes
# dans backups/ avant d'etre remplaces). Aucune migration.
FRESH_START = True

SLEEP_BETWEEN_ARTISTS = 3.5
SLEEP_BETWEEN_ARTWORKS = 2.8

# --- Timeouts / retry (augmentes) ---
PAGE_TIMEOUT = 120000          # 120 s au premier essai (ms)
TIMEOUT_STEP = 30000           # +30 s a chaque nouvel essai
MAX_RETRIES = 5                # essais par page (passe normale)
SECOND_PASS_RETRIES = 6        # essais par page (seconde passe)
SECOND_PASS_TIMEOUT = 180000   # timeout de depart de la seconde passe
RETRY_BASE_DELAY = 4           # secondes, double a chaque essai
RETRY_MAX_DELAY = 90
IMAGE_RETRIES = 4
IMAGE_TIMEOUT = 60

MAX_ARTISTS = None                # mets 5 pour tester rapidement
MAX_ARTWORKS_PER_ARTIST = None

CATEGORY_LABELS = {
    "peintres": "Peintre",
    "sculpteurs": "Sculpteur",
    "design": "Design",
}

ARTIST_FIELDS = ["id", "name", "url", "bio", "type"]
ARTWORK_FIELDS = [
    "artist_id", "artist_name", "artist_slug", "artist_category",
    "title", "dimensions", "image", "local_image", "url",
]

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


def backoff_sleep(attempt: int):
    delay = min(RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY)
    delay += random.uniform(0, 2)
    time.sleep(delay)


# ----------------------------------------------------------------------
# FETCH AVEC RETRY + TIMEOUT CROISSANT
# ----------------------------------------------------------------------

def fetch_page(url: str, retries: int = MAX_RETRIES, base_timeout: int = PAGE_TIMEOUT):
    """
    Charge une page avec retry. Le timeout augmente a chaque essai,
    et on attend de plus en plus longtemps entre deux essais.
    Retourne la page ou None si tous les essais ont echoue.
    """
    for attempt in range(1, retries + 1):
        timeout = base_timeout + (attempt - 1) * TIMEOUT_STEP
        try:
            page = StealthyFetcher.fetch(
                url, headless=True, network_idle=True, timeout=timeout
            )
            status = getattr(page, "status", 200)
            if isinstance(status, int) and status >= 400:
                raise RuntimeError(f"HTTP {status}")
            return page
        except Exception as e:
            log.warning(
                f"      Essai {attempt}/{retries} echoue ({timeout // 1000}s) "
                f"pour {url} : {e}"
            )
            if attempt < retries:
                backoff_sleep(attempt)
    log.error(f"      ABANDON apres {retries} essais : {url}")
    return None


def download_image(img_url: str, save_path: Path) -> bool:
    if not img_url:
        return False
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": BASE_URL + "/",
    }
    for attempt in range(1, IMAGE_RETRIES + 1):
        try:
            resp = requests.get(img_url, headers=headers, timeout=IMAGE_TIMEOUT, stream=True)
            if resp.status_code == 200 and "image" in resp.headers.get("content-type", ""):
                save_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = save_path.with_suffix(save_path.suffix + ".part")
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(8192):
                        f.write(chunk)
                os.replace(tmp, save_path)
                return True
            raise RuntimeError(f"HTTP {resp.status_code} / {resp.headers.get('content-type')}")
        except Exception as e:
            log.warning(f"      Image essai {attempt}/{IMAGE_RETRIES} echoue {img_url} : {e}")
            if attempt < IMAGE_RETRIES:
                backoff_sleep(attempt)
    return False


# ----------------------------------------------------------------------
# 1. LISTE DES ARTISTES (avec categorie)
# ----------------------------------------------------------------------

def get_artists() -> list[dict]:
    log.info(f"Chargement page artistes : {ARTISTS_URL}")

    # La liste est critique : on la retente jusqu'a obtenir des artistes
    for global_attempt in range(1, 4):
        page = fetch_page(ARTISTS_URL, retries=MAX_RETRIES)
        if page is None:
            log.error(f"Page artistes inaccessible (cycle {global_attempt}/3)")
            time.sleep(15 * global_attempt)
            continue

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

        if artists:
            log.info(f"Total artistes trouves : {len(artists)}")
            return artists

        log.warning(f"Aucun artiste extrait (cycle {global_attempt}/3), nouvel essai...")
        time.sleep(15 * global_attempt)

    log.error("Impossible de recuperer la liste des artistes.")
    return []


# ----------------------------------------------------------------------
# 2. BIO REELLE (plusieurs methodes en cascade)
# ----------------------------------------------------------------------

def extract_artist_bio(page) -> str | None:
    """
    Récupère la bio réelle d'un artiste.
    Plusieurs méthodes en cascade pour rester robuste face aux variations HTML.
    """

    def _texts_from_selector(selector: str) -> list[str]:
        results = []
        try:
            elements = page.css(selector)
            if not elements:
                return []
            for el in elements:
                parts = el.css("::text").getall() if hasattr(el, "css") else []
                if parts:
                    t = clean_text(" ".join(parts))
                else:
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

    # --- Méthode 1 : sélecteurs CSS ciblés ---
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

    # --- Méthode 2 : XPath ---
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

    # --- Méthode 3 : texte brut autour de "Biographie" ---
    full_text = ""
    try:
        full_text = page.get_all_text() or ""
    except Exception:
        pass

    if "Biographie" in full_text:
        after = full_text.split("Biographie", 1)[1]
        lines = [ln.strip() for ln in after.splitlines() if ln.strip()]
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
        for extra in ["Nom*", "Prénom*", "Contactez-nous sur WhatsApp", "Plus d'info sur cet artiste"]:
            if extra in after:
                after = after.split(extra, 1)[0]
        bio = clean_text(after)
        if bio and len(bio) > 80:
            return bio

    # --- Méthode 4 : meta description ---
    try:
        meta = page.css('meta[name="description"]::attr(content)').get()
        meta = clean_text(meta)
        if meta and len(meta) > 80:
            return meta
    except Exception:
        pass

    # --- Méthode 5 : JSON-LD ---
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
# 3. PAGE ARTISTE : bio + liens d'oeuvres (une seule visite)
# ----------------------------------------------------------------------

def process_artist_page(artist: dict, retries: int = MAX_RETRIES,
                        base_timeout: int = PAGE_TIMEOUT):
    """
    Retourne (artist_record, liste_liens_oeuvres), ou (None, []) si la
    page est inaccessible apres tous les essais.
    """
    log.info(f"  -> Artiste : {artist['name']} [{artist['category']}]")
    page = fetch_page(artist["url"], retries=retries, base_timeout=base_timeout)
    if page is None:
        return None, []

    time.sleep(SLEEP_BETWEEN_ARTISTS)

    bio = extract_artist_bio(page)
    if bio:
        log.info(f"    Bio recuperee ({len(bio)} caracteres)")
    else:
        log.warning(f"    Aucune bio trouvee pour {artist['name']}")

    # Structure finale : 5 champs
    artist_data = {
        "id": artist["id"],
        "name": artist["name"],
        "url": artist["url"],
        "bio": bio,
        "type": artist["category"],
    }
    artist_record = {"id": artist["id"], "data": artist_data}

    # Seuls les sous-chemins stricts de l'URL de l'artiste sont des oeuvres
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
# 4. DETAIL D'UNE OEUVRE + IMAGE
# ----------------------------------------------------------------------

def scrape_artwork_detail(item: dict, retries: int = MAX_RETRIES,
                          base_timeout: int = PAGE_TIMEOUT):
    """
    Retourne (statut, record) avec statut dans {"ok", "skip", "fail"}.
      ok   : oeuvre recuperee
      skip : page volontairement ignoree (redirection, incomplete)
      fail : echec reseau apres tous les essais -> a retenter en 2e passe
    """
    url = item["url"]
    page = fetch_page(url, retries=retries, base_timeout=base_timeout)
    if page is None:
        return "fail", None

    time.sleep(SLEEP_BETWEEN_ARTWORKS)

    current_url = str(getattr(page, "url", url))
    if current_url.rstrip("/") == item["artist_url"].rstrip("/"):
        log.warning(f"      Redirection vers page artiste -> ignore : {url}")
        return "skip", None

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

    image = (
        page.css("img.wp-post-image::attr(src)").get()
        or page.css(".oeuvre-image img::attr(src)").get()
        or page.css("article img::attr(src)").get()
        or page.css("figure img::attr(src)").get()
        or page.css("img::attr(src)").get()
    )
    image = absolute_url(image) if image else None

    local_image_path = None
    if image:
        artwork_slug = slugify(title or url.rstrip("/").split("/")[-1])
        dims_for_file = normalize_dimensions_for_filename(dimensions)
        filename_base = f"{artwork_slug}-{dims_for_file}" if dims_for_file else artwork_slug
        ext = Path(urlparse(image).path).suffix.lower()
        if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
            ext = ".jpg"
        save_path = Path(IMAGES_ROOT) / item["artist_slug"] / f"{filename_base}{ext}"
        if save_path.exists() and save_path.stat().st_size > 0:
            local_image_path = str(save_path).replace("\\", "/")
        elif download_image(image, save_path):
            local_image_path = str(save_path).replace("\\", "/")
            log.info(f"      Image sauvee -> {local_image_path}")
        else:
            log.warning(f"      Image non telechargee : {image}")

    if not title or not item["artist_name"]:
        log.warning(f"      Oeuvre incomplete ignoree : {url}")
        return "skip", None

    # Structure finale : 9 champs, rien d'autre
    data = {
        "artist_id": item["artist_id"],
        "artist_name": item["artist_name"],
        "artist_slug": item["artist_slug"],
        "artist_category": item["artist_category"],
        "title": title,
        "dimensions": dimensions,
        "image": image,
        "local_image": local_image_path,
        "url": url,
    }
    return "ok", {"id": make_stable_id(url), "data": data}


# ----------------------------------------------------------------------
# 5. SCRAPING COMPLET (passe normale + seconde passe sur les echecs)
# ----------------------------------------------------------------------

def scrape_all() -> tuple[list, list]:
    artists = get_artists()
    if MAX_ARTISTS:
        artists = artists[:MAX_ARTISTS]

    artist_records = []
    artwork_records = []
    seen_artwork_ids = set()
    failed_artists = []
    failed_links = []

    def handle_links(links, retries, base_timeout):
        if MAX_ARTWORKS_PER_ARTIST:
            links = links[:MAX_ARTWORKS_PER_ARTIST]
        for link in links:
            status, detail = scrape_artwork_detail(link, retries, base_timeout)
            if status == "ok":
                if detail["id"] not in seen_artwork_ids:
                    seen_artwork_ids.add(detail["id"])
                    artwork_records.append(detail)
                    d = detail["data"]
                    log.info(f"      OK {d.get('title')} | {d.get('dimensions')}")
            elif status == "fail":
                failed_links.append(link)

    # --- Passe normale ---
    for artist in artists:
        artist_record, artwork_links = process_artist_page(artist)
        if artist_record is None:
            failed_artists.append(artist)
            continue
        artist_records.append(artist_record)
        handle_links(artwork_links, MAX_RETRIES, PAGE_TIMEOUT)

    # --- Seconde passe : artistes rates ---
    if failed_artists:
        log.info(f"=== SECONDE PASSE : {len(failed_artists)} artiste(s) a retenter ===")
        for artist in failed_artists:
            artist_record, artwork_links = process_artist_page(
                artist, SECOND_PASS_RETRIES, SECOND_PASS_TIMEOUT
            )
            if artist_record is None:
                log.error(f"  Artiste definitivement en echec : {artist['name']} ({artist['url']})")
                continue
            artist_records.append(artist_record)
            handle_links(artwork_links, SECOND_PASS_RETRIES, SECOND_PASS_TIMEOUT)

    # --- Seconde passe : oeuvres ratees ---
    if failed_links:
        links_to_retry = list(failed_links)
        failed_links.clear()
        log.info(f"=== SECONDE PASSE : {len(links_to_retry)} oeuvre(s) a retenter ===")
        for link in links_to_retry:
            status, detail = scrape_artwork_detail(link, SECOND_PASS_RETRIES, SECOND_PASS_TIMEOUT)
            if status == "ok" and detail["id"] not in seen_artwork_ids:
                seen_artwork_ids.add(detail["id"])
                artwork_records.append(detail)
                log.info(f"      OK (2e passe) {detail['data'].get('title')}")
            elif status == "fail":
                log.error(f"  Oeuvre definitivement en echec : {link['url']}")

    log.info(f"Total artistes valides : {len(artist_records)}")
    log.info(f"Total oeuvres valides  : {len(artwork_records)}")
    return artist_records, artwork_records


# ----------------------------------------------------------------------
# 6. FICHIERS JSON + HASH INTERNE
# ----------------------------------------------------------------------

def make_content_hash(data: dict, exclude: set = frozenset()) -> str:
    payload = {k: v for k, v in data.items() if k not in exclude}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def load_json(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Lecture impossible de {path} : {e}")
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


def diff_fields(old_data: dict, new_data: dict, exclude: set = frozenset({"local_image"})) -> dict:
    changes = {}
    for key in new_data:
        if key in exclude:
            continue
        if old_data.get(key) != new_data.get(key):
            changes[key] = {"avant": old_data.get(key), "apres": new_data.get(key)}
    return changes


def merge_catalogue(catalogue: dict, items: list, label: str,
                    hashes: dict, exclude: set) -> dict:
    """
    Detection NOUVEAU / CHANGE / ORPHELIN.
    Les hashs sont stockes dans `hashes` (fichier interne), JAMAIS dans
    le catalogue. Les orphelins sont conserves tels quels dans le JSON
    (simplement signales dans le log).
    """
    seen_ids = set()

    for item in items:
        pid = item["id"]
        data = item["data"]
        seen_ids.add(pid)

        if pid not in catalogue:
            catalogue[pid] = data
            hashes[pid] = make_content_hash(data, exclude)
            log.info(f"[NOUVEAU:{label}] {data.get('name') or data.get('title')}")
            continue

        old = catalogue[pid]

        # Ne jamais ecraser une valeur deja connue par du vide
        if label == "artiste" and not data.get("bio") and old.get("bio"):
            data["bio"] = old["bio"]
        if label == "oeuvre" and not data.get("local_image") and old.get("local_image"):
            data["local_image"] = old["local_image"]

        new_hash = make_content_hash(data, exclude)
        old_hash = hashes.get(pid) or make_content_hash(old, exclude)

        if old_hash != new_hash:
            changes = diff_fields(old, data, exclude)
            log.info(f"[CHANGE:{label}]  {data.get('name') or data.get('title')} -> {changes}")
        catalogue[pid] = data
        hashes[pid] = new_hash

    for pid, entry in catalogue.items():
        if pid not in seen_ids:
            log.info(f"[ORPHELIN:{label}] {entry.get('name') or entry.get('title')} (conserve)")

    return catalogue


def ratio_ok(catalogue: dict, nb_new: int) -> bool:
    nb_avant = len(catalogue)
    if nb_avant == 0:
        return True
    return (nb_new / nb_avant) >= MIN_RATIO_SECURITE


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

    all_hashes = {} if FRESH_START else load_json(HASH_FILE)
    hashes_artistes = all_hashes.get("artistes", {})
    hashes_oeuvres = all_hashes.get("oeuvres", {})

    # --- Artistes ---
    artistes_catalogue = {} if FRESH_START else load_json(ARTISTS_FILE)
    if artist_records:
        if not ratio_ok(artistes_catalogue, len(artist_records)):
            log.error("Scraping ARTISTES suspect (trop peu de resultats). artistes.json NON modifie.")
        else:
            backup_file(ARTISTS_FILE, "artistes")
            artistes_catalogue = merge_catalogue(
                artistes_catalogue, artist_records, "artiste", hashes_artistes, set()
            )
            save_json(ARTISTS_FILE, artistes_catalogue)

    # --- Oeuvres ---
    oeuvres_catalogue = {} if FRESH_START else load_json(CATALOGUE_FILE)
    if artwork_records:
        if not ratio_ok(oeuvres_catalogue, len(artwork_records)):
            log.error("Scraping OEUVRES suspect (trop peu de resultats). catalogue.json NON modifie.")
        else:
            backup_file(CATALOGUE_FILE, "catalogue")
            oeuvres_catalogue = merge_catalogue(
                oeuvres_catalogue, artwork_records, "oeuvre", hashes_oeuvres, {"local_image"}
            )
            save_json(CATALOGUE_FILE, oeuvres_catalogue)

    save_json(HASH_FILE, {"artistes": hashes_artistes, "oeuvres": hashes_oeuvres})

    # --- Resume ---
    avec_bio = sum(1 for e in artistes_catalogue.values() if e.get("bio"))
    avec_image = sum(1 for e in oeuvres_catalogue.values() if e.get("local_image"))

    log.info("===== RESUME =====")
    log.info(f"[Artistes]  total={len(artistes_catalogue)} avec_bio={avec_bio} -> {ARTISTS_FILE}")
    log.info(f"[Oeuvres]   total={len(oeuvres_catalogue)} avec_image={avec_image} -> {CATALOGUE_FILE}")
    log.info(f"Hash internes -> {HASH_FILE}")
    log.info(f"Dossier images : {IMAGES_ROOT}/")
    log.info("Termine.")


if __name__ == "__main__":
    main()