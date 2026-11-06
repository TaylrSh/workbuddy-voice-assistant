. (Join-Path $PSScriptRoot "common.ps1")

try {
    if (-not (Test-AssistantInstalled)) {
        throw "Voice assistant is not installed. Run install.ps1 first."
    }

    $existing = @(Get-AssistantProcesses)
    if ($existing.Count -gt 0) {
        Write-Result @{
            ok = $true
            running = $true
            alreadyRunning = $true
            pids = @($existing | ForEach-Object { [int]$_.ProcessId })
            installDir = $script:AppDir
        }
        exit 0
    }

    New-Item -ItemType Directory -Force -Path $script:DataRoot | Out-Null
    $commandLine = '"' + $script:VenvPythonw + '" "' + $script:AppScript + '"'
    $created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = $commandLine
        CurrentDirectory = $script:AppDir
    }
    if ($created.ReturnValue -ne 0 -or -not $created.ProcessId) {
        throw "Failed to create the assistant process. WMI return code: $($created.ReturnValue)"
    }

    $processId = [int]$created.ProcessId
    $processId | Set-Content -LiteralPath $script:PidFile -Encoding ASCII
    Start-Sleep -Milliseconds 1500

    $alive = @(Get-AssistantProcesses | Where-Object { [int]$_.ProcessId -eq $processId }).Count -gt 0
    if (-not $alive) {
        Write-Result @{
            ok = $false
            running = $false
            exitCode = $null
            log = Get-LogTail -Lines 20
        }
        exit 1
    }

    Write-Result @{
        ok = $true
        running = $true
        alreadyRunning = $false
        pids = @($processId)
        installDir = $script:AppDir
        log = $script:LogFile
    }
}
catch {
    Write-Result @{
        ok = $false
        running = $false
        error = $_.Exception.Message
    }
    exit 1
}
