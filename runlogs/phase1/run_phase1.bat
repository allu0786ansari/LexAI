@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  LexAI Phase 1 verification - Windows Command Prompt (cmd.exe), no uv needed
REM
REM  From the repository folder:
REM     runlogs\phase1\run_phase1.bat                 all steps except "outage"
REM     runlogs\phase1\run_phase1.bat smoke           one step
REM     runlogs\phase1\run_phase1.bat ingest smoke    several steps
REM     runlogs\phase1\run_phase1.bat outage          stops Ollama, tests, restarts it
REM
REM  Steps: check setup quality models ingest smoke outage
REM  Each step writes a numbered log file in runlogs\phase1\output
REM ===========================================================================

cd /d "%~dp0..\.."
set "ROOT=%CD%"
set "BACKEND=%ROOT%\backend"
set "OUT=%ROOT%\runlogs\phase1\output"
set "VPY=%BACKEND%\.venv\Scripts\python.exe"
if not exist "%OUT%" mkdir "%OUT%"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "LEXAI_API_ENVIRONMENT=ci"

set "STEPS=%*"
if "%STEPS%"=="" set "STEPS=check setup quality models ingest smoke"

for %%S in (%STEPS%) do call :dispatch %%S

echo.
echo Done. Logs are in runlogs\phase1\output\
endlocal
goto :eof


REM ---------------------------------------------------------------------------
:dispatch
echo.
echo ########## %~1 ##########
if /I "%~1"=="check"   goto step_check
if /I "%~1"=="setup"   goto step_setup
if /I "%~1"=="quality" goto step_quality
if /I "%~1"=="models"  goto step_models
if /I "%~1"=="ingest"  goto step_ingest
if /I "%~1"=="smoke"   goto step_smoke
if /I "%~1"=="outage"  goto step_outage
echo Unknown step "%~1". Use: check setup quality models ingest smoke outage
goto :eof


REM ---------------------------------------------------------------------------
REM  call :run LOGFILE  - runs the command in the CMD variable and appends it, its output and exit code
:run
>>"%~1" echo $ %CMD%
%CMD% >>"%~1" 2>&1
>>"%~1" echo exit=%ERRORLEVEL%
>>"%~1" echo.
goto :eof


REM ---------------------------------------------------------------------------
REM  Sets PY to a Python 3.12 or 3.13 launcher command, or leaves it empty.
:find_python
set "PY="
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.12" & goto :eof
py -3.13 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.13" & goto :eof
python -c "import sys; v = sys.version_info; sys.exit(0 if v[0] == 3 and v[1] in [12, 13] else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
goto :eof


REM ---------------------------------------------------------------------------
:step_check
set "LOG=%OUT%\01_check.log"
type nul > "%LOG%"
set "CMD=ver" & call :run "%LOG%"
set "CMD=git -C "%ROOT%" rev-parse --abbrev-ref HEAD" & call :run "%LOG%"
set "CMD=git -C "%ROOT%" rev-parse HEAD" & call :run "%LOG%"
set "CMD=py -0p" & call :run "%LOG%"
set "CMD=python --version" & call :run "%LOG%"
set "CMD=where python" & call :run "%LOG%"
call :find_python
>>"%LOG%" echo Python chosen for LexAI: [%PY%]
>>"%LOG%" echo.
set "CMD=ollama --version" & call :run "%LOG%"
set "CMD=curl -s http://localhost:11434/api/version" & call :run "%LOG%"
set "CMD=nslookup registry.ollama.ai" & call :run "%LOG%"
set "CMD=nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv" & call :run "%LOG%"
>>"%LOG%" echo $ systeminfo (memory lines)
systeminfo | findstr /C:"Total Physical Memory" /C:"Available Physical Memory" >>"%LOG%" 2>&1
>>"%LOG%" echo.
type "%LOG%"
goto :eof


