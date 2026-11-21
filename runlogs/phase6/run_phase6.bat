@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  LexAI Phase 6 verification - Windows Command Prompt (cmd.exe), Docker Desktop
REM
REM  From the repository folder:
REM     runlogs\phase6\run_phase6.bat                all steps (about 60-75 minutes)
REM     runlogs\phase6\run_phase6.bat deploy         one step
REM
REM  Steps: check build up network models ingest deploy browser observability backup rehearsal collect
REM  Logs go to runlogs\phase6\output.
REM
REM  Ollama keeps running on Windows (models from Phase 2); Qdrant, Redis, the API and
REM  the web UI run in Docker. Keep the laptop plugged in and awake. The stack stays up
REM  at the end: http://127.0.0.1:8090  (stop it with: deploy\lexai.bat down)
REM ===========================================================================

cd /d "%~dp0..\.."
set "ROOT=%CD%"
set "OUT=%ROOT%\runlogs\phase6\output"
set "LEXAI=call "%ROOT%\deploy\lexai.bat""
set "VPY=%ROOT%\backend\.venv\Scripts\python.exe"
set COMPOSE=docker compose --project-directory "%ROOT%\deploy" --env-file "%ROOT%\deploy\env\laptop-docker.env" -f "%ROOT%\deploy\compose.yaml" -f "%ROOT%\deploy\compose.laptop.yaml"
if not exist "%OUT%" mkdir "%OUT%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY=python"
if exist "%VPY%" set "PY=%VPY%"

set "STEPS=%*"
if "%STEPS%"=="" set "STEPS=check build up network models ingest deploy browser observability backup rehearsal collect"

for %%S in (%STEPS%) do call :dispatch %%S

echo.
echo Done. Logs are in runlogs\phase6\output\
echo The stack is still running: http://127.0.0.1:8090   (deploy\lexai.bat down to stop it)
echo Next: commit and push runlogs\phase6\output
endlocal
goto :eof


REM ---------------------------------------------------------------------------
:dispatch
echo.
echo ########## %~1 ##########
if /I "%~1"=="check"         goto step_check
if /I "%~1"=="build"         goto step_build
if /I "%~1"=="up"            goto step_up
if /I "%~1"=="network"       goto step_network
if /I "%~1"=="models"        goto step_models
if /I "%~1"=="ingest"        goto step_ingest
if /I "%~1"=="deploy"        goto step_deploy
if /I "%~1"=="browser"       goto step_browser
if /I "%~1"=="observability" goto step_observability
if /I "%~1"=="backup"        goto step_backup
if /I "%~1"=="rehearsal"     goto step_rehearsal
if /I "%~1"=="collect"       goto step_collect
echo Unknown step "%~1". Use: check build up network models ingest deploy browser observability backup rehearsal collect
goto :eof

REM  Steps that need the model server are skipped once `network` has failed in this run
REM  (otherwise one cause shows up as a dozen failures).
:need_models
if not "%MODELS_OK%"=="0" goto :eof
echo SKIPPED: the containers cannot reach Ollama (see 04_network.log). Fix that first:
echo     deploy\ollama-for-docker.bat   then   runlogs\phase6\run_phase6.bat network models ingest deploy browser backup rehearsal collect
set "SKIP=1"
goto :eof


REM ---------------------------------------------------------------------------
REM  call :run LOGFILE  - runs CMD, appends the command, its output and exit code
:run
>>"%~1" echo $ %CMD%
%CMD% >>"%~1" 2>&1
set "RC=%ERRORLEVEL%"
>>"%~1" echo exit=%RC%
>>"%~1" echo.
goto :eof

REM  call :tee LOGFILE  - like :run, and shows the output in this window (long steps)
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

REM  Starts Ollama in its own window if it is not answering.
:ensure_ollama
curl -s -m 3 http://localhost:11434/api/version >nul 2>&1
if not errorlevel 1 goto :eof
echo Ollama is not running - starting it in its own window ...
if exist "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe" (
    start "" "%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"
) else (
    start "Ollama" /MIN ollama serve
)
set /a "TRIES=0"
:ensure_wait
timeout /t 2 /nobreak >nul
curl -s -m 3 http://localhost:11434/api/version >nul 2>&1
if not errorlevel 1 goto :eof
set /a "TRIES+=1"
if %TRIES% LSS 30 goto ensure_wait
echo WARNING: Ollama did not answer within a minute.
goto :eof

REM  Memory and CPU of every container right now
:stats
>>"%~1" echo $ docker stats --no-stream
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}" >>"%~1" 2>&1
>>"%~1" echo.
goto :eof


