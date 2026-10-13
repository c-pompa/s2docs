$ErrorActionPreference='SilentlyContinue'
Write-Output "=== cs2 / node procs ==="
(Get-Process cs2,node -ErrorAction SilentlyContinue | Select-Object Id,ProcessName | Format-Table -AutoSize | Out-String)
Write-Output "=== netcon 2121 ==="
(Get-NetTCPConnection -LocalPort 2121 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalPort,OwningProcess | Format-Table -AutoSize | Out-String)
Write-Output "=== lock ==="
if (Test-Path C:\iso_build\iso_host.lock) { "LOCK PRESENT: " + (Get-Content C:\iso_build\iso_host.lock) } else { "no lock (clean)" }
Write-Output "=== iso_* / s2iso tasks ==="
schtasks /query /fo LIST 2>&1 | Select-String -Pattern 'iso_f12|s2iso|iso_director|CS2Launch|iso_rc' | Out-String
Write-Output "=== screenshots dir ==="
$s='C:\Program Files (x86)\Steam\userdata\1065199776\760\remote\730\screenshots'
if (Test-Path $s) { "count=" + (Get-ChildItem $s -File | Measure-Object).Count; Get-ChildItem $s -File | Sort-Object LastWriteTime -Descending | Select-Object -First 5 Name,LastWriteTime | Format-Table -AutoSize | Out-String } else { 'no dir' }
Write-Output "=== nav file present? ==="
$r='C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive'
Get-ChildItem "$r\game\csgo_addons\iso_spectate\maps\de_dust2_iso.nav" -ErrorAction SilentlyContinue | Select-Object Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String
