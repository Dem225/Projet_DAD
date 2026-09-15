"""
Script d'automatisation : attribution d'IP aux nouveaux sites sur les passerelles existantes.

Prérequis :
    pip install pandas openpyxl

Fichiers d'entrée :

1) Gateway.xlsx (liste des passerelles / sous-réseaux existants) - colonnes attendues :
       Block d'adresse, Masque, vlanid, Technology, Router Gateway,
       Vendor, Router hostname, Longitude, Latitude, Area, Nom du Router

2) SiteID_List.xlsx (sites déjà raccordés, sert d'inventaire des IP utilisées) - colonnes attendues :
       Site ID, Nom du site, Area, ..., Router Gateway, ...
       puis pour chaque techno : "<Tech> IP", "<Tech> GW IP", "<Tech> vlanid"
       (ex: "2G IP", "2G GW IP", "2G vlanid", "3G OaM IP", "3G OaM GW IP", ...)
       Les IP de site sont au format "x.x.x.x/masque", les GW IP au format "x.x.x.x".

3) Fichier des NOUVEAUX sites à ajouter - colonnes attendues :
       Site ID (ou nom du site), Router Gateway, Technology
       -> une ligne par (site, techno) à raccorder.
"""

import ipaddress
import re

import pandas as pd


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Nettoie les noms de colonnes (espaces en trop, etc.)."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    return df


def load_gateways(path: str) -> pd.DataFrame:
    """Charge Gateway.xlsx et nettoie les colonnes clés."""
    df = _clean_columns(pd.read_excel(path))
    df["Block d'adresse"] = df["Block d'adresse"].astype(str).str.strip()
    df["Masque"] = df["Masque"].astype(str).str.strip()
    df["Router Gateway"] = df["Router Gateway"].astype(str).str.strip()
    df["Technology"] = df["Technology"].astype(str).str.strip()
    return df


def load_existing_sites(path: str) -> pd.DataFrame:
    """Charge SiteID_List.xlsx (inventaire des sites/IP déjà utilisés)."""
    return _clean_columns(pd.read_excel(path))


def find_gateway_network(gateways_df: pd.DataFrame, router_gateway: str, technology: str):
    """
    Retourne (network, vlanid, row) pour le couple (Router Gateway, Technology) demandé.
    Lève une erreur explicite si aucune correspondance (ou plusieurs) n'est trouvée.
    """
    match = gateways_df[
        (gateways_df["Router Gateway"] == str(router_gateway).strip())
        & (gateways_df["Technology"].str.upper() == str(technology).strip().upper())
    ]

    if match.empty:
        raise ValueError(
            f"Aucune passerelle trouvée pour Router Gateway='{router_gateway}' "
            f"et Technology='{technology}'."
        )
    if len(match) > 1:
        raise ValueError(
            f"Plusieurs passerelles trouvées pour Router Gateway='{router_gateway}' "
            f"et Technology='{technology}' — vérifie Gateway.xlsx (doublons)."
        )

    row = match.iloc[0]
    block_address = row["Block d'adresse"]
    mask = row["Masque"]
    network = ipaddress.ip_network(f"{block_address}/{mask}", strict=False)
    return network, row.get("vlanid"), row


def _extract_ips_from_value(value) -> list[str]:
    """
    Extrait une IP d'une cellule qui peut être 'x.x.x.x', 'x.x.x.x/masque',
    vide, ou NaN. Retourne une liste (0 ou 1 élément) pour rester simple à agréger.
    """
    if pd.isna(value):
        return []
    text = str(value).strip()
    if not text:
        return []
    ip_part = text.split("/")[0].strip()
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip_part):
        return []
    return [ip_part]


def get_used_ips_in_network(sites_df: pd.DataFrame, network: ipaddress.IPv4Network) -> set[str]:
    """
    Parcourt toutes les colonnes se terminant par 'IP' dans l'inventaire des sites
    (IP attribuées ET GW IP), et retourne celles qui appartiennent au sous-réseau ciblé.
    """
    ip_columns = [c for c in sites_df.columns if c.strip().upper().endswith("IP")]

    used = set()
    for col in ip_columns:
        for value in sites_df[col]:
            for ip_str in _extract_ips_from_value(value):
                try:
                    ip_obj = ipaddress.ip_address(ip_str)
                except ValueError:
                    continue
                if ip_obj in network:
                    used.add(str(ip_obj))
    return used


