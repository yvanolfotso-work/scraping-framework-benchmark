import scrapy


class RetailSpider(scrapy.Spider):
    name = "retail"

    start_urls = [
        "https://retail-pap.vercel.app/"
    ]

    def parse(self, response):

        yield {

            # Informations générales
            "url": response.url,
            "title": response.css("title::text").get(),

            # Description SEO
            "meta_description": response.css(
                'meta[name="description"]::attr(content)'
            ).get(),

            "keywords": response.css(
                'meta[name="keywords"]::attr(content)'
            ).get(),

            # Structure de la page
            "h1": response.css("h1::text").getall(),
            "h2": response.css("h2::text").getall(),
            "h3": response.css("h3::text").getall(),

            # Texte complet visible
            "full_text": " ".join(
                text.strip()
                for text in response.css("body ::text").getall()
                if text.strip()
            ),

            # Liens internes et externes
            "links": [
                {
                    "text": link.css("::text").get(),
                    "url": link.attrib.get("href")
                }
                for link in response.css("a")
            ],

            # Images
            "images": [
                {
                    "alt": img.attrib.get("alt"),
                    "src": img.attrib.get("src")
                }
                for img in response.css("img")
            ],

            # Informations HTML
            "all_classes": list(set(
                response.css("*::attr(class)").getall()
            )),

            # Scripts présents
            "scripts": response.css(
                "script::attr(src)"
            ).getall(),

            # Styles CSS
            "stylesheets": response.css(
                'link[rel="stylesheet"]::attr(href)'
            ).getall(),

            # Données structurées JSON-LD (souvent importantes)
            "structured_data": response.css(
                'script[type="application/ld+json"]::text'
            ).getall(),

        }