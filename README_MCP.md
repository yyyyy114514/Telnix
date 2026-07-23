# Telnix MCP Server

把 Telnix 的抓包、拦截、改包、重放能力暴露为 MCP (Model Context Protocol) 工具，让 Claude Desktop、Cursor、VS Code Continue 等 MCP 客户端直接调用。

## 它解决什么问题

传统工作流中，AI 助手想帮你分析抓包数据，需要你手动复制粘贴流量内容。接入 MCP Server 后，AI 可以直接调用 `packets_list` 拉流量、调用 `intercept_add` 创建改包规则、调用 `packets_diff` 对比两次请求差异 —— 全程不需要你切换窗口。

73 个工具覆盖 Telnix CLI 的全部功能，从基础的抓包控制到高级的签名字段检测、依赖链追踪都能用。新增 5 个工具（settings × 3 + system_install_dep × 2），总计 **78 个**。

## 架构

```
┌─────────────────┐     stdio (JSON-RPC)     ┌──────────────────┐
│  MCP 客户端      │ ◄──────────────────────► │  telnix.mcp_server │
│  (Claude/Cursor) │                          │  (Python 进程)      │
└─────────────────┘                          └────────┬─────────┘
                                                      │ HTTP API
                                                      ▼
                                             ┌──────────────────┐
                                             │  Telnix 后端      │
                                             │  (127.0.0.1:18901) │
                                             └──────────────────┘
```

MCP Server 是一个独立的 Python 进程，通过 stdio 与 MCP 客户端通信，通过 HTTP 调用 Telnix 后端 API。它不直接操作数据库或文件系统（`agent_start/end` 的状态备份除外），所有数据操作都走后端 API，保证与 CLI 行为一致。

## 环境要求

| 项 | 要求 |
|---|---|
| Python | 3.10+ |
| MCP SDK | `mcp>=1.2`（`pip install "mcp[cli]"`） |
| Telnix 后端 | 运行中，默认监听 `127.0.0.1:18901` |
| 操作系统 | Windows（完整支持）；macOS / Linux（HTTP/HTTPS 抓包可用，TCP/UDP 抓包依赖 WinDivert 暂不支持） |

确认后端在跑：

```powershell
netstat -ano | findstr ":18901.*LISTENING"
```

如果没有输出，启动后端：

```powershell
cd .\src\host
python -m telnix --no-browser
```

`--no-browser` 避免自动打开浏览器干扰你的工作。

## 快速开始

三步跑通第一个工具调用。

### 第一步：安装依赖

```powershell
pip install "mcp[cli]"
```

### 第二步：验证 MCP Server 能启动

```powershell
cd .\src\host
python -m telnix.mcp_server
```

看到 stderr 输出 `[telnix-mcp] starting, base_url=http://127.0.0.1:18901` 就说明启动成功。按 Ctrl+C 退出（它会在 MCP 客户端启动时自动拉起）。

### 第三步：配置客户端

以 Claude Desktop 为例，编辑配置文件（路径：`%APPDATA%\Claude\claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "Telnix": {
      "command": "python",
      "args": ["-m", "telnix.mcp_server"],
      "cwd": ".\\src\\host",
      "env": {
        "TELNIX_API": "http://127.0.0.1:18901"
      }
    }
  }
}
```

重启 Claude Desktop，在对话里说"列出 Telnix 的所有工具"，它会调用 `tools/list` 返回 78 个工具。

## 启动参数

```powershell
# 模块直接运行（推荐）
python -m telnix.mcp_server

# 安装后的入口点（pip install -e . 之后可用）
telnix-mcp

# 指定后端地址
python -m telnix.mcp_server --base-url http://127.0.0.1:18901

# 用环境变量指定后端地址
set TELNIX_API=http://127.0.0.1:18901
python -m telnix.mcp_server
```

`--base-url` 优先级高于环境变量。两个都没设则用默认值 `http://127.0.0.1:18901`。

## 客户端接入配置

### Claude Desktop

配置文件路径：

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "Telnix": {
      "command": "python",
      "args": ["-m", "telnix.mcp_server"],
      "cwd": ".\\src\\host",
      "env": {
        "TELNIX_API": "http://127.0.0.1:18901"
      }
    }
  }
}
```

如果 `python` 不在 PATH，用完整路径：

```json
{
  "mcpServers": {
    "Telnix": {
      "command": "C:\\Users\\Administrator\\AppData\\Local\\Programs\\Python\\Python311\\python.exe",
      "args": ["-m", "telnix.mcp_server"],
      "cwd": ".\\src\\host",
      "env": {
        "TELNIX_API": "http://127.0.0.1:18901"
      }
    }
  }
}
```

### Cursor

在项目根目录创建 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "Telnix": {
      "command": "python",
      "args": ["-m", "telnix.mcp_server"],
      "cwd": ".\\src\\host",
      "env": {
        "TELNIX_API": "http://127.0.0.1:18901"
      }
    }
  }
}
```

