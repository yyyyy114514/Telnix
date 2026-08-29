// Flowfilter DSL 解析器（mitmproxy 风格简化版）
//
// 语法：
//   ~d <host>        host 包含
//   ~m <METHOD>      method 等于（GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS）
//   ~s <code>        状态码（支持 200 精确，或 4xx/5xx 通配）
//   ~p <proto>       协议（http/https/tcp/udp/ws）
//   ~u <path>        URL 路径包含
//   ~h <header>      任意 header 名/值包含
//   ~b <text>        request/response body 包含
//   "<text>"         任意字段包含（host/url/method/headers/body 综合）
//
// 组合：
//   &   AND（默认分隔符为空格也即 AND）
//   |   OR（暂未实现，默认全部为 AND）
//
// 字段值支持 `*` 通配符（转成 JS 正则）
//
// 例：
//   ~d google.com ~m GET            # google.com 的 GET 请求
//   ~s 4xx                          # 4xx 错误
//   ~d *.example.com ~u /api/v1     # example.com 子域 + /api/v1 路径
//   "user_id=123"                   # 任意字段含 user_id=123

import type { Flow } from '../api/client'

export interface ParsedFilter {
  raw: string
  // 解析后的条件列表（与关系）
  conditions: Condition[]
  // 错误（如有）
  error?: string
}

interface Condition {
  type: 'host' | 'method' | 'status' | 'protocol' | 'url' | 'header' | 'body' | 'any'
  // status 支持 '4xx' 通配，用 pattern 字符串
  value: string
  // 编译后的正则（仅对 host/url/header/body/any 有效）
  regex?: RegExp
  // method 用精确比较
  // status 用通配比较
}

const FIELD_ALIASES: Record<string, Condition['type']> = {
  d: 'host', host: 'host',
  m: 'method', method: 'method',
  s: 'status', status: 'status', code: 'status',
  p: 'protocol', proto: 'protocol', protocol: 'protocol',
  u: 'url', url: 'url', path: 'url',
  h: 'header', header: 'header',
  b: 'body', body: 'body',
}

/**
 * 解析 Flowfilter DSL 字符串。
 * 失败时返回 error 字段；前端可显示红色提示。
 */
export function parseFlowFilter(input: string): ParsedFilter {
  const trimmed = input.trim()
  if (!trimmed) return { raw: input, conditions: [] }

  const conditions: Condition[] = []
  const tokens = tokenize(trimmed)

  for (const tok of tokens) {
    if (tok.startsWith('~')) {
      // ~d host 形式
      const sp = tok.indexOf(' ')
      if (sp === -1) {
        return { raw: input, conditions, error: `Syntax error: '${tok}' missing value, expected ~X value` }
      }
      const key = tok.slice(1, sp)
      const value = tok.slice(sp + 1).trim()
      if (!value) {
        return { raw: input, conditions, error: `Syntax error: '~${key}' value is empty` }
      }
      const type = FIELD_ALIASES[key.toLowerCase()]
      if (!type) {
        return { raw: input, conditions, error: `Unknown field: ~${key}` }
      }
      conditions.push(buildCondition(type, value))
    } else if (tok.startsWith('"') && tok.endsWith('"') && tok.length >= 2) {
      // "任意文本"
      const value = tok.slice(1, -1)
      if (value) conditions.push(buildCondition('any', value))
    } else {
      // 裸词：当作 any 处理
      conditions.push(buildCondition('any', tok))
    }
  }

  return { raw: input, conditions }
}

function buildCondition(type: Condition['type'], value: string): Condition {
  // method/status 不用正则
  if (type === 'method') {
    return { type, value: value.toUpperCase() }
  }
  if (type === 'status') {
    return { type, value: value.toLowerCase() }  // 4xx / 200
  }
  // host/url/header/body/any 用正则
  let regex: RegExp | undefined
  try {
    // * → .*, ? → ., 其他转义
    const pattern = value
      .replace(/[.+^${}()|[\]\\]/g, '\\$&')
      .replace(/\*/g, '.*')
      .replace(/\?/g, '.')
    regex = new RegExp(pattern, 'i')
  } catch {
    /* ignore */
  }
  return { type, value, regex }
}

