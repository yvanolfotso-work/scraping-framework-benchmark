# --- PATCH pour contourner le bug browserforge Windows ---
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


URL = "https://retail-pap.vercel.app/"

BASE_URL = "https://retail-pap.vercel.app"


print("Ouverture navigateur :", URL)


# Chargement navigateur réel
page = StealthyFetcher.fetch(
    URL,
    headless=True,
    network_idle=True
)


# petite sécurité pour laisser finir le rendu JS
time.sleep(3)


print("\nTitre :")
print(page.css("title::text").get())


# récupération des cartes produits
products = page.css("article")


print("\nNombre articles trouvés :", len(products))


results = []


for product in products:

    text = product.get_all_text()


    # nettoyage lignes
    lines = [
        line.strip()
        for line in text.split("\n")
        if line.strip()
    ]


    # Nom produit
    name = product.css("h3::text").get()


    if not name and len(lines) > 0:
        name = lines[-1]


    # Extraction prix
    prices = re.findall(
        r"\d[\d\s ]*€",
        text
    )


    price = None

    if prices:
        price = prices[0].replace(" ", " ").strip()


    # Image
    image = product.css(
        "img::attr(src)"
    ).get()


    if image:

        if image.startswith("/"):
            image = BASE_URL + image


    # Badge
    badge = None

    text_lower = text.lower()


    if "réduction" in text_lower:
        badge = "RÉDUCTION"

    elif "nouveauté" in text_lower:
        badge = "NOUVEAUTÉ"


    # Description
    description = None


    for line in reversed(lines):

        if (
            "fit" in line.lower()
            or "laine" in line.lower()
            or "lin" in line.lower()
            or "coton" in line.lower()
            or "velours" in line.lower()
        ):
            description = line
            break


    results.append(
        {
            "name": name,
            "price": price,
            "description": description,
            "image": image,
            "badge": badge
        }
    )



print("\n===== PRODUITS EXTRAITS =====")


for product in results[:5]:
    print(product)



# sauvegarde JSON
output_file = "scrapling_browser_products.json"


with open(
    output_file,
    "w",
    encoding="utf-8"
) as f:

    json.dump(
        results,
        f,
        ensure_ascii=False,
        indent=4
    )


print(
    f"\nFichier créé : {output_file}"
)
print(
    f"Total produits sauvegardés : {len(results)}"
)