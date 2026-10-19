param([string] $Python = 'python')
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    & $Python -c "import struct; assert struct.calcsize('P') == 8, '64-bit Python is required'; import frida, psutil, tkinter, PyInstaller"
    if ($LASTEXITCODE -ne 0) { throw 'Install the pinned requirements-build.txt with 64-bit Python first.' }
    & $Python -m unittest discover -s work/jieqi/candidate -p 'test_*.py'
    if ($LASTEXITCODE -ne 0) { throw 'Launcher unit tests failed.' }
    & $Python work/engine_bridge/test_parent_guard_integration.py
    if ($LASTEXITCODE -ne 0) { throw 'Bridge parent-guard integration tests failed.' }
    & $Python -m PyInstaller --noconfirm --distpath work/engine_bridge/dist --workpath work/engine_bridge/build work/engine_bridge/bridge.spec
    if ($LASTEXITCODE -ne 0) { throw 'Bridge build failed.' }
    & (Join-Path $PSScriptRoot 'work/engine_bridge/dist/PengfeiJieQi-EngineBridge.exe') --check-resources
    if ($LASTEXITCODE -ne 0) { throw 'Bridge resource check failed.' }
    & $Python -m PyInstaller --noconfirm --distpath work/jieqi/dist --workpath work/jieqi/build work/jieqi/candidate/launcher.spec
    if ($LASTEXITCODE -ne 0) { throw 'Launcher build failed.' }
    & $Python -c "import subprocess,sys; sys.exit(subprocess.run([r'work/jieqi/dist/PengfeiJieQi-Launcher.exe','--selftest','--result',r'work/jieqi/dist/selftest.json']).returncode)"
    if ($LASTEXITCODE -ne 0) { throw 'Frozen launcher selftest failed.' }
    Get-Content -LiteralPath 'work/jieqi/dist/selftest.json'
} finally { Pop-Location }
