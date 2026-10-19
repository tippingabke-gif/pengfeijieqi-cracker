# 鹏飞揭棋启动助手源码

启动器版本 `1.2.0-jieqi`，引擎桥接版本 `1.2.1`。源码包保留构建时目录结构，包含启动器、桥接、ACE 运行时适配器、Windows 进程生命周期组件与许可证；不包含商业主程序、ACE 二进制、个人配置或运行日志。

1.2.0 整合 A-天天揭棋固定方案的拟合配置、数字标签适配、真实几何矩形保留和分类分数恢复。分数修正仅在原字段非有限、19项原始输出全有限且原生类别与 argmax 一致时执行，恢复该类别的真实原始分数；棋子阈值和将帅校验保持原样。

桥接的 `fen_compat.py` 处理 GUI 与 ACE 的揭棋局面格式差异：在已定义的暗子起始格把 `X/x` 转成 ACE 对应暗子编码，将 GUI 原有剩余棋子库存原样放入 `$` 段并调整字段位置；已揭子、行棋方、计数器及后续 `moves` 保持相应含义。原生 ACE 格式和其他协议命令直接保留。模块对不完整或不确定的局面记录明确错误，不推测库存。

桥接日志分别记录 `bridge_ready`、`protocol_ready` 和搜索输出。前两者用于启动和协议状态，搜索是否成功必须另看真实 `info`、`bestmove` 与 GUI 局面处理证据；源码自检不构成搜索验收。

在 Windows x64 使用 Python 3.10 x64。先安装固定构建依赖，再从本目录运行构建脚本：

```powershell
python -m pip install -r work/jieqi/candidate/requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Python python
```

依赖为 `frida==17.22.0`、`psutil==7.2.2`、`pyinstaller==6.22.2`，Tkinter 随官方 Python 安装。构建脚本依次运行启动器单元测试、桥接父进程退出测试、构建桥接、桥接资源自检、构建启动器和冻结 EXE 自检；这些自检不启动棋盘。

产物位于 `work/engine_bridge/dist/PengfeiJieQi-EngineBridge.exe` 和 `work/jieqi/dist/PengfeiJieQi-Launcher.exe`。构建完成后可运行源码界面：`python work/jieqi/candidate/launcher.py`。目标选择原软件的 `App\PengfeiChess.exe`。

桥接父进程退出集成测试可单独运行：`python work/engine_bridge/test_parent_guard_integration.py`。它只创建并清理自身 Python 子进程，覆盖正常与强制父进程退出；不启动商业主程序或 ACE。

`work/engine_bridge/test_fen_bridge_replay.py` 是另一个需要真实 ACE 的可选测试，默认构建脚本不执行它。它预期完整私有副本中的引擎位于 `work/jieqi/private/test_copy/App/Engine/ACE_JieQi_64.exe`，并校验精确 SHA-256。它重放捕获的 GUI 开局 FEN、实际启动 ACE 并检查 `info`/`bestmove`；WFP 与真实 GUI 不在这个测试范围内。调用方式为 `python work/engine_bridge/test_fen_bridge_replay.py --out work/replay.json --log work/replay.jsonl`。

每个源文件的 SHA-256 见 `SOURCE_MANIFEST.json`。重建 EXE 的哈希受 Python、Windows 和打包元数据影响，不要求与分发 EXE 字节相同。
