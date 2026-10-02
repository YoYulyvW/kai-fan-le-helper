@echo off
REM ============================================================
REM  一次性初始化本地嵌入式构建环境（需联网）
REM  - 下载 Python 3.8.10 embeddable
REM  - 配置 site-packages、安装 pip 24.3.1（最后支持 3.8 的版本）
REM  - 用本机 pip 预下载 cp38 wheels，嵌入式离线安装
REM  完成后可完全离线构建（build_local.bat）
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set PYDIR=build_env\python38
set PY=%PYDIR%\python.exe

if exist "%PY%" (
  echo 构建环境已存在: %PY%
  echo 如需重建，请先删除 build_env 目录
  exit /b 0
)

echo === 下载 Python 3.8.10 embeddable ===
if not exist build_env mkdir build_env
powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.8.10/python-3.8.10-embed-amd64.zip' -OutFile 'build_env\python38-embed.zip' -UseBasicParsing"

echo === 解压 ===
powershell -NoProfile -Command "Expand-Archive -Path 'build_env\python38-embed.zip' -DestinationPath '%PYDIR%' -Force"

echo === 配置 _pth（启用 site-packages） ===
powershell -NoProfile -Command "$p='%PYDIR%\python38._pth'; $c=@('python38.zip','.','Lib\site-packages','','# Uncomment to run site.main() automatically','import site'); Set-Content -Path $p -Value $c -Encoding ASCII"
if not exist "%PYDIR%\Lib\site-packages" mkdir "%PYDIR%\Lib\site-packages"

echo === 下载并安装 pip 24.3.1（最后支持 Python 3.8 的版本） ===
python -m pip download --only-binary=:all: --no-deps -d build_env\wheels "pip==24.3.1"
python -m pip install --no-index --no-deps --find-links build_env\wheels --target "%PYDIR%\Lib\site-packages" "pip==24.3.1"

echo === 预下载项目依赖 wheels（cp38 / win_amd64） ===
python -m pip download --only-binary=:all: --platform win_amd64 --python-version 38 --implementation cp -d build_env\wheels -r requirements.txt
python -m pip download --only-binary=:all: --platform win_amd64 --python-version 38 --implementation cp -d build_env\wheels setuptools wheel importlib_metadata

echo === 嵌入式环境离线安装依赖 ===
"%PY%" -m pip install --no-index --find-links build_env\wheels --no-warn-script-location -r requirements.txt

echo === 验证 ===
"%PY%" -c "import PySide2,PyInstaller,keyboard;print('PySide2',PySide2.__version__);print('PyInstaller',PyInstaller.__version__);print('keyboard ok')"

echo === 就绪，运行 build_local.bat 打包 ===
endlocal
