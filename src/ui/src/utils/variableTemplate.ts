/**
 * 变量替换引擎
 * 支持 Mustache/Jinja2 风格的模板语法
 *
 * 语法：
 * - {{variable}} - 简单变量替换
 * - {{variable.default}} - 带默认值
 * - {{#if variable}}...{{/if}} - 条件块
 * - {{#each variable}}...{{/each}} - 循环块
 * - {{variable.path}} - JSONPath 提取
 * - {{variable|filter}} - 过滤器 (upper, lower, urlencode, base64, json)
 */

/** 模板解析错误 */
export interface TemplateError {
  message: string
  position: number
  line?: number
  column?: number
}

/** 解析后的模板节点 */
export type TemplateNode =
  | { type: 'text'; value: string }
  | { type: 'variable'; name: string; filters: string[]; defaultValue?: string }
  | { type: 'if'; condition: string; content: TemplateNode[] }
  | { type: 'endif' }
  | { type: 'each'; variable: string; content: TemplateNode[] }
  | { type: 'endeach' }

/** 解析结果 */
export interface ParseResult {
  nodes: TemplateNode[]
  errors: TemplateError[]
}

/**
 * 解析模板字符串为 AST
 */
export function parseTemplate(template: string): ParseResult {
  const nodes: TemplateNode[] = []
  const errors: TemplateError[] = []
  let pos = 0
  let line = 1
  let column = 1

  while (pos < template.length) {
    // 查找下一个 {{
    const start = template.indexOf('{{', pos)
    if (start === -1) {
      // 剩余文本
      if (pos < template.length) {
        nodes.push({ type: 'text', value: template.slice(pos) })
      }
      break
    }

    // 文本部分
    if (start > pos) {
      nodes.push({ type: 'text', value: template.slice(pos, start) })
    }

    // 找到 {{
    const varStart = start + 2
    const end = template.indexOf('}}', varStart)
    if (end === -1) {
      errors.push({
        message: 'Unclosed variable expression',
        position: start,
        line,
        column: column + start - pos,
      })
      break
    }

    const expr = template.slice(varStart, end).trim()
    pos = end + 2

    // 更新行列
    for (let i = start; i < pos; i++) {
      if (template[i] === '\n') {
        line++
        column = 1
      } else {
        column++
      }
    }

    if (expr.startsWith('#if ')) {
      nodes.push({ type: 'if', condition: expr.slice(4).trim(), content: [] })
    } else if (expr === '/if') {
      nodes.push({ type: 'endif' })
    } else if (expr.startsWith('#each ')) {
      nodes.push({ type: 'each', variable: expr.slice(6).trim(), content: [] })
    } else if (expr === '/each') {
      nodes.push({ type: 'endeach' })
    } else if (expr) {
      // 解析变量表达式
      const varResult = parseVariableExpression(expr)
      if (varResult.error) {
        errors.push({ ...varResult.error, position: start })
      } else {
        nodes.push(varResult.node!)
      }
    }
  }

  return { nodes, errors }
}

function parseVariableExpression(expr: string): { node?: TemplateNode; error?: TemplateError } {
  // 解析过滤器
  const filters: string[] = []
  let name = expr
  const pipeIdx = expr.indexOf('|')
  if (pipeIdx !== -1) {
    name = expr.slice(0, pipeIdx).trim()
    const filterStr = expr.slice(pipeIdx + 1)
    filters.push(...filterStr.split('|').map(f => f.trim()).filter(Boolean))
  }

  // 解析默认值
  let defaultValue: string | undefined
  if (name.includes('.')) {
    const parts = name.split('.')
    name = parts[0]
    // 最后一个非空部分作为默认值
    for (let i = 1; i < parts.length; i++) {
      if (parts[i]) {
        defaultValue = parts.slice(i).join('.')
        break
      }
    }
  }

  // 验证变量名
  if (!/^[a-zA-Z_][a-zA-Z0-9_.-]*$/.test(name)) {
    return {
      error: {
        message: `Invalid variable name: ${name}`,
        position: 0,
      },
    }
  }

  return {
    node: {
      type: 'variable',
      name,
      filters,
      defaultValue,
    },
  }
}

/**
 * 获取嵌套变量的值
 */
export function getNestedValue(obj: any, path: string): any {
  if (!path) return obj
  const parts = path.split('.')
  let current = obj
  for (const part of parts) {
    if (current === null || current === undefined) return undefined
    // 支持数组索引
    const match = part.match(/^(\w+)\[(\d+)\]$/)
    if (match) {
      current = current[match[1]]
      if (Array.isArray(current)) {
        current = current[parseInt(match[2], 10)]
      }
    } else {
      current = current[part]
    }
  }
  return current
}

/**
 * 应用过滤器
 */
function applyFilters(value: any, filters: string[]): string {
  let result = value
  for (const filter of filters) {
    switch (filter.toLowerCase()) {
      case 'upper':
      case 'uppercase':
        result = String(result).toUpperCase()
        break
      case 'lower':
      case 'lowercase':
        result = String(result).toLowerCase()
        break
      case 'urlencode':
      case 'url':
        result = encodeURIComponent(String(result))
        break
      case 'base64':
        try {
          result = btoa(unescape(encodeURIComponent(String(result))))
        } catch {
          result = String(result)
        }
        break
      case 'json':
        try {
          result = JSON.stringify(typeof result === 'string' ? JSON.parse(result) : result)
        } catch {
          result = String(result)
        }
        break
      case 'int':
        result = parseInt(String(result), 10)
        break
      case 'float':
        result = parseFloat(String(result))
        break
      case 'default':
        // default 过滤器由调用方处理默认值
        break
      default:
        // 未知过滤器，忽略
        break
    }
  }
  return String(result ?? '')
}

