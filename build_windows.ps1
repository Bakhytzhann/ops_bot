param(
    [switch]$OneFile,
    [switch]$UseProjectData
)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create the .venv environment and install requirements-windows.txt first.'
}

$mode = if ($OneFile) { 'onefile' } else { 'onedir' }
$dist = Join-Path $project "dist\$mode"
$build = Join-Path $project "build\$mode"
$staging = Join-Path $build 'bundle'
$icon = Join-Path $project 'desktop\ops_bot.ico'
$countries = Join-Path $project 'services\iso_countries.json'

Push-Location $project
try {
    & $python -m PyInstaller --noconfirm --windowed "--$mode" --name 'OPS Bot' `
        --distpath $staging --workpath $build --specpath $build --icon $icon `
        --add-data "${icon}:desktop" --add-data "${countries}:services" `
        --collect-data tzdata --collect-data airportsdata ops_manager.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    $bundle = if ($OneFile) { $dist } else { Join-Path $dist 'OPS Bot' }
    $built = if ($OneFile) { $staging } else { Join-Path $staging 'OPS Bot' }
    New-Item -ItemType Directory -Path $bundle -Force | Out-Null
    # PyInstaller can replace its own staging directory. Copy only its runtime files
    # to the installed folder, where users may also keep their data and configuration.
    if (-not $OneFile) {
        Copy-Item -LiteralPath (Join-Path $built '_internal') -Destination $bundle -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $built 'OPS Bot.exe') -Destination $bundle -Force
    if ($UseProjectData) {
        Set-Content -LiteralPath (Join-Path $bundle 'ops-bot-home.txt') -Value $project -Encoding UTF8
    }
    Write-Output (Join-Path $bundle 'OPS Bot.exe')
} finally {
    Pop-Location
}
