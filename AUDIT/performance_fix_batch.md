# Telnix 性能优化修复记录

> 分支 Hy3v2。对应复审遗留项 P2-4（search 10000 行）与 P1-3（虚拟滚动）。
> 用户决策：实施"虚拟滚动 (P1)"与"search 10000行 (P2)"两项。

## 已修复

### P2-4 [P2] search_flows 全量拉取内存峰值 — src/host/telnix/db.py `search_flows`
- **问题**：原实现 `SELECT ... LIMIT ?` 后 `conn.execute(sql, args).fetchall()` 一次性把最多 10000 行（含 `request_body`/`response_body` 大字段）全部载入内存，再 Python 逐行正则/hex 匹配。大 body 场景内存峰值可达数百 MB、CPU 耗时百 ms~秒级，且即便命中 `limit`(默认 200) 条也拉满了 10000 行。
- **修复**：改为 `conn.execute(sql, args).fetchmany(500)` 分批游标。内存峰值降至 500 行；命中达到 `limit` 即停止后续扫描，避免无谓拉取。SQL 仍带 `LIMIT 10000` 限制总扫描量，结果正确性不受影响（结果上限仍为 `limit`）。
- **性能**：内存峰值从数千行→500 行；CPU 仅处理命中所需的批次。

### P1-3 [P1] 大列表无虚拟滚动 — 新增 composable + FlowList/RawCaptureView/WebSocketView
- **问题**：FlowList/AnalyzeView/RawCaptureView/WebSocketView 对 `displayFlows` 全量 `v-for` 渲染，配合 `MAX_FLOWS=5000` 时 DOM 节点常驻 5000×N，首次渲染/滚动帧率下降、内存占用高。
- **修复**：
  1. 新增 `src/ui/src/composables/useVirtualList.ts`：轻量固定行高虚拟滚动（无第三方依赖），通过顶部/底部占位 div 维持滚动高度，仅渲染可视区 + overscan 行。
  2. FlowList（`.fl-body`，行高 26px）：模板改为 `v-for="visibleItems"` + 占位 div，`onBodyScroll` 同步 `onVScroll`，`@scroll` 同时驱动虚拟滚动。
  3. RawCaptureView（`.rl-body`，行高 28px）、WebSocketView（`.wl-body`，行高 28px）：同样改造。
- **性能**：DOM 节点从数千降至约（可视行数+overscan），滚动/重渲染帧率显著提升，内存随列表规模不再线性增长。

### 未覆盖说明（AnalyzeView 分组结构）
- AnalyzeView 按 host 分组（组头 + 组内 flows）的聚合列表，简单窗口化需 flatten 分组为有序序列，复杂度与风险高于平铺列表，本次未实施。建议后续采用 `el-table-v2`（支持分组/树形虚拟滚动）或分组虚拟滚动库单独处理。当前 AnalyzeView 的 `pageSize=100000` 与分组展开仍可能存在大 DOM，列为后续优化项。

## 验证建议
- 搜索含大 body 的会话：内存占用与响应时间应明显下降（不再一次性拉 10000 行）。
- 抓包页/Raw 抓包页/WebSocket 页在数千条流量下滚动应流畅，DOM 节点数维持低位（DevTools Elements 面板可见仅可视区行）。

## 注
- raw_capture base64 整段（P2-5）未实施（需前后端协同按需编码，改动较大且无功能影响），保持观察。
