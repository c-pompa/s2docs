$ErrorActionPreference='SilentlyContinue'
Write-Output "=== director.log tail 40 ==="
Get-Content C:\iso_director\director.log -Tail 40 | Out-String
Write-Output "=== iso_occ_sync count in log ==="
(Select-String -Path C:\iso_director\director.log -Pattern 'iso_occ' -SimpleMatch).Count
Write-Output "=== distinct iso_occ targetnames driving ==="
(Select-String -Path C:\iso_director\director.log -Pattern 'iso_occ_\w+' -AllMatches | ForEach-Object { $_.Matches.Value } | Sort-Object -Unique) -join ', '
Write-Output "=== director.err.log ==="
Get-Content C:\iso_director\director.err.log -Tail 20 | Out-String
Write-Output "=== newest screenshots ==="
$s='C:\Program Files (x86)\Steam\userdata\1065199776\760\remote\730\screenshots'
Get-ChildItem $s -File | Sort-Object LastWriteTime -Descending | Select-Object -First 6 Name,Length,LastWriteTime | Format-Table -AutoSize | Out-String
