$ErrorActionPreference='SilentlyContinue'
Write-Output "=== powershell/python/node/wscript procs (cmdline) ==="
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'powershell|python|node|wscript|cmd' } | Select-Object ProcessId,Name,CreationDate,CommandLine | Format-List | Out-String
Write-Output "=== ssh/sshd connections ==="
Get-NetTCPConnection -LocalPort 22 -State Established | Select-Object LocalAddress,RemoteAddress,RemotePort,OwningProcess | Format-Table -AutoSize | Out-String
Write-Output "=== iso_host.lock ==="
Get-Content C:\iso_build\iso_host.lock
Write-Output "=== files in C:\iso_build modified in last 30 min ==="
Get-ChildItem C:\iso_build -Recurse -File | Where-Object { $_.LastWriteTime -gt (Get-Date).AddMinutes(-30) } | Select-Object FullName,Length,LastWriteTime | Sort-Object LastWriteTime -Descending | Select-Object -First 30 | Format-Table -AutoSize | Out-String
Write-Output "=== console.log: xray / spec_setup lines ==="
Select-String -Path 'C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\console.log' -Pattern 'xray|spec_show|spec_mode|jointeam|sv_cheats 1' | Select-Object -Last 15 | ForEach-Object { $_.Line } | Out-String
Write-Output "=== console.log first 5 ==="
Get-Content 'C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\console.log' -TotalCount 3 | Out-String
