# 全面项目审计报告

> 生成时间: 2026-08-04
> 审计范围: Bug(前后端)、安全、性能(前后端)、UX(前端)、功能设计
> 审计方法: 并行多 agent 逐文件逐行审查
> 总消耗: 1,627,400 tokens, 123 次工具调用

---

## 统计概览

| 类别 | 数量 | 高危 |
|------|------|------|
| 后端 Bug | 17 | 1 |
| 前端 Bug | 6 | 2 |
| 安全漏洞 | 12 | 1 |
| 性能问题 | 15 | 3 |
| UX 问题 | 4 | 0 |
| 功能设计 | 20 | - |
| **总计** | **74** | **7** |

---

## 一、后端 Bug (17个)

### 🔴 高危 (1个)

#### 1. script_worker.py - `__import__` 绕过沙箱
- **文件**: `src/host/telnix/auto_reply/script_worker.py`
- **行号**: 247
- **严重程度**: 高
- **类型**: 安全漏洞
- **描述**: `_real_import = _builtins.__import__` 在脚本首次加载前执行。如果用户在脚本中使用 `__import__`，会使用真实 import 而非受限版本。
- **复现**: 用户脚本使用 `__import__('socket')` 可以绕过受限导入。
- **修复建议**: 在受限环境中拦截 `__import__` 调用，或将其从 `_builtins` 中删除。

---

### 🟡 中危 (8个)

#### 2. auto_reply.py - 未捕获的异常变量 `e`
- **文件**: `src/host/telnix/api/auto_reply.py`
- **行号**: 453, 477, 496, 513, 821 等
- **严重程度**: 中
- **类型**: 异常处理
- **描述**: 多处 `except Exception` 块中引用了未定义的变量 `e`，应该使用 `except Exception as e:`。
- **修复建议**: 将所有 `except Exception:` 改为 `except Exception as e:`。

#### 3. export.py - 未捕获的异常变量 `e`
- **文件**: `src/host/telnix/api/export.py`
- **行号**: 142, 209, 476
- **严重程度**: 中
- **类型**: 异常处理

#### 4. settings.py - 未捕获的异常变量 `e`
- **文件**: `src/host/telnix/api/settings.py`
- **行号**: 30, 171, 195, 242, 251, 358
- **严重程度**: 中
- **类型**: 异常处理

#### 5. auth.py - 未捕获的异常变量 `e`
- **文件**: `src/host/telnix/api/auth.py`
- **行号**: 44
- **严重程度**: 中
- **类型**: 异常处理

#### 6. export.py - pcap ACK 序列号硬编码
- **文件**: `src/host/telnix/api/export.py`
- **行号**: 556
- **严重程度**: 中
- **类型**: 逻辑错误
- **描述**: TCP 响应包的 ACK 序列号硬编码为 1，正确值应该是 `base_seq`（请求序列号）。导致 Wireshark 无法正确关联请求响应。
- **修复建议**: 使用请求的序列号作为 ACK 值。

#### 7. export.py - CSV 导出格式错误
- **文件**: `src/host/telnix/api/export.py`
- **行号**: 291
- **严重程度**: 中
- **类型**: 逻辑错误
- **描述**: CSV 导出使用字符串拼接而非 `csv.writer`，当字段包含逗号、引号或换行符时格式不正确。
- **修复建议**: 使用 `csv.writer` 模块正确处理特殊字符。

#### 8. replay.py - 重复重放时 durations 为空导致 ValueError
- **文件**: `src/host/telnix/api/replay.py`
- **行号**: 157
- **严重程度**: 中
- **类型**: 边界条件
- **描述**: `_repeat_replay()` 中如果所有请求都失败（`durations` 为空），`stats` 计算会抛出 `ValueError`。
- **修复建议**: 在计算前检查 `durations` 是否为空。

---

### 🟢 低危 (8个)

#### 9. ssl_bump.py - f-string 语法错误
- **文件**: `src/host/telnix/proxy/ssl_bump.py`
- **行号**: 444
- **严重程度**: 低
- **类型**: 逻辑错误
- **描述**: 普通字符串中写了 `{self.root_cert_path}` 不会进行变量替换。

#### 10. sessions.py - 缓存更新竞态条件
- **文件**: `src/host/telnix/api/sessions.py`
- **行号**: 249
- **严重程度**: 低
- **类型**: 竞态
- **描述**: overview 缓存更新使用 `clear()` 后立即赋值，存在 read-then-write 竞态。