或在 Cursor 设置 → MCP 中添加 Server，填入相同的 command/args/env。

### VS Code (Continue 插件)

编辑 `~/.continue/config.json`，在 `experimental.mcpServers` 中添加：

```json
{
  "experimental": {
    "mcpServers": {
      "Telnix": {
        "command": "python",
        "args": ["-m", "telnix.mcp_server"],
        "cwd": ".\\src\\host",
        "env": {
          "TELNIX_API": "http://127.0.0.1:18901"
        }
      }
    }
  }
}
```

### 通用 MCP 客户端

任何支持 MCP stdio 传输的客户端都能接入。核心配置：

- **command**: `python`
- **args**: `["-m", "telnix.mcp_server"]`
- **cwd**: `.\src\host`（确保能找到 telnix 包）
- **env**: `{"TELNIX_API": "http://127.0.0.1:18901"}`

### 后端不在默认端口时

如果 Telnix 后端跑在其他端口（比如 18902），改 env 或加 `--base-url`：

```json
{
  "env": {
    "TELNIX_API": "http://127.0.0.1:18902"
  }
}
```

或 args 里加参数：

```json
{
  "args": ["-m", "telnix.mcp_server", "--base-url", "http://127.0.0.1:18902"]
}
```

## 工具清单

78 个工具，按功能分 12 类。

### 状态与抓包控制（6 个）

| 工具 | 说明 |
|---|---|
| `get_status` | 后端状态（抓包/会话/代理/证书） |
| `capture_start` | 开始抓包，返回 session_id |
| `capture_stop` | 停止抓包（会话保留，代理仍运行） |
| `capture_clear` | 清空当前会话流量 |
| `capture_pause` | 暂停抓包记录 |
| `capture_resume` | 恢复抓包记录 |

### 流量查询（7 个）

| 工具 | 说明 |
|---|---|
| `packets_list` | 当前会话流量列表 |
| `packets_list_all` | 跨会话流量列表（全局分析） |
| `packets_get` | 单个流量详情 |
| `packets_search` | 正则搜索 body |
| `packets_stats` | 流量统计（按 host/method/status 分组） |
| `packets_delete` | 删除流量 |
| `packets_clear` | 清空流量 |

### 流量高级分析（7 个）

| 工具 | 说明 |
|---|---|
| `packets_export` | 单 flow 导出为 curl/python-requests/postman/json/csv |
| `packets_tag` | 流量标签管理（add/remove/clear/note） |
| `packets_diff` | 对比两条流量字段，输出 unified diff |
| `packets_endpoints` | 唯一 endpoint 提取（path 模板归一化，画 API 地图） |
| `packets_timeline` | 流量时间线（按时间排序，标注大间隔段落） |
| `packets_trace` | 请求依赖链 trace（从响应提取值，在后续请求里搜索） |
| `packets_analyze` | 签名字段自动检测（对比多条请求找可疑 token/sign 字段） |

### 拦截规则（10 个）

| 工具 | 说明 |
|---|---|
| `intercept_add` | 添加规则（支持 dry_run 预览 / idempotent 幂等） |
| `intercept_list` | 列出规则（含命中统计） |
| `intercept_del` | 删除规则 |
| `intercept_hits` | 查看规则命中详情 + 最后命中的流量 |
| `intercept_toggle` | 启用/禁用规则 |
| `intercept_update` | 修改现有规则 |
| `intercept_export` | 导出所有规则为 JSON |
| `intercept_import` | 从 JSON 导入规则（merge/replace 两种模式） |
| `intercept_template_list` | 列出内置规则模板 |
| `intercept_template_apply` | 应用模板创建规则 |

### 重放与发包（4 个）

| 工具 | 说明 |
|---|---|
| `replay` | 重放流量（可覆盖 body/method/url/headers） |
| `send_request` | 从零发包（Composer，不依赖已有 flow） |
| `replay_batch` | 批量时序重放（支持并发 / 保留原始时间间隔） |
| `session_export` | 导出会话为 har/json/csv 等格式 |

