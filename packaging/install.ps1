# ============================================================================
# install.ps1 — Installation OFFLINE de Performator (Windows x64)
# Aucun acces internet requis : tout est dans le package.
#
# Usage (PowerShell, depuis le dossier du package decompresse) :
#     powershell -ExecutionPolicy Bypass -File .\install.ps1
#     powershell -ExecutionPolicy Bypass -File .\install.ps1 -InstallDir D:\performator
# ============================================================================
param(
    [string]$InstallDir = "$env:USERPROFILE\performator",
    [string]$ModelsDir  = "$env:USERPROFILE\.ollama\models"
)

$ErrorActionPreference = 'Stop'
$pkg = $PSScriptRoot

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ---------------------------------------------------------------- 1. Python
Step "1/8 Python 3.12"
$py = $null
foreach ($cand in @('python', 'python3')) {
    $c = Get-Command $cand -ErrorAction SilentlyContinue
    if ($c) {
        $v = & $c.Source --version 2>&1
        if ($v -match 'Python 3\.(1[0-9])') { $py = $c.Source; break }
    }
}
if (-not $py) {
    Write-Host "Python 3.10+ introuvable, installation silencieuse de Python 3.12.7..."
    $p = Start-Process "$pkg\tools\python-3.12.7-amd64.exe" `
        -ArgumentList '/quiet','InstallAllUsers=0','PrependPath=1','Include_test=0' `
        -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Installation Python echouee (code $($p.ExitCode))" }
    $py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
}
Write-Host "Python : $py ($(& $py --version))"

# ------------------------------------------------------- 2. Application + venv
Step "2/8 Application et environnement virtuel"
New-Item -ItemType Directory -Force $InstallDir | Out-Null
Copy-Item "$pkg\app\*" $InstallDir -Recurse -Force
& $py -m venv "$InstallDir\.venv"
$venvPy = "$InstallDir\.venv\Scripts\python.exe"
& $venvPy -m pip install --no-index --find-links "$pkg\wheels" -r "$InstallDir\requirements.txt" --quiet
& $venvPy -c "import numpy, mcp; print('venv OK - numpy', numpy.__version__)"

# ------------------------------------------------- 3. ripgrep + ctags (portables)
Step "3/8 ripgrep et universal-ctags"
$bin = "$InstallDir\bin"
New-Item -ItemType Directory -Force $bin | Out-Null
$tmp = "$env:TEMP\perf-extract"
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive "$pkg\tools\ripgrep-15.1.0-x86_64-pc-windows-msvc.zip" $tmp
Get-ChildItem $tmp -Recurse -Filter rg.exe | Select-Object -First 1 | Copy-Item -Destination $bin
Remove-Item $tmp -Recurse -Force
Expand-Archive "$pkg\tools\ctags-v6.1.0-clang-x64.zip" $tmp
Get-ChildItem $tmp -Recurse -Include ctags.exe,readtags.exe | Copy-Item -Destination $bin
Remove-Item $tmp -Recurse -Force
Write-Host "$bin : $((Get-ChildItem $bin).Name -join ', ')"

