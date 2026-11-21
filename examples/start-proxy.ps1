# Windows 下把代理挂起来的参考脚本。
# 用法：改成你自己的 Python 路径，然后放进开机启动，或者被别的启动脚本调用。
#
# 注意：不要用「另开一个终端窗口跑 python」然后关掉窗口的方式——
# 窗口一关进程就没了。Start-Process + -WindowStyle Hidden 才是常驻。

$ErrorActionPreference = 'Stop'

$python = 'python'                                   # 或 'C:\Python312\python.exe'
$script = Join-Path $PSScriptRoot '..\proxy\coopanion_proxy.py'
$port   = 8898

function PortOpen([int]$p) {
    return (Test-NetConnection -ComputerName '127.0.0.1' -Port $p -InformationLevel Quiet -WarningAction SilentlyContinue)
}

if (PortOpen $port) {
    Write-Host "代理已经在跑（$port），跳过"
    exit 0
}

if (-not (Test-Path $script)) {
    Write-Host "找不到 $script"
    exit 1
}

Write-Host "启动代理（$port）..."
Start-Process -FilePath $python -ArgumentList $script, $port -WindowStyle Hidden

# 等它起来
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (PortOpen $port) {
        Write-Host "起来了"
        exit 0
    }
}

Write-Host '没起来，看代理自己的日志（logs\coopanion_proxy.log）'
exit 1
