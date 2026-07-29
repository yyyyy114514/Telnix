# Telnix 性能审计报告 (Batch 1)

> 范围：src/host/telnix/ 后端 + src/ 前端；忽略 i18n。性能优先。

## 汇总表

| # | 级别 | 位置 | 问题 | 量级 |
|---|------|------|------|------|
| P1-1 | P1 | raw_capture.py:356-364 | 捕获热路径每50包同步 logger.info + f-string | 中 |
| P1-2 | P1 | stores/flows.ts:297,349 | 前端 flows 数组无硬上限，SSE 持续 unshift 增长 | 高 |
| P1-3 | P1 | FlowList.vue/RawCaptureView.vue/WebSocketView.vue v-for | 大列表无虚拟滚动 | 高 |
| P2-4 | P2 | db.py:1134 | search_flows 拉 10000 行入内存逐行 Python 正则 | 中 |
| P2-5 | P2 | raw_capture.py:480,547,565,597 | 每包 base64 编码整段 payload | 中 |
| P2-6 | P2 | db.py:644-647 | SSE 通知每批 flush 误触发 _flush_done | 低 |
| P3-7 | P3 | server.py:1526-1572 | _ssl_ctx_lock 持锁做 stat+load | 低 |
| P3-8 | P3 | db.py:1178-1185 | search_flows 无 body 条件全量返回 | 低 |
| P3-9 | P3 | sessions.py:108-141 | 每订阅者投递完整 lites 拷贝 | 低 |

## 确凿发现

### P1-1 raw_capture.py:356-364
每捕获第50个 HTTP/HTTPS 包在**捕获线程**内同步 logger.info 并构造大 f-string。日志 I/O 阻塞 recv，高流量下丢包。优化：移除统计日志或用模块级计数器+周期异步刷；`logger.isEnabledFor` 守卫。

### P1-2 stores/flows.ts:297,349
flows 为 shallowRef<Flow[]>，SSE pollNewFlows 每批 unshift + selectFlow unshift 持续追加，仅 clear() 清零，无最大长度硬上限。长跑会话数组与 flowIndex Map 无界增长，displayFlows 计算(O(n))、updateMaxFlowId(O(n))、saveToCache 序列化成本线性上升。优化：设上限(如 MAX=5000)，超出尾部 pop + flowIndex.delete。只裁尾部旧数据，不影响 since_id 基线。

### P1-3 前端大列表无虚拟滚动
FlowList.vue:1682、RawCaptureView.vue:976、WebSocketView.vue:707 普通 el-table/v-for 渲染全量行，数千条时 DOM=流量数，滚动/筛选卡顿。优化：el-table-v2 或 vue-virtual-scroller。正确性风险低。

### P2-4 db.py:1134
search_flows 固定 args.append(10000) 把最多 10000 行(含 request_body/response_body)全拉入内存再 Python 逐行正则/hex。大 body 场景内存/CPU 显著。优化：下推 body_regex 到 SQLite LIKE/FTS5；或分块游标。

### P2-5 raw_capture.py base64
每包整段 payload 做 base64.b64encode(payload).decode('ascii')，膨胀 4/3 且 decode 分配新字符串，高频小包重复分配明显。优化：仅确有需要时编码(如非文本/选中查看)。需同步改前端解析(base64: 前缀)。

### P3-7 server.py:1536-1572
_ssl_ctx_lock 在 miss 路径持锁内 os.stat + ssl.create_default_context()。仅新 host 首次握手命中，高并发新 host 串行化 TLS 握手。可改为持锁仅查缓存，构建放锁外。

## 需确认(非确凿)
- SSE+轮询双重开销：前端已用 SSE，15s 心跳+兜底轮询设计合理，非 bug。
- _flow_queue 满丢包(db.py:776-787)：队列上限 50000 满时丢最旧，极端流量可能丢 flow(已计数)。是否接受需产品确认。
- transparent_proxy _nat_lock 每包短临界区，10G+ pps 可能瓶颈，列为观察项。
