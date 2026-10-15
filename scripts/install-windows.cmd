@echo off
rem awa installer for Windows. Double-click it: it installs awa from the local awa folder
rem it sits in, for your user account (no administrator rights), or updates an existing
rem installation, and opens awa.
rem Options: --yes  --source FILE-OR-FOLDER  --home DIR  --project DIR  --shortcut-dir DIR
rem          --uv FILE  --uv-version V  --uv-sha256 HASH  --latest
rem          --no-shortcuts  --no-launch  --uninstall  --help
rem The installer itself is the PowerShell script below; this line runs it unchanged.
setlocal
set "AWA_INSTALLER=%~f0"
set "AWA_INSTALLER_ARGS=%*"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$text = [IO.File]::ReadAllText($env:AWA_INSTALLER); & ([ScriptBlock]::Create($text.Substring($text.IndexOf('#' + 'POWERSHELL'))))"
set "AWA_EXIT=%ERRORLEVEL%"
if not "%AWA_EXIT%"=="0" if not defined AWA_NO_PAUSE pause
exit /b %AWA_EXIT%

#POWERSHELL
Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$PythonVersion = "3.13"
$UvVersion = "0.11.3"  # the uv release that is downloaded when none is installed (--uv-version)

function Write-Step([string]$Text) { Write-Host ""; Write-Host "==> $Text" -ForegroundColor Cyan }
function Stop-Install([string]$Text) {
    Write-Host ""
    Write-Host "The installation did not finish: $Text" -ForegroundColor Red
    exit 1
}

