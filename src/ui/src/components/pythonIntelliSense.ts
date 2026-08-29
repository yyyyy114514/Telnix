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
    ['def', 'Define function'],
    ['return', 'Return value'],
    ['if', 'Conditional branch'],
    ['elif', 'else if'],
    ['else', 'Conditional branch'],
    ['for', 'Loop'],
    ['while', 'Loop'],
    ['in', 'Membership operator'],
    ['not', 'Logical NOT'],
    ['and', 'Logical AND'],
    ['or', 'Logical OR'],
    ['import', 'Import module'],
    ['from', 'From module import'],
    ['as', 'Alias'],
    ['class', 'Define class'],
    ['try', 'Exception handling'],
    ['except', 'Catch exception'],
    ['finally', 'Finally block'],
    ['raise', 'Raise exception'],
    ['with', 'Context manager'],
    ['lambda', 'Anonymous function'],
    ['None', 'Null value'],
    ['True', 'True'],
    ['False', 'False'],
    ['pass', 'Placeholder'],
    ['break', 'Break loop'],
    ['continue', 'Continue loop'],
    ['global', 'Global variable'],
    ['nonlocal', 'Nonlocal variable'],
    ['yield', 'Generator return'],
    ['async', 'Async'],
    ['await', 'Await async'],
  ]

  const builtins: Array<[string, string, string]> = [
    ['print', 'print(*args)', 'Print output'],
    ['len', 'len(obj)', 'Return length'],
    ['str', 'str(obj)', 'Convert to string'],
    ['int', 'int(x)', 'Convert to int'],
    ['float', 'float(x)', 'Convert to float'],
    ['bool', 'bool(x)', 'Convert to bool'],
    ['list', 'list()', 'List'],
    ['dict', 'dict()', 'Dict'],
    ['tuple', 'tuple()', 'Tuple'],
    ['set', 'set()', 'Set'],
    ['range', 'range(n)', 'Range'],
    ['enumerate', 'enumerate(iter)', 'Enumerate with index'],
    ['zip', 'zip(*iters)', 'Parallel iteration'],
    ['map', 'map(fn, iter)', 'Map'],
    ['filter', 'filter(fn, iter)', 'Filter'],
    ['sorted', 'sorted(iter)', 'Sorted'],
    ['reversed', 'reversed(iter)', 'Reversed'],
    ['open', 'open(path)', 'Open file'],
    ['isinstance', 'isinstance(o, t)', 'Type check'],
    ['hasattr', 'hasattr(o, name)', 'Has attribute'],
    ['getattr', 'getattr(o, name)', 'Get attribute'],
    ['setattr', 'setattr(o, name, v)', 'Set attribute'],
    ['bytes', 'bytes(s, enc)', 'Bytes'],
    ['bytearray', 'bytearray()', 'Mutable bytes'],
    ['type', 'type(o)', 'Get type'],
    ['abs', 'abs(x)', 'Absolute value'],
    ['min', 'min(*args)', 'Minimum'],
    ['max', 'max(*args)', 'Maximum'],
    ['sum', 'sum(iter)', 'Sum'],
    ['round', 'round(x, n)', 'Round'],
    ['format', 'format(*args)', 'Format'],
    ['repr', 'repr(o)', 'Repr string'],
  ]

  const jsonMembers: Array<[string, string, string]> = [
    ['loads', 'json.loads(s)', 'JSON string → object'],
    ['load', 'json.load(fp)', 'Read JSON from file'],
    ['dumps', 'json.dumps(obj)', 'Object → JSON string'],
    ['dump', 'json.dump(obj, fp)', 'Write JSON to file'],
    ['JSONDecodeError', 'json.JSONDecodeError', 'JSON decode error'],
  ]

  const reMembers: Array<[string, string, string]> = [
    ['match', 're.match(pat, s)', 'Match from start'],
    ['search', 're.search(pat, s)', 'Search match'],
    ['findall', 're.findall(pat, s)', 'Find all matches'],
    ['sub', 're.sub(pat, repl, s)', 'Replace'],
    ['split', 're.split(pat, s)', 'Split'],
    ['compile', 're.compile(pat)', 'Compile regex'],
    ['IGNORECASE', 're.IGNORECASE', 'Ignore case'],
  ]

  const base64Members: Array<[string, string, string]> = [
    ['b64encode', 'base64.b64encode(b)', 'Encode to base64'],
    ['b64decode', 'base64.b64decode(s)', 'Decode from base64'],
  ]

  // ---------- ctx API ----------
  // ctx 在 on_request / on_response 中可用，提供请求/响应上下文
  const ctxProps: Array<[string, string, string]> = [
    // 请求阶段可用
    ['host', 'ctx.host', 'Target host (e.g. example.com)'],
    ['path', 'ctx.path', 'URL path (e.g. /api/user)'],
    ['method', 'ctx.method', 'HTTP method (GET/POST...)'],
    ['url', 'ctx.url', 'Full URL'],
    ['scheme', 'ctx.scheme', 'Scheme (http/https)'],
    ['pid', 'ctx.pid', 'Initiator process PID'],
    ['process_name', 'ctx.process_name', 'Initiator process name'],
    ['request_headers', 'ctx.request_headers', 'Request headers dict'],
    ['request_body', 'ctx.request_body', 'Request body bytes'],
    // 响应阶段额外可用
    ['status_code', 'ctx.status_code', 'Response status code (on_response only)'],
    ['response_headers', 'ctx.response_headers', 'Response headers dict (on_response only)'],
    ['response_body', 'ctx.response_body', 'Response body bytes (on_response only)'],
  ]

  const ctxMethods: Array<[string, string, string]> = [
    ['set_request_header', 'ctx.set_request_header(name, value)', 'Set/override request header'],
    ['set_request_body', 'ctx.set_request_body(bytes)', 'Set request body'],
    ['set_response_header', 'ctx.set_response_header(name, value)', 'Set/override response header (on_response only)'],
    ['set_response_body', 'ctx.set_response_body(bytes)', 'Set response body (on_response only)'],
    ['set_status_code', 'ctx.set_status_code(code)', 'Set response status code (on_response only)'],
  ]

  // ---------- 钩子函数模板 ----------
  const hookSnippets: Array<[string, string, string]> = [
    [
      'on_request',
      'def on_request(ctx):\n    ${1:pass}\n    return None',
      'Request hook: called before forwarding',
    ],
    [
      'on_response',
      'def on_response(ctx):\n    ${1:pass}\n    return None',
      'Response hook: called before returning to client',
    ],
    [
      'drop',
      '{"drop": True}',
      'Drop request (return in on_request)',
    ],
    [
      'mock',
      '{"mock": True, "status": 200, "headers": {}, "body": b""}',
      'Mock response (return in on_request)',
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
          makeItem(name, monaco.languages.CompletionItemKind.Snippet, body, 'Telnix hook', doc, true)
        )
      }
      // ctx 本身也提示
      suggestions.push(
        makeItem('ctx', monaco.languages.CompletionItemKind.Variable, 'ctx', 'Telnix ctx', 'Request/response context object')
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
