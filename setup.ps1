# One-time setup for stream-clipper on Windows.
# Run in PowerShell from the project folder:   powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Python 3.11/3.12 has the most reliable wheels for faster-whisper / CTranslate2.
$py = $null
foreach ($v in @("3.12", "3.11")) {
    try { & py -$v -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = "py -$v"; break } } catch {}
}
if (-not $py) {
    foreach ($exe in @("python", "python3")) {
        $cmd = Get-Command $exe -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike "*WindowsApps*") {
            $ver = & $exe -c "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
            if ($ver -in @("3.11", "3.12")) { $py = $exe; break }
        }
    }
}
if (-not $py) {
    Write-Host "Python 3.11 or 3.12 was not found." -ForegroundColor Red
    Write-Host "Install Python 3.12 from https://www.python.org/downloads/windows/ (64-bit installer)," -ForegroundColor Yellow
    Write-Host "tick 'Add python.exe to PATH', then reopen this terminal and run setup.ps1 again." -ForegroundColor Yellow
    exit 1
}
Write-Host "Using Python: $py"

if (-not (Test-Path .venv)) {
    Write-Host "Creating virtual environment (.venv)..."
    Invoke-Expression "$py -m venv .venv"
}
& .\.venv\Scripts\python -m pip install --upgrade pip
& .\.venv\Scripts\python -m pip install -r requirements.txt
& .\.venv\Scripts\python -m pip install -r requirements-gpu.txt

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg not found. Installing with winget (Gyan.FFmpeg)..." -ForegroundColor Yellow
    winget install -e --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
    Write-Host "Close and reopen the terminal (or Antigravity) so ffmpeg is on PATH." -ForegroundColor Yellow
}

# Install the Claude Code /make-clips command for this project.
New-Item -ItemType Directory -Force -Path .claude\commands | Out-Null
Copy-Item -Force claude-setup\make-clips.md .claude\commands\make-clips.md
Copy-Item -Force claude-setup\live-clips.md .claude\commands\live-clips.md
Write-Host "Claude Code commands installed: /make-clips, /live-clips"

# Antigravity: regenerate its workflows from the Claude commands so both behave the same.
& .\.venv\Scripts\python tools\sync_antigravity.py
if (Get-Command gemini -ErrorAction SilentlyContinue) {
    Write-Host "Gemini CLI found: --agent gemini is available for run/live (Antigravity users)."
} else {
    Write-Host "Optional for Antigravity users: npm install -g @google/gemini-cli  (then run 'gemini' once to sign in) enables --agent gemini." -ForegroundColor Yellow
}

Write-Host "`nEnvironment check:" -ForegroundColor Cyan
& .\.venv\Scripts\python -m clipper doctor
