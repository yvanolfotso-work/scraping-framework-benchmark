# -*- coding: utf-8 -*-
"""
config.py — TOUS les reglages du flow d'augmentation (A1 -> A3).

Un seul endroit a modifier pour piloter le comportement du pipeline.
Rien n'est code en dur ailleurs : chaque module lit ses parametres ici.

Les valeurs sensibles (cles d'API) ne sont PAS ici : elles viennent
de l'environnement / du fichier .env (voir .env.example).
"""

import os
from pathlib import Path

# ----------------------------------------------------------------------
# CHEMINS
# ----------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent      # dossier data_augmentation/
RACINE_PROJET = BASE_DIR.parent                  # dossier data_augmentation_projet/

_scraping_dir_brut = Path(os.getenv("SCRAPING_DIR", "input_base"))
SCRAPING_DIR = (
    _scraping_dir_brut if _scraping_dir_brut.is_absolute()
    else RACINE_PROJET / _scraping_dir_brut
).resolve()

ARTISTES_JSON = SCRAPING_DIR / "artistes.json"
CATALOGUE_JSON = SCRAPING_DIR / "catalogue.json"

OUT_DIR = BASE_DIR / "out"
RAW_DIR = OUT_DIR / "raw"

A1_OUT = OUT_DIR / "motscles_candidats.json"
A2_OUT = OUT_DIR / "requetes.json"
A3_OUT = OUT_DIR / "resultats_bruts.json"

MANIFEST = OUT_DIR / "manifest.json"
LOG_FILE = OUT_DIR / "augmentation.log"


# ----------------------------------------------------------------------
# A1 — EXTRACTION DES MOTS-CLES CANDIDATS
# ----------------------------------------------------------------------

A1_CHAMPS_STRUCTURES = [
    ("artistes", "name", "artiste"),
    ("artistes", "category", "categorie"),
]

A1_CHAMPS_TEXTE = [
    ("artistes", "bio"),
]
A1_CHAMP_URL_CATALOGUE = "url"

# Segments de chemin marquant le debut d'un bloc /<marqueur>/<artiste>/<oeuvre>/.
A1_URL_MARQUEURS_ARTISTE = ("artistes", "artists")

# Motif de detection d'une dimension "AxB cm" dans le slug, tolerant aux
# tirets/underscores autour des chiffres, du x et de "cm"
# (ex: "150x150cm", "120-x-120-cm").
A1_REGEX_DIMENSION = r"(\d{1,4})\s*[-_]?\s*[xX]\s*[-_]?\s*(\d{1,4})\s*[-_]?\s*cm"

# Taille des n-grammes extraits du texte libre (1 = mots seuls).
A1_NGRAM_MIN = 1
A1_NGRAM_MAX = 3

# Un terme issu du texte libre doit apparaitre au moins N fois
# dans le corpus pour etre retenu.
A1_FREQ_MIN = 3

# Longueur minimale d'un token (en caracteres) pour etre considere.
A1_LONGUEUR_MIN_TOKEN = 3

# Nombre max de termes retenus par type (None = pas de limite).
A1_TOP_N_PAR_TYPE = None

# Les termes structures (artiste / categorie / titre_oeuvre / dimension)
# sont toujours gardes, meme vus une seule fois.
A1_GARDER_STRUCTURES_FREQ_1 = True

# Mots vides francais + bruit specifique au domaine galerie.
A1_STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "du", "de", "d", "au", "aux",
    "et", "ou", "mais", "donc", "or", "ni", "car", "que", "qui", "quoi",
    "dont", "ce", "cet", "cette", "ces", "son", "sa", "ses", "leur",
    "leurs", "mon", "ma", "mes", "ton", "ta", "tes", "notre", "nos",
    "votre", "vos", "il", "elle", "ils", "elles", "on", "nous", "vous",
    "je", "tu", "se", "sa", "lui", "eux", "y", "en", "par", "pour",
    "avec", "sans", "sur", "sous", "dans", "chez", "vers", "entre",
    "est", "sont", "etait", "etaient", "ete", "etre", "avoir", "a",
    "ont", "avait", "avaient", "fait", "faire", "plus", "moins", "tres",
    "aussi", "comme", "ainsi", "alors", "puis", "apres", "avant",
    "depuis", "pendant", "lors", "meme", "tout", "tous", "toute",
    "toutes", "autre", "autres", "cela", "ceci", "celui", "celle",
    "ses", "si", "ne", "pas", "plus", "jamais", "toujours", "deja",
    "encore", "bien", "peu", "beaucoup", "quelque", "quelques",
    "son", "leurs", "dont", "sera", "seront", "peut", "peuvent",
    "cookie", "cookies", "newsletter", "whatsapp", "contact",
    "contactez", "galerie", "galeries", "info", "infos", "site",
    "page", "web", "email", "mail", "tel", "telephone",
}

# Termes explicitement bannis (liste noire manuelle, edite-la librement).
A1_BLACKLIST = set()

# Nombre max d'occurrences detaillees conservees par terme (tracabilite).
A1_MAX_OCCURRENCES_TRACEES = 10


# ----------------------------------------------------------------------
# A2 — FORMULATION DES REQUETES
# ----------------------------------------------------------------------

A2_MODE = "template"

A2_TEMPLATES = {
    "artiste": [
        "{terme} artiste biographie",
        "{terme} peintre sculpteur oeuvres",
        "{terme} exposition galerie",
    ],
    "categorie": [
        "{terme} definition art contemporain",
    ],
    "titre_oeuvre": [
        "{terme} oeuvre art",
    ],
    "dimension": [
        "{terme} format oeuvre art",
    ],
    "technique": [
        "{terme} technique artistique definition",
        "{terme} materiau art",
    ],
    "concept": [
        "{terme} art definition",
        "{terme} mouvement artistique",
    ],
}

A2_TEMPLATE_DEFAUT = ["{terme} art"]

A2_MAX_REQUETES = None

A2_TYPES_INCLUS = {"artiste", "categorie", "technique", "concept"}


# ----------------------------------------------------------------------
# A3 — RECHERCHE EXTERNE
# ----------------------------------------------------------------------

# A3_PROVIDERS = ["wikipedia", "google_cse"]
A3_PROVIDERS = ["wikipedia"]
A3_ECHECS_FATALS_MAX = 3
A3_MAX_RESULTATS_PAR_REQUETE = 5
A3_LANG = "fr"
A3_DELAI_ENTRE_REQUETES = 1.5
A3_TIMEOUT = 20
A3_MAX_RETRIES = 3
A3_BACKOFF = 2.0
A3_SAUVER_BRUT = True

WIKIPEDIA_API = "https://{lang}.wikipedia.org/w/api.php"
WIKIPEDIA_UA = "AugmentationCorpus/1.0 (recherche interne; contact@exemple.com)"

GOOGLE_CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "")


# ----------------------------------------------------------------------
# DIVERS
# ----------------------------------------------------------------------

LIMITE_TEST = None

JSON_INDENT = 2
JSON_ENSURE_ASCII = False