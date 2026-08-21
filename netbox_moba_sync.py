"""
Pull devices and VMs from NetBox and sync them into MobaXterm.ini as
bookmarks under <Organization>\\<Site>\\<Protocol>\\<Name>.

Both devices and VMs are matched against their NetBox Services
(ipam.services) -- any service that looks like SSH, RDP, Telnet, HTTP, or
HTTPS (by name or well-known port) produces its own bookmark in the matching
protocol subfolder, e.g. Acme\\Site A\\SSH\\web01. An item with two matching
service kinds (e.g. both SSH and RDP) gets a bookmark in each protocol
folder -- no name suffixing needed since the folder itself disambiguates the
connection type.

Devices with NO matching service fall back to a default SSH bookmark on
their primary IP (config ssh_port/ssh_username) so devices NetBox hasn't
been fully tagged with services for don't silently disappear from
MobaXterm. VMs have no such fallback -- a VM with no SSH/RDP/Telnet/HTTP(S)
service produces no bookmark.

This edits MobaXterm.ini with targeted text surgery (not a full INI reparse),
so untouched sections -- including binary blobs like [CustomIcons] and hex
values in [SSH_Hostkeys] -- are left byte-for-byte intact, and anything
outside the org's own tree (hand-made bookmarks, other folders) is never
touched. The org's tree itself (every section whose SubRep is "org" or
starts with "org\\") is torn down and rebuilt fresh, in one contiguous
parent-before-child ordered block, on every run. MobaXterm's tree builder
needs each folder level's own section declared before its children's --
incremental append-only edits can leave a newly added ancestor section
trailing after children created in an earlier run, which silently breaks
that folder in the MobaXterm UI (looks empty, doesn't expand). A full
rebuild sidesteps that and is safe here since the org's entire tree is
100% NetBox-derived; nothing manual ever lives under it.

Usage:
    python netbox_moba_sync.py --config config.ini             # dry run, prints a diff summary
    python netbox_moba_sync.py --config config.ini --apply     # writes changes (backs up first)

Close MobaXterm before running with --apply -- it rewrites the ini on exit
and would clobber these changes.
"""

import argparse
import configparser
import datetime
import re
import sys
from pathlib import Path

import requests

TEMPLATES = {
    "ssh": (
        "#109#0%{ip}%{port}%{username}%%-1%-1%%%%%0%-1%0%%%-1%0%0%0%%1080%%0%0%1%%0%%%%0%-1%-1%0"
        "#MobaFont%10%0%0%-1%15%236,236,236%30,30,30%180,180,192%0%-1%0%%xterm%-1%0%_Std_Colors_0_"
        "%80%24%0%1%-1%<none>%%0%1%-1%0%#0# #-1"
    ),
    "rdp": (
        "#91#4%{ip}%{port}%{username}%0%0%0%0%-1%0%0%-1%%%%%0%0%%-1%%-1%-1%0%-1%0%-1%0%0%0%0%"
        "#MobaFont%10%0%0%-1%15%236,236,236%30,30,30%180,180,192%0%-1%0%%xterm%-1%0%_Std_Colors_0_"
        "%80%24%0%1%-1%<none>%%0%1%-1%0%#0# #-1"
    ),
    "telnet": (
        "#98#1%{ip}%{port}%{username}%%2%%%%%0%0%%1080%%%0%-1%0"
        "#MobaFont%10%0%0%-1%15%236,236,236%30,30,30%180,180,192%0%-1%0%%xterm%-1%0%_Std_Colors_0_"
        "%80%24%0%1%-1%<none>%%0%1%-1%0%#0# #-1"
    ),
    "browser": (
        "#313#11%{url}%-1%-1%-1%-1%-1%-1%-1%0%0%0%-1%-1%0%-1%0%-1%0%%"
        "#MobaFont%10%0%0%-1%15%236,236,236%30,30,30%180,180,192%0%-1%0%%xterm%-1%0%_Std_Colors_0_"
        "%80%24%0%1%-1%<none>%%0%1%-1%0%#0# #-1"
    ),
}

