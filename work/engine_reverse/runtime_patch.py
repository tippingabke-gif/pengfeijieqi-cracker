"""Workspace adapter for the verified early ACE runtime patch module.

The caller must own the newly spawned Popen and keep this handle until child exit.
For packaging, copy early_patch.py alongside this module.
"""
from pathlib import Path
import importlib.util
import sys

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'early_patch.py'
if not SOURCE.exists():
    SOURCE = HERE.parent / 'engine_runtime' / 'early_patch.py'
spec = importlib.util.spec_from_file_location('_ace_early_runtime_patch', SOURCE)
impl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = impl
spec.loader.exec_module(impl)

def apply_patch(proc, engine_path, emit=lambda event: None):
    handle = impl.apply_patch(proc, engine_path, emit)
    try:
        if not handle.wait_ready(9):
            raise RuntimeError('ACE early runtime patch failed: '+repr(handle.events))
        return handle
    except Exception:
        handle.close()
        raise
