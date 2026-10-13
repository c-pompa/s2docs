param(
  [string]$CmdFile = 'C:\iso_build\nc_cmds.txt',
  [int]$DrainMs = 700,
  [int]$CmdDelayMs = 500,
  [int]$SettleMs = 1600
)
$ErrorActionPreference = 'Stop'
# --- 1. send commands over netcon ---
$client = New-Object System.Net.Sockets.TcpClient
$client.Connect('127.0.0.1', 2121)
$stream = $client.GetStream()
$enc = [System.Text.Encoding]::ASCII
function Read-Available([int]$ms) {
    $sb = New-Object System.Text.StringBuilder
    $buf = New-Object byte[] 65536
    $deadline = [DateTime]::UtcNow.AddMilliseconds($ms)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            if ($stream.DataAvailable) {
                $n = $stream.Read($buf, 0, $buf.Length)
                if ($n -gt 0) { [void]$sb.Append($enc.GetString($buf, 0, $n)); $deadline = [DateTime]::UtcNow.AddMilliseconds(200) }
            } else { Start-Sleep -Milliseconds 30 }
        } catch { break }
    }
    return $sb.ToString()
}
[void](Read-Available $DrainMs)
$cmds = Get-Content $CmdFile | Where-Object { $_.Trim() -ne '' -and -not $_.StartsWith('#') }
foreach ($c in $cmds) {
    Write-Output "CMD> $c"
    $b = $enc.GetBytes($c + "`n"); $stream.Write($b, 0, $b.Length); $stream.Flush()
    $out = Read-Available $CmdDelayMs
    if ($out.Trim()) { Write-Output $out.Trim() }
}
$client.Close()
Start-Sleep -Milliseconds $SettleMs
# --- 2. trigger Steam F12 in the desktop session ---
$s = 'C:\Program Files (x86)\Steam\userdata\1065199776\760\remote\730\screenshots'
$before = @(Get-ChildItem $s -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
schtasks /create /tn iso_f12 /tr "powershell -NoProfile -ExecutionPolicy Bypass -File C:\iso_director\f12v3.ps1" /sc once /sd 01/01/2030 /st 00:00 /it /f /ru pompa | Out-Null
schtasks /run /tn iso_f12 | Out-Null
Start-Sleep -Seconds 4
schtasks /delete /tn iso_f12 /f | Out-Null
$after = @(Get-ChildItem $s -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name)
$new = $after | Where-Object { $before -notcontains $_ }
Write-Output ("NEWSHOT: " + ($new -join ', '))
