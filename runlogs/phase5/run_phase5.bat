@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  LexAI Phase 5 verification - Windows Command Prompt (cmd.exe), no uv needed
REM
REM  From the repository folder:
REM     runlogs\phase5\run_phase5.bat                 all steps (about 20-25 minutes)
REM     runlogs\phase5\run_phase5.bat phase5          one step
REM
REM  Steps: check setup quality frontend models phase5 phase3 smoke
REM  Logs go to runlogs\phase5\output.
REM
REM  Uses the index built in Phase 2 (backend\var\qdrant). The phase5 step reindexes
REM  one small document through the admin API (the others are carried over).
REM  Close any LexAI API you started yourself first (embedded Qdrant allows one
REM  process). Keep the laptop plugged in and awake.
REM ===========================================================================

cd /d "%~dp0..\.."
set "ROOT=%CD%"
set "BACKEND=%ROOT%\backend"
set "FRONTEND=%ROOT%\frontend"
set "OUT=%ROOT%\runlogs\phase5\output"
set "VPY=%BACKEND%\.venv\Scripts\python.exe"
set "PRESET=%ROOT%\deploy\env\laptop-8gb.env"
set "PROFILE_LINE=# lexai-profile: laptop-8gb v4"
if not exist "%OUT%" mkdir "%OUT%"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "LEXAI_API_ENVIRONMENT=ci"

set "STEPS=%*"
if "%STEPS%"=="" set "STEPS=check setup quality frontend models phase5 phase3 smoke"

for %%S in (%STEPS%) do call :dispatch %%S

echo.
echo Done. Logs are in runlogs\phase5\output\
echo Next: commit and push runlogs\phase5\output
endlocal
goto :eof


REM ---------------------------------------------------------------------------
:dispatch
echo.
echo ########## %~1 ##########
if /I "%~1"=="check"    goto step_check
if /I "%~1"=="setup"    goto step_setup
if /I "%~1"=="quality"  goto step_quality
if /I "%~1"=="frontend" goto step_frontend
if /I "%~1"=="models"   goto step_models
if /I "%~1"=="phase5"   goto step_phase5
if /I "%~1"=="phase3"   goto step_phase3
if /I "%~1"=="smoke"    goto step_smoke
echo Unknown step "%~1". Use: check setup quality frontend models phase5 phase3 smoke
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
REM  call :tee LOGFILE  - like :run, but the output also shows in this window (long steps)
:tee
>>"%~1" echo $ %CMD%
%CMD% > "%OUT%\_last.txt" 2>&1
set "RC=%ERRORLEVEL%"
type "%OUT%\_last.txt"
type "%OUT%\_last.txt" >>"%~1"
del "%OUT%\_last.txt" >nul 2>&1
>>"%~1" echo exit=%RC%
>>"%~1" echo.
goto :eof


