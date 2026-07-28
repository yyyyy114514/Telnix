"""Telnix PyInstaller 打包入口。

PyInstaller 直接执行脚本时不在包上下文里，telnix/__main__.py 的相对 import
（from .config import ...）会失败。这个 launcher 用 runpy 以模块方式启动 telnix，
模拟 python -m telnix 的行为。
"""
import runpy
import sys

if __name__ == "__main__":
    # 把 src\host 加入 sys.path（打包后 _internal/ 已在 path，但保险起见）
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    # 以模块方式运行 telnix（等价于 python -m telnix）
    runpy.run_module("telnix", run_name="__main__", alter_sys=True)
