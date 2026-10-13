$ErrorActionPreference='SilentlyContinue'
$log='C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\csgo\console.log'
$m = Select-String -Path $log -Pattern 'maps/de_dust2_iso/entities/(iso_occ_\w+)_\d+\.vmdl_c' -AllMatches
$names = $m | ForEach-Object { $_.Matches } | ForEach-Object { $_[0].Groups[1].Value } | Sort-Object -Unique
Write-Output ("distinct iso_occ entity-model load failures at runtime: " + $names.Count)
Write-Output ($names -join ', ')
Write-Output "--- total failure lines ---"
($m | Measure-Object).Count
Write-Output "--- which spots ---"
$names | ForEach-Object { ($_ -replace '_\d+$','') } | ForEach-Object { ($_ -replace 'iso_occ_','') -replace '_\d+$','' } | Group-Object | Select-Object Name,Count | Format-Table -AutoSize | Out-String
Write-Output "--- any OTHER de_dust2_iso model load failures (non iso_occ) ---"
(Select-String -Path $log -Pattern 'maps/de_dust2_iso/entities/' | Where-Object { $_.Line -notmatch 'iso_occ_' } | Select-Object -First 5 | ForEach-Object { $_.Line }) -join "`n"
