# Telnix 复审报告 (Batch 1 修复验证 + 全项目再扫)

> 复审日期：2026-07-29。对应：security_audit_batch1 / bug_audit_batch1 / performance_audit_batch1 / frontend_ux_audit_batch1 + FIXES_batch1。
> 方法：4 个专项复审 agent（安全/bug/性能/前端UX），重点验证 Batch1 修复无回归，并全项目再扫确凿未处理项。

## A) Batch1 修复验证结论（全部通过，无回归）

- **安全 S1**（export.py `_build_curl` shlex.quote）：所有注入点（method/URL/头/body/base64 体）均覆盖，无遗漏。✅
- **安全 S3**（send.py/replay.py SSRF guard）：移除 env 绕过分支，校验逻辑完整（private/loopback/link_local/reserved/multicast/unspecified 全覆盖），无绕过。✅
- **安全 S6**（deepseek.py `_redact_sensitive`）：键名单 + 正则双保险，递归 depth≤6 防爆炸，调用点正确。✅
- **Bug B2**（websocket_relay `_direction` finally）：仅 `stop_flag.set()`，两端 socket 由主函数 join 后统一 close，无提前 FIN 丢帧、无泄露。✅
- **性能 P1-1**（raw_capture 日志降频 50→5000）：确在捕获线程热路径，降频有效。✅
- **性能 P1-2**（flows.ts MAX_FLOWS=5000）：slice O(1) 裁剪，不影响 maxFlowId 基线与 flowIndex 一致性。✅
- **UX3**（flows.ts select() 失败提示）：ElMessage 导入正确，catch 提示就位。✅（注：原探测条件有误，见 B 部分新 bug）
- **UX5**（RawCaptureView loading）：finally 异常路径复位，v-loading 绑定列表容器不阻断工具栏。✅

## B) 全项目再扫新发现的确凿问题（已修复）

### Bug P1 — flows.ts select() lite 探测条件失效（flows.ts:344）
- **问题**：用 `f.request_headers === undefined` 探测 lite flow，但后端 `_SSE_LITE_FIELDS`（db.py:871-874）和前端 `CACHE_LITE_FIELDS`（flows.ts:31-36）**都含 `request_headers`/`response_headers`（值为 null）却省略 `request_body`/`response_body`**。故 SSE/缓存 flow 的 `request_headers` 为 `null` 而非 `undefined`，`=== undefined` 恒 false → 重新拉取完整数据分支（含 UX3 提示）成死代码。选中 SSE/缓存 flow 时 Inspector 的 body 永不补齐、显示空白。
- **修复**：探测改为 `f.request_body === undefined`，与 lite 字段集实际省略字段对齐。已修复（见 FIXES_batch2）。

### UX P2 — FlowList 初始加载无 loading 态（components/FlowList.vue:1680）
- **问题**：抓包页首屏 `loadAllFlows` 异步加载期间列表为空，仅显示空态文案，用户无法区分"加载中"与"确实无流量"（UX5 同类缺陷在 FlowList 未覆盖）。
- **修复**：flows store 新增 `initialLoading` ref，`loadFlows`/`loadAllFlows` 包裹置位+`finally` 复位；FlowList `.fl-body` 容器绑定 `v-loading="store.initialLoading && store.flows.length === 0"`。已修复（见 FIXES_batch2）。

## C) 复审确认无确凿 P0/P1 安全/性能遗留
- 安全：路径遍历（SPA realpath 二次校验）、命令注入（subprocess 均参数列表无 shell=True）、凭据日志、认证 Token(hmac)、SQL 注入（白名单+参数化）、依赖，均无确凿漏洞。仅存 `CERT_NONE` 为抓包代理设计固有（沿用评估）。
- 性能：DB 写路径（队列+批量+内存 max_id 缓存+SSE 独立线程）、连接池分片锁、前端 shallowRef+攒批 SSE+requestIdleCallback 缓存，均已优化到位；无确凿新 P0/P1 热路径问题。

## D) 遗留大改动项（性能冲突/重构大，待用户决策，不阻塞发布）
- **虚拟滚动 P1-3**（FlowList/AnalyzeView/RawCaptureView/WebSocketView 数千条无虚拟滚动）：需引入 vue-virtual-scroller/el-table-v2 重构，改动大。当前 shallowRef+v-memo+MAX_FLOWS 已缓解。
- **search_flows 拉 10000 行 P2-4**（db.py:1136）：大 body 场景内存/CPU 高，需下推 SQLite LIKE/FTS5 或分批游标。
- **raw_capture base64 整段 P2-5**（raw_capture.py 多行）：高频小包重复分配，需按需编码（前后端协同改动）。
- 其他 P2/P3 低优先级项（B3 brotli 已评估不修、S2/S4/S5/S7/S8 设计权衡、P2-6/P3-7/8/9 低影响）持续观察。

## 结论
Batch1 四项修复正确无回归；复审新发现 2 项确凿缺陷（Bug P1、UX P2）均已修复并提交。建议将 D 的 3 个遗留大改动项交用户决策是否实施。
