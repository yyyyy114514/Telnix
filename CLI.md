# Telnix CLI 速查手册

Telnix CLI（`telnix-cli`）是 Telnix 抓包/拦截工具的命令行入口，用于控制抓包、查询流量、配置拦截规则、重放请求等。所有命令默认输出机器可读的 NDJSON/JSON，便于脚本处理。

## 连接配置

CLI 默认连接本地后端 `http://127.0.0.1:18901`。如需指向其他地址，设置环境变量：

```bash
# Windows (PowerShell)
$env:TELNIX_API = "http://127.0.0.1:18901"

# Linux / macOS
export TELNIX_API="http://127.0.0.1:18901"
```

可选：用 `TELNIX_SESSION` 指定默认会话 ID，省去每次写 `--session`。

### 获取 Telnix 实际端口

Telnix 支持端口配置与随机端口模式（见设置页「端口配置」），实际监听端口可能与默认值 18901/8888 不同：

- **普通模式**：使用 `settings.json` 中的 `api_port` / `proxy_port`（默认 18901 / 8888），端口被占用时自动切换到随机端口。
- **随机端口模式**（`random_port=true`）：每次启动都用 OS 分配的随机端口。
- **MCP 模式**（后端启动加 `--mcp`）：固定 8888/18901，端口被占用直接报错退出，不自动切换。此模式专供 MCP/agent 自动化场景，确保端口可预测。

agent 在不确定端口时，可通过以下方式获取后端实际监听端口：

```bash
# 方式 1：调用 /api/system/ports 接口（推荐，返回 JSON）
# 假设默认 18901 可达：
curl -s http://127.0.0.1:18901/api/system/ports
# 返回示例：
# {"code":0,"data":{"api_port":18901,"proxy_port":8888,"api_host":"127.0.0.1","proxy_host":"127.0.0.1","port_conflict":null},"msg":"ok"}

# 方式 2：直接设置 TELNIX_API 环境变量指向已知端口
# Windows (PowerShell)
$env:TELNIX_API = "http://127.0.0.1:18901"
# Linux / macOS
export TELNIX_API="http://127.0.0.1:18901"
```

如果 agent 启动 Telnix 后端时未使用 `--mcp`，建议先用默认 18901 探测 `/api/system/ports`，若返回的 `api_port` 与请求端口不一致，则改用返回值中的 `api_port` 重新连接。

## 子命令简写速查表

每个子命令都支持简写别名（alias），全称仍然有效。下表列出常用命令及简写。

| 全称 | 简写 | 说明 |
|------|------|------|
| `status` | `st` | 查看后端状态 |
| `capture` | `cap` | 抓包控制（start/stop/clear/pause/resume） |
| `packets` | `pkts` | 流量查询（list/get/search/export/stats…） |
| `intercept` | `itcp` | 拦截/改写规则管理 |
| `replay` | `rep` | 重放单条流量 |
| `replay-batch` | `rep-batch` | 按会话批量重放 |
| `send` | — | 从零构造并发送一个 HTTP 请求 |
| `processes` | `procs` | 进程列表 / 忽略进程与主机 |
| `export` | `exp` | 导出整个会话（har/json/postman…） |
| `sessions` | `sess` | 会话管理（list/show/delete） |
| `proxy` | `pxy` | 系统代理开关 |
| `raw` | — | TCP/UDP 原始抓包 |
| `cert` | — | 证书管理 |
| `transparent-proxy` | `tp` | 透明代理（需管理员权限） |
| `dns-hijack` | `dns` | DNS 劫持（需管理员权限） |
| `auto-reply` | `ar` | 自动回复规则管理 |
| `log` | — | 日志查看/导出 |
| `system` | `sys` | 系统控制（重启/退出/防火墙等） |
| `settings` | `set` | 设置管理（get/set/engine） |
| `tools` | `tool` | 代理工具（no-cache/block-list/map-local…） |
| `agent` | — | Agent 工作区模式 |
| `focus` | — | 聚焦模式（只抓指定进程/主机） |
| `breakpoint` | `bp` | 断点控制 |

二级子命令同样有简写，常用如下：

| 全称 | 简写 | 全称 | 简写 |
|------|------|------|------|
| `start` | `st` | `list` | `ls` |
| `stop` | `sp` | `delete` | `del` |
| `clear` | `clr` | `search` | `find` |
| `pause` | `pz` | `toggle` | `tgl` |
| `resume` | `rs` | `update` | `upd` |
| `export` | `exp` | `import` | `imp` |
| `template` | `tpl` | `enable` | `en` |
| `disable` | `dis` | `create` | `new` |
| `test-script` | `test` | `overview` | `ov` |
| `endpoints` | `eps` | `timeline` | `tl` |
| `watch` | `w` | `trace` | `tr` |
| `analyze` | `az` | `list-all` | `all` |

`tools` 下的工具子命令简写：`no-cache`→`noc`、`force-cors`→`cors`、`block-list`→`block`、`allow-list`→`allow`、`map-local`→`ml`、`map-remote`→`mr`、`mirror`→`mir`。

## 常用参数简写

| 参数 | 简写 | 参数 | 简写 |
|------|------|------|------|
| `--filter` | `-f` | `--session` | `-s` |
| `--limit` | `-n` | `--host` | `-H` |
| `--port` | `-p` | `--method` | `-X` |
| `--url` | `-u` | `--body` | `-b` |
| `--format` | `-F` | `--json-array` | `-J` |
| `--decode` | `-d` | `--tag` | `-T` |
| `--emit-curl` | `-c` | `--all` | `-a` |
| `--json` | `-j` | `--repeat` | `-r` |
| `--interval` | `-i` | `--mode` | `-m` |
| `--layer` | `-L` | `--level` | `-l` |
| `--category` | `-C` | `--protocol` | `-P` |
| `--name` | `-N` | `--keyword` | `-k` |

