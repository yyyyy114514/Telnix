# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Telnix 是一个 HTTP/HTTPS 抓包代理工具（类似 Fiddler/Charles），支持 SSL bump 解密、自动拦截改包、断点、重放、AI 分析。完全由 AI 构建，提供 MCP Server（88个工具）和 Agent CLI 接口。

## 常用命令

### 安装与运行

```powershell
# 一键安装依赖
.\install.ps1

# 构建前端（首次运行或前端有改动时需要）
.\build.ps1

# 启动 Telnix（自动开代理 + 装证书 + 打开浏览器）
.\run.ps1

# 手动启动（agent 场景不用 --no-browser）
cd src/host
python -m telnix --no-browser
```

### 前端开发

```bash
cd src/ui
npm run dev          # 开发调试（热更新）
npm run typecheck    # 类型检查
npm run build        # 构建（不含类型检查）
npm run build:strict # 类型检查 + 构建
```

### Agent CLI

```bash
# 所有命令前缀
python -m telnix.cli <subcommand>

# 核心命令
python -m telnix.cli status                    # 后端状态
python -m telnix.cli capture start             # 开始抓包
python -m telnix.cli packets list              # 流量列表（NDJSON）
python -m telnix.cli packets get <id>          # 流量详情
python -m telnix.cli intercept add --match '...' --action '...'  # 添加拦截规则
python -m telnix.cli replay <id>               # 重放流量
python -m telnix.cli export --format har -o file.har  # 导出会话
```

### MCP Server

```bash
# 验证启动
python -m telnix.mcp_server

# 配置 Claude Desktop：编辑 %APPDATA%\Claude\claude_desktop_config.json
# {
#   "mcpServers": {
#     "Telnix": {
#       "command": "python",
#       "args": ["-m", "telnix.mcp_server"],
#       "cwd": ".\\src\\host",
#       "env": {"TELNIX_API": "http://127.0.0.1:18901"}
#     }
#   }
# }
```

### 前端构建

```bash
cd src/ui
npm install
npm run build        # 构建（不含类型检查）
npm run typecheck    # 类型检查
npm run build:strict # 类型检查 + 构建
```

## 架构

### 后端核心模块

```
src/host/telnix/
├── __main__.py           # 入口：启动代理 + FastAPI + 设置系统代理
├── cli.py                # Agent CLI（NDJSON 输出，--emit-curl 等 agent 友好设计）
├── mcp_server.py         # MCP Server（88个工具，FastMCP 实现）
├── server.py             # FastAPI 应用工厂
├── db.py                 # SQLite WAL 模式存储
├── config.py             # 端口/路径配置（DEFAULT_PORT=18901, PROXY_PORT=8888）
├── settings_store.py     # 用户设置（settings.json，替代 SQLite 偏好）
├── system_proxy.py       # 跨平台系统代理配置
├── elevation.py          # 跨平台提权（UAC/osascript/pkexec）
│
├── api/                  # FastAPI 路由
│   ├── capture.py        # 抓包控制（start/stop/pause/resume）
│   ├── flows.py          # 流量 CRUD（list/get/delete/search）
│   ├── auto_reply.py     # 拦截规则 CRUD
│   ├── replay.py         # 重放
│   ├── send.py           # 从零发包（Composer）
│   ├── export.py         # 导出 HAR/curl/python-requests
│   ├── sessions.py       # 会话管理
│   ├── settings.py       # 设置读写
│   ├── system.py         # 重启/退出/管理员重启
│   └── raw.py            # TCP/UDP 原始抓包
│
├── proxy/                # 代理服务器核心
│   ├── server.py         # 内置线程代理（默认，稳定）
│   ├── async_proxy.py    # asyncio 代理引擎（实验性）
│   ├── async_engine_v2.py # 高性能全异步引擎
│   ├── mitmproxy_engine.py # mitmproxy 引擎（可选 ~50MB）
│   ├── ssl_bump.py       # SSL Bump 动态签发证书
│   ├── breakpoint.py     # 断点管理
│   ├── raw_capture.py    # TCP/UDP 原始抓包（WinDivert，Windows）
│   ├── raw_capture_unix.py # TCP/UDP 原始抓包（AF_PACKET/BPF，macOS/Linux）
│   ├── transparent_proxy.py  # 透明代理（WinDivert NAT，Windows）
│   ├── transparent_proxy_unix.py # 透明代理（iptables/pf，macOS/Linux）
│   ├── dns_hijack.py     # DNS 劫持（WinDivert，Windows）
│   ├── dns_hijack_local.py # DNS 劫持（本地 DNS + iptables/pf，macOS/Linux）
│   └── process_lookup.py # PID 反查
│
├── auto_reply/           # 自动修改规则引擎
│   ├── rules.py          # 规则匹配与修改逻辑
│   ├── script_runner.py  # Python 脚本规则执行
│   └── script_worker.py  # 独立 worker 子进程
│
└── ai/
    └── deepseek.py       # Claude AI 分析集成
```