#### 11. rules.py - 快速路径逻辑缺陷
- **文件**: `src/host/telnix/auto_reply/rules.py`
- **行号**: 343
- **严重程度**: 低
- **类型**: 逻辑错误
- **描述**: 当 `_has_rules=False` 但缓存已加载时，跳过缓存再次查询 DB。

#### 12. clash/client.py - 双重检查锁定缺陷
- **文件**: `src/host/telnix/clash/client.py`
- **行号**: 127
- **严重程度**: 低
- **类型**: 竞态
- **描述**: 两次检查之间其他线程可能已更新 `_upstream_probe_thread`。

#### 13. script_worker.py - PermissionError 语义问题
- **文件**: `src/host/telnix/auto_reply/script_worker.py`
- **行号**: 250
- **严重程度**: 低
- **类型**: 逻辑错误

#### 14. server.py - SSL context 缓存竞态
- **文件**: `src/host/telnix/proxy/server.py`
- **行号**: 1658
- **严重程度**: 低
- **类型**: 竞态
- **描述**: mtime 缓存更新不在锁内，可能导致缓存不一致。

#### 15. server.py - HTTP/2 连接池资源泄漏
- **文件**: `src/host/telnix/proxy/server.py`
- **行号**: 689
- **严重程度**: 低
- **类型**: 资源泄漏
- **描述**: HTTP/2 连接池在对象销毁时没有显式关闭。

#### 16. auto_reply.py - 规则签名 None vs [] 不一致
- **文件**: `src/host/telnix/auto_reply/auto_reply.py`
- **行号**: 341
- **严重程度**: 低
- **类型**: 边界条件
- **描述**: `_rule_signature()` 中 `modify_rules` 为 `None` 时序列化为 `null` 而非空列表。

---

## 二、前端 Bug (6个)

### 🔴 高危 (2个)

#### 1. AIView.vue - 模板语法错误
- **文件**: `src/ui/src/views/AIView.vue`
- **行号**: 762
- **严重程度**: 高
- **类型**: Bug
- **描述**: 模板语法错误：多余右括号 `{{ formatSize(bodySize(f.request_body)) }}` 导致编译失败。

#### 2. AIView.vue - 模板语法错误
- **文件**: `src/ui/src/views/AIView.vue`
- **行号**: 785
- **严重程度**: 高
- **类型**: Bug
- **描述**: 模板语法错误：多余右括号 `{{ formatSize(bodySize(f.response_body)) }}`。

---

### 🟡 中危 (4个)

#### 3. FlowList.vue - v-memo 依赖问题
- **文件**: `src/ui/src/components/FlowList.vue`
- **行号**: 1832
- **严重程度**: 中
- **类型**: Bug
- **描述**: `v-memo` 依赖 `gridCols`（computed），每次 computed 重算都生成新数组引用，导致 v-memo 永远认为依赖变了而重新渲染。

#### 4. flows.ts - SSE lite flow 每次都触发完整加载
- **文件**: `src/ui/src/stores/flows.ts`
- **行号**: 402
- **严重程度**: 中
- **类型**: Bug
- **描述**: `select()` 中检查 `request_headers == null` 判断是否需要补全数据，但 SSE 推送的 lite flow 本就不包含 `request_headers`，导致每次选中都触发 HTTP 请求。

#### 5. CaptureView.vue - 清空操作影响所有会话
- **文件**: `src/ui/src/views/CaptureView.vue`
- **行号**: 325
- **严重程度**: 中
- **类型**: UX/功能
- **描述**: `onClear` 清空所有会话流量，用户可能只想清空当前会话。

#### 6. flows.ts - 缓存超限直接删除无确认
- **文件**: `src/ui/src/stores/flows.ts`
- **行号**: 233
- **严重程度**: 中
- **类型**: UX
- **描述**: `restoreFromCache` 对超大缓存(>5MB)直接删除，用户失去所有历史数据。

---

## 三、安全漏洞 (12个)

### 🔴 高危 (1个)

