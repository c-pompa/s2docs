param(
  [string]$Src = "C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\content\csgo\items\assets\sprays\workshop\example_spray.tga",
  [string]$Formats = "glb,obj",
  [string]$Extra = "",          # extra multipart field, e.g. "target_tris=20000"
  [int]$TimeoutS = 240,
  [string]$Base = "http://127.0.0.1:18191"
)
# One proof run for the triposr-worker, entirely on the box and fully bounded:
#   1. host-side whole-card VRAM sampler (nvidia-smi in a loop, separate PowerShell job)
#   2. submit one CS2 content image (multipart, bearer token read from .env -- never printed)
#   3. poll the job (250 ms steps, hard deadline), stop the sampler
#   4. parse the produced mesh with trimesh inside the container (app/validate_mesh.py)
# Everything lands in D:\Docker\triposr\proof\ (trace CSV, job JSON, validate JSON, run summary).
$ErrorActionPreference = "Continue"
$root = "D:\Docker\triposr"
$proof = Join-Path $root "proof"
New-Item -ItemType Directory -Force $proof | Out-Null
$tok = ((Get-Content (Join-Path $root ".env")) | Where-Object { $_ -like "TSR_API_TOKEN=*" }) -replace "^TSR_API_TOKEN=", ""
if (-not $tok) { "NO TOKEN in .env"; exit 2 }
$hdr = @{ Authorization = "Bearer $tok" }
$stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
$trace = Join-Path $proof "gpu_trace_$stamp.csv"
$stopf = "$trace.stop"

$sampler = Start-Job -ArgumentList $trace, $stopf, ($TimeoutS + 30) -ScriptBlock {
  param($out, $stopf, $maxs)
  [IO.File]::WriteAllText($out, "epoch_ms,mem_used_mib,mem_total_mib,util_pct`n")
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $maxs -and -not (Test-Path $stopf)) {
    $g = (nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits) -replace " ", ""
    [IO.File]::AppendAllText($out, ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()).ToString() + "," + $g + "`n")
    Start-Sleep -Milliseconds 100
  }
}
$dl = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $dl) {
  if ((Test-Path $trace) -and ((Get-Content $trace).Count -ge 8)) { break }
  Start-Sleep -Milliseconds 250
}
"sampler rows before submit: " + ((Get-Content $trace -ErrorAction SilentlyContinue).Count - 1)

$srcItem = Get-Item -LiteralPath $Src
$srcHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Src).Hash.ToLower()
"source: $Src  bytes=$($srcItem.Length)  sha256=$srcHash"
$curlArgs = @("-s", "-S", "-m", "120", "-H", "Authorization: Bearer $tok", "-F", "image=@$Src", "-F", "formats=$Formats")
if ($Extra) { $curlArgs += @("-F", $Extra) }
$t0ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$sw = [Diagnostics.Stopwatch]::StartNew()
$resp = & curl.exe @curlArgs "$Base/v1/mesh"
"submit -> $resp  (+$([math]::Round($sw.Elapsed.TotalSeconds,2))s)"
$id = $null
try { $id = ($resp | ConvertFrom-Json).task_id } catch {}
if (-not $id) { New-Item -ItemType File $stopf -Force | Out-Null; "SUBMIT FAILED"; exit 3 }

$last = ""; $job = $null
$deadline = (Get-Date).AddSeconds($TimeoutS)
while ((Get-Date) -lt $deadline) {
  try { $job = Invoke-RestMethod -Headers $hdr -TimeoutSec 10 "$Base/v1/jobs/$id" } catch { $job = $null }
  if ($job) {
    $cur = "$($job.status)/$($job.phase)"
    if ($cur -ne $last) { "[{0,6:N2}s] {1}" -f $sw.Elapsed.TotalSeconds, $cur; $last = $cur }
    if ($job.status -in @("succeeded", "failed")) { break }
  }
  Start-Sleep -Milliseconds 250
}
$wall = [math]::Round($sw.Elapsed.TotalSeconds, 2)
$t1ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
"CLIENT WALL-CLOCK submit -> $($job.status): $wall s"
Start-Sleep -Seconds 2
New-Item -ItemType File $stopf -Force | Out-Null
Wait-Job $sampler -Timeout 15 | Out-Null
Remove-Job $sampler -Force -ErrorAction SilentlyContinue
Remove-Item $stopf -ErrorAction SilentlyContinue