function tokenize(input: string): string[] {
  const tokens: string[] = []
  let i = 0
  const n = input.length
  while (i < n) {
    // 跳过空白
    while (i < n && /\s/.test(input[i])) i++
    if (i >= n) break

    if (input[i] === '"') {
      // 引号字符串
      const end = input.indexOf('"', i + 1)
      if (end === -1) {
        // 未闭合：把剩余全部当一个 token
        tokens.push('"' + input.slice(i + 1) + '"')
        i = n
      } else {
        tokens.push(input.slice(i, end + 1))
        i = end + 1
      }
    } else if (input[i] === '~') {
      // ~X value，value 可以是引号或裸词
      let j = i + 1
      while (j < n && input[j] !== ' ') j++
      // 跳过空格
      while (j < n && input[j] === ' ') j++
      const valueStart = j
      if (input[j] === '"') {
        const end = input.indexOf('"', j + 1)
        if (end === -1) {
          tokens.push(input.slice(i))
          i = n
        } else {
          tokens.push(input.slice(i, end + 1))
          i = end + 1
        }
      } else {
        while (j < n && input[j] !== ' ') j++
        tokens.push(input.slice(i, j))
        i = j
      }
    } else {
      // 裸词
      let j = i
      while (j < n && input[j] !== ' ') j++
      tokens.push(input.slice(i, j))
      i = j
    }
  }
  return tokens
}

/**
 * 判断单条 flow 是否匹配过滤条件。
 * @returns 是否匹配
 */
export function matchFlow(flow: Flow, filter: ParsedFilter): boolean {
  if (!filter.conditions.length) return true
  for (const c of filter.conditions) {
    if (!matchCondition(flow, c)) return false
  }
  return true
}

function matchCondition(flow: Flow, c: Condition): boolean {
  switch (c.type) {
    case 'host': {
      const h = flow.host || ''
      return c.regex ? c.regex.test(h) : h.toLowerCase().includes(c.value.toLowerCase())
    }
    case 'method': {
      return (flow.method || '').toUpperCase() === c.value
    }
    case 'status': {
      const code = flow.status_code
      if (code == null) return false
      const v = c.value
      if (v.endsWith('xx')) {
        // 4xx → 4 开头
        const prefix = v[0]
        return String(code).startsWith(prefix)
      }
      return String(code) === v
    }
    case 'protocol': {
      const proto = (flow.protocol || flow.scheme || 'http').toLowerCase()
      const v = c.value.toLowerCase()
      if (v === 'http') return proto === 'http' || proto === 'https'
      return proto === v
    }
    case 'url': {
      const u = flow.url || ''
      return c.regex ? c.regex.test(u) : u.toLowerCase().includes(c.value.toLowerCase())
    }
    case 'header': {
      const reqH = flow.request_headers || ''
      const respH = flow.response_headers || ''
      const all = reqH + '\n' + respH
      return c.regex ? c.regex.test(all) : all.toLowerCase().includes(c.value.toLowerCase())
    }
    case 'body': {
      const reqB = flow.request_body || ''
      const respB = flow.response_body || ''
      const all = reqB + '\n' + respB
      return c.regex ? c.regex.test(all) : all.toLowerCase().includes(c.value.toLowerCase())
    }
    case 'any': {
      const all = [
        flow.host, flow.url, flow.method,
        flow.request_headers, flow.response_headers,
        flow.request_body, flow.response_body,
        flow.process_name,
      ].join('\n')
      return c.regex ? c.regex.test(all) : all.toLowerCase().includes(c.value.toLowerCase())
    }
    default:
      return true
  }
}