#### 1. decode.py - 插件路径遍历 + 任意代码执行
- **文件**: `src/host/telnix/api/decode.py`
- **行号**: 482
- **严重程度**: 高
- **类型**: 注入
- **描述**: `apply_decoder_plugin` 接口直接使用用户传入的 `plugin_path` 构造 import 语句，存在路径遍历和任意文件读取风险。
- **攻击场景**: 攻击者传入 `plugin_path='../../../etc/passwd'` 或 `plugin_path='C:\\Windows\\System32\\config\\SAM'`，即可将任意文件作为 Python 模块导入。
- **修复建议**: 添加 `plugin_path` 白名单校验：仅允许预定义的插件目录（如 `~/.telnix/plugins/`）下的 `.py` 文件。

---

### 🟡 中危 (7个)

#### 2. proxy_tools.py - Map Local 路径遍历
- **文件**: `src/host/telnix/api/proxy_tools.py`
- **行号**: 99
- **严重程度**: 中
- **类型**: 路径遍历
- **描述**: Map Local 规则的 `file_path` 未做路径遍历校验。
- **攻击场景**: `file_path='../../../telnix_data/telnix.db'` 可读取数据库文件。
- **修复建议**: 添加路径白名单校验，检查真实路径是否在允许目录下。

#### 3. settings.py - list_dirs 目录枚举
- **文件**: `src/host/telnix/api/settings.py`
- **行号**: 98
- **严重程度**: 中
- **类型**: 路径遍历
- **描述**: `list_dirs` 接口未限制目录访问范围，可能导致目录枚举攻击。
- **攻击场景**: 传入 `path='C:/'` 可枚举系统目录。
- **修复建议**: Windows 上限制只能列出 data_dir/cert_dir/ui_dir。

#### 4. sessions.py - 跨会话流量删除
- **文件**: `src/host/telnix/api/sessions.py`
- **行号**: 469
- **严重程度**: 中
- **类型**: 输入验证
- **描述**: `batch_delete_flows` 未校验 `ids` 是否属于当前会话。
- **修复建议**: 删除前校验所有 flow_id 确实属于调用方当前 session。

#### 5. export.py - HAR 导出敏感头未脱敏
- **文件**: `src/host/telnix/api/export.py`
- **行号**: 136
- **严重程度**: 中
- **类型**: 敏感信息
- **描述**: 导出 HAR 时 `Authorization/Cookie/Token` 等敏感头原样导出。
- **攻击场景**: 用户导出 HAR 后分享给其他人，凭证泄露。
- **修复建议**: 自动剔除敏感头字段。

#### 6. import_flows.py - 导入文件无大小限制
- **文件**: `src/host/telnix/api/import_flows.py`
- **行号**: 147
- **严重程度**: 中
- **类型**: 输入验证
- **描述**: 导入 HAR/JSON 时未做大小限制，恶意超大文件可能导致 OOM。
- **修复建议**: 设置每个 body 字段上限（如 100MB）。

#### 7. proxy_tools.py - Map Remote SSRF 风险
- **文件**: `src/host/telnix/api/proxy_tools.py`
- **行号**: 158
- **严重程度**: 中
- **类型**: SSRF
- **描述**: `map_remote_add` 的 `target_url` 未校验是否指向内网地址。
- **攻击场景**: `target_url='http://192.168.1.1/admin'` 可探测内网服务。
- **修复建议**: 添加 SSRF 校验，参照 `send.py` 的 `_resolve_safe_target` 实现。

#### 8. server.py - curl/Python 导出脚本安全
- **文件**: `src/host/telnix/proxy/server.py`
- **行号**: 1429
- **严重程度**: 中
- **类型**: 注入
- **描述**: 导出的 curl/python 脚本中 body 使用 `repr()` 或 JSON 嵌入，可能导致命令注入。

---

### 🟢 低危 (4个)

#### 9. auto_reply.py - test_script 资源耗尽
- **文件**: `src/host/telnix/api/auto_reply.py`
- **行号**: 687
- **严重程度**: 低
- **类型**: 拒绝服务
- **描述**: `test_script` 接口允许任意代码执行，可能导致资源耗尽。

#### 10. search.py - 正则 DoS (ReDoS)
- **文件**: `src/host/telnix/api/search.py`
- **行号**: 78
- **严重程度**: 低
- **类型**: 拒绝服务
- **描述**: `body_regex` 参数超长正则可能导致正则回溯攻击。
- **修复建议**: 限制正则长度，使用 timeout 包装 `re.search`。