:step_check
set "LOG=%OUT%\01_check.log"
type nul > "%LOG%"
set "CMD=ver" & call :run "%LOG%"
set "CMD=git -C "%ROOT%" rev-parse --abbrev-ref HEAD" & call :run "%LOG%"
set "CMD=git -C "%ROOT%" rev-parse HEAD" & call :run "%LOG%"
set "CMD=git -C "%ROOT%" status --short" & call :run "%LOG%"
set "CMD=docker version --format "client {{.Client.Version}} server {{.Server.Version}}"" & call :run "%LOG%"
set "CMD=docker compose version" & call :run "%LOG%"
set "CMD=docker info --format "cpus={{.NCPU}} memory={{.MemTotal}} os={{.OperatingSystem}}"" & call :run "%LOG%"
set "CMD=%PY% --version" & call :run "%LOG%"
call :ensure_ollama
set "CMD=ollama --version" & call :run "%LOG%"
set "CMD=ollama list" & call :run "%LOG%"
>>"%LOG%" echo $ systeminfo (memory lines)
systeminfo | findstr /C:"Total Physical Memory" /C:"Available Physical Memory" >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo $ type %%UserProfile%%\.wslconfig
if exist "%UserProfile%\.wslconfig" (type "%UserProfile%\.wslconfig" >>"%LOG%") else (>>"%LOG%" echo no .wslconfig - Docker may use up to half the RAM; see runlogs\phase6\README.md)
>>"%LOG%" echo.
>>"%LOG%" echo $ anything listening on port 8080
netstat -ano | findstr /C:":8080 " | findstr /C:"LISTENING" >>"%LOG%" 2>&1
if errorlevel 1 >>"%LOG%" echo nothing on port 8080
>>"%LOG%" echo.
type "%LOG%"
goto :eof


:step_build
set "LOG=%OUT%\02_build.log"
type nul > "%LOG%"
echo Building the API and web images (first time: 5-15 minutes) ...
set "CMD=%LEXAI% build" & call :tee "%LOG%"
set "CMD=docker image ls --format "{{.Repository}}:{{.Tag}} {{.Size}}" --filter reference=lexai-*" & call :run "%LOG%"
set "CMD=docker run --rm --entrypoint id lexai-backend:local" & call :run "%LOG%"
set "CMD=docker run --rm --entrypoint id lexai-web:local" & call :run "%LOG%"
type "%LOG%" | findstr /V /C:"#"
goto :eof


:step_up
call :ensure_ollama
set "LOG=%OUT%\03_up.log"
type nul > "%LOG%"
set "CMD=%LEXAI% up" & call :tee "%LOG%"
timeout /t 20 /nobreak >nul
set "CMD=%LEXAI% ps" & call :run "%LOG%"
set "CMD=curl -s -i http://127.0.0.1:8090/healthz" & call :run "%LOG%"
set "CMD=curl -s -o NUL -w "metrics through nginx: %%{http_code}" http://127.0.0.1:8090/metrics" & call :run "%LOG%"
set "CMD=curl -s -I http://127.0.0.1:8090/" & call :run "%LOG%"
call :stats "%LOG%"
type "%LOG%"
goto :eof


:step_network
call :ensure_ollama
set "LOG=%OUT%\04_network.log"
type nul > "%LOG%"
>>"%LOG%" echo $ netstat (Ollama on Windows)
netstat -ano | findstr /C:":11434 " | findstr /C:"LISTENING" >>"%LOG%" 2>&1
>>"%LOG%" echo.
echo Checking from inside a container that Ollama on Windows answers ...
set "CMD=%LEXAI% network" & call :run "%LOG%"
set "MODELS_OK=1"
if not "%RC%"=="0" set "MODELS_OK=0"
type "%LOG%"
if "%MODELS_OK%"=="0" (
    echo.
    echo The containers cannot reach Ollama. Run  deploy\ollama-for-docker.bat  (as Administrator
    echo for the firewall part^), then:  runlogs\phase6\run_phase6.bat network models ingest deploy browser backup rehearsal collect
)
goto :eof


:step_models
call :ensure_ollama
set "SKIP=" & call :need_models
if defined SKIP goto :eof
set "LOG=%OUT%\04_models.log"
type nul > "%LOG%"
echo Checking that the containers reach Ollama on Windows (a short generation test) ...
set "CMD=%LEXAI% models" & call :run "%LOG%"
type "%LOG%"
goto :eof


:step_ingest
set "SKIP=" & call :need_models
if defined SKIP goto :eof
call :ensure_ollama
set "LOG=%OUT%\05_ingest.log"
type nul > "%LOG%"
echo Building the index in the Qdrant container (about 10 minutes) ...
set "CMD=%LEXAI% ingest" & call :tee "%LOG%"
set "CMD=%LEXAI% status" & call :run "%LOG%"
call :stats "%LOG%"
type "%LOG%" | findstr /C:"BUILD STATUS" /C:"[FAIL]" /C:"live index" /C:"documents:" /C:"exit=" /C:"ERROR"
goto :eof


:step_deploy
set "SKIP=" & call :need_models
if defined SKIP goto :eof
call :ensure_ollama
set "LOG=%OUT%\06_deploy.log"
type nul > "%LOG%"
echo First release through deploy.py: readiness, UI, five real answers (4 must cite) ...
set "CMD=%LEXAI% deploy local --min-cited 4 --no-backup --ready-timeout 300" & call :tee "%LOG%"
set "CMD=type "%ROOT%\deploy\var\release.json"" & call :run "%LOG%"
call :stats "%LOG%"
goto :eof


