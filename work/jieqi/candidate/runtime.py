"""Local process lifecycle. Only the helper's private application copy is modified."""
from pathlib import Path
import ctypes as C
from ctypes import wintypes as W
import hashlib, json, os, shutil, subprocess, sys, threading, time, uuid

from engine_config_migration import (BRIDGE_NAME, ENGINE_SHA256, MigrationError,
                                     install_engine_bridge)

VERSION = '1.2.0-jieqi'
ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
PROFILE = json.loads((ROOT / 'profile.json').read_text(encoding='utf-8'))
PUBLIC_KEYS = ('launcher_version', 'target_version', 'stage', 'error_code', 'running',
               'script_errors', 'exit_code', 'main_window_seen', 'ready_events',
               'engine_bridge_installed', 'engine_bridge_ready', 'engine_ready',
               'engine_state', 'engine_error')

class LaunchError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def sha256(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def resolve_source(value):
    p = Path(value).expanduser().resolve()
    if p.is_dir():
        p = p / 'PengfeiChess.exe' if (p / 'PengfeiChess.exe').is_file() else p / 'App/PengfeiChess.exe'
    if not p.is_file() or p.name.lower() != 'pengfeichess.exe':
        raise LaunchError('E_FILE')
    if sha256(p) != PROFILE['target_sha256']:
        raise LaunchError('E_VERSION')
    return p

def default_data_dir(source):
    key = hashlib.sha256(str(source.parent).casefold().encode('utf-8')).hexdigest()[:12]
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'PengfeiChessLauncher' / key

def public_status(status):
    return {k: status[k] for k in PUBLIC_KEYS if k in status}

def prepare_copy(source, data_dir):
    source = resolve_source(source)
    data_dir = Path(data_dir).resolve()
    if data_dir == source.parent or source.parent in data_dir.parents or data_dir in source.parent.parents:
        raise LaunchError('E_DATA_PATH')
    data_dir.mkdir(parents=True, exist_ok=True)
    dest = data_dir / 'App'
    if dest.exists():
        if not (dest / 'PengfeiChess.exe').is_file() or sha256(dest / 'PengfeiChess.exe') != PROFILE['target_sha256']:
            raise LaunchError('E_COPY_VERSION')
        return dest / 'PengfeiChess.exe'
    # A partial copy never becomes the active App folder. No existing user
    # configuration is overwritten on subsequent starts.  Some Windows
    # policy/AV combinations deny the final directory rename even though
    # creating and deleting children is allowed, so populate the final
    # directory directly and verify it before returning.
    for directory, dirs, files in os.walk(source.parent, followlinks=False):
        for name in dirs + files:
            p=Path(directory)/name
            if p.is_symlink() or getattr(p.lstat(),'st_file_attributes',0) & 0x400:
                raise LaunchError('E_LINK')
    shutil.copytree(source.parent, dest, ignore=shutil.ignore_patterns('log', '对战记录棋谱', '__pycache__', '*.dmp'))
    if sha256(dest / 'PengfeiChess.exe') != PROFILE['target_sha256']:
        raise LaunchError('E_COPY_VERSION')
    return dest / 'PengfeiChess.exe'

def render_script(log_path):
    script = (ROOT / 'scripts/repair.js').read_text(encoding='utf-8')
    placeholder = '"__PF_LOCAL_LOG__"'
    if script.count(placeholder) != 1:
        raise LaunchError('E_SCRIPT_RESOURCE')
    return script.replace(placeholder, json.dumps(str(Path(log_path).resolve())))

def bridge_resource():
    bundled = ROOT / 'engine_bridge' / BRIDGE_NAME
    if not getattr(sys, 'frozen', False) and not bundled.is_file():
        return ROOT.parent.parent / 'engine_bridge' / 'dist' / BRIDGE_NAME
    return bundled

class Session:
    def __init__(self, source, data_dir=None):
        self.source = resolve_source(source)
        self.data_dir = Path(data_dir).resolve() if data_dir else default_data_dir(self.source)
        self.target = None
        self.proc = self.process_info = None
        self.handle = self.iso = self.agent_session = self.agent = self.mutex = None
        self.base = self.size = 0
        self.event_lock = threading.Lock()
        self.stop_requested = threading.Event()
        self.status = {'launcher_version': VERSION, 'target_version': PROFILE['version'],
                       'stage': 'preparing', 'error_code': None, 'running': False,
                       'script_errors': 0, 'exit_code': None, 'main_window_seen': False,
                       'ready_events': [], 'engine_bridge_installed': False,
                       'engine_bridge_ready': False, 'engine_ready': False,
                       'engine_state': 'not_started', 'engine_error': None}
        self.ready = set()
        self.run_dir = None
        self.close_requested = False
        self.engine_info = None
        self.child_env = None

    def initialize_api(self):
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.ps = C.WinDLL('psapi', use_last_error=True)
        specs = {
            'OpenProcess': ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            'CloseHandle': ([W.HANDLE], W.BOOL),
            'ReadProcessMemory': ([W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)], W.BOOL),
            'WriteProcessMemory': ([W.HANDLE,C.c_void_p,C.c_void_p,C.c_size_t,C.POINTER(C.c_size_t)], W.BOOL),
            'VirtualProtectEx': ([W.HANDLE,C.c_void_p,C.c_size_t,W.DWORD,C.POINTER(W.DWORD)], W.BOOL),
            'FlushInstructionCache': ([W.HANDLE,C.c_void_p,C.c_size_t], W.BOOL),
            'CreateMutexW': ([C.c_void_p,W.BOOL,W.LPCWSTR], W.HANDLE),
        }
        for name,(args,ret) in specs.items():
            fn=getattr(self.k,name);fn.argtypes=args;fn.restype=ret
        class MI(C.Structure):
            _fields_=[('base',C.c_void_p),('size',W.DWORD),('entry',C.c_void_p)]
        self.MI=MI
        self.ps.EnumProcessModulesEx.argtypes=[W.HANDLE,C.POINTER(W.HMODULE),W.DWORD,C.POINTER(W.DWORD),W.DWORD]
        self.ps.EnumProcessModulesEx.restype=W.BOOL
        self.ps.GetModuleInformation.argtypes=[W.HANDLE,W.HMODULE,C.POINTER(MI),W.DWORD]
        self.ps.GetModuleInformation.restype=W.BOOL
        self.ps.GetModuleFileNameExW.argtypes=[W.HANDLE,W.HMODULE,W.LPWSTR,W.DWORD]
        self.ps.GetModuleFileNameExW.restype=W.DWORD
        self.u = C.WinDLL('user32', use_last_error=True)
        self.enum_callback = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        window_specs = {
            'EnumWindows': ([self.enum_callback, W.LPARAM], W.BOOL),
            'GetWindowThreadProcessId': ([W.HWND, C.POINTER(W.DWORD)], W.DWORD),
            'IsWindowVisible': ([W.HWND], W.BOOL),
            'GetWindowTextW': ([W.HWND, W.LPWSTR, C.c_int], C.c_int),
            'PostMessageW': ([W.HWND, W.UINT, W.WPARAM, W.LPARAM], W.BOOL),
        }
        for name, (args, ret) in window_specs.items():
            fn = getattr(self.u, name); fn.argtypes = args; fn.restype = ret

    def ensure(self):
        if not self.proc or self.proc.poll() is not None:
            raise LaunchError('E_PROCESS_EXIT')
        try:
            p=self.process_info
            if p.create_time()!=self.birth or Path(p.exe()).resolve()!=self.target.resolve():
                raise LaunchError('E_PROCESS_IDENTITY')
        except Exception:
            if self.proc.poll() is not None:
                raise LaunchError('E_PROCESS_EXIT') from None
            raise

    def main_windows(self):
        """Visible main windows belonging to this exact child process."""
        self.ensure()
        windows = []
        def visit(hwnd, unused):
            owner = W.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, C.byref(owner))
            if owner.value == self.proc.pid and self.u.IsWindowVisible(hwnd):
                title = C.create_unicode_buffer(512)
                self.u.GetWindowTextW(hwnd, title, len(title))
                if title.value == '鹏飞揭棋 5.9.1.0 Board':
                    windows.append(hwnd)
            return True
        callback = self.enum_callback(visit)
        self.u.EnumWindows(callback, 0)
        return windows

    def read(self, rva, n):
        self.ensure()
        if not 0 <= rva <= self.size-n or not 0 < n <= 1024*1024:
            raise LaunchError('E_ADDRESS')
        buf=C.create_string_buffer(n);got=C.c_size_t()
        if not self.k.ReadProcessMemory(self.handle,self.base+rva,buf,n,C.byref(got)) or got.value!=n:
            raise LaunchError('E_READ')
        return buf.raw

    def write(self, rva, before, after):
        if len(before)!=len(after) or self.read(rva,len(before))!=before:
            raise LaunchError('E_SIGNATURE')
        old=W.DWORD();addr=self.base+rva
        if not self.k.VirtualProtectEx(self.handle,addr,len(after),0x40,C.byref(old)):
            raise LaunchError('E_PROTECT')
        try:
            got=C.c_size_t();buf=C.create_string_buffer(after)
            if not self.k.WriteProcessMemory(self.handle,addr,buf,len(after),C.byref(got)) or got.value!=len(after):
                raise LaunchError('E_WRITE')
            if not self.k.FlushInstructionCache(self.handle,addr,len(after)) or self.read(rva,len(after))!=after:
                raise LaunchError('E_WRITE_VERIFY')
        finally:
            ignored=W.DWORD();self.k.VirtualProtectEx(self.handle,addr,len(after),old.value,C.byref(ignored))

    def on_message(self, message, data):
        with self.event_lock:
            with (self.run_dir/'instrument.jsonl').open('a',encoding='utf-8') as f:
                f.write(json.dumps({'time':time.time(),'message':message},ensure_ascii=False)+'\n')
            if message.get('type')=='error':
                self.status['script_errors']+=1
            payload=message.get('payload',{})
            event=payload.get('event')
            if event:
                self.ready.add(event)
                self.status['ready_events'] = sorted(self.ready)

    def _refresh_engine_status(self):
        """Keep bridge installation, protocol readiness, and search proof distinct."""
        if not self.run_dir:
            return
        log_dir = self.run_dir / 'engine_bridge'
        if not log_dir.is_dir():
            return
        events = []
        for path in sorted(log_dir.glob('*.jsonl')):
            try:
                lines = path.read_text(encoding='utf-8').splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(row, dict):
                    try:
                        event_time = float(row.get('time', 0))
                    except (TypeError, ValueError):
                        continue
                    events.append((event_time, str(path), row))
        events.sort(key=lambda entry: entry[0])
        active_log = None
        active_ids = None
        for _time, source_log, row in events:
            event = row.get('event')
            if event == 'bridge_start':
                active_log, active_ids = source_log, None
                self.status.update(engine_ready=False, engine_state='starting', engine_error=None)
            elif event == 'bridge_ready':
                # A readiness record is useful only when it identifies the
                # exact ACE binary which was validated by the bridge.
                if str(row.get('engine_sha256', '')).casefold() != ENGINE_SHA256:
                    continue
                active_log = source_log
                active_ids = (row.get('bridge_pid'), row.get('engine_pid'))
                self.status.update(engine_bridge_ready=True, engine_ready=False,
                                   engine_state='bridge_ready', engine_error=None)
            elif event == 'protocol_ready':
                # This is a separate bridge-produced event after a GUI/native
                # protocol exchange. Runtime patching or synthetic auth output
                # alone does not establish that the GUI can use the engine.
                ids = (row.get('bridge_pid'), row.get('engine_pid'))
                if (source_log != active_log or ids != active_ids or
                        not all(isinstance(pid, int) and pid > 0 for pid in ids) or
                        str(row.get('engine_sha256', '')).casefold() != ENGINE_SHA256):
                    continue
                self.status.update(engine_ready=True, engine_state='ready', engine_error=None)
            elif event == 'host_stdin':
                # Older bridge builds do not emit protocol_ready. An explicit
                # UCI isready/go line from the GUI is the minimum equivalent
                # handshake evidence; encrypted search records are not.
                line = str(row.get('line', '')).strip().casefold()
                token = line.split(None, 1)[0] if line else ''
                if (source_log == active_log and active_ids is not None and
                        all(isinstance(pid, int) and pid > 0 for pid in active_ids) and
                        token in {'isready', 'go'}):
                    self.status.update(engine_ready=True, engine_state='ready', engine_error=None)
            elif event in {'runtime_patch_error', 'io_error', 'engine_close_error',
                           'wfp_close_error', 'bridge_error', 'job_close_error',
                           'runtime_patch_close_error'}:
                if active_log is None or source_log == active_log:
                    active_ids = None
                    self.status.update(engine_ready=False, engine_state='error', engine_error=event)
            elif event == 'child_exit':
                if active_log is not None and source_log != active_log:
                    continue
                active_ids = None
                self.status['engine_ready'] = False
                code = row.get('returncode')
                if code not in (None, 0) or not row.get('expected', False):
                    self.status.update(engine_state='error', engine_error='child_exit')
                elif self.status.get('engine_bridge_ready'):
                    self.status['engine_state'] = 'stopped'
            elif event == 'bridge_exit':
                if active_log is not None and source_log != active_log:
                    continue
                active_ids = None
                self.status['engine_ready'] = False
                code = row.get('returncode')
                if code not in (None, 0):
                    self.status.update(engine_state='error', engine_error='bridge_exit')
                elif self.status.get('engine_bridge_ready'):
                    self.status['engine_state'] = 'stopped'

    def start(self):
        import frida, psutil
        from wfp_isolation import Isolation
        self.initialize_api()
        mutex_name='Local\\PengfeiChessLauncher-'+hashlib.sha256(str(self.data_dir).casefold().encode()).hexdigest()[:24]
        self.mutex=self.k.CreateMutexW(None,False,mutex_name)
        if not self.mutex or C.get_last_error()==183:
            raise LaunchError('E_ALREADY_RUNNING')
        self.target=prepare_copy(self.source,self.data_dir)
        self.run_dir=self.data_dir/'runs'/time.strftime('%Y%m%d-%H%M%S')
        self.run_dir.mkdir(parents=True,exist_ok=False)
        self.status['stage']='preparing_engine'
        try:
            self.engine_info = install_engine_bridge(self.target.parent, self.data_dir, bridge_resource())
        except MigrationError as exc:
            raise LaunchError(exc.code) from exc
        self.status.update(engine_bridge_installed=True, engine_state='not_started')
        self.child_env=os.environ.copy()
        self.child_env['PF_ENGINE_BRIDGE_LOG_DIR']=str((self.run_dir/'engine_bridge').resolve())
        if self.stop_requested.is_set():raise LaunchError('E_STOPPED')
        self.status['stage']='isolating'
        self.iso=Isolation()
        for exe in self.target.parent.rglob('*.exe'):
            self.iso.add(exe)
        if not self.iso.ids:
            raise LaunchError('E_ISOLATION')
        if self.stop_requested.is_set():raise LaunchError('E_STOPPED')
        self.proc=subprocess.Popen([str(self.target)],cwd=self.target.parent,env=self.child_env)
        self.process_info=psutil.Process(self.proc.pid);self.birth=self.process_info.create_time()
        self.handle=self.k.OpenProcess(0x438,False,self.proc.pid)
        if not self.handle:
            raise LaunchError('E_OPEN_PROCESS')
        modules=(W.HMODULE*2048)();needed=W.DWORD();mi=self.MI()
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            if self.stop_requested.is_set():raise LaunchError('E_STOPPED')
            self.ensure()
            if self.ps.EnumProcessModulesEx(self.handle,modules,C.sizeof(modules),C.byref(needed),3):
                # During early loader startup, the first visible module need
                # not be the application. Match the child executable itself.
                for module in modules[:min(needed.value//C.sizeof(W.HMODULE),len(modules))]:
                    name=C.create_unicode_buffer(32768)
                    if not self.ps.GetModuleFileNameExW(self.handle,module,name,len(name)):
                        continue
                    if Path(name.value).resolve()!=self.target.resolve():
                        continue
                    if self.ps.GetModuleInformation(self.handle,module,C.byref(mi),C.sizeof(mi)):
                        self.base,self.size=mi.base,mi.size
                    break
                if self.base and self.size==PROFILE['image_size']:
                    try:
                        if self.read(0xb78bd0,7).hex()=='5756534883ec20':break
                    except LaunchError as exc:
                        if exc.code!='E_READ':raise
            time.sleep(.1)
        else:
            raise LaunchError('E_RUNTIME_TIMEOUT')
        self.status['stage']='loading'
        native = PROFILE['native_auth']
        for address, signature in ((native['rva'], native['before_prefix']),
                                   (native['allocator_rva'], native['allocator_before'])):
            expected = bytes.fromhex(signature)
            if self.read(int(address, 0), len(expected)) != expected:
                raise LaunchError('E_SIGNATURE')
        # Validate every patch before writing the first byte.
        for patch in PROFILE['patches']:
            if self.read(int(patch['rva'],0),len(bytes.fromhex(patch['before']))).hex()!=patch['before']:
                raise LaunchError('E_SIGNATURE')
        for patch in PROFILE['patches']:
            self.write(int(patch['rva'],0),bytes.fromhex(patch['before']),bytes.fromhex(patch['after']))
        self.agent_session=frida.attach(self.proc.pid)
        self.agent=self.agent_session.create_script(render_script(self.run_dir/'events.jsonl'))
        self.agent.on('message',self.on_message)
        self.agent.load()
        deadline=time.monotonic()+30
        required={'local_auth_replacement_ready', 'main_background_auth_suspended',
                  'fit_vector_seed_ready', 'fit_input_label_adapter_ready',
                  'classifier_score_repair_ready'}
        while time.monotonic()<deadline:
            if self.stop_requested.is_set():raise LaunchError('E_STOPPED')
            self.ensure()
            if self.status['script_errors']:
                raise LaunchError('E_SCRIPT')
            if required <= self.ready:
                if not self.main_windows():
                    self.status['stage'] = 'waiting_for_main_window'
                    time.sleep(.1)
                    continue
                self.status['main_window_seen'] = True
                (self.run_dir/'session.json').write_text(json.dumps({
                    'pid':self.proc.pid,'create_time':self.birth,'image_base':hex(self.base),
                    'image_size':self.size,'filter_count':len(self.iso.ids),
                    'ready':sorted(self.ready),'ready_events':sorted(self.ready),
                    'engine_bridge_installed':self.status['engine_bridge_installed'],
                    'engine_ready':self.status['engine_ready'],
                    'engine_state':self.status['engine_state'],
                    'main_window_seen':True},indent=2),encoding='utf-8')
                self.status.update(stage='running',running=True)
                return self.snapshot()
            time.sleep(.1)
        raise LaunchError('E_WINDOW_TIMEOUT' if required <= self.ready else 'E_SCRIPT_TIMEOUT')

    def snapshot(self):
        self._refresh_engine_status()
        if self.proc and self.proc.poll() is not None:
            self.status.update(stage='stopped',running=False,exit_code=self.proc.returncode,
                               engine_ready=False)
            if self.status['engine_state'] in {'starting', 'bridge_ready', 'ready'}:
                self.status['engine_state'] = 'stopped'
        return public_status(self.status)

    def close(self):
        # Keep Frida and network isolation alive until OUR child has exited.
        if self.proc and self.proc.poll() is None:
            try:
                self.ensure()
                if self.status['main_window_seen']:
                    if not self.close_requested:
                        self.close_requested = True
                        for hwnd in self.main_windows():
                            owner = W.DWORD()
                            self.u.GetWindowThreadProcessId(hwnd, C.byref(owner))
                            if owner.value == self.proc.pid:
                                self.u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                else:
                    # A failed startup has not yielded a usable board window.
                    self.proc.terminate()
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.status.update(stage='waiting_for_exit', running=True)
                return False
            except Exception:
                # Natural exit between poll/identity/window checks is cleanup,
                # not a failure. Never detach from a still-running child.
                if self.proc.poll() is None:
                    self.status.update(stage='waiting_for_exit', running=True)
                    return False
            if self.proc.poll() is None:
                self.status.update(stage='waiting_for_exit', running=True)
                return False
        if self.agent_session:
            try:self.agent_session.detach()
            except Exception:pass
            self.agent_session=None
        if self.handle:
            self.k.CloseHandle(self.handle);self.handle=None
        if self.iso:
            self.iso.close();self.iso=None
        if self.mutex:
            self.k.CloseHandle(self.mutex);self.mutex=None
        if self.proc:
            self.status.update(running=False,exit_code=self.proc.poll())
        return True

def selftest():
    import frida,psutil,tkinter
    script=render_script(Path.cwd()/'diagnostics'/'events.jsonl')
    if '__PF_LOCAL_LOG__' in script or 'local_auth_replacement_ready' not in script:
        raise LaunchError('E_SCRIPT_RESOURCE')
    if C.sizeof(C.c_void_p)!=8:
        raise LaunchError('E_ARCH')
    return {'launcher_version':VERSION,'target_version':PROFILE['version'],'stage':'selftest_passed',
            'error_code':None,'running':False,'script_errors':0}
