# 单台使用模板：复用现有模块。默认只检查，不创建云机。
[CmdletBinding()]
param(
    [ValidateSet('Check', 'Prepare', 'Login', 'Run')]
    [string]$Mode = 'Check',
    [ValidatePattern('^EDGE[A-Za-z0-9]+$')]
    [string]$DbId = 'EDGE5YDRZ1Q7D6TW',
    [ValidateNotNullOrEmpty()]
    [string]$ProxyId = 'proxy-004',
    [ValidatePattern('^[^\s@]+@[^\s@]+\.[^\s@]+$')]
    [string]$Email = 'account001@example.com',
    [ValidateSet('password', 'email_code')]
    [string]$LoginMode = 'password',
    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_]*$')]
    [string]$PasswordEnv = 'VMOS_TIKTOK_PASSWORD',
    [ValidateRange(1, 600)]
    [int]$CodeWait = 120,
    [switch]$AllowCurrentCode,
    [switch]$Plan
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$failureExitCode = 1

function Invoke-Module {
    param([string]$Title, [string[]]$Arguments)
    Write-Host "`n>>> $Title"
    $displayArgs = $Arguments | ForEach-Object { "'" + $_.Replace("'", "''") + "'" }
    Write-Host ('python -X utf8 ' + ($displayArgs -join ' '))
    if ($Plan) { return }
    & python -X utf8 @Arguments
    if ($LASTEXITCODE -ne 0) {
        $script:failureExitCode = $LASTEXITCODE
        throw "$Title 未通过（退出码 $LASTEXITCODE），已停止后续步骤。"
    }
}

Push-Location $projectRoot
try {
    if (-not $Plan) { $null = Get-Command python -ErrorAction Stop }
    if ($Mode -eq 'Check') {
        Invoke-Module -Title '阶段3/4：只读核验代理、地区和网页联网' -Arguments @(
            '.\阶段3-网络代理配置\setup_proxy.py', '--db-id', $DbId,
            '--proxy-id', $ProxyId, '--verify-only', '--wait-time', '60'
        )
        Invoke-Module -Title '阶段5：只读核验安装' -Arguments @(
            '.\阶段5-TikTok应用安装\install_tiktok.py', '--db-ids', $DbId, '--verify-only'
        )
        Invoke-Module -Title '阶段6：只读核验当前本人主页和账号绑定' -Arguments @(
            '.\阶段6-TikTok自动登录\auto_login.py', '--db-id', $DbId,
            '--email', $Email, '--verify-only'
        )
    }
    if ($Mode -in @('Prepare', 'Run')) {
        Invoke-Module -Title '阶段3/4：配置代理和地区，按需启动/重启并保留运行' -Arguments @(
            '.\阶段3-网络代理配置\setup_proxy.py', '--db-id', $DbId,
            '--proxy-id', $ProxyId, '--start-stopped', '--keep-running', '--wait-time', '60'
        )
        Invoke-Module -Title '阶段5：安装TikTok，已有包跳过上传' -Arguments @(
            '.\阶段5-TikTok应用安装\install_tiktok.py', '--db-ids', $DbId, '--start-stopped'
        )
    }
    if ($Mode -in @('Login', 'Run')) {
        $loginArgs = @(
            '.\阶段6-TikTok自动登录\auto_login.py', '--db-id', $DbId, '--email', $Email,
            '--login-mode', $LoginMode, '--code-wait', [string]$CodeWait,
            '--start-stopped', '--keep-running'
        )
        if ($LoginMode -eq 'password') { $loginArgs += @('--password-env', $PasswordEnv) }
        if ($AllowCurrentCode) { $loginArgs += '--allow-current-code' }
        Invoke-Module -Title '阶段6：登录或核验已有账号，保留运行' -Arguments $loginArgs
    }
    Write-Host "`n$(if ($Plan) { '预览结束，未执行任何模块。' } else { '所选步骤全部通过，结果见各阶段报告。' })"
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit $failureExitCode
} finally {
    Pop-Location
}
