# Telnix 安全审计报告 (Batch 1)

> 审计范围：src/host/telnix/ 后端 + src/ 前端；忽略 i18n。
> 严重级别：P0 致命 / P1 高 / P2 中 / P3 低

## 汇总表

| # | 类别 | 级别 | 文件:行 | 状态 |
|---|------|------|---------|------|
| S1 | 命令注入(curl 导出) | P1 | api/export.py:228-258 | 确凿 |
| S2 | TLS 验证关闭(send/replay) | P2 | api/send.py:124-128, api/replay.py:136-140 | 确凿(设计权衡) |
| S3 | SSRF 守卫依赖 env 可绕过 | P2 | api/send.py:64-69, api/replay.py:64-68 | 确凿 |
| S4 | 透明代理 SNI 连接无 SSRF 守卫 | P3 | proxy/server.py:1264-1270,1326 | 可疑 |
| S5 | 局域网 0.0.0.0 监听放大面 | P2 | config.py:70-88, server.py:101-109 | 确凿(设计权衡) |
| S6 | 日志泄露敏感信息(AI) | P1 | ai/deepseek.py:393,446 | 确凿 |
| S7 | verify 关闭(DeepSeek) | P3 | ai/deepseek.py:379-383 | 确凿(需 env) |
| S8 | 沙箱非绝对安全 | P2 | auto_reply/script_runner.py:275-282 | 可疑 |
| — | SQL 注入 | — | db.py | 已防护(白名单+参数化) |
| — | 路径遍历 | — | 导入/快照/证书 | 已防护 |

## 详情

### S1 [P1] curl 导出命令注入 — api/export.py:228-258
`_build_curl` 把请求头 `k: v` 直接以 `-H 'k: v'` 拼入 shell 字符串，请求体以 `-d body`、URL 直接追加。导出内容来自抓包数据（攻击者可构造恶意响应头/URL）。
修复：导出 curl 用 `shlex.quote` 严格转义，或改用 `--config` 文件避免内联注入；头值中的引号/换行需转义。

### S2 [P2] TLS 验证关闭 — api/send.py:124-128 / api/replay.py:136-140
`verify_mode=ssl.CERT_NONE` 且 `check_hostname=False`，重放/发送不校验服务器证书。抓包工具常见设计，但易被中间人利用。

### S3 [P2] SSRF 守卫可被 env 禁用 — api/send.py:64-69 / api/replay.py:64-68
`_resolve_safe_target` 在 `TELNIX_DISABLE_SSRF_GUARD=1` 时直接返回解析 IP 且无私网校验。该变量为全局进程级，一旦设置 send/replay 即可对内网/元数据(169.254.169.254)发起 SSRF。守卫逻辑本身(is_private/loopback/link_local/reserved 全拒才放)正确，但禁用开关过宽松。
修复：将禁用开关限制为仅本地回环源生效，或默认强制开启并记录告警。

### S4 [P3] 透明代理 SNI fallback 连接无 SSRF 守卫 — proxy/server.py:1264-1270
`target=(sni_host,443)` 后直连。SNI 由客户端 ClientHello 提供，属客户端可控，但此处是代理转发被拦截流量，利用性低，标注可疑。

### S5 [P2] 局域网监听放大攻击面 — config.py:70-88 / server.py:101-109
开启"允许局域网"后 API/代理监听 0.0.0.0，仅依赖 `X-API-Token`。Token 由 `secrets.token_hex(32)` 生成、hmac 比较安全，但泄露后局域网任意设备可触发装根证书/提权等高危操作。建议文档明确提示并支持 Token 轮换。

### S6 [P1] 日志泄露敏感信息 — ai/deepseek.py:393,446
日志 `messages={...}` 含完整请求/响应体(含 Authorization、Cookie、token)，经 `/api/logs` 与 `/api/logs/export` 可读。deepseek_api_key 未直接打印(Authorization 头在请求中未入日志)，但 messages 上下文会被记录导出，构成敏感泄露。
修复：日志对请求头脱敏(屏蔽 Authorization/Set-Cookie/Cookie)，或不在日志中记录 flow 明细。

### S7 [P3] DeepSeek verify 可 env 关闭 — ai/deepseek.py:379-383
`TELNIX_DEEPSEEK_INSECURE=1` 禁用证书校验，暴露 Bearer key 给中间人。需 env 触发。

### S8 [P2] 沙箱非绝对安全 — auto_reply/script_runner.py:275-282
注释自承沙箱非绝对安全，依赖子进程隔离+资源限制+Job Object。rule 脚本可由快照导入/AI 自动创建引入外部脚本。建议保持最小环境变量白名单(已实现)并考虑禁用脚本动作网络权限。

## 已验证安全(排除项)
- SQL 注入：db.py 列名经 `_VALID_COL_NAME` 白名单 + 参数化占位符，无注入。
- 路径遍历：ssl_bump.py `_is_valid_host` 拒绝 `/ \ ..`；server.py SPA 路由双重 realpath；script_runner 对 rule_id sanitize+basename。均无问题。
- 认证：api/auth.py 回环免鉴权 + 非回环 hmac.compare_digest，实现正确。
- 命令执行：elevation.py / ssl_bump.py 的 certutil/runas 用参数列表(非 shell=True)并 shlex.quote，无注入。
- 依赖：版本约束合理，无硬编码密钥。

## 优先修复
- S1 (curl 导出转义)
- S6 (日志脱敏)
- S3 (SSRF 禁用开关加固)
