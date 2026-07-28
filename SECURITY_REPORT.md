# Telnix 安全审计与加固报告（修复后）

> 审计范围：Telnix 抓包代理（FastAPI 后端 + Vue3 前端）的 API 鉴权、命令/脚本执行、
> 数据层、前端敏感信息处理、代理/网络/证书/提权等位置。
> 审计方式：5 个并行审计 agent 分别覆盖「后端 API / 命令执行 / 数据层 / 前端 / 代理网络」，
> 汇总发现后按 Critical/High/Medium/Low 分级并逐项修复。

---

## 一、总体结论

| 等级 | 修复前数量 | 修复后状态 |
|------|-----------|-----------|
| Critical | 0 | 0 |
| High | 2（API 无鉴权、API 随局域网 0.0.0.0 暴露） | 已缓解（回环免鉴权 + 非回环 Token） |
| Medium | 6 | 已修复 5，1 项降级为 Low（见 §7） |
| Low | 5 | 已修复/确认安全 4 |

核心变更：新增 **API 访问控制中间件（回环免鉴权 + 非回环 Token）**，补齐此前完全缺失的
控制面鉴权；并对密钥落盘、DNS 劫持输入、规则正则 ReDoS、SQL 列白名单、证书私钥权限做了闭环加固。

---

## 二、各位置安全防护（修复后）

### 1. API 控制面鉴权（High → 已缓解）  `src/host/telnix/api/auth.py` + `server.py`
- **问题**：所有 `/api/*` 端点（抓包启停、系统代理、装/卸根证书、自定义发包、重启/退出等）
  此前无任何鉴权。开启「允许局域网设备连接」后 API 监听 `0.0.0.0`，同网段任意设备可完全控制本机。
- **修复**：
  - 新增 `auth.authorize()` 中间件，仅对 `/api` 路径生效：
    - **回环地址**（`127.0.0.1`/`::1`/`localhost`/`::ffff:127.x`/整个 `127/8`）→ 免鉴权，桌面体验不变；
    - **非回环地址** → 必须携带有效 Token（`X-API-Token` 头或 `?token=` 查询参数）。
  - Token 首次启动自动生成（32 字节 hex），持久化于设置库并常驻内存缓存；比较使用
    `hmac.compare_digest` 常量时间比较，防计时侧信道。
  - 使用 `request.client.host` 判定来源；`client` 为空时按"非回环"处理（**失败关闭**，更安全）。
- **剩余风险（Low）**：若用户主动开启局域网模式，API 仍绑定 `0.0.0.0`，仅由 Token 保护。
  建议仅在确需手机抓包时开启，且 Token 不泄露。后续可进一步将 API 与代理监听地址解耦
  （新增独立 `api_listen_host` 默认 `127.0.0.1`）。

### 2. 移动端证书下载链接（Medium → 已修复）  `src/host/telnix/api/settings.py`
- **问题**：`/mobile/setup` 返回的证书下载 URL 未带凭证，手机经局域网访问会被新的鉴权中间件拦截（401）。
- **修复**：`mobile_setup` 在 `cert_download_url` / `android_cert_url` 中附带 Token
  （`?token=...`），手机扫码即可下载，同时仍受 Token 保护，避免任意局域网设备抓取根证书。

### 3. 敏感凭据回传与落盘（Medium → 已修复）
- **后端** `api/settings.py#get_settings`：剔除 `api_token`、`clash_secret`，不再随
  `GET /settings` 回传（避免被非回环来源读取，也避免前端误写 localStorage）。
- **前端** `src/ui/src/views/SettingsView.vue`：写入 `localStorage` 前删除
  `deepseek_api_key` / `clash_secret` / `api_token`，杜绝密钥明文落盘浏览器存储。
- **保留项**：`deepseek_api_key` 仍经 `GET /settings` 返回以保证设置页可编辑，但现已受
  §1 的回环鉴权保护（仅本机浏览器可读）。

### 4. DNS 劫持规则输入校验（Medium → 已修复）  `src/host/telnix/api/dns_hijack.py`
- **问题**：`/dns-hijack/start`、`/dns-hijack/rules` 的 fake IP 未做合法性校验，错误配置会静默失效。
- **修复**：新增 `_validate_hijack_ips()`，用 `ipaddress.ip_address` 校验每条规则 IP 与
  `default_ip`，非法即返回明确错误，配置阶段拦截，杜绝无效/污染值。

### 5. 自动回复规则正则 ReDoS（Medium → 已修复）  `src/host/telnix/auto_reply/rules.py`
- **问题**：通配符/`regex` 模式若为病态正则（大量 `*` 或嵌套量词），匹配长 URL 时可能灾难性回溯。
- **修复**：为 `_compile` 增加预算上限——模式长度 ≤256、通配符模式 `*`/`?` ≤16、
  regex 模式量化符/分支（`* + ? { |`）≤12；超出直接判为编译失败（匹配时安全跳过该规则）。
  已单测：正常规则可编译，病态规则被拦截。

