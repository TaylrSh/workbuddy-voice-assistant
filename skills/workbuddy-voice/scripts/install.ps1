. (Join-Path $PSScriptRoot "common.ps1")

try {
    if (-not (Test-Path (Join-Path $script:PluginRoot "voice_assistant.py"))) {
        throw "Cannot find voice_assistant.py in the plugin directory."
    }

    New-Item -ItemType Directory -Force -Path $script:AppDir | Out-Null
    New-Item -ItemType Directory -Force -Path $script:DataRoot | Out-Null

    $copied = 0
    Get-ChildItem -LiteralPath $script:PluginRoot -File |
        Where-Object {
            $_.Extension -in @(".py", ".bat") -or
            $_.Name -in @("README.md", "LICENSE", "requirements.txt")
        } |
        ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $script:AppDir -Force
            $copied++
        }

    $assetSource = Join-Path $script:PluginRoot "assets"
    if (Test-Path $assetSource) {
        $assetTarget = Join-Path $script:AppDir "assets"
        New-Item -ItemType Directory -Force -Path $assetTarget | Out-Null
        Copy-Item -Path (Join-Path $assetSource "*") -Destination $assetTarget -Recurse -Force
    }

    if (-not (Test-Path $script:VenvPython)) {
        $basePython = Resolve-BasePython
        if (-not $basePython) {
            throw "Python 3.11 or newer was not found. Install Python and enable 'Add Python to PATH'."
        }
        $venvOutput = & $basePython -m venv --system-site-packages $script:VenvDir 2>&1 | Out-String
        if (-not (Test-Path $script:VenvPython)) {
            throw "Failed to create the Python virtual environment: $venvOutput"
        }
    }

    $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
    $usedSystemPackages = $false
    & $script:VenvPython -m pip install -r (Join-Path $script:AppDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        & $script:VenvPython -c "import numpy, sounddevice, edge_tts, PIL, win32gui" 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed and required packages are unavailable."
        }
        $usedSystemPackages = $true
    }

    Write-Result @{
        ok = $true
        installed = $true
        installDir = $script:AppDir
        copiedFiles = $copied
        python = $script:VenvPythonw
        usedSystemPackages = $usedSystemPackages
        autoStart = $false
    }
}
catch {
    Write-Result @{
        ok = $false
        installed = (Test-AssistantInstalled)
        error = $_.Exception.Message
    }
    exit 1
}
