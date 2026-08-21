<#
Copyright (c) 2026 David Hammer. Licensed under the MIT License (see
LICENSE in the repository root).

.SYNOPSIS
    Sync NetBox devices/VMs into Devolutions Remote Desktop Manager as
    sessions under Group "<Org>\<Site>\<Protocol>", mirroring the MobaXterm
    sync's folder structure.

.DESCRIPTION
    Requires PowerShell 7 and the Devolutions.PowerShell module (NOT the
    legacy "RemoteDesktopManager" module -- that one is version-mismatched
    against current RDM releases and its session cmdlets don't work at all
    against a local Xml or SQLite data source; confirmed via live testing).

    Must be run in an INTERACTIVE session the first time against a
    password-protected data source, so RDM can prompt for the master key.

    Pulls the item list by shelling out to netbox_export_items.py (reuses
    the exact NetBox-fetch/service-matching logic as the MobaXterm sync).
    Every RDM "folder" is a real session entry with Type=Group, so -- same
    as MobaXterm's SubRep sections -- every ancestor level (org, org\site,
    org\site\protocol) needs its own Group entry before leaf sessions can
    reference it. Existing sessions are matched by Name+Group and updated
    in place; nothing is ever deleted.

    Verified field names (live-tested against a real SQLite data source,
    not guessed): CustomPort, HostUserName, Url. Type strings verified:
    SSHShell, RDPConfigured, Telnet, WebBrowser.

.PARAMETER ConfigPath
    Path to config.ini (same file the MobaXterm sync uses for NetBox
    credentials). Defaults to config.ini next to this script.

.PARAMETER DataSourceName
    Name of the RDM data source to sync into. Defaults to "Local".

.PARAMETER Apply
    Without this switch, the script only prints what it *would* do (dry
    run). Pass -Apply to actually create/update sessions in RDM.

.EXAMPLE
    pwsh -File .\netbox_rdm_sync.ps1
    pwsh -File .\netbox_rdm_sync.ps1 -Apply
#>

param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "config.ini"),
    [string]$DataSourceName = "Local",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw "This script requires PowerShell 7 (Devolutions.PowerShell needs it). Run: pwsh -File $PSCommandPath"
}

Import-Module Devolutions.PowerShell -ErrorAction Stop

$typeMap = @{
    ssh    = "SSHShell"
    rdp    = "RDPConfigured"
    telnet = "Telnet"
    http   = "WebBrowser"
    https  = "WebBrowser"
}

Write-Host "Exporting items from NetBox via netbox_export_items.py ..."
$exportScript = Join-Path $PSScriptRoot "netbox_export_items.py"
$json = & python $exportScript --config $ConfigPath
if ($LASTEXITCODE -ne 0) {
    throw "netbox_export_items.py failed (exit $LASTEXITCODE)"
}
$items = $json | ConvertFrom-Json
Write-Host "  $($items.Count) items."

$ds = Get-RDMDataSource | Where-Object Name -eq $DataSourceName
if (-not $ds) {
    throw "Data source '$DataSourceName' not found. Available: $((Get-RDMDataSource).Name -join ', ')"
}
Set-RDMCurrentDataSource -DataSource $ds
Write-Host "Using data source '$DataSourceName' -- this may prompt for the master key the first time."

$existingSessions = Get-RDMSession
Write-Host "  $($existingSessions.Count) existing entries visible."
$existingGroups = $existingSessions | Where-Object ConnectionType -eq "Group" | ForEach-Object { $_.Group }

function Ensure-Group {
    param([string]$FullPath, [string]$ParentPath, [string]$LeafName)
    if ($existingGroups -contains $FullPath) {
        return $false
    }
    if (-not $Apply) {
        Write-Host "[dry-run] would create group: $FullPath"
        $script:existingGroups += $FullPath
        return $true
    }
    $g = if ($ParentPath) {
        New-RDMSession -Name $LeafName -Type "Group" -Group $ParentPath
    } else {
        New-RDMSession -Name $LeafName -Type "Group"
    }
    Set-RDMSession $g | Out-Null
    $script:existingGroups += $FullPath
    return $true
}

# Ancestor groups: org, org\site, org\site\protocol -- each needs its own
# entry, parent-first, same requirement MobaXterm had.
$groupsCreated = 0
$orgName = ($items[0].group -split '\\')[0]
if (Ensure-Group -FullPath $orgName -ParentPath $null -LeafName $orgName) { $groupsCreated++ }

$sitePaths = $items | ForEach-Object { ($_.group -split '\\')[0..1] -join '\' } | Sort-Object -Unique
foreach ($sitePath in $sitePaths) {
    $parts = $sitePath -split '\\'
    if (Ensure-Group -FullPath $sitePath -ParentPath $parts[0] -LeafName $parts[1]) { $groupsCreated++ }
}

$protocolPaths = $items | ForEach-Object { $_.group } | Sort-Object -Unique
foreach ($protocolPath in $protocolPaths) {
    $parts = $protocolPath -split '\\'
    $parentPath = $parts[0..1] -join '\'
    if (Ensure-Group -FullPath $protocolPath -ParentPath $parentPath -LeafName $parts[2]) { $groupsCreated++ }
}

Write-Host "Groups created: $groupsCreated"

# Leaf sessions
$created = 0
$updated = 0
$skipped = 0

foreach ($item in $items) {
    $type = $typeMap[$item.kind]
    if (-not $type) {
        Write-Warning "No RDM type mapping for kind '$($item.kind)' ($($item.name)), skipping."
        $skipped++
        continue
    }
    $hostValue = if ($item.kind -in @("http", "https")) { $item.url } else { $item.ip }

    $existing = $existingSessions | Where-Object { $_.Name -eq $item.name -and $_.Group -eq $item.group -and $_.ConnectionType -ne "Group" }

    if (-not $Apply) {
        $verb = if ($existing) { "update" } else { "create" }
        Write-Host "[dry-run] would $verb`: $($item.group)\$($item.name) ($type -> $hostValue)"
        continue
    }

    try {
        if ($existing) {
            $session = $existing
        } else {
            $session = New-RDMSession -Name $item.name -Host $hostValue -Group $item.group -Type $type
        }

        $session.Host = $hostValue

        if ($item.kind -in @("http", "https")) {
            $session.Url = $item.url
        } else {
            $session.CustomPort = $item.port
            if ($item.username) {
                $session.HostUserName = $item.username
            }
        }

        Set-RDMSession $session | Out-Null

        if ($existing) { $updated++ } else { $created++ }
    } catch {
        Write-Warning "Failed on $($item.group)\$($item.name): $($_.Exception.Message)"
        $skipped++
    }
}

Write-Host ""
Write-Host "Created: $created"
Write-Host "Updated: $updated"
Write-Host "Skipped/failed: $skipped"
if (-not $Apply) {
    Write-Host ""
    Write-Host "Dry run only -- rerun with -Apply to write changes."
}
