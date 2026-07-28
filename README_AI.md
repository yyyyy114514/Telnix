# Telnix Agent 使用手册

> 本文档面向 **AI agent**。读完即可用 CLI 或 HTTP API 控制 Telnix 抓包、改包、重放、发包。

Telnix 是跨平台 HTTP/HTTPS 抓包工具（类似 Fiddler/Charles），支持 SSL bump 解密 HTTPS、自动回复（改字段/mock 响应）、断点、重放、发包（Composer）、导出。提供 **CLI**（给 agent 主用）和 **HTTP API**（CLI 的底层，复杂场景兜底）。

> **平台支持**：Windows / macOS / Linux 全平台完整支持。网络层功能各平台使用不同后端：Windows 用 WinDivert / winreg / UAC，macOS 用 BPF / pf / networksetup / osascript，Linux 用 AF_PACKET / iptables / gsettings / pkexec。所有 CLI 命令全平台可用，详见 [§平台支持矩阵](#平台支持矩阵)。

---

## 平台支持矩阵

> 标记：✅ 全平台 / ⚠️ 部分平台

| 命令分类 | CLI 命令 | Windows | macOS | Linux | 备注 |
|---|---| :---: | :---: | :---: | --- |
| 抓包控制 | `capture start/stop/clear/pause/resume` | ✅ | ✅ | ✅ | HTTP/HTTPS 核心功能 |
| 流量查询 | `packets list/list-all/get/search/stats/endpoints/timeline/trace/analyze/watch/export/tag/diff/delete` | ✅ | ✅ | ✅ | |
| 拦截规则 | `intercept add/list/del/hits/toggle/update/export/import/template-*` | ✅ | ✅ | ✅ | |
| 重放/发包 | `replay` / `send` / `replay-batch` | ✅ | ✅ | ✅ | |
| 导出 | `export` | ✅ | ✅ | ✅ | |
| 断点 | `breakpoint status/on/off/release/timeout` | ✅ | ✅ | ✅ | |
| 专注模式 | `focus status/on/off` | ✅ | ✅ | ✅ | PID 反查用 psutil 替代 GetExtendedTcpTable |
| 进程管理 | `processes list/ignore/unignore/ignore-host/...` | ✅ | ✅ | ✅ | |
| 会话管理 | `sessions list/show/switch/delete/create` | ✅ | ✅ | ✅ | |
| 日志 | `log tail/clear/export` | ✅ | ✅ | ✅ | |
| 设置 | `settings get/set/engine` | ✅ | ✅ | ✅ | |
| 系统控制-通用 | `system restart/quit` | ✅ | ✅ | ✅ | mac/linux 用 subprocess 重启（无 UAC） |
| 可选依赖 | `system install-dep/install-dep-status` | ✅ | ✅ | ✅ | pip install mitmproxy |
| Agent 工作区 | `agent start/end/status` | ✅ | ✅ | ✅ | |
| 自动修改规则 | `auto-reply list/get/create/enable/disable/delete` | ✅ | ✅ | ✅ | |
| **证书管理** | `cert status` | ✅ | ✅ | ✅ | |
| **证书管理** | `cert install/remove` | ✅ | ✅ | ⚠️ | macOS 用 `security add-trusted-cert`；Linux 需手动 `update-ca-certificates` |
| **系统代理** | `proxy status` | ✅ | ✅ | ✅ | Windows 用 winreg，macOS 用 `networksetup`，Linux 用 `gsettings`/`kwriteconfig5` |
| **系统代理** | `proxy on/off` | ✅ | ✅ | ✅ | 全平台自动配置系统代理为 `127.0.0.1:8888` |
| **TCP/UDP 抓包** | `raw status/install/start/stop` | ✅ | ✅ | ✅ | Windows 用 WinDivert，macOS 用 BPF（`/dev/bpfN`），Linux 用 AF_PACKET（`SOCK_RAW`）；`install` 在 mac/linux 不需要（无 pydivert 依赖） |
| **透明代理** | `transparent-proxy status/start/stop` | ✅ | ✅ | ✅ | Windows 用 WinDivert NAT，macOS 用 pf rdr，Linux 用 iptables REDIRECT；需管理员/root |
| **DNS 劫持** | `dns-hijack status/start/stop/rules/clear-log` | ✅ | ✅ | ✅ | Windows 用 WinDivert 拦截 UDP 53，macOS/Linux 用 pf/iptables 重定向 + 本地 DNS 服务；需管理员/root |
| **管理员重启** | `system restart-as-admin` | ✅ | ✅ | ✅ | Windows 用 UAC，macOS 用 osascript，Linux 用 pkexec 或 sudo |

> **总结**：所有 CLI 命令全平台可用。网络层功能（raw / transparent-proxy / dns-hijack）、系统代理（proxy on/off）、管理员重启（system restart-as-admin）在各平台使用不同后端实现，但接口完全一致。`raw install` 在 mac/linux 上不需要（无 pydivert 依赖），会自动跳过。`system firewall-allow`（netsh）仍仅 Windows 可用。

### 各平台后端说明

所有网络层 CLI 命令全平台可用，但底层后端不同：

| 功能 | Windows 后端 | macOS 后端 | Linux 后端 |
|---|---|---|---|
| TCP/UDP 抓包 | WinDivert 内核驱动 | BPF 设备（`/dev/bpfN`） | AF_PACKET（`SOCK_RAW`） |
| 透明代理 | WinDivert NAT | pf rdr anchor | iptables REDIRECT |
| DNS 劫持 | WinDivert 拦截 UDP 53 | pf 重定向 + 本地 DNS 服务 | iptables 重定向 + 本地 DNS 服务 |
| 系统代理 | winreg 注册表 | `networksetup` | `gsettings`（GNOME）/ `kwriteconfig5`（KDE） |
| 管理员提权 | UAC（`ShellExecuteW runas`） | osascript（系统密码弹窗） | pkexec（PolicyKit）或 sudo |
| 原始目的地址查找 | NAT 表反查 | `getsockname()` | `getsockopt(SO_ORIGINAL_DST)` |

> **Linux 证书安装**：`cert install` 在 Linux 上需手动 `update-ca-certificates`（macOS 用 `security add-trusted-cert` 自动完成）。

---

## 启动参数（agent 必读）

后端启动命令 `python -m telnix`（工作目录 `src\host`），支持以下参数：

| 参数 | 说明 |
|---|---|
| `--no-browser` | 不自动打开浏览器，agent 自动化场景必用（避免干扰用户）。等价环境变量 `TELNIX_NO_BROWSER=1` |
| `--help` | 查看帮助 |

```bash
# agent 启动后端，不开浏览器
python -m telnix --no-browser

# 或用环境变量
TELNIX_NO_BROWSER=1 python -m telnix
```

> **macOS / Linux 启动**：用 `python3 -m telnix`（注意 `python3` 而非 `python`），或用项目根目录的 `./start.sh` 一键脚本（等价于 Windows 的 `run.ps1`）。详见 [README.md 的 macOS / Linux 安装与运行章节](README.md)。

**用户设置存储**：用户设置（GUI 偏好、列顺序、导航顺序、主题、缓存阈值等）存在 `<data_dir>/settings.json`（原子写入 + 线程锁），不再用 SQLite。首次启动若 JSON 不存在但 SQLite 有数据会自动迁移。用户可在 GUI 设置页点「打开设置文件」用记事本直接编辑。

---

## 0. 任务→命令决策树（不知道用什么命令先看这里）

| 我想... | 用什么命令 | 章节链接 |
|---|---|---|
| 看后端是否在跑 | `status` | [§3.1](#31-status--后端状态) |
| 开始/停止抓包 | `capture start` / `capture stop` | [§3.2](#32-capture--抓包控制) |
| 看抓到的流量列表 | `packets list` / `packets list --tail` | [§3.3](#33-packets--流量查询) |
| 看单条流量详情 | `packets get <id>` | [§3.3](#33-packets--流量查询) |
| 看软件调了哪些 API | `packets endpoints` | [§3.3](#33-packets--流量查询) |
| 看请求时序 | `packets timeline` | [§3.3](#33-packets--流量查询) |
| 改某接口响应字段 | `intercept add --action 'set-json ...'` | [§5.1](#51-改响应最常用) |
| 改请求字段（转发前篡改） | `intercept add --action 'set-request-json ...'` | [§5.3](#53-改请求转发前篡改) |
| mock 接口返回 404/500 | `intercept add --action 'mock 404 ...'` | [§5.2](#52-mock-响应不走服务器) |
| 写死请求 body 转发到服务器 | `intercept add --action 'mock-request ...'` | [§5.2](#52-mock-响应不走服务器) |
| 注入响应延迟 | `intercept add --action 'delay 5000'` | [§5.5](#55-延迟注入测客户端竞态超时) |
| 跨流量搜索字段 | `packets search --body-regex/--header-regex/--method/--status/--pid/--process` | [§3.3](#33-packets--流量查询) |
| 找签名字段 | `packets analyze <id1> <id2> ... --find-signature` | [§3.3](#33-packets--流量查询) |
| 追踪 token 传递来源 | `packets trace <id> --all` | [§3.3](#33-packets--流量查询) |
| 对比两条流量差异 | `packets diff <id1> <id2> --field F` | [§3.3](#33-packets--流量查询) |
| 重放请求改参数 | `replay <id> --body ... --header ...` | [§3.5](#35-replay--重放流量支持改参数) |
| 批量重放测限流 | `replay <id> --repeat N --parallel M` | [§3.5](#35-replay--重放流量支持改参数) |
| 整会话时序回放 | `replay-batch --session <id> --preserve-timing` | [§3.16](#316-replay-batch--时序回放按-session-整批重放) |
| 看规则命中了没 | `intercept hits <rule_id>` | [§3.4](#34-intercept--拦截改包规则核心) |
| 启用/禁用规则不删除 | `intercept toggle <id> --enable/--disable` | [§3.4](#34-intercept--拦截改包规则核心) |
| 幂等创建规则（避免重复） | `intercept add --idempotent` | [§3.4](#34-intercept--拦截改包规则核心) |
| 跨会话查历史流量 | `packets list-all --host ...` | [§3.3](#33-packets--流量查询) |
| 标记关键流量 | `packets tag <id> --add ...` | [§3.3](#33-packets--流量查询) |
| 按标签过滤 | `packets list --tag <name>` | [§3.3](#33-packets--流量查询) |
| 流量分组统计 | `packets stats` / `packets stats --by endpoint/content_type/process` | [§3.3](#33-packets--流量查询) |
| 只抓指定进程/host | `focus add --pid ...` / `focus add --host ...` | [§3.11](#311-focus--专注模式只抓指定进程host跨类-or-匹配) |
| 设置断点 | `breakpoint set --on-request/--on-response` | [§3.12](#312-breakpoint--断点控制支持超时自动放行--批量-release) |
| 忽略某进程/host | `processes ignore --pid N` / `processes ignore-host --host H` | [§3.13](#313-processes--进程列表--忽略进程host-管理) |
| 导出 HAR/curl | `export --format har -o file.har` | [§3.6](#36-export--导出会话) |
| 单 flow 导出 | `packets export <id> --format curl/python-requests/postman/csv/json` | [§3.3](#33-packets--流量查询) |
| 装根证书 | `cert install` | [§3.8](#38-cert--证书管理) |
| 看日志 | `log tail` / `log clear` / `log export` | [§3.9](#39-log--日志) |
| 重启前后端 | `system restart` | [§3.21](#321-system--系统控制重启--退出--管理员重启) |
| 以管理员身份重启（TCP/UDP 抓包用） | `system restart-as-admin` | [§3.21](#321-system--系统控制重启--退出--管理员重启) |
| 安装可选依赖（mitmproxy） | `system install-dep` + `system install-dep-status` 轮询 | [§3.21.1](#3211-system-install-dep--在线安装可选依赖如-mitmproxy) |
| 查看设置 / 写入设置 | `settings get [-k KEY]` / `settings set -k KEY -v VALUE` | [§3.21.2](#3212-settings--设置管理get--set--engine) |
| 切换代理引擎 | `settings engine [builtin\|async\|mitmproxy]` + `system restart` | [§3.21.2](#3212-settings--设置管理get--set--engine) |
| WinDivert 风险提示查询/确认 | `system windivert-warning-status` / `system windivert-warning-ack` | [§3.21.3](#3213-system-windivert-warning--windivert-风险提示查询确认) |
| 从零发包（Composer） | `send --url ... --method ...` | [§3.22](#322-send--从零发包composer) |
| 接管会话前清场（保留原状，事后询问用户是否退出） | `agent start` / `agent end` | [§3.23](#323-agent--agent-工作模式保留原状end-不关代理不退出) |
| 批量导入规则 | `intercept import rules.json` | [§3.14](#314-intercept-exportimport--拦截规则导入导出) |
| 启动透明代理（抓无代理感知应用） | `transparent-proxy start` | [§3.24](#324-transparent-proxy--透明代理控制) |
| DNS 劫持 | `dns-hijack start --rules "domain=ip"` | [§3.26](#326-dns-hijack--dns-劫持控制) |
| 查看平台能力矩阵 | `system platform-capabilities` | [§3.21](#321-system--系统控制重启--退出--管理员重启) |

逆向工作流模板（6 个完整场景的命令组合）见 [§6 典型工作流](#6-典型工作流agent-抄这个)。

---

## 0.1. 30 秒上手

```bash
# 1. 确认后端在跑（默认 127.0.0.1:18901）
python -m telnix.cli status

# 2. 开始抓包，拿到 session_id
python -m telnix.cli capture start
# stdout: {"session_id": 7, "capturing": true}

# 3. 让被分析软件跑一会儿，tail 看包（Ctrl+C 退出）
python -m telnix.cli packets list --tail --emit-curl

# 4. 发现要改的请求，加拦截规则（先 dry-run 预览）
python -m telnix.cli intercept add \
  --match 'host~=api.example.com && method=POST && path~=/api/v1/*' \
  --action 'set-json remainingUses 99999' \
  --name 'bump-uses' \
  --dry-run

# 5. 确认无误，去掉 --dry-run 真生效
python -m telnix.cli intercept add \
  --match 'host~=api.example.com && method=POST && path~=/api/v1/*' \
  --action 'set-json remainingUses 99999' \
  --name 'bump-uses'

# 6. 收尾
python -m telnix.cli capture stop
python -m telnix.cli export --format har -o evidence.har
```

---

## 1. 连接配置

| 项 | 默认 | 环境变量 |
|---|---|---|
| API 地址 | `http://127.0.0.1:18901` | `TELNIX_API` |
| 默认 session | 活动会话（从 `/status` 查询） | `Telnix_SESSION` |
| 代理端口 | 8888 | — |

> **端口说明**：API 端口是 **18901**（不是 18899）。18899/18900 在部分 Windows 机器被动态端口保留（bind 报 WSAEACCES=13），所以 Telnix 改用 18901。CLI 默认连 18901，与 `config.DEFAULT_PORT` 保持一致。agent 不要假设端口是 18899。

```bash
# 自定义后端地址
TELNIX_API=http://127.0.0.1:18901 python -m telnix.cli status

# 固定 session（agent 脚本里多次命令复用同一 session，省去每条命令带 --session）
export Telnix_SESSION=7
python -m telnix.cli packets list       # 自动用 session 7
python -m telnix.cli packets get 42     # --session 优先级 > 环境变量 > /status
```

**session 优先级**：`--session N` 参数 > `Telnix_SESSION` 环境变量 > `/status` 查询活动会话。

后端未启动时 CLI 会退出码 2 + stderr 输出 `{"ok": false, "error": "无法连接后端...", "kind": "conn"}`。

---

## 2. 输出格式约定（重要）

- **单对象命令**（status / capture start / intercept add 等）：stdout 输出一行紧凑 JSON。
- **列表命令**（packets list / intercept list / log tail）：stdout 输出 **NDJSON**，一行一个 JSON 对象，**不是 JSON 数组**。方便 `while read line; do ...` 流式处理。
- **错误**（stderr，JSON 格式）：`{"ok": false, "error": "错误描述", "kind": "arg|conn|biz", "hint": "建议动作（可选）"}`。
  - `kind`：`arg`=参数错误（退出码 3）、`conn`=连接错误（退出码 2）、`biz`=后端业务错误（退出码 1）。
  - `hint`：**可选字段**，仅部分错误提供。agent 解析时用 `.get("hint")`，不要直接 `error["hint"]` 避免 KeyError。
- **退出码**：

| 退出码 | 含义 | kind |
|---|---|---|
| `0` | 成功 | — |
| `1` | 后端业务错误（500/规则不存在/会话不存在等） | `biz` |
| `2` | 连接错误（后端未启动/网络不通） | `conn` |
| `3` | 参数错误（客户端校验失败） | `arg` |

agent 解析建议：先看退出码判断错误大类，再读 stdout（成功）或 stderr（失败）。不要按文本匹配 stderr，错误输出是结构化 JSON，直接 `json.loads(stderr_content)` 取 `error`/`kind`/`hint`。

---

## 3. CLI 命令详解

> 所有命令前缀：`python -m telnix.cli <subcommand>`
> 工作目录需在 `d:\Desktop\Telnix\src\host` 下，或后端已打包到 PATH。

### 3.1 `status` — 后端状态

```bash
python -m telnix.cli status
```

```json
{"capturing": true, "session_id": 7, "proxy_port": 8888,
 "cert_installed": true, "system_proxy_on": true,
 "breakpoint": {"break_on_request": false, "break_on_response": false,
                "timeout_seconds": 0.0, "pending": []},
 "pinning_suspected": []}
```

- `breakpoint.timeout_seconds`：断点超时（0=永不超时，>0 时 N 秒自动放行，避免 agent 忘 release 卡死连接）。
- `breakpoint.pending`：当前被断点拦住的流量列表，每项含 `flow_id` 和 `waiting_seconds`（已等待秒数）。
- `pinning_suspected`：疑似证书 pinning 的进程列表（TLS 握手失败 + 证书相关错误收集，按 host+pid+proc 去重，上限 50 条）。空数组表示没检测到。

agent 首次接入先调这个，确认后端活着 + 当前是否在抓包 + 证书是否装了 + 是否有 pinning 嫌疑进程。

### 3.2 `capture` — 抓包控制

```bash
# 开始抓包（返回 session_id，后续命令可带 --session 续命）
python -m telnix.cli capture start [--max-duration 60] [--auto-stop 30] [--layer http|tcp|all]
# {"session_id": 7, "capturing": true, "auto_stop_seconds": 30.0,
#  "hint": "后端将在 30.0s 后自动停止抓包"}

# 停止
python -m telnix.cli capture stop [--layer all]
# {"capturing": false}

# 清空当前会话流量
python -m telnix.cli capture clear
# {"cleared": true}

# 暂停抓包（保留会话，代理仍跑，只是不记录新流量）
python -m telnix.cli capture pause
# {"paused": true, "session_id": 7, "hint": "会话保留，代理仍跑。resume 恢复，stop 真正停止"}

# 恢复抓包记录（在原会话继续）
python -m telnix.cli capture resume
# {"resumed": true, "session_id": 7}
```

- `--max-duration`：仅给 agent 的提示字段，不会自动停止；agent 应自己 `sleep N && capture stop`。
- `--auto-stop N`：**真正自动停止**。参数传给后端，由后端常驻进程负责定时（agent CLI 退出也不影响）。N 秒后后端自动 `capture stop` 并结束会话。适合"抓 30 秒就停"场景，agent 不用自己 sleep。
- `--layer http`（默认）：HTTP 代理层抓包（SSL bump 解密 HTTPS）。
- `--layer tcp`：网络层 TCP/UDP 抓包（WinDivert，需管理员+pydivert），见 §3.10。
- `--layer all`：同时开 HTTP + TCP/UDP。`capture stop --layer all` 一次性停掉两层。
- **`capture start` 会自动开系统代理**（`capture.py:69-77` 检查 `system_proxy_on`，未开则自动调 `enable_proxy`），不用单独 `proxy on`。返回值含 `system_proxy_on` 和 `proxy_auto_enabled` 字段。
- **`capture stop` 不关系统代理**：stop 只结束会话 + 停止记录，代理服务器仍在 8888 端口运行，系统代理保持开启。这样流量正常转发不中断，规则（自动修改）仍生效（SSL bump + 规则匹配独立于 capturing 状态）。若需关闭系统代理，显式调 `proxy off`（`POST /system/clear-proxy`）。
- 抓包**独立于拦截规则**：规则只要启用，即使 `capturing=false` 也会解密 HTTPS 应用规则（前提是证书已装）。
- **`pause` vs `stop`**：`pause` 只是把 `capturing` 置 false，会话保留，代理仍跑（规则仍生效），新流量不记录；`stop` 真正结束会话（`ended_at` 填充）。`resume` 恢复记录到原会话。适合"抓一会儿暂停看看，再继续抓"场景，不用反复 start/stop 开新会话。

### 3.3 `packets` — 流量查询

```bash
# 列表（NDJSON），默认当前活动会话，最新在前
python -m telnix.cli packets list \
  [--session 7] \
  [--limit 100] \
  [--since-id 42] \
  [--filter 'host~=api.example.com && method=POST'] \
  [--filter-host api.example.com] \
  [--filter-status 200] \
  [--filter-method POST] \
  [--protocol http|tcp|udp] \
  [--emit-curl] \
  [--json-array] \
  [--tail]
```

- `--filter`：表达式过滤（见 §4），客户端执行。
- `--filter-host/-status/-method`：快捷参数，传给后端 SQL 过滤，更快。
- `--since-id N`：只返回 id > N 的流量（增量查询，非阻塞）。agent 轮询推荐用这个替代 `--tail`。
- `--protocol`：按协议过滤（http/tcp/udp），TCP/UDP 抓包数据用。
- `--emit-curl`：每条流量 JSON 多一个 `curl` 字段，是可直接 `bash -c` 重放的 curl 命令。
- `--json-array`：输出 JSON 数组而非 NDJSON（`[{...},{...}]`），适合一次性 `jq` 处理。
- `--tail`：流式模式，每 1s 拉新流量（内部用 `--since-id`），遇到就输出。**Ctrl+C 退出**。
- **`max_id` 输出位置**：仅当显式传 `--since-id N` 时，列表后 stderr 才会输出 `{"max_id": N, "count": M}`，agent 用 `max_id` 做下次 `--since-id` 基线。普通 `packets list`（不带 `--since-id`）不会污染 stderr。

```bash
# 单条详情
python -m telnix.cli packets get 42 [--emit-curl] [--hex] [--field request_body|response_body|raw_data] [--offset 0] [--length 4096] [--decode plugin.py] [--decode-field request_body|response_body]
```

#### 解码器插件（`--decode`）

逆向 Steam/游戏等二进制协议时，`response_body` 是 `base64:` 前缀的字节串，hex dump 只能看字节不能看语义。`--decode` 让 agent 加载临时 Python 解码器，按目标协议解析 body，不污染主代码。

**解码器接口**（一个文件，一个函数）：
```python
# my_steam_decoder.py
def decode(data: bytes, flow: dict) -> dict:
    """data 是 response_body 解码后的原始字节（base64: 前缀已自动处理）。
    flow 是完整流量对象（含 host/path/headers 等），可按需取用。
    返回 dict 会被原样放到输出 JSON 的 decoded 字段。"""
    # 示例：假设协议是 [长度4字节][消息ID 2字节][payload]
    import struct
    if len(data) < 6:
        return {"error": "too short"}
    length, msg_id = struct.unpack("<IH", data[:6])
    return {
        "length": length,
        "msg_id": msg_id,
        "payload_hex": data[6:].hex(),
        "host": flow.get("host"),
    }
```

**用法**：
```bash
python -m telnix.cli packets get 42 --decode my_steam_decoder.py
# 输出：{...原始 flow 字段..., "decoded": {"length": 128, "msg_id": 5, "payload_hex": "...", "host": "..."}}
```

- 解码器加载失败 / 执行失败会往 stderr 输出 `{"ok": false, "error": "..."}` 并退出码 1，不影响 stdout。
- 解码器可以 import 第三方库（如 protobuf、construct），agent 按目标协议临时写一个即可。
- `data` 永远是 bytes；如果 response_body 是文本（非 base64:），会 utf-8 encode 后传入。
- `--decode-field`：默认解码 `response_body`，传 `--decode-field request_body` 可解码请求体。批量解码（list/list-all）同样支持。

```bash
# 删除
python -m telnix.cli packets delete 42
python -m telnix.cli packets delete --ids 1,2,3

# 跨流量搜索（正则匹配 body/url/path）
python -m telnix.cli packets search --body-regex 'remainingUses.*\d{4,}'
python -m telnix.cli packets search --binary-hex 'efbbbf'  # 搜 BOM 头
python -m telnix.cli packets search --binary-hex 'efbbbf' --offset 0:1024  # 只在前 1KB 搜（节省大 body 搜索时间）
python -m telnix.cli packets search --body-regex 'sig=[a-f0-9]{32}' --all  # 跨所有会话搜索
# 多条件组合搜索（§3.1，所有条件 AND 关系）
python -m telnix.cli packets search --body-regex 'sig=[a-f0-9]{32}' --method POST --status 200
python -m telnix.cli packets search --header-regex 'Authorization: Bearer .+' --method POST --all
python -m telnix.cli packets search --method POST --status 200 --pid 1234 --process chrome.exe
python -m telnix.cli packets search --method GET --status 404    # 只用精确字段过滤（无需正则）

# 跨会话查询所有流量（不依赖活动会话，逆向比对历史用）
python -m telnix.cli packets list-all [--host H] [--process P] [--method M] \
  [--status N] [--protocol http|tcp|udp] [--limit N] [--offset N] [--since-id N] \
  [--filter 'expr'] [--filter-path P] [--filter-url U] [--emit-curl] [--json-array] \
  [--decode plugin.py] [--decode-field request_body|response_body]

# 跨会话清理流量（按需清理旧数据，避免 SQLite 膨胀）
python -m telnix.cli packets clear --all              # 清空全部历史流量
python -m telnix.cli packets clear --before-id 1000   # 删除 id<1000 的旧流量

# 单 flow 导出（逆向取证刚需，不用导出整个 session）
python -m telnix.cli packets export 42 --format curl -o req.sh
python -m telnix.cli packets export 42 --format python-requests -o req.py
python -m telnix.cli packets export 42 --format postman -o req.json
python -m telnix.cli packets export 42 --format csv -o req.csv     # CSV 单行带表头
python -m telnix.cli packets export 42 --format json -o req.json  # 不指定 -o 则 stdout

# 流量统计（按 host/method/status/protocol 分组）
python -m telnix.cli packets stats
python -m telnix.cli packets stats --by content_type      # 按 Content-Type 分组（跨会话全量统计）
python -m telnix.cli packets stats --by process           # 按进程名分组（跨会话全量统计）

# 流量标签（打标 / 按标签过滤，存 SQLite 切换会话不丢）
python -m telnix.cli packets tag 42 --add analyzed
python -m telnix.cli packets tag 42 --add suspicious,key --note "疑似签名字段"
python -m telnix.cli packets tag 42 --remove analyzed
python -m telnix.cli packets tag 42 --clear
python -m telnix.cli packets tag 42 --clear-note          # 清除备注（保留标签）
python -m telnix.cli packets tag --list                   # 列出全局所有标签及每标签的 flow 数（§4.2）
python -m telnix.cli packets list --tag suspicious        # 按标签过滤
python -m telnix.cli packets list-all --tag analyzed      # 跨会话按标签过滤
python -m telnix.cli packets list --has-tags              # 只看有标签的
```

- `--hex`：输出 hex dump 格式（偏移+字节+ASCII），配合 `--field` 指定字段、`--offset`/`--length` 截取。
- `packets search`：跨流量多条件搜索（§3.1）。支持 `--body-regex`（body 正则）、`--binary-hex`（二进制 hex 搜索，可配 `--offset START:END` 限定字节范围）、`--header-regex`（header 正则）、`--method`/`--status`/`--pid`/`--process`（精确过滤）。所有条件 AND 组合，至少一个非空。加 `--all` 跨所有会话搜索（session_id=0），不加则只搜当前活动会话。
- `packets list-all`：跨会话查询所有流量，逆向 agent 比对同一接口在不同会话中的请求差异（如签名变化、token 轮换）。支持 `--since-id` 增量轮询，stderr 输出 `{"max_id": N, "count": M, "total": T}`。`--filter-path P`/`--filter-url U` 走后端 SQL LIKE 过滤（比客户端 `--filter` 快），适合在大流量库里精准定位。
- `packets clear`：跨会话清理。`--all` 清空全部，`--before-id N` 删除 id<N 的旧流量。返回 `{"cleared": true, "scope": "...", "deleted": N}`。
- `packets export`：单 flow 导出。`curl` 出可重放脚本，`python-requests` 出 Python 脚本，`postman` 出 Postman Collection v2.1 JSON，`csv` 出单行 CSV（带表头，12 个字段），`json` 出完整 flow JSON。不指定 `-o` 则输出到 stdout。
- `packets stats`：返回 `{total, by_host, by_method, by_status, by_protocol}` 分组统计。`--by endpoint` 按 path 模板归一化分组（§3.3 统计扩展）。`--by content_type`/`--by process` 走后端 `/flows/stats` 跨会话全量统计（不限于当前会话），分别按响应 Content-Type / 进程名分组，返回 `{groups: [{key, label, count}], total, group_by}`。
- `packets tag <id>`：给流量打标签（存 SQLite，切换会话/重启不丢失）。`--add analyzed` 加标签（多值逗号分隔，如 `--add suspicious,key`）；`--remove analyzed` 移除标签；`--clear` 清空所有标签；`--note "备注"` 设置 `tag_note` 字段（可与 `--add` 同时用）；`--clear-note` 显式清空备注（保留标签不变）。`tags` 字段在 flow JSON 里是逗号分隔字符串，`tag_note` 是备注可空。`packets tag --list` 列出全局所有标签及每标签的 flow 数（NDJSON：`{"tag": "...", "count": N}`），适合先看有哪些标签再决定过滤哪个。配合 `packets list --tag <name>` / `list-all --tag <name>` 按标签过滤，`packets list --has-tags` 只看有标签的流量。适合长会话标记"已分析""可疑""关键接口"。

#### 流量对比 / API 地图 / 时间线（逆向分析利器）

```bash
# 对比两条流量的请求/响应字段（unified diff，JSON body 自动格式化便于 diff）
python -m telnix.cli packets diff 42 43 --field response_body
# {"id1": 42, "id2": 43, "field": "response_body", "diff": "--- #42.response_body\n+++ #43.response_body\n@@ -1,3 +1,3 @@\n-  \"remainingUses\": 1\n+  \"remainingUses\": 99999\n", "identical": false}

# 唯一 endpoint 提取（path 模板归一化，画 API 地图）
python -m telnix.cli packets endpoints [--host H] [--limit 200] [--json-array] [--session S] [--keep-query] [--sample-strategy first|last|random]
# NDJSON，每行一个 endpoint：
# {"method": "POST", "host": "api.example.com", "path_template": "/v1/users/{id}/usage",
#  "count": 12, "status_set": [200, 401], "sample_ids": [42, 43, 44], "query_keys": ["page", "size"]}

# 流量时间线（按时间排序，标注大间隔段落）
python -m telnix.cli packets timeline [--host H] [--gap 1.0] [--limit 200] [--session S]
# {"timeline": [...], "count": N, "segments": M, "gap_threshold": 1.0}
```

- `packets diff <id1> <id2> --field F`：对比两条流量的指定字段（`request_body`/`response_body`/`request_headers`/`url` 等），输出 unified diff。JSON body 会自动 `json.dumps(sort_keys=True, indent=2)` 格式化，让字段顺序差异可见。`identical=true` 表示完全相同。逆向 agent 对比同一接口不同时刻/不同参数的响应差异用这个。
- `packets endpoints`：path 模板归一化提取唯一 endpoint。数字段→`{id}`、UUID→`{uuid}`、长 hex(≥16)→`{hex}`、长 token(≥24)→`{token}`。`count` 是命中次数，`status_set` 是出现过的状态码，`sample_ids` 是 3 个样本流量 ID（默认取前 3 个，`--sample-strategy last` 取后 3 个，`--sample-strategy random` 随机取 3 个）。`--session S` 指定会话分析（默认跨所有会话），`--limit 0` 拉全量（上限 50000），`--keep-query` 保留 query 参数名到 `query_keys` 字段（看每个 endpoint 支持哪些查询参数）。适合快速摸清一个软件调了哪些 API。
- `packets timeline`：按时间排序的流量时间线，间隔超过 `--gap`（默认 1 秒）的标 `segment_break=true` 和 `gap_seconds`，`segment` 字段是段落编号。`--session S` 指定会话。适合分析"用户点了某按钮后发了哪些请求"这种时序场景。

#### 统计扩展（按 endpoint 分组 + 百分位指标）

```bash
# 按 endpoint 分组统计（path 模板归一化）
python -m telnix.cli packets stats --by endpoint [--session S] [--limit 2000]
# {"by": "endpoint", "endpoints": [...]}

# 附加 size/duration 百分位指标（p50/p95/max/min）
python -m telnix.cli packets stats --by endpoint --metrics size,duration
# 每个 endpoint 多 "size": {"p50":..., "p95":..., "max":..., "min":..., "count":...}

# 也可以只加 --metrics 不加 --by，在原有 host/method/status 分组基础上附加全局 size/duration 分布
python -m telnix.cli packets stats --metrics size,duration
```

- `--by endpoint`：拉取本会话最多 500 条流量做 path 模板归一化分组（见 `packets endpoints`），输出每个 endpoint 的 method/host/path_template/count/status_set/sample_ids。
- `--metrics size,duration`：附加百分位指标。`size` 是响应体字节数，`duration_ms` 是耗时。p95 高 = 长尾慢请求，适合定位性能瓶颈。

#### 批量解码器（list/list-all 也能用）

```bash
# 批量解码：list 和 list-all 都支持 --decode，每条流量输出 decoded 字段
python -m telnix.cli packets list --decode my_decoder.py --limit 50
python -m telnix.cli packets list-all --host api.example.com --decode my_decoder.py
```

- `--decode plugin.py`：加载 Python 解码器（接口见上方"解码器插件"小节），对每条流量的 `response_body` 解码，结果放到输出 JSON 的 `decoded` 字段。适合批量解析二进制协议（如 Steam protobuf），不用逐条 `packets get`。

#### `packets watch` — 定向 tail（阻塞输出匹配的新流量）

```bash
# 阻塞输出匹配过滤表达式的新流量（NDJSON），Ctrl+C 退出
python -m telnix.cli packets watch --filter 'host~=api.example.com && method=POST'
# {"id": 42, "method": "POST", "host": "api.example.com", ...}
# {"id": 43, ...}
```

- `--filter 'expr'`：匹配表达式（见 §4），客户端过滤。只输出匹配的新流量。
- `--session S`：指定会话（默认当前活动会话）。
- 内部用 `--since-id` 轮询，1 秒间隔，非阻塞拉取。
- 与 `packets list --tail` 区别：`watch` 支持 `--filter` 客户端过滤，只看匹配的流量；`--tail` 看所有流量。agent 想盯特定接口用 `watch`。

#### `packets trace` — 请求依赖链分析（token 传递追踪）

逆向常需看"请求 A 响应里的 token/session_id 被请求 B 用了"这种依赖关系。`trace` 自动从源 flow 响应提取字符串值，在后续流量的请求里搜索，输出依赖链。

```bash
python -m telnix.cli packets trace 42 [--limit 500] [--min-length 8]
python -m telnix.cli packets trace 42 --all              # 跨会话追踪 token 传递
python -m telnix.cli packets trace 42 --min-length 8     # 只追踪长度 >=8 的字符串（减少短串误报）
# NDJSON，每行一个依赖：
# {"source_flow": 42, "target_flow": 45, "field": "data.token", "value": "abc123def456"}
# {"source_flow": 42, "target_flow": 47, "field": "session_id", "value": "xyz789"}
```

- 提取逻辑：①解析响应 body 为 JSON，遍历所有叶子字符串（长度 ≥ `--min-length`，默认 4）；②正则匹配常见 token 模式（`token`/`access_token`/`session_id`/`sid`/`uid`/`user_id` 等）。
- 搜索范围：后续流量（id > 源 flow）的 `request_headers` + `request_body`，客户端字符串包含匹配。
- `--limit N`：扫描后续流量条数上限（默认 500）。
- `--min-length N`：只追踪长度 ≥ N 的字符串值（默认 4）。调大可减少短串误报（如 `"ok"`、`"1"` 这类常见短串）。
- `--all`：跨会话扫描。不加 `--all` 默认只在当前活动会话内扫描后续流量；加 `--all` 改调 `/flows/all`（since_id=源 id）跨会话扫描，适合追踪跨会话的 token 传递（如 token 在 A 会话登录拿到，B 会话用）。
- 二进制响应（`base64:` 前缀）无法提取字符串，会返回空依赖列表 + hint。
- 适合分析登录后 token 如何在后续请求中传递、OAuth 流程、多步签名等场景。

#### `packets analyze` — 签名字段自动检测

逆向定位签名字段靠人工 diff。`analyze` 自动比对多条同接口请求的 JSON body，找出"长度固定 + 字符集受限 + 每次都不同"的可疑签名字段。

```bash
python -m telnix.cli packets analyze 42 43 44 [--find-signature]
python -m telnix.cli packets analyze 42 43 44 --all      # 跨会话签名字段检测
# NDJSON，每行一个字段分析结果（按 suspicion_score 降序）：
# {"field_path": "data.sig", "lengths": [32], "charsets": ["hex"],
#  "varies": true, "sample_values": ["a1b2...", "c3d4...", "e5f6..."],
#  "suspicion_score": 1.0}
```

- 至少需要 2 个 flow ID（同接口不同时刻的请求）。
- `--find-signature`：显式声明做签名字段检测（默认即启用，flag 仅语义化）。
- `--all`：跨会话扫描后续流量。不加 `--all` 默认只在当前活动会话内扫描；加 `--all` 改调 `/flows/all`（since_id=源 id）跨会话扫描，适合检测跨会话的签名字段变化。
- 分析逻辑：解析每个 flow 的 `request_body` JSON，遍历所有叶子字段，对比同字段在多次请求中的值。
- 可疑评分（0-1）：`varies`（每次都不同）+0.34，长度固定（`lengths` 只有 1 个值）+0.33，字符集受限（hex/base64/alphanumeric）+0.33。满分 1.0 = 高度可疑签名。
- 字符集分类：`numeric`/`hex`/`base64`/`alphanumeric`/`alpha`/`mixed`。
- 二进制 body（`base64:` 前缀）跳过不分析。
- 适合快速定位"哪个字段是动态生成的签名/token"，缩小逆向范围。

流量 JSON 字段（关键）：

```json
{
  "id": 42,
  "session_id": 7,
  "timestamp": "2026-07-16T12:34:56.789",
  "pid": 1234,
  "process_name": "chrome.exe",
  "method": "POST",
  "url": "https://api.example.com/v1/chat",
  "scheme": "https",
  "host": "api.example.com",
  "path": "/v1/chat",
  "request_headers": "Host: api.example.com\r\nContent-Type: application/json\r\n...",
  "request_body": "{\"msg\":\"hi\"}",
  "status_code": 200,
  "response_headers": "Content-Type: application/json\r\n...",
  "response_body": "{\"ok\":true}",
  "duration_ms": 456,
  "size": 1234,
  "breakpoint_status": null,
  "protocol": "http",
  "raw_data": null,
  "src_port": null,
  "dst_port": null
}
```

- `protocol`：`http`（默认）| `tcp` | `udp`（TCP/UDP 抓包数据）。
- `raw_data`：TCP/UDP 原始字节（`base64:` 前缀），HTTP 流量为 null。
- `src_port`/`dst_port`：TCP/UDP 端口，HTTP 流量为 null。
- 二进制 body 用 `base64:` 前缀存储，如 `"request_body": "base64:iVBORw0KGgo..."`。

### 3.4 `intercept` — 拦截/改包规则（核心）

```bash
# 添加规则
python -m telnix.cli intercept add \
  --match 'host~=api.example.com && method=POST && path~=/api/v1/*' \
  --action 'set-json remainingUses 99999' \
  --name 'bump-uses' \          # 写入 note 字段
  [--note '自定义备注'] \        # 覆盖 name
  [--session 7] \                # dry-run 用
  [--dry-run] \                  # 预览：只列出会命中的流量，不创建规则
  [--idempotent]                 # §2.2 幂等创建：已存在相同规则则返回现有 rule_id 不重复创建
```

**`--idempotent` 幂等创建**（§3.1）：优先调后端专用端点 `POST /auto-reply/rules/idempotent`（单次请求 + 服务端比对，比客户端遍历高效）。服务端比对签名（pattern+match_mode+action+modify_rules JSON 序列化后字符串相等）。命中相同规则则不重复创建，直接返回现有 `rule_id`：

```bash
# 幂等创建：已存在相同规则不重复创建
python -m telnix.cli intercept add \
  --match 'host~=api.example.com && path~=/usage' \
  --action 'set-json remainingUses 99999' \
  --name 'bump-uses' --idempotent
# 首次：{"created": true, "rule_id": "abc123", ...}
# 再次执行相同命令：{"created": false, "idempotent": true, "rule_id": "abc123",
#                   "hint": "已存在相同规则，未重复创建"}
```

**注意事项**：
- **modify_rules 顺序敏感**：`[{a:1},{b:2}]` 与 `[{b:2},{a:1}]` 签名不同，视为不同规则。agent 生成 modify_rules 时保持顺序稳定（如按字段名字母序排序）可避免误创建。
- **比对字段**：只比 pattern+match_mode+action+modify_rules 四项。note/mock_status/mock_headers 等字段差异不触发幂等（即使 note 不同，签名相同就视为同一规则）。
- **降级兼容**：后端旧版本无 `/auto-reply/rules/idempotent` 端点时，CLI 自动降级为客户端遍历（`GET /auto-reply/rules` 全量拉取后比对），输出 hint 含"客户端遍历降级"字样。
- agent 批量部署规则 / 脚本反复执行同一规则时用 `--idempotent` 避免 DB 堆积重复规则。大批量规则部署（如几十条）推荐用 `intercept import` 批量导入。

`--dry-run` 输出示例：

```json
{
  "dry_run": true,
  "would_create": {
    "pattern": "*api.example.com*/api/v1/*",
    "match_mode": "wildcard",
    "action": "modify_response",
    "modify_rules": [{"target":"response_body","op":"replace","key":"remainingUses","value":99999}],
    "mock_status": null,
    "note": "bump-uses"
  },
  "matched_count": 3,
  "matched_flows": [
    {"id": 42, "method": "POST", "host": "api.example.com", "path": "/api/v1/chat", "url": "https://..."}
  ]
}
```

真创建输出：

```json
{"created": true, "rule_id": "abc123def456", "pattern": "*api.example.com*/api/v1/*", "note": "bump-uses"}
```

```bash
# 规则列表（NDJSON，每条含 rule_id + hit_count 命中统计字段）
python -m telnix.cli intercept list [--json-array] [--with-stats]
# 每条规则输出含: id, rule_id, pattern, action, enabled, note,
#   method_filter, status_filter, pid_filter, process_filter,
#   hit_count, last_hit_at, last_hit_flow_id

# 查看某规则的命中统计 + 最后命中的流量详情（§4.1）
python -m telnix.cli intercept hits <rule_id>
# {"rule_id": "abc123", "pattern": "*api.x.com*/usage*", "action": "modify_response",
#  "hit_count": 42, "last_hit_at": "2026-07-18T12:34:56", "last_hit_flow_id": 99,
#  "last_hit_flow": {id: 99, method: "POST", host: "api.x.com", ...}}

# 删除
python -m telnix.cli intercept del <rule_id>
python -m telnix.cli intercept del --ids id1,id2,id3

# 启用/禁用规则（不删除，支持批量）
python -m telnix.cli intercept toggle <rule_id>              # 切换 enabled
python -m telnix.cli intercept toggle <rule_id> --enable     # 强制启用
python -m telnix.cli intercept toggle <rule_id> --disable    # 强制禁用
python -m telnix.cli intercept toggle --all --disable        # 批量禁用所有规则
# {"toggled": true, "rule_id": "...", "enabled": false}

# 修改现有规则（不删除重建）
python -m telnix.cli intercept update <rule_id> --note '新备注'
python -m telnix.cli intercept update <rule_id> --match 'host~=api2.example.com' --action 'set-json k v'
python -m telnix.cli intercept update <rule_id> --enable     # 同时启用
python -m telnix.cli intercept update <rule_id> --disable --note '临时停用'
# {"updated": true, "rule_id": "...", "fields": ["note", "enabled"]}
```

**规则匹配优先级**：按 `pattern` 长度降序，更具体的规则优先。多条规则可能都匹配，只执行第一条命中的。

- `intercept list`：返回的每条规则含 `id` 和 `rule_id` 两个字段（值相同），agent 用 `rule_id` 提取更语义化。**命中统计字段**（`hit_count`/`last_hit_at`/`last_hit_flow_id`）默认输出，代理层匹配成功时自增。`--with-stats` 是语义化 flag（不加也输出统计字段，保持兼容）。**filter 字段**（`method_filter`/`status_filter`/`pid_filter`/`process_filter`）有值时是字符串（逗号分隔多值），无值时是 `null`（不是空字符串 `""`）。agent 判断有无 filter 用 `is not None` 而非 `!= ""`。
- `intercept hits <rule_id>`：显示某规则的命中统计（`hit_count`/`last_hit_at`/`last_hit_flow_id`）+ 最后命中的流量详情（`last_hit_flow`，如果 `last_hit_flow_id` 有值则获取该流量完整 JSON）。后端只存最后一次命中的 flow_id（不保留完整命中历史），评估规则近期效果用这个。规则从未命中时 `hit_count=0`/`last_hit_flow_id=null`/`last_hit_flow=null`。
- `intercept toggle`：不删除规则即可启用/禁用。`--all` 批量操作所有规则（配合 `--enable`/`--disable`）。适合"临时停用某条规则但不想删，后面再启用"场景。
- `intercept update`：修改现有规则的 match/action/note/enabled。不传的字段保持原值。比 `del + add` 更方便（保留 rule_id）。
- **method/status/pid/process 过滤现已代理层生效**（§4.1 陷阱已修复）：`--match 'host~=x && method=POST && status=200 && pid=1234 && process=chrome.exe'` 中，`method`/`status`/`pid`/`process` 会自动提取到规则的 `method_filter`/`status_filter`/`pid_filter`/`process_filter` 字段（逗号分隔多值），代理层 `find_matching_rule` 校验这些字段——非空时必须匹配才命中规则。`--dry-run` 输出的 `would_create` 也会显示这些 filter 字段（非空时）。详见 §4。

### 3.5 `replay` — 重放流量（支持改参数）

```bash
# 原样重放
python -m telnix.cli replay 42
# {"replayed": true, "id": 42}

# 改参数重放（覆盖 method/host/port/body/headers）
python -m telnix.cli replay 42 --method PUT --host test.example.com --port 8443
python -m telnix.cli replay 42 --body '{"msg":"modified"}' --header 'X-Test: 1'

# fuzz 批量重放（遍历 JSON body 里某字段的值范围）
python -m telnix.cli replay 42 --fuzz 'user_id=1..100'
# {"results": [...], "count": 100}  # 每个值重放一次，返回所有响应

# 多字段组合 fuzz（cartesian 笛卡尔积 / zip 配对）
python -m telnix.cli replay 42 --fuzz-file payloads.json --mode cartesian
# payloads.json: {"user_id": [1,2,3], "role": ["admin","user"], "sig": ["a","b"]}
# cartesian: 3×2×2=12 次；zip: 3 次（取最短长度）

# 批量重放 N 次并对比响应差异（测服务端是否返回固定结果 / 是否有随机字段）
python -m telnix.cli replay 42 --repeat 5 --compare
# {"replayed": true, "id": 42, "count": 5, "results": [...],
#  "compare": [{"run": 1, "identical": true, "diff": ""}, ...]}

# 并发重放（测服务端并发限制/竞态）
python -m telnix.cli replay 42 --repeat 20 --parallel 5
# {"replayed": true, "id": 42, "count": 20, "parallel": 5, "results": [...]}
# 5 线程并发重放 20 次，results 按 index 排序

# 慢接口 / 长连接：自定义超时（默认带 body 120s，无 body 30s）
python -m telnix.cli replay 42 --timeout 300
```

覆盖参数：

| 参数 | 说明 |
|---|---|
| `--method M` | 覆盖 HTTP 方法 |
| `--host H` | 重定向到其他 host |
| `--port P` | 覆盖端口 |
| `--url U` | 覆盖完整 URL（含 scheme/path/query） |
| `--body B` | 覆盖请求体 |
| `--header K:V` | 覆盖/新增请求头（可多次，`--header K:V1 --header K:V2` 同 key 后者覆盖前者，如需多值合并 `--header 'K: V1, V2'`） |
| `--fuzz 'key=start..end'` | 批量重放：遍历 JSON body 中 `key` 字段从 start 到 end（上限 500） |
| `--fuzz-file FILE` | 多字段组合 fuzz：payloads.json 路径，格式 `{field: [values...]}`（与 `--fuzz` 互斥，优先用 `--fuzz-file`） |
| `--mode cartesian\|zip` | `--fuzz-file` 组合模式：`cartesian`=笛卡尔积（默认），`zip`=按最短长度配对 |
| `--repeat N` | 重复重放 N 次（默认 1），返回 `results` 数组含每次响应 |
| `--parallel N` | 并发重放线程数（默认 1=串行，>1 用 `ThreadPoolExecutor` 并发）。配合 `--repeat` 用，测服务端并发限制/竞态 |
| `--compare` | 配合 `--repeat`，对比多次响应差异（以第一次为基准做 unified diff，JSON body 自动格式化） |
| `--timeout N` | HTTP 请求超时秒数（默认：带 body 120s，无 body 30s）。慢接口可调大，快接口可调小避免长时间等待 |

重放不走拦截规则，直接发到目标服务器。

- `--repeat N --compare`：适合验证"同一请求多次重放，响应是否一致"。`compare` 数组每项含 `run`（第几次）、`identical`（是否与第一次完全相同）、`diff`（unified diff 文本）。响应里有随机 token / 时间戳 / nonce 时 diff 会标出变化行。
- `--fuzz-file`：多字段组合 fuzz。`cartesian` 模式做笛卡尔积（`{"a":[1,2],"b":["x","y"]}` → 4 次），`zip` 模式按最短长度配对（→ 2 次）。每次替换 JSON body 中对应字段（支持点分路径 `a.b.c`）后重放，返回 `results` 数组含每次的 `fields`/`status`/`duration`/`size`。适合爆破/签名碰撞/多参数组合测试。
- `--parallel N`：并发重放（`ThreadPoolExecutor`）。`results` 按 `index` 排序（并发完成顺序可能乱）。单条失败不影响其他（`ok: false` + `error`）。测服务端并发限制/竞态条件用这个。

### 3.6 `export` — 导出会话

```bash
python -m telnix.cli export \
  [--session 7] \
  --format har|json|csv|python-requests|postman|curl \
  [-o evidence.har]
# {"exported": true, "path": "evidence.har", "format": "har"}
```

| 格式 | 说明 | 默认扩展名 |
|---|---|---|
| `har` | 浏览器 DevTools 通用格式（JSON） | `.har` |
| `json` | 原始流量 JSON | `.json` |
| `csv` | CSV 表格（12 字段：id/timestamp/method/host/path/url/status_code/duration_ms/size/process_name/pid/protocol） | `.csv` |
| `python-requests` | 生成 Python 脚本，用 `requests` 库重放所有请求 | `.py` |
| `postman` | Postman Collection v2.1 JSON，可直接导入 Postman | `.json` |
| `curl` | bash 脚本，每条流量一个 curl 命令 | `.sh` |

- 不指定 `-o` 时按 format 自动推导文件名（如 `--format csv` → `Telnix_export.csv`）。
- **Windows 二进制 body 兼容**：当 body 是二进制（`base64:` 前缀）且指定了 `-o` 文件时，Windows 上会额外生成 `<stem>_body.bin` 二进制文件，主文件用引用方式而非内联管道：
  - `--format curl`：生成 `<stem>_body.bin`，curl 命令用 `--data-binary @<stem>_body.bin` 引用（不再用 `echo | base64 -d |` Unix 管道）。
  - `--format python-requests`：生成 `<stem>_body.bin`，脚本里用 `open('req_body.bin','rb')` 读取后传给 `requests.post(..., data=...)`。
  - `--format postman`：用 file 模式引用 `<stem>_body.bin`。
  - 不指定 `-o` 输出到 stdout 时，Windows 仍用 Unix 管道（需 bash 环境，如 Git Bash/WSL），此时会打 stderr 警告提示"二进制 body 用管道需 bash 环境，Windows 建议用 -o 指定文件"。
- **`packets export`（单 flow 导出，§3.3）同理**：`packets export 42 --format curl -o req.sh` 在 Windows 上若 body 是二进制，会额外生成 `req_body.bin`，规则同上。

### 3.7 `proxy` — 系统代理开关

> **平台支持**：全平台可用。Windows 用 winreg 写注册表，macOS 用 `networksetup`，Linux GNOME 用 `gsettings`，KDE 用 `kwriteconfig5`。

```bash
python -m telnix.cli proxy status   # 查状态
python -m telnix.cli proxy on       # 开系统代理（让所有软件走 Telnix）
python -m telnix.cli proxy off      # 关系统代理（不影响规则，规则走 SSL bump 仍生效）
```

输出：`{"system_proxy_on": true, "proxy_host": "127.0.0.1", "proxy_port": 8888}`

### 3.8 `cert` — 证书管理

> **平台支持**：`cert status` 全平台；`cert install/remove` 在 Windows/macOS 可用（macOS 用 `security add-trusted-cert`，安装时会弹窗要求密码授权），**Linux 需手动安装**（不同发行版命令不同，详见 [README.md 平台支持矩阵](README.md)）。

```bash
python -m telnix.cli cert status    # {"installed": true}
python -m telnix.cli cert install   # 装根证书到系统信任库（首次必做）✅ Win/mac ⚠️ Linux 需手动
python -m telnix.cli cert remove    # 从系统信任库卸载根证书 ✅ Win/mac ⚠️ Linux 需手动
```

**HTTPS 解密前置条件**：`cert_installed=true`。没装证书的话，HTTPS 流量只能看到 CONNECT 但解不开 payload，拦截规则也不会生效。`remove` 用于卸载证书（如换机器或不再使用 Telnix 时清理）。

> **Linux 手动安装证书**：
> ```bash
> # Debian / Ubuntu
> sudo cp <data_dir>/certs/telnix_root.crt /usr/local/share/ca-certificates/telnix_root.crt
> sudo update-ca-certificates
> # RHEL / CentOS
> sudo trust anchor <data_dir>/certs/telnix_root.crt
> ```
> `<data_dir>` 默认是 `~/.telnix`，可用 `python3 -c "from telnix.config import get_cert_dir; print(get_cert_dir())"` 查询。

### 3.9 `log` — 日志

```bash
# 查看日志（NDJSON）
python -m telnix.cli log tail \
  [--level ERROR|WARNING|INFO|DEBUG] \
  [--category proxy|ai|settings] \
  [--limit 100]

# 清空日志
python -m telnix.cli log clear
# {"cleared": true}

# 导出日志为 JSONL 文件（可带过滤条件）
python -m telnix.cli log export -o logs.jsonl
python -m telnix.cli log export -o error_logs.jsonl --level ERROR --category proxy
python -m telnix.cli log export --keyword "证书"    # 不指定 -o 则输出到 stdout
```

- `log tail`：查看日志。`category=proxy` 看抓包/规则匹配细节，`category=ai` 看 AI 调用。
- `log clear`：清空所有日志。
- `log export`：导出日志为 JSONL 格式（每行一个 JSON 对象）。支持 `--level`/`--category`/`--keyword` 过滤。`-o` 指定文件路径，不指定则输出到 stdout。

### 3.10 `raw` — TCP/UDP 原始抓包

> **平台支持**：全平台可用。Windows 用 WinDivert，macOS 用 BPF（`/dev/bpfN`），Linux 用 AF_PACKET（`SOCK_RAW`）。需管理员/root 权限。`install` 仅 Windows 需要（安装 pydivert），mac/linux 自动跳过（无第三方依赖）。

网络层抓包，独立于 HTTP 代理。能抓非 HTTP 协议（Steam P2P、protobuf、自定义 TCP 协议等）。需管理员/root 权限。

```bash
# 查状态（管理员/root？后端？在跑？）
python -m telnix.cli raw status
# {"running": false, "is_admin": true, "pydivert_installed": false,
#  "backend": "windivert", "supported": true,
#  "hint": "运行: python -m telnix.cli raw install"}

# 安装 pydivert（仅 Windows 需要，调后端 pip install，驱动 WinDivert64.sys 随包附带）
# mac/linux 自动跳过（使用原生内核接口，无需 pydivert）
python -m telnix.cli raw install
# {"installed": true, "output": "Successfully installed pydivert..."}

# 启动（需先 capture start 拿到 session_id）
python -m telnix.cli capture start
python -m telnix.cli raw start [--pid 1234] [--port 443] [--filter 'tcp or udp']
# {"running": true}

# 停止
python -m telnix.cli raw stop
# {"running": false}
```

- `raw status` 返回字段：`is_admin`（是否管理员/root）、`pydivert_installed`（pydivert 是否已装，mac/linux 恒 false）、`running`（是否在抓）、`backend`（`windivert`/`af_packet`/`bpf`）、`supported`（恒 true）。
- `raw install`：仅 Windows 需要，调后端安装 pydivert。mac/linux 自动跳过（使用 AF_PACKET/BPF 原生接口）。
- `--pid`：只抓指定 PID 的包（可多次）。
- `--port`：只抓指定端口的包（可多次）。
- `--filter`：过滤表达式（Windows 用 WinDivert BPF 语法，默认 `tcp or udp`）。
- 抓到的包存入 flows 表，`protocol=tcp/udp`，`method=SEND/RECV`，`raw_data=base64:...`。
- 用 `packets list --protocol tcp` 查看，用 `packets get <id> --hex --field raw_data` 看 hex dump。
- **SNIFF 模式不断网**：只嗅探不拦截，包正常流转。默认排除 8888/18901 端口和环回地址。

**首次使用提示（Windows）**：`pydivert_installed=false` 时直接 `raw install`。WinDivert64.sys 驱动由 pydivert 自带，首次运行会自动加载。mac/Linux 无需安装任何依赖。

**WinDivert 风险提示（仅 Windows，首次启用未确认时拦截）**：`raw start` 入口会先调 `check_windivert_ack_or_block()` 检查 `settings.json` 的 `windivert_warning_acknowledged`，未确认时返回 `403 + need_ack=true`，CLI 自动走原生弹窗流程（见 [§3.21.3](#3213-system-windivert-warning--windivert-风险提示查询确认)），用户选「是」后立即 ack=1 并自动重试 `raw start`；选「否」则 CLI stderr 输出 `{"ok": false, "rejected_by_user": true, ...}` + 退出码 1。已确认后所有 WinDivert 相关端点直接放行不再弹窗。重置为未确认：`settings set -k windivert_warning_acknowledged -v 0`。macOS/Linux 使用原生内核接口，无此风险提示。

### 3.11 `focus` — 专注模式（只抓指定进程/host，跨类 OR 匹配）

```bash
# 查状态
python -m telnix.cli focus status
# {"enabled": false, "pids": [], "hosts": []}

# 按 PID 专注
python -m telnix.cli focus on --pid 1234

# 按进程名专注（自动解析 PID + 子进程）
python -m telnix.cli focus on --name chrome.exe
python -m telnix.cli focus on --name chrome.exe --no-children  # 不含子进程

# 按 host 通配符专注（只抓指定域名的包，其他直接放行）
python -m telnix.cli focus on --host '*.example.com'
python -m telnix.cli focus on --host '*.example.com,api.*.com'  # 逗号分隔多个

# 进程 + host 组合（跨类 OR 匹配：满足任一即记录/拦截）
python -m telnix.cli focus on --name chrome.exe --host '*.google.com'

# 关闭
python -m telnix.cli focus off
```

- `--name`：按进程名专注，后端用 **psutil** 解析所有同名进程 PID（不依赖 wmic，Win11 24H2+ 兼容）。
- `--no-children`：默认包含子进程（psutil BFS 递归找所有后代），加此参数排除。
- `--host`：按 host 通配符专注（`*`→`.*` `?`→`.`，大小写不敏感）。逗号分隔多个。
- **跨类 OR 匹配**：pid 和 host 满足任一专注条件即记录/拦截；都不满足则直接放行不记录（哪怕有断点也是）。专注列表都为空时不拦截（避免开专注但啥都没选导致全部放行）。
- 专注模式开启后，非专注进程/host 的流量直接放行不记录。

### 3.12 `breakpoint` — 断点控制（支持超时自动放行 + 批量 release）

```bash
# 查状态
python -m telnix.cli breakpoint status
# {"break_on_request": false, "break_on_response": false,
#  "timeout_seconds": 0.0, "pending": [], "pending_flows": []}

# 开请求断点（可选超时，超时后自动放行避免连接卡死）
python -m telnix.cli breakpoint on --type request --timeout 30
# {"break_on_request": true, "timeout_seconds": 30.0,
#  "hint": "断点 request 已开启，30.0s 未放行自动 release", ...}

# 开响应断点
python -m telnix.cli breakpoint on --type response --timeout 30

# 关闭
python -m telnix.cli breakpoint off --type request

# 单独改超时（不影响开关状态）
python -m telnix.cli breakpoint timeout --timeout 60

# 放行单条被拦截的流量
python -m telnix.cli breakpoint release 42
# {"flow_id": 42, "action": "release"}

# 丢弃单条（模拟连接断开，客户端会收到错误）
python -m telnix.cli breakpoint drop 42
# {"flow_id": 42, "action": "drop"}

# 批量放行/丢弃所有 pending 断点（agent 退出前清理用）
python -m telnix.cli breakpoint release --all
# {"action": "release", "total": 3, "released": 3}
python -m telnix.cli breakpoint drop --all
```

- `--type request|response`：断点类型，不传默认 `request`。
- `--timeout N`：**强烈建议 always 设置**。断点开启后，被拦截的流量会阻塞等待 agent `release`；如果 agent 忘了 release，连接会永久卡死。设了超时后，N 秒未放行自动 release，并在日志里打 warning。
- `pending`：当前被拦住的流量列表，每项 `{"flow_id": N, "waiting_seconds": M}`。agent 可看 `waiting_seconds` 判断"卡了多久"。
- `release <flow_id>`：放行单条被拦截的流量（走 `POST /flows/{id}/release`）。
- `drop <flow_id>`：丢弃单条（action=drop，模拟连接断开）。
- `release --all` / `drop --all`：批量操作所有 pending 断点（agent 退出前清理推荐用这个，避免遗留卡死连接）。
- **agent 反模式警告**：开全局请求断点 + 不设超时 + 忘 release → 所有流量卡死，后端无法响应。务必带 `--timeout`，或在退出前 `breakpoint release --all`。

### 3.13 `processes` — 进程列表 / 忽略进程·host 管理

逆向 agent 找目标 PID + 看进程当前 TCP 连接用。底层调 `/processes/snapshot`（psutil 实现），失败回退到 `/processes`。不带子命令时列出进程，带子命令时做忽略管理。

```bash
# 当前所有进程（NDJSON）
python -m telnix.cli processes

# 按进程名过滤（大小写不敏感）
python -m telnix.cli processes --name chrome

# 附带每个进程的当前 TCP 连接（找"谁连着 api.example.com:443"）
python -m telnix.cli processes --name chrome --with-connections
# 每个进程多 "connections": [{"laddr":"...","raddr":"1.2.3.4:443","status":"ESTABLISHED"}, ...]
# 和 "connection_count": N

# 按进程树输出（找父子关系，如 svchost.exe 的子进程）
python -m telnix.cli processes --name svchost.exe --tree
# {"processes": [{"pid":..., "name":"svchost.exe", "children":[...]}], "tree": true, "count": N}

# 输出 JSON 数组
python -m telnix.cli processes --name chrome --json-array
```

#### 忽略进程·host（子命令）

被忽略的进程/host 流量直连不抓（不走代理、不解密 HTTPS、不记录）。适合排除系统后台进程、无关 host 的噪音。

```bash
# 忽略进程（按 PID 或按名称，可添加多个；pid 为空时按名称忽略）
python -m telnix.cli processes ignore --pid 1234
python -m telnix.cli processes ignore --name chrome.exe
python -m telnix.cli processes ignore --pid 1234 --name chrome.exe   # 组合

# 取消忽略进程（按行 id，先 processes ignored 看 id）
python -m telnix.cli processes unignore 3

# 列出已忽略进程
python -m telnix.cli processes ignored
# {"id": 3, "pid": 1234, "process_name": "chrome.exe", "ignored_at": "..."}

# 忽略 host（通配符，如 *.example.com）
python -m telnix.cli processes ignore-host --host "*.example.com"

# 取消忽略 host（按行 id）
python -m telnix.cli processes unignore-host 2

# 列出已忽略 host
python -m telnix.cli processes ignored-hosts
# {"id": 2, "host_pattern": "*.example.com", "ignored_at": "..."}
```

| 参数 | 说明 |
|---|---|
| `--name N` | 按进程名过滤（大小写不敏感，模糊包含匹配） |
| `--with-connections` | 附带每个进程的当前 TCP 连接（laddr/raddr/status），找"谁在连某服务器"用 |
| `--tree` | 按进程树输出，每个进程含 `children` 数组，找父子关系用 |
| `--include-listen` | `--with-connections` 时包含 LISTEN 状态连接（默认跳过，只看 ESTABLISHED 等活动连接）。查"谁监听了某端口"用 |
| `--json-array` | 输出 JSON 数组而非 NDJSON |
| 子命令 `ignore` | 忽略进程：`--pid N`（按 PID）和/或 `--name X`（按名称）。pid 为空时按名称忽略（可添加多个同名规则） |
| 子命令 `unignore <row_id>` | 取消忽略进程（按行 id） |
| 子命令 `ignored` | 列出已忽略进程 |
| 子命令 `ignore-host --host PATTERN` | 添加忽略 host 通配符（如 `*.example.com`） |
| 子命令 `unignore-host <id>` | 取消忽略 host（按行 id） |
| 子命令 `ignored-hosts` | 列出已忽略 host |

- 输出字段：`pid`/`name`/`ppid`/`username`/`cmdline`，`--with-connections` 时多 `connections`/`connection_count`，`--tree` 时多 `children`。
- `--with-connections` 默认跳过 LISTEN 状态只看 ESTABLISHED 等活动连接，加 `--include-listen` 可包含监听端口。
- **忽略生效范围**：对新连接生效。已有 keep-alive 连接需重启后端或重开浏览器才生效（PID 在 CONNECT 建立时反查一次后复用）。
- 用途：① 找目标软件 PID（配合 `focus on --pid`）；② 看某进程连了哪些服务器（配合 `focus on --host`）；③ 排查进程父子关系（找谁拉起了谁）；④ 查谁监听了某端口（`--with-connections --include-listen`）；⑤ 排除噪音进程/host（`ignore`/`ignore-host`）。

### 3.14 `intercept export/import` — 拦截规则导入导出

规则集备份/迁移/复用。导出所有规则到 JSON 文件，导入时可选 merge（追加）或 replace（先清空再导入）。

```bash
# 导出所有规则到文件
python -m telnix.cli intercept export -o my_rules.json
# {"exported": true, "path": "my_rules.json", "count": 5}
# 文件格式：{"rules": [...], "exported_at": "2026-07-16T12:34:56"}

# 导入规则（默认 merge 追加，不去重）
python -m telnix.cli intercept import my_rules.json
# {"imported": true, "mode": "merge", "created": 5, "deleted_old": 0, "total_in_file": 5}

# 导入规则（replace 模式：先清空所有现有规则再导入）
python -m telnix.cli intercept import my_rules.json --mode replace
# {"imported": true, "mode": "replace", "created": 5, "deleted_old": 3, "total_in_file": 5}

# 大批量导入：--quiet 只输出汇总，不输出 results 数组
python -m telnix.cli intercept import big_rules.json --quiet
# {"imported": true, "mode": "merge", "created": 98, "deleted_old": 0,
#  "failed": 2, "total_in_file": 100}
# 默认（不加 --quiet）会输出 "results": [{...}, {...}, ...] 每条规则的导入结果
```

| 参数 | 说明 |
|---|---|
| `export -o FILE` | 导出文件路径（默认 `Telnix_rules.json`） |
| `import FILE` | 规则 JSON 文件路径 |
| `--mode merge\|replace` | `merge`=追加（默认），`replace`=先清空所有现有规则再导入 |
| `--quiet` | 只输出汇总（created/failed/total），不输出 results 数组。大批量导入时精简输出，便于 agent 直接看成功失败总数 |

- 导入时会去掉规则原有的 `id`，让后端生成新 id（避免 id 冲突）。
- `replace` 模式会先 `DELETE` 所有现有规则再导入，适合"用一套规则覆盖"场景。
- 文件格式兼容手工编辑：只要顶层有 `rules` 数组即可，每条规则结构同 `POST /auto-reply/rules` 的 body。
- 典型场景：① 把某次逆向分析的规则集存档复用；② 不同环境间迁移规则；③ 批量编辑规则（导出 → 编辑 JSON → replace 导入）。

### 3.15 `sessions` — 会话管理（list / show / delete）

```bash
# 列出所有会话（含每会话流量数 flow_count）
python -m telnix.cli sessions list
# {"id": 1, "name": "会话 2026-07-16 21:31:28", "started_at": "...", "ended_at": null, "flow_count": 42}
# {"id": 2, ...}

# 查看会话详情（含 flow_count）
python -m telnix.cli sessions show 7
# {"id": 7, "name": "...", "started_at": "...", "ended_at": "...", "flow_count": 42}

# 删除会话（同时删除该会话的所有流量）
python -m telnix.cli sessions delete 7
# {"deleted": true, "session_id": 7, "flows_deleted": 42}
```

- `sessions list`：列出所有会话（NDJSON），未结束的会话 `ended_at` 为 null。**`flow_count` 字段**直接返回每会话的流量数（批量统计，不用逐个 `show` 查询）。
- `sessions show <id>`：会话详情，含 `flow_count`（该会话的流量数）。
- `sessions delete <id>`：删除会话及其所有流量。适合清理旧的测试会话，避免 SQLite 膨胀。
- 底层 API：`GET /sessions`（列表，含 flow_count 批量统计）、`GET /sessions/{id}`（详情）、`DELETE /sessions/{id}`（删除）。

### 3.16 `replay-batch` — 时序回放（按 session 整批重放）

按 session 拉取全部流量，按 `timestamp` 升序排序后整批重放。可选保留原始时间间隔（测服务端限流/风控），或并发立即重放。

```bash
# 立即连续重放整个会话（串行）
python -m telnix.cli replay-batch --session 7
# NDJSON，每行一个重放结果：
# {"index": 0, "flow_id": 42, "ok": true, "status": 200, "duration": 456}
# {"index": 1, "flow_id": 43, "ok": true, "status": 200, "duration": 120}

# 按原始时间间隔重放（测服务端限流/风控，强制串行）
python -m telnix.cli replay-batch --session 7 --preserve-timing
# 每条请求之间 sleep 原始 timestamp 差值（单次最多 sleep 60s 防卡死）

# 并发立即重放（测服务端并发处理，真流式输出）
python -m telnix.cli replay-batch --session 7 --parallel 5
# 5 线程并发重放，用 as_completed 完成一个输出一个（不按提交顺序阻塞等前面的）
# {"index": 1, "flow_id": 43, "ok": true, "status": 200, "duration": 120}   # 先完成的先输出
# {"index": 0, "flow_id": 42, "ok": true, "status": 200, "duration": 456}   # 慢的后输出

# 选择性重放（客户端过滤后重放，过滤表达式见 §4）
python -m telnix.cli replay-batch --session 7 --filter 'method=POST'  # 只重放 POST 请求
# 输出含 filtered_count 字段表示被过滤掉的数量
```

**输出模式**（三种模式都是流式）：
- **stdout**：每条重放结果流式 NDJSON（完成一条输出一条，`flush=True`），不等全部完成。
- **stderr**：最后输出一行汇总 `{"replayed": true, "session": 7, "count": N, "filtered_count": M}`，不干扰 stdout 的 NDJSON 流。agent 解析时分开读 stdout/stderr。
- **并发模式**用 `concurrent.futures.as_completed`（§3.2 真流式），先完成的先输出，不按提交顺序阻塞。串行/`--preserve-timing` 模式按顺序流式输出。

| 参数 | 说明 |
|---|---|
| `--session S` | 会话 ID（必填） |
| `--preserve-timing` | 按原始时间间隔 sleep 后重放（强制串行，`--parallel` 无效）。单次最多 sleep 60s 防卡死 |
| `--parallel N` | 并发线程数（默认 1=串行；`--preserve-timing` 时无效）。并发用 `as_completed` 真流式输出 |
| `--filter 'expr'` | 客户端过滤表达式（见 §4），过滤后的流量再重放。输出含 `filtered_count` 字段表示被过滤掉的数量 |

- 拉取 session 所有流量（`limit=50000`），按 `timestamp` 升序排序。
- 每条流量原样重放（不修改参数，走 `/flows/{id}/replay`）。
- `--preserve-timing`：计算相邻流量 timestamp 差值，sleep 后重放。适合复现真实用户操作时序，测服务端限流/风控/防重放。
- `--filter 'expr'`：客户端过滤后重放。表达式语法同 `--match`（§4），如 `method=POST`、`host~=api.example.com`、`status=200`。适合"只重放会话里的 POST 请求，跳过静态资源"场景。输出含 `filtered_count` 字段表示被过滤掉的数量。**大 session 优化**：`replay-batch` 会先 `GET /sessions/{sid}/flows?limit=50000` 拉全量再客户端过滤，session 流量过万时网络/内存开销大。此时可先用 `packets list --session S --filter 'expr' --json-array` 拿到 flow id 列表，再循环 `replay <id>` 单条重放，避免一次性拉全量。
- 单条失败不影响其他（`ok: false` + `error`）。
- 与 `replay --repeat` 区别：`replay --repeat` 是同一流量重放 N 次；`replay-batch` 是整个 session 不同流量各重放一次。

### 3.17 规则模板库

内置常见规则模板，一键应用到指定 match 表达式。适合快速 mock 失败/解锁 VIP/绕过付费等常见场景。

**CLI 用法**（推荐，agent 友好）：

```bash
# 列出所有内置模板
python -m telnix.cli intercept template list
# NDJSON：{"name":"mock-404","description":"返回 404 Not Found 响应","action_spec":{...}}
#         {"name":"unlock-vip","description":"改响应体 is_vip=true",...}

# 应用模板（创建规则）
python -m telnix.cli intercept template apply unlock-vip \
  --match '*api.example.com*/vip*' --note '解锁VIP测试'
# {"created":true,"rule_id":"abc123","template":"unlock-vip","pattern":"*api.example.com*/vip*"}

# 应用带过滤字段的模板
python -m telnix.cli intercept template apply mock-404 \
  --match '*api.example.com*/user*' --method-filter GET,POST --note 'mock 404 测试'

# 创建为禁用状态（稍后手动启用）
python -m telnix.cli intercept template apply slow-response \
  --match '*api.example.com*/api*' --disabled
```

**HTTP API 用法**（curl 兜底，CLI 已封装推荐用 CLI）：

```bash
# 列出所有模板
curl http://127.0.0.1:18901/api/templates
# {"code":0,"data":[{"name":"mock-404","description":"返回 404 Not Found 响应","action_spec":{...}}, ...]}

# 应用模板（创建规则）
curl -X POST http://127.0.0.1:18901/api/templates/unlock-vip/apply \
  -H "Content-Type: application/json" \
  -d '{"pattern":"*api.example.com*/vip*","note":"解锁VIP测试"}'
# {"code":0,"data":{"created":true,"rule_id":"abc123","template":"unlock-vip","pattern":"*api.example.com*/vip*"}}
```

内置模板：

| 模板名 | 说明 | action |
|---|---|---|
| `mock-404` | 返回 404 Not Found 响应（不转发到服务器） | `mock` status=404 |
| `mock-500` | 返回 500 Internal Server Error 响应 | `mock` status=500 |
| `strip-auth` | 删除请求 Authorization 头 | `modify_request` |
| `unlock-vip` | 改响应体 `is_vip=true`（全局字段替换） | `modify_response` |
| `bypass-pay` | 改响应体 `price=0`（全局字段替换） | `modify_response` |
| `slow-response` | 延迟 5 秒响应（`target=delay`） | `modify_response` |

**ApplyTemplate body**：

```json
{
  "pattern": "*api.example.com*/vip*",
  "match_mode": "wildcard",
  "note": "规则备注",
  "enabled": true,
  "method_filter": "",
  "status_filter": "",
  "pid_filter": "",
  "process_filter": ""
}
```

- `pattern`：URL 匹配 pattern（wildcard 通配符，如 `*api.example.com*/vip*`）。
- `method_filter`/`status_filter`/`pid_filter`/`process_filter`：可选覆盖模板的过滤字段（§4.1，逗号分隔多值）。
- 应用后立即生效（规则写入 DB + 刷新缓存）。

### 3.18 环境快照 snapshot（HTTP API）

导出/导入当前环境状态（所有规则 + focus 设置 + 断点设置）。适合多目标切换、规则集备份、环境复现。

```bash
# 导出当前环境状态
curl http://127.0.0.1:18901/api/snapshot -o state.json
# state.json: {"rules":[...], "focus":{...}, "breakpoint":{...}, "exported_at":"..."}

# 导入环境状态（默认清空现有规则后导入）
curl -X POST http://127.0.0.1:18901/api/snapshot \
  -H "Content-Type: application/json" \
  -d @state.json
# {"code":0,"data":{"rules_imported":5,"rules_cleared":3,"focus_set":true,"breakpoint_set":true}}
```

**导出格式**（`GET /snapshot`）：

```json
{
  "rules": [{...完整规则对象，mock_headers/modify_rules 为对象...}],
  "focus": {"enabled": true, "pids": [...], "hosts": [...], "methods": [...], ...},
  "breakpoint": {"break_on_request": false, "break_on_response": false, "breakpoint_timeout": 0.0},
  "exported_at": "2026-07-17T12:34:56"
}
```

**导入 body**（`POST /snapshot`）：

```json
{
  "rules": [...],
  "focus": {...},
  "breakpoint": {...},
  "clear_rules": true
}
```

- 所有字段可选，未提供则不修改对应部分。
- `clear_rules: true`（默认）：先清空所有现有规则再导入；`false`：追加。
- 规则导入时 `hit_count`/`last_hit_at`/`last_hit_flow_id` 重置，id 冲突时自动生成新 id。
- 单条规则导入失败不影响其他（静默跳过）。
- focus 设置：直接传 `pids`/`hosts`/`methods`/`status_codes`/`content_types`（`process_names` 字段保留但不在此解析，需前端单独调 focus API 转换）。
- 与 `intercept export/import` 区别：`intercept export/import` 只管规则（JSON 文件）；`snapshot` 管规则 + focus + 断点（完整环境状态）。

### 3.19 流量分组 groups（HTTP API）

将多条流量归组管理（收藏夹/分析分组）。分组只存 flow_ids 引用，不复制流量。

```bash
# 创建分组
curl -X POST http://127.0.0.1:18901/api/flows/groups \
  -H "Content-Type: application/json" \
  -d '{"name":"login-flow","flow_ids":[42,43,44]}'
# {"code":0,"data":{"id":1,"name":"login-flow","flow_ids":[42,43,44]}}

# 列出所有分组
curl http://127.0.0.1:18901/api/flows/groups

# 查看分组详情（含每条 flow 的完整详情）
curl http://127.0.0.1:18901/api/flows/groups/1
# {"code":0,"data":{"id":1,"name":"login-flow","flow_ids":[42,43,44],"flows":[{...},{...},{...}]}}

# 更新分组（改名 / 改 flow_ids）
curl -X PUT http://127.0.0.1:18901/api/flows/groups/1 \
  -H "Content-Type: application/json" \
  -d '{"name":"login-flow-v2","flow_ids":[42,43,44,45]}'

# 删除分组（不删除组内的流量）
curl -X DELETE http://127.0.0.1:18901/api/flows/groups/1
```

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/flows/groups` | 创建分组，body: `{name, flow_ids: []}` |
| GET | `/flows/groups` | 列出所有分组 |
| GET | `/flows/groups/{id}` | 查看分组（含 flow 详情） |
| PUT | `/flows/groups/{id}` | 更新分组，body: `{name?, flow_ids?}` |
| DELETE | `/flows/groups/{id}` | 删除分组（不删流量） |

- 分组只存 flow_ids 引用（逗号分隔字符串），删除分组不影响流量。
- `GET /flows/groups/{id}` 返回 `flows` 数组（每条 flow 的完整详情），方便一次性查看分组内所有流量。
- 适合逆向分析时把"登录流程""支付流程""可疑签名请求"等分类管理。

### 3.20 导入流量 import（HTTP API）

支持导入 JSON（Telnix 原生导出）和 HAR（HTTP Archive 1.2 标准）格式的流量数据。自动创建新会话，将所有流量插入。

```bash
# 导入 Telnix 导出的 JSON
curl -X POST http://127.0.0.1:18901/api/import \
  -H "Content-Type: application/json" \
  -d '{"format":"json","content":"<文件内容字符串>","session_name":"导入测试"}'
# {"code":0,"data":{"session_id":8,"session_name":"导入测试","imported":42,"total":42}}

# 导入 HAR（浏览器 DevTools 导出的 .har 文件）
curl -X POST http://127.0.0.1:18901/api/import \
  -H "Content-Type: application/json" \
  -d '{"format":"har","content":"<HAR 文件内容字符串>"}'
```

**ImportRequest body**：

```json
{
  "format": "json",
  "content": "文件内容字符串",
  "session_name": "可选会话名，空则自动生成"
}
```

- `format`：`json`（默认）| `har`。
- JSON 格式支持两种：`{session: {...}, flows: [...]}` 或纯 `[...]` 数组。
- HAR 格式：解析 `log.entries`，每条 entry 含 `request`/`response`/`headers`/`postData`/`content`，自动转 Telnix flow 结构。
- 自动创建新会话，插入所有流量。单条失败不阻塞其他。
- 返回 `{session_id, session_name, imported, total}`。
- 前端 UI 在抓包页/全局分析页/搜索页都提供导入按钮，按文件扩展名（`.json`/`.har`）自动判断格式。
- 适合导入历史抓包数据做分析、导入浏览器 DevTools 导出的 HAR 做比对。

### 3.21 `system` — 系统控制（重启 / 退出 / 管理员重启 / 平台能力）

> **平台支持**：全平台可用。`restart/quit/install-dep` 全平台；`restart-as-admin` 全平台（Windows 用 UAC，macOS 用 osascript，Linux 用 pkexec/sudo）；`platform-capabilities` 全平台。

```bash
# 重启前后端服务（Windows 用 ShellExecuteW，mac/linux 用 subprocess.Popen）✅ 全平台
python -m telnix.cli system restart
# {"restarting": true}

# 退出 Telnix（关闭前后端 + 清系统代理）✅ 全平台
python -m telnix.cli system quit
# {"quitting": true}

# 以管理员身份重启（用于 TCP/UDP 抓包等需管理员的功能）✅ 全平台
python -m telnix.cli system restart-as-admin
# 用户同意：{"restarting": true, "as_admin": true, "approved": true, "message": "用户已批准..."}
# 用户拒绝：stderr 输出 {"ok": false, "error": "...", "rejected_by_user": true, ...}，退出码 1

# 查看平台能力矩阵（各功能的后端和支持状态）✅ 全平台
python -m telnix.cli system platform-capabilities
# 返回各能力（raw_capture/transparent_proxy/dns_hijack/system_proxy/admin_elevation 等）
# 的 supported/backend/needs_admin/admin_hint 信息
```

- `system restart`：重启前后端。用于修改后端代码或配置后生效，或后端异常时恢复。重启前会自动关闭系统代理。重启后保留原启动参数（如 `--no-browser`）。
- `system quit`：完全退出 Telnix。清系统代理 + 关闭服务。agent 收尾时用。
- `system restart-as-admin`：以管理员身份重启，走 GUI 用户确认流程：
  1. CLI 调 `POST /system/request-admin-restart` 创建 pending 请求
  2. 后端弹提权确认窗口（Windows: 原生 MessageBox 置顶弹窗；macOS: osascript 系统密码弹窗；Linux: pkexec PolicyKit 弹窗或 sudo）
  3. 用户同意 → 后端调对应平台提权机制 → CLI 收到 `{"approved": true, ...}`
  4. 用户拒绝 → CLI stderr 输出 `{"ok": false, "rejected_by_user": true, ...}` + 退出码 1
  5. CLI 长轮询最多 3 分钟，超时也按拒绝处理
  - 提权时透传原启动参数（`--no-browser` 等），管理员进程行为一致
  - agent 可通过 `rejected_by_user: true` 字段区分"用户主动拒绝"和"超时/错误"
- `system platform-capabilities`：返回当前平台各网络层功能的支持状态和后端信息，agent 可据此判断哪些功能可用及需要什么权限。
- 底层 API：
  - `POST /system/restart`、`POST /system/quit`、`POST /system/clear-proxy`、`POST /system/enable-proxy`（全平台，mac/linux 用 networksetup/gsettings/kwriteconfig5）
  - `POST /system/restart-as-admin`（兼容旧接口，直接提权不经 GUI 确认；全平台，Windows 用 UAC，macOS 用 osascript，Linux 用 pkexec/sudo）
  - `POST /system/request-admin-restart` + `GET /system/admin-request/{id}/wait` + `POST /system/admin-request/{id}/respond`（新 GUI 确认流程，全平台）
  - `GET /system/platform-capabilities`（返回平台能力矩阵，全平台）
  - `POST /system/install-dep` + `GET /system/install-dep/status` + `POST /system/install-dep/cancel`（可选依赖在线安装，全平台 pip install）

#### 3.21.1 `system install-dep` — 在线安装可选依赖（如 mitmproxy）

```bash
# 触发 pip install mitmproxy（异步任务，命令立即返回）
python -m telnix.cli system install-dep
# {"status": "running", "package": "mitmproxy"}
# stderr: [Telnix] 安装任务已启动，使用 `telnix system install-dep-status` 查询进度

# 指定包名（默认 mitmproxy）
python -m telnix.cli system install-dep --package mitmproxy

# 查询安装进度（轮询直到 status=success 或 failed）
python -m telnix.cli system install-dep-status
# {"status": "success", "package": "mitmproxy", "return_code": 0,
#  "mitmproxy_available": true, "log": "..."}
# stderr: [Telnix] mitmproxy 安装成功（已可切换为代理引擎），重启 Telnix 后生效
```

- `system install-dep`：异步任务，立即返回 `status=running`，需轮询 `install-dep-status` 查询进度
- `system install-dep-status`：返回 `{status, package, log, return_code, mitmproxy_available}`：
  - `status=success` 且 `package=mitmproxy` 时额外返回 `mitmproxy_available` 字段
  - 若安装成功但当前进程未加载（`mitmproxy_available=false`），返回 `note` 字段提示需重启 Telnix 才能生效
- **典型工作流**：`install-dep` → 轮询 `install-dep-status` 直到 `success` → `settings engine mitmproxy`（切换引擎） → `system restart`（重启让引擎生效）
- agent 用法：第一次调用 `system_install_dep`，然后用 1.5s 间隔轮询 `system_install_dep_status` 直到 `status != running`，最后根据 `mitmproxy_available` 决定是否提示重启

#### 3.21.2 `settings` — 设置管理（get / set / engine）

```bash
# 读取所有设置（NDJSON）
python -m telnix.cli settings get
# {"data_path": "...", "proxy_engine": "builtin", "mitmproxy_available": false, ...}
# stderr: [Telnix] 当前全部设置（key=value）：
#           data_path = ...
#           proxy_engine = builtin
#           ...

# 读单个 key
python -m telnix.cli settings get -k proxy_engine
# {"key": "proxy_engine", "value": "builtin"}

# 写入单个 key（bool/数字/list/dict 自动反序列化）
python -m telnix.cli settings set -k auto_scroll -v true
python -m telnix.cli settings set -k flow_columns -v '["method","status","host"]'

# 查看当前代理引擎
python -m telnix.cli settings engine
# {"proxy_engine": "builtin", "mitmproxy_available": false,
#  "available_engines": ["async", "builtin", "mitmproxy"]}
# stderr: [Telnix] 当前代理引擎: builtin
#         [Telnix] mitmproxy 可用: 否（未安装可执行 telnix system install-dep）
#         [Telnix] 可选引擎: builtin（默认线程）/ async（asyncio）/ mitmproxy（需 pip install mitmproxy）
#         [Telnix] 切换示例: telnix settings engine async
#         [Telnix] 切换后需执行: telnix system restart

# 切换引擎（builtin → async）
python -m telnix.cli settings engine async
# {"ok": true, "proxy_engine": "async", "previous": "builtin",
#  "mitmproxy_available": false, "hint": "需重启后端才生效：telnix system restart"}
# stderr: [Telnix] 代理引擎已切换: builtin → async
#         [Telnix] 重要：需重启后端才生效，执行: telnix system restart

# 切换到 mitmproxy（未安装时报错）
python -m telnix.cli settings engine mitmproxy
# {"ok": false, "error": "mitmproxy 未安装，无法切换到该引擎",
#  "hint": "先执行 `telnix system install-dep` 安装 mitmproxy，再切换引擎"}
# stderr: [Telnix] 错误：mitmproxy 未安装，无法切换到该引擎
#         [Telnix] 修复建议：先执行 `telnix system install-dep` 安装 mitmproxy，再切换引擎
# 退出码 1
```

- `settings get [-k KEY]`：读取所有设置或单个 key。返回完整设置 dict（含 `data_path`、`mitmproxy_available` 等注入字段）
- `settings set -k KEY -v VALUE`：写入单个设置项。`VALUE` 会自动尝试 JSON 反序列化：
  - `"true"` / `"false"` → bool
  - `"123"` → int
  - `"[1,2,3]"` → list
  - `'{"k":"v"}'` → dict
  - 其他 → 字符串
- `settings engine [NAME]`：查看或切换代理引擎：
  - 省略 NAME → 仅查看当前引擎、mitmproxy 可用性、可选引擎列表
  - 指定 NAME → 切换引擎。`mitmproxy` 未安装时返回 `{"ok": false, ...}` + 退出码 1，并给出 `system install-dep` 修复建议
  - **切换后必须 `system restart`** 才能让新引擎加载到当前进程
- 底层 API：`GET /settings` + `PUT /settings`（任意 key 写入，list/dict 自动 JSON 序列化、bool 转 1/0）

#### 3.21.3 `system windivert-warning` — WinDivert 风险提示查询/确认

> **背景**：WinDivert64.sys 是 Windows 内核驱动，Telnix 用它做 TCP/UDP 抓包 / 透明代理 / DNS 劫持。该驱动常被漏洞利用工具使用，部分杀毒软件（360 / 火绒 / Windows Defender）可能将其作为"漏洞驱动"拦截或报警。**首次启用相关功能前必须让用户知情同意**，确认后写入 `settings.json` 的 `windivert_warning_acknowledged=1` 永久不再提示。

```bash
# 查询 WinDivert 风险提示状态（needed / ack / message / brief / platform）
python -m telnix.cli system windivert-warning-status
# {"needed": true, "ack": false, "platform": "win32",
#  "message": "即将启用的功能需要加载 WinDivert64.sys 内核驱动...\n是否确认开启？",
#  "brief": "即将加载 WinDivert64.sys 内核驱动...不会对您的设备带来安全隐患。"}
# stderr: [Telnix] WinDivert 风险提示状态:
#         needed = true   # 是否需要提示（仅 Windows + 未确认时为 true）
#         ack    = false  # 当前是否已确认
#         platform = win32

# 永久确认（标记 ack=1，后续所有 WinDivert 相关端点直接放行不再弹窗）
python -m telnix.cli system windivert-warning-ack
# {"ack": true, "msg": "已确认 WinDivert 风险提示，后续不再提示"}
# stderr: [Telnix] 已确认 WinDivert 风险提示，后续不再提示
```

- `system windivert-warning-status`：查询状态。`needed=true` 表示 Windows 平台且未确认，下次 `raw start` / `transparent-proxy start` / `dns-hijack start` 会被拦截。
- `system windivert-warning-ack`：永久确认（写入 `settings.json`）。**agent 主动场景**慎用——一般应让用户在弹窗里选择，而非 agent 直接调 ack 跳过。
- **重置为未确认**：`settings set -k windivert_warning_acknowledged -v 0`（下次启用相关功能时再次弹窗）。

**触发流程**（`raw start` / `transparent-proxy start` 自动处理，agent 无需手动调本组子命令）：

1. CLI 调 `POST /raw/start` 等端点 → 后端检测 `windivert_warning_acknowledged != 1` → 返回 `403 + need_ack=true`
2. CLI 的 `_req_with_windivert_ack` 自动调 `POST /system/request-windivert-ack` 创建 pending 请求 + 弹**原生 Windows 置顶 Yes/No 弹窗**（`MB_TOPMOST | MB_SYSTEMMODAL | MB_SETFOREGROUND`，任务栏图标闪烁，默认聚焦「否」按钮防误按）
3. 用户点「是」→ 后端立即 ack=1 持久化 → CLI 长轮询 `GET /system/windivert-ack-request/{rid}/wait` 收到 `status=accepted` → 自动重试原请求
4. 用户点「否」→ CLI 收到 `status=rejected` → stderr 输出 `{"ok": false, "rejected_by_user": true, "error": "用户拒绝了 WinDivert 风险提示", ...}` + 退出码 1
5. 用户 3 分钟无响应 → CLI stderr 输出 `{"ok": false, "error": "等待用户响应 WinDivert 风险提示超时（3 分钟无响应）", ...}` + 退出码 1，提示「可在 GUI 设置页确认，或请用户在场后重试」

**MCP 等价工具**：`system_windivert_warning_status` / `system_windivert_warning_ack`（参数与返回同 CLI），触发流程由 `_api_with_windivert_ack` 自动处理。

**底层 API**：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/system/windivert-warning` | 查询是否需要提示 + 风险说明文本 + 当前 ack 状态 |
| POST | `/api/system/windivert-warning/ack` | 标记为已确认（永久不再提示，GUI 用） |
| POST | `/api/system/request-windivert-ack` | 创建 pending ack 请求 + 弹原生 MessageBox（CLI/MCP 用，非 Windows/已 ack 时返回 `skipped=true`） |
| GET | `/api/system/windivert-ack-request/{rid}/wait` | 长轮询等待用户响应（60s 超时返回 `status=pending`，CLI/MCP 最多重试 3 次覆盖 3 分钟） |

### 3.22 `send` — 从零发包（Composer）

构造任意 HTTP 请求并发送，不走代理、不写入 flows 表。适合测试接口、调试 API、验证服务端响应。前端 GUI 在「发包」页提供同样功能。

```bash
# 基础 GET 请求
python -m telnix.cli send --url https://api.example.com/v1/users
# {"status_code": 200, "reason": "OK", "size": 1234, "duration_ms": 456,
#  "response_headers": "...", "response_body": "...",
#  "request": {"method": "GET", "url": "...", "headers": {...}, "body": null}}

# POST 带 JSON body
python -m telnix.cli send --method POST --url https://api.example.com/v1/users \
  --header 'Content-Type: application/json' \
  --body '{"name":"alice","age":30}'

# 从文件读 body（二进制/大 body 用）
python -m telnix.cli send --method POST --url https://api.example.com/upload \
  --body-file payload.bin --header 'Content-Type: application/octet-stream'

# 自定义超时（默认 30s）
python -m telnix.cli send --url https://slow.example.com --timeout 60

# 只输出响应头（不打印 body，适合只看状态码/头）
python -m telnix.cli send --url https://api.example.com --headers-only

# 只输出响应体（纯文本，方便管道处理）
python -m telnix.cli send --url https://api.example.com/text --body-only

# 导出 curl 命令（不实际发送）
python -m telnix.cli send --method POST --url https://api.example.com \
  --header 'X-Test: 1' --body '{"k":1}' --emit-curl
# 输出: curl -X POST 'https://api.example.com' -H 'X-Test: 1' -d '{"k":1}'
```

| 参数 | 说明 |
|---|---|
| `--url URL` | 目标 URL（必填，含 scheme） |
| `--method M` | HTTP 方法（默认 `GET`，可选 GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS） |
| `--header K:V` | 请求头（可多次，格式 `Key: Value`） |
| `--body STR` | 请求体字符串 |
| `--body-file PATH` | 从文件读请求体（与 `--body` 互斥，优先用 `--body-file`） |
| `--timeout N` | 超时秒数（默认 30） |
| `--headers-only` | 只输出响应头 |
| `--body-only` | 只输出响应体（纯文本） |
| `--emit-curl` | 只输出等效 curl 命令，不实际发送 |

- **独立 socket**：`send` 不走 Telnix 代理，直接 `socket.create_connection` 连接目标，不写入 flows 表，不影响抓包数据。
- **不依赖抓包状态**：无需 `capture start` 即可发包，适合快速测试单个接口。
- **前端 GUI**：发包页（路由 `/send`）提供同样功能，支持历史记录（localStorage 存最近 50 条）、模板保存、cURL 导入/导出、表单持久化（切换页面不丢失）。
- 底层 API：`POST /send`，body `{method, url, headers, body, timeout}`，返回 `{status_code, reason, response_headers, response_body, size, duration_ms, request}`。

---

### 3.23 `agent` — agent 工作模式（保留原状，end 不关代理/不退出）

agent 接管会话前的"清场"工具：临时关闭所有影响抓包/拦截的因素（自动回复规则、专注模式、断点），并把 agent 自身进程加入忽略列表（防止抓自己的包），完成工作后恢复用户原状。**注意：`agent end` 不关闭系统代理、不退出 Telnix**——因为用户可能依赖 Telnix 的自动修改规则继续工作，agent 应询问用户后再决定是否调 `system quit`。

```bash
# 1. 开工前：保存原状并清空工作区
python -m telnix.cli agent start
# → 备份当前所有规则 + focus 设置 + 断点设置到临时文件
# → 禁用所有自动回复规则、关闭 focus、关闭断点
# → 把 agent 进程（如 TRAE SOLO CN.exe）加入忽略列表，避免抓自己的包
# → 返回 backup_path 供排查

# 2. 做事：抓包 / 重放 / 改包 / 发包 ...
python -m telnix.cli capture start
python -m telnix.cli packets list --tail
python -m telnix.cli replay 42 --body '{"x":1}'
python -m telnix.cli send --url https://api.example.com/v1/login --method POST --body '{"user":"a","pass":"b"}'

# 3. 收工：从备份恢复原状（不关代理、不退出）
python -m telnix.cli agent end
# → 规则按备份原样恢复（enabled 状态保留）
# → focus 设置恢复
# → 断点设置恢复
# → 移除 agent start 时添加的忽略进程（保留用户原本就忽略的进程）
# → 删除备份文件
# → 系统代理保持开启、Telnix 继续运行
# → 返回 hint：询问用户是否关闭 Telnix

# 4.（可选）询问用户后退出
# 用户同意 → 调 system quit（后端退出时 atexit 自动清理系统代理）
# 用户拒绝 → 啥也不做，Telnix 保持运行（用户可继续使用自动修改规则）
```

**子命令**：

| 子命令 | 作用 |
|---|---|
| `start` | 保存原状（snapshot）→ 禁用所有规则 + 关 focus + 关断点 + **把 agent 进程加入忽略列表**（默认含 `TRAE SOLO CN.exe`，源码 `AGENT_IGNORE_PROCESSES`）。**重复 start 会报错**（避免覆盖未恢复的备份） |
| `end` | 从备份恢复 rules + focus + 断点（`clear_rules=true` 先清空再导入，避免 agent 工作期间新建的规则残留）→ **移除 agent start 时添加的忽略进程**（保留用户原本忽略的）→ 删除备份。**不关代理、不退出 Telnix**（用户可能还要用自动修改规则）。**未 start 就 end 会报错** |
| `status` | 查询工作区状态：`active`（有备份）/ `inactive`（无备份），含备份内的规则数、原 focus/断点状态 |

**agent 忽略进程列表**（源码 `cli.py: AGENT_IGNORE_PROCESSES`）：
- `TRAE SOLO CN.exe` — 当前 agent 进程（防止抓自己的包）

`agent start` 时若列表中的进程已在用户忽略列表中，则跳过（不重复添加），但 `end` 时只移除 `start` 实际新增的（不会误删用户原本忽略的进程）。

**备份文件**：系统临时目录下 `Telnix_agent_workspace_backup.json`（跨进程保留，重启后端不影响）。格式与 `GET /snapshot` 一致，多一个 `agent_added_ignored_processes` 字段记录本次 start 新增的忽略进程名，可手动查看/编辑。

**返回字段**：

```json
// agent start
{"agent_workspace": "started", "backup_path": "...", "rules_disabled": 3,
 "focus_cleared": true, "breakpoint_cleared": true,
 "ignored_processes_added": ["TRAE SOLO CN.exe"],
 "exported_at": "...",
 "hint": "工作区已清空：规则已禁用、focus 已关闭、断点已关闭、agent 进程已加入忽略列表。完成工作后请调 `agent end` 恢复原状。"}

// agent end（注意 system_proxy_cleared=false，Telnix 保持运行）
{"agent_workspace": "ended", "restored": true, "rules_restored": 3,
 "focus_set": true, "breakpoint_set": true,
 "ignored_processes_removed": 1,
 "system_proxy_cleared": false,
 "hint": "工作区已恢复到 agent start 之前的状态。注意：自动修改规则需要 Telnix 运行才生效，因此系统代理未关闭、Telnix 未退出。请询问用户是否关闭 Telnix；用户同意后再调 `system quit`（后端退出时会自动清理系统代理）。"}

// agent status (active)
{"agent_workspace": "active", "backup_path": "...", "rules_in_backup": 3,
 "focus_was_enabled": false, "break_on_request_was_on": true, ...}

// agent status (inactive)
{"agent_workspace": "inactive", "hint": "没有活跃的 agent 工作区"}
```

**典型 agent 工作流**：

```
agent start
├─ 自动加入忽略列表：TRAE SOLO CN.exe（防止抓自己的包）
├─ capture start
├─ <do work: packets list / replay / send / intercept add ...>
├─ capture stop
agent end                ← 恢复原状 + 移除 agent 添加的忽略进程，Telnix 继续运行
└─ 询问用户是否关闭 Telnix
   ├─ 同意 → system quit  ← 后端 atexit 自动清代理
   └─ 拒绝 → 保留运行（用户可继续用自动修改规则）
```

**注意事项**：
- `agent start` 后即使后端重启，备份文件仍在，重启后 `agent end` 仍可恢复
- `agent end` **不会关代理、不会退出 Telnix**——因为用户可能依赖自动修改规则继续工作
- 若 agent 异常退出未调 `end`，下次 `agent start` 会报错"已有未恢复的备份"，需先 `agent end` 恢复
- `agent start` 不影响 `capturing` 状态（抓包是否记录独立于规则/focus/断点）
- **关闭 Telnix 的正确方式**：agent 询问用户同意后调 `system quit`，后端退出时 atexit 钩子自动清系统代理，无需 agent 手动清理
- **忽略进程的语义**：`agent start` 添加的忽略进程只在 `agent end` 时移除；若用户在 agent 工作期间手动调 `proxy ignore --name TRAE SOLO CN.exe` 重复添加，`end` 也会按备份记录的进程名匹配移除（可能误删用户手动加的同名项，建议 agent 工作期间不要重复添加同名进程）
- **agent 进程改名/换 IDE**：若 agent 改用其他 IDE（如 VSCode），需更新 `cli.py` 中的 `AGENT_IGNORE_PROCESSES` 列表

---

### 3.24 `transparent-proxy` — 透明代理控制（网络层重定向）

> **平台支持**：全平台可用。Windows 用 WinDivert NAT，macOS 用 pf rdr anchor，Linux 用 iptables REDIRECT。需管理员/root 权限。

把出站 HTTP(80)/HTTPS(443) 流量在网络层重定向到本地代理端口，**应用无需配置系统代理即可被抓包**。适合抓那些不走系统代理的 Electron 应用、命令行工具、原生 socket 客户端。HTTPS 走 raw TCP 隧道（端到端 TLS，不解密），如需解密 HTTPS 仍需配合证书。

与 `proxy on`（系统代理）互补：系统代理处理已配置的客户端，透明代理处理无代理感知的客户端，两者可并存。

```bash
# 查看状态（running/redirected_count/nat_table_size/last_error/supported/backend/is_admin）
python -m telnix.cli transparent-proxy status

# 启动（需管理员/root 权限）
python -m telnix.cli transparent-proxy start

# 停止
python -m telnix.cli transparent-proxy stop
```

**返回字段**：

```json
// status
{"running": false, "supported": true, "backend": "windivert",
 "is_admin": false, "redirected_count": 0,
 "nat_table_size": 0, "last_error": "", "local_port": 8888,
 "redirect_ports": [80, 443],
 "hint": "需要管理员权限。请运行: python -m telnix.cli system restart-as-admin"}
```

**注意事项**：
- `start` 失败时 `last_error` 会标明原因；若提示需要管理员权限，先调 `system restart-as-admin` 以管理员身份重启后端
- `backend` 字段标明当前后端：`windivert`（Windows）/ `iptables`（Linux）/ `pf`（macOS）
- Windows 用 NAT 表做反向流量映射；macOS/Linux 由内核处理反向流量，无需 NAT 表
- 透明代理不修改系统代理设置，对应用完全透明，难以被探测
- 用完务必 `stop`，避免重定向规则残留影响网络

---

### 3.25 `auto-reply` — 自动修改规则管理（list/get/create/enable/disable/delete）

与 `intercept` 互补，专注 **Python 脚本规则**管理。`create` 支持通过 `--script-path` 从本地 .py 文件加载脚本内容（agent 友好，无需内联大段源码），脚本在独立 worker 子进程运行，定义 `on_request(ctx)` / `on_response(ctx)` 实现复杂改包逻辑（动态签名、条件分支、多字段联动）。

```bash
# 列出所有规则（NDJSON，含命中统计；--json-array 输出 JSON 数组）
python -m telnix.cli auto-reply list
python -m telnix.cli auto-reply list --json-array

# 查看规则详情
python -m telnix.cli auto-reply get <id>

# 创建规则（action=script，从本地 .py 文件加载脚本）
python -m telnix.cli auto-reply create \
  --pattern '*api.example.com/v1/*' \
  --action script \
  --script-path my_rule.py \
  --note '动态签名改包'

# 创建规则（action=script，内联源码；与 --script-path 互斥）
python -m telnix.cli auto-reply create \
  --pattern '*api.example.com/v1/*' \
  --action script \
  --script 'def on_request(ctx): ctx["body"]["sig"]="xxx"; return ctx' \
  --note '内联脚本'

# 创建规则（非 script 动作，用 --action-spec 复用 intercept add 的语法）
python -m telnix.cli auto-reply create \
  --pattern '*api.example.com/login*' \
  --action mock \
  --action-spec 'mock 200 {"ok":true}' \
  --note 'mock 登录响应'

# 创建规则（带过滤条件，避免误命中）
python -m telnix.cli auto-reply create \
  --pattern '*api.example.com/v1/*' \
  --action script \
  --script-path sign.py \
  --method-filter POST \
  --status-filter 200 \
  --process-filter chrome.exe \
  --note '只改 chrome 的 POST 200'

# 启用/禁用/删除规则
python -m telnix.cli auto-reply enable <id>
python -m telnix.cli auto-reply disable <id>
python -m telnix.cli auto-reply delete <id>
```

**`create` 参数**：

| 参数 | 说明 |
|---|---|
| `--pattern` | URL 匹配 pattern（必填，如 `*api.example.com/v1/*`） |
| `--action` | 动作类型（必填）：`script`/`mock`/`modify_response`/`modify_request`/`mock_request` |
| `--script-path` | `action=script` 时：从本地 .py 文件加载脚本内容（agent 友好，与 `--script` 互斥） |
| `--script` | `action=script` 时：内联 Python 脚本源码（与 `--script-path` 互斥） |
| `--action-spec` | 非 script 动作时：动作规范字符串（如 `'set-json key value'` / `'mock 200 {}'`），复用 `intercept add` 语法 |
| `--match-mode` | 匹配模式 `wildcard`/`exact`/`regex`（默认 `wildcard`） |
| `--note` | 规则备注 |
| `--method-filter` | 方法过滤（逗号分隔，如 `POST,PUT`） |
| `--status-filter` | 状态码过滤（逗号分隔，如 `200,201`） |
| `--pid-filter` | PID 过滤 |
| `--process-filter` | 进程名过滤 |
| `--disabled` | 创建为禁用状态（默认启用） |

**脚本接口约定**：`on_request(ctx)` / `on_response(ctx)` 接收 ctx 字典（含 headers/body/url/method/status 等），返回修改后的 ctx 或 `None`（不修改）。脚本出错时规则自动跳过，不影响代理转发。

**返回字段**：

```json
// create
{"created": true, "rule_id": 12, "pattern": "*api.example.com/v1/*", "action": "script"}

// list（每条规则）
{"id": 12, "rule_id": 12, "pattern": "*api.example.com/v1/*", "action": "script",
 "enabled": true, "hit_count": 3, "last_hit_at": "...", "last_hit_flow_id": 42, ...}
```

**与 `intercept` 的区别**：
- `intercept add` 通用，支持所有动作类型，参数化创建（`--match` 表达式 / `--action` 规范字符串）
- `auto-reply create` 专注脚本规则，**`--script-path` 从文件加载**是核心差异，方便 agent 把复杂逻辑写到 .py 文件再引用

---

### 3.26 `dns-hijack` — DNS 劫持控制

> **平台支持**：全平台可用。Windows 用 WinDivert 拦截 UDP 53，macOS/Linux 用 pf/iptables 重定向到本地 DNS 服务（`127.0.0.1:5354`）。需管理员/root 权限。

将指定域名的 DNS 解析结果劫持到指定 IP，用于测试 API 切换环境、模拟 DNS 污染等场景。

```bash
# 查看状态（enabled/running/rules/default_ip/stats/backend/is_admin）
python -m telnix.cli dns-hijack status

# 启动 DNS 劫持（需管理员/root）
python -m telnix.cli dns-hijack start --rules "api.example.com=10.0.0.1,cdn.example.com=10.0.0.2" --default-ip 127.0.0.1

# 更新规则（运行中也可更新）
python -m telnix.cli dns-hijack rules --rules "api.example.com=10.0.0.1" --default-ip 127.0.0.1

# 停止
python -m telnix.cli dns-hijack stop

# 清空劫持日志
python -m telnix.cli dns-hijack clear-log
```

**规则格式**：`domain=ip,domain=ip,...`，支持 `*.example.com` 通配符。`--default-ip` 指定未匹配规则的域名默认指向的 IP（可选）。

**返回字段**：

```json
// status
{"enabled": true, "running": true, "backend": "windivert",
 "is_admin": true, "rules": [{"domain": "api.example.com", "ip": "10.0.0.1"}],
 "default_ip": "127.0.0.1", "stats": {"total_queries": 42, "hijacked": 15}}
```

**注意事项**：
- `backend` 字段：`windivert`（Windows）/ `iptables`（Linux）/ `pf`（macOS）
- macOS/Linux 后端启动本地 DNS 服务在 `127.0.0.1:5354`，通过 iptables/pf 将 UDP 53 重定向到该端口
- 匹配域名返回构造的 A 记录响应；AAAA 查询返回空响应强制 IPv4 回退；未匹配域名转发到上游 DNS（带 60s TTL 缓存）
- 用完务必 `stop`，避免 DNS 重定向规则残留

---

## 4. 匹配表达式语法（`--match`）

格式：`key op value [ && key op value ...]`

| key | 说明 | 示例值 |
|---|---|---|
| `host` | 域名 | `api.example.com` |
| `method` | HTTP 方法 | `POST` |
| `path` | URL 路径 | `/api/v1/chat` |
| `url` | 完整 URL | `https://api.example.com/v1/chat` |
| `status` | 响应码 | `200` |
| `pid` | 进程 PID | `1234` |
| `process` | 进程名 | `chrome.exe` |

| op | 含义 |
|---|---|
| `=` | 精确相等 |
| `~=` | 通配符匹配（`*`→`.*`，`?`→`.`） |
| `!=` | 不等 |
| `>=` `<=` `>` `<` | 数值比较（status/pid 用） |

**示例**：

```
host~=api.example.com && method=POST && path~=/api/v1/*
host~=*.google.com && status>=400
process=chrome.exe && method!=GET
```

**注意**：表达式会被翻译成后端的 URL 通配符 `pattern`（如 `host~=x && path~=/y` → `*x*/y*`）。`method`/`status`/`pid`/`process` 字段会额外提取到规则的 `method_filter`/`status_filter`/`pid_filter`/`process_filter` 字段（逗号分隔多值），代理层 `find_matching_rule` 会校验这些字段——**非空时必须匹配才命中规则**。

> ✅ **§4.1 陷阱已修复：method/status/pid/process 过滤现在代理层生效**
>
> v4 之前规则匹配只看 URL，`intercept add --match 'host~=x && method=POST'` 里的 `method=POST` 只在 `--dry-run` 客户端预览时生效，真正创建的规则后端代理层不检查 method，同 URL 的 GET/PUT/DELETE 都会被命中改包。
>
> **现已修复**：
> - 规则结构新增 `method_filter`/`status_filter`/`pid_filter`/`process_filter` 字段（逗号分隔多值，空=不过滤）
> - 代理层 `find_matching_rule` 接收完整请求上下文（method/status_code/pid/process_name），非空 filter 必须匹配才命中
> - CLI `intercept add --match 'host~=x && method=POST && status=200'` 会自动把 `method=POST`→`method_filter="POST"`、`status=200`→`status_filter="200"` 写入规则
> - `--dry-run` 输出的 `would_create` 里也会显示这些 filter 字段（非空时），agent 可确认后端会做这些过滤
> - 响应阶段会重新匹配带 `status_filter` 的 `modify_response` 规则（请求阶段拿不到 status_code，响应阶段补匹配）
>
> **限制**：
> - `!=`（不等）暂不支持后端 filter，只做客户端过滤（dry-run 时生效，真规则不检查）
> - 数值比较（`>=`/`<=`/`>`/`<`）同理只做客户端过滤
> - `status_filter` 仅对 `modify_response` 规则有意义（请求阶段无 status_code）；`modify_request` 规则带 `status_filter` 不会在请求阶段命中
> - 多值用逗号分隔：`method=POST,PUT` → `method_filter="POST,PUT"`（满足任一即匹配）

**示例**：

```
host~=api.example.com && method=POST && path~=/api/v1/*
# → pattern: *api.example.com*/api/v1/*, method_filter: POST

host~=*.google.com && status>=400
# → pattern: *.google.com*, status 只在客户端过滤（>= 不是 =/~=）

process=chrome.exe && method!=GET
# → pattern: *, process_filter: chrome.exe, method!=GET 只在客户端过滤
```

---

## 5. 动作规范语法（`--action`）

格式：`<name> <args...>`，支持 shlex 引号。

### 5.1 改响应（最常用）

| 动作 | 说明 | 示例 |
|---|---|---|
| `set-json <key> <value>` | 全局搜索 JSON 响应体里所有同名 key，替换值 | `set-json remainingUses 99999` |
| `set-json-path <path> <value>` | 精确路径定位（含 `.` 和 `[n]`） | `set-json-path data.list[0].count 5` |
| `remove-json <key>` | 全局删除同名 key | `remove-json debug_info` |
| `remove-json-path <path>` | 精确路径删除 | `remove-json-path data.internal` |
| `replace-header <K> <V>` | 改/增响应头 | `replace-header X-Custom hello` |

**`set-json` vs `set-json-path`**：
- 字段名不含 `.` → 全局递归替换所有同名字段（如 `remainingUses` 会在数组、嵌套对象里全改）
- 含 `.` → 精确定位（如 `data.status.remainingUses`）

**值类型自动识别**：`true`/`false`/`null`/数字会自动转 JSON 类型，其余当字符串。要强制字符串用引号：`set-json flag "yes"`。

### 5.2 mock 响应（不走服务器）

| 动作 | 说明 | 示例 |
|---|---|---|
| `mock <code> [body]` | 直接返回 code + body | `mock 200 {"ok":true}` |
| `status <code>` | 返回 code + 空 body | `status 404` |
| `drop` | 返回 503 + 空 body（模拟失败） | `drop` |
| `mock-request <body> [ctype]` | 写死请求 body 转发到真实服务器，返回真实响应 | `mock-request '{"u":1}' application/json` |

mock 的 `Content-Type` 默认 `application/json`。

**`mock-request` vs `mock`**：
- `mock`：完全 mock 响应，请求不转发到服务器，客户端直接拿到 mock 的 code+body。
- `mock-request`：**只 mock 请求体**，请求仍然转发到真实服务器，拿到的是服务器的真实响应。适合"测试服务器对特定请求体的处理"——比如把请求 body 改成构造好的 payload 看服务器怎么回，但不改其他字段（url/method/headers 保持原样）。

底层实现：规则的 `action=mock_request`、`mock_body`（写死的请求体）、`mock_headers`（默认 `{"Content-Type": "application/json"}`，可用第二个参数覆盖）。代理层匹配到该规则后，把请求 body 替换为 `mock_body` 再转发。

```bash
# 写死请求 body 转发到服务器，看真实响应
python -m telnix.cli intercept add \
  --match 'host~=api.target.com && path~=/login' \
  --action 'mock-request {"username":"test","password":"abc"}' \
  --name 'fixed-login'

# 指定 Content-Type（默认 application/json）
python -m telnix.cli intercept add \
  --match 'host~=api.target.com && path~=/upload' \
  --action "mock-request '<xml>data</xml>' 'application/xml'" \
  --name 'fixed-xml'
```

### 5.3 改请求（转发前篡改）

| 动作 | 说明 | 示例 |
|---|---|---|
| `set-request-header <K> <V>` | 改/增请求头 | `set-request-header X-Test 1` |
| `set-request-json <key> <value>` | 全局改请求体 JSON 同名字段 | `set-request-json user_id 999` |
| `set-request-json-path <path> <value>` | 精确路径改请求体 JSON | `set-request-json-path data.uid 999` |
| `remove-request-json <key>` | 全局删除请求体 JSON 字段 | `remove-request-json debug` |
| `set-request-body-hex <hex>` | 替换整个请求体为 hex 字节 | `set-request-body-hex 0001ff` |
| `replace-request-bytes <offset>:<hex>` | 二进制偏移替换请求体 | `replace-request-bytes 10:ff00` |

改请求在转发到目标服务器**之前**执行。适合伪造请求头、改请求参数测试服务端校验。

### 5.4 二进制响应操作

| 动作 | 说明 | 示例 |
|---|---|---|
| `replace-bytes <offset>:<hex>` | 按偏移替换响应体字节 | `replace-bytes 100:414243` |
| `replace-bytes-regex <regex> <hex>` | 正则匹配响应体字节并替换 | `replace-bytes-regex 'efbbbf' ''` |

用于非 JSON 的二进制协议（protobuf、自定义协议）。`hex` 为空表示删除匹配字节。

### 5.5 延迟注入（测客户端竞态/超时）

| 动作 | 说明 | 示例 |
|---|---|---|
| `delay <ms>` | 响应延迟 N 毫秒（modify_response，代理层 `time.sleep`） | `delay 5000` |
| `delay-request <ms>` | 请求延迟 N 毫秒（modify_request，转发前 sleep） | `delay-request 2000` |

```bash
# 响应延迟 5 秒（测客户端超时处理）
python -m telnix.cli intercept add \
  --match 'host~=api.target.com && path~=/login' \
  --action 'delay 5000' --name 'slow-login'

# 请求延迟 2 秒（测服务端竞态）
python -m telnix.cli intercept add \
  --match 'host~=api.target.com && path~=/pay' \
  --action 'delay-request 2000' --name 'slow-pay-req'
```

底层实现：规则 `modify_rules` 里 `target=delay`/`delay-request`、`op=sleep`、`value=<ms>`，代理层匹配到该规则后在对应阶段 `time.sleep(value/1000)`。适合测客户端对慢响应/慢请求的处理（鉴权绕过、竞态、超时重试逻辑）。

---

## 6. 典型工作流（agent 抄这个）

不确定用什么命令？先看 [§0 任务→命令决策树](#0-任务命令决策树不知道用什么命令先看这里)。

v9 提供的 6 个逆向实战工作流模板，从"未知软件"到"协议理解+改包验证"的完整路径。agent 拿到 CLI 后直接套用，不用自己摸索命令组合。

### 工作流 1：未知目标软件的 API 地图绘制

**场景**：拿到一个未知软件，想知道它调用了哪些 API。

```bash
# 1. 环境自检
python -m telnix.cli status
python -m telnix.cli cert status    # 证书未安装则 cert install

# 2. 启动抓包（含 auto-stop 防忘关）
python -m telnix.cli capture start --auto-stop 300

# 3. 操作目标软件（agent 触发或提示用户操作）

# 4. 停止抓包
python -m telnix.cli capture stop

# 5. 绘制 API 地图
python -m telnix.cli packets endpoints --limit 0 --keep-query
# 输出每个 endpoint 的 method/host/path_template/count/status_set/sample_ids/query_keys

# 6. 看时间线找关键时序
python -m telnix.cli packets timeline --limit 0

# 7. 标记关键接口
python -m telnix.cli packets tag <sample_id> --add key-api --note "登录接口"

# 8. 查看具体请求
python -m telnix.cli packets get <sample_id>
```

### 工作流 2：字段修改验证（modify_response）

**场景**：想修改某接口响应里的字段，验证客户端行为（如改 remainingUses 测 VIP 解锁）。

```bash
# 1. 先 dry-run 确认规则会匹配到目标流量
python -m telnix.cli intercept add \
  --match 'host~=api.target.com && path~=/api/usage' \
  --action 'set-json data.remainingUses 999' \
  --name 'bump-uses' --dry-run

# 2. 确认 matched_flows 非空后，幂等创建规则
python -m telnix.cli intercept add \
  --match 'host~=api.target.com && path~=/api/usage' \
  --action 'set-json data.remainingUses 999' \
  --name 'bump-uses' --idempotent

# 3. 触发请求（操作软件或重放）
python -m telnix.cli replay <flow_id>

# 4. 看规则是否命中
python -m telnix.cli intercept hits <rule_id>
# 或看日志
python -m telnix.cli log tail --category proxy

# 5. 验证不通过则禁用规则（A/B 对比）
python -m telnix.cli intercept toggle <rule_id> --disable
# 操作软件看原始响应
python -m telnix.cli intercept toggle <rule_id> --enable
# 操作软件看修改后响应

# 6. 用完删除
python -m telnix.cli intercept del <rule_id>
```

### 工作流 3：签名/加密字段定位

**场景**：接口请求里有签名字段（如 `sign=abc123`），想定位它是怎么生成的。

```bash
# 1. 抓多次同接口请求（不同参数）
python -m telnix.cli capture start --auto-stop 60
# 操作软件触发 3-5 次同接口请求（不同参数）

# 2. 用 endpoints 找到目标接口的 sample_ids
python -m telnix.cli packets endpoints --host api.target.com --limit 0

# 3. 用 analyze 自动检测签名字段
python -m telnix.cli packets analyze <id1> <id2> <id3> <id4> --find-signature
# 输出候选字段：[{field_path, length, charset, varies, sample_values}]

# 4. 用 trace 追踪签名是否从其他接口响应传递来
python -m telnix.cli packets trace <id1> --all --min-length 8

# 5. 标记可疑字段
python -m telnix.cli packets tag <id1> --add suspicious --note "sign 字段待分析"
```

### 工作流 4：mock 接口测试客户端容错

**场景**：想测试客户端对各种异常响应的处理（404、500、空 body、超时）。

```bash
# 1. 幂等创建多个 mock 规则
python -m telnix.cli intercept add --match 'path~=/api/user' --action 'mock 404 notfound' --name 'mock-404' --idempotent
python -m telnix.cli intercept add --match 'path~=/api/user' --action 'mock 500 error' --name 'mock-500' --idempotent
python -m telnix.cli intercept add --match 'path~=/api/user' --action 'mock 200 ""' --name 'mock-empty' --idempotent
python -m telnix.cli intercept add --match 'path~=/api/user' --action 'delay 5000' --name 'mock-slow' --idempotent

# 2. 逐个启用测试（确保只启用一条）
python -m telnix.cli intercept toggle --all --disable
python -m telnix.cli intercept toggle <mock-404-id> --enable
# 操作软件观察客户端行为
python -m telnix.cli intercept toggle <mock-404-id> --disable
python -m telnix.cli intercept toggle <mock-500-id> --enable
# ...

# 3. 用完批量删除
python -m telnix.cli intercept toggle --all --disable
python -m telnix.cli intercept del --ids <id1>,<id2>,<id3>,<id4>
```

### 工作流 5：跨会话历史分析

**场景**：之前抓的包想重新分析，不依赖活动会话。

```bash
# 1. 看所有会话
python -m telnix.cli sessions list

# 2. 看某会话概览
python -m telnix.cli sessions show <session_id>

# 3. 跨会话查询流量
python -m telnix.cli packets list-all --host api.target.com --limit 200

# 4. 跨会话搜索
python -m telnix.cli packets search --body-regex 'sig=[a-f0-9]{32}' --header-regex 'Authorization: Bearer .+' --all

# 5. 跨会话 endpoints
python -m telnix.cli packets endpoints --session <old_session_id> --limit 0

# 6. 清理旧数据（保留最近 N 条）
python -m telnix.cli packets list-all --limit 1   # 看最新 id
python -m telnix.cli packets clear --before-id <old_id>
```

### 工作流 6：批量重放测服务端稳定性

**场景**：想重放某接口的请求，测服务端限流/风控/稳定性。

```bash
# 1. 串行重放看每次响应是否一致
python -m telnix.cli replay <flow_id> --repeat 10 --compare
# --compare 输出多次响应的 diff

# 2. 并发重放测限流
python -m telnix.cli replay <flow_id> --repeat 50 --parallel 10
# 10 线程并发，as_completed 真流式输出

# 3. 时序回放整会话测风控
python -m telnix.cli replay-batch --session <id> --preserve-timing
# 按原始时间间隔重放

# 4. 选择性重放（只重放 POST）
python -m telnix.cli replay-batch --session <id> --filter 'method=POST' --parallel 5
```

---

## 7. HTTP API（CLI 底层，复杂场景兜底）

Base URL：`http://127.0.0.1:18901/api`
响应格式：`{"code": 0, "data": <...>, "msg": "..."}`，`code=0` 成功。

### 抓包/状态

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/status` | 抓包/代理/证书/断点状态 |
| POST | `/capture/start` | 开始抓包，返回 `{session_id, capturing}` |
| POST | `/capture/stop` | 停止 |
| POST | `/capture/clear` | 清空当前会话流量 |
| POST | `/capture/pause` | 暂停抓包（保留会话，代理仍跑，不记录新流量） |
| POST | `/capture/resume` | 恢复抓包记录（在原会话继续） |

### 会话/流量

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/sessions` | 会话列表 |
| GET | `/sessions/{id}` | 会话详情（含 flow_count） |
| DELETE | `/sessions/{id}` | 删除会话及其所有流量 |
| GET | `/sessions/{id}/flows?limit=&offset=&host=&method=&status_code=&process=&tag=` | 流量列表（最新在前，支持 tag 过滤） |
| GET | `/flows/{id}` | 单条详情 |
| PATCH | `/flows/{id}` | 修改流量（断点放行时用） |
| DELETE | `/flows/{id}` | 删除单条 |
| POST | `/flows/batch-delete` | body: `{ids: [1,2,3]}` |
| POST | `/flows/{id}/replay` | 重放 |
| POST | `/flows/{id}/release` | 断点放行，body: `{action: "release"\|"drop"}` |
| POST | `/flows/batch-release` | body: `{ids: [...], action: "release"}` |
| PATCH | `/flows/{id}/tags` | 流量标签，body: `{tags: "analyzed,suspicious", tag_note?: "备注"}`（tags 逗号分隔，tag_note 可选备注） |

### 自动回复规则

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/auto-reply/rules` | 列表（含 hit_count/last_hit_at/last_hit_flow_id 命中统计） |
| POST | `/auto-reply/rules` | 创建 |
| POST | `/auto-reply/rules/idempotent` | §3.1 幂等创建：已存在相同签名规则则返回现有 rule_id 不重复创建。body 同 POST /auto-reply/rules |
| POST | `/auto-reply/rules/batch-create` | 批量创建，body: `{rules: [...]}`，一次传数组（比逐条 POST 快） |
| PUT | `/auto-reply/rules/{id}` | 更新 |
| DELETE | `/auto-reply/rules/{id}` | 删除 |
| POST | `/auto-reply/rules/batch-update` | body: `{ids: [...], enabled: bool}` |
| POST | `/auto-reply/rules/batch-delete` | body: `{ids: [...]}` |

**创建规则 body**：

```json
{
  "enabled": true,
  "match_mode": "wildcard",
  "pattern": "*api.example.com*/v1/*",
  "action": "modify_response",
  "mock_status": null,
  "mock_headers": {},
  "mock_body": "",
  "modify_rules": [
    {"target": "response_body", "op": "replace", "key": "remainingUses", "value": 99999}
  ],
  "note": "必填，agent 创建时写明意图",
  "method_filter": "POST",
  "status_filter": "",
  "pid_filter": "",
  "process_filter": "chrome.exe"
}
```

- `match_mode`: `wildcard` | `exact` | `regex`
- `action`: `mock` | `modify_request` | `modify_response`
- `modify_rules[].target`: `request_header` | `request_body` | `response_header` | `response_body` | `delay` | `delay-request`
- `modify_rules[].op`: `replace` | `append` | `remove` | `sleep`（delay 动作用）
- `modify_rules[].key`: 字段名或路径（含 `.` 走精确路径，不含走全局搜索）
- `note`: **必填**（agent 创建时强制写备注）
- `method_filter`/`status_filter`/`pid_filter`/`process_filter`: §4.1 过滤字段（逗号分隔多值，空=不过滤，非空=代理层必须匹配才命中规则）
- 命中统计字段（只读，代理层匹配时自增）：`hit_count`/`last_hit_at`/`last_hit_flow_id`

### 断点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/breakpoint/status` | `{break_on_request, break_on_response, pending_flows}` |
| POST | `/breakpoint/request` | body: `{enabled: bool}` |
| POST | `/breakpoint/response` | body: `{enabled: bool}` |

### 进程

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/processes` | 当前有流量的进程列表 |
| GET | `/processes/snapshot?with_connections=&tree=&name=` | 进程快照（psutil，可选连接快照/进程树/按名过滤），见 §3.13 |
| GET | `/processes/ignored` | 已忽略进程 |
| POST | `/processes/ignore` | body: `{pid: int, name: string}`（pid=-1 表示按名） |
| DELETE | `/processes/ignore/{pid}` | 取消忽略 |

### 证书/系统

> **平台支持**：`/cert/*` 全平台（mac 用 `security`，Linux 需手动）；`/system/enable-proxy` / `/system/clear-proxy` 全平台（Windows 用 winreg，macOS 用 `networksetup`，Linux 用 `gsettings`/`kwriteconfig5`）；`/system/restart` / `/system/quit` 全平台。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/cert/status` | `{installed: bool}` ✅ 全平台 |
| POST | `/cert/install` | 装根证书（mac 用 `security`，Linux 需手动 `update-ca-certificates`）✅ Win/mac ⚠️ Linux |
| POST | `/cert/remove` | 卸载 ✅ Win/mac ⚠️ Linux |
| POST | `/system/enable-proxy` | 开系统代理（Windows 写注册表，macOS 用 networksetup，Linux 用 gsettings/kwriteconfig5）✅ 全平台 |
| POST | `/system/clear-proxy` | 关系统代理（同上）✅ 全平台 |
| POST | `/system/restart` | 重启后端（Windows 用 ShellExecuteW，mac/linux 用 subprocess）✅ 全平台 |
| POST | `/system/quit` | 退出 Telnix ✅ 全平台 |

### 专注模式

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/focus` | `{enabled, pids, process_names, hosts, include_children}` |
| POST | `/focus` | body: `{enabled, pids?, process_names?, hosts?, include_children?}` |

- `process_names` 传入后后端自动用 **psutil** 解析 PID（含子进程，Win11 24H2+ 兼容，不依赖 wmic），返回 `{pids: [...]}`。
- `hosts`：按 host 通配符专注（`*`→`.*` `?`→`.`，大小写不敏感）。跨类 OR 匹配：pid 和 host 满足任一即记录/拦截，都不满足则放行不记录。见 §3.11。

### TCP/UDP 原始抓包（全平台）

> **平台支持**：全平台可用。Windows 用 WinDivert，macOS 用 BPF（`/dev/bpfN`），Linux 用 AF_PACKET（`SOCK_RAW`）。需管理员/root。`install` 仅 Windows 需要（安装 pydivert），mac/linux 自动跳过。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/raw/status` | `{running, is_admin, pydivert_installed, backend, supported, filter?, msg?}` |
| POST | `/raw/install-pydivert` | 用 pip 安装 pydivert（仅 Windows，mac/linux 不需要） |
| POST | `/raw/start` | body: `{pid_filter?, port_filter?, filter_str?}` |
| POST | `/raw/stop` | 停止 TCP/UDP 抓包 |

- `/raw/status` 字段：`is_admin`（是否管理员/root）、`pydivert_installed`（pydivert 是否已装，mac/linux 恒 false）、`running`（是否在抓）、`backend`（`windivert`/`af_packet`/`bpf`）、`supported`（恒 true）。
- SNIFF 模式：只嗅探不拦截，包正常流转，不断网。

### DNS 劫持（全平台，CLI + HTTP API）

> **平台支持**：全平台可用。Windows 用 WinDivert 拦截 UDP 53，macOS/Linux 用 pf/iptables 重定向 + 本地 DNS 服务。需管理员/root。CLI 命令见 [§3.26](#326-dns-hijack--dns-劫持控制)。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/dns-hijack/status` | `{enabled, running, rules, default_ip, stats, is_admin, backend, supported}` |
| POST | `/dns-hijack/start` | 启动 DNS 劫持（需管理员/root，body: `{rules, default_ip}`） |
| POST | `/dns-hijack/stop` | 停止 DNS 劫持 |
| PUT | `/dns-hijack/rules` | 更新劫持规则（body: `{rules, default_ip}`） |
| POST | `/dns-hijack/clear-log` | 清空劫持日志 |

### 搜索/统计/Hex

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/flows/search` | 多条件搜索，body: `{session_id?, body_regex?, binary_hex?, header_regex?, method?, status?, pid?, process?, limit?, offset?}`（条件 AND 组合） |
| GET | `/sessions/{id}/stats` | 分组统计（host/method/status/protocol） |
| GET | `/flows/{id}/hex?field=&offset=&length=` | hex dump（field=request_body\|response_body\|raw_data） |

- `/flows/search` 支持多条件组合（AND）：`body_regex`（body 正则）、`binary_hex`（二进制 hex 搜索）、`header_regex`（header 正则）、`method`/`status`/`pid`/`process`（精确过滤）、`limit`/`offset`（分页）。所有条件可选，非空条件 AND 组合。

### 导入

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/import` | 导入流量，body: `{format: "json"\|"har", content, session_name?}`（见 §3.20） |

### 规则模板

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/templates` | 模板列表（6 个内置：mock-404/mock-500/strip-auth/unlock-vip/bypass-pay/slow-response） |
| POST | `/templates/{name}/apply` | 应用模板创建规则，body: `{pattern, match_mode?, note?, enabled?, method_filter?, status_filter?, pid_filter?, process_filter?}`（见 §3.17） |

### 环境快照

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/snapshot` | 导出当前环境状态（规则 + focus + 断点，见 §3.18） |
| POST | `/snapshot` | 导入环境状态，body: `{rules?, focus?, breakpoint?, clear_rules?}` |

### 流量分组

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/flows/groups` | 创建分组，body: `{name, flow_ids: []}`（见 §3.19） |
| GET | `/flows/groups` | 列出所有分组 |
| GET | `/flows/groups/{id}` | 查看分组（含 flow 详情） |
| PUT | `/flows/groups/{id}` | 更新分组，body: `{name?, flow_ids?}` |
| DELETE | `/flows/groups/{id}` | 删除分组（不删流量） |

### 日志

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/logs?level=&category=&keyword=&limit=&offset=` | 日志列表 |
| DELETE | `/logs` | 清空 |
| GET | `/logs/export?level=&category=&keyword=` | JSONL 导出 |

### 导出

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/export/{session_id}` | body: `{format: "har"\|"json"\|"csv"\|"python-requests"\|"postman"\|"curl"}` |

### 发包（Composer）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/send` | 从零发包，body: `{method, url, headers?, body?, timeout?}`，返回 `{status_code, reason, response_headers, response_body, size, duration_ms, request}`。不走代理、不写入 flows 表。见 §3.22 |

### 系统控制（含管理员重启 GUI 确认流程）

> **平台支持**：全平台。`/system/restart` / `/system/quit` 全平台；`/system/clear-proxy` / `/system/enable-proxy` 全平台（Windows 用 winreg，macOS 用 networksetup，Linux 用 gsettings/kwriteconfig5）；`/system/restart-as-admin` 及 admin-request 系列流程全平台（Windows 用 UAC，macOS 用 osascript，Linux 用 pkexec/sudo）；`/system/platform-capabilities` 全平台。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/system/restart` | 重启后端（Windows 用 ShellExecuteW，mac/linux 用 subprocess，保留启动参数）✅ 全平台 |
| POST | `/system/quit` | 退出 Telnix（清代理+关服务）✅ 全平台 |
| POST | `/system/clear-proxy` | 关闭系统代理（全平台，各平台用不同后端）✅ 全平台 |
| POST | `/system/enable-proxy` | 开启系统代理（全平台，各平台用不同后端）✅ 全平台 |
| POST | `/system/restart-as-admin` | 兼容旧接口：直接提权（不经 GUI 确认）✅ 全平台 |
| POST | `/system/request-admin-restart` | 创建 pending 管理员重启请求，返回 `{request_id}` ✅ 全平台 |
| GET | `/system/admin-request/{id}/wait` | 长轮询等待 GUI 用户响应（60s 超时返回 pending）✅ 全平台 |
| POST | `/system/admin-request/{id}/respond` | GUI 用户响应，body: `{response: "accept"\|"reject"}` ✅ 全平台 |
| GET | `/system/pending-admin-actions` | 列出待确认请求（前端轮询用，返回 `{items: [...]}`）✅ 全平台 |
| GET | `/system/platform-capabilities` | 返回平台能力矩阵（各功能 supported/backend/needs_admin）✅ 全平台 |

**管理员重启 GUI 确认流程**（[§3.21](#321-system--系统控制重启--退出--管理员重启)）：
- `POST /system/request-admin-restart` 创建 pending 后，后端弹提权确认窗口（Windows: 原生 MessageBox 置顶弹窗；macOS: osascript 系统密码弹窗；Linux: pkexec PolicyKit 弹窗或 sudo）
- 用户同意 → 后端调对应平台提权机制 → `GET /admin-request/{id}/wait` 返回 `{status: "accepted"}`
- 用户拒绝 → `GET /admin-request/{id}/wait` 返回 `{status: "rejected"}`
- 提权时透传原启动参数（`--no-browser` 等）

### 设置文件

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings` | 获取所有设置（含 `data_path`、`settings_file_path`） |
| PUT | `/settings` | 更新设置（null 值跳过） |
| POST | `/settings/open-file` | 用记事本打开 settings.json（Windows）/ xdg-open（Linux） |

- 用户设置存 `<data_dir>/settings.json`（原子写入 + 线程锁）。首次启动若 JSON 不存在但 SQLite 有数据会自动迁移。

### AI（通常 agent 不用，留给前端）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/ai/analyze` | body: `{flow_ids: [...]}` 或 `[]` 自由对话 |
| POST | `/ai/chat` | body: `{chat_id, message}` |
| GET | `/ai/chats` | 聊天记录列表 |

---

## 8. agent 注意事项

1. **首次接入先 `status`**：确认后端活着、证书装了、代理开着。三件套缺一不可才能抓 HTTPS。
2. **HTTPS 解密要证书**：`cert_installed=false` 时 HTTPS 只能看到 CONNECT，看不到 payload。先 `cert install`。
3. **规则独立于抓包**：`capturing=false` 时只要证书装了 + 有启用规则，HTTPS 仍会被解密应用规则。所以"加规则"和"抓包"是两件事。
4. **规则按 pattern 长度降序匹配**：更具体的规则优先。加宽泛规则（`*`）会吃掉所有流量，慎用。
5. **`set-json` 全局替换**：字段名不含 `.` 时会递归遍历整个 JSON 树替换所有同名 key。数组里、嵌套对象里都会改。要精确改用 `set-json-path`。
6. **dry-run 先行**：加规则前先 `--dry-run` 看 `matched_flows` 对不对，避免误伤其他请求。
7. **--tail 是阻塞的**：会一直跑直到 Ctrl+C。agent 用时设个超时或用 `timeout 30 python -m telnix.cli packets list --tail`。
8. **二进制 body**：`request_body`/`response_body` 里二进制内容是 `base64:` 前缀的字符串，agent 要先 decode。
9. **删除规则记 id**：`intercept add` 返回 `rule_id`，agent 要存下来，用完删掉避免残留。
10. **进程归属**：每条流量带 `pid` 和 `process_name`，可用来按软件过滤。`process~=chrome.exe` 这种过滤很有用。
11. **轮询用 `--since-id` 而非 `--tail`**：agent 场景用 `--since-id <max_id>` 增量拉取，非阻塞，每次拉完记下 stderr 里的 `max_id` 作为下次基线。`--tail` 是给人看的，agent 别用。
12. **TCP/UDP 抓包需管理员权限 + pydivert**：`raw status` 返回 `admin=true` 且 `pydivert=true` 才能用。pydivert 没装先 `pip install pydivert`，WinDivert64.sys 驱动由 pydivert 自带首次运行加载。非管理员账号启动后端会 `admin=false`，需以管理员身份重启 Python 进程。
13. **`protocol` 字段区分 HTTP/TCP/UDP**：HTTP 流量 `protocol=http`、有 `url/host/method`；TCP/UDP 流量 `protocol=tcp/udp`、`method=SEND/RECV`、`url=null`、`raw_data=base64:...`。写过滤表达式注意别混用 HTTP 字段。
14. **HTTPS 证书 pinning 导致解密失败**：某些 app（如银行/游戏）做了证书 pinning，Telnix 装根证书也解不开。表现：`packets list` 里看到 `CONNECT host:443` 但没后续 HTTP 请求，或客户端报 SSL 错误。这种情况无解，只能用 TCP/UDP 抓包看密文流量（`raw start --port 443`）。
15. **focus 按进程名含子进程**：`focus on --name x` 默认 psutil BFS 递归找所有后代 PID，多实例浏览器/游戏时可能匹配过多。需排除子进程加 `--no-children`。
16. **replay 不走规则**：`replay 42` 直接发请求到目标服务器，不触发自动回复规则。改参数 replay 用 `--body/--method/--host/--port`，fuzz 用 `--fuzz 'key=1..100'`（上限 500）。
17. **export 多种格式**：`--format python-requests` 生成可重放 Python 脚本，`--format postman` 生成 Postman Collection v2.1，`--format curl` 生成 bash 脚本。HAR 格式给浏览器 DevTools 用。
18. **搜索/统计先开抓包**：`packets search` 和 `packets stats` 作用在已抓到的流量上，没抓包没数据。搜索支持正则 body 和二进制 hex（如搜 BOM 头 `--binary-hex efbbbf`）。
19. **断点务必带 `--timeout`**：`breakpoint on --type request --timeout 30`。不设超时 + 忘 release → 连接永久卡死，后端线程耗尽。超时后自动放行并打 warning 日志。`breakpoint status` 的 `pending` 字段含每条被拦流量的 `waiting_seconds`，agent 可据此判断是否该 release。
20. **`capture --auto-stop N` 由后端定时**：参数传给后端常驻进程，agent CLI 退出也不影响。N 秒后后端自动 `capture stop` + 结束会话。比 `--max-duration`（仅提示）更可靠。
21. **`modify_request` 改包有 diff 日志**：改请求头/请求体的规则应用后，会在 `log tail --category proxy` 里打印原始 vs 修改后的 headers + body（前 200 字节，hex + text 双视图）。agent 确认规则是否生效看这个日志。
22. **`pinning_suspected` 在 `status` 里**：TLS 握手失败 + 证书相关错误的进程会被收集（按 host+pid+proc 去重，上限 50 条）。`status` 返回的 `pinning_suspected` 数组每项含 `host/pid/process_name/timestamp/error`。agent 抓不到 HTTPS payload 时先查这个字段。
23. **focus 用 psutil 不依赖 wmic**：Win11 24H2+ 移除了 wmic，Telnix 已改用 psutil 解析进程名 + 子进程（BFS 递归）。`focus on --name x` 在新版 Windows 仍能正常工作。
24. **method/status/pid/process 过滤现已代理层生效**（§4.1 陷阱已修复）：`intercept add --match 'host~=x && method=POST && status=200 && pid=1234 && process=chrome.exe'` 中，`method`/`status`/`pid`/`process` 会自动提取到规则的 `method_filter`/`status_filter`/`pid_filter`/`process_filter` 字段，代理层 `find_matching_rule` 校验这些字段——非空时必须匹配才命中。同 URL 的不同方法/状态/进程不会再被误伤。`!=` 和数值比较（`>=`/`<=`）仍只做客户端过滤（dry-run 生效，真规则不检查）。详见 §4。
25. **逆向分析三件套：diff/endpoints/timeline**：`packets diff <id1> <id2>` 对比两条流量字段差异（JSON body 自动格式化）；`packets endpoints` 提取唯一 API endpoint（path 模板归一化，画 API 地图）；`packets timeline` 按时间排序标注段落分隔。这三个命令是逆向 agent 摸清软件 API 结构的利器。
26. **`processes` 看连接快照找目标 PID**：`processes --name x --with-connections` 能看某进程当前连了哪些服务器（raddr），配合 `focus on --host` 精准专注。`--tree` 看进程父子关系。
27. **`replay --repeat N --compare` 测响应一致性**：批量重放 N 次并对比响应差异。响应里有随机 token/nonce/时间戳时 diff 会标出变化行，适合分析哪些字段是动态生成的。
28. **`intercept export/import` 规则集迁移**：导出规则到 JSON 文件备份/迁移，`--mode replace` 先清空再导入。逆向分析的规则集可存档复用，不同环境间迁移用这个。
29. **批量解码器 `--decode`**：`packets list --decode plugin.py` 批量解码二进制协议（如 Steam protobuf），不用逐条 `packets get`。解码器接口见 §3.3。
30. **`packets stats --by endpoint --metrics size,duration`**：按 endpoint 分组 + 百分位指标。p95 高 = 长尾慢请求，定位性能瓶颈用。
31. **`capture pause` vs `stop`**：`pause` 保留会话只停记录（代理仍跑，规则仍生效），`stop` 结束会话。`resume` 在原会话继续。适合"抓一会儿暂停看看再继续"，不用反复开新会话。
32. **`intercept toggle/update` 不删规则改状态**：`toggle <id> --disable` 临时停用规则不删除，`update <id> --note x` 改备注。比 `del + add` 保留 rule_id，适合 A/B 测试规则开关效果。
33. **`packets watch --filter 'expr'` 定向 tail**：阻塞输出匹配过滤表达式的新流量。比 `packets list --tail`（看所有）更精准，agent 盯特定接口用这个。
34. **`sessions delete <id>` 清理旧会话**：删除会话及其所有流量，避免 SQLite 膨胀。`sessions list` 看所有会话，`sessions show <id>` 看详情含 flow_count。
35. **`export --format csv` 表格导出**：12 字段 CSV（id/timestamp/method/host/path/url/status_code/duration_ms/size/process_name/pid/protocol），适合导入 Excel 分析。不指定 `-o` 时按格式自动推导文件名。
36. **`processes --include-listen` 查监听端口**：`--with-connections` 默认跳过 LISTEN，加 `--include-listen` 可查"谁监听了某端口"。
37. **`endpoints/timeline/stats --session S --limit 0`**：指定会话分析 + 拉全量（上限 50000）。`--keep-query` 保留 query 参数名到 `query_keys` 字段。
38. **`packets trace <id>` 请求依赖链分析**：从源 flow 响应提取字符串值（JSON 叶子 + token 正则），在后续流量请求里搜索，输出依赖链（token 传递追踪）。适合分析登录后 token 如何在后续请求中传递。见 §3.3。
39. **`packets analyze <id1> <id2> ...` 签名字段检测**：多条同接口请求对比，自动找"长度固定+字符集受限+每次都不同"的可疑签名字段，按 `suspicion_score` 降序输出。至少 2 个 flow ID。见 §3.3。
40. **`replay --parallel N` 并发重放**：配合 `--repeat N` 用，`ThreadPoolExecutor` 并发重放 N 次。测服务端并发限制/竞态条件用这个。`results` 按 index 排序。见 §3.5。
41. **`replay --fuzz-file payloads.json --mode cartesian|zip`**：多字段组合 fuzz。`cartesian` 笛卡尔积，`zip` 按最短长度配对。支持点分路径字段。适合爆破/签名碰撞/多参数组合测试。见 §3.5。
42. **`replay-batch --session S [--preserve-timing]`**：按 session 整批重放。`--preserve-timing` 按原始时间间隔 sleep（测服务端限流/风控，强制串行）。`--parallel N` 并发立即重放。见 §3.16。
43. **`delay`/`delay-request` 动作**：响应/请求延迟 N 毫秒（代理层 `time.sleep`）。测客户端竞态/超时处理用。见 §5.5。
44. **规则命中统计**：`intercept list` 每条规则含 `hit_count`/`last_hit_at`/`last_hit_flow_id`（代理层匹配时自增）。评估规则效果看这个，不用翻日志。
45. **规则模板库（HTTP API）**：6 个内置模板（mock-404/mock-500/strip-auth/unlock-vip/bypass-pay/slow-response），`POST /templates/{name}/apply` 一键创建规则。见 §3.17。
46. **环境快照（HTTP API）**：`GET /snapshot` 导出规则+focus+断点，`POST /snapshot` 导入。多目标切换/环境复现用。比 `intercept export/import` 范围更大（含 focus+断点）。见 §3.18。
47. **流量分组（HTTP API）**：`POST /flows/groups` 创建分组（收藏夹），`GET /flows/groups/{id}` 查看含 flow 详情。分组只存 flow_ids 引用，删除分组不删流量。见 §3.19。
48. **导入流量（HTTP API）**：`POST /import` 支持 JSON（Telnix 原生）和 HAR（浏览器 DevTools）格式，自动创建新会话。前端 UI 在抓包/分析/搜索页都有导入按钮。见 §3.20。
49. **多条件组合搜索**：`packets search` 支持 `--body-regex`/`--binary-hex`/`--header-regex`/`--method`/`--status`/`--pid`/`--process` 多条件 AND 组合（底层 `POST /flows/search`），不再只是单正则。可只用精确字段过滤（如 `--method POST --status 404`），无需正则。加 `--all` 跨所有会话搜索。见 §3.3。
50. **批量创建规则**：`POST /auto-reply/rules/batch-create` 一次传 rules 数组，比逐条 POST 快。大批量导入规则用这个（CLI `intercept import` 已用此端点优化）。
51. **流量标签 tag**：`packets tag <id> --add analyzed` 给流量打标签，`packets list --tag analyzed` 按标签过滤。适合长会话标记"已分析""可疑""关键接口"，切换会话/重启后不丢失（存 SQLite）。tags 逗号分隔，tag_note 备注可空。`--add`/`--remove` 多值逗号分隔，`--clear` 清空所有标签，`--note` 设备注（可与 `--add` 同时用）。`packets list --has-tags` 只看有标签的流量。见 §3.3。
52. **agent 启动后端必用 `--no-browser`**：`python -m Telnix --no-browser` 不自动开浏览器，避免干扰用户。等价环境变量 `Telnix_NO_BROWSER=1`。`system restart` 和 `system restart-as-admin` 会透传此参数，重启后行为一致。见顶部「启动参数」。
53. **`send` 从零发包**：`send --url ... --method ...` 构造任意 HTTP 请求发送，不走代理、不写入 flows 表，适合测试接口/调试 API。支持 `--header`/`--body`/`--body-file`/`--timeout`/`--headers-only`/`--body-only`/`--emit-curl`。见 §3.22。
54. **管理员重启走 GUI 确认**：`system restart-as-admin` 会在桌面弹**原生 Windows 置顶 Yes/No 弹窗**（任务栏闪烁，默认聚焦「否」按钮防误按），用户同意才 UAC 提权。用户拒绝时 CLI 返回 `{"rejected_by_user": true, ...}` + 退出码 1，agent 可据此区分"用户拒绝"和"超时/错误"。见 §3.21。
55. **WinDivert 首次启用风险提示**：`raw start` / `transparent-proxy start` / `dns-hijack start` 首次调用时（Windows + 未确认 `windivert_warning_acknowledged`），后端返回 `403 + need_ack=true`，CLI/MCP 自动弹原生置顶 Yes/No 弹窗（同管理员重启弹窗风格），用户选「是」立即 ack=1 持久化 + 自动重试原请求；选「否」CLI 返回 `{"rejected_by_user": true, ...}` + 退出码 1。3 分钟无响应也按拒绝处理。已确认后所有 WinDivert 端点直接放行不再弹窗。**agent 不应直接调 `system windivert-warning-ack` 跳过用户确认**——应让用户在弹窗里选择。见 §3.21.3。
56. **用户设置存 settings.json**：GUI 偏好（列顺序/导航顺序/主题/缓存阈值等）存 `<data_dir>/settings.json`（原子写入 + 线程锁），不再用 SQLite。首次启动自动从 SQLite 迁移。用户可在设置页点「打开设置文件」用记事本直接编辑。
57. **`agent start/end` 工作模式**：agent 接管会话前调 `agent start`，临时禁用所有自动回复规则 + 关 focus + 关断点 + **把 agent 进程（`TRAE SOLO CN.exe`）加入忽略列表防止抓自己的包**（备份原状到临时文件），做事，收工调 `agent end` 恢复原状 + 移除 agent 添加的忽略进程（保留用户原本的）。**`agent end` 不关代理、不退出 Telnix**（用户可能依赖自动修改规则继续工作）。agent 应在 `end` 后询问用户是否关闭 Telnix，用户同意才调 `system quit`（后端退出时 atexit 自动清代理）。见 §3.23。

---

## 9. 错误排查

### 退出码→处理（先看这个）

CLI 退出码语义明确，按退出码快速定位问题大类（详见 [§2 输出格式约定](#2-输出格式约定重要)）：

| 退出码 | 含义 | kind | 立即处理 |
|---|---|---|---|
| `0` | 成功 | — | 继续下一步 |
| `1` | 后端业务错误（500/规则不存在/会话不存在等） | `biz` | 读 stderr 的 `error` 字段，按 `hint`（如有）处理 |
| `2` | 连接错误（后端未启动/网络不通） | `conn` | `cd src\host && python -m Telnix` 启动后端 |
| `3` | 参数错误（客户端校验失败） | `arg` | 读 stderr 的 `error` 字段，改命令参数后重试 |

**解析建议**：
- 先看退出码判断错误大类，再读 stdout（成功）或 stderr（失败）。
- stderr 是结构化 JSON：`{"ok": false, "error": "...", "kind": "...", "hint": "..."}`。直接 `json.loads(stderr_content)` 取字段，不要按文本匹配。
- `hint` 字段**可选**，用 `.get("hint")` 而非 `error["hint"]` 避免 KeyError。

### 常见现象排查

| 现象 | 原因 | 解决 |
|---|---|---|
| CLI 退出码 2 | 后端没启动 | 启动后端：`cd src\host && python -m Telnix`（agent 场景加 `--no-browser`） |
| CLI 报连接失败但后端在跑 | 端口不对（误以为 18899） | Telnix API 端口是 **18901**（不是 18899）。CLI 默认连 18901，检查 `TELNIX_API` 环境变量是否被误设为 18899 |
| `bind 报 WSAEACCES=13` 启动失败 | 端口被 Windows 动态保留 / 已被占用 | Telnix 已改用 18901 避开 18899/18900 保留段；若 18901 也被占，检查 `netstat -ano \| findstr 18901` 找占用进程 |
| HTTPS 流量看不到 body | 证书没装 | `cert install` |
| 规则不生效 | pattern 没匹配上 / 被更宽泛的规则抢先 | `intercept list` 看 pattern，注意长度降序 |
| `set-json` 没改到 | 响应不是 JSON / key 不存在 | `packets get <id>` 看 `response_body` 实际结构 |
| mock 返回了真实响应 | 规则 action 写错 / pattern 没匹配 | 查 `log tail --category proxy` 看匹配日志 |
| 改完字段客户端还报错 | Content-Length 没更新 / 字段类型不对 | Telnix 会自动更新 Content-Length；类型用 `--dry-run` 确认 value 类型 |
| `raw start` 报 `pydivert=false` | pydivert 未安装 | `pip install pydivert`（驱动 WinDivert64.sys 随包附带，首次自动加载） |
| `raw start` 报 `admin=false` | 后端非管理员运行 | `python -m telnix.cli system restart-as-admin` 触发 GUI 置顶弹窗，用户同意后 UAC 提权重启 |
| `system restart-as-admin` 报 `rejected_by_user: true` | 用户在桌面弹窗点了「否」/超时未响应 | 提示用户同意，或让用户手动右键管理员身份启动后端 |
| `raw start` 报 `driver load failed` | WinDivert64.sys 被杀软拦 / 旧残留 | 加白名单；或重启系统后重试；管理员运行 `sc stop WinDivert` 清理旧服务 |
| HTTPS 抓到 CONNECT 但无 payload | 客户端证书 pinning | 无解，改用 `raw start --port 443` 抓密文 TCP 流量 |
| `replay 42` 报连接超时 | 目标 host 不可达 / 端口错 | `packets get 42` 确认 url，用 `--host/--port` 覆盖到可访问地址 |
| `replay --fuzz` 报 body 非 JSON | 原 body 不是 JSON 无法改字段 | 改用 `--body` 手动构造每个请求；或先 `--body` 单测一次 |
| `packets search` 返回空 | 没抓包 / 正则没命中 | 先 `capture start` 抓包；正则用 `packets get <id>` 验证 body 内容 |
| `focus on --name x` 返回 0 个 PID | 进程名写错 / 进程未启动 | `processes` 查实际进程名（含 .exe 后缀） |
| `intercept add` 报 `updated_at NOT NULL` | 后端旧版本无此字段 | 重启后端跑 db migration；或更新到最新代码 |
| 断点开启后所有流量卡死 | 没设 `--timeout` + 忘 release | `breakpoint off --type request` 关掉；后续 always 带 `--timeout N` |
| `breakpoint status` 的 `pending` 一直不空 | agent 没 release 被拦流量 | `POST /flows/{id}/release` 放行；或等超时自动放行 |
| `capture --auto-stop` 没生效 | 后端旧版本不支持 / CLI 旧版本用 daemon 线程 | 重启后端（新版本由后端常驻进程定时）；更新 CLI |
| `status` 的 `pinning_suspected` 有条目 | 客户端做了证书 pinning，SSL bump 失败 | 无解（pinning 无法绕过）；改用 `raw start --port 443` 抓密文 TCP |
| `focus on --name x` 返回 0 个 PID（Win11 24H2+） | 旧版本用 wmic，新版 Windows 移除了 wmic | 更新到 psutil 版本；或临时用 `--pid` 手动指定 |
| `capture resume` 报"无活动会话" | 没有活动会话（未 start 或已 stop） | 先 `capture start`，pause 只在活动会话期间有效 |
| `intercept toggle` 报"规则不存在" | rule_id 写错 / 规则已被删除 | 先 `intercept list` 拿真实 rule_id |
| `sessions delete` 报"会话不存在" | 会话 id 写错 / 已删除 | `sessions list` 确认 id |
| `packets watch` 无输出 | 没有匹配的新流量 / 没在抓包 | 确认 `capture start` 在跑；放宽 `--filter` 表达式 |

---

## 10. 命令速查表

```bash
# 状态
python -m telnix.cli status
python -m telnix.cli cert status
python -m telnix.cli proxy status
python -m telnix.cli raw status
python -m telnix.cli focus status

# 抓包（HTTP 层）
python -m telnix.cli capture start
python -m telnix.cli capture start --auto-stop 30          # 30 秒后自动停（后端定时）
python -m telnix.cli capture stop
python -m telnix.cli capture clear
python -m telnix.cli capture pause                          # 暂停记录（保留会话）
python -m telnix.cli capture resume                         # 恢复记录

# 抓包（TCP/UDP 网络层，WinDivert）
python -m telnix.cli capture start --layer all
python -m telnix.cli raw install                                       # 一键装 pydivert 驱动
python -m telnix.cli raw start --port 443
python -m telnix.cli raw start --pid 1234 --filter 'tcp or udp'
python -m telnix.cli raw stop
python -m telnix.cli capture stop --layer all

# 看包
python -m telnix.cli packets list --limit 50
python -m telnix.cli packets list --since-id 42 --json-array   # 增量轮询
python -m telnix.cli packets list --protocol tcp               # 只看 TCP/UDP
python -m telnix.cli packets list --tail --emit-curl
python -m telnix.cli packets list --filter 'host~=x && method=POST'
python -m telnix.cli packets get 42
python -m telnix.cli packets get 42 --hex --field raw_data     # 看 TCP/UDP 原始字节
python -m telnix.cli packets get 42 --decode my_decoder.py     # 自定义解码器解析 body
python -m telnix.cli packets delete 42
python -m telnix.cli packets delete --ids 1,2,3

# 流量标签（打标 / 按标签过滤，存 SQLite 切换会话不丢）
python -m telnix.cli packets tag 42 --add analyzed --note "备注"
python -m telnix.cli packets tag 42 --add suspicious,key --note "疑似签名字段"
python -m telnix.cli packets tag 42 --remove analyzed
python -m telnix.cli packets tag 42 --clear
python -m telnix.cli packets tag --list                                       # 列出全局所有标签及每标签的 flow 数
python -m telnix.cli packets list --tag suspicious                          # 按标签过滤
python -m telnix.cli packets list-all --tag analyzed                        # 跨会话按标签过滤
python -m telnix.cli packets list --has-tags                                # 只看有标签的

# 逆向分析三件套：对比/API 地图/时间线
python -m telnix.cli packets diff 42 43 --field response_body             # 对比两条流量字段差异
python -m telnix.cli packets endpoints --limit 200                        # 唯一 endpoint 提取（API 地图）
python -m telnix.cli packets endpoints --session 7 --keep-query --limit 0 # 指定会话+全量+保留 query 参数名
python -m telnix.cli packets timeline --gap 2.0                           # 流量时间线（段落分隔）
python -m telnix.cli packets timeline --session 7                         # 指定会话时间线
python -m telnix.cli packets list --decode my_decoder.py --limit 50       # 批量解码
python -m telnix.cli packets list --decode my_decoder.py --decode-field request_body  # 解码请求体
python -m telnix.cli packets list-all --host x --decode my_decoder.py     # 跨会话批量解码
python -m telnix.cli packets watch --filter 'host~=api.x.com && method=POST'  # 定向 tail 匹配的新流量

# 跨流量搜索 + 统计
python -m telnix.cli packets search --body-regex 'remainingUses.*\d{4,}'
python -m telnix.cli packets search --binary-hex 'efbbbf'
python -m telnix.cli packets search --binary-hex 'efbbbf' --offset 0:1024   # 只在前 1KB 搜
python -m telnix.cli packets search --body-regex 'sig=[a-f0-9]{32}' --all   # 跨所有会话
python -m telnix.cli packets search --method POST --status 200 --pid 1234 --process chrome.exe  # 多条件组合搜索
python -m telnix.cli packets search --method GET --status 404               # 只用精确字段过滤（无需正则）
python -m telnix.cli packets stats
python -m telnix.cli packets stats --by endpoint                           # 按 endpoint 分组
python -m telnix.cli packets stats --by endpoint --metrics size,duration   # + 百分位指标
python -m telnix.cli packets stats --by content_type                        # 按 Content-Type 分组（跨会话全量）
python -m telnix.cli packets stats --by process                             # 按进程名分组（跨会话全量）

# 逆向分析：依赖链 + 签名检测
python -m telnix.cli packets trace 42                                       # 请求依赖链（token 传递追踪）
python -m telnix.cli packets trace 42 --all                                 # 跨会话追踪 token 传递
python -m telnix.cli packets analyze 42 43 44 --find-signature              # 签名字段自动检测
python -m telnix.cli packets analyze 42 43 44 --all                         # 跨会话签名字段检测

# 跨会话查询/清理（不依赖活动会话）
python -m telnix.cli packets list-all --host api.example.com --limit 50
python -m telnix.cli packets list-all --filter-path '/api/v1/' --limit 50     # 后端 SQL LIKE 过滤 path
python -m telnix.cli packets list-all --filter-url 'example.com' --limit 50   # 后端 SQL LIKE 过滤 url
python -m telnix.cli packets list-all --since-id 100 --json-array             # 增量轮询
python -m telnix.cli packets clear --all                                      # 清空全部
python -m telnix.cli packets clear --before-id 1000                           # 删除旧流量

# 单 flow 导出（逆向取证）
python -m telnix.cli packets export 42 --format curl -o req.sh
python -m telnix.cli packets export 42 --format python-requests -o req.py
python -m telnix.cli packets export 42 --format csv -o req.csv                # CSV 单行带表头

# 拦截
python -m telnix.cli intercept add --match '...' --action 'set-json k v' --name n --dry-run
python -m telnix.cli intercept add --match 'host~=x && method=POST && status=200' --action 'set-json k v' --name n  # method/status 过滤代理层生效
python -m telnix.cli intercept add --match '...' --action 'mock 200 {}' --name n
python -m telnix.cli intercept add --match '...' --action 'mock-request {"u":1}' --name n  # 写死请求 body 转发到真实服务器
python -m telnix.cli intercept add --match '...' --action 'set-request-json k v' --name n  # 改请求
python -m telnix.cli intercept add --match '...' --action 'replace-bytes 10:414243' --name n  # 二进制
python -m telnix.cli intercept add --match 'host~=x && path~=/login' --action 'delay 5000' --name 'slow-login'  # 响应延迟 5s
python -m telnix.cli intercept add --match 'host~=x && path~=/pay' --action 'delay-request 2000' --name 'slow-pay'  # 请求延迟 2s
python -m telnix.cli intercept add --match '...' --action 'set-json k v' --name n --idempotent  # §3.1 幂等创建（后端端点+客户端降级）
python -m telnix.cli intercept list                                          # 含 hit_count 命中统计
python -m telnix.cli intercept hits <rule_id>                                # 查看规则命中统计+最后命中流量详情
python -m telnix.cli intercept del <id>
python -m telnix.cli intercept toggle <id> --disable                       # 禁用规则（不删除）
python -m telnix.cli intercept toggle <id> --enable                        # 启用规则
python -m telnix.cli intercept toggle --all --disable                      # 批量禁用所有规则
python -m telnix.cli intercept update <id> --note '新备注'                  # 修改规则备注
python -m telnix.cli intercept update <id> --match 'host~=x' --enable      # 改匹配+启用
python -m telnix.cli intercept export -o my_rules.json                    # 导出规则集
python -m telnix.cli intercept import my_rules.json                       # 导入（merge 追加）
python -m telnix.cli intercept import my_rules.json --mode replace        # 导入（先清空再导入）
python -m telnix.cli intercept import big_rules.json --quiet              # 大批量导入只输出汇总

# 重放（支持改参数 / fuzz / 批量对比 / 并发 / 多字段组合）
python -m telnix.cli replay 42
python -m telnix.cli replay 42 --method PUT --host test.example.com --port 8443
python -m telnix.cli replay 42 --body '{"x":1}' --header 'X-Test: 1'
python -m telnix.cli replay 42 --fuzz 'user_id=1..100'
python -m telnix.cli replay 42 --fuzz-file payloads.json --mode cartesian  # 多字段笛卡尔积 fuzz
python -m telnix.cli replay 42 --fuzz-file payloads.json --mode zip        # zip 配对模式
python -m telnix.cli replay 42 --repeat 5 --compare                       # 批量重放 + 响应 diff
python -m telnix.cli replay 42 --repeat 20 --parallel 5                    # 5 线程并发重放 20 次
python -m telnix.cli replay 42 --timeout 300                               # 自定义超时（慢接口）

# 时序回放（按 session 整批重放）
python -m telnix.cli replay-batch --session 7                              # 立即连续重放（串行）
python -m telnix.cli replay-batch --session 7 --preserve-timing            # 按原始时间间隔重放
python -m telnix.cli replay-batch --session 7 --parallel 5                 # 并发立即重放
python -m telnix.cli replay-batch --session 7 --filter 'method=POST'       # 选择性时序回放（只重放 POST）

# 进程（找目标 PID / 看连接 / 进程树 / 忽略管理）
python -m telnix.cli processes --name chrome
python -m telnix.cli processes --name chrome --with-connections            # 看当前 TCP 连接
python -m telnix.cli processes --name chrome --with-connections --include-listen  # 含 LISTEN 端口
python -m telnix.cli processes --name svchost.exe --tree                   # 进程树
python -m telnix.cli processes ignore --pid 1234                            # 忽略进程（按 PID）
python -m telnix.cli processes ignore --name chrome.exe                     # 忽略进程（按名称，可多个）
python -m telnix.cli processes unignore 3                                   # 取消忽略（按行 id）
python -m telnix.cli processes ignored                                      # 列出已忽略进程
python -m telnix.cli processes ignore-host --host "*.example.com"           # 忽略 host（通配符）
python -m telnix.cli processes unignore-host 2                              # 取消忽略 host（按行 id）
python -m telnix.cli processes ignored-hosts                                # 列出已忽略 host

# 会话管理
python -m telnix.cli sessions list                    # 列出所有会话
python -m telnix.cli sessions show 7                  # 会话详情（含 flow_count）
python -m telnix.cli sessions delete 7                # 删除会话及其流量

# 专注模式
python -m telnix.cli focus on --pid 1234
python -m telnix.cli focus on --name chrome.exe
python -m telnix.cli focus on --name chrome.exe --no-children
python -m telnix.cli focus on --host '*.example.com'                      # 按 host 通配符专注
python -m telnix.cli focus on --name chrome.exe --host '*.google.com'      # 进程+host 跨类 OR
python -m telnix.cli focus off

# 断点（务必带 --timeout 避免卡死）
python -m telnix.cli breakpoint status
python -m telnix.cli breakpoint on --type request --timeout 30
python -m telnix.cli breakpoint on --type response --timeout 30
python -m telnix.cli breakpoint off --type request
python -m telnix.cli breakpoint timeout --timeout 60
python -m telnix.cli breakpoint release 42              # 放行单条
python -m telnix.cli breakpoint drop 42                 # 丢弃单条
python -m telnix.cli breakpoint release --all           # 批量放行所有 pending
python -m telnix.cli breakpoint drop --all              # 批量丢弃所有 pending

# 导出（多种格式）
python -m telnix.cli export --format har -o out.har
python -m telnix.cli export --format python-requests -o replay.py
python -m telnix.cli export --format postman -o collection.json
python -m telnix.cli export --format curl -o replay.sh
python -m telnix.cli export --format csv                    # 不指定 -o 按格式推导文件名 Telnix_export.csv

# 系统
python -m telnix.cli proxy on                               # 开系统代理 ✅ 全平台
python -m telnix.cli proxy off                              # 关系统代理 ✅ 全平台
python -m telnix.cli cert status                            # 证书状态 ✅ 全平台
python -m telnix.cli cert install                           # 装根证书 ✅ Win/mac ⚠️ Linux 需手动
python -m telnix.cli cert remove                            # 卸载根证书 ✅ Win/mac ⚠️ Linux 需手动
python -m telnix.cli log tail --category proxy --level ERROR
python -m telnix.cli log clear                              # 清空所有日志
python -m telnix.cli log export -o logs.jsonl               # 导出日志为 JSONL
python -m telnix.cli log export -o err.jsonl --level ERROR --category proxy  # 带过滤导出
python -m telnix.cli system restart                         # 重启前后端（保留 --no-browser 等启动参数）✅ 全平台
python -m telnix.cli system quit                            # 退出 Telnix（关代理+关服务）✅ 全平台
python -m telnix.cli system restart-as-admin                # 以管理员身份重启（全平台：UAC/osascript/pkexec）
python -m telnix.cli system platform-capabilities           # 查看平台能力矩阵 ✅ 全平台

# 透明代理（网络层重定向，需管理员/root 权限，应用无需配置代理即可被抓包）✅ 全平台
python -m telnix.cli transparent-proxy status               # 查看状态
python -m telnix.cli transparent-proxy start                # 启动（需管理员/root）
python -m telnix.cli transparent-proxy stop                 # 停止

# 自动修改规则（list/get/create/enable/disable/delete，create 支持 --script-path 从 .py 加载）
python -m telnix.cli auto-reply list                       # 列出所有规则（NDJSON，含命中统计）
python -m telnix.cli auto-reply list --json-array          # 输出 JSON 数组
python -m telnix.cli auto-reply get <id>                   # 查看规则详情
python -m telnix.cli auto-reply create \
  --pattern '*api.example.com/v1/*' --action script \
  --script-path my_rule.py --note '动态签名改包'              # 从本地 .py 文件加载脚本创建规则（agent 友好）
python -m telnix.cli auto-reply create \
  --pattern '*api.example.com/login*' --action mock \
  --action-spec 'mock 200 {"ok":true}'                     # 非 script 动作，复用 intercept add 语法
python -m telnix.cli auto-reply create \
  --pattern '*api.example.com/v1/*' --action script \
  --script-path sign.py --method-filter POST --status-filter 200 \
  --process-filter chrome.exe                               # 带过滤条件，避免误命中
python -m telnix.cli auto-reply enable <id>                # 启用规则
python -m telnix.cli auto-reply disable <id>               # 禁用规则
python -m telnix.cli auto-reply delete <id>                # 删除规则

# 发包（Composer，不走代理不写入 flows）
python -m telnix.cli send --url https://api.example.com                          # GET
python -m telnix.cli send --method POST --url https://api.example.com \
  --header 'Content-Type: application/json' --body '{"k":1}'                       # POST JSON
python -m telnix.cli send --method POST --url https://api.example.com/upload \
  --body-file payload.bin                                                          # 从文件读 body
python -m telnix.cli send --url https://api.example.com --timeout 60             # 自定义超时
python -m telnix.cli send --url https://api.example.com --headers-only           # 只输出响应头
python -m telnix.cli send --url https://api.example.com --body-only              # 只输出响应体
python -m telnix.cli send --method POST --url https://api.example.com \
  --header 'X-Test: 1' --body '{"k":1}' --emit-curl                                # 导出 curl 命令不发

# 后端启动（agent 自动化场景必用 --no-browser）
python -m Telnix --no-browser                                                      # 不开浏览器
Telnix_NO_BROWSER=1 python -m Telnix                                              # 等价环境变量

# HTTP API（CLI 未覆盖的功能，用 curl 调）
curl http://127.0.0.1:18901/api/templates                                    # 规则模板列表
curl -X POST http://127.0.0.1:18901/api/templates/unlock-vip/apply \
  -H "Content-Type: application/json" \
  -d '{"pattern":"*api.example.com*/vip*","note":"解锁VIP"}'                  # 应用模板创建规则
curl http://127.0.0.1:18901/api/snapshot -o state.json                       # 导出环境快照（规则+focus+断点）
curl -X POST http://127.0.0.1:18901/api/snapshot -H "Content-Type: application/json" -d @state.json  # 导入环境快照
curl -X POST http://127.0.0.1:18901/api/flows/groups \
  -H "Content-Type: application/json" -d '{"name":"login-flow","flow_ids":[42,43,44]}'  # 创建流量分组
curl http://127.0.0.1:18901/api/flows/groups/1                               # 查看分组（含 flow 详情）
curl -X POST http://127.0.0.1:18901/api/import \
  -H "Content-Type: application/json" \
  -d '{"format":"har","content":"<HAR 内容>"}'                                # 导入 HAR 流量
curl -X POST http://127.0.0.1:18901/api/flows/search \
  -H "Content-Type: application/json" \
  -d '{"body_regex":"sig=[a-f0-9]{32}","method":"POST","status":200}'        # 多条件组合搜索
```

---

## 附录 B：输出字段速查表

agent 写解析代码时按命令查字段，避免逐个命令翻文档。`+` 表示在上一行字段基础上新增。

### packets 命令族

| 命令 | 核心字段 |
|---|---|
| `packets list` | `id`, `session_id`, `method`, `host`, `path`, `url`, `status_code`, `pid`, `process_name`, `protocol`, `timestamp`, `duration_ms`, `size`, `tags`, `tag_note` |
| `packets get` | + `request_headers`, `request_body`, `response_headers`, `response_body`, `breakpoint_status`, `src_port`, `dst_port`, `raw_data`（TCP/UDP 才有）, `curl`（`--emit-curl` 时）, `decoded`（`--decode` 时） |
| `packets search` | `matches`: [flow 同 `packets get` 字段], `count` |
| `packets endpoints` | `method`, `host`, `path_template`, `count`, `status_set`: [int], `sample_ids`: [int], `query_keys`: [str]（`--keep-query` 时） |
| `packets timeline` | `timeline`: [{`id`, `timestamp`, `method`, `host`, `path`, `segment`, `segment_break`, `gap_seconds`}], `count`, `segments`, `gap_threshold` |
| `packets diff` | `id1`, `id2`, `field`, `diff`（unified diff 文本）, `identical` |
| `packets stats` | `total`, `by_host`, `by_method`, `by_status`, `by_protocol`；`--by endpoint` 时 `endpoints`: [{method, host, path_template, count, status_set, sample_ids}]；`--metrics size,duration` 时附加 `size`/`duration` 的 `{p50, p95, max, min, count}` |
| `packets tag --list` | NDJSON：`{tag, count}` |

### intercept 命令族

| 命令 | 核心字段 |
|---|---|
| `intercept list` | `id`/`rule_id`（同值）, `enabled`, `match_mode`, `pattern`, `action`, `mock_status`, `mock_headers`（dict）, `mock_body`, `modify_rules`（list）, `note`, `method_filter`/`status_filter`/`pid_filter`/`process_filter`（无值时是 `null` 不是 `""`）, `hit_count`, `last_hit_at`, `last_hit_flow_id`, `mock_method`, `mock_url`, `created_at`, `updated_at` |
| `intercept hits <id>` | `rule_id`, `pattern`, `action`, `hit_count`, `last_hit_at`, `last_hit_flow_id`, `last_hit_flow`（完整 flow 对象，`last_hit_flow_id` 为 null 时此字段为 null） |
| `intercept add` | `created`: bool, `rule_id`, `pattern`, `note`；`--idempotent` 时多 `idempotent`: bool + 可选 `hint`；`--dry-run` 时输出 `matched_flows` 而非创建 |
| `intercept toggle` | `toggled`: bool, `rule_id`, `enabled`: bool |
| `intercept update` | `updated`: bool, `rule_id`, `fields`: [str]（实际更新的字段名列表） |
| `intercept del` | `deleted`: bool/int（单条是 bool，`--ids` 批量是 int） |

### capture / status

| 命令 | 核心字段 |
|---|---|
| `status` | `capturing`: bool, `session_id`: int\|null, `proxy_port`, `cert_installed`: bool, `system_proxy_on`: bool, `breakpoint`: {`break_on_request`, `break_on_response`, `timeout_seconds`, `pending`: [{`flow_id`, `waiting_seconds`}]}, `pinning_suspected`: [{host, pid, process_name, error}] |
| `capture start` | `session_id`: int, `capturing`: bool, `auto_stop_seconds`: float（`--auto-stop` 时）, `system_proxy_on`: bool, `proxy_auto_enabled`: bool, `hint` |
| `capture stop` | `capturing`: false |
| `capture pause`/`resume` | `paused`/`resumed`: bool, `session_id`: int, `hint` |

### replay 命令族

| 命令 | 核心字段 |
|---|---|
| `replay <id>` | `flow_id`, `ok`: bool, `status`: int, `duration_ms`: int, `error`（失败时）；`--repeat` 时输出 `repeats`: [{`index`, `status`, `duration_ms`, `ok`}]；`--compare` 时多 `diff` |
| `replay-batch` | NDJSON（stdout）：{`index`, `flow_id`, `ok`, `status`, `duration_ms`, `error`（失败时）}；汇总（stderr）：{`replayed`: bool, `session`, `count`, `filtered_count`} |

### sessions

| 命令 | 核心字段 |
|---|---|
| `sessions list` | NDJSON：`id`, `name`, `started_at`, `ended_at`, `flow_count` |
| `sessions show <id>` | + `flow_count`: int |

### processes

| 命令 | 核心字段 |
|---|---|
| `processes` | `pid`, `name`, `cmdline`, `connections`: [{`local`, `remote`, `state`}]（`--with-connections` 时） |
| `processes ignored` | `id`, `pid`（null=按名称忽略）, `process_name`, `ignored_at` |
| `processes ignored-hosts` | `id`, `host_pattern`, `created_at` |

### 错误（stderr，所有命令失败时统一格式）

```json
{"ok": false, "error": "错误描述", "kind": "arg|conn|biz", "hint": "建议动作（可选）"}
```

- `kind`：`arg`=参数错误（退出码 3）、`conn`=连接错误（退出码 2）、`biz`=后端业务错误（退出码 1）
- `hint`：**可选字段**，用 `.get("hint")` 而非 `error["hint"]`

---

## 附录 C：MCP Server（IDE 集成用）

Telnix 提供 MCP (Model Context Protocol) 服务器，让 Claude Desktop / Cursor / VS Code 等 MCP 客户端直接调用 Telnix 的抓包/拦截/改包能力。

### C.1 启动

```bash
# 方式1：模块直接运行（推荐）
python -m Telnix.mcp_server

# 方式2：安装后的入口点
Telnix-mcp

# 自定义后端地址
python -m Telnix.mcp_server --base-url http://127.0.0.1:18901
# 或用环境变量
set TELNIX_API=http://127.0.0.1:18901
```

### C.2 客户端配置

**Claude Desktop** (`claude_desktop_config.json`)：

```json
{
  "mcpServers": {
    "Telnix": {
      "command": "python",
      "args": ["-m", "Telnix.mcp_server"],
      "cwd": ".\\src\\host",
      "env": { "TELNIX_API": "http://127.0.0.1:18901" }
    }
  }
}
```

**Cursor / VS Code**：参考各客户端的 MCP 配置文档，command 填 `python`，args 填 `["-m", "Telnix.mcp_server"]`。

### C.3 工具清单（88 个，100% 覆盖 CLI）

| 分类 | 工具 | 说明 |
|---|---|---|
| **状态/抓包** | `get_status` | 后端状态 |
| | `capture_start` / `capture_stop` / `capture_clear` / `capture_pause` / `capture_resume` | 抓包控制 |
| **流量查询** | `packets_list` | 当前会话流量列表 |
| | `packets_list_all` | 跨会话流量列表 |
| | `packets_get` | 单个流量详情 |
| | `packets_search` | 正则搜索 body |
| | `packets_stats` | 流量统计 |
| | `packets_delete` / `packets_clear` | 删除/清空流量 |
| **流量高级分析** | `packets_export` | 单 flow 导出（curl/python-requests/postman/json/csv） |
| | `packets_tag` | 流量标签管理 |
| | `packets_diff` | 对比两条流量（unified diff） |
| | `packets_endpoints` | 唯一 endpoint 提取（API 地图） |
| | `packets_timeline` | 流量时间线（大间隔分段） |
| | `packets_trace` | 请求依赖链 trace |
| | `packets_analyze` | 签名字段自动检测 |
| **拦截规则** | `intercept_add` | 添加规则（支持 dry_run/idempotent） |
| | `intercept_list` | 列出规则（含命中统计） |
| | `intercept_del` | 删除规则 |
| | `intercept_hits` | 查看规则命中详情 |
| | `intercept_toggle` | 启用/禁用规则 |
| | `intercept_update` | 修改现有规则 |
| | `intercept_export` / `intercept_import` | 规则导入导出 |
| | `intercept_template_list` / `intercept_template_apply` | 规则模板 |
| **重放/发包** | `replay` | 重放流量（可覆盖参数） |
| | `send_request` | 从零发包（Composer） |
| | `replay_batch` | 批量时序重放 |
| | `session_export` | 会话导出（har/json/csv 等） |
| **断点** | `breakpoint_status` / `breakpoint_on` / `breakpoint_off` / `breakpoint_release` / `breakpoint_timeout` | 断点控制 |
| **系统控制** | `proxy_status` / `proxy_on` / `proxy_off` | 系统代理 |
| | `cert_status` / `cert_install` / `cert_remove` | HTTPS 证书 |
| | `focus_status` / `focus_on` / `focus_off` | 专注模式 |
| | `raw_capture_status` / `raw_capture_start` / `raw_capture_stop` / `raw_capture_install` | TCP/UDP 抓包 |
| | `system_restart` / `system_quit` / `system_restart_as_admin` | 系统控制 |
| | `system_install_dep` / `system_install_dep_status` | 可选依赖安装（如 mitmproxy） |
| **设置管理** | `settings_get` / `settings_set` / `settings_proxy_engine` | 设置读写 + 代理引擎切换 |
| **进程管理** | `processes_list` | 进程列表（含连接快照/进程树） |
| | `processes_ignore` / `processes_unignore` / `processes_ignored_list` | 忽略进程 |
| | `processes_ignore_host` / `processes_unignore_host` / `processes_ignored_hosts_list` | 忽略 host |
| **会话** | `sessions_list` / `sessions_show` / `sessions_switch` / `sessions_delete` / `sessions_create` | 会话管理 |
| **Agent 工作区** | `agent_start` / `agent_end` / `agent_status` | 工作区管理（保存/恢复状态） |
| **日志** | `log_tail` / `log_clear` / `log_export` | 日志管理 |
| **透明代理** | `transparent_proxy_status` / `transparent_proxy_start` / `transparent_proxy_stop` | WinDivert NETWORK 层重定向 80/443（需管理员） |
| **自动修改规则** | `auto_reply_list` / `auto_reply_get` | 规则列表/详情 |
| | `auto_reply_create` | 创建规则（`script_path` 从本地 .py 加载脚本，agent 友好） |
| | `auto_reply_enable` / `auto_reply_disable` / `auto_reply_delete` | 启用/禁用/删除规则 |

> `packets watch`（CLI 阻塞 tail）在 MCP 中用 `packets_list` + `since_id` 非阻塞轮询替代。

### C.4 工具返回格式

所有工具返回 JSON 文本（MCP text content）：

```json
// 成功
{"capturing": true, "session_id": 7, ...}

// 错误
{"ok": false, "error": "错误描述", "hint": "修复建议（可选）"}
```

### C.5 与 CLI 的区别

| 维度 | CLI | MCP |
|---|---|---|
| 通信 | stdio + stdout JSON | JSON-RPC over stdio |
| 输出 | NDJSON（一行一对象） | 单个 JSON 文本 |
| 错误 | stderr JSON + 非零退出码 | 返回 `{"ok": false, "error": "..."}` |
| 截断 | 无 | 大输出自动截断（64KB） |
| 大字段 | 完整返回 | `request_body`/`response_body` 截断到 16KB |
| 参数 | argparse 命令行 | JSON schema（类型注解自动生成） |

### C.6 典型工作流（MCP 版）

```
用户：帮我抓包看看 api.example.com 的请求，把响应里的 status 字段改成 ok

AI 调用：
1. get_status → 确认后端在跑
2. capture_start → 开始抓包，拿到 session_id
3. （用户操作软件触发请求）
4. packets_list --host example.com → 找到目标流量
5. intercept_add --match 'host~=api.example.com' --action 'set-json status ok' --note '改status为ok' --dry_run
   → 预览匹配的流量
6. intercept_add（去掉 dry_run）→ 创建规则
7. （用户再次触发请求，规则生效）
8. intercept_hits → 确认规则命中
9. capture_stop → 停止抓包
```
