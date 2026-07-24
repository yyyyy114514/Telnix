/**
 * Monaco Editor 按需加载器（CDN 模式）
 *
 * monaco-editor 体积大（5MB+，几千个模块），无论静态还是动态导入都会严重拖慢构建。
 * 改用 @guolao/vue-monaco-editor 的 loader 从 CDN 加载，monaco-editor 不参与构建。
 *
 * Python 语言定义：monaco-editor 的 python.js 子路径在 CDN 下不好访问，
 * 改为自己注册一个简单的 Monarch tokenizer（足够语法高亮用）。
 */

import { loader } from '@guolao/vue-monaco-editor'
import type * as Monaco from 'monaco-editor'

let configured = false

/**
 * 配置 Monaco loader（从 CDN 加载）+ 注册 Python 语言定义。
 * 幂等，多次调用安全。返回 void（loader 内部管理 monaco 实例）。
 *
 * 组件用法：
 *   import { loader } from '@guolao/vue-monaco-editor'
 *   const editor = ... // loader 会自动从 CDN 加载 monaco
 */
export function setupMonaco() {
  if (configured) return
  configured = true

  // loader 默认从 CDN（jsdelivr）加载 monaco-editor，不需要显式 config。
  // 如果需要指定 CDN 路径，可以取消下面注释：
  // loader.config({ paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs' } })

  // Python Monarch tokenizer：注册自定义语言定义（不依赖 monaco-editor 的 python.js）
  loader.init().then((monaco) => {
    registerPythonLanguage(monaco)
  }).catch(() => { /* 忽略，loader 会重试 */ })
}

/**
 * 注册 Python 语言定义（Monarch tokenizer + 语言配置）。
 * 简化版，覆盖常见 Python 语法高亮需求。
 */
function registerPythonLanguage(monaco: typeof Monaco) {
  // 如果已注册则跳过
  try {
    // 语言配置：括号配对、自动缩进、注释
    monaco.languages.setLanguageConfiguration('python', {
      comments: {
        lineComment: '#',
        blockComment: ['"""', '"""'],
      },
      brackets: [
        ['{', '}'],
        ['[', ']'],
        ['(', ')'],
      ],
      autoClosingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"', notIn: ['string'] },
        { open: "'", close: "'", notIn: ['string', 'comment'] },
        { open: '"""', close: '"""', notIn: ['string', 'comment'] },
        { open: "'''", close: "'''", notIn: ['string', 'comment'] },
      ],
      surroundingPairs: [
        { open: '{', close: '}' },
        { open: '[', close: ']' },
        { open: '(', close: ')' },
        { open: '"', close: '"' },
        { open: "'", close: "'" },
        { open: '"""', close: '"""' },
        { open: "'''", close: "'''" },
      ],
      onEnterRules: [
        {
          beforeText: /^\s*(?:def|class|for|if|elif|else|while|try|except|finally|with|async\s+def)\b.*:\s*$/,
          action: { indentAction: monaco.languages.IndentAction.Indent },
        },
      ],
      folding: {
        offSide: true,
        markers: {
          start: /^\s*#region\b/,
          end: /^\s*#endregion\b/,
        },
      },
    })

    // Monarch tokenizer：Python 语法高亮规则
    monaco.languages.registerTokensProviderFactory('python', {
      create: async () => ({
        defaultToken: '',
        tokenPostfix: '.python',
        keywords: [
          'and', 'as', 'assert', 'async', 'await', 'break', 'class', 'continue',
          'def', 'del', 'elif', 'else', 'except', 'False', 'finally', 'for',
          'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'None',
          'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'True', 'try',
          'while', 'with', 'yield', 'self', 'cls',
        ],
        operators: [
          '+', '-', '*', '**', '/', '//', '%', '@', '<<', '>>', '&', '|', '^',
          '~', '<', '>', '<=', '>=', '==', '!=', '=', '+=', '-=', '*=', '/=',
          '//=', '%=', '**=', '>>=', '<<=', '&=', '|=', '^=', '@=', ':=',
        ],
        symbols: /[=><!~?:&|+\-*/^%]+/,
        escapes: /\\(?:[abfnrtv\\"']|x[0-9A-Fa-f]{1,4}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})/,
        digits: /\d+(_+\d+)*/,
        octaldigits: /[0-7]?(_+[0-7])*$/,
        binarydigits: /[0-1]?(_+[0-1])*$/,
        hexdigits: /[[0-9a-fA-F]?(_+[0-9a-fA-F])*$/,
        tokenizer: {
          root: [
            [/[a-zA-Z_]\w*/, {
              cases: {
                '@keywords': 'keyword',
                '@default': 'identifier',
              },
            }],
            { include: '@whitespace' },
            [/\d+(\.\d+)?([eE][+-]?\d+)?/, 'number'],
            [/0[xX][0-9a-fA-F]+/, 'number.hex'],
            [/0[oO][0-7]+/, 'number.octal'],
            [/0[bB][01]+/, 'number.binary'],
            [/"""/, { token: 'string', next: '@stringTriple' }],
            [/'''/, { token: 'string', next: '@stringTripleSingle' }],
            [/"/, { token: 'string', next: '@string' }],
            [/'/, { token: 'string', next: '@stringSingle' }],
            [/[{}()[\]]/, '@brackets'],
            [/@symbols/, {
              cases: {
                '@operators': 'operator',
                '@default': '',
              },
            }],
            [/#region\b/, 'comment'],
            [/#endregion\b/, 'comment'],
          ],
          whitespace: [
            [/\s+/, 'white'],
            [/#.*$/, 'comment'],
          ],
          string: [
            [/[^\\"]+/, 'string'],
            [/@escapes/, 'string.escape'],
            [/\\./, 'string.escape.invalid'],
            [/"/, { token: 'string', next: '@pop' }],
          ],
          stringSingle: [
            [/[^\\']+/, 'string'],
            [/@escapes/, 'string.escape'],
            [/\\./, 'string.escape.invalid'],
            [/'/, { token: 'string', next: '@pop' }],
          ],
          stringTriple: [
            [/[^\\"]+/, 'string'],
            [/@escapes/, 'string.escape'],
            [/\\./, 'string.escape.invalid'],
            [/"""/, { token: 'string', next: '@pop' }],
            [/"/, 'string'],
          ],
          stringTripleSingle: [
            [/[^\\']+/, 'string'],
            [/@escapes/, 'string.escape'],
            [/\\./, 'string.escape.invalid'],
            [/'''/, { token: 'string', next: '@pop' }],
            [/'/, 'string'],
          ],
        },
      }),
    })
  } catch {
    // 已注册则忽略
  }
}
