@echo off
chcp 65001 >nul
setlocal

:: ============================================================
:: 开饭了助手 - 防火墙一键配置
:: 双击运行，会弹一次 UAC，之后永久生效
:: ============================================================

set RULE_NAME=KaiFanLeHelper
set UDP_PORT=8849
set TCP_PORT=8848
set HS_PORT=8850

:: ---------- 检查是否已提权 ----------
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo 需要管理员权限，正在申请提权...
    echo.
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

:: ---------- 已是管理员，开始写入规则 ----------
echo.
echo ============================================
echo   开饭了助手 - 防火墙配置
echo ============================================
echo.

echo [1/4] 删除旧规则（如果存在）...
netsh advfirewall firewall delete rule name="%RULE_NAME%_UDP" >nul 2>&1
netsh advfirewall firewall delete rule name="%RULE_NAME%_TCP" >nul 2>&1
netsh advfirewall firewall delete rule name="%RULE_NAME%_HS" >nul 2>&1

echo [2/4] 添加 UDP %UDP_PORT% 入站规则（接收手机广播）...
netsh advfirewall firewall add rule ^
    name="%RULE_NAME%_UDP" ^
    dir=in action=allow protocol=UDP localport=%UDP_PORT%
if %errorlevel% neq 0 (
    echo ❌ UDP 规则添加失败
) else (
    echo ✅ UDP %UDP_PORT% 已放行
)

echo [3/4] 添加 TCP %TCP_PORT% 入站规则（手机 HTTP 服务对接）...
netsh advfirewall firewall add rule ^
    name="%RULE_NAME%_TCP" ^
    dir=in action=allow protocol=TCP localport=%TCP_PORT%
if %errorlevel% neq 0 (
    echo ❌ TCP 规则添加失败
) else (
    echo ✅ TCP %TCP_PORT% 已放行
)

echo [4/4] 添加 TCP %HS_PORT% 入站规则（手机主动握手）...
netsh advfirewall firewall add rule ^
    name="%RULE_NAME%_HS" ^
    dir=in action=allow protocol=TCP localport=%HS_PORT%
if %errorlevel% neq 0 (
    echo ❌ TCP %HS_PORT% 规则添加失败
) else (
    echo ✅ TCP %HS_PORT% 已放行
)

echo.
echo ============================================
echo   配置完成！以后启动 App 无需再操作。
echo ============================================
echo.
echo 按任意键退出...
pause >nul

endlocal
