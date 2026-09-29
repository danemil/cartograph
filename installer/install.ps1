<#
.SYNOPSIS
    Install Cartograph for the CLI hosts on a Windows machine with no VS Code.

.DESCRIPTION
    The Windows twin of install.sh. Same contract: it downloads nothing, and
    -Payload names a payload directory or a .vsix already on the machine.

    UNVERIFIED. Written from the same payload contract install.sh implements,
    but never run — this project has no Windows machine and no Windows build of
    the engine (PyInstaller freezes the interpreter it runs on, so a win32-x64
    payload has to be built on Windows). Treat it as a starting point that has
    read the contract, not as a tested script.

.EXAMPLE
    .\install.ps1 -Payload ..\dist\payload\win32-x64 -Repo C:\work\app
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Payload,
    [string]$Repo,
    [string]$CartoHome = (Join-Path $HOME ".cartograph")
)

$ErrorActionPreference = "Stop"
$self = Split-Path -Parent $MyInvocation.MyCommand.Path
$staging = $null

try {
    if ($Payload -match '\.(vsix|zip)$') {
        # A .vsix is a zip whose payload sits under extension/, so the same
        # file serves both hosts.
        $staging = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([guid]::NewGuid()))
        Expand-Archive -Path $Payload -DestinationPath $staging -Force
        $Payload = Join-Path $staging "extension\payload"
    }

    if (-not (Test-Path (Join-Path $Payload "PAYLOAD.json"))) {
        throw "$Payload has no PAYLOAD.json - is it a Cartograph payload?"
    }

    Write-Host "Installing into $CartoHome"
    $bin = Join-Path $CartoHome "bin"
    New-Item -ItemType Directory -Force -Path $bin | Out-Null

    $installed = Join-Path $CartoHome "payload"
    if (Test-Path $installed) { Remove-Item -Recurse -Force $installed }
    Copy-Item -Recurse -Force $Payload $installed

    Set-Content -Path (Join-Path $CartoHome "runtime.path") -Value $installed -NoNewline

    # Both names: the hook lines `carto install` writes guard on `cartograph`
    # being resolvable and then invoke `carto`.
    foreach ($name in @("carto.cmd", "cartograph.cmd")) {
        Copy-Item -Force (Join-Path $self "launcher\carto.cmd") (Join-Path $bin $name)
    }

    & (Join-Path $bin "carto.cmd") --version

    if ($Repo) {
        Write-Host "Placing the skills pack in $Repo"
        & (Join-Path $bin "carto.cmd") install --platform copilot --no-instructions -y --repo $Repo
    }

    if (($env:PATH -split ';') -notcontains $bin) {
        Write-Host ""
        Write-Host "One step left: $bin is not on your PATH."
        Write-Host "Add it for your user account with:"
        Write-Host ""
        Write-Host "    [Environment]::SetEnvironmentVariable('PATH', `"$bin;`$env:PATH`", 'User')"
    }
}
finally {
    if ($staging) { Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue }
}
