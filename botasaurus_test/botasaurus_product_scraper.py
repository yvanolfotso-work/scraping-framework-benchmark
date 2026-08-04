from botasaurus.browser import browser, Driver
import json


@browser()
def scrape_products(driver: Driver, data):

    url = "https://retail-pap.vercel.app/"

    print("Ouverture du site :", url)

    driver.get(url)

    # Attente du rendu React
    driver.sleep(5)

    print("Titre :", driver.title)


    products = driver.run_js("""
    return Array.from(document.querySelectorAll('article'))
    .map(article => {

        const image = article.querySelector('img');

        const title = article.querySelector('h3');

        const paragraphs = Array.from(
            article.querySelectorAll('p')
        )
        .map(p => p.innerText.trim())
        .filter(Boolean);


        const badgeElement = article.querySelector(
            'span'
        );

        let badge = null;

        if (badgeElement) {
            let value = badgeElement.innerText.trim();

            if (
                value.includes('RÉDUCTION') ||
                value.includes('NOUVEAUTÉ')
            ) {
                badge = value;
            }
        }


        return {

            name: title 
                ? title.innerText.trim()
                : null,

            price: paragraphs.find(
                p => p.includes('€')
            ) || null,


            description: paragraphs.find(
                p => !p.includes('€')
            ) || null,


            image: image
                ? image.src
                : null,


            badge: badge

        };

    })
    .filter(product => product.name !== null);

    """)


    # Ajout catégorie automatiquement
    categories = {
        "Costume": "Costumes",
        "Veste": "Vestes & Blazers",
        "Blazer": "Vestes & Blazers",
        "Pantalon": "Pantalons",
        "Chemise": "Chemises",
        "Smoking": "Costumes"
    }


    final_products = []


    for product in products:

        name = product["name"]

        category = "Autre"


        for keyword, cat in categories.items():

            if keyword.lower() in name.lower():
                category = cat
                break


        final_products.append({

            "name": product["name"],

            "price": product["price"],

            "category": category,

            "description": product["description"],

            "image": product["image"],

            "badge": product["badge"]

        })


    print("\n===== CATALOGUE PRODUITS =====\n")


    for product in final_products:

        print(product)


    with open(
        "products_catalog.json",
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            final_products,
            file,
            ensure_ascii=False,
            indent=4
        )


    print(
        "\nCatalogue créé : products_catalog.json"
    )


    return final_products



if __name__ == "__main__":

    scrape_products()