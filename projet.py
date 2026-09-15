"""
Script d'automatisation : attribution d'IP aux nouveaux sites sur les passerelles existantes.

Prérequis :
    pip install pandas openpyxl

Fichier Excel des NOUVEAUX SITES (entrée) - colonnes obligatoires :
    site_name        -> nom du nouveau site
    gateway_address   -> adresse de la passerelle (ex: 192.168.1.1)
    gateway_mask      -> masque en notation CIDR (ex: 24)

Fichier d'INVENTAIRE des IP déjà utilisées (Excel ou CSV) - colonne obligatoire :
    ip_address        -> une ligne par IP déjà attribuée (ex: 192.168.1.5)
    (le nom de la colonne est configurable via le paramètre ip_column)
"""

import ipaddress
import platform
import subprocess
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Méthode 1 (recommandée) : détection via un fichier d'inventaire existant
# ---------------------------------------------------------------------------

def load_used_ips(inventory_path: str, ip_column: str = "ip_address") -> set[str]:
    """
    Charge la liste des IP déjà utilisées depuis un fichier Excel ou CSV.
    Une ligne = une IP utilisée. Le nom de la colonne est configurable.
    """
    path = Path(inventory_path)
    if path.suffix.lower() == ".csv":
        df = pd.read_csv(inventory_path)
    else:
        df = pd.read_excel(inventory_path)

    if ip_column not in df.columns:
        raise ValueError(
            f"Colonne '{ip_column}' introuvable dans {inventory_path}. "
            f"Colonnes disponibles : {list(df.columns)}"
        )

    return set(df[ip_column].astype(str).str.strip())


def get_used_and_free_ips_from_inventory(
    address: str, mask: str, used_ips: set[str], exclude: list[str] | None = None
):
    """
    Sépare les IP hôtes d'une passerelle entre utilisées / libres,
    en se basant sur l'ensemble used_ips issu de l'inventaire (pas de ping).
    """
    exclude_set = set(exclude or [])
    hosts = get_hosts(address, mask)

    used, free = [], []
    for ip in hosts:
        if ip in exclude_set:
            continue
        if ip in used_ips:
            used.append(ip)
        else:
            free.append(ip)
    return used, free


# ---------------------------------------------------------------------------
# Méthode 2 (fallback) : détection par ping, si aucun inventaire n'existe
# ---------------------------------------------------------------------------

def is_ip_alive(ip: str, timeout: int = 1) -> bool:
    """
    Vérifie si une IP répond au ping (donc considérée comme déjà utilisée).
    ATTENTION : un appareil éteint ou qui bloque le ping (firewall) sera vu
    comme "libre" même s'il a une IP réservée. Moins fiable qu'un inventaire.
    """
    is_windows = platform.system().lower() == "windows"
    count_flag = "-n" if is_windows else "-c"
    timeout_flag = "-w" if is_windows else "-W"
    timeout_value = str(timeout * 1000) if is_windows else str(timeout)

    cmd = ["ping", count_flag, "1", timeout_flag, timeout_value, ip]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout + 1
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def get_used_and_free_ips_by_ping(address: str, mask: str, exclude: list[str] | None = None):
    """Scanne le sous-réseau via ping. Retourne (ip_utilisees, ip_libres)."""
    exclude_set = set(exclude or [])
    hosts = get_hosts(address, mask)

    used, free = [], []
    for ip in hosts:
        if ip in exclude_set:
            continue
        if is_ip_alive(ip):
            used.append(ip)
        else:
            free.append(ip)
    return used, free


# ---------------------------------------------------------------------------
# Commun
# ---------------------------------------------------------------------------

def get_hosts(address: str, mask: str) -> list[str]:
    """
    Retourne toutes les IP hôtes utilisables du sous-réseau (hors réseau/broadcast).
    mask est attendu en notation CIDR (ex: "24").
    """
    network = ipaddress.ip_network(f"{address}/{mask}", strict=False)
    return [str(ip) for ip in network.hosts()]


def process_excel(
    input_path: str,
    output_path: str,
    inventory_path: str | None = None,
    ip_column: str = "ip_address",
) -> pd.DataFrame:
    """
    Lit le fichier Excel des nouveaux sites, attribue une IP libre à chacun
    dans la passerelle indiquée, et écrit un fichier de résultat.

    Si inventory_path est fourni -> détection via l'inventaire (recommandé).
    Sinon -> détection par ping (fallback, moins fiable).
    """
    df = pd.read_excel(input_path)

    required_cols = {"site_name", "gateway_address", "gateway_mask"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"Colonnes manquantes dans l'Excel : {missing}")

    all_used_ips = load_used_ips(inventory_path, ip_column) if inventory_path else None

    results = []
    # Cache pour ne scanner chaque passerelle qu'une seule fois même si
    # plusieurs sites lui sont rattachés dans le fichier.
    gateway_cache: dict[tuple[str, str], tuple[list[str], list[str]]] = {}

    for _, row in df.iterrows():
        site = row["site_name"]
        addr = str(row["gateway_address"]).strip()
        mask = str(row["gateway_mask"]).strip()
        key = (addr, mask)

        if key not in gateway_cache:
            if all_used_ips is not None:
                gateway_cache[key] = get_used_and_free_ips_from_inventory(
                    addr, mask, all_used_ips, exclude=[addr]
                )
            else:
                gateway_cache[key] = get_used_and_free_ips_by_ping(addr, mask, exclude=[addr])

        used, free = gateway_cache[key]

        if free:
            assigned_ip = free.pop(0)
            used.append(assigned_ip)
            if all_used_ips is not None:
                all_used_ips.add(assigned_ip)  # évite de réattribuer la même IP à 2 sites du fichier
            status = "OK"
        else:
            assigned_ip = None
            status = "AUCUNE PLACE LIBRE"

        results.append(
            {
                "site_name": site,
                "gateway_address": addr,
                "gateway_mask": mask,
                "assigned_ip": assigned_ip,
                "status": status,
            }
        )

    result_df = pd.DataFrame(results)
    result_df.to_excel(output_path, index=False)
    print(f"Traitement terminé. Résultats écrits dans : {output_path}")
    return result_df


if __name__ == "__main__":
    process_excel(
        input_path="nouveaux_sites.xlsx",
        output_path="resultats_attribution.xlsx",
        inventory_path="inventaire_ip.xlsx",   # ou "inventaire_ip.csv"
        ip_column="ip_address",
    )