#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrap_real_fragrances.py
------------------------------------------------------------------
Scraper robuste pour the-real-fragrances.vercel.app, sur le meme
principe que scrap_catalogue.py (retail-pap) :

  - Scrapling DynamicFetcher (rendu JS)
  - Hash d'identite (id stable) + hash de contenu (detection de
    changement) -> NOUVEAU / CHANGE / INCHANGE / ORPHELIN
  - Backup automatique du catalogue avant chaque ecriture
  - Garde-fou anti-ecrasement si le scraping ramene trop peu de
    produits par rapport au run precedent

Nouveaute par rapport a scrap_catalogue.py :
  - Telechargement des images en local dans le dossier images/
    (nommees par le slug du produit, ex: images/bleu-de-chanel.jpg)
  - Le champ "image" du catalogue pointe vers le chemin local

Usage :
    python scrap_real_fragrances.py

A adapter avant le premier run :
  - URL : verifie l'URL exacte de la page qui liste les 19 parfums
    (bouton "VOIR TOUT LE CATALOGUE" sur la page d'accueil)
  - Selecteurs CSS : inspecte le HTML reel (clic droit > Inspecter
    sur une carte produit) et ajuste PRODUCT_SELECTOR / les
    selecteurs internes si besoin -> voir section CONFIGURATION
------------------------------------------------------------------
"""

# --- PATCH pour contourner le bug browserforge sur Windows ---
import browserforge.headers.generator as bf_gen


def _fake_generate(self, **kwargs):
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,image/webp,*/*;q=0.8"
        ),
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
import os
import time
import shutil
import hashlib
import logging
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin
import urllib.request


# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

# Domaine de production (stable dans le temps, contrairement aux URL
# de preview qui changent a chaque deploiement type xxxxx.vercel.app)
# ATTENTION : "COLLECTIONS" n'est pas une route serveur, c'est un onglet
# gere en JS (bouton, pas lien) -> on charge la racine et on clique dessus.
URL = "https://the-real-fragrances.vercel.app/"
BASE_URL = "https://the-real-fragrances.vercel.app"

# Selecteur exact confirme sur le HTML reel : chaque carte a un id
# du type id="perfume-card-bleu-de-chanel"
PRODUCT_SELECTOR = "div[id^='perfume-card-']"

CATALOGUE_FILE = "catalogue_fragrances.json"
BACKUP_DIR = "backups"
IMAGES_DIR = "images"
LOG_FILE = "scraping.log"

# Si le nouveau scraping ramene moins de X% du nombre de produits
# habituel, on considere le scraping suspect et on n'ecrase rien.
MIN_RATIO_SECURITE = 0.5  # 50%


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
# UTILITAIRES
# ----------------------------------------------------------------------

def normaliser(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()


def slugify(s: str) -> str:
    s = normaliser(s or "")
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "produit"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# 1. TELECHARGEMENT DES IMAGES
# ----------------------------------------------------------------------

def telecharger_image(image_url: str, slug: str) -> str | None:
    """
    Telecharge l'image du produit dans IMAGES_DIR, nommee par le slug.
    Retourne le chemin local relatif (ex: images/bleu-de-chanel.jpg),
    ou None si l'image n'a pas pu etre recuperee.
    """
    if not image_url:
        return None

    os.makedirs(IMAGES_DIR, exist_ok=True)

    # Determine l'extension a partir de l'URL, .jpg par defaut
    ext_match = re.search(r"\.(jpg|jpeg|png|webp|avif)(?:\?|$)", image_url, re.IGNORECASE)
    ext = ext_match.group(1).lower() if ext_match else "jpg"

    local_path = os.path.join(IMAGES_DIR, f"{slug}.{ext}")

    # Si l'image existe deja localement, pas besoin de retelecharger
    if os.path.exists(local_path):
        return local_path

    try:
        req = urllib.request.Request(
            image_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            data = response.read()
        with open(local_path, "wb") as f:
            f.write(data)
        return local_path
    except Exception as e:
        log.warning(f"Echec telechargement image ({image_url}) : {e}")
        return None


# ----------------------------------------------------------------------
# 2. SCRAPING
# ----------------------------------------------------------------------

def _open_collections_tab(page):
    """
    Action executee dans le navigateur avant extraction : clique sur
    le bouton d'onglet "COLLECTIONS" (ce n'est pas un lien, la SPA
    gere l'affichage en JS sans changer d'URL).
    """
    try:
        page.click("button:has-text('COLLECTIONS')", timeout=8000)
        page.wait_for_timeout(1500)
    except Exception as e:
        log.warning(f"Impossible de cliquer sur l'onglet COLLECTIONS : {e}")
    return page


def scrape_products() -> list:
    log.info(f"Ouverture navigateur : {URL}")

    try:
        page = StealthyFetcher.fetch(
            URL,
            headless=True,
            network_idle=True,
            page_action=_open_collections_tab,
        )
    except Exception as e:
        log.error(f"Echec du chargement de la page : {e}")
        return []

    # SPA (bundle JS unique) : le contenu est injecte apres coup,
    # on laisse un peu plus de marge que sur un site classique.
    time.sleep(5)

    products = page.css(PRODUCT_SELECTOR)
    log.info(f"Nombre articles trouves (bruts) : {len(products)}")

    results = []

    for index, product in enumerate(products):
        # --- Identifiant naturel : deja present dans le DOM lui-meme ---
        raw_id = product.css("::attr(id)").get() or ""
        slug = raw_id.replace("perfume-card-", "") if raw_id else None

        # --- Nom du produit ---
        name = product.css("h3::text").get()
        if not name:
            continue
        if not slug:
            slug = slugify(name)

        # --- Badge genre (Homme / Femme / Mixte) ---
        badge = product.css("span[class*='backdrop-blur-sm']::text").get()

        # --- Famille olfactive : 1er <p> du bloc space-y-2 ---
        famille = product.css("div.space-y-2 > p:nth-of-type(1)::text").get()

        # --- Description : 2eme <p> du meme bloc ---
        description = product.css("div.space-y-2 > p:nth-of-type(2)::text").get()

        # --- Reference "Inspire par ..." ---
        inspire_par = product.css("div.pt-2 span::text").get()

        # --- Prix : span dedie, deja au format "3 500 FCFA" ---
        price_raw = product.css("span.text-sm.font-mono::text").get()
        price_value = None
        currency = None
        if price_raw:
            digits = re.sub(r"[^\d]", "", price_raw)
            if digits:
                price_value = float(digits)
                currency = "XAF"

        # --- Image : absente sur certaines cartes (art CSS a la place) ---
        image_src = product.css("img::attr(src)").get()
        image_url = urljoin(BASE_URL, image_src) if image_src else None

        local_image = telecharger_image(image_url, slug) if image_url else None

        product_data = {
            "name": name,
            "famille_olfactive": famille,
            "inspire_par": inspire_par,
            "description": description,
            "price_raw": price_raw,
            "price_value": price_value,
            "currency": currency,
            "badge_genre": badge,
            "image_url": image_url,
            "image_local": local_image,
        }

        searchable_parts = [
            name or "",
            famille or "",
            description or "",
            inspire_par or "",
            price_raw or "",
        ]
        product_data["searchable_text"] = " - ".join(p for p in searchable_parts if p)

        is_valid = bool(name) and bool(price_raw)
        if not is_valid:
            log.warning(f"Produit #{index} incomplet, ignore : {product_data}")
            continue

        results.append({
            "natural_id": raw_id or None,
            "position": index,
            "data": product_data
        })

    log.info(f"Produits valides extraits : {len(results)}")
    return results


# ----------------------------------------------------------------------
# 3. IDENTIFIANT ET HASH (meme logique que scrap_catalogue.py)
# ----------------------------------------------------------------------

def make_id(item: dict) -> str:
    natural_id = item.get("natural_id")
    if natural_id:
        base = f"natural:{natural_id}"
    else:
        base = f"name:{item['data'].get('name') or ''}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def make_content_hash(data: dict) -> str:
    # On exclut le champ derive (searchable_text) et le chemin local
    # de l'image (qui ne varie pas en soi) du hash de contenu, pour
    # ne detecter que les vrais changements de donnees produit.
    payload_data = {k: v for k, v in data.items() if k not in ("searchable_text", "image_local")}
    payload = json.dumps(payload_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# 4. CATALOGUE : chargement, sauvegarde, backup
# ----------------------------------------------------------------------

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


# ----------------------------------------------------------------------
# 5. FUSION avec historique des changements
# ----------------------------------------------------------------------

def diff_fields(old_data: dict, new_data: dict) -> dict:
    changes = {}
    for key in new_data:
        if key in ("searchable_text",):
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
                "source_url": URL,
                "content_hash": chash,
                "first_seen": timestamp,
                "last_checked": timestamp,
                "last_changed": timestamp,
                "status": "active",
                "data": data,
                "history": []
            }
            log.info(f"[NOUVEAU]   {data.get('name')}")
        else:
            entry = catalogue[pid]
            entry["last_checked"] = timestamp
            entry["source_url"] = URL
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
                log.info(f"[CHANGE]    {data.get('name')} -> {changes}")

    for pid, entry in catalogue.items():
        if pid not in seen_ids and entry["status"] != "orphan":
            entry["status"] = "orphan"
            entry["last_checked"] = timestamp
            log.info(f"[ORPHELIN]  {entry['data'].get('name')}")

    return catalogue


# ----------------------------------------------------------------------
# 6. MAIN
# ----------------------------------------------------------------------

def main():
    scraped = scrape_products()

    if not scraped:
        log.error("Aucun produit valide extrait. Arret sans toucher au catalogue.")
        log.error(f"Verifie l'URL ({URL}) et le selecteur PRODUCT_SELECTOR ({PRODUCT_SELECTOR}).")
        return

    catalogue = load_catalogue()

    if catalogue:
        nb_actifs_avant = sum(1 for e in catalogue.values() if e["status"] == "active")
        if nb_actifs_avant > 0:
            ratio = len(scraped) / nb_actifs_avant
            if ratio < MIN_RATIO_SECURITE:
                log.error(
                    f"Scraping suspect : {len(scraped)} produits trouves "
                    f"contre {nb_actifs_avant} attendus (ratio {ratio:.0%}). "
                    f"Le catalogue existant n'est PAS modifie."
                )
                return

    backup_catalogue()
    catalogue = merge_catalogue(catalogue, scraped)
    save_catalogue(catalogue)

    total = len(catalogue)
    active = sum(1 for e in catalogue.values() if e["status"] == "active")
    orphans = sum(1 for e in catalogue.values() if e["status"] == "orphan")
    changed = sum(
        1 for e in catalogue.values()
        if e.get("history") and e["history"][-1]["date"] == e["last_changed"]
        and e["last_changed"] != e["first_seen"]
    )

    log.info("===== RESUME =====")
    log.info(f"Total catalogue : {total}")
    log.info(f"Actifs          : {active}")
    log.info(f"Orphelins       : {orphans}")
    log.info(f"Modifies (ce run) : {changed}")
    log.info(f"Fichier          : {CATALOGUE_FILE}")
    log.info(f"Images dans      : {IMAGES_DIR}/")


if __name__ == "__main__":
    main()