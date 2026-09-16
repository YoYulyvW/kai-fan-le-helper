@echo off
chcp 65001 >nul
setlocal

set RULE_NAME=KaiFanLeHelper

net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo 正在删除防火墙规则...
netsh advfirewall firewall delete rule name="%RULE_NAME%_UDP" >nul 2>&1
netsh advfirewall firewall delete rule name="%RULE_NAME%_TCP" >nul 2>&1
echo 完成。
pause >nul

endlocal
