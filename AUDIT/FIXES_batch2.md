# Telnix 修复记录 (Batch 2)

> 复审（REVIEW_batch1）新发现的确凿缺陷修复。分支 Hy3v2。

## 已修复

### Bug P1 [P1] flows.ts select() lite 探测条件失效 — ui/src/stores/flows.ts `select()`
- **问题**：用 `f.request_headers === undefined` 判断 lite flow 需重新拉取完整数据；但后端 `_SSE_LITE_FIELDS`（db.py:871-874）和前端 `CACHE_LITE_FIELDS`（flows.ts:31-36）都含 `request_headers`/`response_headers`（值为 `null`）却省略 `request_body`/`response_body`/`raw_data`。故 SSE/缓存 flow 的 `request_headers` 为 `null` 而非 `undefined`，`=== undefined` 恒为 false → 重新拉取分支（含 UX3 的失败提示）成为死代码。选中 SSE/缓存 flow 时 Inspector 的 body 永不补齐、显示空白且无错误提示。
- **修复**：探测条件改为 `f.request_body === undefined`，与 lite 字段集实际省略字段对齐。选中 SSE/缓存 flow 时正确异步拉取完整数据补齐 body，拉取失败仍触发 `ElMessage.error`。
- **性能**：无影响（仅变更判断条件）。

### UX P2 [P2] FlowList 初始加载无 loading 态 — ui/src/stores/flows.ts + ui/src/components/FlowList.vue
- **问题**：抓包页首屏 `loadAllFlows` 异步加载期间列表为空，仅显示空态文案，用户无法区分"加载中"与"确实无流量"（与 UX5 同类缺陷，FlowList 未覆盖）。
- **修复**：flows store 新增 `initialLoading` ref，`loadFlows`/`loadAllFlows` 包裹 `initialLoading=true` + `finally` 复位；FlowList `.fl-body` 容器绑定 `v-loading="store.initialLoading && store.flows.length === 0"`。
- **性能**：无影响。

## 复审验证（无回归）
Batch1 四项修复（S1/S3/S6/B2/P1-1/P1-2/UX3/UX5）经 4 个专项复审 agent 逐项验证，确认正确、无回归、无新引入漏洞/bug。详见 REVIEW_batch1.md。

## 遗留大改动项（待用户决策，不阻塞发布）
- 虚拟滚动 P1-3（FlowList/AnalyzeView/RawCaptureView/WebSocketView）：需引入 vue-virtual-scroller/el-table-v2 重构。
- search_flows 拉 10000 行 P2-4（db.py:1136）：需下推 SQLite LIKE/FTS5 或分批游标。
- raw_capture base64 整段 P2-5：需按需编码（前后端协同）。

## 验证建议
重启前端，验证：选中经 SSE 实时推送/缓存恢复的流量时 Inspector 能正确显示请求/响应 body；抓包页首屏加载时列表区显示 loading 遮罩，加载完成自动消失。
