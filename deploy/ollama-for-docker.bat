@echo off
setlocal EnableExtensions
REM ===========================================================================
REM  Lets the LexAI containers reach Ollama running on Windows.
REM
REM  Rancher Desktop (and Docker on WSL2) point host.docker.internal at the Windows side
REM  of the WSL network (the "vEthernet (WSL)" adapter), not at 127.0.0.1. Ollama listens on
REM  127.0.0.1 by default, so the containers cannot reach it. This script:
REM    1. sets OLLAMA_HOST=0.0.0.0:11434 for your user and restarts Ollama with it;
REM    2. allows TCP 11434 from the WSL network (172.16.0.0/12) through Windows Firewall -
REM       this part needs an Administrator command prompt (it tells you if it is not one).
REM  Other computers stay blocked unless a firewall rule already allows Ollama from anywhere.
REM
REM     deploy\ollama-for-docker.bat          (run it again as Administrator for step 2)
REM     deploy\ollama-for-docker.bat undo     (back to 127.0.0.1 and remove the rule)
REM ===========================================================================

set "RULE=Ollama - LexAI containers (WSL)"
set "APP=%LOCALAPPDATA%\Programs\Ollama\ollama app.exe"

if /I "%~1"=="undo" goto undo

echo --- 1. Ollama listening address ---
netstat -ano | findstr /C:"0.0.0.0:11434 " | findstr /C:"LISTENING" >nul
if not errorlevel 1 (
    echo Ollama already listens on all addresses ^(0.0.0.0:11434^).
    goto firewall
)
setx OLLAMA_HOST 0.0.0.0:11434 >nul
set "OLLAMA_HOST=0.0.0.0:11434"
echo Set OLLAMA_HOST=0.0.0.0:11434 for your user. Restarting Ollama ...
taskkill /IM "ollama app.exe" /F >nul 2>&1
taskkill /IM "ollama.exe" /F >nul 2>&1
timeout /t 3 /nobreak >nul
if exist "%APP%" (start "" "%APP%") else (start "Ollama" /MIN ollama serve)
set /a "TRIES=0"
:wait
timeout /t 2 /nobreak >nul
netstat -ano | findstr /C:"0.0.0.0:11434 " | findstr /C:"LISTENING" >nul
if not errorlevel 1 goto listening
set /a "TRIES+=1"
if %TRIES% LSS 30 goto wait
echo WARNING: Ollama is not listening on 0.0.0.0:11434 after a minute.
echo Quit Ollama from the tray, open a NEW Command Prompt and start Ollama again.
goto firewall
:listening
echo Ollama now listens on 0.0.0.0:11434.

:firewall
echo.
echo --- 2. Windows Firewall ---
netsh advfirewall firewall show rule name="%RULE%" >nul 2>&1
if not errorlevel 1 (
    echo Firewall rule "%RULE%" already exists.
    goto done
)
net session >nul 2>&1
if errorlevel 1 (
    echo This window is not an Administrator command prompt, so the firewall rule was not added.
    echo If Windows showed a "Windows Defender Firewall has blocked some features of Ollama"
    echo dialog, allow it for PRIVATE networks only. Otherwise open "Command Prompt" with
    echo "Run as administrator", go to this folder and run:  deploy\ollama-for-docker.bat
    goto done
)
netsh advfirewall firewall add rule name="%RULE%" dir=in action=allow protocol=TCP localport=11434 remoteip=172.16.0.0/12 profile=any
echo Added firewall rule "%RULE%" (TCP 11434 from 172.16.0.0/12 only).

:done
echo.
echo Next: deploy\lexai.bat network     (checks from inside a container)
exit /b 0

:undo
setx OLLAMA_HOST 127.0.0.1:11434 >nul
echo OLLAMA_HOST set back to 127.0.0.1:11434 (restart Ollama to apply).
net session >nul 2>&1
if errorlevel 1 (
    echo Run this again as Administrator to remove the firewall rule "%RULE%".
    exit /b 0
)
netsh advfirewall firewall delete rule name="%RULE%"
exit /b 0
