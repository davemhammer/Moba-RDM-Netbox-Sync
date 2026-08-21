"""
Copyright (c) 2026 David Hammer. Licensed under the MIT License (see
LICENSE in the repository root).

Export the same NetBox-derived bookmark items used by netbox_moba_sync.py
as JSON, for consumption by netbox_rdm_sync.ps1 (or anything else that
wants the raw item list without touching MobaXterm.ini).

Usage:
    python netbox_export_items.py --config config.ini > items.json
"""

import argparse
import json
import sys

import requests

from netbox_moba_sync import (
    PROTOCOL_LABELS,
    build_device_items,
    build_vm_items,
    load_config,
    netbox_get_all,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.ini")
    args = ap.parse_args()

    cfg = load_config(args.config)
    nb_url = cfg["netbox"]["url"]
    nb_token = cfg["netbox"]["token"]
    verify_ssl = cfg["netbox"].getboolean("verify_ssl", fallback=True)
    ca_bundle = cfg["netbox"].get("ca_bundle", "").strip()
    verify = ca_bundle if (verify_ssl and ca_bundle) else verify_ssl

    org = cfg["moba"]["organization"]
    default_port = cfg["moba"].get("ssh_port", "22")
    default_username = cfg["moba"].get("ssh_username", "")
    usernames = {
        "ssh": cfg["moba"].get("ssh_username", ""),
        "rdp": cfg["moba"].get("rdp_username", ""),
        "telnet": cfg["moba"].get("telnet_username", ""),
    }

    session = requests.Session()
    session.headers.update({"Authorization": f"Token {nb_token}"})

    devices = netbox_get_all(session, nb_url, "/api/dcim/devices/", verify,
                              {"has_primary_ip": "true"})
    vms = netbox_get_all(session, nb_url, "/api/virtualization/virtual-machines/", verify)
    services = netbox_get_all(session, nb_url, "/api/ipam/services/", verify)

    items = build_device_items(devices, services, usernames, default_port, default_username)
    items += build_vm_items(vms, services, usernames)

    for item in items:
        item["group"] = f"{org}\\{item['site']}\\{PROTOCOL_LABELS[item['kind']]}"

    print(json.dumps(items, indent=2), file=sys.stdout)


if __name__ == "__main__":
    sys.exit(main())
