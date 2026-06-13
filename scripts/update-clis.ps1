#!/usr/bin/env pwsh
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 John Chapman
#
# Update the agentic coding CLIs that Rutherford drives, on Windows / PowerShell.
#
# For each SUPPORTED CLI that is installed on this machine, this runs its native updater (or the npm /
# uv command that owns it), then reports the version before and after. CLIs with no safe, non-interactive
# updater are reported version-only with a one-line manual hint -- the script never guesses a destructive
# command. Anything not installed is skipped. Rutherford's adapters target third-party CLIs whose headless
# flags drift between releases, so after running this, re-verify with `just test-integration` and update
# docs/cli-maintenance.md if a version changed. See that doc for the per-CLI status and known issues.
#
# Usage:
#   pwsh scripts/update-clis.ps1            # update installed CLIs, then report versions
#   pwsh scripts/update-clis.ps1 -CheckOnly # report versions only; make no changes
#   pwsh scripts/update-clis.ps1 -Only amp,cline,pi   # restrict to specific adapter ids
#   pwsh scripts/update-clis.ps1 -TimeoutSec 240      # per-CLI updater timeout (default 180s)
[CmdletBinding()]
param(
  [switch]$CheckOnly,
  [string[]]$Only,
  [int]$TimeoutSec = 180
)

$ErrorActionPreference = 'Continue'

# `pwsh -File ... -Only amp,cline` passes the comma list as ONE string; split it so the filter works
# whether -Only is given as an array or a comma-joined string.
if ($Only) { $Only = $Only -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ } }

# Each entry: Id (Rutherford adapter id), Bin (the command on PATH), Version (args that print the version),
# Update (the argv that updates it, or $null for report-only), Note (a manual-update hint when Update is $null).
# Update commands are non-interactive and verified against each CLI's --help where a native updater exists;
# npm/uv own the packages installed through them. Keep this table in sync with docs/cli-maintenance.md.
$Clis = @(
  # --- the 9 added for v2.0.0 ---
  @{ Id = 'amp';        Bin = 'amp';         Version = @('--version'); Update = @('amp', 'update') }
  @{ Id = 'cline';      Bin = 'cline';       Version = @('--version'); Update = @('cline', 'update') }
  @{ Id = 'cn';         Bin = 'cn';          Version = @('--version'); Update = @('npm', 'install', '-g', '@continuedev/cli@latest') }
  @{ Id = 'hermes';     Bin = 'hermes';      Version = @('--version'); Update = @('hermes', 'update') }
  @{ Id = 'junie';      Bin = 'junie';       Version = @('--version'); Update = $null; Note = 'auto-updates on launch (the shim applies a pending update); to force, re-run https://junie.jetbrains.com/install.ps1' }
  @{ Id = 'kilo';       Bin = 'kilo';        Version = @('--version'); Update = @('kilo', 'upgrade') }
  @{ Id = 'kimi';       Bin = 'kimi';        Version = @('--version'); Update = @('kimi', 'upgrade') }
  @{ Id = 'openhands';  Bin = 'openhands';   Version = @('--version'); Update = @('uv', 'tool', 'upgrade', 'openhands') }
  @{ Id = 'pi';         Bin = 'pi';          Version = @('--version'); Update = @('pi', 'update', 'self') }
  # --- the pre-existing roster ---
  @{ Id = 'claude_code'; Bin = 'claude';     Version = @('--version'); Update = @('claude', 'update') }
  @{ Id = 'codex';      Bin = 'codex';       Version = @('--version'); Update = @('npm', 'install', '-g', '@openai/codex@latest') }
  @{ Id = 'qwen';       Bin = 'qwen';        Version = @('--version'); Update = @('npm', 'install', '-g', '@qwen-code/qwen-code@latest') }
  @{ Id = 'copilot';    Bin = 'copilot';     Version = @('--version'); Update = @('npm', 'install', '-g', '@github/copilot@latest') }
  @{ Id = 'opencode';   Bin = 'opencode';    Version = @('--version'); Update = @('opencode', 'upgrade') }
  @{ Id = 'cursor';     Bin = 'cursor-agent'; Version = @('--version'); Update = @('cursor-agent', 'update') }
  @{ Id = 'droid';      Bin = 'droid';       Version = @('--version'); Update = $null; Note = 'Factory droid auto-updates; reinstall from https://docs.factory.ai if needed' }
  @{ Id = 'vibe';       Bin = 'vibe';        Version = @('--version'); Update = $null; Note = 'update via the Mistral Vibe installer (vibe --setup / project docs)' }
  @{ Id = 'kiro';       Bin = 'kiro-cli';    Version = @('--version'); Update = $null; Note = 'update via the Kiro installer (https://kiro.dev/docs/cli/)' }
  @{ Id = 'goose';      Bin = 'goose';       Version = @('--version'); Update = $null; Note = 'update via the Block Goose installer (https://block.github.io/goose/)' }
  @{ Id = 'antigravity'; Bin = 'agy';        Version = @('--version'); Update = $null; Note = 'agy auto-updates; the antigravity adapter pins verified_version -- re-verify the brain/ transcript layout after a bump' }
  @{ Id = 'ollama';     Bin = 'ollama';      Version = @('--version'); Update = $null; Note = 'update via winget upgrade Ollama.Ollama or the ollama.com installer (optional local model)' }
  @{ Id = 'lmstudio';   Bin = 'lms';         Version = @('version');   Update = $null; Note = 'LM Studio self-updates in the desktop app (optional local model)' }
)

