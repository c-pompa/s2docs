$ErrorActionPreference='SilentlyContinue'
$log='C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\console.log'
Write-Output "=== xray/spec_show lines ==="
Select-String -Path $log -Pattern 'xray' | Select-Object -Last 12 | ForEach-Object { $_.Line } | Out-String
Write-Output "=== lines around 16:00:07-16:00:30 (actor) ==="
Select-String -Path $log -Pattern '16:00:(0[5-9]|1[0-9]|2[0-9])' | ForEach-Object { $_.Line } | Select-Object -First 60 | Out-String
Write-Output "=== any 'quit' or disconnect ==="
Select-String -Path $log -Pattern 'quit|Disconnect|shutdown|Stopping' | Select-Object -Last 8 | ForEach-Object { $_.Line } | Out-String