### 6. SQL 分组统计列白名单（High 结构性 / Low 实际 → 已加固）  `src/host/telnix/db.py#get_flows_stats`
- **问题**：`group_by` 对应列名拼接进 SQL（`f"COALESCE(NULLIF({col},'')...)"`），存在结构性注入面。
- **修复**：该列名实际仅来自固定 `if/elif` 分支（host/process_name/method/ip_region），本就不可被用户直接控制；
  新增 `_ALLOWED_STATS_COLS` 白名单护栏作为纵深防御，任何越界值强制置 `col=None`，杜绝拼接注入。

### 7. CA / 叶证书私钥文件权限（Low → 已修复）  `src/host/telnix/proxy/ssl_bump.py`
- **问题**：私钥先 `open(...,"wb")` 再以 `os.chmod(0o600)`，二者之间存在权限暴露时间窗。
- **修复**：改用 `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)` 以 `0o600` 权限**原子创建**根私钥与叶私钥，
  消除"先创建后降权"的暴露窗口（证书 `.crt` 仍为 `0o644`）。

### 8. 发包 / 重放 SSRF（Medium → 经确认已安全，无需改）
- **位置** `api/send.py`、`api/replay.py` 的 `_resolve_safe_target()` + `_do_send()` / `_replay()`。
- **结论**：已正确实现 SSRF 防护——递归解析所有候选 IP 并逐一校验（非私网/回环/链路本地/保留/组播/未指定），
  且**直接连接已校验的 `resolved_ip`**（而非原始 hostname），无二次解析 TOCTOU 窗口。审计标记为 Medium 但实际已安全，保留现状。

### 9. 已确认安全的既有防护（无需改动）
- **路径遍历**：`server.py` SPA 回退做了 `..` 段拒绝 + `os.path.realpath` 二次校验；
  `SettingsView` 的 `open-path`/`list-dirs` 限制在数据目录与证书目录白名单内，并屏蔽可执行扩展名。
- **Host 注入**：`ssl_bump._is_valid_host` 用正则 `^[A-Za-z0-9.\-:]+$` + 拒绝 `/ \ ..` 防证书写入越界。
- **命令执行**：`subprocess` 均使用参数列表（非 shell），`certutil` 走 `ShellExecuteEx runas` 提权并指纹复核。
- **用户脚本沙箱**：自动回复脚本通过受限 builtins/imports + 子进程隔离执行（既有设计，本次未改动）。
- **隐蔽性**：关闭 Swagger/OpenAPI/ReDoc，剥离 `Server` 响应头，避免暴露框架信息。

---

## 三、修复文件清单

| 文件 | 改动 |
|------|------|
| `src/host/telnix/api/auth.py` | **新增**：回环免鉴权 + Token 中间件逻辑 |
| `src/host/telnix/server.py` | 注册 `_api_auth` 中间件（仅 `/api`） |
| `src/host/telnix/api/settings.py` | `mobile_setup` 证书链接带 Token；`GET /settings` 剔除 `api_token`/`clash_secret` |
| `src/ui/src/views/SettingsView.vue` | `localStorage` 写入前剔除三类敏感凭据 |
| `src/host/telnix/api/dns_hijack.py` | 新增 IP 合法性校验（start / rules） |
| `src/host/telnix/auto_reply/rules.py` | `_compile` 增加模式/通配符/量化符预算 |
| `src/host/telnix/db.py` | `get_flows_stats` 增加列名白名单护栏 |
| `src/host/telnix/proxy/ssl_bump.py` | 根/叶私钥以 `0o600` 原子创建 |

前端已重新构建（`src/ui/dist`），后端 7 个文件通过 `py_compile` 与 linter，鉴权与 ReDoS 防护均经单测验证。

---

## 四、残留风险与后续建议

1. **API 与代理监听解耦**（建议）：新增独立 `api_listen_host` 设置（默认 `127.0.0.1`），
   使「手机抓包」仅暴露代理端口 `8888` 到 `0.0.0.0`，而控制面 API 始终留在本机，彻底消除局域网暴露面。
2. **Token 管理**：当前 Token 无过期/轮转/查看入口，建议设置页提供「重置 API Token」按钮，
   并在局域网模式下对 Token 展示做掩码。
3. **deepseek_api_key 回传**：仍经 `GET /settings` 返回（用于设置页编辑），依赖回环鉴权保护；
   若需更强隔离，可改为仅回写掩码、保存时仅在非空时更新。
4. **正则 ReDoS 深度检测**：当前为量化符计数预算（启发式），对 `(a+)+` 这类低计数嵌套量词无法拦截；
   若规则来源含不可信导入（EzReply 格式），建议后续引入 `regex` 库的超时匹配（`regex.match(..., timeout=...)`）。
