# Parkridge HOA Extract - one-command setup + run (Windows)
# Safe to run every time: installs what's missing, then processes packets.

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=================================================="
Write-Host "  Parkridge HOA Extract"
Write-Host "=================================================="

# ---------- helper: add a dir to this session's PATH if it exists ----------
function Add-ToPath($dir) {
    if ((Test-Path $dir) -and ($env:Path -notlike "*$dir*")) {
        $env:Path = "$env:Path;$dir"
    }
}

# ---------- helper: pick up PATH changes made by installers ----------
function Refresh-Path {
    $machine = [System.Environment]::GetEnvironmentVariable("Path","Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("Path","User")
    $env:Path = "$machine;$user"
    # known install locations (installers don't always register these immediately)
    Add-ToPath "C:\Program Files\Tesseract-OCR"
    Add-ToPath "${env:LOCALAPPDATA}\Programs\Tesseract-OCR"
    foreach ($p in @(
        "C:\Program Files\poppler*\Library\bin",
        "C:\Program Files\poppler*\bin",
        "${env:LOCALAPPDATA}\Microsoft\WinGet\Packages\oschwartz10612.Poppler*\poppler*\Library\bin"
    )) {
        Get-ChildItem -Path $p -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Add-ToPath $_.FullName }
    }
}

function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Refresh-Path

# ---------- check winget exists ----------
if (-not (Have "winget")) {
    Write-Host ""
    Write-Host "  This needs 'winget' (App Installer), which is missing."
    Write-Host "  Install 'App Installer' from the Microsoft Store, then re-run this command."
    Write-Host ""
    Read-Host "  Press Return to close"
    exit 1
}

# ---------- install Python if missing ----------
if (-not (Have "python")) {
    Write-Host ""
    Write-Host "  Installing Python..."
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    Refresh-Path
}

# ---------- install Tesseract if missing ----------
if (-not (Have "tesseract")) {
    Write-Host ""
    Write-Host "  Installing Tesseract (reads scanned packets)..."
    winget install -e --id UB-Mannheim.TesseractOCR --accept-source-agreements --accept-package-agreements
    Refresh-Path
}

# ---------- install Poppler if missing ----------
if (-not (Have "pdftotext")) {
    Write-Host ""
    Write-Host "  Installing Poppler (reads PDFs)..."
    winget install -e --id oschwartz10612.Poppler --accept-source-agreements --accept-package-agreements
    Refresh-Path
}

# ---------- verify tools are reachable ----------
$missing = @()
foreach ($t in @("python","tesseract","pdftotext")) {
    if (-not (Have $t)) { $missing += $t }
}
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "=================================================="
    Write-Host "  Almost there - Windows needs a restart of this window."
    Write-Host ""
    Write-Host "  Not yet found: $($missing -join ', ')"
    Write-Host ""
    Write-Host "  Please CLOSE this PowerShell window, open a new one,"
    Write-Host "  and paste the same command again. This only happens once."
    Write-Host "=================================================="
    Write-Host ""
    Read-Host "  Press Return to close"
    exit 1
}

# ---------- set up working folder ----------
$WorkDir = Join-Path $env:USERPROFILE "Parkridge"
$Packets = Join-Path $WorkDir "packets"
New-Item -ItemType Directory -Force -Path $Packets | Out-Null

# ---------- download the latest analysis script ----------
Write-Host ""
Write-Host "  Getting the latest analysis script..."
try {
    Invoke-WebRequest -UseBasicParsing `
        -Uri "https://raw.githubusercontent.com/Egbert-Azure/HOA-Extract/main/hoa_extract.py" `
        -OutFile (Join-Path $WorkDir "hoa_extract.py")
} catch {
    Write-Host "  Could not download the script. Check internet and re-run."
    Read-Host "  Press Return to close"
    exit 1
}

# ---------- any packets? ----------
$files = @(Get-ChildItem -Path $Packets -File -ErrorAction SilentlyContinue)
if ($files.Count -eq 0) {
    Write-Host ""
    Write-Host "=================================================="
    Write-Host "  SETUP DONE - no packets yet."
    Write-Host ""
    Write-Host "  1. A folder just opened: Parkridge > packets"
    Write-Host "  2. Drop your TMT packet files (PDF or zip) in there"
    Write-Host "  3. Paste this same command again to build reports"
    Write-Host "=================================================="
    Start-Process explorer.exe $Packets
    Read-Host "  Press Return to close"
    exit 0
}

# ---------- run analysis ----------
Write-Host ""
Write-Host "  Found $($files.Count) packet(s). Analyzing..."
Set-Location $WorkDir
$packetPaths = $files | ForEach-Object { $_.FullName }
python (Join-Path $WorkDir "hoa_extract.py") @packetPaths

# ---------- convert reports to readable HTML ----------
$OutDir = Join-Path $WorkDir "hoa_extract_out"
$converter = Join-Path $WorkDir "_convert.py"
Invoke-WebRequest -UseBasicParsing `
    -Uri "https://raw.githubusercontent.com/Egbert-Azure/HOA-Extract/main/md_to_html.py" `
    -OutFile $converter
python $converter $OutDir

Write-Host ""
Write-Host "=================================================="
Write-Host "  DONE - opening your reports."
Write-Host "  (also saved in $OutDir)"
Write-Host "=================================================="
Start-Process (Join-Path $OutDir "index.html")
Start-Process explorer.exe $OutDir
Read-Host "  Press Return to close"
