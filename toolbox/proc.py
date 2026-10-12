"""子进程工具：以「当前解释器」跑 toolbox 子模块的命令行构造。

源码形态：[python, -m, toolbox.xxx, ...]
冻结形态（PyInstaller exe）：[exe, toolbox.xxx, ...]——exe 不认 -m，
由 launch_pet.py 入口把 `toolbox._*` 参数转交 runpy 执行；workers 的
`if __name__ == "__main__"` 块两种形态通用。

NO_WINDOW：GUI 进程（exe / pythonw）spawn 控制台程序（ffmpeg/ffprobe/
taskkill/ssh…）时 Windows 会给子进程新开控制台窗口——跑链切一片闪一下
黑窗。所有后台子进程统一带 CREATE_NO_WINDOW 抑制；管道输出不受影响
（该标志只关控制台分配，不碰句柄继承）。
"""
import subprocess
import sys

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def module_cmd(module, *args):
    return ([sys.executable, module, *[str(a) for a in args]]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", module, *[str(a) for a in args]])
