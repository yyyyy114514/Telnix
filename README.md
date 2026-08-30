# Telnix

> bug很多，暂时没时间做了，先上传。main是glm5.2，dev是fable5，烧了快2亿token了

[快速开始](#快速开始)

[MCP配置\&使用](#mcp配置使用)

[Agent CLI调用](#agent-cli调用)

- [平台支持矩阵](#平台支持矩阵)
- [项目特色](#项目特色)
  - [1. 完全由 GLM-5.2 构建](#1-完全由-glm-52-构建)
  - [2. 强大的自动修改（拦截改包）](#2-强大的自动修改拦截改包)
  - [3. 解决痛点的 Clash 集成](#3-解决痛点的-clash-集成)
  - [4. AI 分析](#4-ai-分析)
  - [5. MCP + Agent CLI 赋能](#5-mcp--agent-cli-赋能)
    - [MCP Server（88 个工具）](#mcp-server88-个工具)
    - [Agent CLI](#agent-cli)
  - [6. 使用简单](#6-使用简单)
- [项目结构](#项目结构)
- [使用文档](#使用文档)
- [性能优化](#性能优化)
- [技术栈](#技术栈)
- [License](#license)

## 快速开始

### 环境要求

| 项         | 要求                                                                  |
| ---------- | --------------------------------------------------------------------- |
| 操作系统   | Windows 10 / 11 / macOS / Linux（全平台完整支持，网络层功能各平台使用不同后端：WinDivert / AF_PACKET+BPF / iptables+pf） |
| Python     | 3.10+                                                                 |
| Node.js    | 18+（仅构建前端需要，运行已构建产物不需要）                           |
| 管理员权限 | 网络层抓包（TCP/UDP 抓包、透明代理、DNS 劫持）需要管理员/root 权限（Windows UAC / macOS osascript / Linux pkexec 或 sudo 自动提权）；HTTP/HTTPS 抓包不需要 |

### MCP配置&使用

详见 [README_MCP.md](README_MCP.md) 

### Agent CLI调用

按下面步骤安装依赖后让Agent阅读 [README_AI.md](README_AI.md) 即可

### 一键安装

```powershell
# 克隆仓库
git clone https://github.com/yyyyy114514/Telnix.git
cd Telnix

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

macOS / Linux 用户可使用对应的依赖安装脚本：

```bash
./scripts/install-deps-linux.sh   # Linux
./scripts/install-deps-mac.sh     # macOS
```

### 一键运行

```powershell
# 构建前端（首次运行或前端有改动时需要）
.\build.ps1

# 启动 Telnix（自动开代理 + 装证书 + 打开浏览器）
.\run.ps1

# 或不打开浏览器
.\run.ps1 --no-browser
```

或手动启动：

```powershell
cd src\host
python -m telnix
```

启动后访问 http://127.0.0.1:18901 即可使用。

### macOS / Linux 安装与运行

macOS / Linux 用户使用项目根目录的 bash 脚本，等价于 Windows 的 `install.ps1` / `build.ps1` / `run.ps1`。

#### 一、安装依赖

```bash
# 方式 A：一键脚本（推荐）
./scripts/install-deps-linux.sh   # Linux
./scripts/install-deps-mac.sh     # macOS

# 方式 B：手动安装
cd src/host
pip install -e .                  # 后端依赖（pydivert 等 Windows 专属包会自动跳过）
# 前端依赖（可选，仅修改前端时需要）
cd ../ui
npm install
```

> 说明：`pydivert` 是 Windows 专属包，pip 在非 Windows 上会自动跳过安装。macOS/Linux 使用原生内核接口（AF_PACKET / BPF / iptables / pf）替代，TCP/UDP 抓包、透明代理、DNS 劫持均可正常使用。

#### 二、构建前端

```bash
# 构建前端到 src/ui/dist（首次运行或前端改动后需要）
./build.sh
```

`build.sh` 与 `build.ps1` 行为等价：检测 python3/npm → 关闭占用 18901/8888 端口的残留进程 → 清理 dist + Vite 缓存 → 可选类型检查（非阻塞）→ 运行 `npm run build`。macOS / Linux 无需设置 `UV_THREADPOOL_SIZE=1` / `GOMAXPROCS=1`（这是 Windows 上 esbuild Go runtime 死锁的 workaround）。

> **构建脚本说明**：
> - `npm run build` — 仅执行 `vite build`（不包含类型检查，保证构建成功）
> - `npm run typecheck` — 单独运行 `vue-tsc --noEmit` 类型检查
> - `npm run build:strict` — 类型检查 + 构建（类型错误时阻止构建）
> - `.\build.ps1` / `./build.sh` — 完整构建流程（含非阻塞类型检查）

#### 三、启动 Telnix

```bash
# 默认（自动开浏览器）
./start.sh

# 不开浏览器（agent / 自动化场景）
./start.sh --no-browser

# 自定义端口
./start.sh --port 18902

# 或手动启动
cd src/host
python3 -m telnix
```

启动后访问 http://127.0.0.1:18901。

#### 四、系统代理配置（macOS / Linux）

**说明**：macOS / Linux 上 Telnix **会自动设置系统代理**（macOS 用 `networksetup`，Linux GNOME 用 `gsettings`，KDE 用 `kwriteconfig5`）。如自动配置未生效或使用非 GNOME/KDE 桌面，可手动将浏览器/系统代理配置为 `127.0.0.1:8888`：

- **macOS**：系统偏好设置 → 网络 → 高级 → 代理 → 网页代理(HTTP) / 安全网页代理(HTTPS) → 填入 `127.0.0.1:8888`
- **Linux (GNOME)**：设置 → 网络 → 网络代理 → 手动 → HTTP/HTTPS 代理填 `127.0.0.1` 端口 `8888`
- **Linux (KDE)**：系统设置 → 网络 → 代理 → 手动配置
- **命令行**：`export http_proxy=http://127.0.0.1:8888 https_proxy=http://127.0.0.1:8888`
- **浏览器单独配置**：Firefox 偏好设置 → 网络设置 → 手动代理；Chrome 默认跟随系统代理

#### 五、安装根证书（解密 HTTPS 必备）

- **macOS**：Telnix 启动后用 `security add-trusted-cert` 安装根证书到系统钥匙串，安装时会弹窗要求输入密码授权（首次启用 HTTPS 抓包时自动触发，也可在 GUI 设置页点「安装根证书」按钮）
- **Linux**：手动安装（不同发行版命令不同）：
  ```bash
  # Debian / Ubuntu
  sudo cp <data_dir>/certs/telnix_root.crt /usr/local/share/ca-certificates/telnix_root.crt
  sudo update-ca-certificates
  # RHEL / CentOS
  sudo trust anchor <data_dir>/certs/telnix_root.crt
  ```
  `<data_dir>` 默认是 `~/.telnix`，可用 `python3 -c "from telnix.config import get_cert_dir; print(get_cert_dir())"` 查询

### TCP/UDP 抓包（需要管理员）

TCP/UDP 原始抓包依赖 WinDivert，需要管理员权限。在 CLI 里执行：

```powershell
python -m telnix.cli system restart-as-admin
```

会弹提权窗口（Windows UAC / macOS 系统密码 / Linux pkexec），同意后后端以管理员身份重启。

> **平台说明**：
> - **Windows**：使用 WinDivert 内核驱动，`system restart-as-admin` 通过 UAC 提权
> - **macOS**：使用 BPF 设备（`/dev/bpfN`）抓包，`system restart-as-admin` 通过 osascript 提权（系统密码弹窗）
> - **Linux**：使用 AF_PACKET（`SOCK_RAW`）抓包，`system restart-as-admin` 通过 pkexec（PolicyKit GUI）或 sudo 提权
> - 各平台后端不同但接口一致，`raw capture start` / `transparent-proxy start` / `dns-hijack start` 等命令全平台可用

### WinDivert 风险提示（仅 Windows，首次启用时弹窗）

> 此提示仅适用于 **Windows 平台**。macOS / Linux 使用原生内核接口（AF_PACKET / BPF / iptables / pf），无第三方驱动，不存在此风险。

WinDivert 是 Windows 内核驱动，Telnix 用它做 TCP/UDP 抓包、透明代理、DNS 劫持。**该驱动常被漏洞利用工具使用**，部分杀毒软件（360 / 火绒 / Windows Defender 等）可能将其作为"漏洞驱动"拦截或报警，导致功能无法启动。

> Telnix 仅将该驱动用于抓包 / 透明代理 / DNS 劫持，**不会对您的设备带来任何安全隐患**。若驱动加载被拦截，请将 Telnix 目录与 `WinDivert64.sys` 加入杀软白名单后重试。

**首次启用相关功能时（GUI / CLI / MCP 都会触发）**：

- **GUI**：弹 Vue 对话框，用户点「了解，不再显示此提示」后会调 `POST /api/system/windivert-warning/ack` 持久化（写入 `settings.json` 的 `windivert_warning_acknowledged=1`），后续不再提示；点「取消」则不开功能。
- **CLI / MCP**：在桌面弹**原生 Windows 置顶 Yes/No 弹窗**（`MB_TOPMOST | MB_SYSTEMMODAL`，任务栏图标闪烁），用户选「是」后立即标记 ack=1 并自动重试原请求；选「否」则拒绝原请求（CLI 输出 `rejected_by_user: true` + 退出码 1）。
- **已确认（ack=1）后**：所有 WinDivert 相关端点直接放行，不再弹窗。可通过 `settings set -k windivert_warning_acknowledged -v 0` 重置为未确认状态。

**相关 API 端点**（详见 [README_AI.md](README_AI.md)）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/system/windivert-warning` | 查询是否需要提示 + 风险说明文本 + 当前 ack 状态 |
| POST | `/api/system/windivert-warning/ack` | 标记为已确认（永久不再提示，GUI 用） |
| POST | `/api/system/request-windivert-ack` | 创建 pending ack 请求 + 弹原生 MessageBox（CLI/MCP 用） |
| GET | `/api/system/windivert-ack-request/{rid}/wait` | 长轮询等待用户响应（CLI/MCP 用，60s 超时返回 pending） |

**CLI 子命令**：
```powershell
python -m telnix.cli system windivert-warning-status   # 查询状态
python -m telnix.cli system windivert-warning-ack       # 永久确认（不再提示）
```

---

## 平台支持矩阵

| 功能 | Windows | macOS | Linux | 备注 |
| --- | :---: | :---: | :---: | --- |
| **HTTP/HTTPS 抓包** | ✅ | ✅ | ✅ | 核心功能，全平台可用 |
| **SSL bump 解密 HTTPS** | ✅ | ✅ | ✅ | macOS 用 `security`，Linux 需手动 `update-ca-certificates` |
| **自动修改（拦截改包）** | ✅ | ✅ | ✅ | modify_request/response、mock、script 全平台可用 |
| **断点 / 重放 / 发包** | ✅ | ✅ | ✅ | |
| **AI 分析（DeepSeek）** | ✅ | ✅ | ✅ | |
| **规则 / 模板 / 命中统计** | ✅ | ✅ | ✅ | |
| **专注模式 / 忽略进程** | ✅ | ✅ | ✅ | PID 反查用 psutil 替代 GetExtendedTcpTable |
| **MCP Server（88 工具）** | ✅ | ✅ | ✅ | 全平台可用，详见 [README_MCP.md](README_MCP.md) |
| **Agent CLI** | ✅ | ✅ | ✅ | 全平台可用，详见 [README_AI.md](README_AI.md) |
| **Clash / Mihomo 上游代理集成** | ✅ | ✅ | ✅ | |
| **会话管理 / 导出 HAR/curl** | ✅ | ✅ | ✅ | |
| **WebSocket 抓包** | ✅ | ✅ | ✅ | |
| **HTTP/2 转发（ALPN h2）** | ✅ | ✅ | ✅ | |
| **根证书自动安装** | ✅ | ✅ | ⚠️ | Linux 需手动 `update-ca-certificates` |
| **系统代理自动配置** | ✅ | ✅ | ✅ | Windows 用 winreg，macOS 用 `networksetup`，Linux GNOME 用 `gsettings`，KDE 用 `kwriteconfig5` |
| **系统代理状态监控** | ✅ | ✅ | ✅ | 各平台读取实际代理状态 |
| **TCP/UDP 原始抓包** | ✅ | ✅ | ✅ | Windows 用 WinDivert，macOS 用 BPF（`/dev/bpfN`），Linux 用 AF_PACKET（`SOCK_RAW`） |
| **透明代理** | ✅ | ✅ | ✅ | Windows 用 WinDivert NAT，macOS 用 pf rdr，Linux 用 iptables REDIRECT |
| **DNS 劫持** | ✅ | ✅ | ✅ | Windows 用 WinDivert 拦截 UDP 53，macOS/Linux 用 pf/iptables 重定向 + 本地 DNS 服务 |
| **`system restart-as-admin`** | ✅ | ✅ | ✅ | Windows 用 UAC，macOS 用 osascript，Linux 用 pkexec 或 sudo |
| **`system firewall-allow`（netsh）** | ✅ | ❌ | ❌ | mac/linux 用 `ufw` / `firewall-cmd` / 系统偏好设置 |
| **PyInstaller 打包 exe** | ✅ | ⚠️ | ⚠️ | 可打包但未做 mac/linux 安装包（无 Inno Setup 等价物） |
| **Inno Setup 安装包** | ✅ | ❌ | ❌ | 仅 Windows |

> **总结**：macOS / Linux 上 **全部功能可用**。网络层功能（TCP/UDP 抓包、透明代理、DNS 劫持）各平台使用不同后端（WinDivert / AF_PACKET+BPF / iptables+pf），系统代理自动配置支持 macOS（networksetup）/ Linux GNOME（gsettings）/ KDE（kwriteconfig5），提权支持 UAC / osascript / pkexec。仅 `system firewall-allow`（netsh）和 Inno Setup 安装包为 Windows 专属。

---

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

**痛点**：抓包时若走 Clash 代理，Clash 会劫持系统代理，Telnix 抓不到；关掉 Clash 又连不上被墙的 API。

**Telnix 的方案**：内置 Clash/Mihomo 上游代理集成。Telnix 始终作为系统代理，出站连接可选走 Clash 的 mixed-port（默认 7890）。这样：

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

Telnix 不只是 GUI 工具，还为 AI Agent 提供了完整的编程接口：

#### MCP Server（88 个工具）

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

- **一键启动**：`python -m telnix` 自动开代理、装证书、启前端
- **HTTPS 开箱即用**：首次启动自动签发根证书并安装到系统信任存储
- **手机抓包向导**：扫码下载证书、自动计算安卓 7+ 系统证书哈希、教程链接
- **深色/浅色主题**：一键切换，CodeMirror 编辑器跟随主题
- **可视化规则编辑**：拖拽排序、批量启停、命中统计一目了然
- **专注模式**：只抓指定进程/host，过滤噪音
- **多选批量操作**：批量删除、批量放行断点、批量 AI 分析

---

## 项目结构

```
telnix/
├── src/
│   ├── host/                  # Python 后端
│   │   ├── telnix/
│   │   │   ├── api/           # FastAPI 路由（20+ 模块）
│   │   │   ├── proxy/         # 代理服务器核心
│   │   │   │   ├── server.py        # HTTP/HTTPS 抓包代理（builtin 线程引擎，默认）
│   │   │   │   ├── async_proxy.py   # asyncio 代理引擎（G 方案，实验性）
│   │   │   │   ├── raw_capture.py   # TCP/UDP 原始抓包（WinDivert，Windows）
│   │   │   │   ├── raw_capture_unix.py # TCP/UDP 原始抓包（AF_PACKET/BPF，macOS/Linux）
│   │   │   │   ├── transparent_proxy.py  # 透明代理（WinDivert NAT，Windows）
│   │   │   │   ├── transparent_proxy_unix.py # 透明代理（iptables/pf，macOS/Linux）
│   │   │   │   ├── dns_hijack.py    # DNS 劫持（WinDivert，Windows）
│   │   │   │   ├── dns_hijack_local.py # DNS 劫持（本地 DNS + iptables/pf，macOS/Linux）
│   │   │   │   ├── ssl_bump.py      # SSL Bump 动态签发证书
│   │   │   │   ├── breakpoint.py    # 断点管理
│   │   │   │   └── process_lookup.py # PID 反查
│   │   │   ├── auto_reply/   # 自动修改规则引擎
│   │   │   ├── clash/         # Clash 集成
│   │   │   ├── ai/            # DeepSeek AI 分析
│   │   │   ├── cli.py         # Agent CLI
│   │   │   ├── mcp_server.py  # MCP Server（88 个工具）
│   │   │   ├── elevation.py   # 跨平台提权（UAC / osascript / pkexec）
│   │   │   ├── system_proxy.py # 跨平台系统代理配置（winreg / networksetup / gsettings）
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
├── scripts/                   # 跨平台依赖安装脚本（linux/mac）
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
- [README_MCP.md](README_MCP.md) — MCP Server 88 个工具清单与客户端接入配置

---

## 性能优化

Telnix 在性能上做了大量优化，确保高并发场景下不卡顿：

- **后端线程池**：每个客户端连接独立线程，避免 asyncio 在 Windows 上的 IOCP 问题
- **连接复用**：keep-alive 连接池（默认 32 个 per key），避免重复 TCP+TLS 握手
- **SQLite WAL 模式 + threading.local 连接池**：每线程复用长连接，避免每次 connect/close
- **Body 截断记录**：大 body（视频/图片流）记录到 DB 时截断到 512KB，避免 base64 膨胀阻塞代理线程
- **前端异步高亮**：hljs 语法高亮用 `requestIdleCallback` 异步执行，避免大文本阻塞主线程
- **三阶段渲染**：纯转义（同步立即可见）→ hljs 高亮（异步）→ 搜索高亮（同步叠加），搜索时保留语法颜色
- **Vite 构建优化**：`minify: 'esbuild'`（terser 在 element-plus 大 bundle 上会卡死）、禁用 sourcemap、禁用 manualChunks

---

## 技术栈

**后端**：Python 3.10+ / FastAPI / Uvicorn / SQLite (WAL) / psutil / cryptography / WinDivert+pydivert（Windows）/ AF_PACKET+BPF（macOS/Linux 抓包）/ iptables+pf（macOS/Linux 透明代理&DNS 劫持）/ h2（HTTP/2）

**前端**：Vue 3 / Vite 5 / Element Plus / Pinia / CodeMirror 6 / highlight.js / axios

**AI**：DeepSeek（deepseek-v4-flash / deepseek-v4-pro）

**集成**：MCP (Model Context Protocol) / Clash (Mihomo) / PyInstaller（打包 exe）/ Inno Setup（安装包）

---

## License

MIT
