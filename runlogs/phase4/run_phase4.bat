@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  LexAI Phase 4 evaluation - Windows Command Prompt (cmd.exe), no uv needed
REM
REM  From the repository folder:
REM     runlogs\phase4\run_phase4.bat                 all steps (about 45-60 minutes)
REM     runlogs\phase4\run_phase4.bat retrieval       one step
REM
REM  Steps: check setup quality models validate retrieval test answers fixture load report
REM  Extra: speed (machine and model speed diagnostics, appended to 00_speed.log)
REM         retrieval-quick (the retrieval step without HyDE, about 8 minutes)
REM  Logs go to runlogs\phase4\output; results to eval\reports\<commit>, the CI
REM  fixture to eval\fixtures and the gate baseline to eval\baselines.
REM
REM  Uses the index built in Phase 2 (backend\var\qdrant); no rebuild is needed.
REM  Close any LexAI API you started yourself first (embedded Qdrant allows one
REM  process). Keep the laptop plugged in and awake.
REM ===========================================================================

cd /d "%~dp0..\.."
set "ROOT=%CD%"
set "BACKEND=%ROOT%\backend"
set "OUT=%ROOT%\runlogs\phase4\output"
set "VPY=%BACKEND%\.venv\Scripts\python.exe"
set "PRESET=%ROOT%\deploy\env\laptop-8gb.env"
set "PROFILE_LINE=# lexai-profile: laptop-8gb v3"
set "EVAL=.venv\Scripts\python.exe -m lexai.evaluation"
if not exist "%OUT%" mkdir "%OUT%"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "LEXAI_API_ENVIRONMENT=ci"

set "STEPS=%*"
if "%STEPS%"=="" set "STEPS=check setup quality models validate retrieval test answers fixture load report"

for %%S in (%STEPS%) do call :dispatch %%S

echo.
echo Done. Logs are in runlogs\phase4\output\
echo Next: commit and push runlogs\phase4\output, eval\reports, eval\fixtures and eval\baselines
endlocal
goto :eof


REM ---------------------------------------------------------------------------
:dispatch
echo.
echo ########## %~1 ##########
if /I "%~1"=="check"     goto step_check
if /I "%~1"=="setup"     goto step_setup
if /I "%~1"=="quality"   goto step_quality
if /I "%~1"=="models"    goto step_models
if /I "%~1"=="validate"  goto step_validate
if /I "%~1"=="retrieval" goto step_retrieval
if /I "%~1"=="retrieval-quick" goto step_retrieval_quick
if /I "%~1"=="speed"     goto step_speed
if /I "%~1"=="test"      goto step_test
if /I "%~1"=="answers"   goto step_answers
if /I "%~1"=="fixture"   goto step_fixture
if /I "%~1"=="load"      goto step_load
if /I "%~1"=="report"    goto step_report
echo Unknown step "%~1". Use: check setup quality models validate retrieval retrieval-quick test answers fixture load speed report
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
REM Phase 2 replaced FAISS and rank-bm25 with Qdrant; remove the old packages if present.
set "CMD=.venv\Scripts\python.exe -m pip uninstall -y faiss-cpu rank-bm25" & call :run "%LOG%"
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
>>"%LOG%" echo and tick "Add python.exe to PATH" during installation, then re-run: run_phase4.bat setup
type "%LOG%"
goto :eof


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
REM  Every golden item is checked against the indexed text (gold provisions exist,
REM  key phrases appear in them). Failures are listed and excluded from scoring.
:step_validate
set "LOG=%OUT%\05_validate.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
pushd "%BACKEND%"
set "CMD=%EVAL% validate" & call :tee "%LOG%"
popd
goto :eof


REM ---------------------------------------------------------------------------
REM  Retrieval on the dev split: the default pipeline and four ablations.
REM  hyde makes one model call per question (about 15 minutes of the total).
:step_retrieval
set "LOG=%OUT%\06_retrieval_dev.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
echo Retrieval on the dev split, 5 configurations - about 20 minutes ...
pushd "%BACKEND%"
set "CMD=%EVAL% retrieval --split dev --configs hybrid,dense,bm25,no-routing,hyde" & call :tee "%LOG%"
popd
goto :eof


REM ---------------------------------------------------------------------------
:step_retrieval_quick
set "LOG=%OUT%\06_retrieval_dev.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
echo Retrieval on the dev split, 4 configurations - about 8 minutes ...
pushd "%BACKEND%"
set "CMD=%EVAL% retrieval --split dev --configs hybrid,dense,bm25,no-routing" & call :tee "%LOG%"
popd
goto :eof


