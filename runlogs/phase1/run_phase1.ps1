# Phase 1 verification for Windows PowerShell.
#
# Run from the repository root:
#   powershell -ExecutionPolicy Bypass -File runlogs\phase1\run_phase1.ps1
#   powershell -ExecutionPolicy Bypass -File runlogs\phase1\run_phase1.ps1 -Steps smoke
#   (steps: env install quality models ingest smoke)
#
# Every step writes runlogs\phase1\output\<step>.log. Send those files back
# (or commit and push them) for review.

param(
    [string[]]$Steps = @("env", "install", "quality", "models", "ingest", "smoke"),
    [int]$Port = 8000,
    [int]$Concurrency = 5   # use 2 on CPU-only machines
)

$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Out = Join-Path $Root "runlogs\phase1\output"
New-Item -ItemType Directory -Force -Path $Out | Out-Null
Set-Location (Join-Path $Root "backend")

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:LEXAI_API_ENVIRONMENT = "ci"   # JSON logs, easier to review

function Write-Log([string]$File, [string]$Text) {
    $Text | Tee-Object -FilePath $File -Append
}

function Invoke-Logged([string]$File, [string]$Exe, [string[]]$CmdArgs) {
    Write-Log $File ("$ " + $Exe + " " + ($CmdArgs -join " "))
    & $Exe @CmdArgs 2>&1 | ForEach-Object { "$_" } | Tee-Object -FilePath $File -Append
    Write-Log $File ("exit=" + $LASTEXITCODE)
    Write-Log $File ""
}

function Step-Env {
    $f = Join-Path $Out "01_environment.log"; Set-Content -Path $f -Value ""
    Invoke-Logged $f "git" @("-C", $Root, "rev-parse", "--abbrev-ref", "HEAD")
    Invoke-Logged $f "git" @("-C", $Root, "rev-parse", "HEAD")
    Write-Log $f ([System.Environment]::OSVersion.VersionString)
    Invoke-Logged $f "uv" @("--version")
    Invoke-Logged $f "uv" @("run", "--no-sync", "python", "--version")
    Invoke-Logged $f "ollama" @("--version")
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { Invoke-Logged $f "nvidia-smi" @() }
    else { Write-Log $f "nvidia-smi: not found (CPU only?)" }
}

function Step-Install {
    $f = Join-Path $Out "02_install.log"; Set-Content -Path $f -Value ""
    Invoke-Logged $f "uv" @("sync", "--extra", "rerank")
}

function Step-Quality {
    $f = Join-Path $Out "03_quality.log"; Set-Content -Path $f -Value ""
    Invoke-Logged $f "uv" @("run", "ruff", "check", ".")
    Invoke-Logged $f "uv" @("run", "ruff", "format", "--check", ".")
    Invoke-Logged $f "uv" @("run", "mypy")
    Invoke-Logged $f "uv" @("run", "pytest", "-v")
}

function Step-Models {
    $f = Join-Path $Out "04_models.log"; Set-Content -Path $f -Value ""
    # Downloads print progress bars, so they go to the terminal only.
    & ollama pull qwen2.5:7b-instruct-q4_K_M
    & ollama pull qwen3-embedding:0.6b
    Invoke-Logged $f "ollama" @("create", "qwen2.5-7b-instruct", "-f", (Join-Path $Root "deploy\ollama\Modelfile"))
    Invoke-Logged $f "ollama" @("list")
    Invoke-Logged $f "ollama" @("show", "qwen2.5-7b-instruct")
    try {
        $models = Invoke-RestMethod -Uri "http://localhost:11434/v1/models" -TimeoutSec 10
        Write-Log $f ("served models: " + (($models.data | ForEach-Object { $_.id }) -join ", "))
        $body = @{ model = "qwen3-embedding:0.6b"; input = @("test") } | ConvertTo-Json
        $emb = Invoke-RestMethod -Uri "http://localhost:11434/v1/embeddings" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 120
        Write-Log $f ("embedding dimension: " + $emb.data[0].embedding.Count)
    } catch {
        Write-Log $f ("Ollama check failed: " + $_.Exception.Message)
    }
}

function Step-Ingest {
    $f = Join-Path $Out "05_ingest.log"; Set-Content -Path $f -Value ""
    Invoke-Logged $f "uv" @("run", "lexai-ingest")
}

function Step-Smoke {
    $f = Join-Path $Out "06_smoke.log"; Set-Content -Path $f -Value ""
    $serverLog = Join-Path $Out "06_server.log"
    $serverErr = Join-Path $Out "06_server.err.log"
    $server = Start-Process -FilePath "uv" -PassThru -NoNewWindow `
        -ArgumentList @("run", "uvicorn", "lexai.api.app:create_app", "--factory", "--host", "127.0.0.1", "--port", "$Port") `
        -RedirectStandardOutput $serverLog -RedirectStandardError $serverErr
    Write-Log $f ("API started (pid " + $server.Id + "). First start downloads the reranker model (~2 GB); waiting up to 15 min ...")
    $up = $false
    for ($i = 0; $i -lt 900; $i++) {
        if ($server.HasExited) { Write-Log $f "API process exited; see 06_server.log and 06_server.err.log"; break }
        try {
            Invoke-WebRequest -Uri "http://127.0.0.1:$Port/healthz" -UseBasicParsing -TimeoutSec 2 | Out-Null
            $up = $true; break
        } catch { Start-Sleep -Seconds 1 }
    }
    if ($up) {
        Invoke-Logged $f "uv" @("run", "python", "scripts/smoke_phase1.py", "--base-url", "http://127.0.0.1:$Port", "--concurrency", "$Concurrency")
    }
    & taskkill /PID $server.Id /T /F | Out-Null
    Write-Log $f "API stopped."
}

foreach ($s in $Steps) {
    Write-Host "########## $s ##########"
    switch ($s) {
        "env"     { Step-Env }
        "install" { Step-Install }
        "quality" { Step-Quality }
        "models"  { Step-Models }
        "ingest"  { Step-Ingest }
        "smoke"   { Step-Smoke }
        default   { Write-Host "unknown step: $s (env install quality models ingest smoke)" }
    }
}

Write-Host ""
Write-Host "Done. Logs are in runlogs\phase1\output\. Next: the outage check in runlogs\phase1\README.md (step 7)."
