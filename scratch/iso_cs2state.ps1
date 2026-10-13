$ErrorActionPreference='SilentlyContinue'
Write-Output "=== cs2 process ==="
Get-Process cs2 | Select-Object Id,ProcessName,@{n='WS_MB';e={[int]($_.WorkingSet64/1MB)}},StartTime,Responding | Format-List | Out-String
Write-Output "=== netcon listen ==="
Get-NetTCPConnection -LocalPort 2121 -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess | Format-Table -AutoSize | Out-String
Write-Output "=== GPU/RAM ==="
nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader
Get-CimInstance Win32_OperatingSystem | Select-Object @{n='FreeGB';e={[int]($_.FreePhysicalMemory/1MB)}} | Format-Table -AutoSize | Out-String
Write-Output "=== crash dumps / mdmp recent ==="
Get-ChildItem 'C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\bin\win64' -Filter *.mdmp | Sort-Object LastWriteTime -Descending | Select-Object -First 3 Name,LastWriteTime | Format-Table -AutoSize | Out-String
Write-Output "=== console.log tail ==="
Get-Content 'C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\console.log' -Tail 12 | Out-String
Write-Output "=== newest screenshots ==="
$s='C:\Program Files (x86)\Steam\userdata\1065199776\760\remote\730\screenshots'
Get-ChildItem $s -File | Sort-Object LastWriteTime -Descending | Select-Object -First 4 Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String
