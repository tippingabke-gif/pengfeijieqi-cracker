# 鹏飞揭棋 PengfeiChess 破解器

适配 **PengfeiChess 5.9.1.0 揭棋版（20260929）** 的 Windows x64 本地启动助手，提供引擎桥接及 A-天天揭棋固定方案的识别、同步修正。

**当前版本：1.2.0。** [下载发行版](https://github.com/tippingabke-gif/pengfeijieqi-cracker/releases/tag/v1.2.0) · [修复原理与验证](docs/REPAIR.md) · [源码构建](docs/BUILD.md)

## 使用

1. 从 Release 下载 `PengfeiJieQi-Cracker-v1.2.0-Windows-x64.zip` 并解压，或直接下载同版本 EXE。
2. 运行启动器，按提示允许 Windows 管理员权限请求。
3. 选择原程序目录中的 `App/PengfeiChess.exe`，点击启动；外层 `鹏飞揭棋.exe` 是 AppLoader。
4. 保持助手打开。在棋盘内选择 A-天天揭棋固定方案并连接。

助手建立并复用独立工作副本，设置和方案保存在该副本中。每次启动加载运行时修正；原安装目录的 EXE 保持原样。引擎桥接组件随助手提供，原软件和 ACE 引擎需由使用者提供。

## 界面

![鹏飞揭棋主界面](https://raw.githubusercontent.com/tippingabke-gif/pengfeijieqi-cracker/377c07ab3b191d91277a3c71d6f20d10bf65fc00/docs/screenshots/board.png)

以上是已有的真实主界面运行截图，用于展示界面；本次连线验收依据单独记录在验证文档中。

## 1.2.0 更新

- 整合原生拟合缓存初始化及几何矩形保留。
- 将整盘检测模型的数字标签转换为消费端要求的棋子符号。
- 修正逐格分类分数往返后成为负无穷的问题：仅在原始19项输出有限、类别与 argmax 一致时恢复真实分数。
- 保留原判定阈值、空格判定及将帅校验。

2026-10-19 的 A-天天揭棋实测已确认棋盘开始同步。40条抽样校验跨越100.274秒，覆盖4种棋盘内容，均得到9×10、32枚棋子及双方各一枚将帅。269条真实输出回放、13项边界检查、7项启动器测试和整合脚本干净启动检查通过。

该验收覆盖本次识别和同步。其他固定方案、长时间完整对局以及冻结EXE自身的手动连线尚未单独验收。后续还记录了一次目标棋盘父窗口变化提示，见[验证说明](docs/REPAIR.md)。

## 构建

Windows x64、Python 3.10 x64：

```powershell
python -m pip install -r work/jieqi/candidate/requirements-build.txt
powershell -ExecutionPolicy Bypass -File .\build.ps1 -Python python
```

构建产物：`work/jieqi/dist/PengfeiJieQi-Launcher.exe`。依赖、目录结构和测试范围见[构建说明](docs/BUILD.md)。

## 版本与许可

主程序 SHA-256：`c01a7997ae5870f5b8dcbbb437b18544ea71f79c38e2d69cceeb1663db19d8ba`。

ACE SHA-256：`065497ebe421e2337a2c5e83a948964c78e5e99b7058789843ac39d5d77edaea`。

本项目代码使用 [MIT](LICENSE) 许可；第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