$rows = Import-Csv $trace | Where-Object { $_.epoch_ms -match "^\d+$" -and $_.mem_used_mib -match "^\d+$" } |
  ForEach-Object { [pscustomobject]@{ t = [int64]$_.epoch_ms; mem = [int]$_.mem_used_mib; tot = [int]$_.mem_total_mib; util = $_.util_pct } }
$pre = @($rows | Where-Object { $_.t -lt $t0ms })
$during = @($rows | Where-Object { $_.t -ge $t0ms -and $_.t -le ($t1ms + 500) })
$post = @($rows | Where-Object { $_.t -gt ($t1ms + 500) })
$total = ($rows | Select-Object -First 1).tot
$basePeak = ($pre | Measure-Object mem -Maximum).Maximum
$peak = ($during | Measure-Object mem -Maximum).Maximum
$peakRow = $during | Sort-Object mem -Descending | Select-Object -First 1
"HOST nvidia-smi: samples pre=$($pre.Count) during=$($during.Count) post=$($post.Count)"
"HOST nvidia-smi: baseline(max pre)=$basePeak MiB  peak during job=$peak MiB (at +$([math]::Round(($peakRow.t - $t0ms)/1000,1))s, util $($peakRow.util)%)  total=$total MiB  headroom=$($total - $peak) MiB  job delta=$($peak - $basePeak) MiB"
"HOST nvidia-smi: after job=" + (($post | Select-Object -Last 1).mem) + " MiB"

$utf8 = New-Object System.Text.UTF8Encoding($false)
$jobFile = Join-Path $proof "job_$id.json"
[IO.File]::WriteAllText($jobFile, ($job | ConvertTo-Json -Depth 12), $utf8)
$s = $job.stats
"WORKER stats: model_was_warm=$($s.model_was_warm) model_load_s=$($s.model_load_s) preprocess_s=$($s.preprocess_s) inference_s=$($s.inference_s) extract_mesh_s=$($s.extract_mesh_s) export_s=$($s.export_s) job_s=$($s.job_s) submit_to_done_s=$($s.submit_to_done_s)"
"WORKER nvidia-smi (in container): first=$($s.nvsmi_first_mib) peak=$($s.nvsmi_peak_mib) samples=$($s.nvsmi_samples)  by_phase=" + ($s.nvsmi_by_phase | ConvertTo-Json -Compress)
"WORKER torch: peak_allocated=$($s.torch_peak_allocated_mib) MiB peak_reserved=$($s.torch_peak_reserved_mib) MiB  device=$($s.device) gpu=$($s.gpu_name)"
"WORKER mesh: raw_tris=$($s.raw_triangles) raw_verts=$($s.raw_vertices) tris=$($s.triangles) verts=$($s.vertices) watertight=$($s.watertight)"
if ($job.error) { "JOB ERROR: $($job.error)" }

if ($job.status -eq "succeeded") {
  $outDir = Join-Path $root "triposr_data\outputs\$id"
  Get-ChildItem $outDir -File | ForEach-Object { "artifact: {0}  {1:N0} bytes" -f $_.FullName, $_.Length }
  $inArgs = @("exec", "triposr-worker", "python", "/app/validate_mesh.py")
  foreach ($f in ($Formats -split ",")) { $inArgs += "/outputs/$id/mesh.$($f.Trim())" }
  $val = & docker @inArgs 2>&1 | Out-String
  "validate exit=$LASTEXITCODE"
  $valFile = Join-Path $proof "validate_$id.json"
  [IO.File]::WriteAllText($valFile, $val, $utf8)
  $val
}
$summary = [ordered]@{
  stamp = $stamp; task_id = $id; status = $job.status; source = $Src; source_bytes = $srcItem.Length; source_sha256 = $srcHash
  input_sha256_seen_by_worker = $job.input.sha256; client_wall_s = $wall
  host_nvsmi = [ordered]@{ baseline_mib = $basePeak; peak_mib = $peak; total_mib = $total; headroom_mib = ($total - $peak); samples_during = $during.Count; trace = $trace }
  worker_stats = $s
}
[IO.File]::WriteAllText((Join-Path $proof "run_$stamp.json"), ($summary | ConvertTo-Json -Depth 12), $utf8)
"summary: " + (Join-Path $proof "run_$stamp.json")
