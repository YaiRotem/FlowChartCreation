@echo off
REM FlowChartCreation - verify that the files are intact (SHA-256 against CHECKSUMS.sha256)
cd /d "%~dp0"
if not exist CHECKSUMS.sha256 (
    echo CHECKSUMS.sha256 is missing - extract the whole ZIP again.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$bad=0; Get-Content -Encoding UTF8 'CHECKSUMS.sha256' | Where-Object { $_ -match '^[0-9a-f]{64}\s+\S' } | ForEach-Object {" ^
  "  $h,$f = $_ -split '\s+',2; $f=$f.Trim();" ^
  "  if (-not (Test-Path -LiteralPath $f)) { Write-Host ('MISSING  ' + $f) -ForegroundColor Red; $bad++; return }" ^
  "  $a=(Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash.ToLower();" ^
  "  if ($a -eq $h) { Write-Host ('OK       ' + $f) -ForegroundColor Green } else { Write-Host ('DAMAGED  ' + $f + '   (download/extract it again)') -ForegroundColor Red; $bad++ } };" ^
  "if ($bad) { Write-Host ''; Write-Host ($bad.ToString() + ' file(s) failed the check.') -ForegroundColor Red; exit 1 } else { Write-Host ''; Write-Host 'All files are intact.' -ForegroundColor Green }"
echo.
pause