#### 11. clash.py - clash_secret 明文存储
- **文件**: `src/host/telnix/api/clash.py`
- **行号**: 97
- **严重程度**: 低
- **类型**: 敏感信息
- **描述**: `clash_secret` 明文存储在 settings.json。
- **修复建议**: 使用 Fernet 对称加密存储。

#### 12. ai.py - deepseek_api_key 明文存储
- **文件**: `src/host/telnix/api/ai.py`
- **行号**: 74
- **严重程度**: 低
- **类型**: 敏感信息
- **描述**: `deepseek_api_key` 明文存储在 settings.json。

---

## 四、性能问题 (15个)

### 🔴 高危 (3个)

#### 1. db.py - get_all_tags_summary 无分页
- **文件**: `src/host/telnix/db.py`
- **行号**: 1608
- **严重程度**: 高
- **类型**: 性能
- **描述**: 加载所有流量的 tags 字段到内存聚合，无 LIMIT。10万+流量时占用数百MB内存。
- **修复建议**: 添加 LIMIT 1000 或在 SQL 层聚合。

#### 2. db.py - get_flows_stats 全量 Python 聚合
- **文件**: `src/host/telnix/db.py`
- **行号**: 1836
- **严重程度**: 高
- **类型**: 性能
- **描述**: `content_type` 分组遍历全部行并逐行 JSON.parse。
- **修复建议**: 增加 `request_content_type/response_content_type` 列使 SQL GROUP BY 可用。

#### 3. db.py - get_flows_heatmap 无 LIMIT
- **文件**: `src/host/telnix/db.py`
- **行号**: 1988
- **严重程度**: 高
- **类型**: 性能
- **描述**: 加载全部 flows 到 Python 再分桶聚合。
- **修复建议**: 添加时间范围过滤 + LIMIT。

---

### 🟡 中危 (8个)

#### 4. db.py - search_flows 重复编译正则
- **文件**: `src/host/telnix/db.py`
- **行号**: 1454
- **严重程度**: 中
- **类型**: 性能
- **描述**: 每次调用都 `re.compile()`。
- **修复建议**: 添加模块级 LRU 缓存。

#### 5. db.py - _overview_cache 永不过期
- **文件**: `src/host/telnix/db.py`
- **行号**: 2347
- **严重程度**: 中
- **类型**: 性能
- **描述**: `_overview_cache` 一次性缓存但从未失效/清理。
- **修复建议**: 在 `delete_all_flows/clear_sessions` 时调用失效。

#### 6. server.py - SSL context 缓存过大
- **文件**: `src/host/telnix/proxy/server.py`
- **行号**: 672
- **严重程度**: 中
- **类型**: 性能
- **描述**: `_SSL_CTX_CACHE_MAX=500` 过大，每个约 1MB，高流量时 500MB。
- **修复建议**: 将上限降至 100-200。

#### 7. server.py - SNI 解析重复执行
- **文件**: `src/host/telnix/proxy/server.py`
- **行号**: 1454
- **严重程度**: 中
- **类型**: 性能
- **描述**: SNI fallback 时连续两次调用 `_parse_sni_from_tls`。
- **修复建议**: 解析一次后缓存结果。

#### 8. FlowList.vue - LRU 缓存 O(n) 删除
- **文件**: `src/ui/src/components/FlowList.vue`
- **行号**: 528
- **严重程度**: 中
- **类型**: 性能
- **描述**: 用 `Map.keys().next()` 遍历找第一个 key 删除（O(n)）。
- **修复建议**: 使用 ordered-keys Map 或记录第一个 key 引用直接删除。

#### 9. flows.ts - SSE lite flow 每次重新加载
- **文件**: `src/ui/src/stores/flows.ts`
- **行号**: 402
- **严重程度**: 中
- **类型**: 性能
- **描述**: 选中 SSE 来的 flow 都会触发一次 HTTP 请求。

#### 10. db.py - get_network_topology SQL 无 LIMIT
- **文件**: `src/host/telnix/db.py`
- **行号**: 1884
- **严重程度**: 中
- **类型**: 性能
- **描述**: GROUP BY 后大量中间结果在 Python 端截断。
- **修复建议**: SQL 层加上 LIMIT。

---

### 🟢 低危 (4个)

