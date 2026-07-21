# OpenNet

> 完全由 [GLM-5.2](https://chatglm.cn) 构建的 Fiddler 式 HTTP/HTTPS 抓包代理工具。
> FastAPI 后端 + Vue 3 前端 + SQLite 存储，原生 Windows 支持，单命令启动。

---

- [OpenNet](#opennet)
  - [快速开始](#快速开始)
    - [环境要求](#环境要求)
    - [MCP配置\&使用](#mcp配置使用)
    - [Agent CLI调用](#agent-cli调用)
    - [一键安装](#一键安装)
    - [一键运行](#一键运行)
    - [TCP/UDP 抓包（需要管理员）](#tcpudp-抓包需要管理员)
  - [项目特色](#项目特色)
    - [1. 完全由 GLM-5.2 构建](#1-完全由-glm-52-构建)
    - [2. 强大的自动修改（拦截改包）](#2-强大的自动修改拦截改包)
    - [3. 解决痛点的 Clash 集成](#3-解决痛点的-clash-集成)
    - [4. AI 分析](#4-ai-分析)
    - [5. MCP + Agent CLI 赋能](#5-mcp--agent-cli-赋能)
      - [MCP Server（73 个工具）](#mcp-server73-个工具)
      - [Agent CLI](#agent-cli)
    - [6. 使用简单](#6-使用简单)
  - [项目结构](#项目结构)
  - [使用文档](#使用文档)
  - [性能优化](#性能优化)
  - [技术栈](#技术栈)
  - [License](#license)

## 快速开始

### 环境要求

| 项         | 要求                                                    |
| ---------- | ------------------------------------------------------- |
| 操作系统   | Windows 10 / 11（依赖 WinDivert，暂不支持 macOS/Linux） |
| Python     | 3.10+                                                   |
| Node.js    | 18+（仅构建前端需要，运行已构建产物不需要）             |
| 管理员权限 | TCP/UDP 原始抓包需要；HTTP/HTTPS 抓包不需要             |

### MCP配置&使用

详见 [README_MCP.md](README_MCP.md) 

### Agent CLI调用

按下面步骤安装依赖后让Agent阅读 [README_AI.md](README_AI.md) 即可

### 一键安装

```powershell
# 克隆仓库
git clone https://github.com/yyyyy114514/Opennet.git
cd Opennet

# 一键安装所有依赖（Python + Node）
.\install.ps1
```

或手动安装：

```powershell
# 后端依赖
cd src\host
pip install -e .

# 前端依赖（可选，仅需要改前端时安装）
cd ..\ui
npm install
```

### 一键运行

```powershell
# 构建前端（首次运行或前端有改动时需要）
.\build.ps1

# 启动 OpenNet（自动开代理 + 装证书 + 打开浏览器）
.\run.ps1

# 或不打开浏览器
.\run.ps1 --no-browser
```

或手动启动：

```powershell
cd src\host
python -m opennet
```

启动后访问 http://127.0.0.1:18901 即可使用。

### TCP/UDP 抓包（需要管理员）

TCP/UDP 原始抓包依赖 WinDivert，需要管理员权限。在 CLI 里执行：

```powershell
python -m opennet.cli system restart-as-admin
```

会弹 UAC 提权窗口，同意后后端以管理员身份重启。

------

## 项目特色

### 1. 完全由 GLM-5.2 构建

从架构设计到每一行代码、从前后端到 MCP Server，全部由智谱 GLM-5.2 编写，无任何人工手写代码。项目本身也是 AI 编程能力的一次实战展示。

### 2. 强大的自动修改（拦截改包）

不用写正则，不用懂 JSONPath，也能搞定复杂改包：

- **通配符匹配**：`*.example.com` / `*/api/v1/*` 一行搞定
- **JSON 字段级替换**：按 key 名递归替换所有同名字段，或用 JSONPath 精确定位
- **全局搜索替换**：字段路径不含 `.` 时自动递归遍历整个 JSON
- **规则优先级**：按 pattern 长度降序，更具体的规则优先
- **改请求 / 改响应 / Mock 响应 / Mock 请求**：四种动作自由组合
- **过滤条件**：按 method / status / pid / process 过滤，避免误命中
- **命中统计**：每条规则记录命中次数、最后命中时间、最后命中 flow id
- **断点 Preview 可编辑**：断点暂停时直接在 Preview 里改 JSON / XML / CSS / JS / Text

动作语法示例：

```
set-json status ok                    # 改 JSON 字段
set-json-path $.data.user.name "张三" # JSONPath 精确定位
remove-json debug_info                # 删字段
replace-header Content-Type application/xml
mock 200 {"ok":true}                  # Mock 整个响应
set-request-header Authorization "Bearer xxx"
delay 2000                            # 延迟 2 秒响应
```

### 3. 解决痛点的 Clash 集成

**痛点**：抓包时若走 Clash 代理，Clash 会劫持系统代理，OpenNet 抓不到；关掉 Clash 又连不上被墙的 API。

**OpenNet 的方案**：内置 Clash/Mihomo 上游代理集成。OpenNet 始终作为系统代理，出站连接可选走 Clash 的 mixed-port（默认 7890）。这样：

- 抓包与翻墙同时进行，互不干扰
- 一键在"直连 / 走 Clash"之间切换
- 自动读取 Clash 配置，无需手动改端口
- 支持 Clash Party / Clash Verge / Mihomo 等所有遵循 Clash 内核的客户端

设置教程见 [CLASH_SET.md](CLASH_SET.md)。

### 4. AI 分析

内置 DeepSeek AI（支持 deepseek-v4-flash / deepseek-v4-pro），可对抓到的流量进行智能分析：

- **单 flow 分析**：选中一条流量，AI 解析请求/响应，提取关键信息
- **批量分析**：多选 flow，AI 找共性和差异（适合逆向签名算法）
- **全局分析**：跨会话流量列表，AI 帮你从几百条流量里找出可疑的签名/token 字段
- **聊天上下文**：AI 记住之前的对话，可以追问"刚才那个登录请求的 token 在哪用到了"
- **隐私可控**：API Key 存本地，可在设置页随时开/关 AI 功能

### 5. MCP + Agent CLI 赋能

OpenNet 不只是 GUI 工具，还为 AI Agent 提供了完整的编程接口：

#### MCP Server（73 个工具）

把抓包、拦截、改包、重放能力暴露为 MCP (Model Context Protocol) 工具，让 Claude Desktop / Cursor / VS Code Continue 等 MCP 客户端直接调用。AI 可以：

- `packets_list` 拉流量、`packets_get` 看详情
- `intercept_add` 创建改包规则（支持 `--dry_run` 预览）
- `packets_diff` 对比两次请求差异
- `packets_trace` 追踪请求依赖链（从响应提取值，在后续请求里搜索）
- `packets_analyze` 自动检测签名字段（对比多条请求找可疑 token/sign）
- `replay_batch` 批量时序重放压测
- `agent_start` / `agent_end` 工作区隔离（AI 操作不影响用户现有配置）

详见 [README_MCP.md](README_MCP.md)。

#### Agent CLI

对 Agent 友好的 CLI 设计：

- **NDJSON 输出**：每行一个 JSON 对象，便于 Agent 解析
- **非交互模式**：所有命令支持 `--json` 参数，无彩色无提示
- **会话化操作**：`capture start` 自动创建会话，后续命令继承
- **声明式拦截规则**：一条命令创建复杂规则
- **`--dry-run` 预览**：先看会命中哪些流量，再决定是否真改
- **`--emit-curl`**：把流量转为 curl 命令，方便复制到别处调试

详见 [README_AI.md](README_AI.md)。

### 6. 使用简单

- **一键启动**：`python -m opennet` 自动开代理、装证书、启前端
- **HTTPS 开箱即用**：首次启动自动签发根证书并安装到系统信任存储
- **手机抓包向导**：扫码下载证书、自动计算安卓 7+ 系统证书哈希、教程链接
- **深色/浅色主题**：一键切换，CodeMirror 编辑器跟随主题
- **可视化规则编辑**：拖拽排序、批量启停、命中统计一目了然
- **专注模式**：只抓指定进程/host，过滤噪音
- **多选批量操作**：批量删除、批量放行断点、批量 AI 分析

---

## 项目结构

```
opennet/
├── src/
│   ├── host/                  # Python 后端
│   │   ├── opennet/
│   │   │   ├── api/           # FastAPI 路由（20+ 模块）
│   │   │   ├── proxy/         # 代理服务器核心
│   │   │   │   ├── server.py        # HTTP/HTTPS 抓包代理
│   │   │   │   ├── raw_capture.py   # TCP/UDP 原始抓包（WinDivert）
│   │   │   │   ├── ssl_bump.py      # SSL Bump 动态签发证书
│   │   │   │   ├── breakpoint.py    # 断点管理
│   │   │   │   └── process_lookup.py # PID 反查
│   │   │   ├── auto_reply/   # 自动修改规则引擎
│   │   │   ├── clash/         # Clash 集成
│   │   │   ├── ai/            # DeepSeek AI 分析
│   │   │   ├── cli.py         # Agent CLI
│   │   │   ├── mcp_server.py  # MCP Server（73 个工具）
│   │   │   ├── db.py          # SQLite 存储
│   │   │   └── __main__.py    # 入口
│   │   ├── pyproject.toml
│   │   └── requirements.txt
│   └── ui/                    # Vue 3 前端
│       ├── src/
│       │   ├── views/         # 页面（抓包/分析/设置等）
│       │   ├── components/    # 组件（FlowList/HexView/CodeEditor 等）
│       │   ├── stores/        # Pinia 状态管理
│       │   └── api/           # 后端 API 客户端
│       └── package.json
├── installer/                 # Inno Setup 安装包脚本
├── docs/                      # 文档截图
├── build.ps1                  # 一键构建脚本
├── install.ps1                # 一键安装依赖脚本
├── run.ps1                    # 一键运行脚本
└── README.md
```

---

## 使用文档

- [CLASH_SET.md](CLASH_SET.md) — Clash / Mihomo 集成设置教程
- [MOBILE_CAPTURE.md](MOBILE_CAPTURE.md) — 安卓手机抓包教程
- [README_AI.md](README_AI.md) — Agent CLI 完整用法（AI 友好的 NDJSON / 非交互模式）
- [README_MCP.md](README_MCP.md) — MCP Server 73 个工具清单与客户端接入配置

---

## 性能优化

OpenNet 在性能上做了大量优化，确保高并发场景下不卡顿：

- **后端线程池**：每个客户端连接独立线程，避免 asyncio 在 Windows 上的 IOCP 问题
- **连接复用**：keep-alive 连接池（默认 32 个 per key），避免重复 TCP+TLS 握手
- **SQLite WAL 模式 + threading.local 连接池**：每线程复用长连接，避免每次 connect/close
- **Body 截断记录**：大 body（视频/图片流）记录到 DB 时截断到 512KB，避免 base64 膨胀阻塞代理线程
- **前端异步高亮**：hljs 语法高亮用 `requestIdleCallback` 异步执行，避免大文本阻塞主线程
- **三阶段渲染**：纯转义（同步立即可见）→ hljs 高亮（异步）→ 搜索高亮（同步叠加），搜索时保留语法颜色
- **Vite 构建优化**：`minify: 'esbuild'`（terser 在 element-plus 大 bundle 上会卡死）、禁用 sourcemap、禁用 manualChunks

---

## 技术栈

**后端**：Python 3.10+ / FastAPI / Uvicorn / SQLite (WAL) / psutil / cryptography / WinDivert / pydivert

**前端**：Vue 3 / Vite 5 / Element Plus / Pinia / CodeMirror 6 / highlight.js / axios

**AI**：DeepSeek（deepseek-v4-flash / deepseek-v4-pro）

**集成**：MCP (Model Context Protocol) / Clash (Mihomo) / PyInstaller（打包 exe）/ Inno Setup（安装包）

---

## License

MIT