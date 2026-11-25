@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "VENV=.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "MARK=%VENV%\requirements.installed"
set "CHECK_X64=import sysconfig,sys;sys.exit(sysconfig.get_platform()!='win-amd64')"

rem ── 0. 判斷環境 ──
rem ARM64 Windows 上，tkinterdnd2 附的 ARM64 拖放元件與 Python 內建的 Tcl 8.6 不相容，
rem 所以優先使用 x64 版 Python（系統會自動模擬執行），拖放才能用。
set "IS_ARM=0"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "IS_ARM=1"
if /i "%PROCESSOR_ARCHITEW6432%"=="ARM64" set "IS_ARM=1"

set "X64PY="
where py >nul 2>nul
if not errorlevel 1 call :find_x64

rem ARM64 上若舊的 venv 不是 x64，而現在找得到 x64 Python，就重建
if not exist "%VPY%" goto make_venv
if "%IS_ARM%"=="0" goto have_venv
if not defined X64PY goto have_venv
"%VPY%" -c "%CHECK_X64%" >nul 2>nul
if not errorlevel 1 goto have_venv
echo [設定] 偵測到 x64 版 Python，改用它重建虛擬環境（啟用拖放）...
rmdir /s /q "%VENV%"

rem ── 1. 建立 venv ──
:make_venv
set "BASEPY=%X64PY%"
if defined BASEPY goto do_make
if "%IS_ARM%"=="1" (
    echo [提醒] 找不到 x64 版 Python，改用預設 Python；拖放檔案功能將無法使用。
    echo        安裝 x64 版 Python 後再執行本檔，會自動切換。
)
where py >nul 2>nul
if not errorlevel 1 (set "BASEPY=py -3") else (set "BASEPY=python")
:do_make
echo [設定] 正在用「%BASEPY%」建立虛擬環境 %VENV% ...
%BASEPY% -m venv "%VENV%"
if not exist "%VPY%" (
    echo [錯誤] 無法建立虛擬環境，請確認已安裝 Python 3（含 py 啟動器或已加入 PATH）。
    pause
    exit /b 1
)
:have_venv

rem ── 2. requirements.txt 有變動才重新安裝套件 ──
if exist "%MARK%" (
    fc /b "requirements.txt" "%MARK%" >nul 2>nul
    if not errorlevel 1 goto run_app
)
echo [設定] 正在安裝套件 ...
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q
"%VPY%" -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 (
    echo [錯誤] 套件安裝失敗，請檢查網路連線或上方錯誤訊息。
    pause
    exit /b 1
)
copy /y "requirements.txt" "%MARK%" >nul

rem ── 3. 啟動 GUI（用 pythonw，不留主控台視窗）──
:run_app
start "" "%VENV%\Scripts\pythonw.exe" app.py
endlocal
exit /b 0

rem ── 找 x64 版 Python（py 啟動器的版本標籤，x64 版沒有 -arm64 / -32 後綴）──
:find_x64
for %%V in (3.14 3.13 3.12 3.11 3.10) do (
    if not defined X64PY (
        py -V:%%V -c "%CHECK_X64%" >nul 2>nul && set "X64PY=py -V:%%V"
    )
)
exit /b 0
