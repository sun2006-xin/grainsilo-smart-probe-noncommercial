param(
    [switch]$NoBrowser,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$stationDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$baseUrl = "http://127.0.0.1:$Port/"
$healthUrl = "http://127.0.0.1:$Port/api/v1/health"
$pingUrl = "http://127.0.0.1:$Port/api/ping"
$requiredSchemaVersion = 10
$pythonPath = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

function Test-StationReady {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if (-not $response.ok -or [int]$response.schema_version -lt $requiredSchemaVersion) {
            return $false
        }
        foreach ($page in @("index.html", "alerts.html", "environment.html", "probes.html", "piles.html", "db.html", "crop-models.html", "warehouses.html")) {
            $check = Invoke-WebRequest -Uri ($baseUrl + $page) -TimeoutSec 2 -UseBasicParsing
            if ([int]$check.StatusCode -ne 200) { return $false }
        }
        return $true
    }
    catch {
        return $false
    }
}

if (-not (Test-StationReady)) {
    $existingService = $false
    try {
        $ping = Invoke-RestMethod -Uri $pingUrl -TimeoutSec 2
        $existingService = [bool]$ping.ok
    }
    catch { }
    if ($existingService) {
        $runningSchema = "未知版本"
        try { $runningSchema = (Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2).schema_version }
        catch { }
        throw "检测到 $Port 端口已有旧版 GrainSilo 服务（schema $runningSchema），新页面尚未加载。为避免打断 S3 数据上报，启动器没有强制结束它。确认允许短暂中断后，请先关闭旧中心站进程，再重新打开本启动器。"
    }
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw "找不到与 S3 窄防火墙规则对应的 Python 运行环境：$pythonPath"
    }

    $logDir = Join-Path $env:LOCALAPPDATA "GrainSilo\logs"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdoutPath = Join-Path $logDir "station-$Port-$stamp.out.log"
    $stderrPath = Join-Path $logDir "station-$Port-$stamp.err.log"
    $serverPath = Join-Path $stationDir "server.py"
    $process = Start-Process -FilePath $pythonPath `
        -ArgumentList @(('"{0}"' -f $serverPath), [string]$Port, "--no-browser") `
        -WorkingDirectory $stationDir -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        if (Test-StationReady) {
            $ready = $true
            break
        }
        $process.Refresh()
        if ($process.HasExited) { break }
    }

    if (-not $ready) {
        $process.Refresh()
        if (-not $process.HasExited) {
            $owned = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.Id)" -ErrorAction SilentlyContinue
            if ($owned -and $owned.ExecutablePath -eq $pythonPath -and
                $owned.CommandLine -like "*$serverPath*") {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
        $stderr = if (Test-Path -LiteralPath $stderrPath) {
            (Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue).Trim()
        } else { "" }
        throw "新中心站未在 30 秒内启动并通过页面检查。日志：$stdoutPath；$stderrPath。$stderr"
    }
}

if ($NoBrowser) {
    Write-Output "GrainSilo 中心站已就绪：$baseUrl"
    exit 0
}

Start-Process -FilePath $baseUrl
Write-Output "已打开 GrainSilo 中心站：$baseUrl"
