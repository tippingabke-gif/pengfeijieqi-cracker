"""Desktop and command-line entry points; no account, telemetry, or remote endpoint."""
from pathlib import Path
import argparse,ctypes,json,os,subprocess,sys,tempfile,threading,time,traceback
from runtime import LaunchError,Session,VERSION,public_status,resolve_source,selftest

MESSAGES={
 'E_FILE':'请选择 App 文件夹中的 PengfeiChess.exe。',
 'E_VERSION':'该文件不是本版本支持的鹏飞揭棋 5.9.1.0 主程序。',
 'E_ALREADY_RUNNING':'此工作副本已由另一个助手启动。请使用现有窗口。',
 'E_COPY_VERSION':'现有工作副本版本不匹配，请选择一个新的数据目录。',
 'E_BRIDGE_RESOURCE':'启动器缺少鹏飞揭棋引擎桥接组件。',
 'E_ENGINE_VERSION':'工作副本中的 ACE_JieQi_64.exe 版本不匹配。',
 'E_ENGINE_CONFIG':'引擎配置缺失、损坏或没有 ACE_JieQi_64.exe 条目。',
 'E_ENGINE_BACKUP':'引擎配置备份失败，已停止启动。',
 'E_DATA_PATH':'数据目录应位于原程序文件夹之外。',
 'E_LINK':'原程序目录包含链接，请使用完整解压的普通文件夹。',
 'E_SCRIPT':'运行脚本报告错误，已停止本次启动。',
 'E_RUNTIME_TIMEOUT':'等待主程序初始化超时。',
 'E_WINDOW_TIMEOUT':'启动修复已加载，但等待揭棋主窗口超时。',
 'E_SCRIPT_TIMEOUT':'等待启动修复就绪事件超时。',
 'E_SMOKE_EARLY_EXIT':'棋盘在启动检查结束前退出。',
 'E_PROCESS_CRASH':'棋盘异常退出，请查看本次运行记录。',
 'E_ISOLATION':'应用网络隔离未建立，已停止启动。',
}

def save_result(path,value):
    if path:
        p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
        data=json.dumps(public_status(value),ensure_ascii=False,indent=2)
        temporary=None
        try:
            fd,temporary=tempfile.mkstemp(prefix=f'.{p.name}.',suffix='.tmp',dir=str(p.parent))
            with os.fdopen(fd,'w',encoding='utf-8',newline='') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # The status file is also consumed by a lightweight monitor. A
            # reader may hold it briefly on Windows; status reporting must
            # never terminate the chess process in that case.
            for attempt in range(5):
                try:
                    os.replace(temporary,p)
                    temporary=None
                    break
                except PermissionError:
                    if attempt == 4:
                        return False
                    time.sleep(.05*(attempt+1))
        except OSError:
            return False
        finally:
            if temporary:
                try:os.unlink(temporary)
                except OSError:pass
        return True

def is_admin():
    return bool(ctypes.windll.shell32.IsUserAnAdmin())

def elevate():
    shell=ctypes.WinDLL('shell32',use_last_error=True)
    shell.ShellExecuteW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_int]
    shell.ShellExecuteW.restype=ctypes.c_void_p
    args=sys.argv[1:] if getattr(sys,'frozen',False) else [str(Path(__file__).resolve()),*sys.argv[1:]]
    result=shell.ShellExecuteW(None,'runas',sys.executable,subprocess.list2cmdline(args),str(Path.cwd()),0)
    if not result or result<=32:
        raise LaunchError('E_ELEVATION')

def error_result(exc,session=None):
    code=exc.code if isinstance(exc,LaunchError) else 'E_START'
    if session and session.run_dir:
        (session.run_dir/'launcher-error.txt').write_text(traceback.format_exc(),encoding='utf-8')
    result = session.snapshot() if session else {'launcher_version':VERSION,'running':False}
    result.update(stage='error',error_code=code)
    return result

def run_cli(args):
    session=Session(args.launch,args.data_dir)
    result=None
    smoke_passed=False
    return_code=0
    try:
        result=session.start();save_result(args.result,result)
        start=time.monotonic()
        while session.proc.poll() is None:
            result=session.snapshot();save_result(args.result,result)
            if result['script_errors']:raise LaunchError('E_SCRIPT')
            if args.smoke_seconds and time.monotonic()-start>=args.smoke_seconds:
                smoke_passed=True
                result['stage']='startup_smoke_passed'
                break
            time.sleep(.5)
        else:
            result=session.snapshot()
            if args.smoke_seconds:
                raise LaunchError('E_SMOKE_EARLY_EXIT')
            if result['exit_code'] != 0:
                raise LaunchError('E_PROCESS_CRASH')
        save_result(args.result,result)
    except Exception as exc:
        result=error_result(exc,session);return_code=1
        save_result(args.result,result)
    finally:
        notified=False
        while not session.close():
            waiting=session.snapshot()
            if result and result.get('error_code'):
                waiting['error_code']=result['error_code']
            save_result(args.result,waiting)
            if not notified and sys.stderr:
                print('请在棋盘窗口完成退出；助手会保持运行环境。',file=sys.stderr)
                notified=True
            time.sleep(.5)
        final=session.snapshot()
        if result and result.get('error_code'):
            final.update(stage='error',error_code=result['error_code'])
        elif smoke_passed:
            if final['exit_code'] == 0:
                final['stage']='startup_smoke_passed'
            else:
                final.update(stage='error',error_code='E_PROCESS_CRASH');return_code=1
        save_result(args.result,final)
    return return_code

