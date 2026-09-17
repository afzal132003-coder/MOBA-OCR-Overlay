# Pins the relay's name to its address, so duckdns being down cannot take
# the show off air.
#
# The relay must be reached by NAME -- the TLS certificate is issued to
# titanium-relay.duckdns.org, so an IP in the URL fails validation
# instead. That makes every connection depend on duckdns resolving, and
# twice now it has not: eight lookups in a row failing while the VM was
# perfectly reachable on its address the whole time.
#
# A hosts entry keeps the name (so the certificate still matches) and
# skips the lookup. Verified: connecting to the IP while presenting the
# name validates against the real certificate.
#
# Safe to run more than once. Undo with -Remove.
#
# RUN AS ADMINISTRATOR -- the hosts file is not writable otherwise.

param(
    [string]$Name = "titanium-relay.duckdns.org",
    [string]$Address = "14.192.16.223",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$hosts = "$env:SystemRoot\System32\drivers\etc\hosts"
$marker = "# MOBA-OCR-Overlay relay"

# What Windows ships. Used only to rebuild a hosts file that has been
# emptied -- an earlier version of this script wrote with Set-Content,
# which opens the file (truncating it) BEFORE it can fail, and left a
# zero-byte hosts file behind when it did. Everything in it is comments,
# so nothing stops working, but an empty system file invites a much
# worse afternoon later.
$default = @(
    "# Copyright (c) 1993-2009 Microsoft Corp."
    "#"
    "# This is a sample HOSTS file used by Microsoft TCP/IP for Windows."
    "#"
    "# This file contains the mappings of IP addresses to host names. Each"
    "# entry should be kept on an individual line. The IP address should"
    "# be placed in the first column followed by the corresponding host name."
    "# The IP address and the host name should be separated by at least one"
    "# space."
    "#"
    "# Additionally, comments (such as these) may be inserted on individual"
    "# lines or following the machine name denoted by a '#' symbol."
    "#"
    "# For example:"
    "#"
    "#      102.54.94.97     rhino.acme.com          # source server"
    "#       38.25.63.10     x.acme.com              # x client host"
    ""
    "# localhost name resolution is handled within DNS itself."
    "#	127.0.0.1       localhost"
    "#	::1             localhost"
    ""
)

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This needs Administrator. Right-click PowerShell -> Run as administrator, then run it again." -ForegroundColor Red
    exit 1
}

# Read with .NET rather than Get-Content: Get-Content is lazy and can
# still hold the file open when the write starts, which is how the
# truncation happened.
$existing = @()
if ((Test-Path $hosts) -and ((Get-Item $hosts).Length -gt 0)) {
    $existing = [System.IO.File]::ReadAllLines($hosts)
} else {
    Write-Host "hosts was empty -- restoring the stock Windows contents." -ForegroundColor Yellow
    $existing = $default
}

$kept = @($existing | Where-Object { $_ -notmatch [regex]::Escape($Name) })

if ($Remove) {
    $final = $kept
} else {
    $final = $kept + "$Address`t$Name`t$marker"
}

if ($final.Count -eq 0) {
    Write-Host "Refusing to write an empty hosts file." -ForegroundColor Red
    exit 1
}

# Written to a temp file and moved into place, so a failure anywhere
# above leaves the real file untouched rather than half-written.
$tmp = "$hosts.new"
[System.IO.File]::WriteAllLines($tmp, [string[]]$final, [System.Text.Encoding]::ASCII)
Move-Item -Path $tmp -Destination $hosts -Force

if ($Remove) {
    Write-Host "Removed the pin for $Name." -ForegroundColor Yellow
} else {
    Write-Host "Pinned $Name -> $Address" -ForegroundColor Green
}
Write-Host "hosts is now $((Get-Item $hosts).Length) bytes."

ipconfig /flushdns | Out-Null
Write-Host "DNS cache flushed."

# Prove it, rather than assume it.
try {
    $resolved = [System.Net.Dns]::GetHostAddresses($Name) |
        ForEach-Object { $_.IPAddressToString }
    Write-Host "$Name now resolves to: $($resolved -join ', ')"
} catch {
    Write-Host "$Name still does not resolve: $_" -ForegroundColor Red
}