#### 11. rules.py - _host_match_cache FIFO 非 LRU
- **文件**: `src/host/telnix/auto_reply/rules.py`
- **行号**: 53
- **严重程度**: 低
- **类型**: 性能
- **描述**: 简单 FIFO 淘汰，高频 host 可能被淘汰。

#### 12. FlowList.vue - 重复排序
- **文件**: `src/ui/src/components/FlowList.vue`
- **行号**: 605
- **严重程度**: 低
- **类型**: 性能
- **描述**: SSE 推送的 batch 已排序，再次排序浪费 CPU。

#### 13. FlowList.vue - watcher 频繁触发
- **文件**: `src/ui/src/components/FlowList.vue`
- **行号**: 795
- **严重程度**: 低
- **类型**: 性能
- **描述**: `watch(() => store.flows)` 高频触发 `updateKnownsNow()`。

#### 14. flows.ts - 缓存延迟写入过长
- **文件**: `src/ui/src/stores/flows.ts`
- **行号**: 193
- **严重程度**: 低
- **类型**: 性能
- **描述**: debounce 2000ms + requestIdleCallback 3000ms，最多延迟 5 秒。
- **修复建议**: 将 debounce 降至 1000ms。

---

## 五、UX 问题 (4个)

#### 1. flows.ts - 缓存超限无提示删除
- **文件**: `src/ui/src/stores/flows.ts`
- **行号**: 233
- **严重程度**: 中
- **描述**: 当 localStorage 缓存超过 5MB 时直接删除，用户失去所有历史数据无任何提示。

#### 2. flows.ts - 缓存截断无确认
- **文件**: `src/ui/src/stores/flows.ts`
- **行号**: 234
- **严重程度**: 中
- **描述**: 缓存超限时直接丢弃 30% 数据，用户不知情。

#### 3. CaptureView.vue - 清空范围不明确
- **文件**: `src/ui/src/views/CaptureView.vue`
- **行号**: 325
- **严重程度**: 低
- **描述**: 清空操作清空所有会话，但界面未明确告知用户。

#### 4. auto_reply.py - 脚本错误无历史
- **文件**: `src/host/telnix/api/auto_reply.py`
- **行号**: 522
- **严重程度**: 低
- **描述**: 脚本错误只返回最后一条，无历史记录。

---

## 六、功能设计 (20个)

### 数据模型问题

#### 1. flow_groups 表使用逗号分隔字符串
- **文件**: `src/host/telnix/db.py`
- **行号**: 576
- **类型**: 数据模型
- **描述**: `flow_ids` 存储为逗号分隔字符串而非关系型中间表。
- **建议**: 改用 `flow_group_flows(flow_group_id, flow_id)` 中间表。

### 恢复机制缺失

#### 2. 迁移代码静默吞异常
- **文件**: `src/host/telnix/db.py`
- **行号**: 582
- **类型**: 恢复机制缺失
- **描述**: `ignored_processes` 表迁移静默捕获所有异常，可能导致不一致状态。

#### 3. _flush_update_batch 静默丢弃失败
- **文件**: `src/host/telnix/db.py`
- **行号**: 1214
- **类型**: 恢复机制缺失
- **描述**: UPDATE 批量失败最多只记录前 3 条错误。

#### 4. get_setting 空值处理不一致
- **文件**: `src/host/telnix/settings_store.py`
- **行号**: 106
- **类型**: 恢复机制缺失
- **描述**: 无法区分"未设置"和"显式设置为空"两种状态。

### 边界条件缺失

#### 5. SSE 心跳 15 秒可能被关闭
- **文件**: `src/host/telnix/api/sessions.py`
- **行号**: 109
- **类型**: 边界缺失
- **描述**: SSE 心跳间隔 15 秒，某些代理可能关闭空闲连接。
- **建议**: 默认值不超过 10 秒。

#### 6. auto_stop_seconds 无上限
- **文件**: `src/host/telnix/api/capture.py`
- **行号**: 91
- **类型**: 边界缺失
- **描述**: `auto_stop_seconds` 可设为极大值。
- **建议**: 添加上限如 86400 秒。

#### 7. 热力图时间解析假设 ISO 8601
- **文件**: `src/host/telnix/db.py`
- **行号**: 2032
- **类型**: 边界缺失
- **描述**: 使用 `substr(timestamp, 0, 19)` 假设格式。

