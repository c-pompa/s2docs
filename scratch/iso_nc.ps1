param([int]$DrainMs = 1500, [int]$CmdDelayMs = 700, [string]$CmdFile = 'C:\iso_build\nc_cmds.txt')
$ErrorActionPreference = 'Stop'
$client = New-Object System.Net.Sockets.TcpClient
$client.Connect('127.0.0.1', 2121)
$stream = $client.GetStream()
$stream.ReadTimeout = 250
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
            } else { Start-Sleep -Milliseconds 40 }
        } catch { break }
    }
    return $sb.ToString()
}
Write-Output "----- INITIAL DRAIN -----"
Write-Output (Read-Available $DrainMs)
$cmds = Get-Content $CmdFile | Where-Object { $_.Trim() -ne '' -and -not $_.StartsWith('#') }
foreach ($c in $cmds) {
    Write-Output "----- SEND: $c -----"
    $bytes = $enc.GetBytes($c + "`n")
    $stream.Write($bytes, 0, $bytes.Length)
    $stream.Flush()
    Write-Output (Read-Available $CmdDelayMs)
}
$client.Close()