### 前端核心模块

```
src/ui/src/
├── views/                # 页面（CaptureView/AnalyzeView/SettingsView 等）
├── components/           # 组件
│   ├── FlowList.vue      # 流量列表（虚拟滚动）
│   ├── HexView.vue       # Hex 查看器
│   └── CodeEditor.vue    # CodeMirror 编辑器
├── stores/               # Pinia 状态管理
└── api/                  # 后端 API 客户端
```

### 平台差异

| 功能 | Windows | macOS | Linux |
|------|---------|-------|-------|
| TCP/UDP 抓包 | WinDivert 内核驱动 | BPF 设备 | AF_PACKET |
| 透明代理 | WinDivert NAT | pf rdr | iptables |
| DNS 劫持 | WinDivert | pf + 本地 DNS | iptables + 本地 DNS |
| 系统代理 | winreg | networksetup | gsettings/kwriteconfig5 |
| 管理员提权 | UAC | osascript | pkexec/sudo |

### 数据存储

- `~/.telnix/` — 数据目录
  - `telnix.db` — SQLite 数据库（会话、流量、规则）
  - `settings.json` — 用户偏好设置（原子写入 + 线程锁）
  - `certs/` — 根证书（telnix_root.crt / telnix_root.pfx）

### 代理引擎选择

通过 `settings.json` 的 `proxy_engine` 配置：
- `builtin`（默认）：内置线程代理，零依赖，稳定
- `async`：asyncio 代理引擎，实验性
- `v2`：高性能全异步引擎（uvloop/winloop）
- `mitmproxy`：可选依赖，需先 `pip install mitmproxy`

## 匹配表达式语法（--match）

```
key op value && key op value && ...
```
- key: `host` `method` `path` `url` `status` `pid` `process`
- op: `=`（精确）`~=`（通配符）`!=` `>=` `<=` `>` `<`

示例：`host~=api.example.com && method=POST && path~=/api/v1/*`

## 动作规范语法（--action）

### 改响应
- `set-json key value` — 改 JSON 字段
- `set-json-path path value` — JSONPath 精确定位
- `remove-json key` — 删字段
- `replace-header K V` — 替换响应头
- `mock CODE BODY` — Mock 整个响应
- `delay N` — 延迟 N 毫秒

### 改请求
- `set-request-header K V` — 改请求头
- `set-request-json key value` — 改请求 JSON
- `mock-request BODY` — 写死请求 body，转发到服务器

### Python 脚本
- `script 'def on_request(ctx): ... return None'` — 内联脚本
- `script file <path>` — 从文件加载

## 端口说明

- API 端口：**18901**（注意：18899/18900 在部分 Windows 机器被动态端口保留）
- 代理端口：8888
- CLI 默认连 18901，与 `config.DEFAULT_PORT` 保持同步

## WinDivert 风险提示（仅 Windows）

首次启用 TCP/UDP 抓包、透明代理、DNS 劫持时会弹风险确认窗口。已确认后不再提示。重置：`settings set -k windivert_warning_acknowledged -v 0`