DEFAULT_PORTS = {"ssh": 22, "rdp": 3389, "telnet": 23, "http": 80, "https": 443}

SECTION_HEADER_RE = re.compile(r"^\[(Bookmarks(?:_\d+)?)\]\s*$")
SUBREP_RE = re.compile(r"^SubRep=(.*)$")


def load_config(path):
    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg


def netbox_get_all(session, url, endpoint, verify, params=None):
    """Page through a NetBox list endpoint and return all results."""
    results = []
    next_url = url.rstrip("/") + endpoint
    p = dict(params or {}, limit=200)
    while next_url:
        resp = session.get(next_url, params=p, verify=verify, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])
        next_url = data.get("next")
        p = None
    return results


def classify_service(service):
    """Return 'ssh' / 'rdp' / 'telnet' / 'http' / 'https' for a NetBox
    service, or None. 'https' is checked before 'http' since the substring
    'http' matches both."""
    name = (service.get("name") or "").strip().lower()
    if "ssh" in name:
        return "ssh"
    if "rdp" in name or "remote desktop" in name:
        return "rdp"
    if "telnet" in name:
        return "telnet"
    if "https" in name:
        return "https"
    if "http" in name:
        return "http"
    ports = service.get("ports") or []
    if 22 in ports:
        return "ssh"
    if 3389 in ports:
        return "rdp"
    if 23 in ports:
        return "telnet"
    if 443 in ports:
        return "https"
    if 80 in ports:
        return "http"
    return None


def services_by_parent(services, parent_object_type):
    """Group classifiable services by their parent object id, deduped by kind
    (first matching service of a given kind wins for a given parent)."""
    grouped = {}
    for svc in services:
        if svc.get("parent_object_type") != parent_object_type:
            continue
        kind = classify_service(svc)
        if kind is None:
            continue
        seen = grouped.setdefault(svc["parent_object_id"], {})
        seen.setdefault(kind, svc)
    return grouped


def service_items_for(name, site_name, ip, kinds, usernames):
    """Build one bookmark item per matched service kind. No name suffixing --
    each kind lands in its own protocol subfolder, so the folder itself
    disambiguates the connection type."""
    items = []
    for kind, svc in kinds.items():
        port = (svc.get("ports") or [DEFAULT_PORTS[kind]])[0]
        item = {
            "name": name,
            "site": site_name,
            "ip": ip,
            "port": port,
            "username": usernames.get(kind, ""),
            "kind": kind,
        }
        if kind in ("http", "https"):
            item["url"] = f"{kind}://{ip}" if port == DEFAULT_PORTS[kind] else f"{kind}://{ip}:{port}"
        items.append(item)
    return items


def build_device_items(devices, services, usernames, default_port, default_username):
    """Physical devices matched against their NetBox Services. A device with
    no matching service falls back to a default SSH bookmark so devices
    NetBox hasn't been tagged with services for don't disappear."""
    grouped = services_by_parent(services, "dcim.device")
    items = []
    for d in devices:
        primary_ip4 = d.get("primary_ip4")
        site = d.get("site")
        if not primary_ip4 or not site:
            continue
        ip = primary_ip4["address"].split("/")[0]
        kinds = grouped.get(d["id"])
        if kinds:
            items.extend(service_items_for(d["name"], site["name"], ip, kinds, usernames))
        else:
            items.append({
                "name": d["name"],
                "site": site["name"],
                "ip": ip,
                "port": default_port,
                "username": default_username,
                "kind": "ssh",
            })
    return items


