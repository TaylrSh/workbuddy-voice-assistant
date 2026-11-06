. (Join-Path $PSScriptRoot "common.ps1")

try {
    $processes = @(Get-AssistantProcesses)
    $hotkey = $null
    $configPath = Join-Path $script:AppDir "voice-assistant-config.json"

    if (Test-Path $configPath) {
        try {
            $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
            $hotkey = $config.hotkey
        }
        catch {
            $hotkey = $null
        }
    }

    Write-Result @{
        ok = $true
        installed = (Test-AssistantInstalled)
        running = ($processes.Count -gt 0)
        pids = @($processes | ForEach-Object { [int]$_.ProcessId })
        installDir = $script:AppDir
        hotkey = $hotkey
        log = $script:LogFile
    }
}
catch {
    Write-Result @{
        ok = $false
        error = $_.Exception.Message
    }
    exit 1
}
