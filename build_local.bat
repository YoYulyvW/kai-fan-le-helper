@echo off
REM ============================================================
REM  本地构建脚本（离线、便携、不依赖 CI / 本机 Python）
REM  使用项目内嵌入式 Python 3.8.10 + 预置 wheels 打包
REM
REM  注意：PyInstaller 的 PySide2 hook 无法处理含中文的路径，
REM  因此本脚本会自动把项目复制到纯 ASCII 临时目录（默认 C:\kflbuild）
REM  构建，再把产物拷回 dist\。
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set PY=build_env\python38\python.exe
if not exist "%PY%" (
  echo [ERROR] 未找到嵌入式 Python: %PY%
  echo 请先运行 build_env_setup.bat 初始化构建环境
  exit /b 1
)

set BUILD_DIR=C:\kflbuild
echo === 准备 ASCII 构建目录: %BUILD_DIR% ===
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
mkdir "%BUILD_DIR%"

echo === 复制源码 ===
copy /y main.py "%BUILD_DIR%\" >nul
if exist relay_server.py copy /y relay_server.py "%BUILD_DIR%\" >nul
if exist requirements.txt copy /y requirements.txt "%BUILD_DIR%\" >nul

echo === 复制构建环境（约 300MB，稍候） ===
robocopy "build_env" "%BUILD_DIR%\build_env" /E /NFL /NDL /NJH /NJS /NP >nul

echo === 打包 ===
pushd "%BUILD_DIR%"
set PYTHONUTF8=1
"build_env\python38\python.exe" -m PyInstaller --noconfirm --onefile --windowed ^
  --name KaiFanLe-Helper --win-private-assemblies ^
  --distpath build_output --workpath build_temp --specpath build_temp --clean ^
  main.py
set RC=%ERRORLEVEL%
popd

if not "%RC%"=="0" (
  echo [ERROR] 打包失败
  exit /b 1
)

echo === 拷贝产物到 dist ===
if not exist dist mkdir dist
copy /y "%BUILD_DIR%\build_output\KaiFanLe-Helper.exe" "dist\KaiFanLe-Helper.exe" >nul
if not exist "dist\data" mkdir "dist\data"
if exist mappings.txt copy /y mappings.txt "dist\data\mappings.txt" >nul

echo === 生成 SHA256 ===
"%PY%" -c "import hashlib; p=r'dist\KaiFanLe-Helper.exe'; h=hashlib.sha256(open(p,'rb').read()).hexdigest().upper(); open('dist\KaiFanLe-Helper.exe.sha256','w',encoding='ascii').write(h); print('SHA256:', h)"

echo === 清理临时目录 ===
rmdir /s /q "%BUILD_DIR%"

echo === 完成: dist\KaiFanLe-Helper.exe ===
endlocal
