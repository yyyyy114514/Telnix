import axios, { type AxiosResponse } from 'axios'
import { waitForWindivertAck } from '../stores/windivertWarning'
import i18n from '../i18n'

// ============ 类型定义 ============

/** 延迟规则数据结构 */
export interface DelayRule {
  id?: string
  enabled: boolean
  pattern: string
  match_mode: 'wildcard' | 'regex' | 'exact'
  phase: 'request' | 'response'
  delay_ms: number
  host: string
  note: string
  jitter_base?: number
  jitter_variance?: number
}

/** 延迟规则命中日志 */
export interface DelayHitLog {
  id: number
  timestamp: string
  rule_id: string
  rule_pattern: string
  flow_id: number
  url: string
  matched_delay_ms: number
  actual_delay_ms?: number
}

/** 延迟分布热力图数据 */
export interface DelayHeatmapData {
  buckets: Array<{
    time: string
    host: string
    avg_delay: number
    count: number
  }>
  host_distribution: Array<{
    host: string
    total_count: number
    avg_delay: number
  }>
}

/** 梯度延迟配置 */
export interface DelayJitterConfig {
  enabled: boolean
  base_ms: number
  variance_ms: number
}

/** Mock 响应模板 */
export interface MockTemplate {
  id?: string
  name: string
  description?: string
  variables: TemplateVariable[]
  body_template: string
  headers_template?: string
  status_code?: number
  content_type?: string
  delay_ms?: number
}

/** Mock 模板变量 */
export interface TemplateVariable {
  name: string
  description?: string
  type: 'string' | 'number' | 'boolean' | 'json' | 'regex'
  default_value?: string
  extraction_pattern?: string
  from_flow_field?: string
}

/** Mock 多条件匹配规则 */
export interface MockMultiMatchRule {
  id?: string
  enabled: boolean
  name: string
  conditions: MockMatchCondition[]
  mock_response: MockResponseTemplate
  priority?: number
  note?: string
}

/** Mock 匹配条件 */
export interface MockMatchCondition {
  field: 'method' | 'path' | 'host' | 'header' | 'body' | 'query' | 'status'
  operator: 'equals' | 'contains' | 'startsWith' | 'endsWith' | 'regex' | 'exists' | 'notExists'
  value?: string
  header_name?: string
}

/** Mock 响应模板 */
export interface MockResponseTemplate {
  status_code: number
  headers: Record<string, string>
  body: string
  delay_ms?: number
  template_mode?: boolean
}

/** Mock 规则（兼容现有定义） */
export interface MockRule {
  id?: string
  enabled: boolean
  method: string
  path: string
  match_mode: 'exact' | 'prefix' | 'regex'
  status_code: number
  headers: Record<string, string>
  body: string
  content_type: string
  delay_ms: number
  note: string
  conditions?: MockMatchCondition[]
  template_mode?: boolean
  template_variables?: TemplateVariable[]
}

/** Mock 日志条目 */
export interface MockLog {
  id: number
  timestamp: string
  matched_rule_id: string | null
  method: string
  path: string
  status_code: number
  response_time_ms: number
  request_body_size: number
  response_body_size: number
}

/** Mock 服务状态 */
export interface MockStatus {
  running: boolean
  port: number
  request_count: number
}

/** 录制脚本变量 */
export interface RecordScriptVariables {
  script_id: string
  variables: ScriptVariable[]
  flows: Array<{
    flow_id: number
    url: string
    extracted_variables: Record<string, string>
  }>
}

/** 脚本变量 */
export interface ScriptVariable {
  name: string
  description?: string
  type: 'path' | 'query' | 'header' | 'body' | 'json_path' | 'regex'
  extraction_rule: string
  sample_values: string[]
  from_flow_id: number
}

/** 回放环境配置 */
export interface ReplayEnvironment {
  id?: string
  name: string
  description?: string
  variables: Record<string, string>
  is_default?: boolean
}

/** 回放条件规则 */
export interface ReplayConditionRule {
  id?: string
  name: string
  condition_type: 'status_code' | 'response_body' | 'response_header' | 'request_header'
  operator: 'equals' | 'contains' | 'regex' | 'greater' | 'less'
  value: string
  action: 'skip' | 'delay' | 'mock' | 'abort'
  action_params?: Record<string, any>
}

/** 回放条件 */
export interface RecordCondition {
  field: string
  operator: string
  value: string
}

/** 断言规则 */
export interface AssertionRule {
  id?: string
  name: string
  field: 'status_code' | 'response_body' | 'response_header' | 'request_header'
  operator: 'equals' | 'contains' | 'regex' | 'greater' | 'less'
  expected_value: string
  enabled: boolean
  note: string
}

/** 后端统一响应格式 */
export interface ApiResult<T = any> {
  code: number
  data: T
  msg: string
  /** WinDivert 风险提示标记：true 表示需要先弹窗让用户确认 */
  need_ack?: boolean
}

/** WinDivert 风险提示状态 */
export interface WindivertWarningStatus {
  needed: boolean
  message: string
  brief?: string
  ack: boolean
  platform: string
}

/** 抓包状态 */
export interface Status {
  capturing: boolean
  proxy_port: number
  session_id: number
  cert_installed: boolean
  system_proxy_on?: boolean
  proxy_lost?: boolean
  started_at?: number
  /** 实际使用的 API 端口（可能与 settings.json 不同：随机端口或冲突自动切换） */
  api_port?: number
  /** 端口冲突信息：手动设置的端口被占用时记录原端口与新端口，前端弹窗提示 */
  port_conflict?: PortConflict | null
}

/** 端口冲突信息 */
export interface PortConflict {
  api?: { old: number; new: number }
  proxy?: { old: number; new: number }
}

/** 后端实际端口信息（/api/system/ports 返回） */
export interface PortInfo {
  api_port: number | null
  proxy_port: number | null
  api_host: string
  proxy_host: string
  port_conflict: PortConflict | null
}

/** 断点状态 */
export interface BpStatus {
  break_on_request: boolean
  break_on_response: boolean
  pending?: number[]
  pending_flows?: any[]
}

/** 流量数据结构 */
export interface Flow {
  id: number
  session_id: number
  timestamp: string
  pid: number | null
  process_name: string | null
  method: string
  url: string
  scheme: string
  host: string
  path: string
  request_headers: string | null
  request_body: string | null
  status_code: number | null
  response_headers: string | null
  response_body: string | null
  duration_ms: number | null
  size: number | null
  breakpoint_status: string | null
  protocol?: string // http | tcp | udp
  raw_data?: string | null // base64: 前缀的原始字节（TCP/UDP）
  src_port?: number | null
  dst_port?: number | null
  remote_ip?: string | null // 对端 IP（IP 属地分析）
  ip_region?: string | null // IP 属地（格式化后的字符串）
  cert_info?: string | null // TLS 证书信息 JSON 字符串
  tags?: string // 逗号分隔标签
  tag_note?: string // 标签备注
  http_version?: string // HTTP/1.1 | HTTP/2
  timing?: string | null // 响应时间组成 JSON（forward_ms 等）
}

/** 流量列表结果 */
export interface FlowsResult {
  flows: Flow[]
  total: number
}

/** 技术栈识别结果项 */
export interface TechFingerprint {
  name: string
  category: string  // server / language / framework / frontend / cms / cdn_waf / analytics / build_tool
  confidence: 'high' | 'medium' | 'low'
  version?: string
}

/** 透明代理状态 */
export interface TransparentProxyStatus {
  supported: boolean
  running: boolean
  redirected_count?: number
  passed_count?: number
  nat_table_size?: number
  last_error?: string
  local_port?: number
  redirect_ports?: number[]
  hint?: string
  // 跨平台字段
  backend?: string  // 'windivert' | 'iptables' | 'pf' | 'none'
  is_admin?: boolean
}

