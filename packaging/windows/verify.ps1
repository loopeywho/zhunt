param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\Zhunt"),
    [int]$Port = 4000
)

$ErrorActionPreference = "Stop"

$executable = Join-Path $InstallRoot "zhunt.exe"
if (-not (Test-Path $executable)) {
    throw "Zhunt executable not found at $executable"
}

& $executable --help | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "zhunt --help failed with exit code $LASTEXITCODE"
}

$process = Start-Process `
    -FilePath $executable `
    -ArgumentList @("serve", "--port", "$Port") `
    -PassThru `
    -WindowStyle Hidden

try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Seconds 1
        if ($process.HasExited) {
            throw "zhunt serve exited early with code $($process.ExitCode)"
        }
        if ((Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet)) {
            $ready = $true
            break
        }
    }
    if (-not $ready) {
        throw "zhunt serve did not open localhost:$Port within 20 seconds"
    }

    $body = '{"model":"zhunt-auto","messages":[{"role":"user","content":"health check"}]}'
    try {
        Invoke-WebRequest `
            -Uri "http://127.0.0.1:$Port/v1/chat/completions" `
            -Method Post `
            -ContentType "application/json" `
            -Body $body `
            -UseBasicParsing | Out-Null
        throw "Unauthenticated request unexpectedly succeeded"
    } catch {
        $status = $_.Exception.Response.StatusCode.value__
        if ($status -ne 401) {
            throw "Expected HTTP 401 from unauthenticated request, got $status"
        }
    }
} finally {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
}

$envFile = Join-Path $env:USERPROFILE ".zhunt\env"
if (-not (Test-Path $envFile)) {
    throw "Expected local Zhunt environment file at $envFile"
}

Write-Host "Windows smoke verification passed. Review ACLs with:"
Write-Host "  icacls `"$envFile`""
