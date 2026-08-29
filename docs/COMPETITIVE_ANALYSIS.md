# Telnix vs 竞品功能差距报告

> 调查日期：2026-08-04
> 调查工具：Charles Proxy、Fiddler、Proxyman、Wireshark、mitmproxy
> 说明：仅提供难度和价值评估，不实施

---

## 第一梯队：高价值 + 相对可实现

### 1. 请求重写 / 重映射 (Request Rewrite / URL Map)
**竞品支持情况：**
- Charles: Map Remote / Map Local / Rewrite
- Fiddler: AutoResponder (URL重定向)
- Proxyman: URL Map / Response Override
- mitmproxy: `map_local`, `map_remote` (已有基础实现)

**Telnix现状：** 已有基础的 Map Local / Map Remote 规则，但不支持：
- 按请求参数/Header 条件重写（如 `header:X-Token=abc` → 重写为 `token=xyz`）
- 正则表达式匹配重写
- 批量导入/导出规则集

**价值评估：** ★★★★★
**实现难度：** ★★☆☆☆（已有框架，扩展规则条件匹配）

---

### 2. 性能剖析 / 请求瀑布流时间分解
**竞品支持情况：**
- Charles: 请求时间分解（DNS/Connect/SSL/Request/Response）
- Chrome DevTools: Network Waterfall（已有类似实现）
- Proxyman: 响应时间分析

**Telnix现状：** TimelineView.vue 已有瀑布流，但不支持：
- DNS/TCP握手/SSL/TLS 握手/首字节时间分解
- 慢请求标记（>1s 标红）
- 并发请求数统计（同一时间窗口内并发数）

**价值评估：** ★★★★☆
**实现难度：** ★★★☆☆（需要后端记录各阶段时间戳，或从 timing 数据中解析）

---

### 3. 证书管理 / HTTPS 解密增强
**竞品支持情况：**
- Charles: 一键安装证书、自动信任、证书链查看
- Fiddler: DOH配置、证书固定(Certificate Pinning)检测
- mitmproxy: 自动生成证书、 Let's Encrypt 支持

**Telnix现状：** 基础证书安装功能已有，不支持：
- 证书链查看（cert_info tab 存在但数据为空）
- 证书固定检测
- 自动更新证书（过期提醒）

**价值评估：** ★★★★☆
**实现难度：** ★★★☆☆（后端已有基础，需要完善 cert_info 填充和前端展示）

---

## 第二梯队：中等价值

### 4. 接口diff / 版本对比
**竞品支持情况：**
- Charles: 无原生支持（靠第三方插件）
- Fiddler: Inspector 比较工具
- Postman: 内置 diff

**Telnix现状：** 无

**价值评估：** ★★★☆☆
**实现难度：** ★★★☆☆

---

### 5. 自动化测试 / CI集成
**竞品支持情况：**
- mitmdump: Python脚本自动化
- Fiddler: JScript.NET脚本
- Charles: AutoTester

**Telnix现状：** CLI/MCP 基础已有（可获取流量、配置规则），但：
- 无原生自动化测试框架
- 无录制回放（record 功能有，但不完整）

**价值评估：** ★★★☆☆
**实现难度：** ★★★★☆（需要完整录制回放系统）

---

### 6. 多会话管理增强
**竞品支持情况：**
- Charles: 多会话标签页
- Fiddler: 多Session

**Telnix现状：** session_id 基础已有，不支持：
- 会话命名/颜色标记
- 会话对比
- 会话分组（按项目/环境）

**价值评估：** ★★★☆☆
**实现难度：** ★★☆☆☆

---

### 7. 插件系统 / 扩展机制
**竞品支持情况：**
- Fiddler: 完整的插件API（JScript/C#/Python）
- mitmproxy: Addon API
- Charles: 无官方插件，但有第三方工具

**Telnix现状：** 无

**价值评估：** ★★★☆☆
**实现难度：** ★★★★★（架构改动大）

---

### 8. 远程抓包 / 分布式代理
**竞品支持情况：**
- Charles: Remote Recording
- Proxyman: Remote Device Capture

**价值评估：** ★★★☆☆
**实现难度：** ★★★★☆（后端架构改动大）

---

## 第三梯队：低价值或高难度

### 9. HTTP/3 (QUIC) 抓包
**竞品支持情况：**
- mitmproxy 11: 完整支持 HTTP/3 透明代理和反向代理
- Charles: 有限支持

**价值评估：** ★★☆☆☆
**实现难度：** ★★★★★（需要完整 QUIC/HTTP3 实现）

---

### 10. WebSocket 帧级调试增强
**竞品支持情况：**
- Charles: WebSocket 消息帧详细视图
- Proxyman: WebSocket 调试面板（可二进制解码）

**Telnix现状：** WS视图已有，不支持：
- 二进制帧解码（Protobuf/MsgPack等）
- 消息过滤（按内容/时间过滤）
- 导出为HAR格式

**价值评估：** ★★☆☆☆
**实现难度：** ★★★☆☆

---

### 11. 手机APP抓包简化
**竞品支持情况：**
- Charles: 扫码安装证书
- Proxyman: 一键生成描述文件

**Telnix现状：** 基础证书安装已有

**价值评估：** ★★☆☆☆
**实现难度：** ★★★☆☆

---

### 12. 流量搜索与全文索引
**竞品支持情况：**
- Wireshark: 完整包内容搜索
- mitmproxy: `view.flows()` 过滤

**Telnix现状：** SearchView 有基础搜索，不支持：
- 搜索历史
- 高亮搜索结果上下文

**价值评估：** ★★☆☆☆
**实现难度：** ★★☆☆☆

---

## 总结建议

**Telnix 应优先实施：**
1. **第一梯队全部**（价值最高、实现难度适中）
2. **第二梯队第4项**（接口diff，低难度高价值）
3. **第二梯队第6项**（多会话管理，低难度）

**长期路线图：**
- HTTP/3 支持 → 跟随 mitmproxy 路线
- 插件系统 → 需要架构重构
- 远程抓包 → 需要服务化改造
