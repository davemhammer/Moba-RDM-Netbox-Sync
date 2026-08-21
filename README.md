# netbox-moba-sync

Syncs devices and VMs from NetBox into MobaXterm bookmarks and Devolutions
Remote Desktop Manager (RDM) sessions. Everything is derived from NetBox on
each run — nothing here should be hand-edited on the MobaXterm/RDM side,
since both syncs treat NetBox as the source of truth.

## Folder structure produced

Both syncs organize into the same tree:

```
<Organization>\<Site>\<Protocol>\<Device or VM name>
```

e.g. `Acme\Site A\SSH\web01`

Protocol is derived from each device/VM's NetBox Services (`ipam.services`):
a service named/ported like SSH, RDP, Telnet, HTTP, or HTTPS produces a
bookmark/session in the matching folder. An item with two matching service
kinds (e.g. both SSH and RDP) gets an entry in each protocol folder.

## Setup

1. Copy `config.ini.example` to `config.ini` and fill in:
   - `[netbox]` — your NetBox URL and API token
   - `[moba]` — path to `MobaXterm.ini`, the organization name, default
     ports/usernames per protocol
2. If your NetBox server's TLS cert doesn't verify cleanly (missing
   intermediate cert, common with some CA-issued certs), build a combined CA
   bundle and point `ca_bundle` in `config.ini` at it:
   ```
   python -c "import certifi,shutil; shutil.copy(certifi.where(),'ca_bundle.pem')"
   # then append your server's missing intermediate cert to ca_bundle.pem
   ```
   `godaddy_g2_intermediate.pem` is already included as an example — this
   repo's NetBox instance uses a GoDaddy cert missing its intermediate.

## Scripts

### `netbox_moba_sync.py` — MobaXterm bookmarks

```
python netbox_moba_sync.py --config config.ini            # dry run
python netbox_moba_sync.py --config config.ini --apply    # writes changes
```

- Close MobaXterm before running with `--apply` — it rewrites its own ini on
  exit and would clobber the sync.
- Every run **fully rebuilds** the organization's bookmark tree (every
  section whose `SubRep` is the org name or starts with `org\`) in one
  contiguous, correctly parent-before-child ordered block, then writes it
  back. This is safe because everything under the org folder is 100%
  NetBox-derived — nothing manual should ever be added there. Anything
  outside the org's tree (your own hand-made bookmarks, other folders) is
  never touched.
- Always backs up `MobaXterm.ini` (timestamped `.bak`) before writing.
- Devices with no matching NetBox service still get a bookmark: they fall
  back to a default SSH bookmark on their primary IP (`ssh_port`/
  `ssh_username` from config) so devices NetBox hasn't been fully tagged
  with services for don't silently disappear. VMs have no such fallback — a
  VM with no matching service produces no bookmark.

### `netbox_export_items.py` — shared item export

```
python netbox_export_items.py --config config.ini > items.json
```

Exports the same NetBox-derived item list (device/VM name, site, IP, port,
username, protocol kind, target folder) as JSON. Used internally by
`netbox_rdm_sync.ps1`; also handy on its own for inspecting what a sync
would produce.

### `netbox_rdm_sync.ps1` — Devolutions RDM sessions

```
pwsh -File .\netbox_rdm_sync.ps1            # dry run
pwsh -File .\netbox_rdm_sync.ps1 -Apply     # writes changes
```

#### One-time RDM setup

RDM's PowerShell automation has two hard requirements that aren't obvious
up front, both confirmed the hard way (live testing, not docs):

1. **You need PowerShell 7 and the `Devolutions.PowerShell` module** — not
   the older `RemoteDesktopManager` module. That module is tied to RDM
   releases roughly 2022.3 and earlier; against a current RDM install
   (2026.x) its session cmdlets (`Get-RDMSession`, `Get-RDMVault`, etc.)
   fail outright with errors like `Connection not found`, regardless of
   data source type or vault state. Check your installed RDM app's actual
   version (`(Get-Process RemoteDesktopManager).Path | Get-Item |
   Select VersionInfo`) and match the module generation to it.
   ```powershell
   winget install Microsoft.PowerShell
   pwsh -Command "Install-Module Devolutions.PowerShell -Scope CurrentUser"
   ```

2. **Your RDM data source must be an "advanced" type — SQLite, SQL Server,
   or Devolutions Server/Hub-backed — not the default local Xml type.**
   PowerShell session management (`Get-RDMSession`, `Get-RDMVault`, etc.)
   simply isn't supported against a plain Xml data source; you'll hit
   `Get-RDMVault : Command available with an advanced data source only`
   and `Get-RDMSession : Connection not found`. SQLite is the easiest fix:
   still fully local and free, just a different file format with real
   query/connection support.

   In the RDM desktop app:
   - **File → Data Sources → New Data Source → SQLite**
   - Give it a name (the script defaults to expecting one named `Local` —
     pass `-DataSourceName` to override) and a file location, then Create
   - Migrate any existing entries from your old Xml data source into it
     (drag-and-drop between data sources in the tree, or Export from the
     old source / Import into the new one)
   - Set the new SQLite source as current/default if you want it to be
     what RDM opens by default

3. **Run the script interactively the first time.** If the data source is
   password-protected, RDM needs to prompt you for the master key —
   automation tooling that forces non-interactive PowerShell (scheduled
   tasks, some CI runners, some agent harnesses) will fail with
   `The entered master key is invalid` / `NonInteractive mode... Read and
   Prompt functionality is not available` even with a correct key, because
   the prompt itself can't be shown. Run it yourself in a normal `pwsh`
   window first; later scheduled/unattended runs work fine once you're
   past that (or if the data source has no master key set).

#### Behavior

- `-DataSourceName` defaults to `Local` — override if your data source is
  named differently.
- RDM folders are literal session entries with `Type=Group`. The script
  creates any missing ancestor groups (org, org\site, org\site\protocol)
  before creating leaf sessions, since RDM (like MobaXterm) requires each
  folder level to exist explicitly.
- Existing sessions are matched by Name + Group and updated in place;
  nothing is ever deleted.
- Verified field mapping (confirmed by live round-trip testing against a
  real RDM data source, not guessed from docs): SSH/RDP/Telnet use `Host` +
  `CustomPort` + `HostUserName`; HTTP/HTTPS use `Host` + `Url`. Type strings:
  `SSHShell`, `RDPConfigured`, `Telnet`, `WebBrowser`.

## Typical workflow

1. Tag devices/VMs in NetBox with the services they actually run (`SSH`,
   `RDP`, `Telnet`, `HTTP`, `HTTPS` on `ipam.services`), whether by hand or
   with your own bulk-tagging tooling.
2. Run `netbox_moba_sync.py --apply` to update MobaXterm.
3. Run `netbox_rdm_sync.ps1 -Apply` to update RDM.
4. Re-run either anytime — both are idempotent and safe to schedule.

## License

MIT — see [LICENSE](LICENSE).
