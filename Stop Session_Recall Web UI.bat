@echo off
echo Stopping Session_Recall Web UI (port 8765)...
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue; if ($c) { $c.OwningProcess | Sort-Object -Unique | ForEach-Object { try { Stop-Process -Id $_ -Force; Write-Host ('Stopped process ' + $_) } catch {} } } else { Write-Host 'Not running.' }"
timeout /t 2 >nul
