# Telnix 功能审计报告（Functional Audit Report）

> 审计范围：整个项目（前端 `src/ui/src` + 后端 `src/host/telnix`），**透明代理（transparent proxy）相关 bug 按需求忽略**。
> 审计方式：多 agent 并行功能审计 + 修复 + 再审计（Round 1 / Round 2 / Round 3 验证），直到无确定性问题。
> 审计日期：2026-07-28

---

## 一、审计结论

- **高严重级功能性崩溃 bug：0 项残留**（原 `FlowList.vue` 缓存损坏白屏已修复并验证）。
- **中/低严重级功能 bug：已修复 14 项**，涉及后端（db/导出导入/代理引擎/DNS）、前端（stores/views 组件）。
- **透明代理相关 bug：按需求跳过未处理**（报告中不计入）。
- 经 Round 3 并行验证复审，所有修复均正确生效、无回归、无引入新 bug。

---

## 二、已修复问题清单

### 后端（Python）

| # | 文件:行 | 严重级 | 问题 | 修复 |
|---|---------|--------|------|------|
| B1 | `db.py:1523` | 中 | `get_flows_overview` 状态分桶中 `status_code=0` 被误计为 "1xx" | 增加 `if code == 0: status_buckets["other"] += ...` |
| B2 | `db.py:1053/1296/1100` | 高 | `if status_code:` 把 `0` 当 falsy，按状态码 0 过滤被静默忽略 | 三处改为 `if status_code is not None:` |
| B3 | `db.py:1414` | 中 | `get_flows_stats` 状态码分桶用 `status_code IS NOT NULL`，与 overview 修复不一致（0 未归 other） | 改为 `...AND status_code != 0` |
| B4 | `export.py:69` | 中 | HAR 导出 `bodySize` 硬编码 `-1` | 改为 `f.get("size") or -1` |
| B5 | `import_flows.py:79` | 中 | HAR 导入 `status_code` 缺失时 `or 0` | 改为缺失时返回 `None` |
| B6 | `import_flows.py:66` | 中 | HAR 导入 `size` 优先用 content.size（可能为 -1）而非真实 body 长度 | 当 `resp_body` 非空且 size≤0 时用 `len(resp_body)` 兜底 |
| B7 | `import_flows.py:106` | 中 | `_telnix_flow_to_flow` 的 `status_code or 0` 与 HAR 分支不一致 | 改 `flow.get("status_code")` 保留 None |
| B8 | `raw_capture.py:78-96` | 中 | `_extract_path` 用 `url.split(host)` 在 host 重复出现于 path 时误截断 | 新增 `_extract_path`，按 `scheme://host` 前缀精确裁剪 |
| B9 | `auto_reply/rules.py:132-178` | 高 | `_compile` 仅统计扁平量词数量，`(a+)+` 类嵌套量词 ReDoS 漏检 | 新增 `_has_nested_quantifier` 栈检测嵌套结构 |
| B10 | `auto_reply/rules.py:198` | 中 | `exact` 模式用 `re.IGNORECASE`，与"完全相等"语义矛盾 | 移除 IGNORECASE |
| B11 | `api/clash.py:116-123` | 中 | `clash_status` 静默丢弃 ver/cfg 错误，401 secret 错误显示 reachable=True 无提示 | 透出 `info["error"]` |
| B12 | `proxy/mitmproxy_engine.py` | 高 | 请求 hook 仅处理 mock/modify_request/modify_response，缺失 script/mock_request 动作 | 补齐分支 + 新增 `_handle_mock_request`/`_handle_script_request`/`_handle_script_response`/`_set_mock_response` |
| B13 | `proxy/mitmproxy_engine.py:247/293` | 中 | 规则命中计数与自研引擎不一致，modify_request/mock_request/script(请求) 漏计、响应阶段重复计 | 请求阶段标记 `telnix_rule_counted`，响应阶段仅在未计过时补计 |
| B14 | `proxy/dns_hijack_local.py:455-466` | 高 | DNS 响应缓存原样复用上游包，transaction id 属首次客户端，后续客户端 TXID 不匹配被丢弃（间歇性解析失败） | 新增 `_rewrite_response_id`，回送/缓存均用当前查询 id 重写响应头 |