### 断点控制（5 个）

| 工具 | 说明 |
|---|---|
| `breakpoint_status` | 查看断点状态 |
| `breakpoint_on` | 开启断点（request/response） |
| `breakpoint_off` | 关闭断点 |
| `breakpoint_release` | 放行/丢弃断点暂停的流量（支持批量） |
| `breakpoint_timeout` | 设置断点自动放行超时 |

### 系统控制（12 个）

| 工具 | 说明 |
|---|---|
| `proxy_status` / `proxy_on` / `proxy_off` | 系统代理控制 |
| `cert_status` / `cert_install` / `cert_remove` | HTTPS 证书管理 |
| `focus_status` / `focus_on` / `focus_off` | 专注模式（只抓指定进程/host） |
| `raw_capture_status` / `raw_capture_start` / `raw_capture_stop` / `raw_capture_install` | TCP/UDP 原始抓包 |
| `system_restart` / `system_quit` / `system_restart_as_admin` | 系统控制（重启/退出/管理员提权） |
| `system_install_dep` / `system_install_dep_status` | 可选依赖安装（如 mitmproxy） |

### 设置管理（3 个）

| 工具 | 说明 |
|---|---|
| `settings_get` | 读取所有设置或单个 key（`key` 省略返回全部） |
| `settings_set` | 写入单个设置项（value 自动 JSON 反序列化 bool/数字/list/dict） |
| `settings_proxy_engine` | 查看/切换代理引擎：builtin（默认线程）/ async（asyncio）/ mitmproxy（需先 `system_install_dep`）。切换后需调 `system_restart` 生效 |

### 进程管理（7 个）

| 工具 | 说明 |
|---|---|
| `processes_list` | 进程列表（含连接快照/进程树） |
| `processes_ignore` / `processes_unignore` / `processes_ignored_list` | 忽略进程 |
| `processes_ignore_host` / `processes_unignore_host` / `processes_ignored_hosts_list` | 忽略 host |

### 会话管理（5 个）

| 工具 | 说明 |
|---|---|
| `sessions_list` | 列出所有会话 |
| `sessions_show` | 查看会话详情 |
| `sessions_switch` | 切换会话 |
| `sessions_delete` | 删除会话 |
| `sessions_create` | 创建新会话并切换 |

### Agent 工作区（3 个）

| 工具 | 说明 |
|---|---|
| `agent_start` | 保存当前状态 → 禁用规则/专注/断点 → 把 agent 进程加入忽略列表 |
| `agent_end` | 恢复 `agent_start` 之前的状态 |
| `agent_status` | 查看工作区状态 |

### 日志（3 个）

| 工具 | 说明 |
|---|---|
| `log_tail` | 查看最近日志 |
| `log_clear` | 清空日志 |
| `log_export` | 导出日志为 JSONL |

## 返回格式

所有工具返回 JSON 文本（MCP text content）。

### 成功

直接返回数据对象，JSON 格式：

```json
{
  "session_id": 7,
  "capturing": true,
  "flow_count": 42
}
```

### 错误

返回 `{"ok": false, "error": "...", "hint": "..."}` 结构：

```json
{
  "ok": false,
  "error": "无法连接后端 http://127.0.0.1:18901: [Errno 111] Connection refused",
  "hint": "后端未启动？运行: cd src\\host && python -m telnix"
}
```

`hint` 字段是可选的，提供 agent 可操作的修复建议。常见 hint：

- 后端未启动 → 提示启动命令
- 证书未安装 → 提示调 `cert_install`
- 需要管理员权限 → 提示调 `system_restart_as_admin`
- 会话不存在 → 提示调 `capture_start`

### 输出截断

MCP 工具返回过大会让客户端卡死，因此有两层截断：

- 总输出上限 64KB（超过则尾部截断并标注原始大小）
- 大字段截断：`request_body`、`response_body`、`raw_data` 单字段超过 16KB 时截断

如果需要完整的大字段内容，用 `packets_get(flow_id, field="response_body")` 单独取。

## 匹配表达式语法

`intercept_add`、`packets_list` 的 `filter_expr` 参数用统一匹配表达式：

```
key op value && key op value && ...
```

### 支持的 key

| key | 含义 | 示例值 |
|---|---|---|
| `host` | 请求主机 | `api.example.com` |
| `method` | HTTP 方法 | `GET`、`POST` |
| `path` | 请求路径 | `/api/v1/users` |
| `url` | 完整 URL | `https://api.example.com/v1/users` |
| `status` | 响应状态码 | `200`、`404` |
| `pid` | 进程 PID | `1234` |
| `process` | 进程名 | `chrome.exe` |

