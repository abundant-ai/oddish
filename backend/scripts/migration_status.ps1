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
            # Take the last TWO progress lines and difference them.
            #
            # The rate printed in the log is CUMULATIVE from the shard's t0,
            # which starts before the experiment pre-create -- so on harbor-forge
            # it averages in ~40 minutes of zero progress and badly understates
            # the truth. Each line gives done and cumulative rate, so elapsed is
            # done/rate. Differencing two points cancels the t0 offset entirely
            # and yields the real current rate, from logs already on disk.
            $last2 = $lines | Select-String -Pattern "^\s+progress" | Select-Object -Last 2
            $m = [regex]::Match($prog.Line, "progress (\d+)/(\d+)\s+rate=([\d.]+).*?(\d+) errors")
            if ($m.Success) {
                $d = [int]$m.Groups[1].Value
                $t = [int]$m.Groups[2].Value
                $rCum = [double]$m.Groups[3].Value
                $e = [int]$m.Groups[4].Value

                $r = $rCum          # fall back to cumulative if we only have one point
                $inst = $false
                if ($last2.Count -eq 2) {
                    $m1 = [regex]::Match($last2[0].Line, "progress (\d+)/\d+\s+rate=([\d.]+)")
                    if ($m1.Success) {
                        $d1 = [double]$m1.Groups[1].Value; $r1 = [double]$m1.Groups[2].Value
                        $d2 = [double]$d;                  $r2 = $rCum
                        if ($r1 -gt 0 -and $r2 -gt 0) {
                            $t1 = $d1 / $r1
                            $t2 = $d2 / $r2
                            if ($t2 -gt $t1) { $r = ($d2 - $d1) / ($t2 - $t1); $inst = $true }
                        }
                    }
                }

                $done += $d; $errors += $e
                if ($shardTotal -gt 0) { $total += $shardTotal } else { $total += $t }
                $state = "running"
                if (-not $inst) { $state = "running (cumulative rate)" }
                if ($doneLine) { $state = "DONE"; $finished++ } else { $rate += $r; $running++ }
                Write-Host ("{0,-22} {1,10} {2,10} {3,9:N2} {4,8}  {5}{6}" -f `
                            $log.Name, $d, $t, $r, $e, $state, $suffix)
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
