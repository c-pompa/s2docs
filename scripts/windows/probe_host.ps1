# Read-only health probe for a CS2 test host. Prints one JSON object.
# Usage: ssh <host> "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand <base64 of this file>"
$ErrorActionPreference = 'SilentlyContinue'

$gpu = @()
$smi = & nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>$null
foreach ($line in $smi) {
    $p = $line -split ',\s*'
    $gpu += [ordered]@{ name = $p[0]; util_pct = [int]$p[1]; mem_used_mib = [int]$p[2]; mem_total_mib = [int]$p[3] }
}

$names = 'LM Studio', 'lms', 'llama-server', 'cs2', 'steam', 'hammer', 'resourcecompiler'
$procs = Get-Process | Where-Object { $names -contains $_.ProcessName } |
    Select-Object ProcessName, Id, @{ n = 'mem_mb'; e = { [int]($_.WorkingSet64 / 1MB) } }

# Heavy GPU consumers that suggest a game or model is loaded (best effort).
$gpuApps = Get-Process | Where-Object { $_.ProcessName -match '^(cs2|dota2|deadlock|project8|LM Studio|lms|ollama|llama-server|steamwebhelper)$' } |
    Select-Object -ExpandProperty ProcessName -Unique

# Find CS2 via Steam library folders.
$cs2 = $null
$steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam').SteamPath
if ($steam) {
    $vdf = Join-Path $steam 'steamapps\libraryfolders.vdf'
    $libs = @($steam) + ((Get-Content $vdf -Raw) | Select-String '"path"\s+"([^"]+)"' -AllMatches).Matches.ForEach({ $_.Groups[1].Value -replace '\\\\', '\' })
    foreach ($lib in $libs | Select-Object -Unique) {
        $candidate = Join-Path $lib 'steamapps\common\Counter-Strike Global Offensive'
        if (Test-Path (Join-Path $candidate 'game\bin\win64\cs2.exe')) {
            $inf = Get-Content (Join-Path $candidate 'game\csgo\steam.inf')
            $cs2 = [ordered]@{
                path          = $candidate
                build         = (($inf | Select-String 'ClientVersion=(\d+)').Matches.Groups[1].Value)
                workshop_tools = (Test-Path (Join-Path $candidate 'game\bin\win64\resourcecompiler.exe'))
            }
            break
        }
    }
}

$os = Get-CimInstance Win32_OperatingSystem
[ordered]@{
    host           = $env:COMPUTERNAME
    gpu            = $gpu
    ram_free_gb    = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
    ram_total_gb   = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
    processes      = $procs
    gpu_heavy_apps = $gpuApps
    cs2            = $cs2
} | ConvertTo-Json -Depth 5 -Compress
