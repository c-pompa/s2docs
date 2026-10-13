$ErrorActionPreference='SilentlyContinue'
$log='C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\console.log'
Write-Output "=== load: missing-material warnings at map load? ==="
Select-String -Path $log -Pattern 'missing material|vrf|physics_surfaces' | Select-Object -First 6 | ForEach-Object { $_.Line } | Out-String
Write-Output "=== load: map load + nav lines ==="
Select-String -Path $log -Pattern 'de_dust2_iso|nav_|Loading map|Started map' | Select-Object -First 20 | ForEach-Object { $_.Line } | Out-String
Write-Output "=== last 6 console lines (exit) ==="
Get-Content $log -Tail 6 | Out-String
Write-Output "=== cs2 crash events (last 2h) ==="
Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddHours(-2)} | Where-Object { $_.Message -match 'cs2' } | Select-Object -First 5 TimeCreated,Id,ProviderName,@{n='Msg';e={$_.Message.Substring(0,[Math]::Min(180,$_.Message.Length))}} | Format-List | Out-String
Write-Output "=== cs2 proc now ==="
(Get-Process cs2 -ErrorAction SilentlyContinue | Measure-Object).Count
Write-Output "=== mdmp newest ==="
Get-ChildItem 'C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\bin\win64' -Filter *.mdmp | Sort-Object LastWriteTime -Descending | Select-Object -First 2 Name,LastWriteTime | Format-Table -AutoSize | Out-String
