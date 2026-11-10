@echo off
REM FlowChartCreation - recalculate CHECKSUMS.sha256 after you edited a file on purpose
REM (for example the ABOUT block at the top of FlowChartCreation.html).
cd /d "%~dp0"
echo This records the CURRENT files as the correct ones.
echo Only run it after changes you made yourself - not to "fix" a damaged download.
echo.
set /p ok=Continue? (Y/N) 
if /i not "%ok%"=="Y" exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = (Get-Location).Path; $files = @(Get-ChildItem -File) + @(if (Test-Path docs) { Get-ChildItem -File -Path docs }) | Where-Object { $_.Name -ne 'CHECKSUMS.sha256' -and $_.Name -notlike '*.bak' } | Sort-Object FullName;" ^
  "$lines = foreach ($f in $files) { $rel = $f.FullName.Substring($root.Length + 1).Replace('\','/'); (Get-FileHash -Algorithm SHA256 -LiteralPath $f.FullName).Hash.ToLower() + '  ' + $rel };" ^
  "[System.IO.File]::WriteAllText((Join-Path (Get-Location) 'CHECKSUMS.sha256'), (($lines -join \"`n\") + \"`n\"), (New-Object System.Text.UTF8Encoding $false));" ^
  "Write-Host ('Updated CHECKSUMS.sha256 for ' + $files.Count + ' files.') -ForegroundColor Green"
echo.
echo Now run verify_files.bat - every line should say OK.
pause
