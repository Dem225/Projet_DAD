"""
main.py — point d'entrée en ligne de commande. Aucune logique de base de
données ici : tout vit dans db.py. On se contente d'appeler ses fonctions
selon la commande demandée.

Usage :
    python main.py init
        -> crée reseau.db et (re)charge Gateway.xlsx dedans (sans doublon)

    python main.py ajouter --site AC999 --gateway AC622 --tech 2G 5G --nom "NOUVEAU SITE"
        -> ajoute un site avec une ou plusieurs technologies

    python main.py import nouveaux_sites.xlsx
        -> ajoute plusieurs sites en une fois depuis un fichier Excel

    python main.py charger AC999
        -> affiche les infos réseau d'un site déjà en base
"""

import argparse

from Model.bd import (
    get_connection,
    create_schema,
    importer_gateways_depuis_excel,
    ajouter_site,
    importer_sites_depuis_excel,
    charger_gateway_site,
    lister_gateways,
)

DB_PATH = "reseau.db"
GATEWAY_EXCEL = "Gateway.xlsx"


def cmd_init(args):
    conn = get_connection(DB_PATH)
    create_schema(conn)
    importer_gateways_depuis_excel(conn, GATEWAY_EXCEL)
    conn.close()
    print("Base initialisée / mise à jour depuis", GATEWAY_EXCEL)


def cmd_ajouter(args):
    conn = get_connection(DB_PATH)
    resultats = ajouter_site(
        conn,
        site_id=args.site,
        router_gateway=args.gateway,
        technologies=args.tech,
        nom_site=args.nom,
    )
    conn.close()
    for r in resultats:
        print(r)


def cmd_import(args):
    conn = get_connection(DB_PATH)
    resultats = importer_sites_depuis_excel(conn, args.fichier)
    conn.close()
    for r in resultats:
        print(r)


def cmd_charger(args):
    conn = get_connection(DB_PATH)
    infos = charger_gateway_site(conn, args.site)
    conn.close()
    print(infos)


def cmd_chercher(args):
    conn = get_connection(DB_PATH)
    gateways = lister_gateways(conn, args.gateway)
    conn.close()
    if not gateways:
        print("Aucune passerelle trouvée.")
    for g in gateways:
        print(g)


def main():
    parser = argparse.ArgumentParser(description="Gestion des sites et passerelles réseau")
    sous_commandes = parser.add_subparsers(dest="commande", required=True)

    p_init = sous_commandes.add_parser("init", help="Créer/mettre à jour la base depuis Gateway.xlsx")
    p_init.set_defaults(func=cmd_init)

    p_ajouter = sous_commandes.add_parser("ajouter", help="Ajouter un site")
    p_ajouter.add_argument("--site", required=True, help="Site ID, ex: AC999")
    p_ajouter.add_argument("--gateway", required=True, help="Router Gateway, ex: AC622")
    p_ajouter.add_argument("--tech", required=True, nargs="+", help="Une ou plusieurs technos, ex: 2G 5G")
    p_ajouter.add_argument("--nom", default=None, help="Nom du site (optionnel)")
    p_ajouter.set_defaults(func=cmd_ajouter)

    p_import = sous_commandes.add_parser("import", help="Importer plusieurs sites depuis un Excel")
    p_import.add_argument("fichier", help="Chemin du fichier Excel des nouveaux sites")
    p_import.set_defaults(func=cmd_import)

    p_charger = sous_commandes.add_parser("charger", help="Afficher les infos réseau d'un site")
    p_charger.add_argument("site", help="Site ID à consulter")
    p_charger.set_defaults(func=cmd_charger)

    p_chercher = sous_commandes.add_parser("chercher", help="Lister les passerelles disponibles (BLOC ADRESSE)")
    p_chercher.add_argument("--gateway", default=None, help="Filtrer par Router Gateway, ex: AC622 (optionnel)")
    p_chercher.set_defaults(func=cmd_chercher)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()