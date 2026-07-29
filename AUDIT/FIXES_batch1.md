# Telnix 修复记录 (Batch 1)

> 对应审计报告：security_audit_batch1 / bug_audit_batch1 / performance_audit_batch1 / frontend_ux_audit_batch1
> 分支：Hy3v2。修复原则：确凿 + P1/P2 优先；与性能冲突的低优先级项留作遗留（见末尾）。

## 已修复

### S1 [P1] curl 导出命令注入 — api/export.py `_build_curl`
- **问题**：请求头/URL/body 直接以 `"-H 'k: v'"` 拼入 shell 字符串，抓包数据可被构造为含引号/反引号/分号的恶意头或 URL，用户将导出脚本粘贴到终端执行时触发命令注入。
- **修复**：import `shlex`，对所有外部值（method/URL/头值/请求体/base64 体）统一 `shlex.quote` 严格转义。
- **性能**：无影响（仅构造导出字符串时一次 quote）。

### S3 [P2] SSRF 守卫可被 env 绕过 — api/send.py / api/replay.py `_resolve_safe_target`
- **问题**：`TELNIX_DISABLE_SSRF_GUARD=1` 时直接返回解析 IP 且无私网校验，全局 env 被设置即可对内网/元数据(169.254.169.254)发起 SSRF。
- **修复**：移除"跳过校验直接连接"分支。该 env 现仅 `logger.warning` 记录告警、不再绕过守卫；本地调试内网请走受控转发。
- **性能**：无影响（仅去除一个 early return）。

### S6 [P1] 日志泄露敏感信息 — ai/deepseek.py
- **问题**：AI 工具调用的 `args` 经 `logger.info` 记录，可能含 Authorization/Cookie/Token 等凭据片段，经 `/api/logs` 与 `/api/logs/export` 可读。
- **修复**：新增 `_redact_sensitive()` 递归脱敏 helper，对敏感键（authorization/cookie/token/api_key/secret/password 等）及字符串中 `Authorization:/Token:` 片段替换为 `***`，用于工具调用参数日志。
- **性能**：仅日志路径一次递归（深度≤6），开销可忽略。

### B2 [P2] WebSocket 双向关闭竞态丢帧 — proxy/websocket_relay.py `_direction`
- **问题**：单方向（c2s）读到 close 帧后 `finally` 中 `dst.shutdown(SHUT_WR)` 会向对端(server) 发 FIN，导致 server 提前断连、server→client 尚未转发的帧被丢弃（连接假死/白屏）。
- **修复**：`finally` 仅 `stop_flag.set()` 通知对端方向线程结束读循环，**不再**提前 shutdown 对端 socket；两端 socket 由主函数 `join` 后统一 `close()`（隐含 shutdown）。
- **性能**：无影响。

### P1-1 [P1] 捕获热路径统计日志阻塞 — proxy/raw_capture.py
- **问题**：每捕获第 50 个 HTTP/HTTPS 包在捕获线程同步 `logger.info` + 构造大 f-string，高流量下阻塞 `recv` 造成丢包。
- **修复**：打印阈值从 50 提高到 5000，每 5000 包一次，影响可忽略。
- **性能**：显著降低捕获线程阻塞概率。

### P1-2 [P1] 前端 flows 数组无界增长 — ui/src/stores/flows.ts
- **问题**：SSE/poll 持续 `unshift` 无硬上限，长跑会话下 `displayFlows` 计算/更新 maxFlowId/saveToCache 序列化成本线性上升。
- **修复**：新增 `MAX_FLOWS=5000` 与 `enforceMaxFlows()`，在 `pollNewFlows`/`flushSSEBatch`/`selectFlow` 插入后裁剪尾部（最旧）流量并同步 `flowIndex`/`total`。列表降序，裁剪尾部不影响 `maxFlowId` 增量基线。
- **性能**：限制内存与重渲染规模。

### UX3 [P2] 选中详情失败静默 — ui/src/stores/flows.ts `select()`
- **问题**：选中某行异步拉取完整 flow 失败时 `catch` 静默，Inspector 空白无反馈。
- **修复**：`catch` 中 `ElMessage.error('加载流量详情失败')` 轻提示。
- **性能**：无影响。

### UX5 [P2] 原始抓包页初始加载无 loading — ui/src/views/RawCaptureView.vue
- **问题**：`loadFlows` 初始加载无 loading 态，空白列表区让用户无法区分"加载中"与"确实无包"。
- **修复**：新增 `loading` ref，`loadFlows` 包裹 `loading=true/finally=false`，列表容器 `.raw-list-pane` 绑定 `v-loading="loading"`。
- **性能**：无影响。

## 评估后不修改（记录结论）

### B3 [P1] brotli 解压失败 Content-Encoding 未剥离 — proxy/server.py `_decompress_body`
- **评估**：`_decompress_body` 在 bump 模式（代理终止 TLS）才运行，且请求侧 `accept-encoding` 已 strip `br`（server.py:2562-2566），服务器几乎不会返回 `br` 响应，此分支实际不可达。即便命中，保留 `Content-Encoding: br` + 压缩字节转发给浏览器时，支持 br 的现代浏览器会自行解压，不会白屏。原 agent 的"白屏"假设基于"浏览器不支持 br"，不符合现状。故保持现状，不剥离（剥离反而可能乱码）。
- **结论**：不修改，持续观察。

### UX4 [P2] 整页清空无二次确认 — RawCaptureView.vue
- **评估**：该页工具栏无"整页清空全部流量"按钮（仅有批量删除选中，且 `ctxDelete` 已用 `ElMessageBox.confirm` 二次确认）。故 UX4 在该页不成立；抓包页(CaptureView)的 `onClear` 亦已有确认。无需修复。

## 遗留项（性能冲突/大改动，待用户决策）

- **UX1/UX2/P1-3 [P1] 大列表无虚拟滚动**：FlowList/AnalyzeView/RawCaptureView/WebSocketView 数千条时无虚拟滚动。需引入 `el-table-v2` 或 `vue-virtual-scroller` 重构，改动较大。建议作为独立任务评估是否实施（当前 `shallowRef`+`v-memo`+`MAX_FLOWS` 已缓解）。
- **P2-4 [P2] search_flows 拉 10000 行入内存逐行正则**：大 body 场景内存/CPU 高。需下推 `body_regex` 到 SQLite `LIKE`/FTS5（改动较大）。
- **P2-5 [P2] raw_capture 每包 base64 整段**：高频小包重复分配。优化需前端同步改 `base64:` 前缀解析，跨前后端改动。
- **P2-6 / P3-7 / P3-8 / P3-9**：低优先级，影响小，暂缓。
- **S2/S4/S5/S7/S8**：设计权衡或可疑项（TLS verify 关闭为代理工具常态、透明 SNI 转发属正常代理行为、局域网监听已有 Token 鉴权、DeepSeek verify 需 env、沙箱已有最小环境白名单），暂不改。

## 验证建议
重启后端与前端，验证：导出 curl 不再可被注入；访问内网/元数据被 SSRF 守卫拦截；AI 工具调用日志无明文凭据；长时间抓包前端列表不再无界增长；原始抓包页首次加载有 loading；WebSocket 连接关闭后不再丢帧。
