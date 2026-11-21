@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  LexAI in Docker on a Windows laptop (cmd.exe). Ollama keeps running on Windows;
REM  Qdrant, Redis, the API and the web UI run in containers (compose.laptop.yaml).
REM
REM     deploy\lexai.bat build                  build the images
REM     deploy\lexai.bat up                     start  (web UI: http://127.0.0.1:8090)
REM     deploy\lexai.bat network                can the containers reach Ollama on Windows?
REM     deploy\lexai.bat models                 are the models served and working?
REM     deploy\lexai.bat ingest [--only ID]     download sources and build the index
REM     deploy\lexai.bat status                 the live index
REM     deploy\lexai.bat observability          also start Prometheus (:9095) and Grafana (:3001)
REM     deploy\lexai.bat smoke                  readiness + five questions through nginx
REM     deploy\lexai.bat deploy [TAG] [opts]    switch to TAG (default local), smoke test, rollback
REM     deploy\lexai.bat rollback               back to the previous release
REM     deploy\lexai.bat backup                 snapshot the index into deploy\backups
REM     deploy\lexai.bat restore [NAME]         restore a backup (default: latest)
REM     deploy\lexai.bat eval ARGS              python -m lexai.evaluation ARGS
REM     deploy\lexai.bat ps / logs / down / wipe
REM ===========================================================================

set "D=%~dp0"
REM deploy.py needs Python 3.11+: the backend virtual environment if there is one.
if not defined LEXAI_PYTHON set "LEXAI_PYTHON=python"
if exist "%D%..\backend\.venv\Scripts\python.exe" set "LEXAI_PYTHON=%D%..\backend\.venv\Scripts\python.exe"
REM var\host.env (written by "network" when needed) says how the containers reach Windows.
set "HOSTENV=%D%var\host.env"
set COMPOSE=docker compose --project-directory "%D%." --env-file "%D%env\laptop-docker.env"
if exist "%HOSTENV%" set COMPOSE=%COMPOSE% --env-file "%HOSTENV%"
set COMPOSE=%COMPOSE% -f "%D%compose.yaml" -f "%D%compose.laptop.yaml"

set "CMD=%~1"
if "%CMD%"=="" goto help
shift
set "ARGS="
:collect
if "%~1"=="" goto dispatch
set ARGS=%ARGS% %1
shift
goto collect

:dispatch
if /I "%CMD%"=="build"         goto c_build
if /I "%CMD%"=="up"            goto c_up
if /I "%CMD%"=="network"       goto c_network
if /I "%CMD%"=="models"        goto c_models
if /I "%CMD%"=="ingest"        goto c_ingest
if /I "%CMD%"=="status"        goto c_status
if /I "%CMD%"=="observability" goto c_obs
if /I "%CMD%"=="smoke"         goto c_smoke
if /I "%CMD%"=="deploy"        goto c_deploy
if /I "%CMD%"=="rollback"      goto c_rollback
if /I "%CMD%"=="backup"        goto c_backup
if /I "%CMD%"=="restore"       goto c_restore
if /I "%CMD%"=="eval"          goto c_eval
if /I "%CMD%"=="ps"            goto c_ps
if /I "%CMD%"=="logs"          goto c_logs
if /I "%CMD%"=="down"          goto c_down
if /I "%CMD%"=="wipe"          goto c_wipe
:help
echo Usage: deploy\lexai.bat build^|up^|network^|models^|ingest^|status^|observability^|smoke^|deploy^|rollback^|backup^|restore^|eval^|ps^|logs^|down^|wipe
exit /b 2

:c_build
%COMPOSE% build api web
exit /b %ERRORLEVEL%

:c_up
%COMPOSE% up -d api web
exit /b %ERRORLEVEL%

:c_network
set "PROBE=%D%var\probe.txt"
%COMPOSE% run --rm --no-deps --entrypoint python ingest scripts/probe_host.py > "%PROBE%" 2>&1
set "RC=%ERRORLEVEL%"
type "%PROBE%"
if not "%RC%"=="3" exit /b %RC%
if defined LEXAI_NETWORK_RETRY exit /b 1
REM Exit 3: only another address of Windows answers. Save it and recreate the containers.
set "GW="
for /f "tokens=2 delims==" %%G in ('findstr /B /C:"LEXAI_HOST_GATEWAY=" "%PROBE%"') do set "GW=%%G"
if not defined GW exit /b 1
>"%HOSTENV%" echo LEXAI_HOST_GATEWAY=%GW%
echo Saved LEXAI_HOST_GATEWAY=%GW% to deploy\var\host.env; recreating api and web with it ...
set "LEXAI_NETWORK_RETRY=1"
call "%~f0" up
call "%~f0" network
exit /b %ERRORLEVEL%

:c_models
%COMPOSE% run --rm --no-deps --entrypoint python ingest scripts/check_models.py
exit /b %ERRORLEVEL%

:c_ingest
%COMPOSE% run --rm ingest fetch%ARGS%
if errorlevel 1 exit /b %ERRORLEVEL%
%COMPOSE% run --rm ingest build%ARGS%
exit /b %ERRORLEVEL%

:c_status
%COMPOSE% run --rm ingest status
exit /b %ERRORLEVEL%

:c_obs
%COMPOSE% --profile observability up -d prometheus grafana
exit /b %ERRORLEVEL%

:c_smoke
%COMPOSE% run --rm --no-deps smoke --base-url http://web:8080%ARGS%
exit /b %ERRORLEVEL%

:c_deploy
if not defined ARGS set "ARGS= local"
"%LEXAI_PYTHON%" "%D%deploy.py" --laptop --no-pull --tag%ARGS%
exit /b %ERRORLEVEL%

:c_rollback
"%LEXAI_PYTHON%" "%D%deploy.py" --laptop --rollback --no-pull%ARGS%
exit /b %ERRORLEVEL%

:c_backup
%COMPOSE% run --rm ingest backup
exit /b %ERRORLEVEL%

:c_restore
%COMPOSE% run --rm ingest restore%ARGS%
exit /b %ERRORLEVEL%

:c_eval
%COMPOSE% run --rm eval%ARGS%
exit /b %ERRORLEVEL%

:c_ps
%COMPOSE% --profile observability ps -a
exit /b %ERRORLEVEL%

:c_logs
%COMPOSE% --profile observability logs --tail 200%ARGS%
exit /b %ERRORLEVEL%

:c_down
%COMPOSE% --profile observability --profile job down
exit /b %ERRORLEVEL%

:c_wipe
echo Removes the containers AND the index, sessions, feedback and metrics volumes.
set /p "OK=Type yes to continue: "
if /I not "%OK%"=="yes" exit /b 1
%COMPOSE% --profile observability --profile job down -v
exit /b %ERRORLEVEL%
