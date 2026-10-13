$ErrorActionPreference='SilentlyContinue'
Write-Output "=== netcon established connections ==="
Get-NetTCPConnection | Where-Object { $_.LocalPort -eq 2121 -or $_.RemotePort -eq 2121 } | Select-Object State,LocalAddress,LocalPort,RemoteAddress,RemotePort,OwningProcess | Format-Table -AutoSize | Out-String
Write-Output "=== director before kill ==="
Get-Process -Id 49508 -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime | Format-List | Out-String
Stop-Process -Id 49508 -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Write-Output "=== director after kill (should be empty) ==="
Get-Process -Id 49508 -ErrorAction SilentlyContinue | Select-Object Id,ProcessName | Format-List | Out-String
Write-Output "=== any node.exe left ==="
Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Select-Object ProcessId,CommandLine | Format-List | Out-String
Write-Output "=== iso_f12 / s2iso tasks ==="
schtasks /query /fo LIST 2>&1 | Select-String -Pattern 'iso_f12|s2iso|iso_rc|CS2Launch' | Out-String
Write-Output "done"
