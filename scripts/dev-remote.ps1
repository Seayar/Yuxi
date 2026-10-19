<#
.SYNOPSIS
    在 Windows 本机启动 Yuxi 开发栈，中间件使用远端测试环境。

.DESCRIPTION
    本机只运行 api、worker、web 与 sandbox-provisioner；PostgreSQL、Redis、MinIO、
    Milvus 与 MinerU 由 remote-dev-tunnel 容器经 SSH 隧道转发到远端测试环境。
    中间件地址、SSH 目标与凭据全部来自 .env，本脚本不保存任何地址。

    本机中间件服务（postgres/redis/minio/milvus/etcd/graph/tugraph）不参与启动，
    因此所有 compose 调用都带 --no-deps，并显式列出需要的服务。

.EXAMPLE
    ./scripts/dev-remote.ps1 up
    ./scripts/dev-remote.ps1 status
    ./scripts/dev-remote.ps1 logs
    ./scripts/dev-remote.ps1 down
#>
[CmdletBinding()]
param(
    [ValidateSet('up', 'down', 'restart', 'status', 'logs')]
    [string]$Action = 'up'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$composeFiles = @('-f', 'docker-compose.yml', '-f', 'docker-compose.remote-dev.yml')
$appServices = @('api', 'worker', 'web', 'sandbox-provisioner')
$apiPort = if ($env:YUXI_API_PORT) { $env:YUXI_API_PORT } else { '5050' }

function Invoke-Compose {
    param([string[]]$ComposeCommand)

    $arguments = $composeFiles + $ComposeCommand
    # docker 把构建进度写到 stderr；Stop 会把它当成终止性错误，这里改为按退出码判断。
    $ErrorActionPreference = 'Continue'
    & docker compose @arguments
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($exitCode -ne 0) {
        throw "docker compose $($ComposeCommand -join ' ') 失败（exit $exitCode）"
    }
}

function Wait-TunnelHealthy {
    param([int]$TimeoutSeconds = 90)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $containerId = (& docker compose @composeFiles ps -q remote-dev-tunnel 2>$null | Select-Object -First 1)
        if ($containerId) {
            $status = (& docker inspect --format '{{.State.Health.Status}}' $containerId 2>$null)
            if ($status -eq 'healthy') { return }
        }
        Start-Sleep -Seconds 3
    }
    throw "隧道容器 $TimeoutSeconds 秒内未就绪；用 ./scripts/dev-remote.ps1 logs 查看 SSH 报错"
}

function Wait-ApiReady {
    param([int]$TimeoutSeconds = 240)

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $url = "http://127.0.0.1:$apiPort/api/system/ready"
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-RestMethod -Uri $url -TimeoutSec 10
            if ($response.status -eq 'ready') {
                Write-Host "API 就绪：$url"
                $response | ConvertTo-Json -Depth 5
                return
            }
            Write-Host "尚未就绪：$($response | ConvertTo-Json -Depth 5 -Compress)"
        }
        catch {
            Write-Host "等待 API 启动：$($_.Exception.Message)"
        }
        Start-Sleep -Seconds 5
    }
    throw "API 在 $TimeoutSeconds 秒内未就绪；用 ./scripts/dev-remote.ps1 logs 查看 api/worker 日志"
}

if (-not (Test-Path '.env')) {
    throw '缺少 .env；请按 docs/develop-guides/local-remote-middleware.md 准备'
}

switch ($Action) {
    'up' {
        Write-Host '[1/4] 启动 SSH 隧道容器'
        Invoke-Compose @('up', '-d', '--no-deps', '--build', 'remote-dev-tunnel')
        Wait-TunnelHealthy

        Write-Host '[2/4] 运行 storage-migrator（校验远端 Schema 版本）'
        Invoke-Compose @('run', '--rm', '--no-deps', 'storage-migrator')

        Write-Host '[3/4] 启动 api / worker / web / sandbox-provisioner'
        Invoke-Compose (@('up', '-d', '--no-deps') + $appServices)

        Write-Host '[4/4] 等待 /api/system/ready'
        Wait-ApiReady
    }
    'down' {
        Invoke-Compose @('down')
    }
    'restart' {
        Invoke-Compose @('down')
        & $PSCommandPath up
    }
    'status' {
        Invoke-Compose @('ps', '--all')
    }
    'logs' {
        Invoke-Compose (@('logs', '--tail', '100') + @('remote-dev-tunnel', 'api', 'worker'))
    }
}
