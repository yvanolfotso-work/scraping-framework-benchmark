#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transform_json.py
------------------------------------------------------------------
Transforme le catalogue scrapé (galeries-bartoux.com) en un JSON
qui respecte le type `Oeuvre` de src/types/index.ts.

⚠️ Chaque œuvre embarque `bio` et `univers`. Le type `Oeuvre`
(src/types/index.ts) doit déclarer ces deux champs
(`bio: string`, `univers: string`).

Bio / univers :
  - Artiste dans BIO_ARTISTES / UNIVERS_ARTISTES → textes éditoriaux.
  - Sinon → génération automatique FR, déterministe (seedée sur l'id),
    avec variété stylistique. Jamais de phrase de mise en garde dans
    le texte affiché : le signal « à valider » reste uniquement dans
    aCurer (fichier meta).

Entrée  : data_transform/input/catalogue.json
Sorties :
  - data_transform/output/catalogue_transforme.json
  - data_transform/output/catalogue_transforme_meta.json

Usage :
    python transform_json.py
    python transform_json.py --images-dir "C:\\...\\public\\artistes"

Non destructif, rejouable à volonté (mêmes id → mêmes textes / prix).
------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote

# ── Chemins ─────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
INPUT_FILE = INPUT_DIR / "catalogue.json"
OUTPUT_FILE = OUTPUT_DIR / "catalogue_transforme.json"
META_FILE = OUTPUT_DIR / "catalogue_transforme_meta.json"
RAPPORT_IMAGES = OUTPUT_DIR / "rapport_images_manquantes.json"

MEDIUM_GARBAGE_MARKERS = [
    "strictement nécessaire",
    "intérêt légitime",
    "cookie",
    "consentement",
]

GRADIENT_PAR_DEFAUT = "linear-gradient(155deg, #9AA0A6, #3A3E44)"
MEDIUM_PAR_DEFAUT = ""

# ── Textes éditoriaux par artiste ────────────────────────────────
BIO_ARTISTES: dict[str, str] = {
    "Bruno Catalano":
        "Sculpteur mondialement reconnu pour ses « Voyageurs » en bronze au corps fragmenté, dont la célèbre composition Le Voyageur. Ses personnages, valise à la main, semblent suspendus entre départ et souvenir.",
    "Andrea Roggi":
        "Maître italien de la sculpture en bronze, célébré pour ses arbres de vie et ses couples enlacés inscrits dans le cercle. Son œuvre chante le lien, la nature et la transmission.",
    "Paul Beckrich":
        "Sculpteur du bronze doré à l'or fin et du raku, il façonne des bustes précieux — dames de Chine, samouraïs — où la matière noble rencontre la sérénité du visage.",
    "Lorenzo Quinn":
        "Sculpteur des mains et des émotions, mondialement connu pour ses œuvres monumentales sur l'amour et le lien humain. Ses bronzes traduisent en gestes ce qui échappe aux mots.",
    "Al Freno":
        "Peintre figuratif à l'univers pop et coloré, il compose de grands formats à l'huile aux bleus profonds où le regard et l'énergie tiennent le premier rôle.",
    "Kiko":
        "Peintre au couteau, il sculpte la lumière et la couleur en épaisseur sur la toile de lin. Ses profils et ses couples vibrent d'une matière multicolore et sensible.",
    "Harding Meyer":
        "Portraitiste contemporain de renom, il peint à l'huile des visages monumentaux au regard suspendu, d'une présence magnétique et cinématographique.",
    "Roberta Coni":
        "Peintre italienne du portrait, elle fait surgir des visages d'une facture presque classique avant de les brouiller de coulures et d'empâtements abstraits. Fleurs, papillons et feuille d'or y traversent le fond sombre.",
    "Zhuang Hong Yi":
        "Il plie et peint des milliers de fleurs de papier de riz qu'il monte en relief sur la toile. La couleur y change selon l'angle du regard : l'œuvre se recompose à chaque pas du visiteur.",
}

UNIVERS_ARTISTES: dict[str, str] = {
    "Bruno Catalano":
        "Voyageurs en bronze au torse évidé/fragmenté, valise à la main ; thèmes du départ, du voyage, de la mémoire, du mouvement. Sujets : silhouettes d'hommes et de femmes en marche.",
    "Andrea Roggi":
        "Sculpture figurative en bronze autour de la nature et du lien humain : arbres de vie, racines, cercles, couples enlacés, famille, amour, spiritualité méditerranéenne.",
    "Paul Beckrich":
        "Bustes sculptés (bronze doré à l'or fin, raku) d'inspiration asiatique : dames de Chine, samouraïs japonais ; élégance, matière précieuse, sérénité des visages.",
    "Lorenzo Quinn":
        "Sculptures de mains et de gestes traitant des émotions et des relations humaines : amour, don, protection, lien, infini. Œuvres souvent monumentales.",
    "Al Freno":
        "Peinture figurative pop, très colorée : scènes et figures de la culture américaine (pin-ups, cinéma, publicité vintage, americana), regards et énergie. Grands formats à l'huile.",
    "Kiko":
        "Peinture au couteau, matière colorée en fort relief : profils et visages stylisés, couples, tendresse, fleurs ; abstraction lyrique multicolore. Aucune thématique documentaire ou géographique précise.",
    "Harding Meyer":
        "Portraits contemporains exclusivement : gros plans de visages à l'huile, regards intenses, cadrage cinématographique. Ni paysage, ni scène narrative, ni thème géographique.",
    "Roberta Coni":
        "Portraits féminins hyperréalistes brouillés par des coulures et des empâtements abstraits : visages sur fonds sombres, couronnes de fleurs, papillons, rehauts de feuille d'or. Thèmes de la féminité, du printemps et de la mythologie. Ni sculpture, ni paysage.",
    "Zhuang Hong Yi":
        "Reliefs abstraits en papier de riz plissé et peint, montés sur toile : champs de corolles en fort relief dont la couleur change selon l'angle de vue. Dégradés multicolores, formats carrés, panoramiques ou circulaires. Aucun sujet figuratif, aucun portrait.",
}

# ── Génération auto (variété Deepseek + déterminisme Grok) ───────
TEMPLATES_BIO = {
    "peinture": [
        "Peintre contemporain dont l'œuvre explore {theme}, {artiste} développe un langage plastique où la {matiere} devient le vecteur d'une émotion pure.",
        "{artiste} est un artiste peintre reconnu pour sa maîtrise de la {matiere}, créant des compositions où la {qualite} dialogue avec l'énergie du geste.",
        "La peinture de {artiste} se distingue par son approche {approche} et sa palette {palette}, offrant des œuvres qui captent {emotion}.",
        "Artiste peintre, {artiste} puise son inspiration dans {inspiration} pour créer des œuvres empreintes de {atmosphere}.",
        "{artiste} pratique une peinture {style} où la matière et la couleur s'entremêlent pour révéler {sujet} avec une sensibilité rare.",
    ],
    "sculpture": [
        "Sculpteur contemporain, {artiste} travaille la {matiere} avec une maîtrise affirmée, donnant naissance à des œuvres où {qualite}.",
        "La sculpture de {artiste} explore {theme} à travers des formes {approche}, où la {matiere} devient langage poétique.",
        "{artiste} est un sculpteur dont le geste {geste} transforme la matière brute en {sujet} chargé d'émotion et de sens.",
        "Artiste sculpteur, {artiste} façonne la {matiere} pour créer des pièces où {qualite} et {atmosphere} se rencontrent.",
        "Dans l'œuvre sculptée de {artiste}, la {matiere} se fait {qualite}, révélant {sujet} avec une élégance intemporelle.",
    ],
}

TEMPLATES_UNIVERS = {
    "peinture": [
        "Peinture figurative contemporaine ; thèmes récurrents : {themes}. Univers riche en {qualite}, où la couleur et la composition créent une atmosphère {atmosphere}.",
        "L'univers pictural de {artiste} se caractérise par {caracteristiques}. Une œuvre où {qualite} et {emotion} se conjuguent.",
        "Exploration de {themes} à travers la peinture, avec une prédilection pour {caracteristiques}. Une signature artistique forte et reconnaissable.",
        "Peintures où dominent {themes}, traitées avec une approche {approche}. L'artiste privilégie {caracteristiques} pour exprimer {emotion}.",
        "Univers artistique centré sur {themes}, décliné à travers {caracteristiques}. Des œuvres qui invitent à {emotion}.",
    ],
    "sculpture": [
        "Sculpture contemporaine ; thèmes récurrents : {themes}. Traitement de la {matiere} où {qualite} et {emotion} se répondent.",
        "L'univers sculpté de {artiste} explore {themes} à travers la {matiere}, créant des pièces où {qualite} s'impose.",
        "Œuvres sculpturales autour de {themes}, caractérisées par {caracteristiques}. Une approche où la {matiere} devient expression.",
        "Sculptures traitant de {themes} avec une sensibilité {approche}. {artiste} joue avec {caracteristiques} pour évoquer {emotion}.",
        "Univers de la sculpture où {themes} sont récurrents, avec une attention particulière à {caracteristiques}.",
    ],
}

THEMES_PEINTURE = [
    "la condition humaine, l'intimité, le regard",
    "les paysages intérieurs, la mémoire, le temps",
    "la lumière, les ombres, les contrastes",
    "les émotions, les sensations, l'instantané",
    "le mouvement, l'équilibre, l'harmonie",
    "la nature, les éléments, l'organique",
    "l'urbain, la modernité, le quotidien",
    "le rêve, l'imaginaire, l'onirisme",
    "le corps, la présence, l'incarnation",
    "l'abstraction, la matière, la gestuelle",
]

THEMES_SCULPTURE = [
    "le corps humain, la figure, l'anatomie",
    "le mouvement, l'équilibre, la dynamique",
    "la matière, la texture, la surface",
    "la lumière, les reflets, les ombres",
    "la nature, les formes organiques, le vivant",
    "le temps, la mémoire, la trace",
    "l'abstrait, le géométrique, la structure",
    "le sacré, le spirituel, le symbolique",
    "l'élégance, la grâce, l'harmonie",
    "la puissance, la force, l'énergie",
]

QUALITES = [
    "harmonie", "contraste", "profondeur", "texture", "lumière",
    "équilibre", "mouvement", "grâce", "puissance", "délicatesse",
    "intensité", "sérénité", "force", "élégance", "expressivité",
]

STYLES = [
    "lyrique", "gestuelle", "sensuelle", "contemplative", "énergique",
    "méditative", "intuitive", "spontanée", "maîtrisée", "organique",
]

MATIERES_PEINTURE = ["huile", "acrylique", "matière picturale"]
MATIERES_SCULPTURE = ["bronze", "pierre", "métal", "bois"]

APPROCHES = [
    "subtile", "radicale", "poétique", "architecturale", "expressive",
    "sensible", "contemplative", "gestuelle", "organique", "structurelle",
]

EMOTIONS = [
    "la sérénité", "l'intensité", "la joie", "la mélancolie", "la force",
    "la grâce", "l'élégance", "la puissance", "la douceur", "l'énergie",
]

INSPIRATIONS = [
    "la nature et ses éléments", "le corps et ses mouvements",
    "la lumière méditerranéenne", "les formes organiques",
    "l'architecture urbaine", "les mythes et symboles",
    "l'art primitif", "la tradition classique",
    "le quotidien et ses fragments", "l'imaginaire collectif",
]

PALETTES = ["chaude", "froide", "contrastée", "subtile", "vibrante", "douce"]
ATMOSPHERES = ["apaisante", "dynamique", "contemplative", "énergique", "méditative", "poétique"]
GESTES = ["précis", "ample", "sensible", "maîtrisé", "expressif", "spontané"]

SUJETS_PEINTURE = ["des figures", "des paysages", "des abstractions", "des compositions"]
SUJETS_SCULPTURE = ["des formes", "des volumes", "des silhouettes"]

PHRASES_SUPP_BIO = [
    " Ses œuvres, marquées par une grande {qualite}, invitent à la contemplation et au dialogue avec la matière.",
    " Chaque pièce témoigne d'une recherche constante sur {theme}, où la {matiere} devient langage.",
    " Son travail, à la fois {approche} et {style}, s'inscrit dans une démarche artistique singulière.",
    " {artiste} explore avec passion les possibilités de la {matiere}, créant des œuvres d'une rare intensité.",
]


def _lister_mots(mots_cles: list[str], n: int = 3) -> str:
    choisis = mots_cles[:n] if mots_cles else ["contemporain"]
    return ", ".join(choisis)


def generer_bio_fallback(artiste: str, tag: str, mots_cles: list[str], oid: str) -> str:
    """Bio auto FR, déterministe (seed oid + '-bio'), variété stylistique."""
    rng = random.Random(oid + "-bio")
    tag_key = tag if tag in TEMPLATES_BIO else "peinture"

    theme = rng.choice(THEMES_PEINTURE if tag_key == "peinture" else THEMES_SCULPTURE)
    # Préférer les mots-clés de la fiche s'ils existent
    if mots_cles:
        theme_mots = _lister_mots(mots_cles, 3)
        if rng.random() < 0.5:
            theme = theme_mots

    qualite = rng.choice(QUALITES)
    matiere = rng.choice(MATIERES_PEINTURE if tag_key == "peinture" else MATIERES_SCULPTURE)
    approche = rng.choice(APPROCHES)
    style = rng.choice(STYLES)
    emotion = rng.choice(EMOTIONS)
    inspiration = rng.choice(INSPIRATIONS)
    palette = rng.choice(PALETTES)
    atmosphere = rng.choice(ATMOSPHERES)
    geste = rng.choice(GESTES)
    sujet = rng.choice(SUJETS_PEINTURE if tag_key == "peinture" else SUJETS_SCULPTURE)

    template = rng.choice(TEMPLATES_BIO[tag_key])
    bio = template.format(
        artiste=artiste,
        theme=theme,
        matiere=matiere,
        qualite=qualite,
        approche=approche,
        style=style,
        emotion=emotion,
        inspiration=inspiration,
        sujet=sujet,
        palette=palette,
        atmosphere=atmosphere,
        geste=geste,
    )

    if len(bio) < 90:
        supp = rng.choice(PHRASES_SUPP_BIO).format(
            artiste=artiste,
            theme=theme,
            matiere=matiere,
            qualite=qualite,
            approche=approche,
            style=style,
        )
        bio += supp

    return bio


def generer_univers_fallback(
    artiste: str,
    tag: str,
    mots_cles: list[str],
    dims: "Dimensions",
    oid: str,
) -> str:
    """
    Univers auto FR, déterministe (seed oid + '-univers').
    Pas de phrase de mise en garde dans le texte : le signal
    « à valider » reste uniquement dans aCurer.
    """
    rng = random.Random(oid + "-univers")
    tag_key = tag if tag in TEMPLATES_UNIVERS else "peinture"

    themes = rng.choice(THEMES_PEINTURE if tag_key == "peinture" else THEMES_SCULPTURE)
    if mots_cles:
        themes_mots = _lister_mots(mots_cles, 3)
        if rng.random() < 0.5:
            themes = themes_mots

    caracteristiques: list[str] = []
    if dims.h is not None and dims.l is not None and dims.l > 0:
        ratio = dims.h / dims.l
        if ratio > 1.5:
            caracteristiques.append("formats verticaux élancés")
        elif ratio < 0.7:
            caracteristiques.append("formats horizontaux panoramiques")
        elif abs(ratio - 1) < 0.1:
            caracteristiques.append("formats carrés équilibrés")
        else:
            caracteristiques.append("formats équilibrés")
    else:
        caracteristiques.append("formats variés")

    if tag_key == "sculpture":
        caracteristiques.extend(["travail en volume", "recherche sur la matérialité"])
    else:
        caracteristiques.extend(["travail sur la couleur", "composition picturale"])

    for mot in (mots_cles or [])[:2]:
        m = mot.lower()
        if "couleur" in m or "palette" in m:
            caracteristiques.append("palette chromatique riche")
        elif "matière" in m or "texture" in m:
            caracteristiques.append("sensibilité tactile")
        elif "lumière" in m:
            caracteristiques.append("travail sur la luminosité")
        elif "mouvement" in m:
            caracteristiques.append("dynamique des formes")

    # Déduplique en conservant l'ordre
    seen: set[str] = set()
    carac_uniques: list[str] = []
    for c in caracteristiques:
        if c not in seen:
            seen.add(c)
            carac_uniques.append(c)

    template = rng.choice(TEMPLATES_UNIVERS[tag_key])
    univers = template.format(
        artiste=artiste,
        themes=themes,
        caracteristiques=", ".join(carac_uniques[:3]),
        qualite=rng.choice(QUALITES),
        emotion=rng.choice(EMOTIONS),
        approche=rng.choice(APPROCHES),
        matiere=rng.choice(MATIERES_PEINTURE if tag_key == "peinture" else MATIERES_SCULPTURE),
        atmosphere=rng.choice(ATMOSPHERES),
    )

    if tag_key == "peinture":
        univers += f" Techniques privilégiées : {rng.choice(['huile', 'acrylique', 'techniques mixtes'])}."
    else:
        univers += f" Matériaux de prédilection : {rng.choice(['bronze', 'pierre', 'bois', 'métal'])}."

    return univers


def enrichir_bio_univers(
    artiste: str,
    tag: str,
    mots_cles: list[str],
    dims: "Dimensions",
    oid: str,
) -> tuple[str, str, bool, bool]:
    """
    Retourne (bio, univers, bio_ok, univers_ok).

    bio_ok / univers_ok = True  → texte éditorial connu.
    bio_ok / univers_ok = False → texte auto-généré (déterministe) ;
      la fiche reste dans aCurer, sans mention dans le texte affiché.
    """
    bio_ok = artiste in BIO_ARTISTES
    univers_ok = artiste in UNIVERS_ARTISTES

    bio = BIO_ARTISTES[artiste] if bio_ok else generer_bio_fallback(artiste, tag, mots_cles, oid)
    univers = (
        UNIVERS_ARTISTES[artiste]
        if univers_ok
        else generer_univers_fallback(artiste, tag, mots_cles, dims, oid)
    )

    return bio, univers, bio_ok, univers_ok


# ── Banque de mots-clés / couleurs / medium par artiste ─────────────
ARTISTE_DEFAUTS: dict[str, dict[str, Any]] = {
    "Al Freno": {
        "medium": "Huile sur toile",
        "motsCles_base": ["figuratif", "coloré", "pop"],
        "motsCles_variantes": ["grand format", "mouvement", "joie", "regard", "amour", "nature"],
        "couleurs_variantes": [["bleu"], ["bleu", "blanc"], ["bleu", "orange"]],
    },
    "Kiko": {
        "medium": "Peinture au couteau sur toile de lin",
        "motsCles_base": ["couteau", "matière"],
        "motsCles_variantes": ["origami", "fleur", "tendresse", "couple", "fusion", "lumière", "gardien"],
        "couleurs_variantes": [["rose"], ["rose", "rouge"], ["rose", "doré"], ["rose", "multicolore"]],
    },
    "Harding Meyer": {
        "medium": "Huile sur toile",
        "motsCles_base": ["portrait", "visage", "regard"],
        "motsCles_variantes": ["contemporain", "grand format"],
        "couleurs_variantes": [["brun", "chair"]],
    },
    "Bruno Catalano": {
        "medium": "Bronze",
        "motsCles_base": ["voyageur", "valise", "bronze fragmenté", "silhouette", "voyage"],
        "motsCles_variantes": ["homme qui marche", "femme qui marche", "grand format", "bandeau"],
        "couleurs_variantes": [["bronze"], ["bronze", "orange"]],
    },
    "Andrea Roggi": {
        "medium": "Bronze",
        "motsCles_base": ["bronze"],
        "motsCles_variantes": ["couple", "amour", "étreinte", "cercle", "famille", "arbre de vie", "racines", "nature", "lien"],
        "couleurs_variantes": [["bronze", "vert"]],
    },
    "Paul Beckrich": {
        "medium": "Bronze doré à l'or fin",
        "motsCles_base": ["buste", "visage"],
        "motsCles_variantes": ["doré", "féminin", "chine", "samouraï", "guerrier", "raku", "céramique"],
        "couleurs_variantes": [["doré"], ["doré", "bleu"], ["doré", "brun"], ["rouge", "doré"]],
    },
    "Lorenzo Quinn": {
        "medium": "Bronze",
        "motsCles_base": ["mains", "émotion"],
        "motsCles_variantes": ["cœur", "amour", "don", "infini", "aluminium", "petit format"],
        "couleurs_variantes": [["bronze"], ["bleu", "bronze"], ["argent"]],
    },
}

DEFAUT_PAR_TAG: dict[str, dict[str, Any]] = {
    "peinture": {
        "medium": "Huile sur toile",
        "motsCles_base": ["peinture", "figuratif"],
        "motsCles_variantes": ["contemporain", "coloré", "grand format", "scène de vie", "portrait"],
        "couleurs_variantes": [["multicolore"], ["bleu"], ["chaud"], ["neutre"]],
    },
    "sculpture": {
        "medium": "Bronze",
        "motsCles_base": ["sculpture", "volume"],
        "motsCles_variantes": ["bronze", "figuratif", "abstrait", "grand format"],
        "couleurs_variantes": [["bronze"], ["bronze", "vert"], ["doré"]],
    },
}

FOURCHETTE_PRIX = {
    "peinture": (3_000, 20_000, 100),
    "sculpture": (8_000, 40_000, 500),
}


def generer_prix(tag: str, oid: str) -> str:
    mini, maxi, pas = FOURCHETTE_PRIX.get(tag, FOURCHETTE_PRIX["peinture"])
    rng = random.Random(oid + "-prix")
    valeur = rng.randrange(mini, maxi + pas, pas)
    return f"{valeur:,}".replace(",", " ") + " €"


def enrichir_variantes(artiste: str, tag: str, oid: str) -> tuple[list[str], list[str], str]:
    defauts = ARTISTE_DEFAUTS.get(artiste) or DEFAUT_PAR_TAG.get(tag, DEFAUT_PAR_TAG["peinture"])
    rng = random.Random(oid + "-variantes")

    base = list(defauts["motsCles_base"])
    variantes_dispo = list(defauts["motsCles_variantes"])
    rng.shuffle(variantes_dispo)
    n_extra = rng.randint(1, min(2, len(variantes_dispo))) if variantes_dispo else 0
    mots_cles = base + variantes_dispo[:n_extra]

    couleurs = list(rng.choice(defauts["couleurs_variantes"]))
    return mots_cles, couleurs, defauts["medium"]


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("transform_catalogue")


# ── Modèle de sortie ───────────────────────────────────────────────
@dataclass
class Dimensions:
    h: float | None = None
    l: float | None = None
    p: float | None = None


@dataclass
class Oeuvre:
    id: str
    artiste: str
    titre: str
    medium: str
    dimensions: str
    prix: str
    tag: str
    gradient: str
    image: str
    dims: Dimensions
    orientation: str
    motsCles: list[str] = field(default_factory=list)
    couleurs: list[str] = field(default_factory=list)
    descriptionCourte: str = ""
    bio: str = ""
    univers: str = ""


@dataclass
class OeuvreMeta:
    id: str
    idSource: str
    sourceUrl: str | None
    imageDistante: str | None
    annee: str | None
    mediumRecupere: bool
    aCurer: list[str] = field(default_factory=list)
    bioAutoGeneree: bool = False
    universAutoGenere: bool = False


# ── Utilitaires ───────────────────────────────────────────────────
def normaliser(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.lower()


def slugify(s: str) -> str:
    s = normaliser(s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def titre_capitalise(artiste_brut: str) -> str:
    return " ".join(mot.capitalize() for mot in artiste_brut.split())


def medium_est_corrompu(medium: str | None) -> bool:
    if not medium:
        return True
    bas = medium.lower()
    return any(m in bas for m in MEDIUM_GARBAGE_MARKERS)


def parser_dimensions(brut: str | None) -> Dimensions:
    if not brut:
        return Dimensions()
    nettoye = re.sub(r"\s+", " ", brut).strip()

    hauteur_seule = re.match(r"H\.?\s*(\d+(?:[.,]\d+)?)\s*cm", nettoye, re.IGNORECASE)
    if hauteur_seule and "x" not in nettoye.lower():
        return Dimensions(h=float(hauteur_seule.group(1).replace(",", ".")))

    sans_cm = re.sub(r"cm", "", nettoye, flags=re.IGNORECASE)
    parties: list[float] = []
    for p in re.split(r"x", sans_cm, flags=re.IGNORECASE):
        p = p.strip().replace(",", ".")
        try:
            parties.append(float(p))
        except ValueError:
            continue

    if len(parties) == 3:
        return Dimensions(h=parties[0], l=parties[1], p=parties[2])
    if len(parties) == 2:
        return Dimensions(h=parties[0], l=parties[1])
    if len(parties) == 1:
        return Dimensions(h=parties[0])
    return Dimensions()


def determiner_orientation(dims: Dimensions) -> str:
    if dims.h is None or dims.l is None:
        return "volume"
    if dims.h == dims.l:
        return "carre"
    return "portrait" if dims.h > dims.l else "paysage"


def deduire_tag(orientation: str) -> str:
    return "sculpture" if orientation == "volume" else "peinture"


def extraire_medium_depuis_url(image_url: str | None) -> str | None:
    if not image_url:
        return None
    nom = image_url.rsplit("/", 1)[-1]
    nom = re.sub(r"\.(jpg|jpeg|png|webp)$", "", nom, flags=re.IGNORECASE)
    nom = unquote(nom)

    match = re.search(r"cm[-_]+(.+)$", nom, flags=re.IGNORECASE)
    if not match:
        return None

    reste = match.group(1).replace("-", " ").replace("_", " ")
    reste = re.sub(r"\s+", " ", reste).strip()
    reste = re.sub(r"[\s\-–_0-9]+$", "", reste).strip()

    if len(reste) < 4 or len(reste) > 90 or "cm" in reste.lower():
        return None
    return reste[0].upper() + reste[1:]


def construire_image(local_image: str | None) -> str:
    if not local_image:
        return ""
    return local_image if local_image.startswith("/") else f"/{local_image}"


# ── Cœur de la transformation ───────────────────────────────────────
def transformer_entree(entree: dict[str, Any]) -> tuple[Oeuvre, OeuvreMeta] | None:
    data = entree.get("data")
    if not data:
        log.warning("Entrée sans bloc 'data' ignorée (id=%s)", entree.get("id"))
        return None

    artiste_brut = data.get("artist")
    titre_brut = data.get("title")
    if not artiste_brut or not titre_brut:
        log.warning("Entrée sans artiste/titre ignorée (id=%s)", entree.get("id"))
        return None

    statut = entree.get("status")
    if statut and statut != "active":
        log.info("Entrée '%s' ignorée (statut=%s)", titre_brut, statut)
        return None

    artiste = titre_capitalise(artiste_brut)
    dims = parser_dimensions(data.get("dimensions"))
    orientation = determiner_orientation(dims)
    tag = deduire_tag(orientation)

    medium_brut = data.get("medium")
    a_curer: list[str] = []

    oid = f"scrape-{slugify(artiste)}-{slugify(titre_brut)}"
    mots_cles, couleurs, medium_defaut_artiste = enrichir_variantes(artiste, tag, oid)

    bio, univers, bio_ok, univers_ok = enrichir_bio_univers(artiste, tag, mots_cles, dims, oid)

    if not bio_ok:
        a_curer.append("bio")
    if not univers_ok:
        a_curer.append("univers")

    if medium_est_corrompu(medium_brut):
        medium_recupere = extraire_medium_depuis_url(data.get("image"))
        medium = medium_recupere or medium_defaut_artiste
        medium_ok = medium_recupere is not None
        if not medium_ok:
            a_curer.append("medium")
    else:
        medium = medium_brut
        medium_ok = True

    prix = generer_prix(tag, oid)
    a_curer.append("prix")

    gradient = GRADIENT_PAR_DEFAUT
    a_curer.append("gradient")

    description_courte = f"{titre_brut.title()} — œuvre de {artiste}."
    a_curer.append("descriptionCourte")

    if artiste not in ARTISTE_DEFAUTS:
        a_curer.append("motsCles")
        a_curer.append("couleurs")
    a_curer.append("tag")

    oeuvre = Oeuvre(
        id=oid,
        artiste=artiste,
        titre=titre_brut,
        medium=medium,
        dimensions=data.get("dimensions") or "",
        prix=prix,
        tag=tag,
        gradient=gradient,
        image=construire_image(data.get("local_image")),
        dims=dims,
        orientation=orientation,
        motsCles=mots_cles,
        couleurs=couleurs,
        descriptionCourte=description_courte,
        bio=bio,
        univers=univers,
    )

    meta = OeuvreMeta(
        id=oid,
        idSource=entree.get("id", ""),
        sourceUrl=data.get("source_url") or data.get("url"),
        imageDistante=data.get("image"),
        annee=data.get("year"),
        mediumRecupere=medium_ok,
        aCurer=a_curer,
        bioAutoGeneree=not bio_ok,
        universAutoGenere=not univers_ok,
    )

    return oeuvre, meta


def verifier_image_locale(oeuvre: Oeuvre, images_dir: Path | None) -> bool:
    if images_dir is None or not oeuvre.image:
        return True
    chemin = images_dir.parent / oeuvre.image.lstrip("/")
    return chemin.exists()


def transformer_catalogue(brut: dict[str, Any], images_dir: Path | None):
    oeuvres: list[Oeuvre] = []
    metas: list[OeuvreMeta] = []
    rapport_images: list[dict] = []

    for _cle, entree in brut.items():
        resultat = transformer_entree(entree)
        if resultat is None:
            continue
        oeuvre, meta = resultat

        if not verifier_image_locale(oeuvre, images_dir):
            rapport_images.append({
                "artiste": oeuvre.artiste,
                "titre": oeuvre.titre,
                "imageAttendue": oeuvre.image,
            })
            continue

        oeuvres.append(oeuvre)
        metas.append(meta)

    return oeuvres, metas, rapport_images


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transforme catalogue.json scrapé en JSON conforme au type Oeuvre."
    )
    parser.add_argument("--input", type=Path, default=INPUT_FILE)
    parser.add_argument("--output", type=Path, default=OUTPUT_FILE)
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=None,
        help="public/artistes du projet front, pour ne garder que les fiches dont l'image existe.",
    )
    args = parser.parse_args()

    if not args.input.exists():
        log.error("Fichier d'entrée introuvable : %s", args.input)
        raise SystemExit(1)

    log.info("Lecture de %s", args.input)
    with args.input.open("r", encoding="utf-8") as f:
        brut = json.load(f)
    log.info("%d entrées brutes trouvées", len(brut))

    oeuvres, metas, rapport_images = transformer_catalogue(brut, args.images_dir)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump([asdict(o) for o in oeuvres], f, ensure_ascii=False, indent=2)

    with META_FILE.open("w", encoding="utf-8") as f:
        json.dump([asdict(m) for m in metas], f, ensure_ascii=False, indent=2)

    if args.images_dir is not None:
        with RAPPORT_IMAGES.open("w", encoding="utf-8") as f:
            json.dump(rapport_images, f, ensure_ascii=False, indent=2)

    bio_generees = sum(1 for m in metas if m.bioAutoGeneree)
    univers_generees = sum(1 for m in metas if m.universAutoGenere)
    medium_recuperes = sum(1 for m in metas if m.mediumRecupere)

    log.info("✅ %d œuvres écrites dans %s", len(oeuvres), args.output)
    log.info("ℹ️  Suivi de curation -> %s", META_FILE)
    log.info("🔎 medium récupéré automatiquement pour %d/%d fiches", medium_recuperes, len(metas))
    log.info("📝 bio générée automatiquement pour %d/%d fiches", bio_generees, len(metas))
    log.info("📝 univers généré automatiquement pour %d/%d fiches", univers_generees, len(metas))
    if args.images_dir is not None:
        log.info("⏭️  %d fiches ignorées (image locale absente)", len(rapport_images))
    else:
        log.info("ℹ️  Vérification des images locales non effectuée (--images-dir non fourni)")


if __name__ == "__main__":
    main()