def get_next_free_ip(network: ipaddress.IPv4Network, used_ips: set[str]) -> str | None:
    """Retourne la première IP libre du sous-réseau (hors réseau/broadcast/déjà utilisées)."""
    for host in network.hosts():
        if str(host) not in used_ips:
            return str(host)
    return None


def process_new_sites(
    new_sites_path: str,
    gateway_path: str,
    siteid_path: str,
    output_path: str,
) -> pd.DataFrame:
    """
    Pour chaque ligne du fichier des nouveaux sites (Site ID / Nom, Router Gateway, Technology),
    trouve le sous-réseau correspondant, calcule la première IP libre et l'attribue.
    Écrit un fichier de résultat avec le statut de chaque attribution.
    """
    new_sites_df = _clean_columns(pd.read_excel(new_sites_path))
    gateways_df = load_gateways(gateway_path)
    sites_df = load_existing_sites(siteid_path)

    required_cols = {"Router Gateway", "Technology"}
    missing = required_cols - set(new_sites_df.columns)
    if missing:
        raise ValueError(f"Colonnes manquantes dans le fichier des nouveaux sites : {missing}")

    site_label_col = "Site ID" if "Site ID" in new_sites_df.columns else new_sites_df.columns[0]

    results = []
    # Cache pour ne pas recalculer les IP utilisées à chaque ligne pour le même sous-réseau,
    # et pour éviter d'attribuer deux fois la même IP à deux nouveaux sites du même fichier.
    network_cache: dict[tuple[str, str], tuple[ipaddress.IPv4Network, set[str], object]] = {}

    for _, row in new_sites_df.iterrows():
        site_label = row[site_label_col]
        router_gw = str(row["Router Gateway"]).strip()
        technology = str(row["Technology"]).strip()
        key = (router_gw, technology.upper())

        try:
            if key not in network_cache:
                network, vlanid, gw_row = find_gateway_network(gateways_df, router_gw, technology)
                used_ips = get_used_ips_in_network(sites_df, network)
                network_cache[key] = (network, used_ips, vlanid)

            network, used_ips, vlanid = network_cache[key]
            free_ip = get_next_free_ip(network, used_ips)

            if free_ip is None:
                results.append(
                    {
                        "Site ID": site_label,
                        "Router Gateway": router_gw,
                        "Technology": technology,
                        "Sous-reseau": str(network),
                        "vlanid": vlanid,
                        "assigned_ip": None,
                        "status": "AUCUNE PLACE LIBRE",
                    }
                )
                continue

            used_ips.add(free_ip)  # réserve l'IP pour ne pas la réattribuer plus loin dans le fichier
            results.append(
                {
                    "Site ID": site_label,
                    "Router Gateway": router_gw,
                    "Technology": technology,
                    "Sous-reseau": str(network),
                    "vlanid": vlanid,
                    "assigned_ip": f"{free_ip}/{network.prefixlen}",
                    "status": "OK",
                }
            )

        except ValueError as exc:
            results.append(
                {
                    "Site ID": site_label,
                    "Router Gateway": router_gw,
                    "Technology": technology,
                    "Sous-reseau": None,
                    "vlanid": None,
                    "assigned_ip": None,
                    "status": f"ERREUR: {exc}",
                }
            )

    result_df = pd.DataFrame(results)
    result_df.to_excel(output_path, index=False)
    print(f"Traitement terminé. Résultats écrits dans : {output_path}")
    return result_df


if __name__ == "__main__":
    process_new_sites(
        new_sites_path="nouveaux_sites.xlsx",
        gateway_path="Gateway.xlsx",
        siteid_path="SiteID_List.xlsx",
        output_path="resultats_attribution.xlsx",
    )