### 支持的 op

| op | 含义 | 示例 |
|---|---|---|
| `=` | 精确相等 | `method=POST` |
| `~=` | 通配符匹配（`*` 匹配任意串，`?` 匹配单字符） | `host~=*.example.com` |
| `!=` | 不等于 | `method!=OPTIONS` |
| `>=` `<=` `>` `<` | 数值比较 | `status>=400` |

### 示例

```
host~=api.example.com && method=POST
host~=*.example.com && status>=400
process=chrome.exe && path~=*/api/v1/*
method=POST && status=200 && path~=*/login*
```

## 动作规范语法

`intercept_add` 的 `action` 参数用动作规范字符串。分三大类。

### 改响应

| 动作 | 语法 | 示例 |
|---|---|---|
| 设置 JSON 字段 | `set-json key value` | `set-json status ok` |
| 设置 JSON 字段（JSONPath） | `set-json-path path value` | `set-json-path $.data.user.name "张三"` |
| 删除 JSON 字段 | `remove-json key` | `remove-json debug_info` |
| 删除 JSON 字段（JSONPath） | `remove-json-path path` | `remove-json-path $.data.debug` |
| 替换响应头 | `replace-header K V` | `replace-header Content-Type application/xml` |
| 按偏移替换字节 | `replace-bytes offset:hex` | `replace-bytes 10:48656c6c6f` |
| 按正则替换字节 | `replace-bytes-regex regex hex` | `replace-bytes-regex "status.{0,3}" 6f6b` |
| Mock 整个响应 | `mock CODE BODY` | `mock 200 {"ok":true}` |
| 只改状态码 | `status CODE` | `status 204` |
| 丢弃请求（返回 503） | `drop` | `drop` |
| 固定请求内容（不转发） | `mock-request BODY [CTYPE]` | `mock-request {"k":"v"}` |

### 改请求

| 动作 | 语法 | 示例 |
|---|---|---|
| 设置请求头 | `set-request-header K V` | `set-request-header Authorization "Bearer xxx"` |
| 设置请求 JSON 字段 | `set-request-json key value` | `set-request-json version 2` |
| 设置请求 JSON 字段（JSONPath） | `set-request-json-path path value` | `set-request-json-path $.user.name "test"` |
| 删除请求 JSON 字段 | `remove-request-json key` | `remove-request-json password` |
| 设置请求 body（hex） | `set-request-body-hex hex` | `set-request-body-hex 48656c6c6f` |
| 按偏移替换请求字节 | `replace-request-bytes offset:hex` | `replace-request-bytes 5:6f6b` |

### 时序动作

| 动作 | 语法 | 示例 |
|---|---|---|
| 延迟响应 | `delay N` | `delay 2000`（延迟 2 秒响应） |
| 延迟请求 | `delay-request N` | `delay-request 1000`（延迟 1 秒发送） |

### 值类型自动推断

`set-json` / `set-request-json` 的 value 参数会自动推断类型：

- `true` / `false` → 布尔值
- `null` → JSON null
- `123` → 整数
- `1.5` → 浮点数
- 其他 → 字符串

想强制传字符串，用引号包裹：`set-json count "123"`。

## 典型工作流

### 场景一：抓包分析某个 API

```
用户：帮我抓包看看 api.example.com 的请求

AI 工作流：
1. get_status → 确认后端在跑
2. capture_start → 开始抓包，拿到 session_id
3. （用户操作软件触发请求）
4. packets_list --host example.com → 找到目标流量
5. packets_get --flow_id 42 → 查看详情
6. capture_stop → 停止抓包
```

### 场景二：自动改包

```
用户：把 api.example.com/login 响应里的 status 字段改成 ok

AI 工作流：
1. intercept_add --match 'host~=api.example.com && path~=*/login' \
     --action 'set-json status ok' --note '改status为ok' --dry_run
   → 先预览会命中的流量
2. intercept_add（去掉 dry_run）→ 创建规则
3. （用户再次触发请求，规则生效）
4. intercept_hits --rule_id N → 确认规则命中
5. packets_get --flow_id 最新ID → 验证响应已改
```

### 场景三：依赖链追踪

```
用户：这个登录请求返回的 token 后续被哪些请求用到了？

AI 工作流：
1. packets_trace --flow_id 42 --search_all true
   → 从 flow 42 的响应提取 token/session_id 等值
   → 在后续流量的请求头/body 里搜索
   → 返回依赖关系列表
```

