<#
.SYNOPSIS
  Keeps migration shards alive unattended. Relaunches any that die; exits when
  all have finished.

.DESCRIPTION
  ONE-OFF MIGRATION SCRIPT -- TEMPORARY BY DESIGN. Delete with the rest of the
  legacy_* tooling once the migration is finished.

  Every -IntervalMin it checks each shard and takes ONE of three actions:

    * log contains "DONE"          -> finished, never relaunch
    * a modal process is alive     -> leave alone (this includes a shard that is
                                      merely QUEUED for a Modal container -- that
                                      is the correct state to be in when the
                                      workspace is at its container cap, and it
                                      will start on its own when capacity frees)
    * neither                      -> archive its log and relaunch it

  So a shard killed by the 6h function timeout, by preemption, or by a crash
  comes back on its own. Relaunching is always safe: the ledger records what
  moved, in-flight rows stay 'discovered', and the deterministic idempotency key
  turns anything already imported into a 'skipped' rather than a duplicate.

  It does NOT run the final --retry-failed sweep or the validator. Those are
  judgement calls; do them in the morning. It prints the commands when it exits.

.EXAMPLE
  # default: watch all 4 harbor-forge shards, check every 20 minutes
  .\backend\scripts\migration_watchdog.ps1

  # different base / cadence, and only some shards
  .\backend\scripts\migration_watchdog.ps1 -Base abundant-ai/experiments -Shards 0,1 -IntervalMin 10
#>
param(
    [string]$Base        = "abundant-ai/harbor-forge",
    [int[]]$Shards       = @(0, 1, 2, 3),
    [int]$NumShards      = 4,
    [int]$Concurrency    = 8,
    [int]$IntervalMin    = 20,
    [string]$Prefix      = "hf_shard",
    [string]$WatchLog    = "migration_watchdog.log"
)

$ErrorActionPreference = "Continue"

# The shards inherit these. PYTHONUNBUFFERED matters: without it Start-Process
# buffers their stdout and the logs look frozen for long stretches.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"
$env:PYTHONUNBUFFERED = "1"

$modal = Join-Path (Get-Location) ".venv\Scripts\modal.exe"
if (-not (Test-Path $modal)) {
    Write-Host "modal.exe not found at $modal -- run this from the oddish repo root." -ForegroundColor Red
    exit 1
}

function Write-Log {
    param([string]$Message, [string]$Colour = "Gray")
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line -ForegroundColor $Colour
    Add-Content -Path $WatchLog -Value $line -Encoding utf8
}

function Get-ShardProcess {
    param([int]$Index)
    # Match on the full command line so we identify a specific shard. Args are
    # emitted as "--num-shards N --shard I", so anchor on the trailing boundary
    # to keep --shard 1 from matching --shard 12.
    Get-CimInstance Win32_Process -Filter "Name='modal.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match "--num-shards\s+$NumShards\s+--shard\s+$Index(\s|$)" } |
        Select-Object -First 1
}

function Start-Shard {
    param([int]$Index)
    $log = "$Prefix$Index.log"
    $err = "$Prefix$Index.err"

    # Start-Process truncates rather than appends, so keep the previous attempt.
    # .bak keeps it out of the hf_shard*.log glob that migration_status.ps1 uses.
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    foreach ($f in @($log, $err)) {
        if (Test-Path $f) { Rename-Item -Path $f -NewName "$f.$stamp.bak" -ErrorAction SilentlyContinue }
    }

    $a = @("run", "backend\scripts\legacy_transfer.py",
           "--scope-base", $Base, "--execute",
           "--concurrency", "$Concurrency",
           "--num-shards", "$NumShards", "--shard", "$Index")
    Start-Process -NoNewWindow -FilePath $modal -ArgumentList $a `
                  -RedirectStandardOutput $log -RedirectStandardError $err
    Write-Log "launched shard $Index" "Cyan"
}

function Get-ShardState {
    param([int]$Index)
    $log = "$Prefix$Index.log"
    if (-not (Test-Path $log)) { return @{ Done = $false; Progress = $null } }
    $lines = Get-Content $log -ErrorAction SilentlyContinue
    $done  = [bool]($lines | Select-String -Pattern "^DONE" -Quiet)
    $prog  = $lines | Select-String -Pattern "^\s+progress" | Select-Object -Last 1
    return @{ Done = $done; Progress = $(if ($prog) { $prog.Line.Trim() } else { $null }) }
}

Write-Log "watchdog started -- base=$Base shards=$($Shards -join ',') interval=${IntervalMin}m" "Green"
Write-Log "relaunches any shard that is neither finished nor alive; leaves QUEUED shards alone"

while ($true) {
    $finished = 0
    foreach ($i in $Shards) {
        $state = Get-ShardState -Index $i
        $proc  = Get-ShardProcess -Index $i

        if ($state.Done) {
            $finished++
            if ($proc) { Write-Log "shard $i reports DONE (process still winding down)" }
            continue
        }
        if ($proc) {
            $note = "alive (pid $($proc.ProcessId))"
            if ($state.Progress) { $note += " -- $($state.Progress)" }
            else { $note += " -- startup or queued for a container" }
            Write-Log "shard $i $note"
            continue
        }

        Write-Log "shard $i NOT running and not finished -- relaunching" "Yellow"
        Start-Shard -Index $i
    }

    if ($finished -eq $Shards.Count) {
        Write-Log "ALL $($Shards.Count) SHARDS DONE for $Base" "Green"
        Write-Log "next steps (run these yourself):"
        Write-Log "  modal run backend/scripts/legacy_transfer.py --scope-base $Base --execute --retry-failed --concurrency 8"
        Write-Log "  modal run backend/scripts/legacy_validate.py --scope-base $Base"
        break
    }

    Write-Log "$finished/$($Shards.Count) done -- next check in ${IntervalMin}m"
    Start-Sleep -Seconds ($IntervalMin * 60)
}