> 说明：`--header` 未提供 `-h` 简写（`-h` 已被 `--help` 占用）；`--output` 已自带 `-o`，`settings` 的 `--key`/`--value` 已自带 `-k`/`-v`。

## 典型用例

### 1. 抓包

```bash
# 开始抓包（返回 session_id）
telnix-cli cap st

# TCP/UDP 层抓包，过滤端口
telnix-cli cap st -L tcp -p 8080,443

# 60 秒后自动停止（agent 无需 sleep+stop）
telnix-cli cap st --auto-stop 60

# 停止 / 清空当前会话流量 / 暂停 / 恢复
telnix-cli cap sp
telnix-cli cap clr
telnix-cli cap pz
telnix-cli cap rs
```

### 2. 查看流量

```bash
# 列出当前会话最近 20 条
telnix-cli pkts ls -n 20

# 按表达式过滤，并附带可重放的 curl 命令
telnix-cli pkts ls -f "host~=api.example.com && method=POST" -c

# 增量轮询（只返回 id > 1000 的，非阻塞）
telnix-cli pkts ls --since-id 1000

# 查看某条流量详情
telnix-cli pkts get 123

# 跨会话查询全部流量
telnix-cli pkts all -H api.example.com -X POST -n 50

# 搜索请求/响应体内容
telnix-cli pkts find --body-regex "token=[A-Za-z0-9]+" -a

# 监控新流量（阻塞，Ctrl+C 退出）
telnix-cli pkts w -f "host~=api.example.com" -i 1
```

### 3. 重放

```bash
# 重放一条流量
telnix-cli rep 123

# 改方法、加 header、改 host:port，重复 3 次
telnix-cli rep 123 -X POST -H "Authorization: Bearer xxx" -H new.com -p 8080 -r 3

# 批量重放整个会话，保留原始时间间隔
telnix-cli rep-batch -s 5 --preserve-timing

# 从零构造一个请求（Composer）
telnix-cli send -X POST -u https://api.example.com/v1/login -H "Content-Type: application/json" -b '{"u":"a","p":"b"}'
```

### 4. 拦截规则

```bash
# 新增拦截规则（命中即 mock 404）
telnix-cli itcp add --match "host~=api.example.com && path~=/v1/*" --action "mock 404 not found" -N "屏蔽接口"

# 预览匹配（不真正创建）
telnix-cli itcp add --match "host~=x.com" --action "mock 200 ok" --dry-run

# 列出 / 切换启停 / 删除
telnix-cli itcp ls
telnix-cli itcp tgl r1
telnix-cli itcp del r1

# 规则导出 / 导入
telnix-cli itcp exp -o rules.json
telnix-cli itcp imp rules.json -m merge
```

### 5. 自动回复 / 模板

```bash
# 用 Python 脚本创建自动回复规则
telnix-cli ar new --pattern "*api.example.com*/v1/*" --action script --script-path ./hook.py

# 先用 mock 数据测试脚本（不创建规则）
telnix-cli ar test --script-path ./hook.py --mock-resp-status 200

# 应用内置模板
telnix-cli itcp tpl ls
telnix-cli itcp tpl apply mock-404 --match "*api.example.com*/v1/*"
```

### 6. 导出

```bash
# 导出单条流量为 curl
telnix-cli pkts exp 123 -F curl -o flow.sh

# 导出整个会话为 HAR
telnix-cli exp -s 5 -F har -o session.har

# 导出日志
telnix-cli log exp -l ERROR -o error.jsonl
```

### 7. 统计与分析

```bash
# 按主机分组统计
telnix-cli pkts stats --by host

# 多维总览（仪表盘数据源）
telnix-cli pkts ov

# 提取唯一端点（画 API 地图）
telnix-cli pkts eps -H api.example.com

# 时间线（标记大间隔）
telnix-cli pkts tl

# 请求依赖链追踪：从响应中提取字符串，在后续请求中查找
telnix-cli pkts tr 123 -a

# 签名字段自动检测（对比多条流量）
telnix-cli pkts az 100 101 102
```

### 8. 代理工具与设置

```bash
# 系统代理开关
telnix-cli pxy status
telnix-cli pxy on

# 屏蔽列表：加规则、开启
telnix-cli tool block add "*.ads.com" -m wildcard
telnix-cli tool block on

# 本地映射（命中后返回本地文件）
telnix-cli tool ml add "*cdn.example.com*/logo.png" ./local.png
telnix-cli tool ml on

# 切换代理引擎
telnix-cli set engine async

# 读写单项设置
telnix-cli set get -k capture.auto_stop
telnix-cli set set -k capture.auto_stop -v 60
```

### 9. 聚焦与断点

```bash
# 只抓某进程（按名称，避免 PID 变化）
telnix-cli focus on -N chrome.exe

# 开启请求断点（带 10 秒超时自动放行）
telnix-cli bp on --type request --timeout 10

# 放行所有挂起断点
telnix-cli bp release -a
```

## 备注

- 所有简写均为**别名**，原名始终可用；简写不改变任何参数的默认值或行为。
- 同一命令内简写不冲突；不同层级（如一级 `status`/`st` 与二级 `start`/`st`）的相同简写互不影响。
- 完整参数列表与说明请运行：

```bash
telnix-cli --help
telnix-cli <命令> --help
```