REM ---------------------------------------------------------------------------
REM  Power, memory, GPU clocks and the model's generation speed. Appends, so
REM  running it before and after the long steps shows whether the laptop slowed down.
:step_speed
set "LOG=%OUT%\00_speed.log"
>>"%LOG%" echo ===================== %DATE% %TIME% =====================
set "CMD=powercfg /getactivescheme" & call :run "%LOG%"
set "CMD=powershell -NoProfile -Command "$b = Get-CimInstance Win32_Battery; 'BatteryStatus=' + $b.BatteryStatus + ' charge=' + $b.EstimatedChargeRemaining"" & call :run "%LOG%"
>>"%LOG%" echo (BatteryStatus 2 = plugged in, 1 = on battery)
set "CMD=powershell -NoProfile -Command "$o = Get-CimInstance Win32_OperatingSystem; 'free RAM MB=' + [int]($o.FreePhysicalMemory/1024) + ' free virtual MB=' + [int]($o.FreeVirtualMemory/1024)"" & call :run "%LOG%"
set "CMD=powershell -NoProfile -Command "$c = Get-CimInstance Win32_Processor; 'CPU MHz=' + $c.CurrentClockSpeed + ' of ' + $c.MaxClockSpeed + ' load=' + $c.LoadPercentage"" & call :run "%LOG%"
set "CMD=nvidia-smi --query-gpu=temperature.gpu,clocks.sm,clocks.max.sm,power.draw,utilization.gpu,memory.used,clocks_throttle_reasons.active --format=csv" & call :run "%LOG%"
set "CMD=ollama ps" & call :run "%LOG%"
if not exist "%VPY%" goto speed_done
pushd "%BACKEND%"
set "CMD=.venv\Scripts\python.exe scripts\check_models.py" & call :run "%LOG%"
popd
:speed_done
set "CMD=ollama ps" & call :run "%LOG%"
type "%LOG%"
goto :eof


REM ---------------------------------------------------------------------------
REM  The held-out test split, default pipeline only. Run once per release.
:step_test
set "LOG=%OUT%\07_retrieval_test.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
pushd "%BACKEND%"
set "CMD=%EVAL% retrieval --split test --configs hybrid" & call :tee "%LOG%"
popd
goto :eof


REM ---------------------------------------------------------------------------
REM  Full answers for 40 dev questions spread over all categories (about 20 minutes).
:step_answers
set "LOG=%OUT%\08_answers.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
echo Generating 40 full answers - about 20 minutes ...
pushd "%BACKEND%"
set "CMD=%EVAL% answers --split dev --limit 40" & call :tee "%LOG%"
popd
goto :eof


REM ---------------------------------------------------------------------------
REM  Exports the index and the smoke queries for the offline CI gate, writes the
REM  baseline, then checks the gate passes and that a planted regression fails.
:step_fixture
set "LOG=%OUT%\09_fixture.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
pushd "%BACKEND%"
set "CMD=%EVAL% export-fixture" & call :tee "%LOG%"
set "CMD=%EVAL% offline --write-baseline" & call :tee "%LOG%"
set "CMD=%EVAL% offline" & call :tee "%LOG%"
>>"%LOG%" echo Planted regression (routing off) - this one is EXPECTED to fail the gate:
set "CMD=%EVAL% offline --config no-routing" & call :tee "%LOG%"
popd
>>"%LOG%" echo --- fixture files ---
dir "%ROOT%\eval\fixtures" >>"%LOG%" 2>&1
goto :eof


REM ---------------------------------------------------------------------------
:step_load
set "LOG=%OUT%\10_load.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
echo Load test - starts the API, about 5 minutes ...
for /f %%C in ('git -C "%ROOT%" rev-parse --short HEAD') do set "COMMIT=%%C"
pushd "%BACKEND%"
set "CMD=.venv\Scripts\python.exe scripts\load_test.py --answers 4 --json "%ROOT%\eval\reports\%COMMIT%\load.json" --server-log "%OUT%\10_server.log"" & call :tee "%LOG%"
popd
goto :eof


REM ---------------------------------------------------------------------------
:step_report
set "LOG=%OUT%\11_report.log"
type nul > "%LOG%"
if not exist "%VPY%" goto no_venv
pushd "%BACKEND%"
set "CMD=%EVAL% report" & call :run "%LOG%"
popd
for /f %%C in ('git -C "%ROOT%" rev-parse --short HEAD') do set "COMMIT=%%C"
type "%ROOT%\eval\reports\%COMMIT%\report.md" >>"%LOG%" 2>&1
type "%LOG%"
goto :eof


REM ---------------------------------------------------------------------------
:no_venv
>>"%LOG%" echo ERROR: backend\.venv not found. Run "runlogs\phase4\run_phase4.bat setup" first.
type "%LOG%"
goto :eof