def build_vm_items(vms, services, usernames):
    """Virtual machines matched against their NetBox Services. A VM with no
    matching service produces no bookmark (no IP-only fallback -- unlike
    devices, VMs aren't meaningfully reachable without a declared service)."""
    grouped = services_by_parent(services, "virtualization.virtualmachine")
    items = []
    for vm in vms:
        primary_ip4 = vm.get("primary_ip4")
        site = vm.get("site")
        kinds = grouped.get(vm["id"])
        if not primary_ip4 or not site or not kinds:
            continue
        ip = primary_ip4["address"].split("/")[0]
        items.extend(service_items_for(vm["name"], site["name"], ip, kinds, usernames))
    return items


PROTOCOL_LABELS = {"ssh": "SSH", "rdp": "RDP", "telnet": "Telnet", "http": "HTTP", "https": "HTTPS"}


def group_by_site_protocol(items):
    """Group items by (site, protocol-folder-name)."""
    grouped = {}
    for i in items:
        key = (i["site"], PROTOCOL_LABELS[i["kind"]])
        grouped.setdefault(key, []).append(i)
    return grouped


def find_bookmark_sections(lines):
    """Return list of section dicts with header_idx, name, subrep, body_end."""
    sections = []
    header_indices = [i for i, l in enumerate(lines) if SECTION_HEADER_RE.match(l)]
    all_headers = [i for i, l in enumerate(lines) if re.match(r"^\[.*\]\s*$", l)]

    for h in header_indices:
        name = SECTION_HEADER_RE.match(lines[h]).group(1)
        later = [x for x in all_headers if x > h]
        body_end = later[0] if later else len(lines)
        subrep = None
        for j in range(h + 1, body_end):
            m = SUBREP_RE.match(lines[j])
            if m:
                subrep = m.group(1).strip()
                break
        sections.append({"header_idx": h, "name": name, "subrep": subrep, "body_end": body_end})
    return sections, all_headers


def next_bookmarks_index(sections):
    used = [0]
    for s in sections:
        m = re.match(r"Bookmarks_(\d+)$", s["name"])
        if m:
            used.append(int(m.group(1)))
    return max(used) + 1


def remove_org_tree_sections(lines, org):
    """Remove every section belonging to the org's tree, at any depth
    (the org placeholder itself, every site placeholder, every protocol
    leaf). Safe because everything under org is 100% NetBox-derived --
    nothing manual ever lives there."""
    removed = []
    while True:
        sections, _ = find_bookmark_sections(lines)
        target = next(
            (s for s in sections
             if s["subrep"] == org or (s["subrep"] and s["subrep"].startswith(org + "\\"))),
            None,
        )
        if not target:
            break
        removed.append(target["subrep"])
        del lines[target["header_idx"]:target["body_end"]]
    return removed


def render(item):
    if item["kind"] in ("http", "https"):
        return TEMPLATES["browser"].format(url=item["url"])
    return TEMPLATES[item["kind"]].format(ip=item["ip"], port=item["port"], username=item["username"])