### 场景四：签名字段检测

```
用户：这个 API 的签名算法是什么？帮我找签名字段

AI 工作流：
1. packets_analyze --flow_ids "42,43,44" 
   → 对比 3 次同接口请求的 JSON body
   → 找出值会变、长度固定、字符集一致的字段
   → 返回可疑字段列表（按 suspicious_score 排序）
2. 对高置信度字段进一步分析（长度、字符集、变化规律）
```

### 场景五：批量重放压测

```
用户：把刚才的会话重放一遍，看看接口性能

AI 工作流：
1. replay_batch --session 7 --parallel 5
   → 5 并发重放会话 7 的全部流量
   → 返回每条请求的状态码和耗时
2. packets_stats --session 7 → 对比重放前后的统计
```

### 场景六：Agent 工作区

`agent_start` / `agent_end` 用于 AI 临时清空工作区，避免干扰用户现有配置：

```
AI 工作流：
1. agent_start → 保存当前规则/专注/断点状态，清空工作区
2. （AI 做自己的抓包/改包工作，不影响用户已有规则）
3. agent_end → 恢复用户原始状态
```

`agent_start` 会：
- 保存当前所有规则、专注模式、断点状态到临时备份文件
- 禁用所有规则、关闭专注模式、关闭断点
- 把 agent 自己的进程加入忽略列表（不抓自己的流量）

`agent_end` 会：
- 从备份恢复所有规则、专注模式、断点状态
- 把 agent 进程从忽略列表移除
- 删除备份文件

如果 `agent_start` 后程序异常退出没调 `agent_end`，下次 `agent_start` 会报错提示先恢复。调 `agent_status` 可以查看当前是否有未恢复的工作区。

## 与 CLI 的对比

| 维度 | CLI | MCP |
|---|---|---|
| 通信方式 | stdio + stdout NDJSON | JSON-RPC over stdio |
| 输出格式 | 每行一个 JSON 对象 | 单个 JSON 文本 |
| 错误处理 | stderr JSON + 非零退出码 | 返回 `{"ok": false, "error": "..."}` |
| 大输出 | 完整返回 | 自动截断（64KB 总量 / 16KB 单字段） |
| 参数传递 | argparse 命令行参数 | JSON schema（类型注解自动生成） |
| 阻塞命令 | `packets watch` 支持阻塞 tail | 无阻塞工具，用 `packets_list` + `since_id` 轮询替代 |
| 调用方 | 脚本/终端 | AI 助手/IDE |

### CLI 到 MCP 的命令映射

| CLI 命令 | MCP 工具 |
|---|---|
| `telnix status` | `get_status` |
| `telnix capture start` | `capture_start` |
| `telnix packets list` | `packets_list` |
| `telnix packets list-all` | `packets_list_all` |
| `telnix packets get N` | `packets_get(flow_id=N)` |
| `telnix packets search` | `packets_search` |
| `telnix packets watch` | `packets_list(since_id=N)` 循环轮询 |
| `telnix intercept add` | `intercept_add` |
| `telnix intercept list` | `intercept_list` |
| `telnix replay N` | `replay(flow_id=N)` |
| `telnix send` | `send_request` |
| `telnix proxy on` | `proxy_on` |
| `telnix cert install` | `cert_install` |
| `telnix system restart` | `system_restart` |
| `telnix agent start` | `agent_start` |

## 故障排查

### MCP 客户端连不上 Server

检查清单：

1. `python` 是否在 PATH：`where python`
2. `telnix.mcp_server` 模块是否能导入：`python -c "import telnix.mcp_server"`
3. `cwd` 是否正确：配置文件里的 `cwd` 必须指向 `src\host` 目录（包含 `telnix` 包的目录）
4. 看 Claude Desktop 的日志：`%APPDATA%\Claude\logs/`

### 工具调用返回"无法连接后端"

```json
{"ok": false, "error": "无法连接后端 http://127.0.0.1:18901: ..."}
```

后端没启动。启动后端：

```powershell
cd .\src\host
python -m telnix --no-browser
```

### 工具调用返回"需要管理员权限"

```json
{"ok": false, "error": "...", "hint": "请用 system_restart_as_admin 工具以管理员身份重启"}
```

TCP/UDP 抓包需要管理员权限。调 `system_restart_as_admin`，后端会弹 Windows UAC 窗口，用户同意后以管理员身份重启。

### 工具调用返回"无活动会话"

