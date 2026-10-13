$ErrorActionPreference = 'SilentlyContinue'
$r = 'C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive'
Write-Output "=== PROCS ==="
Get-Process | Where-Object { $_.ProcessName -match 'lmstudio|llama|cs2|steam|LMStudio' } | Select-Object ProcessName,Id,@{n='WS_MB';e={[int]($_.WorkingSet64/1MB)}} | Format-Table -AutoSize | Out-String
Write-Output "=== GPU ==="
nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>&1 | Out-String
Write-Output "=== RAM ==="
Get-CimInstance Win32_OperatingSystem | Select-Object @{n='TotalGB';e={[int]($_.TotalVisibleMemorySize/1MB)}},@{n='FreeGB';e={[int]($_.FreePhysicalMemory/1MB)}} | Format-Table -AutoSize | Out-String
Write-Output "=== LOCK ==="
if (Test-Path C:\iso_build\iso_host.lock) { Get-Content C:\iso_build\iso_host.lock } else { 'no lock' }
Write-Output "=== DISK C ==="
Get-PSDrive C | Select-Object @{n='FreeGB';e={[int]($_.Free/1GB)}},@{n='UsedGB';e={[int]($_.Used/1GB)}} | Format-Table -AutoSize | Out-String
Write-Output "=== addon maps ==="
Get-ChildItem "$r\game\csgo_addons\iso_spectate\maps" | Select-Object Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String
Write-Output "=== csgo/maps ==="
Get-ChildItem "$r\game\csgo\maps" | Select-Object Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String
Write-Output "=== addon scripts (loose gameplay) ==="
Get-ChildItem "$r\game\csgo\maps\scripts" | Select-Object Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String
Write-Output "=== games/csgo root loose vjs ==="
Get-ChildItem "$r\game\csgo\*.vjs_c" -Recurse -ErrorAction SilentlyContinue | Select-Object FullName,Length | Format-Table -AutoSize | Out-String
Write-Output "=== LOG tail ==="
if (Test-Path C:\iso_build\iso_rc.log) { Get-Content C:\iso_build\iso_rc.log -Tail 25 } else { 'no iso_rc.log' }
Write-Output "=== buildstate ==="
Get-ChildItem C:\iso_build -Filter *.lock | Select-Object Name,LastWriteTime | Format-Table -AutoSize | Out-String
