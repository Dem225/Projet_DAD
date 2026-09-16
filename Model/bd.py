"""
Base de données réseau — schéma en tables séparées par technologie,
conforme au plan manuscrit :

  BLOC ADRESSE (table gateways) : adresse, masque, vlan, technologie, gateway
  SITES                          : site_id, long, lat, zone, gateway, nom_site
  ADRESSE_2G / 3G / 4G / 5G      : site_id, adresse_ip, adresse_gateway, vlan
  OM_3G_4G                       : site_id, technologie('3G_OAM'/'4G_OAM'), adresse_ip, adresse_gateway, vlan
  OM_5G                          : site_id, adresse_ip, adresse_gateway, vlan

Trois fonctions principales, une par tâche du plan :
  ① ajouter_site(...)              -> ajoute un site (ou l'appeler en boucle pour un batch)
  ② importer_sites_depuis_excel(...) -> importe une liste de sites en une fois
  ③ charger_gateway_site(...)       -> récupère la/les passerelle(s) d'un site
"""

import ipaddress
import re
import sqlite3

import pandas as pd

DB_PATH = "reseau.db"

TECH_SIMPLE = ["2G", "3G", "4G", "5G"]  # chacune sa propre table ADRESSE_xG


# ---------------------------------------------------------------------------
# Schéma
# ---------------------------------------------------------------------------

