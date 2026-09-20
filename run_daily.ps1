# ============================================================================
# demo_bilibili_daily_report - daily driver
#
#   1. load ANTHROPIC_* from .env into the process environment
#   2. put python + npm-global on PATH
#   3. hand prompt_daily.txt to `claude -p` (unattended, skip-permissions)
#   4. log everything under logs\
#
# ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 as ANSI/GBK unless
# the file has a BOM, which would mangle any Chinese literal in here. All
# Chinese lives in prompt_daily.txt and is read with an explicit UTF-8 encoding.
# ============================================================================

$ErrorActionPreference = 'Continue'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $Root

$LogDir = Join-Path $Root 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$Stamp = Get-Date -Format 'yyyy-MM-dd_HHmmss'
$LogFile = Join-Path $LogDir ("run_" + $Stamp + ".log")

# UTF8Encoding($false) => no BOM. Add-Content -Encoding UTF8 in PS 5.1 writes a
# BOM on every append, which corrupts the file after the first write.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-Log {
    param([string]$Message)
    $line = "[" + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + "] " + $Message
    Write-Host $line
    [System.IO.File]::AppendAllText($LogFile, $line + [Environment]::NewLine, $Utf8NoBom)
}

Write-Log "=== demo_bilibili_daily_report start ==="
Write-Log ("root = " + $Root)

# --- 1. load .env -----------------------------------------------------------
$EnvFile = Join-Path $Root '.env'
if (-not (Test-Path $EnvFile)) {
    Write-Log ("ERROR: .env not found at " + $EnvFile)
    exit 1
}
$loadedKeys = @()
foreach ($line in [System.IO.File]::ReadAllLines($EnvFile, [System.Text.Encoding]::UTF8)) {
    $t = $line.Trim()
    if ($t.Length -eq 0 -or $t.StartsWith('#') -or -not $t.Contains('=')) { continue }
    $i = $t.IndexOf('=')
    $k = $t.Substring(0, $i).Trim()
    $v = $t.Substring($i + 1).Trim().Trim('"').Trim("'")
    if ($k.Length -eq 0) { continue }
    [Environment]::SetEnvironmentVariable($k, $v, 'Process')
    $loadedKeys += $k
}
Write-Log ("loaded from .env: " + ($loadedKeys -join ', '))
Write-Log ("ANTHROPIC_BASE_URL = " + $env:ANTHROPIC_BASE_URL)
Write-Log ("ANTHROPIC_MODEL    = " + $env:ANTHROPIC_MODEL)
if ([string]::IsNullOrEmpty($env:ANTHROPIC_AUTH_TOKEN)) {
    Write-Log "ERROR: ANTHROPIC_AUTH_TOKEN is empty after loading .env"
    exit 1
}
Write-Log "ANTHROPIC_AUTH_TOKEN = ***set***"

# --- 2. PATH ----------------------------------------------------------------
# NOTE: the requested C:\Python312 does not exist on this machine.
# The real interpreter is the user-scope install below (see memory note
# "python-on-this-machine"). Both are added, python312 first so that a future
# install at that path would win.
$PyCandidates = @(
    'C:\Python312',
    'C:\Python312\Scripts',
    (Join-Path $env:LOCALAPPDATA 'Python\bin')
)
$NpmGlobal = Join-Path $env:APPDATA 'npm'

$pathParts = @()
foreach ($p in $PyCandidates) { if (Test-Path $p) { $pathParts += $p } }
if (Test-Path $NpmGlobal) { $pathParts += $NpmGlobal }
$env:Path = (($pathParts + @($env:Path)) -join ';')
Write-Log ("PATH prepended: " + ($pathParts -join '; '))

$PythonExe = $null
foreach ($p in $PyCandidates) {
    $cand = Join-Path $p 'python.exe'
    if (Test-Path $cand) { $PythonExe = $cand; break }
}
if ($null -eq $PythonExe) {
    Write-Log "ERROR: no python.exe found in the expected locations"
    exit 1
}
Write-Log ("python = " + $PythonExe)
$env:PYTHONIOENCODING = 'utf-8'

$ClaudeCmd = Join-Path $NpmGlobal 'claude.cmd'
if (-not (Test-Path $ClaudeCmd)) {
    Write-Log ("ERROR: claude.cmd not found at " + $ClaudeCmd)
    exit 1
}
Write-Log ("claude = " + $ClaudeCmd)

# --- 3. read the task card --------------------------------------------------
$PromptFile = Join-Path $Root 'prompt_daily.txt'
if (-not (Test-Path $PromptFile)) {
    Write-Log ("ERROR: prompt file not found at " + $PromptFile)
    exit 1
}
$Prompt = [System.IO.File]::ReadAllText($PromptFile, [System.Text.Encoding]::UTF8)
Write-Log ("prompt loaded, " + $Prompt.Length + " chars")

# --- 4. run the agent -------------------------------------------------------
# The task card goes in via stdin rather than as a command-line argument: it is
# multi-line Chinese, and routing that through the .cmd shim's command line is
# where encoding and length limits bite.
#
# Start-Process with -Redirect* hands the redirection to the OS, so the agent's
# bytes never pass through PowerShell's stream decoding. Piping through the PS
# pipeline instead mangles the child's output (it arrives as if UTF-16 read as
# single-byte) and wraps native stderr into NativeCommandError records.
$AgentOut = Join-Path $LogDir ("agent_" + $Stamp + ".out.txt")
$AgentErr = Join-Path $LogDir ("agent_" + $Stamp + ".err.txt")

Write-Log "launching: claude -p --dangerously-skip-permissions (prompt via stdin)"
$started = Get-Date
try {
    $proc = Start-Process -FilePath $ClaudeCmd `
        -ArgumentList '-p', '--dangerously-skip-permissions' `
        -RedirectStandardInput $PromptFile `
        -RedirectStandardOutput $AgentOut `
        -RedirectStandardError $AgentErr `
        -NoNewWindow -Wait -PassThru
    $agentExit = $proc.ExitCode
} catch {
    Write-Log ("ERROR: agent invocation threw: " + $_.Exception.Message)
    $agentExit = 99
}
$elapsed = [int]((Get-Date) - $started).TotalSeconds

# fold the agent's own stdout/stderr into the run log, as UTF-8
foreach ($f in @($AgentOut, $AgentErr)) {
    if (Test-Path $f) {
        $text = [System.IO.File]::ReadAllText($f, [System.Text.Encoding]::UTF8)
        if ($text.Trim().Length -gt 0) {
            $tag = if ($f -eq $AgentOut) { 'agent stdout' } else { 'agent stderr' }
            Write-Log ("---- " + $tag + " ----")
            [System.IO.File]::AppendAllText($LogFile, $text + [Environment]::NewLine, $Utf8NoBom)
        }
    }
}
Write-Log "---- end of agent output ----"
Write-Log ("agent finished, exit code = " + $agentExit + ", elapsed = " + $elapsed + "s")

# --- 5. report what was produced -------------------------------------------
$Today = Get-Date -Format 'yyyy-MM-dd'
$DayDir = Join-Path $Root ("daily_out\" + $Today)
$Brief = Join-Path $DayDir 'daily_brief.md'
$Chart = Join-Path $DayDir 'daily_top10.png'
Write-Log ("daily_brief.md exists = " + (Test-Path $Brief))
Write-Log ("daily_top10.png exists = " + (Test-Path $Chart))

Write-Log ("=== done (exit " + $agentExit + ") ===")
Write-Log ("log file: " + $LogFile)
exit $agentExit
