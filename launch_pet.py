"""PyInstaller 入口：双击（无参数）= 打开桌宠；带参数按首参分流。

    DashijiePet.exe                    -> python -m toolbox pet
    DashijiePet.exe pipeline <地址> …  -> python -m toolbox pipeline <地址> …
    DashijiePet.exe toolbox._scan_worker …   -> python -m toolbox._scan_worker …
      （detector/pickup 的并行 worker：proc.module_cmd 在冻结形态下
       以「exe + 模块名」spawn，这里转交 runpy 执行同一份冻结代码）
"""
import runpy
import sys

from toolbox.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("pet")
    if sys.argv[1].startswith("toolbox._"):       # 内部 worker 模块直通
        # 对齐 python -m 语义：模块名挪进 argv[0]，其余参数原样右移，
        # 否则 worker 看到的参数整体错一位（runpy 不会自己剥模块名）
        sys.argv = sys.argv[1:]
        runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)
    else:
        main()