:step_browser
set "SKIP=" & call :need_models
if defined SKIP goto :eof
set "LOG=%OUT%\07_browser.log"
type nul > "%LOG%"
where npm >nul 2>&1
if errorlevel 1 (
    >>"%LOG%" echo npm not found - install Node.js 22 LTS, then re-run: run_phase6.bat browser
    type "%LOG%"
    goto :eof
)
pushd "%ROOT%\frontend"
if not exist node_modules (set "CMD=call npm ci --no-audit --no-fund" & call :run "%LOG%")
REM Microsoft Edge (installed with Windows) runs the tests against nginx: no browser download.
set "PW_BASE_URL=http://127.0.0.1:8090"
set "PW_LIVE=1"
set "PW_CHANNEL=msedge"
echo Browser tests against nginx (production CSP) and one real question ...
set "CMD=call npx playwright test --reporter=list" & call :tee "%LOG%"
set "PW_BASE_URL=" & set "PW_LIVE=" & set "PW_CHANNEL="
popd
goto :eof


:step_observability
set "LOG=%OUT%\08_observability.log"
type nul > "%LOG%"
set "CMD=%LEXAI% observability" & call :tee "%LOG%"
echo Waiting for Prometheus to scrape and Grafana to provision ...
set "CMD=%PY% "%ROOT%\deploy\ci\check_stack.py" prometheus --replicas 1 --timeout 240" & call :run "%LOG%"
set "CMD=%PY% "%ROOT%\deploy\ci\check_stack.py" grafana --password lexai-laptop --timeout 240" & call :run "%LOG%"
set "CMD=curl -s "http://127.0.0.1:9095/api/v1/query?query=sum%%20by%%20(route,status)(lexai_http_requests_total)"" & call :run "%LOG%"
set "CMD=curl -s "http://127.0.0.1:9095/api/v1/query?query=lexai_process_memory_bytes"" & call :run "%LOG%"
call :stats "%LOG%"
type "%LOG%"
echo Grafana: http://127.0.0.1:3001  (admin / lexai-laptop)   Prometheus: http://127.0.0.1:9095
goto :eof


:step_backup
set "SKIP=" & call :need_models
if defined SKIP goto :eof
set "LOG=%OUT%\09_backup.log"
type nul > "%LOG%"
echo Backup, then delete the index volume and restore it ...
set "CMD=%LEXAI% backup" & call :tee "%LOG%"
set "CMD=dir /s /b "%ROOT%\deploy\backups\lexai-*"" & call :run "%LOG%"
set "CMD=%COMPOSE% rm -sf qdrant" & call :run "%LOG%"
set "CMD=docker volume rm lexai_qdrant-data" & call :run "%LOG%"
set "CMD=%COMPOSE% up -d --wait qdrant" & call :run "%LOG%"
>>"%LOG%" echo --- the index is gone now (status must fail): ---
set "CMD=%LEXAI% status" & call :run "%LOG%"
set "CMD=%LEXAI% restore" & call :tee "%LOG%"
set "CMD=%LEXAI% status" & call :run "%LOG%"
echo Two questions against the restored index ...
set "CMD=%LEXAI% smoke --questions 2 --wait-ready 180" & call :tee "%LOG%"
goto :eof


:step_rehearsal
set "SKIP=" & call :need_models
if defined SKIP goto :eof
set "LOG=%OUT%\10_rehearsal.log"
type nul > "%LOG%"
echo Deploy rehearsal: a planted bad release must be rolled back automatically ...
set "CMD=docker build -f "%ROOT%\deploy\ci\bad-release.Dockerfile" -t lexai-backend:bad "%ROOT%\deploy\ci"" & call :run "%LOG%"
set "CMD=docker tag lexai-web:local lexai-web:bad" & call :run "%LOG%"
set "CMD=%LEXAI% deploy bad --ready-timeout 90" & call :tee "%LOG%"
>>"%LOG%" echo (exit=1 above is the expected result: the bad release was refused)
set "CMD=%COMPOSE% ps api --format "{{.Image}} {{.Status}}"" & call :run "%LOG%"
set "CMD=type "%ROOT%\deploy\var\release.json"" & call :run "%LOG%"
set "CMD=dir /b "%ROOT%\deploy\var\deploy-failure-*.log"" & call :run "%LOG%"
type "%LOG%" | findstr /C:"[deploy" /C:"ROLL" /C:"exit=" /C:"lexai-backend"
goto :eof


:step_collect
set "LOG=%OUT%\11_collect.log"
type nul > "%LOG%"
set "CMD=%COMPOSE% --profile observability ps -a" & call :run "%LOG%"
call :stats "%LOG%"
set "CMD=%COMPOSE% logs --no-color --tail 80 api web qdrant redis" & call :run "%LOG%"
set "CMD=docker system df" & call :run "%LOG%"
set "CMD=git -C "%ROOT%" rev-parse HEAD" & call :run "%LOG%"
copy /Y "%ROOT%\deploy\var\release.json" "%OUT%\release.json" >nul 2>&1
for %%F in ("%ROOT%\deploy\var\deploy-failure-*.log") do copy /Y "%%F" "%OUT%\" >nul 2>&1
echo Collected container status, memory and logs.
goto :eof