/** 进程信息 */
export interface ProcessInfo {
  pid: number
  name: string
}

/** 已忽略进程（含行 id，用于按 id 删除；pid 可空表示按名称忽略） */
export interface IgnoredProcess {
  id: number
  pid: number | null
  process_name: string
  ignored_at?: string
}

/** 自动修改规则 */
export interface AutoReplyRule {
  id?: string
  enabled: boolean
  match_mode: string // wildcard | exact | regex
  pattern: string
  // mock | mock_request | modify_request | modify_response | script
  action: string
  mock_status?: number
  mock_headers?: string | Record<string, string>
  mock_body?: string
  // modify_* 动作为 ModifyRule[]；script 动作为 Python 脚本源码字符串
  modify_rules?: ModifyRule[] | string
  note?: string
  // mock_request：写死请求（用预设请求转发到目标服务器，返回真实响应）
  mock_method?: string
  mock_url?: string
  // §3.2 规则分组与标签
  group_id?: number | null
  tags?: string
  // §3.2 命中统计
  hit_count?: number
  last_hit_at?: string
  last_hit_flow_id?: number | null
}

/** 修改规则项 */
export interface ModifyRule {
  target: string // request_header | request_body | response_header | response_body
  op: string // replace | append | remove
  key?: string
  value?: string
}

/** 脚本测试：模拟请求 */
export interface ScriptMockRequest {
  host: string
  path: string
  method: string
  scheme: string
  http_version?: string
  headers: Record<string, string>
  body: string
}

/** 脚本测试：模拟响应（可选，提供则调用 on_response） */
export interface ScriptMockResponse {
  status_code: number
  headers: Record<string, string>
  body: string
}

/** 脚本测试：单个阶段结果 */
export interface ScriptTestPhase {
  available: boolean       // 脚本是否定义了对应阶段函数
  action: string           // continue | drop | mock
  modified: boolean        // 是否对该阶段做了修改
  headers: Record<string, string>
  body: string
  error: string | null
  traceback: string
  // 请求阶段独有：mock 响应字段
  mock_status?: number | null
  mock_headers?: Record<string, string> | null
  mock_body?: string
  // 响应阶段独有：状态码
  status_code?: number
  // === 调试增强 ===
  /** ctx.log() 和 print() 的输出列表 */
  logs?: string[]
  /** set_var() 设置的中间变量字典 */
  variables?: Record<string, unknown>
}

/** 脚本测试结果 */
export interface ScriptTestResult {
  ok: boolean
  duration_ms: number
  error: string | null
  traceback: string | null
  request_phase: ScriptTestPhase | null
  response_phase: ScriptTestPhase | null
}

/** AI 聊天记录 */
export interface AiChat {
  id: number
  title: string
  flow_ids: number[]
  flow_context?: string
  created_at: string
  updated_at: string
}

/** AI 消息 */
export interface AiMessage {
  id: number
  chat_id: number
  role: string // user | assistant
  content: string
  created_at: string
}

/** AI 使用统计 */
export interface AiUsageStats {
  all_time: {
    requests: number
    input_tokens: number
    output_tokens: number
    total_cost: number
  }
  month: {
    requests: number
    input_tokens: number
    output_tokens: number
    total_cost: number
  }
  today: {
    requests: number
    input_tokens: number
    output_tokens: number
    total_cost: number
  }
  by_service: Array<{
    service: string
    requests: number
    input_tokens: number
    output_tokens: number
    total_cost: number
  }>
}

/** AI 服务状态 */
export interface AiServiceStatus {
  service: string
  name: string
  enabled: boolean
  has_api_key: boolean
  models: string[]
  default_model: string
  current_model: string
}

/** 设置 */
export interface Settings {
  // AI 服务配置
  ai_service?: string
  deepseek_api_key?: string
  deepseek_model?: string
  claude_api_key?: string
  claude_model?: string
  openai_api_key?: string
  openai_model?: string
  gemini_api_key?: string
  gemini_model?: string
  ollama_endpoint?: string
  ollama_model?: string
  // 其他设置
  inspector_tabs?: string[]
  data_path?: string
  trigger_capture_enabled?: boolean
  [key: string]: any
}

/** 重放参数覆盖 */
export interface ReplayOverride {
  method?: string
  url?: string
  host?: string
  port?: number
  scheme?: string
  headers?: Record<string, string>
  body?: string
  fuzz?: string // 'key=start..end' 批量重放
}

/** Repeat Advanced 单次结果 */
export interface RepeatResult {
  index: number
  ok: boolean
  status_code?: number
  size?: number
  duration_ms: number
  error?: string
  response_body?: string
  response_headers?: Record<string, string>
}

/** Replay with environment response */
export interface ReplayResponse {
  script_id: string
  environment_id: string
  results: Array<{
    flow_id: number
    success: boolean
    status_code?: number
    error?: string
  }>
}

/** Repeat Advanced 统计信息 */
export interface RepeatStats {
  success: number
  fail: number
  duration_min_ms: number
  duration_max_ms: number
  duration_avg_ms: number
  status_distribution: Record<string, number>
}

/** Repeat Advanced 响应 */
export interface RepeatResponse {
  count: number
  concurrency: number
  interval_ms: number
  results: RepeatResult[]
  stats: RepeatStats
}

/** TCP/UDP 原始抓包状态 */
export interface RawStatus {
  running: boolean
  is_admin: boolean
  pydivert_installed: boolean
  hint?: string
  filter?: string
  pid_filter?: number[]
  port_filter?: number[]
  msg?: string
  // 跨平台字段
  backend?: string  // 'windivert' | 'af_packet' | 'bpf' | 'none'
  supported?: boolean
}

/** P1 实时带宽监控统计 */
export interface RawStats {
  pps: number              // 包每秒
  mbps: number             // 带宽 Mbps
  total_packets: number    // 总包数
  total_bytes: number      // 总字节数
  dropped_packets: number  // 丢包数
  proto_distribution: Record<string, number>  // 协议分布
  tcp_count: number
  udp_count: number
  dns_count: number
  http_count: number
  https_count: number
  trend: Array<{ ts: number; pps: number; mbps: number }>  // 带宽趋势
  active_connections: ActiveConnection[]  // 活跃连接列表
  top_processes: Array<{ name: string; count: number; pkts: number; bytes: number }>  // Top 进程
  timestamp: number
}

/** 活跃连接信息 */
export interface ActiveConnection {
  proto: string
  local_ip: string
  local_port: number
  remote_ip: string
  remote_port: number
  pid: number | null
  proc_name: string
  last_seen: number
  pkt_count: number
  bytes: number
}

/** DNS 劫持状态 */
export interface DnsHijackStatus {
  running: boolean
  last_error: string
  rules: Record<string, string>
  default_ip: string
  stats: {
    total_packets: number
    hijacked_packets: number
    skipped_no_match: number
    errors: number
  }
  log: Array<{
    ts: string
    domain: string
    original_ips: string[]
    new_ip: string
    dns_server: string
  }>
  is_admin: boolean
  is_windows: boolean
  // 跨平台字段
  backend?: string  // 'windivert' | 'iptables+local_dns' | 'pf+local_dns' | 'none'
  supported?: boolean
  hint?: string
}

/** DoH 检测结果 */
export interface DoHDetectionResult {
  is_doh: boolean
  is_dot: boolean
  provider: string | null
  reason: string
  confidence: 'high' | 'medium' | 'low' | 'none'
}

/** DoH 检测批量结果 */
export interface DoHBatchDetectionResult {
  doh_count: number
  dot_count: number
  doh_flows: Array<DoHDetectionResult & { id?: number; url?: string; method?: string; sni?: string; host?: string }>
  dot_flows: Array<DoHDetectionResult & { id?: number; url?: string; method?: string; sni?: string; port?: number }>
  doh_providers: Record<string, number>
  total_checked: number
}