#### 8. do_bump 多缓存状态不一致
- **文件**: `src/host/telnix/proxy/server.py`
- **行号**: 1566
- **类型**: 边界缺失
- **描述**: SSL bump 决策依赖多个独立缓存状态，可能不一致。

### 配置问题

#### 9. AGENT_IGNORE_PROCESSES 硬编码
- **文件**: `src/host/telnix/cli.py`
- **行号**: 2107
- **类型**: 配置问题
- **描述**: 进程名硬编码，应支持 settings.json 配置。

#### 10. MAX_RECORDED_BODY 等硬编码阈值
- **文件**: `src/host/telnix/proxy/server.py`
- **行号**: 42
- **类型**: 配置问题
- **描述**: 关键性能参数硬编码，不支持动态调整。

#### 11. ReDoS 防护参数硬编码
- **文件**: `src/host/telnix/auto_reply/rules.py`
- **行号**: 33
- **类型**: 配置问题
- **描述**: `_MAX_PATTERN_LEN = 256` 等参数固定。

#### 12. MCP 输出截断阈值硬编码
- **文件**: `src/host/telnix/mcp_server.py`
- **行号**: 58
- **类型**: 配置问题
- **描述**: `MAX_OUTPUT_BYTES = 64 * 1024` 不支持配置。

#### 13. 并发参数硬编码
- **文件**: `src/host/telnix/proxy/server.py`
- **行号**: 678
- **类型**: 配置问题
- **描述**: `_client_sem = 500`, `max_workers=2` 硬编码。

### 设计不合理

#### 14. SSL bump 决策逻辑分散
- **文件**: `src/host/telnix/proxy/server.py`
- **行号**: 1536
- **类型**: 设计不合理
- **描述**: do_bump 决策逻辑分散在 100+ 行代码中。
- **建议**: 提取为独立函数便于测试。

#### 15. 缓存实现各自独立
- **文件**: `src/host/telnix/db.py`
- **行号**: 1758
- **类型**: 设计不合理
- **描述**: `_overview_cache` 和 `_stats_cache` 各自实现。
- **建议**: 抽象统一缓存工具类。

#### 16. CLI --by endpoint 客户端聚合
- **文件**: `src/host/telnix/cli.py`
- **行号**: 1395
- **类型**: 可用性问题
- **描述**: `cmd_packets_stats --by endpoint` 拉取所有流后客户端处理。
- **建议**: 下沉到后端 API 使用 SQL GROUP BY。

### 功能缺失

#### 17. 脚本错误无历史记录
- **文件**: `src/host/telnix/api/auto_reply.py`
- **行号**: 522
- **类型**: 功能缺失
- **描述**: `/auto-reply/rules/{rule_id}/script-error` 只返回最后一条错误。
- **建议**: 维护最近 10 条错误历史。

#### 18. 缓存超限无用户通知
- **文件**: `src/ui/src/stores/flows.ts`
- **行号**: 234
- **类型**: 功能缺失
- **描述**: 缓存达到阈值时无用户通知。
- **建议**: 发送通知让用户选择处理方式。

---

## 七、修复优先级建议

### P0 - 必须立即修复
1. `script_worker.py` - `__import__` 绕过沙箱 (安全)
2. `decode.py` - 插件路径遍历 (安全)

### P1 - 高优先级
1. `export.py` - HAR 敏感头未脱敏 (安全)
2. `proxy_tools.py` - SSRF 风险 (安全)
3. `AIView.vue` - 模板语法错误 (前端 Bug)
4. `db.py` - 性能问题 (3个高危)

### P2 - 中优先级
1. 异常变量 `e` 未定义问题 (4个文件)
2. CSV 导出格式错误
3. pcap ACK 序列号错误
4. 重复重放边界条件
5. `flows.ts` - SSE lite flow 重复加载
6. `FlowList.vue` - v-memo 依赖问题

### P3 - 低优先级 (可后续迭代)
1. 硬编码配置参数化
2. 缓存实现统一
3. SSL bump 决策逻辑重构
4. 数据模型规范化
5. 恢复机制完善

---

## 附录：原始 JSON 数据

原始审计结果保存在以下文件：
- `audit_results_backend_bugs.json`
- `audit_results_security.json`
- `audit_results_performance.json`
- `audit_results_feature_design.json`
- `audit_results_frontend.json`
