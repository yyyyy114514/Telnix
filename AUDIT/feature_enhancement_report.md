# Telnix 功能增强报告

> 生成时间：2026-08-04
> 分析范围：19 个前端页面 + 33 个后端 API 模块
> 报告生成：Claude Code

---

## 一、总览

本报告对 Telnix 的 19 个前端页面进行全面的功能增强分析，提出约 **85 个创新实用功能点**。

### 页面覆盖

| 页面 | 路由 | 增强点数 |
|------|------|----------|
| 抓包页面 | /capture | 7 |
| AI 分析页面 | /ai | 6 |
| 工具页面 | /tools | 7 |
| 设置页面 | /settings | 6 |
| 分析页面 | /analyze | 7 |
| 发包工具 | /send | 5 |
| 日志页面 | /logs | 6 |
| 拦截规则 | /auto-reply | 7 |
| 搜索页面 | /search | 8 |
| Clash 集成 | /clash | 10 |
| DNS 劫持 | /dns-hijack | - |
| 延迟测试 | /delay | 5 |
| 录制回放 | /record | - |
| Mock 服务 | /mock | - |
| 原始抓包 | /raw | 5 |
| WebSocket | /ws | - |
| Cookie 管理 | /cookies | - |
| 站点地图 | /site-map | - |
| 时序分析 | /timeline | - |

---

## 二、抓包页面（CaptureView）增强方案

### 2.1 智能流量标记与分类系统

**需求**：当前系统仅有 `tags` 字段，但没有便捷的标记 UI 和自动标记能力。

**实现方案**：
```typescript
interface FlowTag {
  id: string
  name: string           // 标记名称
  color: string          // 十六进制颜色
  icon?: string          // 可选图标
  auto_rules?: TagRule[] // 自动标记规则
}

interface TagRule {
  field: 'host' | 'method' | 'status' | 'path' | 'content_type'
  operator: 'contains' | 'equals' | 'regex' | 'startsWith'
  value: string
}
```

**交互设计**：
- 行内快速标记（点击状态码旁的标记图标）
- 右键菜单批量标记
- 标记筛选器（工具栏下拉）
- 自动标记规则管理（Settings 页面）

### 2.2 流量对比视图

**需求**：对比两个相似请求的差异（修改参数前后、不同环境）。

**实现方案**：
```vue
<!-- CompareDialog.vue -->
<template>
  <el-dialog title="流量对比" width="90%">
    <div class="compare-container">
      <div class="compare-left">
        <FlowPicker label="流量 A" @select="flowA = $event" />
        <RequestView :flow="flowA" />
        <ResponseView :flow="flowA" :diff-mode="true" />
      </div>
      <div class="compare-divider" />
      <div class="compare-right">
        <!-- 流量 B -->
      </div>
    </div>
  </el-dialog>
</template>
```

**Diff 算法**：JSON 使用 `fast-json-patch`，文本使用 `diff-match-patch`

### 2.3 流量内联预览增强

**需求**：无需点击即可判断流量内容。

**实现方案**：
```typescript
const previewPlugins = [
  {
    contentType: ['image/png', 'image/jpeg', 'image/gif', 'image/webp'],
    render: (flow) => `<img src="data:${ct};base64,${body}" class="inline-preview" />`
  },
  {
    contentType: ['application/json'],
    render: (flow) => `<pre class="json-preview">${formatJson(body)}</pre>`
  }
]
```

**交互**：行 hover 时右侧滑出缩略预览，多选模式支持批量预览缩略图。

### 2.4 高级批量操作与工作流

**需求**：复杂的测试场景需要更灵活的操作序列。

**实现方案**：
```typescript
interface BatchWorkflow {
  id: string
  name: string
  steps: WorkflowStep[]
}

interface WorkflowStep {
  action: 'replay' | 'modify' | 'delay' | 'export' | 'tag' | 'wait'
  params: Record<string, any>
  condition?: FilterCondition
}
```

### 2.5 流量时间线与瀑布图视图

**需求**：感知流量在时间维度上的分布和耗时组成。

