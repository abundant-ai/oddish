<#
.SYNOPSIS
  Local status view for a running legacy migration. No Modal container needed.

.DESCRIPTION
  ONE-OFF MIGRATION SCRIPT -- TEMPORARY BY DESIGN. Delete with the rest of the
  legacy_* tooling once the migration is finished.

  Parses the shard logs on disk rather than querying the database, so it costs
  nothing and cannot be blocked by Modal capacity. Shows per-shard progress,
  the summed rate across shards, and an ETA.

  Requires the shards to have been started with $env:PYTHONUNBUFFERED = "1",
  otherwise Start-Process buffers their output and the logs look frozen.

  LIMITATION: this reads logs, not the ledger. A shard that was preempted and
  restarted prints a fresh "scope:" line and its counter restarts, so the total
  here UNDER-reports actual progress. The ledger is the source of truth --
  use legacy_monitor.py when a container is available and you need certainty.

.EXAMPLE
  .\backend\scripts\migration_status.ps1
  .\backend\scripts\migration_status.ps1 -Pattern "hf_shard*.log" -Refresh 60
#>
param(
    [string]$Pattern = "hf_shard*.log",
    [int]$Refresh = 0          # seconds; 0 = print once and exit
)

$script:PrevSamples = @{}

function Show-Status {
    param([string]$Pattern)

    $logs = Get-ChildItem $Pattern -ErrorAction SilentlyContinue | Sort-Object Name
    if (-not $logs) {
        Write-Host "No logs matching '$Pattern' in $(Get-Location)" -ForegroundColor Yellow
        return
    }

    $done = 0; $total = 0; $rate = 0.0; $errors = 0
    $running = 0; $finished = 0; $starting = 0

    Write-Host ""
    # PowerShell alignment is a signed width -- negative left, positive right.
    # There is no ">" prefix; using one is a format error at runtime.
    Write-Host ("{0,-22} {1,10} {2,10} {3,9} {4,8}  {5}" -f `
                "shard", "done", "of", "rate/s", "errors", "state")
    Write-Host ("-" * 78)

    foreach ($log in $logs) {
        $lines = Get-Content $log.FullName -ErrorAction SilentlyContinue
        if (-not $lines) { continue }

        # A DONE line means this shard finished its worklist.
        $doneLine = $lines | Select-String -Pattern "^DONE" | Select-Object -Last 1
        $prog     = $lines | Select-String -Pattern "^\s+progress" | Select-Object -Last 1
        # More than one scope: line means the shard was preempted and restarted.
        $restarts = ($lines | Select-String -Pattern "^scope:" | Measure-Object).Count

        $suffix = ""
        if ($restarts -gt 1) { $suffix = "  (restarted x$($restarts - 1))" }

        # Take this shard's workload from its scope: line even if it has not
        # started importing. Otherwise a shard still in pre-create contributes
        # nothing to the denominator and the ETA describes only the shards that
        # happen to be running -- wildly optimistic while others start up.
        $shardTotal = 0
        $scopeLine = $lines | Select-String -Pattern "^scope:" | Select-Object -Last 1
        if ($scopeLine) {
            $sm = [regex]::Match($scopeLine.Line, "trials=(\d+)")
            if ($sm.Success) { $shardTotal = [int]$sm.Groups[1].Value }
        }

        if ($prog) {
            $m = [regex]::Match($prog.Line, "progress (\d+)/(\d+)\s+rate=([\d.]+).*?(\d+) errors")
            if ($m.Success) {
                $d = [int]$m.Groups[1].Value
                $e = [int]$m.Groups[4].Value

                # Measure the rate against the REAL clock between samples.
                #
                # Two earlier attempts were wrong. The log's own rate= is
                # cumulative from a t0 that starts before the ~40 minute
                # experiment pre-create, so it understates badly. Deriving
                # elapsed as done/rate and differencing two log lines cancels
                # that offset in theory, but rate is printed to two decimals and
                # elapsed is ~4000s, so rounding moves it by ~11s against a real
                # gap of ~55s -- adverse rounding collapses the gap and the rate
                # explodes (it reported 40/s for a shard doing ~2/s).
                #
                # Sampling done against Get-Date needs no derivation and cannot
                # be destabilised by the log's precision.
                $r = 0.0
                $inst = $false
                $prev = $script:PrevSamples[$log.Name]
                if ($prev) {
                    $dt = ((Get-Date) - $prev.At).TotalSeconds
                    if ($dt -ge 5 -and $d -ge $prev.Done) {
                        $r = ($d - $prev.Done) / $dt
                        $inst = $true
                    }
                }
                $script:PrevSamples[$log.Name] = @{ Done = $d; At = (Get-Date) }

                $done += $d; $errors += $e
                if ($shardTotal -gt 0) { $total += $shardTotal } else { $total += [int]$m.Groups[2].Value }

                $state = "running"
                if (-not $inst) { $state = "running (measuring...)" }
                if ($doneLine) { $state = "DONE"; $finished++ }
                elseif ($inst) { $rate += $r; $running++ }
                else { $running++ }

                $rateTxt = "-"
                if ($inst) { $rateTxt = "{0:N2}" -f $r }
                Write-Host ("{0,-22} {1,10} {2,10} {3,9} {4,8}  {5}{6}" -f `
                            $log.Name, $d, $shardTotal, $rateTxt, $e, $state, $suffix)
                continue
            }
        }

        # No progress line yet -- report which startup phase it is in.
        $phase = "queued (no container?)"
        if ($lines | Select-String -Pattern "^processing")  { $phase = "pre-create done, starting trials" }
        elseif ($lines | Select-String -Pattern "prefetched") { $phase = "experiment pre-create" }
        elseif ($lines | Select-String -Pattern "^scope:")    { $phase = "manifest prefetch" }
        $starting++
        $total += $shardTotal
        Write-Host ("{0,-22} {1,10} {2,10} {3,9} {4,8}  {5}{6}" -f `
                    $log.Name, "-", $(if ($shardTotal) { $shardTotal } else { "-" }), "-", "-", $phase, $suffix)
    }

    Write-Host ("-" * 78)
    if ($rate -gt 0 -and $total -gt 0) {
        $left = $total - $done
        $eta  = $left / $rate / 3600.0
        Write-Host ("TOTAL {0:N0}/{1:N0} ({2:N1}%)   {3:N2}/s   eta {4:N1}h   errors {5}" -f `
                    $done, $total, (100.0 * $done / $total), $rate, $eta, $errors) `
                   -ForegroundColor Cyan
        if ($starting -gt 0) {
            Write-Host ("  (eta assumes only the {0} running shard(s); it drops as the other {1} start)" -f `
                        $running, $starting) -ForegroundColor DarkGray
        }
    }
    else {
        Write-Host "All shards still in startup -- no rate yet." -ForegroundColor Yellow
    }
    Write-Host ("{0} running, {1} finished, {2} starting" -f $running, $finished, $starting)
    if ($errors -gt 0) {
        Write-Host "Sweep failures at the end with: --execute --retry-failed" -ForegroundColor Yellow
    }
}

if ($Refresh -gt 0) {
    while ($true) {
        Clear-Host
        Write-Host ("migration status  --  {0}  (refresh {1}s, Ctrl+C to stop)" -f `
                    (Get-Date -Format "HH:mm:ss"), $Refresh)
        Show-Status -Pattern $Pattern
        Start-Sleep -Seconds $Refresh
    }
}
else {
    Show-Status -Pattern $Pattern
}