REM ---------------------------------------------------------------------------
:step_setup
set "LOG=%OUT%\02_setup.log"
type nul > "%LOG%"
call :find_python
if not defined PY goto setup_no_python
>>"%LOG%" echo Using Python: %PY%
pushd "%BACKEND%"
if exist ".venv\Scripts\python.exe" goto setup_have_venv
set "CMD=%PY% -m venv .venv" & call :run "%LOG%"
:setup_have_venv
set "CMD=.venv\Scripts\python.exe -m pip install --upgrade pip --progress-bar off" & call :run "%LOG%"
set "CMD=.venv\Scripts\python.exe -m pip install -r requirements-dev.txt --progress-bar off" & call :run "%LOG%"
set "CMD=.venv\Scripts\python.exe -m pip install --no-deps -e . --progress-bar off" & call :run "%LOG%"
REM A .env without a "lexai-profile:" line is left over from v0.1: keep a copy, replace it.
if not exist ".env" goto setup_write_env
findstr /C:"lexai-profile:" ".env" >nul
if not errorlevel 1 goto setup_have_env
copy /Y ".env" ".env.before-phase1" >nul
>>"%LOG%" echo Old backend\.env without a LexAI profile saved as backend\.env.before-phase1
:setup_write_env
copy /Y "%ROOT%\deploy\env\laptop-8gb.env" ".env" >nul
>>"%LOG%" echo Wrote backend\.env from deploy\env\laptop-8gb.env
goto setup_done
:setup_have_env
>>"%LOG%" echo backend\.env already has a LexAI profile - left unchanged
:setup_done
>>"%LOG%" echo --- active backend\.env settings ---
findstr /B /C:"LEXAI_" /C:"# lexai-profile" ".env" >>"%LOG%" 2>&1
>>"%LOG%" echo.
set "CMD=.venv\Scripts\python.exe -c "import lexai, faiss, fastapi; print('lexai', lexai.__version__, 'imports OK')"" & call :run "%LOG%"
popd
type "%LOG%"
goto :eof
:setup_no_python
>>"%LOG%" echo ERROR: Python 3.12 or 3.13 was not found.
>>"%LOG%" echo Install Python 3.12 from https://www.python.org/downloads/windows/
>>"%LOG%" echo and tick "Add python.exe to PATH" during installation, then re-run: run_phase1.bat setup
type "%LOG%"
goto :eof


REM ---------------------------------------------------------------------------
:step_quality
set "LOG=%OUT%\03_quality.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
pushd "%BACKEND%"
set "CMD=.venv\Scripts\python.exe -m ruff check ." & call :run "%LOG%"
set "CMD=.venv\Scripts\python.exe -m ruff format --check ." & call :run "%LOG%"
set "CMD=.venv\Scripts\python.exe -m mypy" & call :run "%LOG%"
set "CMD=.venv\Scripts\python.exe -m pytest -v" & call :run "%LOG%"
popd
type "%LOG%"
goto :eof


REM ---------------------------------------------------------------------------
:step_models
set "LOG=%OUT%\04_models.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
REM Downloads show progress in this window; only results go to the log.
call :ensure_model qwen2.5:7b-instruct-q4_K_M
call :ensure_model nomic-embed-text
set "CMD=ollama create qwen2.5-7b-instruct -f "%ROOT%\deploy\ollama\Modelfile.laptop"" & call :run "%LOG%"
set "CMD=ollama list" & call :run "%LOG%"
pushd "%BACKEND%"
set "CMD=.venv\Scripts\python.exe scripts\check_models.py" & call :run "%LOG%"
popd
type "%LOG%"
goto :eof

:ensure_model
ollama list | findstr /I /C:"%~1" >nul
if not errorlevel 1 (
    >>"%LOG%" echo model %~1: already downloaded
    goto :eof
)
echo Downloading %~1 - this can take a while ...
ollama pull %~1
>>"%LOG%" echo model %~1: ollama pull exit=%ERRORLEVEL%
goto :eof


REM ---------------------------------------------------------------------------
:step_ingest
set "LOG=%OUT%\05_ingest.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
pushd "%BACKEND%"
set "CMD=.venv\Scripts\python.exe -m lexai.ingestion.cli" & call :run "%LOG%"
popd
type "%LOG%"
goto :eof


REM ---------------------------------------------------------------------------
:step_smoke
set "LOG=%OUT%\06_smoke.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
pushd "%BACKEND%"
set "CMD=.venv\Scripts\python.exe scripts\smoke_phase1.py --start-server --concurrency 2 --server-log "%OUT%\06_server.log"" & call :run "%LOG%"
popd
type "%LOG%"
goto :eof


REM ---------------------------------------------------------------------------
:step_outage
set "LOG=%OUT%\07_outage.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
echo This step stops Ollama, checks that LexAI reports the outage, then starts Ollama again.
>>"%LOG%" echo Stopping Ollama ...
taskkill /IM "ollama app.exe" /T /F >>"%LOG%" 2>&1
taskkill /IM ollama.exe /T /F >>"%LOG%" 2>&1
timeout /t 5 /nobreak >nul
pushd "%BACKEND%"
set "CMD=.venv\Scripts\python.exe scripts\smoke_phase1.py --start-server --expect-down --server-log "%OUT%\07_server.log"" & call :run "%LOG%"
popd
>>"%LOG%" echo Restarting Ollama ...
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" goto outage_restart
>>"%LOG%" echo Ollama app not found at the default location - start Ollama from the Start menu.
goto outage_done
:outage_restart
start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
>>"%LOG%" echo Ollama started again.
:outage_done
type "%LOG%"
goto :eof


REM ---------------------------------------------------------------------------
:no_venv
>>"%LOG%" echo ERROR: backend\.venv not found. Run "runlogs\phase1\run_phase1.bat setup" first.
type "%LOG%"
goto :eof