**实现方案**：
- 水平时间轴，流量按时间顺序排列
- 颜色编码：2xx（绿）、3xx（蓝）、4xx（橙）、5xx（红）
- 点击条目跳转详情
- 底部显示流量密度热力图

### 2.6 流量搜索增强

**需求**：更强大的搜索能力和搜索历史。

**实现方案**：
- 正则表达式支持：`/regex/flags`
- 搜索历史（本地持久化）
- 保存的搜索（带名称和图标）
- 搜索高亮（列表中匹配文本高亮）

### 2.7 流量书签与书签组

**需求**：临时保存重要流量位置，类似浏览器书签。

**实现方案**：
- 右键菜单添加书签（支持备注）
- 工具栏书签下拉
- 书签组管理（创建/重命名/删除/排序）
- 点击书签跳转并高亮对应行
- 书签导出/导入（JSON）

---

## 三、AI 分析页面（AIView）增强方案

### 3.1 多 AI 服务支持

**现状**：硬编码 Claude，扩展困难

**实现方案**：
```python
class BaseAIProvider:
    async def chat(self, messages, tools=None) -> ChatResult: ...
    async def analyze(self, flows) -> str: ...

class DeepSeekProvider(BaseAIProvider): ...
class ClaudeProvider(BaseAIProvider): ...
class OpenAIProvider(BaseAIProvider): ...
class OllamaProvider(BaseAIProvider): ...  # 本地部署
```

**设置界面**：
- 支持配置多个 API Key
- 模型选择下拉：DeepSeek-v4 / Claude 3.5 / GPT-4o / Gemini 1.5 / Ollama

### 3.2 流式响应（SSE）+ Markdown 渐进渲染

**现状**：等待完整响应后一次性显示

**实现方案**：
```python
@router.post("/ai/chat/stream")
async def chat_stream(body: ChatRequest):
    """SSE 流式响应"""
    async def event_generator():
        async for chunk in deepseek.stream_chat(...):
            yield f"data: {json.dumps({'token': chunk})}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

### 3.3 智能上下文管理

**现状**：历史消息无限累积，浪费 token

**实现方案**：
```python
class ContextManager:
    def __init__(self, max_tokens: int = 128000):
        self.summary_threshold = 0.7
    
    def compress_if_needed(self, messages: list) -> list:
        """自动摘要压缩历史"""
```

### 3.4 自动分析策略

**需求**：配置自动触发规则，被动发现安全/性能问题。

**实现方案**：
```python
class AutoAnalyzePolicy(BaseModel):
    triggers: list[Trigger] = []

class Trigger(BaseModel):
    condition: str  # "host~=*.api.* && status>=500"
    action: str     # "analyze" | "alert" | "auto_rule"
    severity: str   # "low" | "medium" | "high" | "critical"
```

### 3.5 分析报告导出

**需求**：导出分析结果为 Markdown/PDF/HAR+AI

**实现方案**：
```python
@router.get("/ai/chats/{chat_id}/export")
async def export_chat(chat_id: int, format: str = "markdown"):
    """导出聊天记录"""
