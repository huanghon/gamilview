#requires -Version 5
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$WorkRoot = Join-Path $PSScriptRoot "_build"
$DistDir  = Join-Path $WorkRoot "dist"
$BuildDir = Join-Path $WorkRoot "build"
$SpecDir  = Join-Path $WorkRoot "spec"
$OutDir   = Join-Path $PSScriptRoot "GmailViewer"
$ZipPath  = Join-Path $PSScriptRoot "GmailViewer.zip"

Write-Host "[1/5] cleaning..." -ForegroundColor Cyan
Remove-Item -Recurse -Force $WorkRoot, $OutDir, $ZipPath -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $WorkRoot, $DistDir, $BuildDir, $SpecDir | Out-Null

Write-Host "[2/5] running PyInstaller..." -ForegroundColor Cyan
$TemplatesSrc = Join-Path $PSScriptRoot "templates"
$StaticSrc    = Join-Path $PSScriptRoot "static"
python -m PyInstaller `
  --noconfirm --clean `
  --name app --onedir --console `
  --distpath $DistDir --workpath $BuildDir --specpath $SpecDir `
  --add-data "$TemplatesSrc;templates" `
  --add-data "$StaticSrc;static" `
  --collect-submodules uvicorn `
  --collect-submodules fastapi `
  --hidden-import uvicorn.logging `
  --hidden-import uvicorn.protocols `
  --hidden-import uvicorn.protocols.http.auto `
  --hidden-import uvicorn.protocols.http.h11_impl `
  --hidden-import uvicorn.protocols.websockets.auto `
  --hidden-import uvicorn.lifespan.on `
  app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Write-Host "[3/5] assembling GmailViewer\..." -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Copy-Item -Recurse -Force "$DistDir\app\*" $OutDir
Copy-Item -Force ".env.example" $OutDir
Copy-Item -Recurse -Force "config" $OutDir
New-Item -ItemType Directory -Force -Path "$OutDir\gmail_credentials" | Out-Null
New-Item -ItemType Directory -Force -Path "$OutDir\data" | Out-Null

Write-Host "[4/5] writing launcher scripts..." -ForegroundColor Cyan
Copy-Item -Force "_assets\start.bat"  "$OutDir\start.bat"
Copy-Item -Force "_assets\stop.bat"   "$OutDir\stop.bat"
Copy-Item -Force "_assets\README.txt" "$OutDir\README.txt"

Write-Host "[5/5] packing zip..." -ForegroundColor Cyan
Compress-Archive -Path $OutDir -DestinationPath $ZipPath -Force

$size = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)
Write-Host ""
Write-Host "DONE" -ForegroundColor Green
Write-Host ("  dir: {0}" -f $OutDir)
Write-Host ("  zip: {0}  ({1} MB)" -f $ZipPath, $size)
