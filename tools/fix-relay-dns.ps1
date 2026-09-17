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
# Safe to run more than once. Undo by deleting the line it adds, or by
# running this with -Remove.
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

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "This needs Administrator. Right-click PowerShell -> Run as administrator, then run it again." -ForegroundColor Red
    exit 1
}

$lines = @(Get-Content $hosts -ErrorAction SilentlyContinue)
$kept = $lines | Where-Object { $_ -notmatch [regex]::Escape($Name) }

if ($Remove) {
    Set-Content -Path $hosts -Value $kept -Encoding ascii
    Write-Host "Removed the pin for $Name." -ForegroundColor Yellow
} else {
    $kept += "$Address`t$Name`t$marker"
    Set-Content -Path $hosts -Value $kept -Encoding ascii
    Write-Host "Pinned $Name -> $Address" -ForegroundColor Green
}

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
