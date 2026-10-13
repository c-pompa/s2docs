$ErrorActionPreference = 'SilentlyContinue'
Write-Output "=== cs2 cmdline ==="
Get-CimInstance Win32_Process -Filter "Name='cs2.exe'" | Select-Object ProcessId,CommandLine | Format-List | Out-String
Write-Output "=== netcon 2121 ==="
(Get-NetTCPConnection -LocalPort 2121 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess | Format-Table -AutoSize | Out-String)
Write-Output "=== s2iso tasks ==="
schtasks /query /fo LIST 2>&1 | Select-String -Pattern 's2iso|iso_f12|CS2Launch|iso_rc' | Out-String
Write-Output "=== LOG full tail 40 ==="
Get-Content C:\iso_build\iso_rc.log -Tail 40 | Out-String
Write-Output "=== ui compile log? ==="
Get-ChildItem C:\iso_build -Filter *.log | Select-Object Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String
