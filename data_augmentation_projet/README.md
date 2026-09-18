# Flow d'augmentation du corpus - étapes A1 → A3

Projet **séparé** du scraper. Il ne modifie jamais `artistes.json` ni
`catalogue.json` : il les lit seulement, et écrit tout dans `out/`.

Objectif : partir de la source unique (le corpus scrapé Galeries Bartoux)
et aller chercher de la donnée complémentaire sur le net, **avant** de
lancer le pipeline P0–P9 de construction du référentiel.

```
artistes.json + catalogue.json          (produits par le scraper)
        │
        ▼
   A1  extraction des mots-clés candidats      → out/motscles_candidats.json
        │
        ▼
   A2  formulation des requêtes (déterministe) → out/requetes.json
        │
        ▼
   A3  recherche externe Wikipédia + Google    → out/resultats_bruts.json
        │                                        + out/raw/*.json (preuves)
        ▼
   VÉRIF  contrôle d'intégrité (C1 → C6)
```

---

## Installation

```bash
cd data_augmentation_projet
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # puis éditer .env
```

Dans `.env`, `SCRAPING_DIR` doit pointer sur le dossier qui contient
`artistes.json` et `catalogue.json`.

---

## Utilisation

```bash
# 1. Extraire les mots-clés candidats
python run.py a1

# 2. Générer les requêtes
python run.py a2

# 3. TEST RÉEL sur 3 requêtes, Wikipédia seulement (pas de clé nécessaire)
python run.py a3 --limite 3 --providers wikipedia

# 4. Contrôler que rien n'a été inventé
python run.py verif

# Tout d'un coup, en mode test
python run.py all --limite 5

# Contrôler la reproductibilité de A1
python run.py a1 --verifier-determinisme
```

Commence toujours par `--limite` et `--providers wikipedia` : Wikipédia
ne demande aucune clé, donc tu peux valider le flow de bout en bout avant
de configurer Google.

---

## Les trois étapes

### A1 — extraction des mots-clés candidats

Deux familles de termes, extraites des deux fichiers du scraper :

| Famille | Provenance | Filtrage |
|---|---|---|
| **structurés** | `name`, `category`, `title`, `medium` | gardés même vus une seule fois |
| **texte libre** | `bio`, `description` | n-grammes 1→3, mots vides retirés, fréquence ≥ `A1_FREQ_MIN` |

Un terme vu dans un champ structuré reste structuré même s'il apparaît
aussi dans une description — sinon un titre d'œuvre cité dans un texte
basculait en « concept » et se faisait éliminer par le seuil de fréquence.

Chaque terme sort avec son type, sa fréquence, ses variantes d'écriture,
et la **liste des enregistrements et champs d'où il vient**.

### A2 — formulation des requêtes

**Aucun LLM.** Une requête = un gabarit (`A2_TEMPLATES`) + un terme de A1.
Les gabarits sont choisis selon le type du terme, donc on ne pose pas une
question de biographie à un nom de technique.

Chaque requête garde le `terme_id` et le `template` exact utilisés, donc
elle est reconstructible — et impossible à faire porter sur un sujet
absent du corpus.

### A3 — recherche externe

**Aucun LLM non plus.** Deux fournisseurs, tous deux via API officielle
(pas de scraping des pages de résultats) :

- **Wikipédia** — API MediaWiki publique, sans clé.
- **Google** — Programmable Search JSON API, clé + CX requis. Sans clé,
  le fournisseur est simplement ignoré.

Pour chaque appel on conserve : URL appelée (clé masquée), code HTTP,
horodatage UTC, `sha256` du corps de la réponse, et le **corps brut
sauvegardé** dans `out/raw/`.

---

## Comment on prouve que rien n'est inventé

C'est l'objet de `python run.py verif`. Six contrôles :

| Code | Contrôle |
|---|---|
| **C1** | Le chaînage des empreintes tient : A2 cite bien la version actuelle de A1, A3 celle de A2. Modifier un fichier à la main casse la chaîne. |
| **C2** | Chaque terme de A1 est re-cherché dans le corpus scrapé. Terme introuvable = terme inventé = échec. |
| **C3** | Chaque requête de A2 est reconstruite depuis `gabarit + terme`. Non reconstructible = texte libre injecté = échec. |
| **C4** | Chaque résultat porte un HTTP 200, et le `sha256` du fichier brut est **recalculé** et comparé. |
| **C5** | Chaque URL/titre restitué est retrouvé **dans la réponse brute du serveur**. |
| **C6** | L'étape de collecte déclare `llm_utilise: false`. |

La commande sort avec le code **0** si tout est conforme, **1** sinon —
donc utilisable telle quelle dans un contrôle automatisé.

Ces contrôles ont été testés en négatif : injecter un résultat fabriqué
fait tomber C5, altérer un fichier brut fait tomber C4.

**À savoir :** C4/C5 prouvent que le contenu vient bien d'une réponse
serveur et qu'il n'a pas été altéré depuis. Ils ne jugent pas la
*qualité* ni la *pertinence* du résultat — c'est le rôle de l'étape A4
(filtrage), non incluse ici.

---

## Réglages

Tout est dans `config.py`, rien n'est codé en dur ailleurs. Les
paramètres qui bougent le plus :

| Paramètre | Effet |
|---|---|
| `A1_FREQ_MIN` | seuil de fréquence du texte libre. Plus haut = moins de bruit, moins de concepts. |
| `A1_NGRAM_MAX` | longueur max des expressions extraites (3 = « peinture à l'huile »). |
| `A1_STOPWORDS` | mots vides. À enrichir dès que du bruit apparaît dans la sortie. |
| `A2_TEMPLATES` | les gabarits de requêtes, par type de terme. |
| `A2_TYPES_INCLUS` | quels types de termes déclenchent une recherche. |
| `A3_MAX_RESULTATS_PAR_REQUETE` | volume ramené par requête. |
| `A3_DELAI_ENTRE_REQUETES` | politesse réseau. Ne pas descendre trop bas sur Google. |

Les paramètres effectifs de chaque run sont recopiés dans le `meta` du
fichier de sortie et dans `out/manifest.json` : on sait toujours avec
quels réglages un fichier a été produit.

---

## Ce qui n'est pas dans ce projet

Volontairement, d'après le cadrage actuel :

- **A4 filtrage de pertinence** — à faire, sur la base des résultats A3.
- **A5/A6 normalisation et fusion** — à faire une fois A4 stabilisé.
- **Déduplication** — reportée : traitée plus tard par distance sémantique.
- **Rafraîchissement périodique / historique** — non requis pour l'instant.

---

## Arborescence

```
data_augmentation_projet/
├── run.py                  ← point d'entrée CLI
├── requirements.txt
├── .env.example
├── .gitignore
├── README.md
└── data_augmentation/
    ├── config.py           ← TOUS les réglages
    ├── common.py           ← IO déterministe, hash, normalisation
    ├── a1_extraction.py
    ├── a2_requetes.py
    ├── a3_recherche.py
    ├── verifier.py         ← contrôles C1 → C6
    └── out/                ← sorties (ignoré par git)
        ├── motscles_candidats.json
        ├── requetes.json
        ├── resultats_bruts.json
        ├── manifest.json
        ├── augmentation.log
        └── raw/            ← réponses HTTP brutes (preuves)
```