/**
 * 执行变量替换
 */
export function renderTemplate(
  template: string,
  context: Record<string, any>,
  strict = false
): { result: string; errors: TemplateError[] } {
  const { nodes, errors } = parseTemplate(template)
  let result = ''

  function processNodes(nodeList: TemplateNode[]): string {
    let output = ''
    let i = 0
    while (i < nodeList.length) {
      const node = nodeList[i]
      switch (node.type) {
        case 'text':
          output += node.value
          break
        case 'variable': {
          let value = getNestedValue(context, node.name)
          if (value === undefined || value === null) {
            if (node.defaultValue !== undefined) {
              value = node.defaultValue
            } else if (strict) {
              errors.push({
                message: `Undefined variable: ${node.name}`,
                position: 0,
              })
            }
          }
          output += applyFilters(value, node.filters)
          break
        }
        case 'if': {
          // 收集 if 块内容
          const content: TemplateNode[] = []
          i++
          while (i < nodeList.length && nodeList[i].type !== 'endif') {
            content.push(nodeList[i])
            i++
          }
          const condValue = getNestedValue(context, node.condition)
          if (condValue && condValue !== null && condValue !== undefined) {
            output += processNodes(content)
          }
          break
        }
        case 'endif':
          break
        case 'each': {
          const content: TemplateNode[] = []
          i++
          while (i < nodeList.length && nodeList[i].type !== 'endeach') {
            content.push(nodeList[i])
            i++
          }
          const items = getNestedValue(context, node.variable)
          if (Array.isArray(items)) {
            for (const item of items) {
              output += processNodes(content.map(n =>
                n.type === 'variable' ? { ...n, name: `item.${n.name}` } : n
              ))
            }
          }
          break
        }
        case 'endeach':
          break
      }
      i++
    }
    return output
  }

  result = processNodes(nodes)
  return { result, errors }
}

/**
 * 验证模板语法
 */
export function validateTemplate(template: string): TemplateError[] {
  const { errors } = parseTemplate(template)
  return errors
}

/**
 * 提取模板中的所有变量名
 */
export function extractVariables(template: string): string[] {
  const { nodes } = parseTemplate(template)
  const variables = new Set<string>()

  function collect(nodeList: TemplateNode[]) {
    for (const node of nodeList) {
      if (node.type === 'variable') {
        variables.add(node.name)
      }
    }
  }

  collect(nodes)
  return [...variables]
}

/** 语法高亮标记类型 */
export interface HighlightSpan {
  start: number
  end: number
  className: string
}

/**
 * 获取语法高亮标记
 */
export function getHighlightSpans(template: string): HighlightSpan[] {
  const spans: HighlightSpan[] = []
  const regex = /\{\{([^}]+)\}\}/g
  let match

  while ((match = regex.exec(template)) !== null) {
    const start = match.index
    const end = start + match[0].length
    const content = match[1].trim()

    // 变量名
    spans.push({ start, end, className: 'tpl-brace' })
    spans.push({ start: start + 2, end: start + 2 + match[0].length - 4, className: 'tpl-content' })

    // 检查内容类型
    if (content.startsWith('#if ') || content.startsWith('/if')) {
      spans.push({ start: start + 4, end: start + match[0].length - 2, className: 'tpl-keyword' })
    } else if (content.startsWith('#each ') || content.startsWith('/each')) {
      spans.push({ start: start + 4, end: start + match[0].length - 2, className: 'tpl-keyword' })
    } else if (content.includes('|')) {
      // 过滤器
      const pipeIdx = content.indexOf('|')
      spans.push({ start: start + 2, end: start + 2 + pipeIdx, className: 'tpl-variable' })
      spans.push({ start: start + 2 + pipeIdx, end: start + match[0].length - 2, className: 'tpl-filter' })
    } else {
      spans.push({ start: start + 2, end: start + match[0].length - 2, className: 'tpl-variable' })
    }
  }

  return spans
}

/** 模板编辑器样式（可注入到 shadow DOM 或独立样式表） */
export const TEMPLATE_EDITOR_STYLES = `
.tpl-editor {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  line-height: 1.6;
  tab-size: 2;
  white-space: pre-wrap;
  word-break: break-all;
}
.tpl-brace { color: #888; }
.tpl-variable { color: #61afef; }
.tpl-filter { color: #e5c07b; }
.tpl-keyword { color: #c678dd; font-weight: 500; }
.tpl-content { color: inherit; }
.tpl-error { color: #f44747; text-decoration: wavy underline; }
`

/** 简单的高亮 HTML 生成 */
export function highlightTemplate(template: string): string {
  const regex = /(\{\{[^}]+\}\})/g
  return template.replace(regex, (match) => {
    const content = match.slice(2, -2).trim()
    if (content.startsWith('#if ') || content.startsWith('/if')) {
      return `<span class="tpl-brace">{{</span><span class="tpl-keyword">${content}</span><span class="tpl-brace">}}</span>`
    }
    if (content.startsWith('#each ') || content.startsWith('/each')) {
      return `<span class="tpl-brace">{{</span><span class="tpl-keyword">${content}</span><span class="tpl-brace">}}</span>`
    }
    if (content.includes('|')) {
      const [varPart, ...filterParts] = content.split('|')
      return `<span class="tpl-brace">{{</span><span class="tpl-variable">${varPart.trim()}</span><span class="tpl-filter">|${filterParts.join('|')}</span><span class="tpl-brace">}}</span>`
    }
    return `<span class="tpl-brace">{{</span><span class="tpl-variable">${content}</span><span class="tpl-brace">}}</span>`
  })
}
