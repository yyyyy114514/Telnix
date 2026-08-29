# -*- mode: python ; coding: utf-8 -*-

# Telnix PyInstaller 打包配置（onedir 模式，性能优先）
# 用法：在 src\host 目录下执行 pyinstaller telnix_host.spec
# 输出：dist/telnix_host/telnix_host.exe + _internal/

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
        # telnix 包源码 → _internal/telnix/（launcher.py 用 runpy.run_module 启动）
        # 必须显式复制，否则 PyInstaller 只打包 .pyc 到 PYZ，runpy 找不到包
        ('telnix', 'telnix'),
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
        # telnix 包（launcher.py 用 runpy.run_module 启动，需要显式收集）
        'telnix',
        'telnix.__main__',
        'telnix.config',
        'telnix.db',
        'telnix.logger',
        'telnix.server',
        'telnix.cli',
        'telnix.mcp_server',
        'telnix.settings_store',
        'telnix.ai',
        'telnix.ai.deepseek',
        'telnix.api',
        'telnix.api.ai',
        'telnix.api.auth',
        'telnix.api.auto_reply',
        'telnix.api.breakpoint',
        'telnix.api.capture',
        'telnix.api.clash',
        'telnix.api.decode',
        'telnix.api.dns_hijack',
        'telnix.api.export',
        'telnix.api.focus',
        'telnix.api.groups',
        'telnix.api.import_flows',
        'telnix.api.logs',
        'telnix.api.processes',
        'telnix.api.raw',
        'telnix.api.replay',
        'telnix.api.search',
        'telnix.api.send',
        'telnix.api.sessions',
        'telnix.api.settings',
        'telnix.api.snapshot',
        'telnix.api.system',
        'telnix.api.tech_fingerprint',
        'telnix.api.templates',
        'telnix.api.throttle',
        'telnix.api.transparent_proxy',
        'telnix.auto_reply',
        'telnix.auto_reply.rules',
        'telnix.auto_reply.script_runner',
        'telnix.auto_reply.script_worker',
        'telnix.cert_info',
        'telnix.clash',
        'telnix.clash.client',
        'telnix.elevation',
        'telnix.ip_region',
        'telnix.proxy',
        'telnix.proxy.async_proxy',
        'telnix.proxy.breakpoint',
        'telnix.proxy.dns_hijack',
        'telnix.proxy.dns_hijack_local',
        'telnix.proxy.dns_parser',
        'telnix.proxy.h2_forward',
        'telnix.proxy.mitmproxy_engine',
        'telnix.proxy.process_lookup',
        'telnix.proxy.raw_capture',
        'telnix.proxy.raw_capture_unix',
        'telnix.proxy.server',
        'telnix.proxy.ssl_bump',
        'telnix.proxy.throttle',
        'telnix.proxy.transparent_proxy',
        'telnix.proxy.transparent_proxy_unix',
        'telnix.proxy.websocket_relay',
        'telnix.system_proxy',
        'telnix.tech_fingerprint',
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
    name='telnix_host',
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
    name='telnix_host',
)
