# 第三方组件声明

本项目代码使用 MIT。第三方组件的原有许可见 `licenses/`。鹏飞揭棋原程序、模型和引擎不随本发布分发，亦不受本项目 MIT 许可覆盖。

| 组件 | 版本 | 用途 / 上游源码 |
| --- | --- | --- |
| Python | 3.10.11 | 运行时；https://www.python.org/downloads/release/python-31011/ |
| Tcl/Tk | 8.6 | 图形界面；https://www.tcl.tk/software/tcltk/ |
| frida | 17.22.0 | https://pypi.org/project/frida/17.22.0/ |
| psutil | 7.2.2 | https://pypi.org/project/psutil/7.2.2/ |
| pyinstaller | 6.22.2 | https://pypi.org/project/pyinstaller/6.22.2/ |
| pyinstaller-hooks-contrib | 2026.7 | https://pypi.org/project/pyinstaller-hooks-contrib/2026.7/ |
| altgraph | 0.17.5 | https://pypi.org/project/altgraph/0.17.5/ |
| packaging | 25.0 | https://pypi.org/project/packaging/25.0/ |
| pefile | 2024.8.26 | https://pypi.org/project/pefile/2024.8.26/ |
| pywin32-ctypes | 0.2.3 | https://pypi.org/project/pywin32-ctypes/0.2.3/ |
| setuptools | 65.5.0 | https://pypi.org/project/setuptools/65.5.0/ |
| typing-extensions | 4.15.0 | https://pypi.org/project/typing-extensions/4.15.0/ |

Frida 使用 wxWindows Library Licence 3.1，含二进制分发例外；对应源码见 https://github.com/frida/frida/tree/17.22.0 和其版本对应的子模块。Python 与 Tcl/Tk 按其发行版许可证分发。psutil 为 BSD 许可。PyInstaller 构建工具及引导程序采用其 GPL 许可和打包分发例外，详见所附许可；其许可不改变本项目自编代码的 MIT 许可。

构建依赖列表不表示每个构建工具的完整代码均包含在 EXE 中。依赖未经本项目修改；上游源码、版权和子组件声明由相应项目维护。