```

### 3.6 AI 成本追踪

**需求**：防止意外超额，帮助优化 API 使用

**实现方案**：
```python
# db.py 新增表
CREATE TABLE ai_usage (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cost_usd REAL,
    created_at TEXT
)
```

---

## 四、工具页面（ToolsView）增强方案

### 4.1 Map Local 变量替换 + 目录映射

**功能描述**：支持 URL 变量映射到文件系统

**技术方案**：
```
URL: GET /api/users/123/profile
Map: /api/users/{id}/profile → D:\mock\users\{id}\profile.json
结果: D:\mock\users\123\profile.json
```

**扩展**：
- 正则捕获组：`/api/v2/users/(\d+)/orders` → `mock/users_$1_orders.json`
- 查询参数：`{query.page}`、`{query.size}`
- 通配目录映射：`/static/*` → `D:\assets\{path}`

### 4.2 Map Remote 增强 - 请求/响应管道

**功能描述**：在远程转发过程中插入请求/响应修改管道

**技术方案**：
```yaml
rules:
  - pattern: "*/api/*"
    target: https://staging.example.com
    request_transform:
      headers:
        add: { "X-Debug": "true" }
        remove: ["Authorization"]
    response_transform:
      headers:
        add: { "X-Served-By": "telnix" }
```

### 4.3 Mirror 增强 - 智能录制 + HAR 导出

**功能描述**：Mirror 不仅保存文件，还能录制完整会话

**界面改进**：
```
Mirror 配置：
├─ 保存目录: [________________] [浏览]
├─ 过滤条件:
│   ├─ ☑ 仅保存成功响应 (2xx)
│   ├─ Content-Type: [application/json ▼]
├─ 保存格式:
│   ├─ (●) 原始文件 + .meta.json
│   ├─ ( ) HAR 格式 (完整会话)
│   └─ ( ) cURL 脚本
└─ [开始录制] [停止录制] [导出 HAR]
```

### 4.4 规则管理增强

**功能描述**：专业级规则管理界面

**实现方案**：
- **规则分组**：不同环境（开发/测试/生产）
- **命中统计**：显示每条规则的命中次数
- **批量操作**：多选、拖拽排序、导入/导出

### 4.5 条件链 + 循环

**功能描述**：超越简单匹配的规则引擎

**技术方案**：
```yaml
rules:
  - name: "生产环境 Debug"
    condition: "header.X-Debug == 'true'"
    then:
      action: map_local
      file: "debug_response.json"
    else:
      action: passthrough
```

### 4.6 响应模板引擎

**功能描述**：Mustache/Jinja2 风格的响应模板

**技术方案**：
```json
{
  "id": "{{request.params.id}}",
  "name": "User {{request.params.id}}",
  "timestamp": "{{timestamp}}",
  "random": "{{random 1 100}}"
}
```

### 4.7 工具执行顺序控制

**功能描述**：精确控制工具执行顺序

**UI 设计**：
```
工具执行顺序（可拖拽排序）：
┌──────────────────────────────────────────┐
│ ☐ No Caching        [全局优先]            │
│ 1. Allow List       [最先检查]   [禁用] │
│ 2. Block List       [第二检查]   [启用] │
│ 3. Map Local        [第三检查]   [启用] │
│ 4. Map Remote       [第四检查]   [启用] │
│ 5. Mirror           [最后执行]   [启用] │
└──────────────────────────────────────────┘
```

---

## 五、设置页面（SettingsView）增强方案

### 5.1 SSL/TLS 高级配置面板

**现状**：SSL bump 仅能签发证书，无法控制 TLS 行为

**实现方案**：
```
┌─────────────────────────────────────────────────────┐
│  SSL/TLS 高级配置                                    │
├─────────────────────────────────────────────────────┤
│  最低 TLS 版本: [TLS 1.2 ▼]                         │
│  密码套件:                                           │
│    [✓] TLS_AES_256_GCM_SHA384                       │
│    [✓] TLS_CHACHA20_POLY1305_SHA256                 │
│    [ ] TLS_RSA_WITH_AES_256_CBC_SHA                 │
│  SNI 伪装:                                           │
│    模式: [禁用 ▼]                                    │
└─────────────────────────────────────────────────────┘
```

### 5.2 性能配置可视化面板

**现状**：性能参数硬编码

**实现方案**：
```
┌─────────────────────────────────────────────────────┐
│  性能调优                                           │
├─────────────────────────────────────────────────────┤
│  预设方案: [自动 ▼]                                  │
│  记录 Body 上限: [512] KB                           │
│  解压阈值: [256] KB                                 │
│  SSL 上下文缓存: [150] 个                           │
│  最大并发连接: [500]                                │
│  连接空闲超时: [120] 秒                             │
└─────────────────────────────────────────────────────┘
```

### 5.3 证书生命周期管理

**实现方案**：
```
┌─────────────────────────────────────────────────────┐
│  证书管理                                           │
├─────────────────────────────────────────────────────┤
│  根证书状态:                                        │
│    有效期: 2024-01-01 → 2034-01-01 (8255天剩余)   │
│  叶证书缓存:                                        │
│    缓存数量: 127 / 2000                             │
│  证书过期告警:                                       │
│    [✓] 根证书到期前 30 天提醒                        │
└─────────────────────────────────────────────────────┘
```

### 5.4 代理引擎监控面板

**实现方案**：
```
┌─────────────────────────────────────────────────────┐
│  引擎监控                                           │
├─────────────────────────────────────────────────────┤
│  当前引擎: builtin  ● 运行中                        │
│  实时统计:                                          │
│  ┌──────────┬──────────┬──────────┐              │
│  │ builtin  │   v2     │  mitm    │              │
│  │  ● ACTIVE│  ○ IDLE  │  ○ IDLE  │              │
│  └──────────┴──────────┴──────────┘              │
└─────────────────────────────────────────────────────┘
```

### 5.5 界面个性化配置中心

**实现方案**：
```
┌─────────────────────────────────────────────────────┐
│  界面个性化                                          │
├─────────────────────────────────────────────────────┤
│  主题色:                                            │
│  ● 预设: [科技蓝] [翡翠绿] [玫瑰红] [琥珀金]        │
│  代码字体: [JetBrains Mono ▼]  大小: [13] px        │
│  流量列表:                                           │
│  行高: [紧凑 ▼] (紧凑/标准/宽松)                    │
└─────────────────────────────────────────────────────┘
```

### 5.6 配置导入导出与同步

**实现方案**：
```
┌─────────────────────────────────────────────────────┐
│  配置管理                                           │
├─────────────────────────────────────────────────────┤
│  导出配置: [导出全部] [仅导出设置] [仅导出规则]     │
│  导入配置: [从文件导入] [从剪贴板导入]             │
│  云同步:                                             │
│  [ ] 启用云同步                                      │
│  同步服务: [自建服务器 ▼] [WebDAV] [S3]           │
└─────────────────────────────────────────────────────┘
```

---

## 六、分析页面（AnalyzeView）增强方案

### 6.1 响应时间深度分析

**现状**：只有平均耗时，没有分布图

**实现方案**：
```python
GET /flows/latency-stats
返回: { 
  p50: 45, p75: 90, p90: 180, p95: 350, p99: 800,
  buckets: [
    {"range": "<50ms", "count": 1200, "pct": 60},
    {"range": "50-200ms", "count": 500, "pct": 25}
  ]
}
```

### 6.2 多维度交叉分析

**实现方案**：
```python
GET /flows/cross-analysis?dim1=host&dim2=status_code
返回交叉矩阵（热力图显示）
```

### 6.3 智能异常检测与告警

**实现方案**：
```python
GET /flows/anomalies?hours=24
自动检测:
- 高延迟异常: P95 > 历史基线 2x
- 错误率异常: 4xx/5xx 占比 > 10%
- 流量突增/突降: 环比变化 > 50%
```

### 6.4 会话对比分析

**实现方案**：
```python
GET /flows/compare?session1=1&session2=2
返回: {
  summary: { total_diff: +15%, latency_diff: -20% },
  new_endpoints: [...],
  performance_delta: [...]
}
```

### 6.5 增强报表导出

**实现方案**：
```python
GET /flows/report?format=json|csv|html
支持: JSON / CSV / HTML / PDF
包含: 摘要、图表、Top 端点、异常、建议
```

### 6.6 趋势预测与容量规划

**实现方案**：
```python
GET /flows/trends?hours=168
返回: {
  current_qps: 125,
  predicted_24h: { qps: [...], confidence: 0.85 },
  capacity_needed: { current: 200, recommended: 500 }
}
```

### 6.7 端点性能画像

**实现方案**：
```python
GET /flows/endpoint-profile/{host}/{path_template}
返回该端点完整画像: 请求数、成功率、延迟分布、错误详情
```

---

## 七、发包工具（SendView）增强方案

### 7.1 变量系统 + 多环境配置

**现状**：无法定义 `{{baseUrl}}`、`{{token}}` 等变量

**实现方案**：
```typescript
interface Environment {
  name: string
  variables: Record<string, string>
  isActive: boolean
}

// 请求中引用
URL: {{baseUrl}}/users/{{userId}}/profile
Headers: Authorization: Bearer {{token}}
```

### 7.2 参数化批量测试

**现状**：要手动改参数发多次请求

**实现方案**：
```typescript
// CSV 导入批量发送
| userId | status | response_time |
|--------|--------|---------------|
| 1      | 200    | 120ms         |
| 2      | 404    | 80ms          |
| 3      | 200    | 115ms         |
```

### 7.3 请求序列/工作流引擎

**现状**：多步骤流程要手动复制粘贴

**实现方案**：
```typescript
interface Workflow {
  steps: WorkflowStep[]
}
interface WorkflowStep {
  request: SendRequest
  extract: { variable: string, jsonPath: string }[]
}
```

### 7.4 智能历史管理

**现状**：历史记录无法搜索和批量操作

**实现方案**：
- 历史高级筛选（域名/状态码/时间）
- 历史差异对比
- 批量删除/导出/转模板

### 7.5 响应断言

**现状**：手动检查响应正确性

**实现方案**：
```typescript
interface ResponseAssertion {
  type: 'status' | 'body_contains' | 'json_path' | 'header'
  expected: string | number
  operator: 'equals' | 'contains' | 'gt' | 'lt' | 'regex'
}
```

---

## 八、日志页面（LogView）增强方案

### 8.1 日志实时监控中心

**现状**：日志页面无实时性

**实现方案**：
```python
@router.get("/logs/stream")
async def log_stream():
    """SSE 实时推送新日志"""
```

### 8.2 日志智能分析与告警

**实现方案**：
```typescript
interface LogIntelligence {
  summary: { errorRate, categoryDistribution }
  anomalyDetection: { type, description, suggestion }[]
  alertRules: { condition, action, enabled }[]
}
```

### 8.3 日志关联跳转

**实现方案**：
- 点击日志中的 flow_id 跳转到抓包页
- 解析 error.message 中的请求 ID

### 8.4 Cookie 生命周期管理器

**实现方案**：
- Cookie 编辑与重放
- 有效期可视化（红色警告 <7天）
- Netscape 格式导入/导出

### 8.5 智能站点地图分析

**实现方案**：
- API 版本识别与分组
- 路径参数模板化
- OpenAPI/Swagger 导出

### 8.6 时序瀑布流性能分析

**实现方案**：
- 慢请求自动高亮
- 并发分析
- P50/P90/P99 延迟统计

---

## 九、拦截规则页面（AutoReplyView）增强方案

### 9.1 规则匹配智能预览

**现状**：用户无法预览规则会匹配哪些流量

**实现方案**：
```python
POST /auto-reply/rules/preview-match
返回: 匹配数量 + 示例 URL 列表
```

### 9.2 规则执行可视化监控

**现状**：不知道哪些规则在生效

**实现方案**：
```
实时显示规则命中率排行榜
最近 100 次命中的时间线
每条规则的执行耗时热力图
```

### 9.3 规则模板市场

**现状**：新建规则是空白表单

**实现方案**：
- 内置 20+ 常见场景模板
- 用户可把规则存为模板
- 支持从 URL 导入社区模板

### 9.4 规则分组与标签管理

**实现方案**：
```python
class RuleCreate(BaseModel):
    group_id: str = ""
    tags: list[str] = []
    priority: int = 0
```

### 9.5 规则版本管理与撤销

**实现方案**：
- 每次修改保存快照
- 支持查看历史版本
- 支持对比差异和回滚

### 9.6 规则导入冲突可视化

**实现方案**：
- 导入前预览冲突规则
- 对每个冲突提供策略：覆盖/跳过/重命名
- 选择性导入（勾选要导入的规则）

### 9.7 脚本调试增强

**实现方案**：
- 捕获 `print()` 输出并返回前端
- 支持 `ctx.log()` 记录中间变量
- 执行步骤标记（`@debug` 装饰器）

---

## 十、搜索页面（SearchView）增强方案

### 10.1 智能搜索历史系统

**现状**：无搜索历史存储

**实现方案**：
```typescript
interface SearchHistoryItem {
  id: number
  query: SearchRequest
  label?: string
  hitCount: number
}
```

### 10.2 搜索结果上下文高亮

**现状**：列表只显示基本信息，无匹配位置

**实现方案**：
```
#123 | POST | api.example.com/v1/users | 200
       匹配: "users" in response_body (pos 234)
```

### 10.3 正则表达式安全助手

**现状**：无效正则导致后端报错

**实现方案**：
- 前端实时验证
- 常用正则模板快捷插入
- 正则测试工具

### 10.4 搜索结果批量操作栏

**实现方案**：
```
已选择 15 项
[添加标签] [导出] [重放] [删除] [全选] [取消]
```

### 10.5 搜索结果无限滚动

**现状**：超过 500 条后无法查看更多

**实现方案**：虚拟滚动 + 分页加载

### 10.6 高级搜索语法

**实现方案**：
```
body:regex "error" && status >= 400
host contains "api" || host contains "cdn"
```

### 10.7 搜索性能监控

**实现方案**：
```
找到 234 条结果
(扫描 10,000 行, 耗时 234ms)
```

### 10.8 搜索书签

**实现方案**：
```typescript
interface SavedSearch {
  name: string
  query: SearchRequest
  autoRefresh: boolean
  refreshInterval: number
}
```

---

## 十一、Clash 集成增强方案

### 11.1 DoH/DoT 安全 DNS 拦截

**问题**：浏览器默认启用 DoH，绕过 UDP/TCP 53 端口劫持

**解决方案**：
- 检测浏览器 DoH 行为，动态注入 DNS 修正
- 系统级 DoH 禁用引导
- 透明代理联动

### 11.2 Clash 节点管理器前端

**现状**：后端有完整 API，前端只调用了 5 个

**实现方案**：
```
┌─ 代理节点管理 ───────────────────────────────────┐
│ ▼ Proxy (代理)                                  │
│   ├─ 🇺🇸 美国 01  (156ms)  [选中]              │
│   ├─ 🇭🇰 香港 01  (89ms)                       │
│   └─ 🇸🇬 新加坡 01 (112ms)                     │
│ ▼ URL Test (自动选择)                           │
│   └─ 🔄 测试延迟...                             │
└──────────────────────────────────────────────────┘
```

### 11.3 DNS 规则分组与优先级

**现状**：规则是扁平列表

**实现方案**：
```
┌─ DNS 规则管理 ──────────────────────┐
│ ▼ 开发调试 (优先级 1) [启用]        │
│   *.dev.local → 127.0.0.1          │
│ ▼ 广告拦截 (优先级 2) [启用]        │
│   *.ads.* → 0.0.0.0                │
└─────────────────────────────────────┘
```

### 11.4 DNS 规则导入/导出

**实现方案**：
- 支持 Clash 域名规则格式
- 支持 hosts 格式
- 支持内置规则集

### 11.5 DNS 日志高级搜索

**实现方案**：
```python
GET /dns-hijack/logs?search=ads&new_ip=0.0.0.0
GET /dns-hijack/stats?period=24h
```

### 11.6 Clash 连接可视化监控

**实现方案**：
```
┌─ 活跃连接监控 ───────────────────────────────────┐
│ 进程          Host           上行    下行    节点│
│ chrome.exe    api.example    12KB/s  56KB/s  US01│
└──────────────────────────────────────────────────┘
```

### 11.7 智能 DNS 缓存预热

**解决方案**：切换 DNS 时自动预热关键域名

### 11.8 Clash 规则集实时预览

**实现方案**：预览某请求会被哪个规则匹配

### 11.9 DNS 劫持与抓包联动

**实现方案**：被 DNS 劫持的请求在抓包页显示 🔒 图标

### 11.10 多域名后端模式

**实现方案**：
```
*.staging.company.com → 10.0.0.100
*.prod.company.com → 10.0.0.200
```

---

## 十二、延迟/录制/Mock 页面增强方案

### 12.1 智能延迟规则生成器

**现状**：无法从抓包历史一键生成规则

**实现方案**：
- FlowList 行操作新增"添加延迟规则"按钮
- 延迟分布热力图
- 梯度延迟：`jitter(base=300, variance=50)`
- 命中日志面板

### 12.2 流量录制增强 + 变量替换引擎

**现状**：录制时无法处理环境差异

**实现方案**：
```typescript
// 变量提取
URL: /api/users/{{userId}}/profile
环境变量: { "dev": {userId: 1}, "prod": {userId: 999} }

// 条件执行
if (response.status == 401) skip()
```

### 12.3 高级 Mock 规则引擎

**现状**：body 只能是硬编码字符串

**实现方案**：
```json
// 动态响应模板
{
  "id": "{{request.params.id}}",
  "timestamp": "{{system.now}}"
}

// 多条件匹配
conditions: [
  { field: "path", op: "equals", value: "/api/users" },
  { field: "query.id", op: ">=", value: "100" }
]
```

### 12.4 三大模块联动工作流

**实现方案**：
- Mock + 延迟联调
- 录制 → 回放 → Mock 自举
- 录制 → 添加延迟 → 回放压力测试

### 12.5 团队协作与数据持久化

**实现方案**：
- 规则导入/导出标准格式
- 规则版本历史
- 批量导入 Postman Collection

---

## 十三、原始抓包和 WebSocket 页面增强方案

### 13.1 RawCapture 实时带宽监控仪表盘

**现状**：无法感知网络繁忙程度

**实现方案**：
```
┌─────────────────────────────────────────────────────┐
│  TCP/UDP 实时监控                          [重置]   │
├─────────────────────────────────────────────────────┤
│  速率: 1,234 pps  │  带宽: 5.6 Mbps  │  丢包: 12   │
├─────────────────────────────────────────────────────┤
│  协议分布           │  进程分布（Top 5）              │
│  ┌───┐             │  chrome.exe ██████████ 45%   │
│  │TCP│ 72%        │  electron.exe █████ 22%        │
│  └───┘             │                                 │
└─────────────────────────────────────────────────────┘
```

### 13.2 RawCapture PCAP 导出与导入

**现状**：无法用 Wireshark 分析

**实现方案**：
- `/raw/export-pcap` API
- `/raw/import-pcap` API
- 前端 PCAP 工具对话框

### 13.3 WebSocket 连接会话追踪

**现状**：消息是平铺列表

**实现方案**：
```
┌──────────────────────────────────────────────────────────────────┐
│  ▶ ws://api.example.com/socket    [在线]  2m 34s  128 条消息     │
│  │  ├── #45  ↑ SEND  {"type":"ping"}         12:01:23  12 B     │
│  │  ├── #46  ↓ RECV  {"type":"pong"}          12:01:23  28 B    │
│  │  └── #47  ↑ SEND  {"type":"msg"}           12:01:30  ...    │
└──────────────────────────────────────────────────────────────────┘
```

### 13.4 WebSocket 消息时间线可视化

**现状**：无法识别消息模式

**实现方案**：
```
12:01:20.123 │▓▓▓▓▓▓▓▓│ SEND   128 B  {"type":"subscribe"...}
12:01:20.456 │▓▓▓▓▓▓▓▓▓▓▓│ RECV  2.1 KB {"type":"update",...}
```

### 13.5 RawCapture + WebSocket 联动分析

**现状**：HTTP 和 WS 是独立视图

**实现方案**：
```
┌─ HTTP/WS 关联分析 ─────────────────────────────────────┐
│ GET https://api.example.com/socket  [101 Switching]    │
│   └── WS 连接: ws://api.example.com/socket (128 条)   │
│       ├── ↑ SEND  {"action":"auth"}                    │
│       └── ↓ RECV  {"success":true}                     │
└────────────────────────────────────────────────────────┘
```

---

## 十四、优先级总结

### P0（必须实现 - 核心竞争力）

| 功能 | 页面 | 理由 |
|------|------|------|
| 搜索历史系统 | SearchView | 使用频率极高 |
| 搜索结果上下文高亮 | SearchView | 核心体验 |
| 日志实时监控 | LogView | 调试必备 |
| Cookie 编辑与重放 | CookiesView | 实际测试需求 |
| AI 流式响应 | AIView | 体验提升最直接 |
| 多 AI 服务支持 | AIView | 灵活性最大 |
| Map Local 变量替换 | ToolsView | 最大痛点 |
| 规则匹配预览 | AutoReplyView | 高频痛点 |
| Clash 节点管理器 | ClashView | 核心功能缺失 |
| DoH/DoT 拦截 | DnsHijackView | 功能可用性 |

### P1（重要增强 - 用户价值高）

| 功能 | 页面 | 理由 |
|------|------|------|
| 智能流量标记 | CaptureView | 组织能力 |
| 流量对比视图 | CaptureView | 排查效率 |
| 内联预览增强 | CaptureView | 减少点击 |
| 响应时间深度分析 | AnalyzeView | 性能分析 |
| 多维度交叉分析 | AnalyzeView | 深度分析 |
| 变量系统 + 环境配置 | SendView | 基础体验 |
| 历史高级管理 | SendView | 痛点明显 |
| 参数化批量测试 | SendView | 核心功能 |
| 性能配置面板 | SettingsView | 大流量场景 |
| SSL/TLS 高级配置 | SettingsView | 安全性 |
| 延迟规则命中日志 | DelayView | 可见性 |
| Mock 动态模板 | MockView | 核心增强 |
| RawCapture 带宽监控 | RawCaptureView | 运维必备 |

### P2（进阶功能 - 锦上添花）

| 功能 | 页面 | 理由 |
|------|------|------|
| 批量工作流 | CaptureView | 自动化 |
| 时间线视图 | CaptureView | 维度感知 |
| 书签系统 | CaptureView | 效率提升 |
| 会话对比 | AnalyzeView | 评估改包 |
| 增强报表导出 | AnalyzeView | 协作需求 |
| 自动分析策略 | AIView | 主动监控 |
| 成本追踪 | AIView | 预算控制 |
| WS 会话追踪 | WebSocketView | 可用性 |
| WS 时间线可视化 | WebSocketView | 差异化 |
| HTTP-WS 联动 | 综合 | 差异化 |

### P3（高级功能 - 小众需求）

| 功能 | 页面 | 理由 |
|------|------|------|
| 趋势预测 | AnalyzeView | 高级分析 |
| 端点性能画像 | AnalyzeView | 全面了解 |
| 告警规则引擎 | LogView | 主动监控 |
| 跨页面关联 | 综合 | 效率提升 |
| PCAP 导出 | RawCaptureView | 专业需求 |
| Mock HTTPS | MockView | 特殊场景 |
| 团队协作同步 | 综合 | 团队需求 |

---

## 十五、差异化亮点（相对于 Fiddler/Charles）

Telnix 相比竞品的独特优势方向：

1. **WebSocket 时间线可视化**：现有工具均无此功能
2. **HTTP-WS 联动分析**：跨协议关联分析
3. **AI 深度集成**：DeepSeek/Claude/GPT 多模型支持
4. **全链路测试平台**：延迟+录制+Mock 一体化
5. **变量替换引擎**：让录制回放适应多环境
6. **智能上下文管理**：自动压缩对话上下文

---

## 十六、技术债务提示

1. **db.py search_flows 函数**（~120 行），建议拆分
2. **规则存储分散**：三个模块各自独立存储在 `settings.json`
3. **Mock 服务器**：轮询刷新日志，建议 WebSocket 推送
4. **ReDoS 防护**：正则表达式未限制复杂度
5. **单元测试覆盖**：关键逻辑缺少测试

---

**报告结束**
