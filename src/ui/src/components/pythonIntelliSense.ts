/**
 * Python IntelliSense 注册（针对 Telnix 脚本规则）
 *
 * 注册内容：
 * 1. Python 关键字补全（def / return / if / for / import 等）
 * 2. 常用内置函数（print / len / str / dict / list / range / open 等）
 * 3. json / re / base64 模块成员
 * 4. ctx API：当输入 `ctx.` 时弹出 ctx 的属性和方法（含文档）
 * 5. on_request / on_response 钩子函数模板
 *
 * 注册是全局的（monaco 单例），多次调用幂等。
 * monaco 实例由调用方传入（动态加载后传入），避免本文件静态导入 monaco-editor。
 */
import type * as Monaco from 'monaco-editor'

let registered = false

export function registerPythonIntelliSense(monaco: typeof Monaco) {
  if (registered) return
  registered = true

  // ---------- 通用 Python 补全（关键字 + 内置函数）----------
  const keywords: Array<[string, string]> = [
    ['def', '定义函数'],
    ['return', '返回值'],
    ['if', '条件分支'],
    ['elif', 'else if'],
    ['else', '条件分支'],
    ['for', '循环'],
    ['while', '循环'],
    ['in', '成员运算符'],
    ['not', '逻辑非'],
    ['and', '逻辑与'],
    ['or', '逻辑或'],
    ['import', '导入模块'],
    ['from', '从模块导入'],
    ['as', '别名'],
    ['class', '定义类'],
    ['try', '异常处理'],
    ['except', '捕获异常'],
    ['finally', '最终执行'],
    ['raise', '抛出异常'],
    ['with', '上下文管理器'],
    ['lambda', '匿名函数'],
    ['None', '空值'],
    ['True', '真'],
    ['False', '假'],
    ['pass', '占位'],
    ['break', '跳出循环'],
    ['continue', '继续循环'],
    ['global', '全局变量'],
    ['nonlocal', '非局部变量'],
    ['yield', '生成器返回'],
    ['async', '异步'],
    ['await', '等待异步'],
  ]

  const builtins: Array<[string, string, string]> = [
    ['print', 'print(*args)', '打印输出'],
    ['len', 'len(obj)', '返回长度'],
    ['str', 'str(obj)', '转字符串'],
    ['int', 'int(x)', '转整数'],
    ['float', 'float(x)', '转浮点数'],
    ['bool', 'bool(x)', '转布尔'],
    ['list', 'list()', '列表'],
    ['dict', 'dict()', '字典'],
    ['tuple', 'tuple()', '元组'],
    ['set', 'set()', '集合'],
    ['range', 'range(n)', '范围'],
    ['enumerate', 'enumerate(iter)', '带索引遍历'],
    ['zip', 'zip(*iters)', '并行遍历'],
    ['map', 'map(fn, iter)', '映射'],
    ['filter', 'filter(fn, iter)', '过滤'],
    ['sorted', 'sorted(iter)', '排序'],
    ['reversed', 'reversed(iter)', '反转'],
    ['open', 'open(path)', '打开文件'],
    ['isinstance', 'isinstance(o, t)', '类型判断'],
    ['hasattr', 'hasattr(o, name)', '是否有属性'],
    ['getattr', 'getattr(o, name)', '获取属性'],
    ['setattr', 'setattr(o, name, v)', '设置属性'],
    ['bytes', 'bytes(s, enc)', '字节串'],
    ['bytearray', 'bytearray()', '可变字节串'],
    ['type', 'type(o)', '获取类型'],
    ['abs', 'abs(x)', '绝对值'],
    ['min', 'min(*args)', '最小值'],
    ['max', 'max(*args)', '最大值'],
    ['sum', 'sum(iter)', '求和'],
    ['round', 'round(x, n)', '四舍五入'],
    ['format', 'format(*args)', '格式化'],
    ['repr', 'repr(o)', ' repr 字符串'],
  ]

  const jsonMembers: Array<[string, string, string]> = [
    ['loads', 'json.loads(s)', 'JSON 字符串 → 对象'],
    ['load', 'json.load(fp)', '从文件读 JSON'],
    ['dumps', 'json.dumps(obj)', '对象 → JSON 字符串'],
    ['dump', 'json.dump(obj, fp)', '写入 JSON 到文件'],
    ['JSONDecodeError', 'json.JSONDecodeError', 'JSON 解析错误'],
  ]

  const reMembers: Array<[string, string, string]> = [
    ['match', 're.match(pat, s)', '从开头匹配'],
    ['search', 're.search(pat, s)', '搜索匹配'],
    ['findall', 're.findall(pat, s)', '全部匹配'],
    ['sub', 're.sub(pat, repl, s)', '替换'],
    ['split', 're.split(pat, s)', '分割'],
    ['compile', 're.compile(pat)', '编译正则'],
    ['IGNORECASE', 're.IGNORECASE', '忽略大小写'],
  ]

  const base64Members: Array<[string, string, string]> = [
    ['b64encode', 'base64.b64encode(b)', '编码为 base64'],
    ['b64decode', 'base64.b64decode(s)', '从 base64 解码'],
  ]

  // ---------- ctx API ----------
  // ctx 在 on_request / on_response 中可用，提供请求/响应上下文
  const ctxProps: Array<[string, string, string]> = [
    // 请求阶段可用
    ['host', 'ctx.host', '目标 host（如 example.com）'],
    ['path', 'ctx.path', 'URL 路径（如 /api/user）'],
    ['method', 'ctx.method', 'HTTP 方法（GET/POST...）'],
    ['url', 'ctx.url', '完整 URL'],
    ['scheme', 'ctx.scheme', '协议（http/https）'],
    ['pid', 'ctx.pid', '发起进程 PID'],
    ['process_name', 'ctx.process_name', '发起进程名'],
    ['request_headers', 'ctx.request_headers', '请求头 dict'],
    ['request_body', 'ctx.request_body', '请求体 bytes'],
    // 响应阶段额外可用
    ['status_code', 'ctx.status_code', '响应状态码（仅 on_response）'],
    ['response_headers', 'ctx.response_headers', '响应头 dict（仅 on_response）'],
    ['response_body', 'ctx.response_body', '响应体 bytes（仅 on_response）'],
  ]

  const ctxMethods: Array<[string, string, string]> = [
    ['set_request_header', 'ctx.set_request_header(name, value)', '设置/覆盖请求头'],
    ['set_request_body', 'ctx.set_request_body(bytes)', '设置请求体'],
    ['set_response_header', 'ctx.set_response_header(name, value)', '设置/覆盖响应头（仅 on_response）'],
    ['set_response_body', 'ctx.set_response_body(bytes)', '设置响应体（仅 on_response）'],
    ['set_status_code', 'ctx.set_status_code(code)', '设置响应状态码（仅 on_response）'],
  ]

  // ---------- 钩子函数模板 ----------
  const hookSnippets: Array<[string, string, string]> = [
    [
      'on_request',
      'def on_request(ctx):\n    ${1:pass}\n    return None',
      '请求钩子：转发前调用',
    ],
    [
      'on_response',
      'def on_response(ctx):\n    ${1:pass}\n    return None',
      '响应钩子：返回客户端前调用',
    ],
    [
      'drop',
      '{"drop": True}',
      '拒绝请求（在 on_request 中返回）',
    ],
    [
      'mock',
      '{"mock": True, "status": 200, "headers": {}, "body": b""}',
      '伪造响应（在 on_request 中返回）',
    ],
  ]

  // 辅助：构造 CompletionItem
  function makeItem(
    label: string,
    kind: Monaco.languages.CompletionItemKind,
    insertText: string,
    detail: string,
    documentation: string,
    insertAsSnippet = false
  ): Monaco.languages.CompletionItem {
    return {
      label,
      kind,
      insertText: insertText as any,
      insertTextRules: insertAsSnippet
        ? monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet
        : monaco.languages.CompletionItemInsertTextRule.None,
      detail,
      documentation: { value: documentation },
    } as Monaco.languages.CompletionItem
  }

  // 注册补全 provider
  monaco.languages.registerCompletionItemProvider('python', {
    triggerCharacters: ['.', '(', 'def ', 'return '].slice(0, 1) as any, // 仅 . 作为触发符
    provideCompletionItems(model, position) {
      const word = model.getWordUntilPosition(position)
      const range = {
        startLineNumber: position.lineNumber,
        endLineNumber: position.lineNumber,
        startColumn: word.startColumn,
        endColumn: word.endColumn,
      }

      // 取光标前一行文本（用于检测 ctx. / json. / re. 等前缀）
      const lineUpToCursor = model.getValueInRange({
        startLineNumber: position.lineNumber,
        startColumn: 1,
        endLineNumber: position.lineNumber,
        endColumn: position.column,
      })

      const suggestions: Monaco.languages.CompletionItem[] = []

      // ---------- 检测 xxx. 前缀，给特定成员 ----------
      const dotMatch = lineUpToCursor.match(/([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*$/)
      if (dotMatch) {
        const prefix = dotMatch[1]
        if (prefix === 'ctx') {
          for (const [name, insert, doc] of ctxProps) {
            suggestions.push(
              makeItem(name, monaco.languages.CompletionItemKind.Property, name, insert, doc)
            )
          }
          for (const [name, insert, doc] of ctxMethods) {
            suggestions.push(
              makeItem(name, monaco.languages.CompletionItemKind.Method, insert, insert, doc, true)
            )
          }
          return { suggestions }
        }
        if (prefix === 'json') {
          for (const [name, insert, doc] of jsonMembers) {
            suggestions.push(
              makeItem(name, monaco.languages.CompletionItemKind.Function, insert, insert, doc, true)
            )
          }
          return { suggestions }
        }
        if (prefix === 're') {
          for (const [name, insert, doc] of reMembers) {
            suggestions.push(
              makeItem(name, monaco.languages.CompletionItemKind.Function, insert, insert, doc, true)
            )
          }
          return { suggestions }
        }
        if (prefix === 'base64') {
          for (const [name, insert, doc] of base64Members) {
            suggestions.push(
              makeItem(name, monaco.languages.CompletionItemKind.Function, insert, insert, doc, true)
            )
          }
          return { suggestions }
        }
        // 其他 xxx. 不给提示（让用户自行输入）
        return { suggestions }
      }

      // ---------- 没有点号前缀：给关键字 + 内置 + 钩子模板 ----------
      // 仅当光标前的单词非空时才提示
      if (!word.word) {
        // 也在行首给关键字
      }

      for (const [kw, doc] of keywords) {
        suggestions.push(
          makeItem(kw, monaco.languages.CompletionItemKind.Keyword, kw, 'keyword', doc)
        )
      }
      for (const [name, sig, doc] of builtins) {
        suggestions.push(
          makeItem(name, monaco.languages.CompletionItemKind.Function, name, sig, doc)
        )
      }
      for (const [name, body, doc] of hookSnippets) {
        suggestions.push(
          makeItem(name, monaco.languages.CompletionItemKind.Snippet, body, 'Telnix 钩子', doc, true)
        )
      }
      // ctx 本身也提示
      suggestions.push(
        makeItem('ctx', monaco.languages.CompletionItemKind.Variable, 'ctx', 'Telnix ctx', '请求/响应上下文对象')
      )

      return { suggestions }
    },
  })

  // ---------- 注册 hover provider：给 ctx 成员加 hover 文档 ----------
  monaco.languages.registerHoverProvider('python', {
    provideHover(model, position) {
      const word = model.getWordAtPosition(position)
      if (!word) return null
      const lineText = model.getLineContent(position.lineNumber)
      // 取 word 前面的 `xxx.` 前缀
      const before = lineText.slice(0, word.startColumn - 1)
      const prefixMatch = before.match(/([A-Za-z_][A-Za-z0-9_]*)\s*\.\s*$/)
      if (!prefixMatch) return null
      const prefix = prefixMatch[1]
      if (prefix !== 'ctx') return null

      const name = word.word
      const all = [...ctxProps, ...ctxMethods]
      const hit = all.find(([n]) => n === name)
      if (!hit) return null
      const [, sig, doc] = hit
      return {
        range: {
          startLineNumber: position.lineNumber,
          endLineNumber: position.lineNumber,
          startColumn: word.startColumn,
          endColumn: word.endColumn,
        },
        contents: [
          { value: `**${sig}**` },
          { value: doc },
        ],
      }
    },
  })

  // ---------- 注册 signature help：给 ctx 方法参数提示 ----------
  monaco.languages.registerSignatureHelpProvider('python', {
    signatureHelpTriggerCharacters: ['(', ','],
    provideSignatureHelp(model: any, position: any): any {
      // 取光标前文本，反向查找最近的 ( 用于匹配方法
      const lineUpToCursor = model.getValueInRange({
        startLineNumber: position.lineNumber,
        startColumn: 1,
        endLineNumber: position.lineNumber,
        endColumn: position.column,
      })
      const m = lineUpToCursor.match(/ctx\.([a-z_]+)\s*\($/)
      if (!m) return null
      const methodName = m[1]
      const method = ctxMethods.find(([n]) => n === methodName)
      if (!method) return null
      const [, sig, doc] = method
      // 解析参数：set_request_header(name, value) → ['name', 'value']
      const argMatch = sig.match(/\((.*)\)/)
      const argStr = argMatch ? argMatch[1] : ''
      const params = argStr ? argStr.split(',').map((s) => s.trim()) : []
      return {
        activeSignature: 0,
        activeParameter: 0,
        signatures: [
          {
            label: sig,
            documentation: doc,
            parameters: params.map((p) => ({ label: p, documentation: '' })),
          },
        ],
      } as Monaco.languages.SignatureHelp
    },
  })
}