def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS gateways (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            adresse     TEXT NOT NULL,
            masque      TEXT NOT NULL,
            vlan        TEXT,
            technologie TEXT NOT NULL,
            gateway     TEXT NOT NULL,
            UNIQUE(gateway, technologie)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sites (
            site_id     TEXT PRIMARY KEY,
            nom_site    TEXT,
            longitude   TEXT,
            latitude    TEXT,
            zone        TEXT,
            gateway     TEXT
        )
        """
    )

    for tech in TECH_SIMPLE:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS adresse_{tech.lower()} (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id         TEXT NOT NULL,
                adresse_ip      TEXT NOT NULL,
                adresse_gateway TEXT,
                vlan            TEXT,
                UNIQUE(site_id),
                FOREIGN KEY (site_id) REFERENCES sites(site_id)
            )
            """
        )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS om_3g_4g (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id         TEXT NOT NULL,
            technologie     TEXT NOT NULL,  -- '3G_OAM' ou '4G_OAM'
            adresse_ip      TEXT NOT NULL,
            adresse_gateway TEXT,
            vlan            TEXT,
            UNIQUE(site_id, technologie),
            FOREIGN KEY (site_id) REFERENCES sites(site_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS om_5g (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id         TEXT NOT NULL,
            adresse_ip      TEXT NOT NULL,
            adresse_gateway TEXT,
            vlan            TEXT,
            UNIQUE(site_id),
            FOREIGN KEY (site_id) REFERENCES sites(site_id)
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _extract_ip(value) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    ip_part = text.split("/")[0].strip()
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip_part):
        return None
    return ip_part


def _table_for(technologie: str) -> tuple[str, str | None]:
    """Retourne (nom_table, sous_technologie_ou_None) pour une technologie donnée."""
    t = technologie.strip().upper()
    if t in ("2G", "3G", "4G", "5G"):
        return f"adresse_{t.lower()}", None
    if t in ("3G_OAM", "4G_OAM", "OM_3G_4G"):
        return "om_3g_4g", t if t in ("3G_OAM", "4G_OAM") else None
    if t in ("5G_OAM", "OM_5G"):
        return "om_5g", None
    raise ValueError(f"Technologie inconnue : {technologie}")


# ---------------------------------------------------------------------------
# ① Ajouter un site (seul, ou en boucle pour un batch)
# ---------------------------------------------------------------------------

def find_gateway_network(conn: sqlite3.Connection, router_gateway: str, technologie: str):
    """Cherche le sous-réseau dans BLOC ADRESSE (table gateways) pour gateway+techno.
    Pour 3G_OAM/4G_OAM, on cherche sous 'OM_3G_4G' dans la table gateways.
    Pour 5G_OAM, on cherche sous 'OM_5G'."""
    t = technologie.strip().upper()
    lookup_tech = {"3G_OAM": "OM_3G_4G", "4G_OAM": "OM_3G_4G", "5G_OAM": "OM_5G"}.get(t, t)

    cur = conn.execute(
        "SELECT adresse, masque, vlan FROM gateways WHERE gateway = ? AND UPPER(technologie) = ?",
        (router_gateway.strip(), lookup_tech.upper()),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"Aucune passerelle pour gateway='{router_gateway}' / technologie='{technologie}'.")
    adresse, masque, vlan = row
    network = ipaddress.ip_network(f"{adresse}/{masque}", strict=False)
    return network, vlan


def _get_used_ips(conn: sqlite3.Connection, table: str, network: ipaddress.IPv4Network) -> set[str]:
    used = set()
    rows = conn.execute(f"SELECT adresse_ip, adresse_gateway FROM {table}").fetchall()
    for ip, gw in rows:
        for candidate in (ip, gw):
            if not candidate:
                continue
            candidate = str(candidate).split("/")[0]
            try:
                ip_obj = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if ip_obj in network:
                used.add(str(ip_obj))
    return used


def _next_free_ip(network: ipaddress.IPv4Network, used_ips: set[str]) -> str | None:
    hosts = list(network.hosts())
    if not hosts:
        return None
    gateway_ip = str(hosts[0])  # 1er host toujours réservé à la passerelle
    for host in hosts[1:]:
        if str(host) not in used_ips and str(host) != gateway_ip:
            return str(host)
    return None


def ajouter_site(
    conn: sqlite3.Connection,
    site_id: str,
    router_gateway: str,
    technologies: list[str],
    nom_site: str | None = None,
    longitude: str | None = None,
    latitude: str | None = None,
    zone: str | None = None,
) -> list[dict]:
    """
    ① Ajoute un site (nouveau ou existant) avec les technologies demandées.
    Pour chaque technologie : trouve le bon sous-réseau, calcule l'IP libre,
    l'insère dans la table dédiée. Persisté immédiatement (commit).
    """
    conn.execute(
        """
        INSERT INTO sites (site_id, nom_site, longitude, latitude, zone, gateway)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(site_id) DO UPDATE SET
            nom_site=excluded.nom_site, gateway=excluded.gateway
        """,
        (site_id, nom_site, longitude, latitude, zone, router_gateway),
    )

    resultats = []
    for tech in technologies:
        table, sous_tech = _table_for(tech)
        try:
            network, vlan = find_gateway_network(conn, router_gateway, tech)
        except ValueError as exc:
            resultats.append({"site_id": site_id, "technologie": tech, "status": f"ERREUR: {exc}"})
            continue

        used_ips = _get_used_ips(conn, table, network)
        free_ip = _next_free_ip(network, used_ips)
        gateway_ip = str(list(network.hosts())[0])

        # Vérifie si ce site a déjà une IP pour cette technologie (évite le faux "OK")
        if table == "om_3g_4g":
            deja_present = conn.execute(
                "SELECT 1 FROM om_3g_4g WHERE site_id = ? AND technologie = ?",
                (site_id, sous_tech or tech.upper()),
            ).fetchone()
        else:
            deja_present = conn.execute(
                f"SELECT 1 FROM {table} WHERE site_id = ?", (site_id,)
            ).fetchone()

        if deja_present:
            resultats.append(
                {
                    "site_id": site_id,
                    "technologie": tech,
                    "sous_reseau": str(network),
                    "status": "DEJA EXISTANT (aucune IP recalculée, aucune écriture)",
                }
            )
            continue

        if free_ip is None:
            resultats.append(
                {"site_id": site_id, "technologie": tech, "sous_reseau": str(network), "status": "AUCUNE PLACE LIBRE"}
            )
            continue

        if table == "om_3g_4g":
            conn.execute(
                """
                INSERT INTO om_3g_4g (site_id, technologie, adresse_ip, adresse_gateway, vlan)
                VALUES (?, ?, ?, ?, ?)
                """,
                (site_id, sous_tech or tech.upper(), f"{free_ip}/{network.prefixlen}", gateway_ip, vlan),
            )
        else:
            conn.execute(
                f"""
                INSERT INTO {table} (site_id, adresse_ip, adresse_gateway, vlan)
                VALUES (?, ?, ?, ?)
                """,
                (site_id, f"{free_ip}/{network.prefixlen}", gateway_ip, vlan),
            )

        resultats.append(
            {
                "site_id": site_id,
                "technologie": tech,
                "sous_reseau": str(network),
                "vlan": vlan,
                "assigned_ip": f"{free_ip}/{network.prefixlen}",
                "status": "OK",
            }
        )

    conn.commit()
    return resultats


# ---------------------------------------------------------------------------
# ② Importer la liste des sites (batch) avec leur gateway et adresses
# ---------------------------------------------------------------------------

def importer_sites_depuis_excel(conn: sqlite3.Connection, path: str) -> list[dict]:
    """
    Importe un Excel de plusieurs sites en une fois.
    Colonnes attendues : Site ID, Nom du site (optionnel), Router Gateway, Technology
    -> une ligne par (site, technologie) à ajouter.
    Retourne la liste des résultats (un par ligne traitée).
    """
    df = pd.read_excel(path)
    df.columns = [str(c).strip() for c in df.columns]

    required = {"Site ID", "Router Gateway", "Technology"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Colonnes manquantes : {missing}")

    tous_resultats = []
    # Regroupe par site pour n'appeler ajouter_site qu'une fois par site avec toutes ses technos
    for site_id, group in df.groupby("Site ID"):
        first = group.iloc[0]
        technologies = group["Technology"].astype(str).str.strip().tolist()
        resultats = ajouter_site(
            conn,
            site_id=str(site_id).strip(),
            router_gateway=str(first["Router Gateway"]).strip(),
            technologies=technologies,
            nom_site=str(first.get("Nom du site", "")).strip() or None,
        )
        tous_resultats.extend(resultats)

    return tous_resultats


# ---------------------------------------------------------------------------
# ③ Charger la/les gateway(s) d'un site
# ---------------------------------------------------------------------------

def charger_gateway_site(conn: sqlite3.Connection, site_id: str) -> dict:
    """
    ③ Récupère toutes les informations réseau d'un site : sa passerelle générale
    (Router Gateway) et le détail de ses adresses IP par technologie.
    """
    site_row = conn.execute(
        "SELECT site_id, nom_site, longitude, latitude, zone, gateway FROM sites WHERE site_id = ?",
        (site_id,),
    ).fetchone()
    if site_row is None:
        raise ValueError(f"Site '{site_id}' introuvable.")

    infos = {
        "site_id": site_row[0],
        "nom_site": site_row[1],
        "longitude": site_row[2],
        "latitude": site_row[3],
        "zone": site_row[4],
        "router_gateway": site_row[5],
        "adresses": [],
    }

    for tech in TECH_SIMPLE:
        row = conn.execute(
            f"SELECT adresse_ip, adresse_gateway, vlan FROM adresse_{tech.lower()} WHERE site_id = ?", (site_id,)
        ).fetchone()
        if row:
            infos["adresses"].append({"technologie": tech, "adresse_ip": row[0], "gateway_ip": row[1], "vlan": row[2]})

    for row in conn.execute(
        "SELECT technologie, adresse_ip, adresse_gateway, vlan FROM om_3g_4g WHERE site_id = ?", (site_id,)
    ):
        infos["adresses"].append({"technologie": row[0], "adresse_ip": row[1], "gateway_ip": row[2], "vlan": row[3]})

    row = conn.execute(
        "SELECT adresse_ip, adresse_gateway, vlan FROM om_5g WHERE site_id = ?", (site_id,)
    ).fetchone()
    if row:
        infos["adresses"].append({"technologie": "5G_OAM", "adresse_ip": row[0], "gateway_ip": row[1], "vlan": row[2]})

    return infos


def lister_gateways(conn: sqlite3.Connection, router_gateway: str | None = None) -> list[dict]:
    """Liste les passerelles disponibles (BLOC ADRESSE), filtrable par Router Gateway."""
    if router_gateway:
        rows = conn.execute(
            "SELECT gateway, technologie, adresse, masque, vlan FROM gateways WHERE gateway = ? ORDER BY technologie",
            (router_gateway.strip(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT gateway, technologie, adresse, masque, vlan FROM gateways ORDER BY gateway, technologie"
        ).fetchall()
    return [
        {"router_gateway": r[0], "technologie": r[1], "sous_reseau": f"{r[2]}/{r[3]}", "vlan": r[4]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Import initial du référentiel des passerelles (BLOC ADRESSE) depuis Gateway.xlsx
# ---------------------------------------------------------------------------

def importer_gateways_depuis_excel(conn: sqlite3.Connection, gateway_path: str) -> None:
    df = pd.read_excel(gateway_path)
    df.columns = [str(c).strip() for c in df.columns]
    for _, row in df.iterrows():
        conn.execute(
            """
            INSERT OR IGNORE INTO gateways (adresse, masque, vlan, technologie, gateway)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(row.get("Block d'adresse", "")).strip(),
                str(row.get("Masque", "")).strip(),
                str(row.get("vlanid", "")).strip(),
                str(row.get("Technology", "")).strip(),
                str(row.get("Router Gateway", "")).strip(),
            ),
        )
    conn.commit()