# ---------------------------------------------------------------- 4. Ollama
Step "4/8 Ollama (variables d'environnement puis installation)"
# Les variables AVANT l'installation pour que le service les voie au 1er demarrage.
[Environment]::SetEnvironmentVariable('OLLAMA_MODELS', $ModelsDir, 'User')
[Environment]::SetEnvironmentVariable('OLLAMA_CONTEXT_LENGTH', '16384', 'User')  # 4096 par defaut casse les boucles d'outils
$ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
if (-not (Test-Path $ollamaExe)) {
    $p = Start-Process "$pkg\tools\OllamaSetup-v0.31.1.exe" `
        -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART' -Wait -PassThru
    if ($p.ExitCode -ne 0) { throw "Installation Ollama echouee (code $($p.ExitCode))" }
} else { Write-Host "Ollama deja present, installation sautee." }

# ------------------------------------------------------- 5. Modeles pre-charges
Step "5/8 Modeles (qwen3:8b + nomic-embed-text, ~5 GB, patience...)"
New-Item -ItemType Directory -Force $ModelsDir | Out-Null
robocopy "$pkg\models" $ModelsDir /E /NFL /NDL /NJH /NJS | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copie des modeles echouee (robocopy $LASTEXITCODE)" }
$global:LASTEXITCODE = 0

# ---------------------------------------------------------------- 6. OpenCode
Step "6/8 OpenCode + seed hors-ligne"
Expand-Archive "$pkg\tools\opencode-v1.17.13-windows-x64.zip" $tmp
Get-ChildItem $tmp -Recurse -Filter opencode.exe | Select-Object -First 1 | Copy-Item -Destination $bin
Remove-Item $tmp -Recurse -Force
# Seed : packages npm du provider + cache du registre de modeles (sinon OpenCode
# tente de les telecharger au premier lancement et echoue sans internet).
New-Item -ItemType Directory -Force "$env:USERPROFILE\.config" | Out-Null
Copy-Item "$pkg\opencode-seed\config" "$env:USERPROFILE\.config\opencode" -Recurse -Force
New-Item -ItemType Directory -Force "$env:USERPROFILE\.cache\opencode" | Out-Null
Copy-Item "$pkg\opencode-seed\cache\models.json" "$env:USERPROFILE\.cache\opencode\" -Force

# --------------------------------------------------------- 7. Config generee
Step "7/8 Generation de opencode.json"
$venvPyEsc  = $venvPy -replace '\\','\\'
$installEsc = $InstallDir -replace '\\','\\'
@"
{
  "`$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama (local)",
      "options": { "baseURL": "http://localhost:11434/v1" },
      "models": {
        "qwen3:8b": { "name": "Qwen3 8B", "tool_call": true }
      }
    }
  },
  "model": "ollama/qwen3:8b",
  "small_model": "ollama/qwen3:8b",
  "mcp": {
    "code-context": {
      "type": "local",
      "command": ["$venvPyEsc", "$installEsc\\mcp_server.py"],
      "enabled": true,
      "environment": {
        "CODE_ROOT": "A_REMPLACER_racine_du_code_cible",
        "TAGS_FILE": "A_REMPLACER_chemin\\tags.json",
        "DB_PATH": "",
        "LOG_PATH": "",
        "DOCS_DB": ""
      }
    }
  },
  "agent": {
    "deliver": {
      "description": "Implemente une tache cadree en non-interactif, ecritures auto-acceptees",
      "mode": "primary",
      "permission": { "edit": "allow", "write": "allow", "bash": "allow" }
    },
    "explore": {
      "description": "Lecture seule : explore et explique sans rien modifier",
      "mode": "primary",
      "permission": { "edit": "deny", "write": "deny", "bash": "ask" }
    }
  }
}
"@ | Out-File -Encoding utf8 "$InstallDir\opencode.json"

# PATH utilisateur : bin de l'installation
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath -notlike "*$bin*") {
    [Environment]::SetEnvironmentVariable('Path', "$userPath;$bin", 'User')
}

# ------------------------------------------------------------ 8. Verification
Step "8/8 Verification (smoke test)"
$env:OLLAMA_MODELS = $ModelsDir
$env:OLLAMA_CONTEXT_LENGTH = '16384'
if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
    Start-Process $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
    Start-Sleep 5
}
& "$bin\rg.exe" --version | Select-Object -First 1
& "$bin\ctags.exe" --version | Select-Object -First 1
& "$bin\opencode.exe" --version
& $ollamaExe list
Write-Host ""
Write-Host "Installation terminee dans $InstallDir" -ForegroundColor Green
Write-Host "Modeles Ollama dans $ModelsDir"
Write-Host "Ouvre une NOUVELLE console (PATH mis a jour), puis voir README-INSTALL.md."