### 前端（Vue/TS）

| # | 文件:行 | 严重级 | 问题 | 修复 |
|---|---------|--------|------|------|
| F1 | `components/FlowList.vue:418` | 高 | `loadColOrder` 的 `JSON.parse` 无 try-catch，缓存损坏即整页白屏 | 包裹 try-catch，损坏回退默认列序 |
| F2 | `stores/flows.ts:344` | 中 | `selectFlow` 注入历史 flow 不更新 `maxFlowId`，跨页跳转可能漏拉中间包 | 注入时 `if (flow.id > maxFlowId) maxFlowId = flow.id` |
| F3 | `stores/flows.ts:114` | 低 | `autoScrollDelay` 用 `Number(...) || 10`，设为 0（立即恢复）退化为 10 | 用空值判断替代 `||` |
| F4 | `views/SettingsView.vue:191` | 中 | `formatSize` 负数/NaN/undefined 边界显示 `NaN B`/`-1 B` | 增加 `!Number.isFinite || <0` 守卫 |
| F5 | `utils/aiFlowSnapshot.ts:85` | 低 | `getFlowSnapshot` 直接返回 `result.flow`，数据污染时无校验 | 校验 `result.flow` 结构与 `flow.id` 类型 |
| F6 | `views/WebSocketView.vue` | 中 | 本地 `maxFlowId`/`flows` 未随后端清空事件重置，增量轮询可能复活已删旧包 | 监听 `telnix:flows-cache-cleared` 重置；`onUnmounted` 移除 |
| F7 | `views/RawCaptureView.vue` | 中 | 同上（TCP/UDP 页本地状态与清空不同步） | 同上 |
| F8 | `views/WebSocketView.vue:725` / `RawCaptureView.vue:993` | 中 | 空状态误判 `!flows.length`（筛选后 displayFlows 空但 flows 非空时报"暂无"） | 改判 `!displayFlows.length` |

---

## 三、已知但未修复项（设计取舍 / 低风险 / 透明代理）

- **透明代理（transparent proxy）bug**：按需求明确忽略，不在本审计处理范围内。
- **`db.py` 入站/出站字节按 `size/2` 估算**（`get_flows_overview`）：统计口径为估算值，对纯请求类流量失真，属低危且非崩溃，保留现状（如需要可改为基于真实 request_body/response_body 长度聚合）。
- **`db.py:1110` `search_flows` 先 `LIMIT 10000` 再 Python 过滤**：大表下可能漏匹配 1 万行之后且未下推正则，性能/完整性隐患，低风险，未改（避免引入 SQLite REGEXP 依赖）。
- **DNS 劫持本机 IP 集合过窄**（`dns_hijack.py`）：仅含 `127.0.0.1` 不含真实网卡 IP，特定环境可能漏劫持，中危但因涉及网络环境差异且高风险改动，保留为已知项。
- **Python 脚本沙箱逃逸**（`script_runner.py`）：`__subclasses__` 类逃逸属已知限制，依赖子进程 + 资源限制作纵深防御；非本次范围。

---

## 四、审计轮次与流程

1. **Round 1（功能审计）**：4+ 并行 agent 覆盖代理引擎、自动回复、db/api、前端 stores/views/components，定位并修复 B1-B12、F1-F5。
2. **Round 2（全项目复审）**：3 并行 agent 覆盖 proxy+auto_reply、db+api、前端全体 + 1 个脚本/工具/CLI agent，发现并修复 B13/B14、F6-F8，确认此前修复无回归。
3. **Round 3（验证复审）**：2 并行 agent 仅验证 Round 2 修复点 + 事件名一致性，确认全部通过、无新 bug。

---

## 五、构建验证

- 后端：所有修改文件 `py_compile` 通过。
- 前端：`npm run build` 成功，无新增 lint 错误。
