$ErrorActionPreference='SilentlyContinue'
Write-Output "=== iso_director ==="
Get-ChildItem C:\iso_director | Select-Object Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String
Write-Output "=== node procs (director?) ==="
Get-CimInstance Win32_Process -Filter "Name='node.exe'" | Select-Object ProcessId,CommandLine | Format-List | Out-String
Write-Output "=== screenshots dir ==="
$s = 'C:\Program Files (x86)\Steam\userdata\1065199776\760\remote\730\screenshots'
if (Test-Path $s) { (Get-ChildItem $s -File).Count; Get-ChildItem $s -File | Sort-Object LastWriteTime -Descending | Select-Object -First 6 Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String } else { 'no screenshots dir' }
Write-Output "=== ui log tail ==="
Get-Content C:\iso_build\iso_ui.log -Tail 15 | Out-String
