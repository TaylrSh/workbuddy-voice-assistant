$ErrorActionPreference = "Stop"

$bundledApp = Join-Path $PSScriptRoot "..\templates"
if (Test-Path -LiteralPath (Join-Path $bundledApp "voice_assistant.py")) {
    $script:PluginRoot = (Resolve-Path $bundledApp).Path
}
else {
    $script:PluginRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
}
$script:DataRoot = Join-Path $env:LOCALAPPDATA "WorkBuddyVoiceAssistant"
$script:AppDir = Join-Path $script:DataRoot "app"
$script:VenvDir = Join-Path $script:DataRoot "venv"
$script:VenvPython = Join-Path $script:VenvDir "Scripts\python.exe"
$script:VenvPythonw = Join-Path $script:VenvDir "Scripts\pythonw.exe"
$script:AppScript = Join-Path $script:AppDir "voice_assistant.py"
$script:PidFile = Join-Path $script:DataRoot "assistant.pid"
$script:LogFile = Join-Path $script:AppDir "voice-assistant.log"

function Test-AssistantInstalled {
    return (Test-Path $script:AppScript) -and (Test-Path $script:VenvPythonw)
}

function Get-AssistantProcesses {
    $needle = [regex]::Escape($script:AppScript)
    return @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.Name -like "python*" -and
                $_.CommandLine -and
                $_.CommandLine -match $needle
            }
    )
}

function Write-Result {
    param([hashtable]$Data)
    $Data | ConvertTo-Json -Compress -Depth 6
}

function Test-BasePython {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    & $Path -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Resolve-BasePython {
    $candidates = @()

    if ($env:WORKBUDDY_VOICE_PYTHON) {
        $candidates += $env:WORKBUDDY_VOICE_PYTHON
    }

    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $fromLauncher = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $fromLauncher) {
            $candidates += $fromLauncher[-1]
        }
    }

    $embedded = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions"
    if (Test-Path -LiteralPath $embedded) {
        $candidates += @(
            Get-ChildItem -LiteralPath $embedded -Directory |
                Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName "python.exe" }
        )
    }

    $localPython = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path -LiteralPath $localPython) {
        $candidates += @(
            Get-ChildItem -LiteralPath $localPython -Directory |
                Sort-Object Name -Descending |
                ForEach-Object { Join-Path $_.FullName "python.exe" }
        )
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $candidates += $python.Source
    }

    foreach ($candidate in $candidates) {
        if (Test-BasePython -Path $candidate) {
            return $candidate
        }
    }
    return $null
}

function Get-LogTail {
    param([int]$Lines = 20)
    if (Test-Path $script:LogFile) {
        return @(Get-Content -LiteralPath $script:LogFile -Tail $Lines)
    }
    return @()
}