def rebuild_org_tree(lines, org, by_site_protocol, img_num):
    """Tear down and regenerate the org's entire bookmark tree as one
    contiguous, parent-before-child ordered block: org placeholder, then for
    each site its placeholder immediately followed by its protocol leaf
    sections. MobaXterm's tree builder needs parents declared before their
    children -- a previous incremental-append design left new ancestor
    sections trailing after their own children in the file, which produced a
    non-expanding empty org folder. Rebuilding fresh each run sidesteps that
    entirely and is safe since nothing manual ever lives under org."""
    removed = remove_org_tree_sections(lines, org)

    sections, _ = find_bookmark_sections(lines)
    insert_at = max(s["body_end"] for s in sections) if sections else len(lines)
    next_idx = next_bookmarks_index(sections)

    block = [f"[Bookmarks_{next_idx}]\n", f"SubRep={org}\n", f"ImgNum={img_num}\n", "\n"]
    next_idx += 1

    sites = sorted({site for site, _protocol in by_site_protocol})
    entries_written = 0
    for site in sites:
        block += [f"[Bookmarks_{next_idx}]\n", f"SubRep={org}\\{site}\n", f"ImgNum={img_num}\n", "\n"]
        next_idx += 1

        protocols = sorted(p for s, p in by_site_protocol if s == site)
        for protocol in protocols:
            items = by_site_protocol[(site, protocol)]
            block += [f"[Bookmarks_{next_idx}]\n", f"SubRep={org}\\{site}\\{protocol}\n", f"ImgNum={img_num}\n"]
            for item in items:
                block.append(f"{item['name']}={render(item)}\n")
                entries_written += 1
            block.append("\n")
            next_idx += 1

    lines[insert_at:insert_at] = block
    return {
        "sections_removed": len(removed),
        "sections_created": 1 + len(sites) + len(by_site_protocol),
        "entries_written": entries_written,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--apply", action="store_true",
                     help="write changes to MobaXterm.ini (default is dry-run)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    nb_url = cfg["netbox"]["url"]
    nb_token = cfg["netbox"]["token"]
    verify_ssl = cfg["netbox"].getboolean("verify_ssl", fallback=True)
    ca_bundle = cfg["netbox"].get("ca_bundle", "").strip()
    verify = ca_bundle if (verify_ssl and ca_bundle) else verify_ssl

    ini_path = Path(cfg["moba"]["ini_path"])
    org = cfg["moba"]["organization"]
    img_num = cfg["moba"].get("img_num", "41")
    default_port = cfg["moba"].get("ssh_port", "22")
    default_username = cfg["moba"].get("ssh_username", "")
    usernames = {
        "ssh": cfg["moba"].get("ssh_username", ""),
        "rdp": cfg["moba"].get("rdp_username", ""),
        "telnet": cfg["moba"].get("telnet_username", ""),
    }

    session = requests.Session()
    session.headers.update({"Authorization": f"Token {nb_token}"})

    print(f"Fetching devices, VMs, and services from {nb_url} ...")
    devices = netbox_get_all(session, nb_url, "/api/dcim/devices/", verify,
                              {"has_primary_ip": "true"})
    vms = netbox_get_all(session, nb_url, "/api/virtualization/virtual-machines/", verify)
    services = netbox_get_all(session, nb_url, "/api/ipam/services/", verify)

    device_items = build_device_items(devices, services, usernames, default_port, default_username)
    print(f"  {len(device_items)} device bookmarks ({len(devices)} devices with a primary IP).")

    vm_items = build_vm_items(vms, services, usernames)
    print(f"  {len(vm_items)} VM bookmarks from SSH/RDP/Telnet services.")

    all_items = device_items + vm_items
    by_site_protocol = group_by_site_protocol(all_items)
    sites = {site for site, _ in by_site_protocol}
    print(f"Total: {len(all_items)} bookmarks across {len(sites)} sites, "
          f"{len(by_site_protocol)} site/protocol folders.")

    original = ini_path.read_text(encoding="utf-8-sig")
    lines = original.splitlines(keepends=True)
    summary = rebuild_org_tree(lines, org, by_site_protocol, img_num)
    updated = "".join(lines)

    print(f"Sections removed (rebuilt fresh): {summary['sections_removed']}")
    print(f"Sections created:                 {summary['sections_created']}")
    print(f"Entries written:                  {summary['entries_written']}")

    if updated == original:
        print("No changes needed.")
        return

    if not args.apply:
        print("\nDry run only -- rerun with --apply to write changes.")
        return

    backup = ini_path.with_suffix(ini_path.suffix + f".bak.{datetime.datetime.now():%Y%m%d%H%M%S}")
    backup.write_text(original, encoding="utf-8-sig")
    ini_path.write_text(updated, encoding="utf-8-sig")
    print(f"\nBackup written to {backup}")
    print(f"Updated {ini_path}")


if __name__ == "__main__":
    sys.exit(main())
