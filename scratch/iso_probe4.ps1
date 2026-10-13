$ErrorActionPreference='SilentlyContinue'
Write-Output "=== node/curl/wscript procs ==="
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'node|curl|wscript|cscript|powershell' } | Select-Object ProcessId,Name,CommandLine | Format-List | Out-String
Write-Output "=== netcon connections to 2121 ==="
Get-NetTCPConnection -LocalPort 2121 -ErrorAction SilentlyContinue | Select-Object State,LocalAddress,RemoteAddress,RemotePort,OwningProcess | Format-Table -AutoSize | Out-String
Write-Output "=== newest screenshots ==="
$s='C:\Program Files (x86)\Steam\userdata\1065199776\760\remote\730\screenshots'
Get-ChildItem $s -File | Sort-Object LastWriteTime -Descending | Select-Object -First 8 Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String
Write-Output "=== director.log tail ==="
Get-Content C:\iso_director\director.log -Tail 8 | Out-String
