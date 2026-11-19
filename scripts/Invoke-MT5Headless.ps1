<#
    Invoke-MT5Headless.ps1 -- PowerShell wrapper for mt5_headless.py and focus-safe MT5 launch.

    Usage:
        # Forward subcommands (discover, bind, portable, init, inspect, set, compile, test, report, compare)
        powershell -NoProfile -File scripts\Invoke-MT5Headless.ps1 discover
        powershell -NoProfile -File scripts\Invoke-MT5Headless.ps1 report sample_run1 --format md --out report.md

        # Launch a prepared tester .ini minimized with .env loading, terminal64 guard, and INI password scrubbing
        powershell -NoProfile -File scripts\Invoke-MT5Headless.ps1 -LaunchMinimized -IniPath "$env:TEMP\mt5-headless-bt\sample_run1.ini"
        powershell -NoProfile -File scripts\Invoke-MT5Headless.ps1 -LaunchMinimized -Portable -IniPath "$env:TEMP\mt5-headless-bt\sample_run1.ini"
#>
$LaunchMinimized = $false
$Portable = $false
$BootstrapPython = $false
$IniPath = ''
$EnvFile = ''
$TerminalExe = ''
$PythonExe = ''
$fwdList = New-Object System.Collections.Generic.List[string]

$argIdx = 0
while ($argIdx -lt $args.Count) {
    $tok = [string]$args[$argIdx]
    if ($tok -ceq '-LaunchMinimized' -or $tok -ieq '-LaunchMinimized') {
        if ($tok -notlike '--*') { $LaunchMinimized = $true; $argIdx++; continue }
    }
    if ($tok -ieq '-Portable' -and $tok -notlike '--*') { $Portable = $true; $argIdx++; continue }
    if (($tok -ieq '-BootstrapPython' -and $tok -notlike '--*') -or $tok -eq '--bootstrap-python') {
        $BootstrapPython = $true; $argIdx++; continue
    }
    if ($tok -ieq '-IniPath' -and $tok -notlike '--*' -and ($argIdx + 1) -lt $args.Count) {
        $IniPath = [string]$args[$argIdx + 1]; $argIdx += 2; continue
    }
    if ($tok -ieq '-EnvFile' -and $tok -notlike '--*' -and ($argIdx + 1) -lt $args.Count) {
        $EnvFile = [string]$args[$argIdx + 1]; $argIdx += 2; continue
    }
    if ($tok -ieq '-TerminalExe' -and $tok -notlike '--*' -and ($argIdx + 1) -lt $args.Count) {
        $TerminalExe = [string]$args[$argIdx + 1]; $argIdx += 2; continue
    }
    if ($tok -ieq '-PythonExe' -and $tok -notlike '--*' -and ($argIdx + 1) -lt $args.Count) {
        $PythonExe = [string]$args[$argIdx + 1]; $argIdx += 2; continue
    }
    $fwdList.Add($tok)
    $argIdx++
}
$ForwardArgs = $fwdList.ToArray()

$ErrorActionPreference = 'Stop'

function Test-RealPython {
    param([string]$Candidate)
    if (-not $Candidate -or ($Candidate -like '*\WindowsApps\*') -or (-not (Test-Path -LiteralPath $Candidate))) {
        return $false
    }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Candidate -c "import sys" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prev
    }
}

function Install-EmbeddedPython {
    param([string]$TargetDir)
    New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
    $pyExe = Join-Path $TargetDir 'python.exe'
    if (Test-RealPython $pyExe) { return $pyExe }
    $zipPath = Join-Path $TargetDir 'python-3.12.8-embed-amd64.zip'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Write-Output "Bootstrapping Python 3.12.8 embeddable runtime into $TargetDir ..."
    Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip' -OutFile $zipPath -UseBasicParsing
    Expand-Archive -LiteralPath $zipPath -DestinationPath $TargetDir -Force
    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    if (-not (Test-RealPython $pyExe)) {
        throw "Failed to bootstrap embeddable Python at: $pyExe"
    }
    return $pyExe
}

function Resolve-MT5Python {
    param([string]$ExplicitPython, [bool]$ForceBootstrap = $false)
    $skillRoot = Split-Path -Parent $PSScriptRoot
    $embedDir = Join-Path $skillRoot '.python'
    if ($ForceBootstrap) {
        return (Install-EmbeddedPython -TargetDir $embedDir)
    }

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($ExplicitPython) { $candidates.Add($ExplicitPython) }
    if ($env:MT5_PYTHON) { $candidates.Add($env:MT5_PYTHON) }
    $candidates.Add((Join-Path $embedDir 'python.exe'))
    $candidates.Add((Join-Path (Get-Location) '.python\python.exe'))

    foreach ($cmdName in @('python', 'python3')) {
        foreach ($cmd in @(Get-Command $cmdName -All -CommandType Application -ErrorAction SilentlyContinue)) {
            if ($cmd.Source -and ($cmd.Source -notlike '*\WindowsApps\*')) {
                $candidates.Add($cmd.Source)
            }
        }
    }

    $globs = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe'),
        'C:\Python*\python.exe',
        (Join-Path $env:ProgramFiles 'Python*\python.exe')
    )
    foreach ($g in $globs) {
        foreach ($hit in @(Get-Item $g -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
            $candidates.Add($hit.FullName)
        }
    }

    foreach ($c in $candidates) {
        if (Test-RealPython $c) { return $c }
    }

    $pyCmd = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pyCmd -and ($pyCmd.Source -notlike '*\WindowsApps\*')) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $resolved = (& $pyCmd.Source -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
            if ($resolved -and (Test-RealPython $resolved.Trim())) { return $resolved.Trim() }
        } finally {
            $ErrorActionPreference = $prev
        }
    }

    return (Install-EmbeddedPython -TargetDir $embedDir)
}

