#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py — pilote du flow d'augmentation du corpus.

Exemples :

    # 1. Extraire les mots-cles candidats depuis les JSON du scraper
    python run.py a1

    # 2. Generer les requetes (deterministe, sans LLM)
    python run.py a2

    # 3. Test reel sur 3 requetes seulement, Wikipedia uniquement
    python run.py a3 --limite 3 --providers wikipedia

    # 4. Controler que tout est trace et non invente
    python run.py verif

    # Tout d'un coup, en mode test
    python run.py all --limite 5

    # Verifier le determinisme de A1 (doit afficher IDENTIQUE)
    python run.py a1 --verifier-determinisme
"""

import argparse
import sys
from pathlib import Path

# Permet de lancer le script depuis n'importe ou
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass  # python-dotenv optionnel

from data_augmentation import config
from data_augmentation import a1_extraction, a2_requetes, a3_recherche, verifier
from data_augmentation.common import sha256_objet, lire_json


def verifier_determinisme() -> bool:
    """Relance A1 et compare l'empreinte des termes avec le run precedent."""
    if not config.A1_OUT.exists():
        print("Aucune sortie A1 precedente — lance d'abord `python run.py a1`.")
        return False
    avant = sha256_objet(lire_json(config.A1_OUT).get("termes", []))
    a1_extraction.executer()
    apres = sha256_objet(lire_json(config.A1_OUT).get("termes", []))
    identique = avant == apres
    print("\n" + "=" * 60)
    print(f"Empreinte avant : {avant}")
    print(f"Empreinte apres : {apres}")
    print("DETERMINISME : " + ("IDENTIQUE" if identique else "DIVERGENT"))
    print("=" * 60)
    return identique


def main():
    parseur = argparse.ArgumentParser(
        description="Flow d'augmentation du corpus (A1 -> A3), en amont de P0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parseur.add_argument(
        "etape",
        choices=["a1", "a2", "a3", "verif", "all"],
        help="etape a executer",
    )
    parseur.add_argument(
        "--limite", type=int, default=None,
        help="mode test : nombre max de termes (a1/a2) ou de requetes (a3)",
    )
    parseur.add_argument(
        "--providers", nargs="+", default=None,
        choices=["wikipedia", "tavily", "google_cse"],
        help="fournisseurs de recherche pour a3 (defaut : config.A3_PROVIDERS)",
    )
    parseur.add_argument(
        "--verifier-determinisme", action="store_true",
        help="relance A1 et compare l'empreinte avec le run precedent",
    )
    args = parseur.parse_args()

    if args.verifier_determinisme:
        sys.exit(0 if verifier_determinisme() else 1)

    if args.etape in ("a1", "all"):
        a1_extraction.executer(limite=args.limite)
    if args.etape in ("a2", "all"):
        a2_requetes.executer(limite=args.limite)
    if args.etape in ("a3", "all"):
        a3_recherche.executer(limite=args.limite, providers=args.providers)
    if args.etape in ("verif", "all"):
        rapport = verifier.executer()
        sys.exit(0 if rapport.tout_ok else 1)


if __name__ == "__main__":
    main()
