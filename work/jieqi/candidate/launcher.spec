# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
root=Path(SPECPATH)
bridge= root.parent.parent / 'engine_bridge' / 'dist' / 'PengfeiJieQi-EngineBridge.exe'
a=Analysis([str(root/'launcher.py')],pathex=[str(root)],
           binaries=[],datas=[(str(root/'profile.json'),'.'),(str(root/'scripts/repair.js'),'scripts'),
                              (str(bridge),'engine_bridge')],
           hiddenimports=['frida','psutil','tkinter','wfp_isolation'],
           hookspath=[],hooksconfig={},runtime_hooks=[],excludes=[],noarchive=False,optimize=0)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,a.binaries,a.datas,[],name='PengfeiJieQi-Launcher',
        version=str(root/'version.txt'),debug=False,bootloader_ignore_signals=False,
        strip=False,upx=False,console=False,disable_windowed_traceback=True)