# Silently load .env into process environment variables without printing values
$dotEnvPath = if ($EnvFile -and (Test-Path -LiteralPath $EnvFile)) {
    $EnvFile
} elseif ($env:MT5_ENV_FILE -and (Test-Path -LiteralPath $env:MT5_ENV_FILE)) {
    $env:MT5_ENV_FILE
} elseif (Test-Path -LiteralPath (Join-Path (Get-Location) '.env')) {
    Join-Path (Get-Location) '.env'
} else {
    $null
}

if ($dotEnvPath) {
    foreach ($rawLine in (Get-Content -LiteralPath $dotEnvPath -Encoding UTF8)) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line.StartsWith('export ')) { $line = $line.Substring(7).Trim() }
        $eqIdx = $line.IndexOf('=')
        if ($eqIdx -lt 1) { continue }
        $key = $line.Substring(0, $eqIdx).Trim()
        $val = $line.Substring($eqIdx + 1).Trim()
        if ($val.Length -ge 2 -and (($val.StartsWith('"') -and $val.EndsWith('"')) -or ($val.StartsWith("'") -and $val.EndsWith("'")))) {
            $val = $val.Substring(1, $val.Length - 2)
        }
        $existing = [Environment]::GetEnvironmentVariable($key, 'Process')
        if ($key -and -not $existing -and $val -ne '') {
            [Environment]::SetEnvironmentVariable($key, $val, 'Process')
        }
    }
}

$pyScript = Join-Path $PSScriptRoot 'mt5_headless.py'
if (-not (Test-Path -LiteralPath $pyScript)) {
    throw "mt5_headless.py not found at: $pyScript"
}

$wantBootstrap = [bool]$BootstrapPython
if ($ForwardArgs -and ($ForwardArgs -contains '--bootstrap-python')) {
    $wantBootstrap = $true
    $ForwardArgs = @($ForwardArgs | Where-Object { $_ -ne '--bootstrap-python' })
}

$PythonExe = Resolve-MT5Python -ExplicitPython $PythonExe -ForceBootstrap:$wantBootstrap
if ($wantBootstrap -and (-not $LaunchMinimized) -and (-not $ForwardArgs -or $ForwardArgs.Count -eq 0)) {
    & $PythonExe --version
    exit $LASTEXITCODE
}

if ($LaunchMinimized) {
    if (-not $IniPath) {
        throw 'Specify -IniPath when using -LaunchMinimized.'
    }
    if (-not (Test-Path -LiteralPath $IniPath)) {
        throw "Tester INI not found: $IniPath"
    }

    if (-not $TerminalExe) {
        if ($env:MT5_TERMINAL_EXE) {
            $TerminalExe = $env:MT5_TERMINAL_EXE
        } elseif ($env:MT5_TERMINAL_DIR) {
            $TerminalExe = Join-Path $env:MT5_TERMINAL_DIR 'terminal64.exe'
        } elseif ($Portable -and (Test-Path -LiteralPath (Join-Path (Get-Location) '.mt5-portable\terminal64.exe'))) {
            $TerminalExe = Join-Path (Get-Location) '.mt5-portable\terminal64.exe'
        } else {
            $TerminalExe = 'C:\Program Files\MetaTrader 5\terminal64.exe'
        }
    }
    if (-not (Test-Path -LiteralPath $TerminalExe)) {
        throw "terminal64.exe not found: $TerminalExe"
    }

    $targetNorm = [IO.Path]::GetFullPath($TerminalExe)
    $runningProcs = Get-Process terminal64 -ErrorAction SilentlyContinue
    if ($runningProcs) {
        if (-not $Portable) {
            throw 'terminal64.exe is running: it owns the data folder and ShutdownTerminal=1 would close it. Close it gracefully via UI first (NEVER kill terminal64.exe).'
        }
        foreach ($rp in $runningProcs) {
            $rpPath = $null
            try { $rpPath = $rp.MainModule.FileName } catch {}
            if (-not $rpPath -or ([IO.Path]::GetFullPath($rpPath) -ieq $targetNorm)) {
                throw "Matching portable terminal64.exe is already running ($targetNorm). Wait for it to finish."
            }
        }
    }

    $absIni = (Resolve-Path -LiteralPath $IniPath).Path
    $argList = if ($Portable) { @('/portable', "/config:$absIni") } else { @("/config:$absIni") }

    try {
        $p = Start-Process -FilePath $TerminalExe -ArgumentList $argList -WindowStyle Minimized -PassThru
        $p.WaitForExit()
        Write-Output "Exit     : $($p.ExitCode)  (not a verdict; inspect report and logs)"
        exit $p.ExitCode
    }
    finally {
        & $PythonExe $pyScript scrub-ini $absIni | Out-Null
    }
}

if (-not $ForwardArgs -or $ForwardArgs.Count -eq 0) {
    & $PythonExe $pyScript --help
    exit $LASTEXITCODE
}

& $PythonExe $pyScript @ForwardArgs
exit $LASTEXITCODE
