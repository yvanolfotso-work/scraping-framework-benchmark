# --- New PATCH pour contourner le bug browserforge Windows ---
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
import time
import hashlib
import os
import shutil
import logging
from datetime import datetime, timezone


# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

URL = "https://retail-pap.vercel.app/"
BASE_URL = "https://retail-pap.vercel.app"

CATALOGUE_FILE = "catalogue.json"
BACKUP_DIR = "backups"
LOG_FILE = "scraping.log"

# Si le nouveau scraping ramène moins de X% du nombre de produits habituel,
# on considère que le scraping a probablement échoué (page pas chargée,
# bug JS, blocage anti-bot...) et on n'écrase PAS le catalogue existant.
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
# 1. SCRAPING
# ----------------------------------------------------------------------

def scrape_products() -> list:
    log.info(f"Ouverture navigateur : {URL}")

    try:
        page = StealthyFetcher.fetch(URL, headless=True, network_idle=True)
    except Exception as e:
        log.error(f"Échec du chargement de la page : {e}")
        return []

    time.sleep(3)

    products = page.css("article")
    log.info(f"Nombre articles trouvés (bruts) : {len(products)}")

    results = []

    for index, product in enumerate(products):
        text = product.get_all_text()
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        name = product.css("h3::text").get()
        if not name and len(lines) > 0:
            name = lines[-1]

        # --- Prix : brut ET normalisé ---
        price_raw = None
        price_value = None
        currency = None

        prices = re.findall(r"\d[\d\s ]*€", text)
        if prices:
            price_raw = prices[0].strip()
            digits = re.sub(r"[^\d]", "", price_raw)
            if digits:
                price_value = float(digits)
                currency = "EUR"

        # --- Image ---
        image = product.css("img::attr(src)").get()
        if image and image.startswith("/"):
            image = BASE_URL + image

        # --- Badge ---
        text_lower = text.lower()
        badge = None
        if "réduction" in text_lower:
            badge = "RÉDUCTION"
        elif "nouveauté" in text_lower:
            badge = "NOUVEAUTÉ"

        # --- Description ---
        description = None
        for line in reversed(lines):
            if any(mot in line.lower() for mot in
                   ["fit", "laine", "lin", "coton", "velours"]):
                description = line
                break

        # --- Identifiant "naturel" du site, si présent dans le DOM ---
        # (data-id / data-sku / lien produit) -> sert de fallback prioritaire
        natural_id = (
            product.css("::attr(data-id)").get()
            or product.css("::attr(data-sku)").get()
            or product.css("a::attr(href)").get()
        )

        product_data = {
            "name": name,
            "price_raw": price_raw,
            "price_value": price_value,
            "currency": currency,
            "description": description,
            "image": image,
            "badge": badge,
        }

        # Texte consolidé, utile pour un futur RAG / recherche sémantique
        searchable_parts = [
            name or "",
            description or "",
            price_raw or "",
            badge or "",
        ]
        product_data["searchable_text"] = " - ".join(
            p for p in searchable_parts if p
        )

        # Validation basique : un produit sans nom est probablement une
        # erreur d'extraction -> on le garde de côté mais on le signale
        is_valid = bool(name) and bool(price_raw)
        if not is_valid:
            log.warning(f"Produit #{index} incomplet, ignoré : {product_data}")
            continue

        results.append({
            "natural_id": natural_id,
            "position": index,
            "data": product_data
        })

    log.info(f"Produits valides extraits : {len(results)}")
    return results


# ----------------------------------------------------------------------
# 2. IDENTIFIANT ET HASH
# ----------------------------------------------------------------------

def make_id(item: dict) -> str:
    """
    Identifiant stable, avec fallback en cascade :
    1. Un identifiant naturel trouvé sur le site (data-id, data-sku, lien)
    2. À défaut, le nom du produit
    """
    natural_id = item.get("natural_id")
    if natural_id:
        base = f"natural:{natural_id}"
    else:
        base = f"name:{item['data'].get('name') or ''}"

    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


def make_content_hash(data: dict) -> str:
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------
# 3. CATALOGUE : chargement, sauvegarde, backup
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
    log.info(f"Backup créé : {backup_path}")


def save_catalogue(catalogue: dict):
    # Écriture atomique : on écrit dans un fichier temporaire puis on
    # remplace l'ancien, pour éviter un fichier corrompu si le script
    # plante en pleine écriture.
    tmp_path = CATALOGUE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(catalogue, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, CATALOGUE_FILE)


# ----------------------------------------------------------------------
# 4. FUSION avec historique des changements
# ----------------------------------------------------------------------

def diff_fields(old_data: dict, new_data: dict) -> dict:
    """Retourne uniquement les champs qui ont changé, avant/après."""
    changes = {}
    for key in new_data:
        if key == "searchable_text":
            continue  # champ dérivé, pas pertinent pour l'historique
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

                log.info(f"[CHANGÉ]    {data.get('name')} -> {changes}")

    # Produits absents de ce scraping -> orphelins (jamais supprimés)
    for pid, entry in catalogue.items():
        if pid not in seen_ids and entry["status"] != "orphan":
            entry["status"] = "orphan"
            entry["last_checked"] = timestamp
            log.info(f"[ORPHELIN]  {entry['data'].get('name')}")

    return catalogue


# ----------------------------------------------------------------------
# 5. MAIN
# ----------------------------------------------------------------------

def main():
    scraped = scrape_products()

    if not scraped:
        log.error("Aucun produit valide extrait. Arrêt sans toucher au catalogue.")
        return

    catalogue = load_catalogue()

    # --- Garde-fou anti-écrasement ---
    if catalogue:
        nb_actifs_avant = sum(
            1 for e in catalogue.values() if e["status"] == "active"
        )
        if nb_actifs_avant > 0:
            ratio = len(scraped) / nb_actifs_avant
            if ratio < MIN_RATIO_SECURITE:
                log.error(
                    f"Scraping suspect : {len(scraped)} produits trouvés "
                    f"contre {nb_actifs_avant} attendus (ratio {ratio:.0%}). "
                    f"Le catalogue existant n'est PAS modifié. "
                    f"Vérifie le site ou le script avant de relancer."
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

    log.info("===== RÉSUMÉ =====")
    log.info(f"Total catalogue : {total}")
    log.info(f"Actifs          : {active}")
    log.info(f"Orphelins       : {orphans}")
    log.info(f"Modifiés (ce run) : {changed}")
    log.info(f"Fichier          : {CATALOGUE_FILE}")


if __name__ == "__main__":
    main()