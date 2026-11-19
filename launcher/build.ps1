# Builds NIFTY_PaperTrading.exe and Stop_Dashboard.exe with the C# compiler that ships with Windows.
# No installs, no internet, no Python packaging.
#
#   .\build.ps1                       build into launcher\dist
#   .\build.ps1 -Install              also copy both programs to your Desktop and write app_dir.txt
#   .\build.ps1 -Install -Target D:\x copy them to another folder instead
param(
    [switch]$Install,
    [string]$Target = [Environment]::GetFolderPath('Desktop')
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path $csc)) { throw 'The .NET Framework 4 compiler (csc.exe) was not found.' }

$dist = Join-Path $here 'dist'
New-Item -ItemType Directory -Force $dist | Out-Null
$refs = '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', '/r:System.Management.dll'
$common = Join-Path $here 'src\Common.cs'

& $csc /nologo /target:winexe /optimize+ /out:"$dist\NIFTY_PaperTrading.exe" @refs $common (Join-Path $here 'src\Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Build of NIFTY_PaperTrading.exe failed.' }
& $csc /nologo /target:winexe /optimize+ /out:"$dist\Stop_Dashboard.exe" @refs $common (Join-Path $here 'src\StopDashboard.cs')
if ($LASTEXITCODE -ne 0) { throw 'Build of Stop_Dashboard.exe failed.' }
Write-Host "Built: $dist\NIFTY_PaperTrading.exe and $dist\Stop_Dashboard.exe"

if ($Install) {
    $app = Join-Path (Split-Path -Parent $here) 'application'
    if (-not (Test-Path (Join-Path $app 'run_ml_demo.py'))) { throw "run_ml_demo.py not found in $app" }
    New-Item -ItemType Directory -Force $Target | Out-Null
    Copy-Item "$dist\NIFTY_PaperTrading.exe", "$dist\Stop_Dashboard.exe" $Target -Force
    # Next to the project folder the programs find it by themselves; otherwise record where it is.
    $sibling = Join-Path $Target 'NIFTY-Options-ML-Paper-Trading\application'
    if ((Test-Path $sibling) -and ((Resolve-Path $sibling).Path -eq (Resolve-Path $app).Path)) {
        Write-Host "Installed to $Target (project folder found next to it; no app_dir.txt needed)"
    } else {
        Set-Content -Path (Join-Path $Target 'app_dir.txt') -Value $app -Encoding ASCII
        Write-Host "Installed to $Target (app_dir.txt points at $app)"
    }
}
