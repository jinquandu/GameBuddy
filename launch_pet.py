"""PyInstaller 入口：双击（无参数）= 打开桌宠；带参数 = toolbox CLI 直通。

    DashijiePet.exe                    -> python -m toolbox pet
    DashijiePet.exe pipeline <地址> …  -> python -m toolbox pipeline <地址> …
    DashijiePet.exe record 123 --once  -> python -m toolbox record 123 --once
"""
import sys

from toolbox.cli import main

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("pet")
    main()