REM ---------------------------------------------------------------------------
REM  Starts Ollama in its own window if it is not answering. An "ollama" command run
REM  while the server is down starts the server inside this script's console: it then
REM  holds the log file open and stops when this window closes.
:ensure_ollama
curl -s -m 3 http://localhost:11434/api/version >nul 2>&1
if not errorlevel 1 goto :eof
echo Ollama is not running - starting it in its own window ...
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" goto ensure_app
start "Ollama" /MIN ollama serve
goto ensure_wait_init
:ensure_app
start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
:ensure_wait_init
set /a "TRIES=0"
:ensure_wait
timeout /t 2 /nobreak >nul
curl -s -m 3 http://localhost:11434/api/version >nul 2>&1
if not errorlevel 1 goto :eof
set /a "TRIES+=1"
if %TRIES% LSS 30 goto ensure_wait
echo WARNING: Ollama did not answer within a minute.
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
set "CMD=git -C "%ROOT%" status --short" & call :run "%LOG%"
call :find_python
>>"%LOG%" echo Python chosen for LexAI: [%PY%]
>>"%LOG%" echo.
set "CMD=ollama --version" & call :run "%LOG%"
set "CMD=node --version" & call :run "%LOG%"
set "CMD=call npm --version" & call :run "%LOG%"
set "CMD=curl -s http://localhost:11434/api/version" & call :run "%LOG%"
set "CMD=nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv" & call :run "%LOG%"
>>"%LOG%" echo $ systeminfo (memory lines)
systeminfo | findstr /C:"Total Physical Memory" /C:"Available Physical Memory" >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo $ anything listening on port 8000 (a running LexAI API locks the index)
netstat -ano | findstr /C:":8000 " | findstr /C:"LISTENING" >>"%LOG%" 2>&1
if errorlevel 1 >>"%LOG%" echo nothing on port 8000
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
REM Removed packages: FAISS and rank-bm25 (Phase 2), slowapi (Phase 5, replaced by `limits`).
set "CMD=.venv\Scripts\python.exe -m pip uninstall -y faiss-cpu rank-bm25 slowapi" & call :run "%LOG%"
set "CMD=.venv\Scripts\python.exe -m pip install --no-deps -e . --progress-bar off" & call :run "%LOG%"
REM backend\.env must carry the current profile line; otherwise keep a copy and replace it.
findstr /X /C:"%PROFILE_LINE%" "%PRESET%" >nul
if errorlevel 1 >>"%LOG%" echo WARNING: %PRESET% does not contain "%PROFILE_LINE%"
if not exist ".env" goto setup_write_env
findstr /X /C:"%PROFILE_LINE%" ".env" >nul
if not errorlevel 1 goto setup_have_env
copy /Y ".env" ".env.previous" >nul
>>"%LOG%" echo Old backend\.env (older profile) saved as backend\.env.previous
:setup_write_env
copy /Y "%PRESET%" ".env" >nul
>>"%LOG%" echo Wrote backend\.env from deploy\env\laptop-8gb.env
goto setup_done
:setup_have_env
>>"%LOG%" echo backend\.env already has the current profile - left unchanged
:setup_done
>>"%LOG%" echo --- active backend\.env settings ---
findstr /B /C:"LEXAI_" /C:"# lexai-profile" ".env" >>"%LOG%" 2>&1
>>"%LOG%" echo.
set "CMD=.venv\Scripts\python.exe -c "import lexai, qdrant_client, yaml, fastapi; from importlib.metadata import version; print('lexai', lexai.__version__, 'qdrant-client', version('qdrant-client'), 'imports OK')"" & call :run "%LOG%"
popd
type "%LOG%"
goto :eof
:setup_no_python
>>"%LOG%" echo ERROR: Python 3.12 or 3.13 was not found.
>>"%LOG%" echo Install Python 3.12 from https://www.python.org/downloads/windows/
>>"%LOG%" echo and tick "Add python.exe to PATH" during installation, then re-run: run_phase5.bat setup
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
call :ensure_ollama
set "LOG=%OUT%\05_models.log"
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
REM  TypeScript check, lint, unit tests and production build of the new frontend.
REM  (The browser end-to-end tests run in CI.) Skipped if Node.js is not installed.
:step_frontend
set "LOG=%OUT%\04_frontend.log"
type nul > "%LOG%"
where npm >nul 2>&1
if errorlevel 1 goto frontend_no_node
echo Installing frontend packages and building - a few minutes the first time ...
pushd "%FRONTEND%"
set "CMD=call npm ci --no-audit --no-fund" & call :run "%LOG%"
set "CMD=call npm run typecheck" & call :run "%LOG%"
set "CMD=call npm run lint" & call :run "%LOG%"
set "CMD=call npm test" & call :run "%LOG%"
set "CMD=call npm run build" & call :run "%LOG%"
popd
type "%LOG%"
goto :eof
:frontend_no_node
>>"%LOG%" echo SKIPPED: Node.js is not installed. Install the LTS version from https://nodejs.org/
>>"%LOG%" echo to run the new UI locally; CI checks it on every pull request.
type "%LOG%"
goto :eof


REM ---------------------------------------------------------------------------
REM  Sessions, rate limit, security controls, every citation opens its source,
REM  feedback, metrics, a full trace, and an admin reindex (about 6-8 minutes).
:step_phase5
call :ensure_ollama
set "LOG=%OUT%\06_phase5.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
echo Running the Phase 5 checks - about 6-8 minutes, three of them generate full answers ...
pushd "%BACKEND%"
set "CMD=.venv\Scripts\python.exe scripts\check_phase5.py --server-log "%OUT%\06_server.log"" & call :tee "%LOG%"
popd
goto :eof


REM ---------------------------------------------------------------------------
REM  Phase 3 checks again, now through the v1 API (regression).
:step_phase3
call :ensure_ollama
set "LOG=%OUT%\07_phase3.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
echo Running the Phase 3 checks through the v1 API - about 5 minutes ...
pushd "%BACKEND%"
set "CMD=.venv\Scripts\python.exe scripts\check_phase3.py --server-log "%OUT%\07_server.log"" & call :tee "%LOG%"
popd
goto :eof


REM ---------------------------------------------------------------------------
:step_smoke
call :ensure_ollama
set "LOG=%OUT%\08_smoke.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
echo Starting the API and asking questions ...
pushd "%BACKEND%"
set "CMD=.venv\Scripts\python.exe scripts\smoke_phase1.py --start-server --concurrency 2 --server-log "%OUT%\08_server.log"" & call :run "%LOG%"
popd
type "%LOG%"
goto :eof


REM ---------------------------------------------------------------------------
:no_venv
>>"%LOG%" echo ERROR: backend\.venv not found. Run "runlogs\phase5\run_phase5.bat setup" first.
type "%LOG%"
goto :eof
