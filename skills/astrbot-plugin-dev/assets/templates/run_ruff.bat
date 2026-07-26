@echo off
setlocal EnableExtensions

chcp 65001 >nul

if "%HOLD_WINDOW%"=="" set "HOLD_WINDOW=1"

set "PLUGIN_DIR=%~dp0"
for %%I in ("%PLUGIN_DIR%") do set "PLUGIN_DIR=%%~fI"

set "ASTRBOT_ROOT=%PLUGIN_DIR%..\..\.."
for %%I in ("%ASTRBOT_ROOT%") do set "ASTRBOT_ROOT=%%~fI"

set "FMT_LOG=%TEMP%\ruff_format_%RANDOM%.log"
set "CHK_LOG=%TEMP%\ruff_check_%RANDOM%.log"
set "PRE_JSON=%TEMP%\ruff_pre_%RANDOM%.json"
set "POST_JSON=%TEMP%\ruff_post_%RANDOM%.json"

set "FMT_COUNTS=%TEMP%\ruff_fmt_counts_%RANDOM%.txt"

set "FMT_REFORMATTED=0"
set "FMT_UNCHANGED=0"
set "CHK_FOUND=0"
set "CHK_FIXED=0"
set "CHK_REMAINING=0"

echo ============================================================
echo [RUFF] AstrBot 插件代码整理脚本
echo ============================================================
echo [INFO] AstrBot root: %ASTRBOT_ROOT%
echo [INFO] Plugin dir  : %PLUGIN_DIR%
echo [INFO] Hold window : %HOLD_WINDOW%
echo.

echo [STEP 1/7] 进入 AstrBot 根目录...
cd /d "%ASTRBOT_ROOT%"
if errorlevel 1 goto :err_root

echo [STEP 2/7] 检查虚拟环境激活脚本...
if not exist ".\venv\Scripts\activate.bat" goto :err_venv

echo [STEP 3/7] 激活虚拟环境...
call ".\venv\Scripts\activate.bat"
if errorlevel 1 goto :err_activate

echo [STEP 4/7] 显示 Ruff 版本信息...
ruff --version
if errorlevel 1 goto :err_ruff

echo [STEP 5/7] 切回插件目录...
cd /d "%PLUGIN_DIR%"
if errorlevel 1 goto :err_plugin

echo.
echo [RUN] ruff format .
ruff format . > "%FMT_LOG%" 2>&1
set "FORMAT_EXIT=%ERRORLEVEL%"
type "%FMT_LOG%"
if not "%FORMAT_EXIT%"=="0" goto :err_format

REM 解析 ruff format 输出（兼容 file/files 与 ANSI 颜色）
python -c "import re;s=open(r'%FMT_LOG%','r',encoding='utf-8',errors='ignore').read();s=re.sub(r'\x1b\[[0-9;]*m','',s);m=re.search(r'(\d+)\s+files?\s+reformatted,\s+(\d+)\s+files?\s+left unchanged',s);n=re.search(r'(\d+)\s+files?\s+left unchanged',s);print((m.group(1)+','+m.group(2)) if m else ('0,'+n.group(1) if n else '0,0'))" > "%FMT_COUNTS%" 2>nul
for /f "usebackq tokens=1,2 delims=," %%a in ("%FMT_COUNTS%") do (
    set "FMT_REFORMATTED=%%a"
    set "FMT_UNCHANGED=%%b"
)

echo.
echo [STEP 6/7] 采集修复前问题数（JSON）...
ruff check --output-format json . > "%PRE_JSON%"
for /f %%i in ('python -c "import json;print(len(json.load(open(r\"%PRE_JSON%\", \"r\", encoding=\"utf-8\"))))" 2^>nul') do set "CHK_FOUND=%%i"

echo.
echo [RUN] ruff check --fix .
ruff check --fix . > "%CHK_LOG%" 2>&1
type "%CHK_LOG%"

echo.
echo [STEP 7/7] 采集修复后剩余问题数（JSON）...
ruff check --output-format json . > "%POST_JSON%"
for /f %%i in ('python -c "import json;print(len(json.load(open(r\"%POST_JSON%\", \"r\", encoding=\"utf-8\"))))" 2^>nul') do set "CHK_REMAINING=%%i"

set /a CHK_FIXED=CHK_FOUND-CHK_REMAINING
if %CHK_FIXED% LSS 0 set "CHK_FIXED=0"

echo.
echo ------------------ Ruff Summary ------------------
echo Reformatted files : %FMT_REFORMATTED%
echo Unchanged files   : %FMT_UNCHANGED%
echo Found errors      : %CHK_FOUND%
echo Fixed errors      : %CHK_FIXED%
echo Remaining errors  : %CHK_REMAINING%
echo --------------------------------------------------

if "%CHK_REMAINING%"=="0" (
    echo [DONE] All checked pass!
    goto :ok
)

goto :err_remaining

:err_root
echo [ERROR] 无法进入 AstrBot 根目录: %ASTRBOT_ROOT%
goto :fail

:err_venv
echo [ERROR] 未找到虚拟环境激活脚本: .\venv\Scripts\activate.bat
goto :fail

:err_activate
echo [ERROR] 虚拟环境激活失败
goto :fail

:err_ruff
echo [ERROR] 无法执行 ruff --version，请确认 ruff 已安装到当前虚拟环境
goto :fail

:err_plugin
echo [ERROR] 无法返回插件目录: %PLUGIN_DIR%
goto :fail

:err_format
echo [ERROR] ruff format 执行失败
goto :fail

:err_remaining
echo [ERROR] 仍存在未修复问题，请根据上方输出处理后重试。
goto :fail

:ok
set "EXIT_CODE=0"
goto :cleanup

:fail
echo.
echo [FAILED] 脚本执行失败，请根据上方日志排查问题。
set "EXIT_CODE=1"

:cleanup
if exist "%FMT_LOG%" del /q "%FMT_LOG%" >nul 2>nul
if exist "%CHK_LOG%" del /q "%CHK_LOG%" >nul 2>nul
if exist "%PRE_JSON%" del /q "%PRE_JSON%" >nul 2>nul
if exist "%POST_JSON%" del /q "%POST_JSON%" >nul 2>nul

if exist "%FMT_COUNTS%" del /q "%FMT_COUNTS%" >nul 2>nul

if "%HOLD_WINDOW%"=="1" (
    echo.
    pause
)

endlocal & exit /b %EXIT_CODE%
