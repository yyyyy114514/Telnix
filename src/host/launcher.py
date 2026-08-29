"""Telnix PyInstaller 打包入口。

PyInstaller 直接执行脚本时不在包上下文里，telnix/__main__.py 的相对 import
（from .config import ...）会失败。这个 launcher 用 runpy 以模块方式启动 telnix，
模拟 python -m telnix 的行为。

打包模式额外支持：telnix_host.exe <script.py> [args...]
当 argv[1] 是 .py 文件时，launcher 切换为直接执行该脚本（用 runpy.run_path）。
这用于 script_runner.py 在打包后启动 script_worker 子进程——
PyInstaller bootloader 不识别 `python -m` 选项，故 script_runner 改用
`exe <script_worker.py> <user_script.py>` 方式调用，由本 launcher 路由到 runpy.run_path。
"""
import runpy
import sys

if __name__ == "__main__":
    # 把 src\host 加入 sys.path（打包后 _internal/ 已在 path，但保险起见）
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)

    # 打包模式：argv[1] 是 .py 文件时，直接执行该脚本（用于子进程启动 script_worker）
    # 开发模式下 argv[0] 是 launcher.py 自身，不会触发此分支
    if (getattr(sys, "frozen", False)
            and len(sys.argv) >= 2
            and sys.argv[1].endswith(".py")
            and os.path.isfile(sys.argv[1])):
        script_path = sys.argv[1]
        # 调整 sys.argv：去掉 exe 自身，把 .py 路径作为 argv[0]，剩余作为脚本参数
        # runpy.run_path(alter_sys=True) 会设置 argv[0]=script_path，但 argv[1:] 需提前调整
        sys.argv = [script_path] + sys.argv[2:]
        runpy.run_path(script_path, run_name="__main__", alter_sys=True)
    else:
        # 以模块方式运行 telnix（等价于 python -m telnix）
        runpy.run_module("telnix", run_name="__main__", alter_sys=True)
