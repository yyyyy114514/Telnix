import fs from 'fs'
import path from 'path'

const root = 'c:/Users/Administrator/Downloads/Telnix-trae-agent-DnHAtu/src/ui/src'

function parseLocale(file) {
  let src = fs.readFileSync(file, 'utf8')
  src = src.replace(/export\s+default/, 'return')
  // eslint-disable-next-line no-new-func
  const fn = new Function(src)
  return fn()
}

function has(obj, key) {
  let cur = obj
  for (const k of key.split('.')) {
    if (cur && typeof cur === 'object' && k in cur) cur = cur[k]
    else return false
  }
  return true
}

const PREFIXES = ['raw', 'flowList', 'capture', 'inspector', 'common', 'analyze', 'nav', 'settings', 'breakpoint', 'replay', 'import', 'rule', 'hex', 'protocol', 'ai', 'search', 'send', 'codec', 'clash', 'dns', 'log', 'ws', 'autoReply', 'cert', 'breakpointBar', 'flows', 'winDivert']

function collectKeys(dir, keys) {
  for (const f of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, f.name)
    if (f.isDirectory()) collectKeys(p, keys)
    else if (/\.(vue|ts|js)$/.test(f.name)) {
      const txt = fs.readFileSync(p, 'utf8')
      let m
      // t() / $t() / tc() / te() / v-t() calls with static string key
      const re = /(?:\$t|[^.\w]t|tc|te|v-t)\(\s*['"]([^'"]+)['"]/g
      while ((m = re.exec(txt))) keys.add(m[1])
      // label: 'xxx.yyy' style literals (potential i18n keys in data arrays)
      const pRe = PREFIXES.join('|')
      const re2 = new RegExp("['\"](" + pRe + ")\\.[a-zA-Z0-9]+['\"]", 'g')
      while ((m = re2.exec(txt))) {
        keys.add(m[0].slice(1, -1)) // strip surrounding quotes
      }
    }
  }
}

const zh = parseLocale(path.join(root, 'locales/zh.ts'))
const en = parseLocale(path.join(root, 'locales/en.ts'))

const keys = new Set()
collectKeys(root, keys)

function walk(obj, prefix, out) {
  for (const k of Object.keys(obj)) {
    const v = obj[k]
    const np = prefix ? prefix + '.' + k : k
    if (v && typeof v === 'object') walk(v, np, out)
    else out.push({ key: np, val: v })
  }
}
const zhAll = [], enAll = []
walk(zh, '', zhAll)
walk(en, '', enAll)
const zhMap = Object.fromEntries(zhAll.map(x => [x.key, x.val]))
const enMap = Object.fromEntries(enAll.map(x => [x.key, x.val]))

const missingZh = [], missingEn = [], anomaly = []
for (const k of keys) {
  const z = zhMap[k], e = enMap[k]
  if (z === undefined) missingZh.push(k)
  if (e === undefined) missingEn.push(k)
  if (typeof z === 'string' && (z === k || z.trim() === '')) anomaly.push(k + `  [zh] => '${z}'`)
  if (typeof e === 'string' && (e === k || e.trim() === '')) anomaly.push(k + `  [en] => '${e}'`)
}

console.log('USED KEYS:', keys.size)
console.log('\n=== MISSING IN ZH ===')
console.log(missingZh.sort().join('\n') || '(none)')
console.log('\n=== MISSING IN EN ===')
console.log(missingEn.sort().join('\n') || '(none)')
console.log('\n=== ANOMALY (value === key or empty) ===')
console.log(anomaly.sort().join('\n') || '(none)')