def gui():
    import tkinter as tk
    from tkinter import filedialog,messagebox
    root=tk.Tk();root.title('鹏飞揭棋启动助手');root.geometry('580x230');root.resizable(False,False)
    selected=tk.StringVar(value=r'D:\PengfeiJieQi\PengfeiChess-5.9.1.0_ReleaseJieQi_20260929\App\PengfeiChess.exe');status=tk.StringVar(value='选择鹏飞揭棋 5.9.1.0 的主程序，然后启动。')
    state={'session':None,'busy':False,'closing':False}
    tk.Label(root,text='鹏飞揭棋 PengfeiChess 破解器',font=('Microsoft YaHei UI',14)).pack(pady=(18,10))
    row=tk.Frame(root);row.pack(fill='x',padx=18)
    tk.Entry(row,textvariable=selected).pack(side='left',fill='x',expand=True)
    def browse():
        path=filedialog.askopenfilename(title='选择 App 文件夹中的主程序',filetypes=[('鹏飞主程序','PengfeiChess.exe')])
        if path:selected.set(path)
    tk.Button(row,text='选择程序',command=browse).pack(side='right',padx=(8,0))
    tk.Label(root,textvariable=status,wraplength=540,justify='left').pack(fill='x',padx=18,pady=14)
    tk.Label(root,text='助手保持打开；设置和方案保存于本机工作副本。',fg='#555555').pack()
    def start_thread():
        s=None
        result=None
        try:
            s=Session(selected.get());state['session']=s
            if state['closing']:s.stop_requested.set()
            s.start()
            root.after(0,lambda:status.set('已进入揭棋主界面。助手需保持打开。'))
            while s.proc.poll() is None and not state['closing']:
                if s.snapshot()['script_errors']:raise LaunchError('E_SCRIPT')
                time.sleep(.5)
            if s.proc.poll() is not None and s.proc.returncode != 0:
                raise LaunchError('E_PROCESS_CRASH')
        except Exception as exc:result=error_result(exc,s)
        finally:
            if s:
                waiting_message=False
                while not s.close():
                    if not waiting_message:
                        root.after(0,lambda:status.set('请在棋盘窗口完成退出；助手会保持运行环境。'))
                        waiting_message=True
                    time.sleep(.5)
                if s.proc and s.proc.returncode != 0 and not result:
                    result=error_result(LaunchError('E_PROCESS_CRASH'),s)
            state.update(busy=False,session=None)
            if state['closing']:root.after(0,root.destroy)
            elif result:
                text=MESSAGES.get(result['error_code'],'启动失败，错误码：'+result['error_code'])
                root.after(0,lambda:status.set(text))
            else:root.after(0,lambda:status.set('棋盘已正常退出，可以再次启动。'))
    def start():
        if state['busy']:return
        if not selected.get():browse()
        if not selected.get():return
        state['busy']=True;status.set('正在准备工作副本和运行环境……')
        threading.Thread(target=start_thread,daemon=True).start()
    tk.Button(root,text='启动',command=start,width=15).pack(pady=8)
    def close():
        if state['busy'] and not messagebox.askyesno('退出助手','退出助手将请求棋盘正常关闭。若棋盘提示确认，请先在那里完成退出。继续吗？'):return
        state['closing']=True
        if state['session']:state['session'].stop_requested.set()
        if state['busy']:status.set('正在关闭本次棋盘并释放助手……')
        else:root.destroy()
    root.protocol('WM_DELETE_WINDOW',close);root.mainloop();return 0

def main():
    parser=argparse.ArgumentParser(description='PengfeiChess 5.9.1.0 local launcher')
    parser.add_argument('--version',action='version',version=VERSION)
    parser.add_argument('--selftest',action='store_true');parser.add_argument('--check')
    parser.add_argument('--launch');parser.add_argument('--data-dir');parser.add_argument('--result')
    parser.add_argument('--smoke-seconds',type=int,default=0)
    args=parser.parse_args()
    try:
        if args.selftest:
            save_result(args.result,selftest());return 0
        if args.check:
            resolve_source(args.check);save_result(args.result,{'launcher_version':VERSION,'stage':'file_check_passed'});return 0
        if not is_admin():
            elevate();return 0
        return run_cli(args) if args.launch else gui()
    except Exception as exc:
        result=error_result(exc);save_result(args.result,result)
        if not args.launch:
            ctypes.windll.user32.MessageBoxW(None,MESSAGES.get(result['error_code'],'错误码：'+result['error_code']),'鹏飞揭棋启动助手',0x10)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
