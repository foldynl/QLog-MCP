# Run with PowerShell 7.4 or newer on Windows x64.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true

$projectRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
Push-Location $projectRoot
try {
    uv sync --locked --no-dev --python 3.12 --reinstall-package qlog-mcp
    $version = uv run --locked --no-dev python versioning.py
    $name = "qlog-mcp-$version-windows-x86_64"

    # Preserve locked application dependencies and distribution metadata used at runtime.
    uv run --locked --no-dev --with pyinstaller==6.22.3 pyinstaller `
        --noconfirm --clean --onefile --console `
        --name $name `
        --distpath dist --workpath build/windows --specpath build/windows `
        --recursive-copy-metadata qlog-mcp `
        packaging/windows/entrypoint.py

    $artifact = Join-Path "dist" "$name.exe"
    uv run --locked --no-dev python packaging/windows/smoke_test.py $artifact $version

    $hash = (Get-FileHash $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $name.exe" | Set-Content "$artifact.sha256" -Encoding utf8NoBOM
    Write-Host "Windows EXE written to $artifact"
}
finally {
    Pop-Location
}