```json
{"ok": false, "error": "无活动会话，请先 capture_start 或用 session 参数指定"}
```

调 `capture_start` 创建会话，或用 `session` 参数指定已有会话 ID。

### 证书未安装导致 HTTPS 抓不到

调 `cert_status` 检查状态，如果 `cert_installed=false`，调 `cert_install` 安装根证书（需要 UAC 提权）。

### 输出被截断

返回内容末尾出现 `... [输出已截断...]` 说明超过 64KB 上限。用更具体的过滤条件减少返回量，或用 `packets_get(flow_id, field="response_body")` 单独取大字段。

### system_restart_as_admin 超时

```json
{"ok": false, "error": "等待用户响应超时（3 分钟无响应）"}
```

UAC 弹窗 3 分钟内没人点。调 `system_restart_as_admin` 重试，确保用户在电脑前。

### system_restart_as_admin 被用户拒绝

```json
{"ok": false, "error": "用户拒绝了管理员重启请求", "rejected_by_user": true}
```

用户在 GUI 弹窗里点了"拒绝"。需要用户同意后重试，或手动以管理员身份启动 Telnix。

## 设计原则与限制

### 设计原则

1. **纯 HTTP 调用**：MCP Server 只做后端 API 的薄封装，不直接操作数据库/文件（`agent_start/end` 的状态备份除外），保证与 CLI 行为一致
2. **错误不抛异常**：所有错误封装成 `{"ok": false, "error": "...", "hint": "..."}` 返回，符合 MCP 工具返回规范
3. **输出截断**：64KB 总输出 + 16KB 大字段截断，防止 MCP 消息过大卡死客户端
4. **STDIO 安全**：绝不写 stdout（MCP 协议约束），日志走 stderr
5. **参数 schema 自动生成**：Python 类型注解 + docstring 自动生成 MCP 工具 schema，无需手写

### 已知限制

- `packets watch`（CLI 阻塞 tail）未实现为 MCP 工具，因为 MCP 工具调用不应长期阻塞。用 `packets_list(since_id=N)` 非阻塞轮询替代
- 大流量场景（>50000 条）下 `packets_list_all` / `replay_batch` 可能较慢，建议用 `host`/`since_id` 参数缩小范围
- `packets_analyze` 的签名字段检测是启发式算法，会漏报（复杂签名算法）和误报（正常变化的业务字段），需要人工复核
- `packets_trace` 的依赖链追踪基于字符串精确匹配，如果值被编码/加密则追踪不到
- 不支持同时连接多个 Telnix 后端实例（`BASE_URL` 是全局变量）

## 开发者笔记

### 修改工具

所有工具定义在 `src/host/telnix/mcp_server.py`。添加新工具只需：

```python
@mcp.tool()
def my_new_tool(param1: str, param2: int = 0) -> str:
    """工具描述（会显示给 MCP 客户端）。

    Args:
        param1: 参数说明
        param2: 参数说明
    """
    res = _api("GET", "/my-endpoint")
    data, err = _ok(res)
    if err:
        return _error(err)
    return _result(data)
```

FastMCP 会自动从类型注解和 docstring 生成 MCP 工具 schema。

### 调试

启动时加环境变量看 HTTP 请求：

```powershell
set TELNIX_MCP_DEBUG=1
python -m telnix.mcp_server
```

或直接用 MCP Inspector 测试：

```powershell
npx @modelcontextprotocol/inspector python -m telnix.mcp_server
```

### 测试

验证所有工具注册：

```powershell
cd .\src\host
python -c "from telnix.mcp_server import mcp; t=mcp._tool_manager._tools; print(f'{len(t)} tools'); [print(' ', n) for n in sorted(t)]"
```

端到端测试（模拟 MCP 客户端调用）：

```python
import json, subprocess, sys
proc = subprocess.Popen(
    [sys.executable, "-m", "telnix.mcp_server"],
    cwd=r".\src\host",
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8",
    env={**__import__("os").environ, "TELNIX_API": "http://127.0.0.1:18901"},
)
def send(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())

send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "test", "version": "1.0"}}})
proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
proc.stdin.flush()

resp = send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "get_status", "arguments": {}}})
print(resp["result"]["content"][0]["text"])
```

### 入口点

`pyproject.toml` 已配置入口点：

```toml
[project.scripts]
telnix = "telnix.__main__:main"
telnix-mcp = "telnix.mcp_server:main"
```

`pip install -e .` 之后可以直接用 `telnix-mcp` 命令启动。
