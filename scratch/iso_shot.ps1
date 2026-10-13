$ErrorActionPreference='SilentlyContinue'
$s = 'C:\Program Files (x86)\Steam\userdata\1065199776\760\remote\730\screenshots'
$before = @(Get-ChildItem $s -File | Select-Object -ExpandProperty Name)
schtasks /create /tn iso_f12 /tr "powershell -NoProfile -ExecutionPolicy Bypass -File C:\iso_director\f12v3.ps1" /sc once /sd 01/01/2030 /st 00:00 /it /f /ru pompa | Out-Null
schtasks /run /tn iso_f12 | Out-Null
Start-Sleep -Seconds 4
schtasks /delete /tn iso_f12 /f | Out-Null
$after = @(Get-ChildItem $s -File | Select-Object -ExpandProperty Name)
$new = $after | Where-Object { $before -notcontains $_ }
Write-Output ("NEWSHOT: " + ($new -join ', '))
Get-Content C:\iso_director\f12v3.log | Out-String
