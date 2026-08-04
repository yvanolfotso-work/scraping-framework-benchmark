from scrapling import Fetcher


url = "https://retail-pap.vercel.app/"


print("Ouverture :", url)


page = Fetcher.get(url)


print("Titre :")
print(page.css("title::text").get())


print("\nNombre images :")
print(len(page.css("img")))


print("\nQuelques textes :")

for text in page.css("h3::text").getall()[:10]:
    print(text)