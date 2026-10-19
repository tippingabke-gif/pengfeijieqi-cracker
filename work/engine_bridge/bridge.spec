# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH).resolve()
work = root.parent
a = Analysis(
    [str(root / 'bridge.py')],
    pathex=[str(root), str(work / 'engine_reverse'), str(work / 'jieqi')],
    binaries=[],
    datas=[(str(work / 'engine_runtime' / 'early_patch.py'), '.')],
    hiddenimports=['frida', 'psutil', 'job_object', 'parent_guard', 'fen_compat', 'runtime_patch', 'wfp_isolation'],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='PengfeiJieQi-EngineBridge',
    debug=False, bootloader_ignore_signals=False,
    strip=False, upx=False, console=True,
)
