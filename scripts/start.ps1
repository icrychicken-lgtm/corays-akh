# Startet Bot + Dashboard unter Windows (ohne Docker). Neustart bei Exit-Code 3 (Owner-Panel → Neustart).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".env")) {
    Write-Host ".env fehlt - kopiere .env.example nach .env und fuelle die Werte aus." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install -q -r requirements.txt

while ($true) {
    & .\.venv\Scripts\python.exe -m app
    if ($LASTEXITCODE -eq 3) {
        Write-Host "Neustart angefordert ..." -ForegroundColor Cyan
        Start-Sleep -Seconds 2
        continue
    }
    Write-Host "Beendet mit Code $LASTEXITCODE"
    exit $LASTEXITCODE
}