/** DoH 状态信息 */
export interface DoHStatus {
  doh_enabled: boolean
  dot_enabled: boolean
  known_providers: string[]
  known_dot_providers: string[]
  supported_doh_content_types: string[]
}

/** 搜索结果 */
export interface SearchResult {
  matches: Flow[]
  count: number
}

/** 站点地图方法统计 */
export interface SiteMapMethod {
  method: string
  count: number
}

/** 站点地图路径节点 */
export interface SiteMapNode {
  path: string
  request_count: number
  methods: SiteMapMethod[]
  children: SiteMapNode[]
}

/** 站点地图主机节点 */
export interface SiteMapHost {
  host: string
  request_count: number
  path_count: number
  children: SiteMapNode[]
}

/** 站点地图结果 */
export interface SiteMap {
  hosts: SiteMapHost[]
}

/** 单个 Cookie（聚合自请求 Cookie 头 / 响应 Set-Cookie 头） */
export interface CookieItem {
  name: string
  value: string
  domain: string
  path: string
  expires: string
  secure: boolean
  httponly: boolean
  samesite: string
  host: string
  /** 来源：set-cookie（服务端设置，带属性） / request-cookie（客户端发送） */
  source: 'set-cookie' | 'request-cookie'
  flow_id: number
  last_seen: string
}

/** 按 host 分组的 Cookie */
export interface CookieHostGroup {
  host: string
  count: number
  cookies: CookieItem[]
}

/** Cookie 管理器聚合结果 */
export interface CookiesResult {
  hosts: CookieHostGroup[]
  total_hosts: number
  total_cookies: number
}

/** 流量统计 */
export interface FlowStats {
  total: number
  by_host: { host: string; c: number; avg_ms: number }[]
  by_method: { method: string; c: number }[]
  by_status: { status_code: number; c: number }[]
  by_protocol: { protocol: string; c: number }[]
}

/** 多维聚合统计（CoolUI 仪表盘用） */
export interface FlowOverview {
  total: number
  total_bytes: number
  incoming_bytes: number
  outgoing_bytes: number
  success_count: number
  error_count: number
  avg_duration_ms: number
  by_protocol: { key: string; count: number }[]
  by_method: { key: string; count: number }[]
  by_status_range: { key: string; count: number }[]
  by_host: { key: string; count: number; bytes: number }[]
  by_process: { key: string; count: number; bytes: number }[]
  by_ip_region: { key: string; count: number }[]
}

/** Hex dump 结果 */
export interface HexResult {
  hex: string
  size: number
  field: string
  offset: number
  length: number
}

/** 专注模式（扩展：支持进程名） */
export interface FocusState {
  enabled: boolean
  pids: number[]
  hosts?: string[]
  process_names?: string[]
  include_children?: boolean
  methods?: string[]
  status_codes?: number[]
  content_types?: string[]
  protocols?: string[]
}

/** 日志条目 */
export interface LogEntry {
  id: number
  timestamp: string
  level: string
  category: string
  message: string
  detail: string
}

/** 日志统计数据 */
export interface LogStats {
  total: number
  by_level: { DEBUG: number; INFO: number; WARNING: number; ERROR: number }
  by_category: Record<string, number>
  recent_errors: Array<{
    id: number
    timestamp: string
    category: string
    message: string
    detail: string
  }>
}

// ============ axios 实例 ============

const client = axios.create({
  baseURL: '/api',
  timeout: 15000,
})

// AI 分析等长耗时接口的超时时间（3 分钟）
const AI_TIMEOUT = 180000

// 响应拦截器：解包 {code, data, msg}
// 特殊处理 WinDivert 风险提示：检测到 need_ack=true 时弹全局对话框，
// 用户确认 → 调 ack API 持久化 → 重试原请求；用户取消 → 拒绝原请求
client.interceptors.response.use(
  (response: AxiosResponse<ApiResult>) => {
    const res = response.data
    if (res && typeof res === 'object' && 'code' in res) {
      if (res.code === 0) {
        return res.data as any
      }
      // code != 0：检查是否为 WinDivert 需要确认（HTTP 200 但业务码 -1 + need_ack）
      if (res.need_ack === true) {
        return _handleWindivertAck(response, res.msg || i18n.global.t('api.needWindivertAck'))
      }
      return Promise.reject(new Error(res.msg || i18n.global.t('api.requestFailed')))
    }
    return res as any
  },
  (error) => {
    // HTTP 4xx/5xx 错误：检查是否为 WinDivert 需要确认（403 + need_ack=true）
    const errData = error?.response?.data
    if (errData && errData.need_ack === true) {
      return _handleWindivertAck(error.response, errData.msg || '需要确认 WinDivert 风险提示')
    }
    const msg = errData?.msg
    if (msg) {
      return Promise.reject(new Error(msg))
    }
    return Promise.reject(error)
  }
)

/**
 * 处理 WinDivert 风险提示：弹全局对话框等用户响应，确认后调 ack API 持久化并重试原请求。
 *
 * @param resp 原始 axios response（用于取 config 重试请求）
 * @param msg 后端返回的提示文本
 */
async function _handleWindivertAck(resp: AxiosResponse, msg: string): Promise<any> {
  // 拉取后端最新的风险说明文本（含 brief 摘要）
  let fullMsg = msg
  let briefMsg = ''
  try {
    const w = await client.get('/system/windivert-warning')
    if (w && typeof w === 'object') {
      const wd = w as any
      if (wd.message) fullMsg = wd.message
      if (wd.brief) briefMsg = wd.brief
    }
  } catch {
    /* 拉取失败时用后端在错误响应里给的 msg 兜底 */
  }
  // 弹全局对话框，等待用户响应
  const accepted = await waitForWindivertAck(fullMsg, briefMsg)
  if (!accepted) {
    return Promise.reject(new Error(i18n.global.t('api.userCancelledWindivertAck')))
  }
  // 用户确认：调 ack API 持久化（永久不再提示）
  try {
    await client.post('/system/windivert-warning/ack')
  } catch {
    /* ack 失败不阻塞重试，下次还会再弹 */
  }
  // 重试原请求
  if (resp?.config) {
    return client.request(resp.config)
  }
  return Promise.reject(new Error(i18n.global.t('api.cannotRetryNoConfig')))
}

// GET 请求去重：相同 url+params 的并发请求合并为一次，避免重复网络往返
// 修复审计 7.2：原代码若请求因网络问题挂起（既不 resolve 也不 reject），
// Map 中的 entry 永不释放，相同 url+params 的后续请求会永远返回这个挂起的 Promise，
// 导致功能永久失效。增加 30s 超时兜底，超时后从 Map 移除并 reject。
const pendingGets = new Map<string, Promise<any>>()
const PENDING_GET_TIMEOUT_MS = 30000  // 30s 超时兜底

// 保存 timer 引用，用于主动清理
const pendingGetTimers = new Map<string, ReturnType<typeof setTimeout>>()

function getDedupKey(url: string, params?: any): string {
  try {
    return url + '::' + JSON.stringify(params || {})
  } catch {
    return url
  }
}

// 由于拦截器已解包，这里把 AxiosPromise 转为 Promise<T>
async function get<T = any>(url: string, params?: any): Promise<T> {
  const key = getDedupKey(url, params)
  const existing = pendingGets.get(key)
  if (existing) {
    return existing as Promise<T>
  }
  const promise = client.get(url, { params }) as unknown as Promise<T>
  pendingGets.set(key, promise)
  // 正常完成时清理（Map 和 timer 都清理）
  promise.then(
    () => {
      pendingGets.delete(key)
      const timer = pendingGetTimers.get(key)
      if (timer) {
        clearTimeout(timer)
        pendingGetTimers.delete(key)
      }
    },
    () => {
      pendingGets.delete(key)
      const timer = pendingGetTimers.get(key)
      if (timer) {
        clearTimeout(timer)
        pendingGetTimers.delete(key)
      }
    }
  )
  // 超时兜底：若 promise 长时间未 settle，强制清理 Map 和 timer 避免永久挂起
  // 注意：此 timer 不会取消 promise 本身（axios 内部仍可能最终 settle），
  // 仅清理 Map 让后续相同 url 的请求可以重新发起
  const timer = setTimeout(() => {
    if (pendingGets.get(key) === promise) {
      pendingGets.delete(key)
      pendingGetTimers.delete(key)
    }
  }, PENDING_GET_TIMEOUT_MS)
  pendingGetTimers.set(key, timer)
  return promise
}