function Get-CliVersion {
  # NB: do not name the args param $Args -- that collides with PowerShell's automatic $args and the
  # version flag never binds (every CLI then runs bare and starts its interactive TUI).
  param([string]$Bin, [string[]]$VersionArgs, [int]$Timeout = 30)
  $job = Start-Job -ScriptBlock {
    param($b, $a)
    try { (& $b @(@($a)) 2>&1 | Out-String).Trim() } catch { "ERROR: $_" }
  } -ArgumentList $Bin, $VersionArgs
  if (Wait-Job $job -Timeout $Timeout) { $out = Receive-Job $job } else { $out = 'TIMEOUT' }
  Remove-Job $job -Force
  if (-not $out) { return '(no version output)' }
  # Strip ANSI, drop warning/path/banner noise, then prefer a version-looking line (some CLIs print a
  # deprecation warning or an ASCII banner before the actual version -- openhands, lms).
  $clean = $out -replace "\x1b\[[0-9;?]*[ -/]*[@-~]", ''
  $lines = $clean -split "`n" | ForEach-Object { $_.Trim() } |
    Where-Object { $_ -and $_ -notmatch 'Warning|Deprecation|site-packages|compatible before|^[A-Za-z]:\\|^/' }
  # Prefer the LAST version-looking line: a CLI that prints a warning/banner before its version (openhands,
  # lms) puts the real version last, while a single-line --version is unaffected.
  $version = $lines | Where-Object { $_ -match '\d+\.\d+|build |version is' } | Select-Object -Last 1
  if ($version) { return $version }
  if ($lines) { return $lines[0] }
  return '(no version output)'
}

function Invoke-Update {
  param([string[]]$Cmd, [int]$Timeout)
  $job = Start-Job -ScriptBlock { param($c) try { $o = & $c[0] @($c[1..($c.Count - 1)]) 2>&1 | Out-String; "EXIT=$LASTEXITCODE`n$o" } catch { "EXIT=1`nERROR: $_" } } -ArgumentList (, $Cmd)
  if (Wait-Job $job -Timeout $Timeout) { $out = Receive-Job $job } else { taskkill /FI "WINDOWTITLE eq *" 2>$null | Out-Null; $out = "EXIT=TIMEOUT (>$Timeout s)" }
  Remove-Job $job -Force
  return $out
}

$results = @()
foreach ($cli in $Clis) {
  if ($Only -and ($cli.Id -notin $Only)) { continue }
  $cmd = Get-Command $cli.Bin -ErrorAction SilentlyContinue
  if (-not $cmd) {
    Write-Host ("[{0,-12}] not installed (binary '{1}' not on PATH) -- skipped" -f $cli.Id, $cli.Bin) -ForegroundColor DarkGray
    $results += [pscustomobject]@{ Id = $cli.Id; Before = '-'; After = '-'; Status = 'not installed' }
    continue
  }
  $before = Get-CliVersion $cli.Bin $cli.Version
  if ($CheckOnly -or $null -eq $cli.Update) {
    $status = if ($null -eq $cli.Update) { 'report-only' } else { 'check-only' }
    Write-Host ("[{0,-12}] {1}" -f $cli.Id, $before) -ForegroundColor Cyan
    if ($null -eq $cli.Update -and $cli.Note) { Write-Host ("              update: {0}" -f $cli.Note) -ForegroundColor DarkGray }
    $results += [pscustomobject]@{ Id = $cli.Id; Before = $before; After = $before; Status = $status }
    continue
  }
  Write-Host ("[{0,-12}] {1} -- updating ({2}) ..." -f $cli.Id, $before, ($cli.Update -join ' ')) -ForegroundColor Cyan
  $out = Invoke-Update $cli.Update $TimeoutSec
  $after = Get-CliVersion $cli.Bin $cli.Version
  $status = if ($after -eq $before) { 'unchanged' } elseif ($after -match 'TIMEOUT|ERROR') { 'check failed' } else { 'updated' }
  $color = if ($status -eq 'updated') { 'Green' } else { 'Yellow' }
  Write-Host ("              -> {0}  [{1}]" -f $after, $status) -ForegroundColor $color
  if ($out -match 'EXIT=(TIMEOUT|1)|ERROR') { Write-Host ("              updater note: " + (($out -split "`n")[0])) -ForegroundColor DarkYellow }
  $results += [pscustomobject]@{ Id = $cli.Id; Before = $before; After = $after; Status = $status }
}

Write-Host "`n==== summary ====" -ForegroundColor White
$results | Format-Table -AutoSize Id, Before, After, Status
Write-Host "Re-verify drifted CLIs with: just test-integration  (see docs/cli-maintenance.md)" -ForegroundColor White
