"""Early, process-local ACE experiment; only accepts the caller's Popen child.

Disk files stay unchanged. Signature checks gate every executable-memory edit.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import threading
import time

import frida
import psutil

EXPECTED = '065497ebe421e2337a2c5e83a948964c78e5e99b7058789843ac39d5d77edaea'
ENGINE = Path('work/jieqi/private/test_copy/App/Engine/ACE_JieQi_64.exe').resolve()
JS = r"""
const m=Process.getModuleByName('ACE_JieQi_64.exe');
if(m.size!==82485248)throw new Error('Unexpected ACE mapped image size: '+m.size);
const gates={0x3dcb15:'0f84bf180000',0x3dcb1e:'0f84b6180000',0x3dcbc8:'0f840c180000',0x3dcbd1:'0f8403180000'};
const authRva=0xa88dd;
const authExpected='5556574881ec40010000488dac248000000048c785b0000000feffffff8b4118';
const readhex=(p,n)=>Array.from(new Uint8Array(p.readByteArray(n))).map(x=>x.toString(16).padStart(2,'0')).join('');
let authDone=false, gatesDone=false;
const start=Date.now();
function poll(){
  try {
    const ap=m.base.add(authRva);
    if(!authDone && readhex(ap,32)===authExpected){
      const sleep=Process.getModuleByName('kernel32.dll').getExportByName('Sleep');
      const raw=[0xb9,0xff,0xff,0xff,0xff,0x48,0xb8];
      const value=sleep.toString().substring(2).padStart(16,'0');
      for(let i=14;i>=0;i-=2)raw.push(parseInt(value.substring(i,i+2),16));
      raw.push(0xff,0xe0);
      Memory.patchCode(ap,raw.length,p=>p.writeByteArray(raw));
      authDone=true;
      send({event:'auth_park_installed',rva:'0xa88dd',before:authExpected,after:readhex(ap,raw.length),sleep:sleep.toString(),ms:Date.now()-start});
    }
    if(!gatesDone && Object.entries(gates).every(([r,s])=>readhex(m.base.add(Number(r)),6)===s)){
      const before={};
      for(const [r,s] of Object.entries(gates)){
        before['0x'+Number(r).toString(16)]=s;
        Memory.patchCode(m.base.add(Number(r)),6,p=>p.writeByteArray([0x90,0x90,0x90,0x90,0x90,0x90]));
      }
      gatesDone=true;
      send({event:'gates_installed',before,ms:Date.now()-start});
    }
    if(authDone && gatesDone){clearInterval(timer);send({event:'ready',base:m.base.toString(),ms:Date.now()-start});}
    else if(Date.now()-start>8000){clearInterval(timer);send({event:'signature_timeout',authDone,gatesDone,authActual:readhex(ap,32)});}
  }catch(e){clearInterval(timer);send({event:'patch_error',message:String(e)});}
}
const timer=setInterval(poll,2);
poll();
rpc.exports={
  prepareSearch(threads){
    if(!authDone || !gatesDone)throw new Error('Patches are not ready');
    const p=m.base.add(0xeba6f0),before=p.readU32();p.writeU32(threads);
    const event={event:'quota_set',rva:'0xeba6f0',before,after:p.readU32()};send(event);return event;
  },
  status(){return {authDone,gatesDone,base:m.base.toString(),quota:m.base.add(0xeba6f0).readU32()};}
};
"""


class PatchHandle:
    def __init__(self, proc, engine_path, emit):
        if not isinstance(proc, subprocess.Popen):
            raise TypeError('Expected caller-owned subprocess.Popen')
        self.proc = proc
        self.engine_path = Path(engine_path).resolve()
        if Path(proc.args[0]).resolve() != self.engine_path:
            raise ValueError('Popen path differs from requested engine')
        if hashlib.sha256(self.engine_path.read_bytes()).hexdigest() != EXPECTED:
            raise ValueError('Engine SHA-256 mismatch')
        if proc.poll() is not None:
            raise RuntimeError('Child has already exited')
        live = psutil.Process(proc.pid)
        self.identity = dict(pid=proc.pid, ppid=live.ppid(),
                             create_time=live.create_time(), exe=live.exe())
        if self.identity['ppid'] != os.getpid():
            raise ValueError('Target is not a direct child of this launcher')
        if Path(self.identity['exe']).resolve() != self.engine_path:
            raise ValueError('Live process executable differs from requested engine')
        # Keep the Popen handle and compare identity on both sides of attach.
        # This rejects a stale Popen object even if its numeric PID was reused.
        self._check_identity()
        self.events = []
        self.ready = threading.Event()
        self.failed = threading.Event()
        self.emit = emit
        self.session = frida.attach(proc.pid)
        try:
            self._check_identity()
            self.script = self.session.create_script(JS)
            self.script.on('message', self._on_message)
            self.script.load()
        except Exception:
            self.session.detach()
            raise

    def _check_identity(self):
        if self.proc.poll() is not None:
            raise RuntimeError('Caller-owned child has exited')
        live = psutil.Process(self.proc.pid)
        if (live.ppid() != self.identity['ppid'] or
                live.create_time() != self.identity['create_time'] or
                Path(live.exe()).resolve() != self.engine_path):
            raise RuntimeError('Live process identity changed')

    def _on_message(self, msg, data):
        event = msg.get('payload', msg)
        self.events.append(event)
        self.emit(event)
        if event.get('event') == 'ready':
            self.ready.set()
        if msg.get('type') == 'error' or event.get('event') in ('signature_timeout', 'patch_error'):
            self.failed.set()

    def wait_ready(self, timeout=9):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.ready.wait(.02):
                return True
            if self.failed.is_set() or self.proc.poll() is not None:
                return False
        return False

    def prepare_search(self, threads=256):
        if not 1 <= threads <= 256:
            raise ValueError('threads must be in 1..256')
        self._check_identity()
        return self.script.exports_sync.prepare_search(threads)

    def close(self):
        # Detaching does not restore the child-local edits; the caller owns exit.
        try:
            self.script.unload()
        except Exception:
            pass
        try:
            self.session.detach()
        except Exception:
            pass


def apply_patch(proc, engine_path, emit=lambda event: None):
    return PatchHandle(proc, engine_path, emit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--seconds', type=float, default=15)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    events = []
    proc = subprocess.Popen([str(ENGINE)], cwd=ENGINE.parent, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    handle = None
    def record(direction, **kw):
        row = dict(t=time.monotonic()-started, direction=direction, **kw)
        events.append(row)
        return row
    def pump(name, fp):
        for raw in iter(fp.readline, b''):
            record(name, text=raw.decode('utf8', 'replace').rstrip('\r\n'))
    for name in ('stdout', 'stderr'):
        threading.Thread(target=pump, args=(name,getattr(proc,name)), daemon=True).start()
    def send(cmd):
        record('stdin', text=cmd)
        proc.stdin.write((cmd+'\n').encode())
        proc.stdin.flush()
    error = None
    try:
        handle = apply_patch(proc, ENGINE, lambda e: record('frida', event=e))
        send('uci'); send('isready')
        if not handle.wait_ready():
            raise RuntimeError('Early patch failed or timed out')
        deadline = time.monotonic()+4
        while time.monotonic()<deadline and not any(e.get('text')=='readyok' for e in events):
            time.sleep(.02)
        if not any(e.get('text')=='readyok' for e in events):
            raise RuntimeError('readyok timeout')
        handle.prepare_search(256)
        send('setoption name Threads value 1')
        send('position startpos'); send('go movetime 1500')
        deadline = time.monotonic()+a.seconds
        while time.monotonic()<deadline and proc.poll() is None:
            time.sleep(.05)
        record('observation', alive=proc.poll() is None,
               bestmoves=[e['text'] for e in events if e.get('text','').startswith('bestmove ')])
        if proc.poll() is None:
            send('stop'); send('quit')
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
    except Exception as exc:
        error = repr(exc)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
        if handle:
            handle.close()
    result = dict(pid=proc.pid, engine=str(ENGINE), elapsed=time.monotonic()-started,
                  returncode=proc.returncode, error=error, events=events)
    (a.out/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({k:v for k,v in result.items() if k!='events'}, ensure_ascii=False))
    for e in events:
        if e['direction'] in ('frida','observation') or e.get('text','').startswith(('bestmove','thread ')):
            print(json.dumps(e,ensure_ascii=False))


if __name__ == '__main__':
    main()