function Show-Usage {
    Write-Host @"
awa installer for Windows (installs or updates awa for your user, then opens it)

awa itself comes from this local copy: the awa folder this script sits in (the
folder above scripts\), or else an awa_agents-*.whl file next to the script; awa is
never downloaded. awa's open-source dependencies are installed exactly as locked in
uv.lock of the awa folder, each checked against its SHA-256 hash (from a wheel there is no
lock: the newest versions awa's ranges allow, unverified). uv, Python and the dependencies
are downloaded from their usual sources, or from your company mirror when UV_INDEX_URL is set.

uv: an uv that is installed already is used (or the one named with --uv). Otherwise uv
$UvVersion is downloaded as the install script https://astral.sh/uv/$UvVersion/install.ps1
from astral.sh over TLS; that script checks the uv archive it fetches against a checksum it
contains. To verify the script too, take its SHA-256 on a trusted machine
(Get-FileHash of the downloaded file) and pass it with --uv-sha256: nothing runs on a mismatch.
Python is fetched by uv, which verifies it against hashes it knows.

  --yes                 do not ask before installing or uninstalling
  --source PATH         install this awa wheel file or awa folder instead
  --uv FILE             use this uv program (also AWA_UV); nothing of uv is downloaded
  --uv-version V        the uv release to download when none is installed (default $UvVersion)
  --uv-sha256 HASH      the SHA-256 the downloaded uv install script must have
  --latest              install the newest dependency versions awa's ranges allow, not the locked ones
  --home DIR            where awa is installed, a folder of its own (default: %LOCALAPPDATA%\awa)
  --project DIR         the folder awa opens (default: Documents\awa)
  --shortcut-dir DIR    put the start shortcut here (default: Desktop and Start menu)
  --no-shortcuts        do not create start shortcuts
  --no-launch           do not open awa at the end
  --uninstall           remove awa and its shortcuts (your project folder stays); only
                        a home folder this installer made is touched, and only what it put there
"@
}

# Native programs write progress to stderr; 'Stop' would turn that into an error.
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments | Out-Host
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Confirm-Step([string]$Question) {
    if ($Yes -or [Console]::IsInputRedirected) { return $true }
    $answer = Read-Host "$Question [Y/n]"
    return -not ($answer -match '^(n|no)$')
}

# ------------------------------------------------------------------ options

$Words = @([regex]::Matches([string]$env:AWA_INSTALLER_ARGS, '"([^"]*)"|(\S+)') | ForEach-Object {
    if ($_.Groups[1].Success) { $_.Groups[1].Value } else { $_.Groups[2].Value }
})
$Source = ""
$AwaHome = Join-Path $env:LOCALAPPDATA "awa"
$Documents = [Environment]::GetFolderPath("MyDocuments")
if (-not $Documents) { $Documents = Join-Path $env:USERPROFILE "Documents" }
$Project = Join-Path $Documents "awa"
$ShortcutDir = ""
$UvPath = [string]$env:AWA_UV
$UvSha256 = ""
$Latest = $false
$Yes = $false
$Shortcuts = $true
$Launch = $true
$Uninstall = $false
$Marker = ".awa-installer"  # in the home folder of every installation this script made
for ($i = 0; $i -lt $Words.Count; $i++) {
    $word = $Words[$i]
    if ($word -in @("--source", "--home", "--project", "--shortcut-dir", "--uv", "--uv-version", "--uv-sha256")) {
        if ($i + 1 -ge $Words.Count) { Stop-Install "$word needs a value" }
        $i++
        switch ($word) {
            "--source" { $Source = $Words[$i] }
            "--home" { $AwaHome = $Words[$i] }
            "--project" { $Project = $Words[$i] }
            "--shortcut-dir" { $ShortcutDir = $Words[$i] }
            "--uv" { $UvPath = $Words[$i] }
            "--uv-version" { $UvVersion = $Words[$i] }
            "--uv-sha256" { $UvSha256 = $Words[$i].ToLowerInvariant() }
        }
        continue
    }
    switch ($word) {
        "--yes" { $Yes = $true }
        "--latest" { $Latest = $true }
        "--no-shortcuts" { $Shortcuts = $false }
        "--no-launch" { $Launch = $false }
        "--uninstall" { $Uninstall = $true }
        { $_ -in @("-h", "--help") } { Show-Usage; exit 0 }
        default { Stop-Install "unknown option '$word' (see --help)" }
    }
}
$AwaHome = [IO.Path]::GetFullPath($AwaHome).TrimEnd("\")
$Project = [IO.Path]::GetFullPath($Project)
if (-not $AwaHome -or $AwaHome.Length -le 2 -or $AwaHome -eq $env:USERPROFILE.TrimEnd("\")) {
    Stop-Install "--home must be a folder of its own, not a drive or your user folder"
}
if (($Project.TrimEnd("\") + "\").StartsWith($AwaHome + "\", [StringComparison]::OrdinalIgnoreCase)) {
    Stop-Install "--home must not contain your project folder $Project"
}
if ($AwaHome.Length -gt 90) {
    Write-Host "Warning: $AwaHome is a deep folder. Windows refuses file paths longer than 260 characters, and some of awa's dependencies have long file names, so awa may fail to start with an ImportError. Use a shorter folder (--home C:\awa) or enable Windows long paths." -ForegroundColor Yellow
}
$EnvDir = Join-Path $AwaHome "env"
$EnvPython = Join-Path $EnvDir "Scripts\python.exe"
$Awa = Join-Path $EnvDir "Scripts\awa.exe"
$ShortcutDirs = if ($ShortcutDir) { @($ShortcutDir) } else {
    @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs") | Where-Object { $_ })
}

# ------------------------------------------------------------------ uninstall

if ($Uninstall) {
    if (-not (Test-Path -LiteralPath $AwaHome)) { Write-Host "awa is not installed in $AwaHome; nothing to remove."; exit 0 }
    if (-not (Test-Path -LiteralPath (Join-Path $AwaHome $Marker))) {
        Stop-Install "$AwaHome was not made by this installer (it has no $Marker file), so nothing was removed. Check --home; to remove the folder anyway, delete it yourself."
    }
    if (-not $Yes -and [Console]::IsInputRedirected) {
        Stop-Install "uninstalling asks first, and no terminal is there to ask: pass --yes to confirm."
    }
    if (-not (Confirm-Step "Remove awa from $AwaHome and its shortcuts? Your project folder stays.")) { exit 0 }
    foreach ($dir in $ShortcutDirs) {
        $link = Join-Path $dir "awa.lnk"
        if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force }
    }
    foreach ($part in @("env", "python", "uv", $Marker)) {
        $path = Join-Path $AwaHome $part
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
    if (-not (Get-ChildItem -LiteralPath $AwaHome -Force)) { Remove-Item -LiteralPath $AwaHome -Force }  # other files in it stay
    Write-Host "awa is removed. Your projects in $Project are still there." -ForegroundColor Green
    exit 0
}

# awa comes from this local copy: --source, else the awa folder around scripts\, else a wheel
# next to this script.
if (-not $Source) {
    $ScriptDir = Split-Path -Parent ([IO.Path]::GetFullPath($env:AWA_INSTALLER))
    $root = Split-Path -Parent $ScriptDir
    $pyproject = Join-Path $root "pyproject.toml"
    $wheel = Get-ChildItem -LiteralPath $ScriptDir -Filter "awa_agents-*.whl" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ((Test-Path -LiteralPath $pyproject) -and (Select-String -LiteralPath $pyproject -Pattern '^name = "awa-agents"' -Quiet)) {
        $Source = $root
    } elseif ($wheel) {
        $Source = $wheel.FullName
    } else {
        Stop-Install "no local awa found. Start the installer from the scripts folder of your awa folder, put an awa_agents-*.whl file next to it, or pass --source PATH."
    }
}
if (-not (Test-Path -LiteralPath $Source)) { Stop-Install "--source '$Source' does not exist" }
$Source = (Resolve-Path -LiteralPath $Source).Path

Write-Host "awa installer"
Write-Host "  awa comes from:    $Source"
Write-Host "  awa goes to:       $AwaHome   (only for your user; no administrator rights)"
Write-Host "  your projects:     $Project"
if (-not (Confirm-Step "Install (or update) awa now?")) { exit 0 }
if ((Test-Path -LiteralPath $AwaHome) -and -not (Test-Path -LiteralPath (Join-Path $AwaHome $Marker)) -and
    @(Get-ChildItem -LiteralPath $AwaHome -Force).Count -gt 0) {
    Stop-Install "$AwaHome already holds files this installer did not make (it has no $Marker file), so nothing was changed. Choose a folder of its own with --home."
}
New-Item -ItemType Directory -Force -Path $AwaHome | Out-Null
Set-Content -LiteralPath (Join-Path $AwaHome $Marker) -Value "made by the awa installer; --uninstall removes only env, python and uv here"

# ------------------------------------------------------------------ 1. uv

Write-Step "Looking for uv (the tool that installs Python and awa)"
$Uv = $null
if ($UvPath) {
    if (-not (Test-Path -LiteralPath $UvPath -PathType Leaf)) { Stop-Install "--uv '$UvPath' is not a file" }
    $Uv = (Resolve-Path -LiteralPath $UvPath).Path
} else {
    $found = Get-Command uv -ErrorAction SilentlyContinue
    if ($found) { $Uv = $found.Source }
    foreach ($candidate in @((Join-Path $AwaHome "uv\uv.exe"), "$env:USERPROFILE\.local\bin\uv.exe")) {
        if (-not $Uv -and (Test-Path -LiteralPath $candidate)) { $Uv = $candidate }
    }
}
if (-not $Uv) {
    $url = "https://astral.sh/uv/$UvVersion/install.ps1"
    Write-Host "    Downloading uv ${UvVersion}: $url"
    $script = Join-Path ([IO.Path]::GetTempPath()) ("awa-uv-install-" + [Guid]::NewGuid().ToString("N") + ".ps1")
    try {
        try { Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $script }
        catch { Stop-Install "uv could not be downloaded. Check the internet connection (behind a company proxy, set HTTPS_PROXY) and run the installer again." }
        if ($UvSha256) {
            $actual = (Get-FileHash -LiteralPath $script -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -ne $UvSha256) {
                Stop-Install "the downloaded uv install script has the SHA-256 $actual, not the $UvSha256 given with --uv-sha256. Nothing was run."
            }
            Write-Host "    The uv install script matches --uv-sha256."
        } else {
            Write-Host "    The uv install script is not verified (pass --uv-sha256 to check it; see --help)."
        }
        $env:UV_INSTALL_DIR = Join-Path $AwaHome "uv"
        $env:UV_NO_MODIFY_PATH = "1"
        $env:INSTALLER_NO_MODIFY_PATH = "1"
        $code = Invoke-Native "powershell" @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $script)
    } finally {
        Remove-Item -LiteralPath $script -Force -ErrorAction SilentlyContinue
    }
    $Uv = Join-Path $AwaHome "uv\uv.exe"
    if ($code -ne 0 -or -not (Test-Path -LiteralPath $Uv)) {
        Stop-Install "uv could not be installed (see above). Check the internet connection and run the installer again."
    }
}
Write-Host "    uv: $Uv"

# ------------------------------------------------------------------ 2. environment

Write-Step "Preparing a private Python environment"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $AwaHome "python"
$env:UV_PYTHON_INSTALL_BIN = "0"
if (Test-Path -LiteralPath $EnvPython) {
    if ((Invoke-Native $EnvPython @("-c", "import sys")) -ne 0) {
        Write-Host "    The existing environment does not start; creating it again."
        Remove-Item -LiteralPath $EnvDir -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $EnvPython)) {
    $code = Invoke-Native $Uv @("venv", $EnvDir, "--python", $PythonVersion, "--python-preference", "only-managed")
    if ($code -ne 0) { Stop-Install "Python $PythonVersion could not be set up (see above). Check the internet connection and try again." }
}

# ------------------------------------------------------------------ 3. awa

# The reinstall makes an update pick up changed files that keep the same version number.
# From the awa folder the dependencies are the ones locked in its uv.lock, each checked by hash.
if (-not $Latest -and (Test-Path -LiteralPath (Join-Path $Source "uv.lock") -PathType Leaf)) {
    Write-Step "Installing the locked dependencies of awa (checked by hash)"
    $locked = Join-Path ([IO.Path]::GetTempPath()) ("awa-locked-" + [Guid]::NewGuid().ToString("N") + ".txt")
    try {
        $code = Invoke-Native $Uv @("export", "--frozen", "--no-dev", "--no-emit-project", "--no-header",
            "--extra", "all", "--project", $Source, "--format", "requirements-txt", "-o", $locked)
        if ($code -ne 0) { Stop-Install "the dependency lock of $Source could not be read (see above)." }
        $code = Invoke-Native $Uv @("pip", "install", "--python", $EnvPython, "--require-hashes", "-r", $locked)
        if ($code -ne 0) { Stop-Install "installing the dependencies failed (see above). Check the internet connection and run the installer again." }
    } finally {
        Remove-Item -LiteralPath $locked -Force -ErrorAction SilentlyContinue
    }
    Write-Step "Installing $Source"
    $code = Invoke-Native $Uv @("pip", "install", "--python", $EnvPython, "--no-deps", "--reinstall-package", "awa-agents", $Source)
    if ($code -ne 0) { Stop-Install "installing awa failed (see above)." }
} else {
    if (-not $Latest) {
        Write-Host "    Note: $Source has no uv.lock, so the dependencies are the newest versions awa's ranges allow, not checked by hash. Install from the awa folder for locked ones."
    }
    Write-Step "Installing $Source[all]"
    $code = Invoke-Native $Uv @("pip", "install", "--python", $EnvPython, "--upgrade", "--reinstall-package", "awa-agents", "$Source[all]")
    if ($code -ne 0) { Stop-Install "installing awa failed (see above). Check the internet connection and run the installer again." }
}
$version = & $Awa --version
if ($LASTEXITCODE -ne 0) { Stop-Install "awa was installed but does not start ($Awa)" }
Write-Host "    $version" -ForegroundColor Green

# ------------------------------------------------------------------ 4. shortcuts

New-Item -ItemType Directory -Force -Path $Project | Out-Null
if ($Shortcuts) {
    Write-Step "Creating the 'awa' shortcut"
    $shell = New-Object -ComObject WScript.Shell
    foreach ($dir in $ShortcutDirs) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $link = $shell.CreateShortcut((Join-Path $dir "awa.lnk"))
        $link.TargetPath = $Awa
        $link.Arguments = "ui --project `"$Project`""
        $link.WorkingDirectory = $Project
        $link.Description = "Open awa: turn a process description into a tested system"
        $link.Save()
        Write-Host "    $(Join-Path $dir 'awa.lnk')"
    }
}

Write-Host ""
Write-Host "awa is installed. Start it any time with the 'awa' shortcut." -ForegroundColor Green
Write-Host "To update later, run this installer again; to remove awa, run it with --uninstall."
Write-Host "To use the awa command in a PowerShell window, run this line in it first:"
Write-Host ("    `$env:PATH = `"" + (Join-Path $EnvDir "Scripts") + ";`$env:PATH`"")
if (-not $Launch) { exit 0 }

Write-Step "Starting awa (your browser opens in a moment)"
Write-Host "Keep this window open while you use awa; close it to stop awa."
& $Awa ui --project $Project
exit $LASTEXITCODE
