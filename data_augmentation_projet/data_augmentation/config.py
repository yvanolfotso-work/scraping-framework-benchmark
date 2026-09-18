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

# Racine du projet d'augmentation (dossier qui contient ce fichier)
BASE_DIR = Path(__file__).resolve().parent

# Dossier ou se trouvent les JSON produits par le scraper Bartoux.
# Par defaut : le dossier parent (ton "Test Scraping"). Surchargeable
# par la variable d'environnement SCRAPING_DIR.
SCRAPING_DIR = Path(
    os.getenv("SCRAPING_DIR", BASE_DIR.parent)
).resolve()

ARTISTES_JSON = SCRAPING_DIR / "artistes.json"
CATALOGUE_JSON = SCRAPING_DIR / "catalogue.json"

# Sorties du flow d'augmentation
OUT_DIR = BASE_DIR / "out"
RAW_DIR = OUT_DIR / "raw"            # copies brutes des reponses HTTP (preuve)

A1_OUT = OUT_DIR / "motscles_candidats.json"
A2_OUT = OUT_DIR / "requetes.json"
A3_OUT = OUT_DIR / "resultats_bruts.json"

MANIFEST = OUT_DIR / "manifest.json"  # empreintes + parametres de chaque run
LOG_FILE = OUT_DIR / "augmentation.log"


# ----------------------------------------------------------------------
# A1 — EXTRACTION DES MOTS-CLES CANDIDATS
# ----------------------------------------------------------------------

# Champs STRUCTURES exploites (valeur = terme candidat tel quel).
# Format : (fichier, chemin_du_champ, type_de_terme)
A1_CHAMPS_STRUCTURES = [
    ("artistes", "name", "artiste"),
    ("artistes", "category", "categorie"),
    ("catalogue", "artist_name", "artiste"),
    ("catalogue", "artist_category", "categorie"),
    ("catalogue", "title", "titre_oeuvre"),
    ("catalogue", "medium", "technique"),
]

# Champs TEXTE LIBRE d'ou l'on extrait des n-grammes.
A1_CHAMPS_TEXTE = [
    ("artistes", "bio"),
    ("catalogue", "description"),
]

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

# Les termes structures sont toujours gardes, meme vus une seule fois.
A1_GARDER_STRUCTURES_FREQ_1 = True

# Mots vides francais + bruit specifique au domaine galerie.
A1_STOPWORDS = {
    # articles / prepositions / conjonctions
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
    # bruit site / navigation
    "cookie", "cookies", "newsletter", "whatsapp", "contact",
    "contactez", "galerie", "galeries", "info", "infos", "site",
    "page", "web", "email", "mail", "tel", "telephone",
}

# Termes explicitement bannis (liste noire manuelle, edite-la librement).
A1_BLACKLIST = set()

# Nombre max d'occurrences detaillees conservees par terme (tracabilite).
# Le compteur total reste exact ; seule la liste detaillee est tronquee
# pour eviter un fichier de sortie gigantesque.
A1_MAX_OCCURRENCES_TRACEES = 10


# ----------------------------------------------------------------------
# A2 — FORMULATION DES REQUETES
# ----------------------------------------------------------------------

# Mode de generation :
#   "template" -> 100% deterministe, aucun LLM (recommande par defaut)
#   "llm"      -> reserve pour plus tard, non active ici
A2_MODE = "template"

# Gabarits de requetes. {terme} est remplace par le mot-cle candidat.
# Les gabarits sont appliques selon le TYPE du terme, ce qui evite de
# poser une question d'artiste a un terme de technique.
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
    "technique": [
        "{terme} technique artistique definition",
        "{terme} materiau art",
    ],
    "concept": [
        "{terme} art definition",
        "{terme} mouvement artistique",
    ],
}

# Gabarits utilises si le type du terme n'a pas d'entree ci-dessus.
A2_TEMPLATE_DEFAUT = ["{terme} art"]

# Nombre max de requetes generees au total (None = pas de limite).
A2_MAX_REQUETES = None

# Types de termes a inclure dans la generation de requetes.
A2_TYPES_INCLUS = {"artiste", "categorie", "technique", "concept"}


# ----------------------------------------------------------------------
# A3 — RECHERCHE EXTERNE
# ----------------------------------------------------------------------

# Fournisseurs actives, dans l'ordre d'execution.
# Valeurs possibles : "wikipedia", "google_cse"
A3_PROVIDERS = ["wikipedia", "google_cse"]

# Nombre de resultats demandes par requete et par fournisseur.
A3_MAX_RESULTATS_PAR_REQUETE = 5

# Langue de recherche.
A3_LANG = "fr"

# Politesse reseau
A3_DELAI_ENTRE_REQUETES = 1.5     # secondes
A3_TIMEOUT = 20                   # secondes
A3_MAX_RETRIES = 3
A3_BACKOFF = 2.0                  # facteur multiplicatif entre 2 essais

# Conserver la reponse HTTP brute sur disque (preuve d'origine).
A3_SAUVER_BRUT = True

# Wikipedia
WIKIPEDIA_API = "https://{lang}.wikipedia.org/w/api.php"
WIKIPEDIA_UA = "AugmentationCorpus/1.0 (recherche interne; contact@exemple.com)"

# Google Programmable Search (API officielle, pas de scraping de Google)
GOOGLE_CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "")


# ----------------------------------------------------------------------
# DIVERS
# ----------------------------------------------------------------------

# Mode test : limite le nombre de termes traites de bout en bout.
# Surchargeable en ligne de commande (--limite).
LIMITE_TEST = None

# Encodage / determinisme
JSON_INDENT = 2
JSON_ENSURE_ASCII = False
