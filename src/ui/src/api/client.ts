import axios, { type AxiosResponse } from 'axios'
import { waitForWindivertAck } from '../stores/windivertWarning'

// ============ 类型定义 ============

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

/** 设置 */
export interface Settings {
  deepseek_api_key?: string
  inspector_tabs?: string[]
  data_path?: string
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
}

/** 搜索结果 */
export interface SearchResult {
  matches: Flow[]
  count: number
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
        return _handleWindivertAck(response, res.msg || '需要确认 WinDivert 风险提示')
      }
      return Promise.reject(new Error(res.msg || '请求失败'))
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
    return Promise.reject(new Error('用户取消了 WinDivert 风险提示确认'))
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
  return Promise.reject(new Error('无法重试原请求（缺少 config）'))
}

// 由于拦截器已解包，这里把 AxiosPromise 转为 Promise<T>
async function get<T = any>(url: string, params?: any): Promise<T> {
  return client.get(url, { params }) as unknown as Promise<T>
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
  exportRulesUrl: () => '/api/auto-reply/rules/export',
  importRules: (rules: any[], mode: string = 'merge') =>
    post<{ imported: number; mode: string }>('/auto-reply/rules/import', { rules, mode }),
  // 测试 Python 脚本（不发起真实请求，用 mock 数据走 worker 子进程）
  // mode: "request" 只请求 / "response" 只响应 / "both" 请求+响应
  testScript: (body: {
    script: string
    mock_request: ScriptMockRequest
    mock_response?: ScriptMockResponse | null
    mode?: 'request' | 'response' | 'both'
  }) => post<ScriptTestResult>('/auto-reply/test-script', body, 15000),
  // 流量删除
  deleteFlow: (id: number) => del(`/flows/${id}`),
  batchDeleteFlows: (ids: number[]) => post('/flows/batch-delete', { ids }),
  batchReleaseFlows: (ids: number[], action: string = 'release') =>
    post('/flows/batch-release', { ids, action }),
  // 全局分析：跨会话查询所有流量 + 清理
  getAllFlows: (params?: any) => get('/flows/all', params),
  // 全量分组统计（不分页，用于统计图显示所有数据比例）
  getFlowsStats: (groupBy: string = 'host', host?: string, process?: string) =>
    get('/flows/stats', { group_by: groupBy, host, process }),
  // 多维聚合统计（CoolUI 仪表盘用，一次返回所有维度）
  getFlowsOverview: () => get<FlowOverview>('/flows/overview'),
  clearAllFlows: (mode: string = 'all', beforeId?: number) =>
    post('/flows/clear', { mode, before_id: beforeId }),

  // 系统控制
  restartService: () => post('/system/restart'),
  restartAsAdmin: () => post('/system/restart-as-admin'),
  quitService: () => post('/system/quit'),
  clearProxy: () => post('/system/clear-proxy'),
  enableProxy: () => post('/system/enable-proxy'),
  // 管理员重启（GUI 确认流程）
  getPendingAdminActions: () => get<{ items: any[] }>('/system/pending-admin-actions'),
  respondAdminRequest: (rid: string, response: 'accept' | 'reject') =>
    post(`/system/admin-request/${rid}/respond`, { response }),

  // WinDivert 风险提示
  getWindivertWarning: () => get<WindivertWarningStatus>('/system/windivert-warning'),
  ackWindivertWarning: () => post<{ ack: boolean }>('/system/windivert-warning/ack'),

  // 可选依赖安装（mitmproxy 等）
  installDep: (pkg: string) => post('/system/install-dep', { package: pkg }),
  installDepStatus: () => get<{
    status: 'idle' | 'running' | 'success' | 'failed'
    package?: string
    started_at?: number | null
    finished_at?: number | null
    log?: string
    return_code?: number | null
    mitmproxy_available?: boolean
    mitmproxy_version?: string | null
    note?: string
  }>('/system/install-dep/status'),
  installDepCancel: () => post('/system/install-dep/cancel'),

  // AI
  aiAnalyze: (body: { flow_ids: number[] }) =>
    post<{ chat_id: number; result: string; title: string }>('/ai/analyze', body, AI_TIMEOUT),
  aiChat: (body: { chat_id: number; message: string; flow_ids?: number[] }) =>
    post<{ result: string }>('/ai/chat', body, AI_TIMEOUT),
  aiListChats: () => get<AiChat[]>('/ai/chats'),
  aiGetChat: (chatId: number) => get<{ chat: AiChat; messages: AiMessage[] }>(`/ai/chats/${chatId}`),
  aiUpdateTitle: (chatId: number, title: string) =>
    put(`/ai/chats/${chatId}/title`, { title }),
  aiDeleteChat: (chatId: number) => del(`/ai/chats/${chatId}`),

  // 导出
  exportSession: (sessionId: number, format: string) =>
    post<{ url?: string; path?: string; data?: any }>(`/export/${sessionId}`, { format }),

  // 导入流量（JSON 或 HAR 格式）
  importFlows: (body: { format: string; content: string; session_name?: string }) =>
    post<{ session_id: number; session_name: string; imported: number; total: number }>('/import', body),

  // 设置
  getSettings: () => get<Settings>('/settings'),
  saveSettings: (body: Settings) => put<Settings>('/settings', body),

  // 进程
  getProcesses: () => get<ProcessInfo[]>('/processes'),
  ignoreProcess: (body: { pid: number | null; name: string }) => post('/processes/ignore', body),
  unignoreProcess: (rowId: number) => del(`/processes/ignore/${rowId}`),
  getIgnoredProcesses: () => get<IgnoredProcess[]>('/processes/ignored'),
  // 忽略 host 通配符（支持 * ? 通配符，如 *.example.com）
  ignoreHost: (host: string) => post('/processes/ignore-host', { host }),
  unignoreHost: (id: number) => del(`/processes/ignore-host/${id}`),
  getIgnoredHosts: () => get<{ id: number; host_pattern: string; created_at: string }[]>('/processes/ignored-hosts'),

  // 证书
  installCert: () => post('/cert/install'),
  getCertStatus: () => get<{ installed: boolean }>('/cert/status'),
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
  logExportUrl: (params?: { level?: string; category?: string; keyword?: string }) => {
    const q = new URLSearchParams()
    if (params?.level) q.set('level', params.level)
    if (params?.category) q.set('category', params.category)
    if (params?.keyword) q.set('keyword', params.keyword)
    const qs = q.toString()
    return `/api/logs/export${qs ? '?' + qs : ''}`
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

  // 搜索与统计
  searchFlows: (body: { session_id: number; body_regex?: string; binary_hex?: string; limit?: number }) =>
    post<SearchResult>('/flows/search', body),
  getStats: (sessionId: number) => get<FlowStats>(`/sessions/${sessionId}/stats`),
  getFlowHex: (flowId: number, params?: { offset?: number; length?: number; field?: string }) =>
    get<HexResult>(`/flows/${flowId}/hex`, params),

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
}

export default client
