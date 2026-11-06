. (Join-Path $PSScriptRoot "common.ps1")

try {
    $processes = @(Get-AssistantProcesses)
    $pids = @($processes | ForEach-Object { [int]$_.ProcessId })

    foreach ($item in $processes) {
        Stop-Process -Id $item.ProcessId -Force -Confirm:$false
    }

    if (Test-Path $script:PidFile) {
        Remove-Item -LiteralPath $script:PidFile -Force
    }

    Write-Result @{
        ok = $true
        running = $false
        stopped = $pids.Count
        pids = $pids
    }
}
catch {
    Write-Result @{
        ok = $false
        error = $_.Exception.Message
    }
    exit 1
}
