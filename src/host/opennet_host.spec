# -*- mode: python ; coding: utf-8 -*-

# OpenNet PyInstaller 打包配置（onedir 模式，性能优先）
# 用法：在 src\host 目录下执行 pyinstaller opennet_host.spec
# 输出：dist/opennet_host/opennet_host.exe + _internal/

a = Analysis(
    ['launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        # 前端构建产物 → _internal/ui/dist/（匹配 config.get_ui_dist_dir()）
        ('../ui/dist', 'ui/dist'),
        # 文档（Clash 教程图片等）→ _internal/docs/（匹配 config.get_docs_dir()）
        ('../../docs', 'docs'),
        # Clash 教程 markdown → _internal/（匹配 config.get_tutorial_md_path()）
        ('../../CLASH_SET.md', '.'),
        # 手机抓包教程 → _internal/
        ('../../MOBILE_CAPTURE.md', '.'),
        # opennet 包源码 → _internal/opennet/（launcher.py 用 runpy.run_module 启动）
        # 必须显式复制，否则 PyInstaller 只打包 .pyc 到 PYZ，runpy 找不到包
        ('opennet', 'opennet'),
    ],
    hiddenimports=[
        # FastAPI / Uvicorn 隐式依赖
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.http.auto',
        'uvicorn.loops.auto',
        # cryptography 后端
        'cryptography.hazmat.bindings._rust',
        # opennet 包（launcher.py 用 runpy.run_module 启动，需要显式收集）
        'opennet',
        'opennet.__main__',
        'opennet.config',
        'opennet.db',
        'opennet.logger',
        'opennet.server',
        'opennet.cli',
        'opennet.mcp_server',
        'opennet.settings_store',
        'opennet.ai',
        'opennet.ai.deepseek',
        'opennet.api',
        'opennet.api.ai',
        'opennet.api.auto_reply',
        'opennet.api.breakpoint',
        'opennet.api.capture',
        'opennet.api.clash',
        'opennet.api.export',
        'opennet.api.focus',
        'opennet.api.groups',
        'opennet.api.import_flows',
        'opennet.api.logs',
        'opennet.api.processes',
        'opennet.api.raw',
        'opennet.api.replay',
        'opennet.api.search',
        'opennet.api.send',
        'opennet.api.sessions',
        'opennet.api.settings',
        'opennet.api.snapshot',
        'opennet.api.system',
        'opennet.api.templates',
        'opennet.api.throttle',
        'opennet.auto_reply',
        'opennet.auto_reply.rules',
        'opennet.clash',
        'opennet.clash.client',
        'opennet.proxy',
        'opennet.proxy.breakpoint',
        'opennet.proxy.dns_parser',
        'opennet.proxy.process_lookup',
        'opennet.proxy.raw_capture',
        'opennet.proxy.server',
        'opennet.proxy.ssl_bump',
        'opennet.proxy.throttle',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除不需要的大模块（减小体积）
        'tkinter',
        'unittest',
        'pydoc',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='opennet_host',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # 保留控制台窗口（抓包工具需要看日志）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        # UPX 压缩这些 DLL 会导致问题
        'python3.dll',
        'VCRUNTIME140.dll',
        'VCRUNTIME140_1.dll',
    ],
    name='opennet_host',
)