// 清理所有 pending GET 请求和 timer（供组件 unmount 时调用，防止内存泄漏）
export function clearPendingGets(): void {
  // 清理所有 timer
  for (const timer of pendingGetTimers.values()) {
    clearTimeout(timer)
  }
  pendingGetTimers.clear()
  pendingGets.clear()
}

async function post<T = any>(url: string, body?: any, timeout?: number): Promise<T> {
  return client.post(url, body, timeout ? { timeout } : undefined) as unknown as Promise<T>
}
async function put<T = any>(url: string, body?: any): Promise<T> {
  return client.put(url, body) as unknown as Promise<T>
}
async function patch<T = any>(url: string, body?: any): Promise<T> {
  return client.patch(url, body) as unknown as Promise<T>
}
async function del<T = any>(url: string, params?: any): Promise<T> {
  return client.delete(url, { params }) as unknown as Promise<T>
}

// ============ API 集合 ============

export const api = {
  // 状态
  getStatus: () => get<Status>('/status'),
  captureStart: () => post('/capture/start'),
  captureStop: () => post('/capture/stop'),
  captureClear: () => post('/capture/clear'),
  // 触发式捕获
  getTrigger: () => get<any>('/capture/trigger'),
  setTrigger: (body: { dsl?: string; conditions?: any[] }) => put<any>('/capture/trigger', body),
  resetTrigger: () => post<any>('/capture/trigger/reset'),

  // 会话与流量
  getSessions: () => get<any[]>('/sessions'),
  getFlows: (sessionId: number, params?: any) =>
    get<FlowsResult>(`/sessions/${sessionId}/flows`, params),
  getFlow: (id: number) => get<Flow>(`/flows/${id}`),
  patchFlow: (id: number, body: any) => patch<Flow>(`/flows/${id}`, body),
  releaseFlow: (id: number, body: { action: string }) => post(`/flows/${id}/release`, body),
  // 协议深度解析（DNS/TLS/HTTP/NTP 等 TCP/UDP 流量）
  decodeFlow: (id: number, field: string = 'raw_data') =>
    get<{ protocol: string; summary?: string; error?: string; fields: { label: string; value: string; color?: string }[] }>(`/flows/${id}/decode`, { field }),

  // 重放
  replayFlow: (id: number) => post(`/flows/${id}/replay`),
  replayFlowOverride: (id: number, override: ReplayOverride) =>
    post(`/flows/${id}/replay`, override),
  // Repeat Advanced：批量并发重放 + 统计
  repeatFlow: (id: number, count: number, concurrency: number = 1, intervalMs: number = 0, override?: ReplayOverride) =>
    post<RepeatResponse>(`/flows/${id}/repeat`, {
      count, concurrency, interval_ms: intervalMs, override: override || null,
    }),

  // 技术栈识别
  getTechFingerprint: (id: number) =>
    get<{ items: TechFingerprint[]; count: number }>(`/flows/${id}/tech-fingerprint`),

  // 透明代理模式
  transparentProxyStatus: () => get<TransparentProxyStatus>('/transparent-proxy/status'),
  transparentProxyStart: () => post<{ running: boolean; msg: string }>('/transparent-proxy/start'),
  transparentProxyStop: () => post<{ running: boolean; msg: string }>('/transparent-proxy/stop'),

  // 断点
  getBpStatus: () => get<BpStatus>('/breakpoint/status'),
  setBpRequest: (body: { enabled: boolean }) => post('/breakpoint/request', body),
  setBpResponse: (body: { enabled: boolean }) => post('/breakpoint/response', body),

  // 自动修改
  getRules: () => get<AutoReplyRule[]>('/auto-reply/rules'),
  createRule: (body: AutoReplyRule) => post<AutoReplyRule>('/auto-reply/rules', body),
  updateRule: (id: string, body: AutoReplyRule) => put(`/auto-reply/rules/${id}`, body),
  deleteRule: (id: string) => del(`/auto-reply/rules/${id}`),
  batchUpdateRules: (ids: string[], enabled: boolean) =>
    post('/auto-reply/rules/batch-update', { ids, enabled }),
  batchDeleteRules: (ids: string[]) =>
    post('/auto-reply/rules/batch-delete', { ids }),
  // 规则导出/导入（兼容 EzReply 格式）
  exportRules: async (): Promise<Blob> => {
    const res = await client.get('/auto-reply/rules/export', {
      responseType: 'blob',
      transformResponse: [(data: any) => data],
    })
    return res as unknown as Blob
  },
  importRules: (body: { rules: any[]; mode?: string }) =>
    post<{ imported: number; mode: string }>('/auto-reply/rules/import', body),
  // 测试 Python 脚本（不发起真实请求，用 mock 数据走 worker 子进程）
  // mode: "request" 只请求 / "response" 只响应 / "both" 请求+响应
  testScript: (body: {
    script: string
    mock_request: ScriptMockRequest
    mock_response?: ScriptMockResponse | null
    mode?: 'request' | 'response' | 'both'
  }) => post<ScriptTestResult>('/auto-reply/test-script', body, 15000),
  // ========== P0 功能增强 ==========
  // 规则匹配预览
  previewMatch: (body: {
    pattern: string
    match_mode: string
    method_filter?: string
    status_filter?: string
    pid_filter?: string
    process_filter?: string
    limit?: number
  }) => post<{ total: number; samples: Array<{ flow_id: number; method: string; host: string; path: string; url: string }> }>('/auto-reply/rules/preview-match', body),
  // 规则分组管理
  getRuleGroups: () => get<Array<{ id: number; name: string; enabled: boolean; sort_order: number; rule_count?: number }>>('/auto-reply/groups'),
  createRuleGroup: (body: { name: string; enabled?: boolean }) =>
    post<{ id: number; name: string; enabled: boolean }>('/auto-reply/groups', body),
  updateRuleGroup: (id: number, body: { name?: string; enabled?: boolean; sort_order?: number }) =>
    put(`/auto-reply/groups/${id}`, body),
  deleteRuleGroup: (id: number) => del(`/auto-reply/groups/${id}`),
  updateRuleGroupMembership: (ruleId: string, groupId: number | null) =>
    put(`/auto-reply/rules/${ruleId}/group`, { group_id: groupId }),
  updateRuleTags: (ruleId: string, tags: string) =>
    put(`/auto-reply/rules/${ruleId}/tags`, { tags }),
  // 规则命中统计
  getRuleHitStats: () => get<{
    total_hits: number
    leaderboard: Array<{ rule_id: string; pattern: string; action: string; note: string; hit_count: number; last_hit_at: string }>
    recent_hits: Array<{ id: string; pattern: string; action: string; hit_count: number; last_hit_at: string; last_hit_flow_id: number | null }>
  }>('/auto-reply/stats/hits'),
  clearRuleHitStats: () => post('/auto-reply/stats/hits/clear'),
  // 实时命中追踪（SSE 流 + 轮询接口）
  getRealtimeHitStats: () => get<{
    total_hits: number
    heatmap: Array<{ rule_id: string; rule_name: string; hit_count: number; avg_ms: number; min_ms: number; max_ms: number }>
    timeline: Array<{ ts: string; rule_id: string; rule_name: string; duration_ms: number; flow_id: number | null }>
    leaderboard: Array<{ rule_id: string; rule_name: string; hit_count: number; avg_ms: number; last_hit_at: string }>
  }>('/auto-reply/stats/hits/realtime'),
  // 创建 SSE EventSource 用于实时命中追踪流
  createHitStatsStream: (): EventSource => {
    return new EventSource('/api/auto-reply/stats/hits/stream')
  },
  // 流量删除
  deleteFlow: (id: number) => del(`/flows/${id}`),
  batchDeleteFlows: (ids: number[]) => post('/flows/batch-delete', { ids }),
  batchReleaseFlows: (ids: number[], action: string = 'release') =>
    post('/flows/batch-release', { ids, action }),
  // 流量标记
  updateFlowTag: (id: number, tag: string, color?: string) =>
    patch<Flow>(`/flows/${id}`, { tags: tag, tag_color: color }),
  // 全局分析：跨会话查询所有流量 + 清理
  getAllFlows: (params?: any) => get('/flows/all', params),
  // 全量分组统计（不分页，用于统计图显示所有数据比例）
  getFlowsStats: (groupBy: string = 'host', host?: string, process?: string) =>
    get('/flows/stats', { group_by: groupBy, host, process }),
  // 2D 热力图聚合（时间分桶 × 维度分组）
  getFlowsHeatmap: (groupBy: string = 'host', bucketSeconds: number = 60,
                    maxBuckets: number = 120, topN: number = 20,
                    host?: string, process?: string) =>
    get<any>('/flows/heatmap', {
      group_by: groupBy, bucket_seconds: bucketSeconds,
      max_buckets: maxBuckets, top_n: topN, host, process,
    }),
  // 网络拓扑数据（process → IP → host）
  getFlowsTopology: (host?: string, process?: string, maxNodes: number = 100) =>
    get<{ nodes: any[]; edges: any[] }>('/flows/topology', { host, process, max_nodes: maxNodes }),
  // 多维聚合统计（CoolUI 仪表盘用，一次返回所有维度）
  getFlowsOverview: () => get<FlowOverview>('/flows/overview'),
  // ========== P1 高级分析 API ==========
  // 响应时间深度分析：延迟分布直方图 + P50/P75/P90/P95/P99
  getLatencyStats: (params?: { host?: string; process?: string; limit?: number }) =>
    get<any>('/flows/latency-stats', params),
  // 多维度交叉分析：Host x Status Code、Content-Type x Size
  getCrossAnalysis: (params?: { host?: string; process?: string; limit?: number }) =>
    get<any>('/flows/cross-analysis', params),
  // 智能异常检测：高延迟、错误率、流量突增/突降
  getAnomalies: (params?: { host?: string; process?: string; limit?: number }) =>
    get<any>('/flows/anomalies', params),
  // 报表导出：JSON/CSV/HTML
  exportReport: async (format: 'json' | 'csv' | 'html' = 'json', params?: { host?: string; process?: string }): Promise<Blob> => {
    const res = await client.get('/flows/export-report', {
      params: { format, ...params },
      responseType: 'blob',
      transformResponse: [(data: any) => data],
    })
    return res as unknown as Blob
  },
  clearAllFlows: (mode: string = 'all', beforeId?: number) =>
    post('/flows/clear', { mode, before_id: beforeId }),

  // 系统控制
  restartService: () => post('/system/restart'),
  restartAsAdmin: () => post('/system/restart-as-admin'),
  quitService: () => post('/system/quit'),
  clearProxy: () => post('/system/clear-proxy'),
  enableProxy: () => post('/system/enable-proxy'),
  // 查询后端实际使用的端口（随机端口模式 / 端口冲突自动切换后，实际端口可能与设置不同）
  getPortInfo: () => get<PortInfo>('/system/ports'),
  // 管理员重启（GUI 确认流程）
  getPendingAdminActions: () => get<{ items: any[] }>('/system/pending-admin-actions'),
  respondAdminRequest: (rid: string, response: 'accept' | 'reject') =>
    post(`/system/admin-request/${rid}/respond`, { response }),

  // WinDivert 风险提示
  getWindivertWarning: () => get<WindivertWarningStatus>('/system/windivert-warning'),
  ackWindivertWarning: () => post<{ ack: boolean }>('/system/windivert-warning/ack'),

  // 平台能力查询（跨平台功能支持情况，用于前端显示/隐藏功能按钮）
  platformCapabilities: () => get<{
    platform: string
    is_windows: boolean
    is_linux: boolean
    is_macos: boolean
    is_unix: boolean
    is_admin: boolean
    capabilities: {
      raw_capture: { supported: boolean; backend: string; needs_admin: boolean; admin_hint: string }
      transparent_proxy: { supported: boolean; backend: string; needs_admin: boolean; admin_hint: string }
      dns_hijack: { supported: boolean; backend: string; needs_admin: boolean; admin_hint: string }
      system_proxy: { supported: boolean; backend: string; hint: string }
      windivert_warning: { supported: boolean; needed: boolean; ack: boolean }
      admin_elevation: { supported: boolean; backend: string; hint: string }
    }
  }>('/system/platform-capabilities'),
  // 兼容旧命名
  getPlatformCapabilities: () => get<{
    platform: string
    is_windows: boolean
    is_linux: boolean
    is_macos: boolean
    is_unix: boolean
    is_admin: boolean
    capabilities: {
      raw_capture: { supported: boolean; backend: string; needs_admin: boolean; admin_hint: string }
      transparent_proxy: { supported: boolean; backend: string; needs_admin: boolean; admin_hint: string }
      dns_hijack: { supported: boolean; backend: string; needs_admin: boolean; admin_hint: string }
      system_proxy: { supported: boolean; backend: string; hint: string }
      windivert_warning: { supported: boolean; needed: boolean; ack: boolean }
      admin_elevation: { supported: boolean; backend: string; hint: string }
    }
  }>('/system/platform-capabilities'),

  // AI
  aiAnalyze: (body: { flow_ids: number[] }) =>
    post<{ chat_id: number; result: string; title: string }>('/ai/analyze', body, AI_TIMEOUT),
  aiChat: (body: { chat_id: number; message: string; flow_ids?: number[] }) =>
    post<{ result: string }>('/ai/chat', body, AI_TIMEOUT),
  aiChatStream: (
    body: { chat_id: number; message: string; flow_ids?: number[] },
    onChunk: (chunk: string) => void
  ): Promise<string> => {
    return new Promise(async (resolve, reject) => {
      try {
        const resp = await fetch('/api/ai/chat/stream', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        })
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({ msg: resp.statusText }))
          reject(new Error(err.msg || 'Stream failed'))
          return
        }
        const reader = resp.body?.getReader()
        const decoder = new TextDecoder()
        let result = ''
        if (!reader) {
          reject(new Error('No response body'))
          return
        }
        while (true) {
          const { done, value } = await reader.read()
          if (done) break
          const text = decoder.decode(value, { stream: true })
          // SSE format: data: {...} or data: content
          for (const line of text.split('\n')) {
            if (line.startsWith('data: ')) {
              const data = line.slice(6)
              if (data === '[DONE]') {
                resolve(result)
                return
              }
              try {
                const parsed = JSON.parse(data)
                if (parsed.content) {
                  result += parsed.content
                  onChunk(parsed.content)
                }
                if (parsed.done) {
                  resolve(result)
                  return
                }
              } catch {
                // 非 JSON，当纯文本处理
                result += data
                onChunk(data)
              }
            }
          }
        }
        resolve(result)
      } catch (e) {
        reject(e)
      }
    })
  },
  aiListChats: () => get<AiChat[]>('/ai/chats'),
  aiGetChat: (chatId: number) => get<{ chat: AiChat; messages: AiMessage[] }>(`/ai/chats/${chatId}`),
  aiUpdateTitle: (chatId: number, title: string) =>
    put(`/ai/chats/${chatId}/title`, { title }),
  aiDeleteChat: (chatId: number) => del(`/ai/chats/${chatId}`),

  // AI 服务管理
  aiGetServices: () => get<AiServiceStatus[]>('/ai/services'),
  aiGetUsage: () => get<AiUsageStats>('/ai/usage'),
  aiUpdateService: (service: string) => put<{ service: string }>('/ai/service', { service }),
  aiUpdateModel: (service: string, model: string) =>
    put<{ service: string; model: string }>('/ai/model', { service, model }),
  aiGetModels: (service: string) =>
    get<{ service: string; name: string; models: string[]; default_model: string; pricing: Record<string, number> }>(`/ai/models/${service}`),

  // 导出
  exportSession: (sessionId: number, format: string) =>
    post<{ url?: string; path?: string; data?: any }>(`/export/${sessionId}`, { format }),

  // 导入流量（JSON 或 HAR 格式）
  importFlows: (body: { format: string; content: string; session_name?: string }) =>
    post<{ session_id: number; session_name: string; imported: number; total: number }>('/import', body),

  // 设置
  getSettings: () => get<Settings>('/settings'),
  saveSettings: (body: Settings) => put<Settings>('/settings', body),

  // 性能配置
  getPerformanceConfig: () => get<{
    max_body_size: number
    decompress_threshold: number
    ssl_context_cache_size: number
    max_connections: number
  }>('/settings/performance'),
  updatePerformanceConfig: (body: {
    max_body_size?: number
    decompress_threshold?: number
    ssl_context_cache_size?: number
    max_connections?: number
  }) => put('/settings/performance', body),
  applyPerformancePreset: (preset: 'light' | 'standard' | 'high_performance') =>
    post('/settings/performance/preset', { preset }),

  // SSL/TLS 配置
  getSslConfig: () => get<{
    min_tls_version: string
    cipher_suites: string
    sni_spoofing: boolean
  }>('/settings/ssl'),
  updateSslConfig: (body: {
    min_tls_version?: string
    cipher_suites?: string
    sni_spoofing?: boolean
  }) => put('/settings/ssl', body),

  // 进程
  getProcesses: () => get<ProcessInfo[]>('/processes'),
  ignoreProcess: (body: { pid: number | null; name: string }) => post('/processes/ignore', body),
  unignoreProcess: (rowId: number) => del(`/processes/ignore/${rowId}`),
  getIgnoredProcesses: () => get<IgnoredProcess[]>('/processes/ignored'),
  // 忽略 host 通配符（支持 * ? 通配符，如 *.example.com）
  ignoreHost: (host: string) => post('/processes/ignore-host', { host }),
  unignoreHost: (id: number) => del(`/processes/ignore-host/${id}`),
  getIgnoredHosts: () => get<{ id: number; host_pattern: string; created_at: string }[]>('/processes/ignored-hosts'),

  // 会话管理（name/color）
  getSession: (id: number) => get<any>(`/sessions/${id}`),
  updateSession: (id: number, name?: string, color?: string) =>
    patch(`/sessions/${id}`, { ...(name !== undefined ? { name } : {}), ...(color !== undefined ? { color } : {}) }),

  // 证书
  installCert: () => post('/cert/install'),
  getCertStatus: () => get<{ installed: boolean }>('/cert/status'),
  getCertDetails: () => get<{
    root_cert_path: string
    installed: boolean
    thumbprint: string
    issued_date: string | null
    expiry_date: string | null
    expiry_countdown: number | null
    serial_number: string | null
    leaf_cert_count: number
    cert_expiry_alert: boolean
  }>('/cert/details'),
  setCertExpiryAlert: (enabled: boolean) => put('/cert/expiry-alert', { enabled }),
  regenerateCert: () => post('/cert/regenerate'),
  // 手机抓包配置信息（本机 IP、代理端口、证书下载 URL）
  mobileSetup: () => get<{ lan_ip: string; proxy_host: string; proxy_port: number; api_port: number; cert_download_url: string }>('/mobile/setup'),

  // 弱网模拟（Throttle）
  getThrottle: () => get<{ enabled: boolean; latency_ms: number; bps_kbps: number; drop_pct: number }>('/throttle'),
  setThrottle: (body: { enabled?: boolean; latency_ms?: number; bps_kbps?: number; drop_pct?: number }) =>
    put<{ enabled: boolean; latency_ms: number; bps_kbps: number; drop_pct: number }>('/throttle', body),

  // 专注模式（支持按进程名 + 子进程 + host 通配符）
  getFocus: () => get<FocusState>('/focus'),
  setFocus: (body: { enabled: boolean; pids?: number[]; process_names?: string[]; include_children?: boolean; hosts?: string[]; methods?: string[]; status_codes?: number[]; content_types?: string[] }) =>
    post('/focus', body),

  // 日志
  logList: (params?: { level?: string; category?: string; keyword?: string; limit?: number; offset?: number }) =>
    get<{ entries: LogEntry[]; total: number }>('/logs', params),
  logClear: () => del('/logs'),
  // 导出日志：用 axios 请求（走拦截器，支持非回环认证），返回 blob
  logExport: async (params?: { level?: string; category?: string; keyword?: string }): Promise<Blob> => {
    const res = await client.get('/logs/export', {
      params: {
        level: params?.level || undefined,
        category: params?.category || undefined,
        keyword: params?.keyword || undefined,
      },
      responseType: 'blob',
      // 绕过响应拦截器的 JSON 解包：导出返回的是 text/plain，不是 {code,data} 结构
      transformResponse: [(data: any) => data],
    })
    return res as unknown as Blob
  },

  // 日志统计
  logStats: () => get<LogStats>('/logs/stats'),

  // 创建 SSE EventSource 用于实时日志流
  createLogStream: (): EventSource => {
    return new EventSource('/api/logs/stream')
  },

  // 路径操作
  openPath: (path: string) => post('/settings/open-path', { path }),
  openSettingsFile: () => post('/settings/open-file'),
  listDirs: (path?: string) => get<{ current: string; dirs: string[]; parent: string }>('/settings/list-dirs', path ? { path } : {}),

  // 发包（Composer）
  sendRequest: (body: { method: string; url: string; headers?: any; body?: string; timeout?: number }) =>
    post<any>('/send', body),

  // TCP/UDP 原始抓包（WinDivert）
  rawStatus: () => get<RawStatus>('/raw/status'),
  rawStart: (body: { pid_filter?: number[]; port_filter?: number[]; filter_str?: string }) =>
    post('/raw/start', body),
  rawStop: () => post('/raw/stop'),
  // P1 实时监控统计
  rawStats: () => get<RawStats>('/raw/stats'),
  rawStatsReset: () => post('/raw/stats/reset'),

  // 搜索与统计
  searchFlows: (body: {
    session_id: number
    body_regex?: string
    binary_hex?: string
    limit?: number
    header_regex?: string
    method?: string
    status_code?: number
    pid?: number
    process_name?: string
    host?: string
    status_min?: number
    status_max?: number
  }) => post<SearchResult>('/flows/search', body),
  getStats: (sessionId: number) => get<FlowStats>(`/sessions/${sessionId}/stats`),
  getFlowHex: (flowId: number, params?: { offset?: number; length?: number; field?: string }) =>
    get<HexResult>(`/flows/${flowId}/hex`, params),

  // 站点地图（Burp Suite 风格的 URL 树形结构）
  getSiteMap: () => get<SiteMap>('/site-map'),

  // 系统维护
  dbStats: () => get<{ path: string; size_mb: number; flows: number; sessions: number; rules: number; ai_chats: number; ai_messages: number }>('/system/db-stats'),
  cleanupDb: () => post<{ before_mb: number; after_mb: number; reclaimed_mb: number }>('/system/cleanup-db'),
  clearData: (type: string) => post<{ cleared: boolean; type: string; deleted?: number; flows?: number; sessions?: number; rules?: number; ai_chats?: number }>('/system/clear-data', { type }),

  // 代理工具（No Caching / Force CORS / Block List / Allow List / Map Local / Map Remote）
  proxyToolsStatus: () => get<{
    no_caching: boolean; force_cors: boolean;
    block_list_enabled: boolean; block_list: Array<{ pattern: string; mode: string } | string>;
    allow_list_enabled: boolean; allow_list: Array<{ pattern: string; mode: string } | string>;
    map_local_enabled: boolean; map_local_rules: Array<{ pattern: string; mode: string; file_path: string; status?: number; content_type?: string }>;
    map_remote_enabled: boolean; map_remote_rules: Array<{ pattern: string; mode: string; target_url: string }>;
    mirror_enabled: boolean; mirror_rules: Array<{ pattern: string; mode: string; save_dir: string }>;
  }>('/proxy-tools'),
  proxyToolsUpdate: (body: Record<string, any>) => put('/proxy-tools', body),
  blockListAdd: (pattern: string, mode: string = 'wildcard') => post('/proxy-tools/block-list', { pattern, mode }),
  blockListDelete: (index: number) => del(`/proxy-tools/block-list/${index}`),
  allowListAdd: (pattern: string, mode: string = 'wildcard') => post('/proxy-tools/allow-list', { pattern, mode }),
  allowListDelete: (index: number) => del(`/proxy-tools/allow-list/${index}`),
  mapLocalAdd: (pattern: string, mode: string, filePath: string, status?: number, contentType?: string, headers?: Record<string, string>) =>
    post('/proxy-tools/map-local', { pattern, mode, file_path: filePath, status, content_type: contentType, headers }),
  mapLocalDelete: (index: number) => del(`/proxy-tools/map-local/${index}`),
  mapRemoteAdd: (pattern: string, mode: string, targetUrl: string, headers?: Record<string, string>) =>
    post('/proxy-tools/map-remote', { pattern, mode, target_url: targetUrl, headers }),
  mapRemoteDelete: (index: number) => del(`/proxy-tools/map-remote/${index}`),
  mirrorAdd: (pattern: string, mode: string, saveDir: string) =>
    post('/proxy-tools/mirror', { pattern, mode, save_dir: saveDir }),
  mirrorDelete: (index: number) => del(`/proxy-tools/mirror/${index}`),
  // 规则命中统计
  proxyToolsStats: () => get<Record<string, Record<string, { hit_count: number; last_hits: Array<{ ts: number; url: string; resolved_path: string }> }>>>('/proxy-tools/stats'),
  proxyToolsStatsClear: (body?: { rule_type?: string; rule_index?: number }) => post('/proxy-tools/stats/clear', body),
  // 规则导入/导出
  proxyToolsExport: async (): Promise<Blob> => {
    const res = await client.get('/proxy-tools/export', {
      responseType: 'blob',
      transformResponse: [(data: any) => data],
    })
    return res as unknown as Blob
  },
  proxyToolsImport: (body: { rules: any; mode?: string }) =>
    post<{ imported: number; skipped: number; conflicts: any[]; details: Record<string, number> }>('/proxy-tools/import', body),
  proxyToolsValidateImport: (body: { rules: any }) =>
    get<{ valid: boolean; conflicts: any[]; warnings: any[] }>('/proxy-tools/validate-import', body),

  // Clash/Mihomo 集成
  clashStatus: () => get<any>('/clash/status'),
  // 轻量接口：只返回 clash_enabled（侧边栏入口可见性），不探测 Mihomo
  clashEnabledQuick: () => get<{ enabled: boolean }>('/clash/enabled'),
  // 测试 Mihomo 连接：强制探测可达性（不受 integrated 状态影响）
  clashTest: () => get<{ reachable: boolean; version?: string; mixed_port?: number; error?: string | null }>('/clash/test'),
  clashEnable: (body?: { api_url?: string; secret?: string; mixed_port?: number }) =>
    put<any>('/clash/enable', body || {}),
  clashDisable: () => put<any>('/clash/disable'),
  clashConfig: (body: { api_url?: string; secret?: string; mixed_port?: number }) =>
    put<any>('/clash/config', body),
  clashTutorial: () => get<{ content: string; path: string }>('/clash/tutorial'),
  clashProxies: () => get<any>('/clash/proxies'),
  clashProxy: (name: string) => get<any>(`/clash/proxies/${encodeURIComponent(name)}`),
  clashSelectProxy: (group: string, name: string) =>
    put(`/clash/proxies/${encodeURIComponent(group)}`, { name }),
  clashProxyDelay: (name: string, url?: string, timeout?: number) =>
    get<any>(`/clash/proxies/${encodeURIComponent(name)}/delay`,
      { url: url || 'https://www.gstatic.com/generate_204', timeout: timeout || 5000 }),
  clashGroupDelay: (group: string, url?: string, timeout?: number) =>
    get<any>(`/clash/group/${encodeURIComponent(group)}/delay`,
      { url: url || 'https://www.gstatic.com/generate_204', timeout: timeout || 5000 }),
  clashProviders: () => get<any>('/clash/providers'),
  clashProvider: (name: string) => get<any>(`/clash/providers/${encodeURIComponent(name)}`),
  clashUpdateProvider: (name: string) => put(`/clash/providers/${encodeURIComponent(name)}`),
  clashProviderHealthcheck: (name: string) =>
    get(`/clash/providers/${encodeURIComponent(name)}/healthcheck`),
  clashConfigs: () => get<any>('/clash/configs'),
  clashPatchConfigs: (body: any) => patch('/clash/configs', body),
  clashReload: (force?: boolean) => put('/clash/reload', { force: !!force }),
  clashRules: () => get<any>('/clash/rules'),
  clashRuleProviders: () => get<any>('/clash/rule-providers'),
  clashUpdateRuleProvider: (name: string) => put(`/clash/rule-providers/${encodeURIComponent(name)}`),
  clashConnections: () => get<any>('/clash/connections'),
  clashCloseAllConnections: () => del('/clash/connections'),
  clashCloseConnection: (id: string) => del(`/clash/connections/${encodeURIComponent(id)}`),
  clashDnsQuery: (name: string, qtype?: string) =>
    get<any>('/clash/dns/query', { name, type: qtype || 'A' }),
  clashFlushDns: () => post('/clash/dns/flush'),
  clashFlushFakeip: () => post('/clash/fakeip/flush'),

  // DNS 劫持
  dnsHijackStatus: () => get<DnsHijackStatus>('/dns-hijack/status'),
  dnsHijackStart: (body: { rules: Record<string, string>; default_ip?: string }) =>
    post<{ running: boolean }>('/dns-hijack/start', body),
  dnsHijackStop: () => post<{ running: boolean }>('/dns-hijack/stop'),
  dnsHijackSetRules: (body: { rules: Record<string, string>; default_ip?: string }) =>
    put<DnsHijackStatus>('/dns-hijack/rules', body),
  dnsHijackClearLog: () => post<{ cleared: boolean }>('/dns-hijack/clear-log'),

  // DNS 规则分组管理
  getDnsGroups: () => get<Array<{
    id: number
    name: string
    priority: number
    enabled: boolean
    rule_count: number
  }>>('/dns-hijack/groups'),
  createDnsGroup: (body: { name: string; priority?: number; enabled?: boolean }) =>
    post<{ id: number; name: string; priority: number; enabled: boolean; rule_count: number }>('/dns-hijack/groups', body),
  updateDnsGroup: (id: number, body: { name?: string; priority?: number; enabled?: boolean }) =>
    put<any>('/dns-hijack/groups/' + id, body),
  deleteDnsGroup: (id: number) => del<{ deleted: boolean }>('/dns-hijack/groups/' + id),
  reorderDnsGroups: (groupIds: number[]) =>
    post<any>('/dns-hijack/groups/reorder', { group_ids: groupIds }),
  getDnsGroupRules: (groupId: number) =>
    get<Array<{
      id: number
      group_id: number
      pattern: string
      mode: string
      action: string
      redirect_to: string | null
    }>>('/dns-hijack/groups/' + groupId + '/rules'),
  createDnsRule: (groupId: number, body: {
    pattern: string
    mode?: string
    action?: string
    redirect_to?: string
  }) => post<any>('/dns-hijack/groups/' + groupId + '/rules', body),
  updateDnsRule: (ruleId: number, body: {
    pattern?: string
    mode?: string
    action?: string
    redirect_to?: string
  }) => put<any>('/dns-hijack/rules/' + ruleId, body),
  deleteDnsRule: (ruleId: number) => del<{ deleted: boolean }>('/dns-hijack/rules/' + ruleId),
  getAllDnsRules: () =>
    get<Array<{
      id: number
      group_id: number
      group_name?: string
      group_priority?: number
      pattern: string
      mode: string
      action: string
      redirect_to: string | null
    }>>('/dns-hijack/rules'),
  applyDnsGroupRules: () =>
    post<{ applied: boolean; rule_count: number }>('/dns-hijack/apply'),

  // DoH 检测
  dohStatus: () => get<DoHStatus>('/dns-hijack/doh-status'),
  dohDetect: (flows: any[]) => post<DoHBatchDetectionResult>('/dns-hijack/doh-detect', { flows }),
  dohDetectSingle: (flow: any) => post<DoHDetectionResult>('/dns-hijack/doh-detect-single', flow),

  // Cookie 管理器（集中查看/清理各 host 的 Cookie）
  getCookies: (host?: string) => get<CookiesResult>('/cookies', host ? { host } : {}),
  deleteHostCookies: (host: string) => del<{ host: string; flows_updated: number; cleared: boolean }>(`/cookies/${encodeURIComponent(host)}`),
  deleteAllCookies: () => del<{ flows_updated: number; cleared: boolean }>('/cookies'),

  // ========== 延迟规则增强 ==========
  // 从流量生成延迟规则
  createDelayRuleFromFlow: (flowId: number, phase: 'request' | 'response', delayMs?: number) =>
    post<DelayRule>('/delay-rules/from-flow', { flow_id: flowId, phase, delay_ms: delayMs }),
  // 延迟规则命中日志
  getDelayRuleHits: (limit?: number) => get<DelayHitLog[]>('/delay-rules/hits', { limit }),
  clearDelayRuleHits: () => del('/delay-rules/hits'),
  // 延迟分布热力图数据
  getDelayHeatmap: (params?: { bucket_seconds?: number; top_n?: number }) =>
    get<DelayHeatmapData>('/delay-rules/heatmap', params),
  // 梯度延迟配置
  getDelayJitterConfig: () => get<DelayJitterConfig>('/delay-rules/jitter-config'),
  setDelayJitterConfig: (config: DelayJitterConfig) => put('/delay-rules/jitter-config', config),

  // ========== Mock 增强 ==========
  // 动态响应模板
  getMockTemplates: () => get<MockTemplate[]>('/mock/templates'),
  createMockTemplate: (template: MockTemplate) => post<MockTemplate>('/mock/templates', template),
  updateMockTemplate: (id: string, template: MockTemplate) => put(`/mock/templates/${id}`, template),
  deleteMockTemplate: (id: string) => del(`/mock/templates/${id}`),
  // 多条件匹配规则
  getMockMultiMatchRules: () => get<MockMultiMatchRule[]>('/mock/multi-match-rules'),
  createMockMultiMatchRule: (rule: MockMultiMatchRule) => post<MockMultiMatchRule>('/mock/multi-match-rules', rule),
  updateMockMultiMatchRule: (id: string, rule: MockMultiMatchRule) => put(`/mock/multi-match-rules/${id}`, rule),
  deleteMockMultiMatchRule: (id: string) => del(`/mock/multi-match-rules/${id}`),
  // Mock + 延迟联调：预览模板渲染结果
  previewMockTemplate: (templateId: string, variables?: Record<string, string>) =>
    post<{ rendered_body: string; rendered_headers: Record<string, string>; errors: string[] }>('/mock/templates/preview', { template_id: templateId, variables }),
  // Mock 模板变量提取（从流量自动提取可用变量）
  extractMockTemplateVariables: (flowId: number) => get<{ variables: TemplateVariable[] }>(`/mock/templates/extract-variables/${flowId}`),

  // ========== 录制回放增强 ==========
  // 变量提取：从录制脚本中提取所有变量定义
  getRecordScriptVariables: (scriptId: string) => get<RecordScriptVariables>(`/record-scripts/${scriptId}/variables`),
  // 多环境配置
  getReplayEnvironments: () => get<ReplayEnvironment[]>('/record-scripts/environments'),
  createReplayEnvironment: (env: ReplayEnvironment) => post<ReplayEnvironment>('/record-scripts/environments', env),
  updateReplayEnvironment: (id: string, env: ReplayEnvironment) => put(`/record-scripts/environments/${id}`, env),
  deleteReplayEnvironment: (id: string) => del(`/record-scripts/environments/${id}`),
  // 带环境变量的回放
  replayWithEnvironment: (scriptId: string, environmentId: string, conditions?: RecordCondition[]) =>
    post<ReplayResponse>('/record-scripts/replay-with-env', { script_id: scriptId, environment_id: environmentId, conditions }),
  // 录制 → Mock 自举：从录制脚本生成 Mock 规则
  bootstrapMockFromScript: (scriptId: string, options?: { include_delay?: boolean; extract_variables?: boolean }) =>
    post<{ mock_rules: MockRule[]; variable_extractions: TemplateVariable[] }>('/record-scripts/bootstrap-mock', { script_id: scriptId, options }),
  // 条件执行规则
  getReplayConditionRules: (scriptId: string) => get<ReplayConditionRule[]>(`/record-scripts/${scriptId}/condition-rules`),
  createReplayConditionRule: (scriptId: string, rule: ReplayConditionRule) =>
    post<ReplayConditionRule>(`/record-scripts/${scriptId}/condition-rules`, rule),
  updateReplayConditionRule: (scriptId: string, ruleId: string, rule: ReplayConditionRule) =>
    put(`/record-scripts/${scriptId}/condition-rules/${ruleId}`, rule),
  deleteReplayConditionRule: (scriptId: string, ruleId: string) =>
    del(`/record-scripts/${scriptId}/condition-rules/${ruleId}`),
  // 变量提取（从 flows 自动检测）
  extractRecordVariables: (scriptId: string, flowIds: number[]) =>
    post<{ variables: ScriptVariable[]; count: number }>(`/record-scripts/${scriptId}/variables/extract`, { flow_ids: flowIds }),
  // 断言规则
  listAssertions: () => get<AssertionRule[]>('/record-scripts/assertions'),
  createAssertion: (rule: AssertionRule) => post<AssertionRule>('/record-scripts/assertions', rule),
  updateAssertion: (id: string, rule: AssertionRule) => put(`/record-scripts/assertions/${id}`, rule),
  deleteAssertion: (id: string) => del(`/record-scripts/assertions/${id}`),
  // 增强回放（带环境、断言、条件）
  replayScriptEnhanced: (scriptId: string, environmentId?: string) =>
    post<ReplayResponse>(`/record-scripts/${scriptId}/replay-enhanced`, { environment_id: environmentId }),
  // 录制 → Mock 转换
  convertToMock: (flowIds: number[], options?: { use_template?: boolean; add_delay?: boolean; delay_ms?: number }) =>
    post<{ rules: MockRule[]; count: number }>('/record-scripts/convert-to-mock', { flow_ids: flowIds, ...options }),
}

export default client
