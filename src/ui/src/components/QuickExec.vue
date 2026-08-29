<script setup lang="ts">
import { ref, nextTick, onMounted, onUnmounted } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useCaptureStore } from '../stores/capture'
import { useFlowsStore } from '../stores/flows'
import { api, type RepeatResponse } from '../api/client'

const { t } = useI18n()
const capture = useCaptureStore()
const flows = useFlowsStore()

const input = ref('')
const inputRef = ref<HTMLInputElement | null>(null)
const showHelp = ref(false)
// 命令历史（上下箭头切换，持久化到 localStorage）
const history = ref<string[]>([])
const historyIdx = ref(-1)
const HISTORY_KEY = 'telnix_quickexec_history'
const CMD_HISTORY_MAX = 50

// 可用命令列表（用于自动补全提示）
const KNOWN_CMDS = [
  'clear', 'cls', 'rep', 'repeat', 'urlencode', 'ue', 'urldecode', 'ud',
  'base64', 'b64', 'b64d',
  'json', 'fmt',
  'hex', 'unhex',
  'md5', 'sha1', 'sha256', 'sha512',
  'time', 'timestamp',
  'help', '?',
  'goto', 'open',
  'session', 'sessions',
  'copy', 'req', 'resp', 'len', 'last', 'echo',
  'export', 'export all', 'grep',
  'size', 'drop',
]

// 从 localStorage 加载历史
function loadHistory() {
  try {
    const saved = localStorage.getItem(HISTORY_KEY)
    if (saved) history.value = JSON.parse(saved)
  } catch { /* ignore */ }
}
function saveHistory() {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(history.value)) } catch { /* ignore */ }
}

// 获取输入中最后一个 token 用于补全
function getLastToken(): string {
  const cmd = input.value.trim()
  const parts = cmd.split(/\s+/)
  return parts[parts.length - 1] || ''
}
function getPrevTokens(): string[] {
  const cmd = input.value.trim()
  const parts = cmd.split(/\s+/)
  return parts.slice(0, -1)
}

// 补全建议
const suggestions = ref<string[]>([])
function updateSuggestions() {
  const last = getLastToken()
  const prev = getPrevTokens()
  if (!last) { suggestions.value = []; return }
  if (prev.length === 0) {
    // 第一个 token：补全命令
    suggestions.value = KNOWN_CMDS.filter(c => c.startsWith(last.toLowerCase())).slice(0, 8)
  } else {
    suggestions.value = []
  }
}

function execute() {
  const cmd = input.value.trim()
  if (!cmd) return

  // 加入历史
  if (history.value[history.value.length - 1] !== cmd) {
    history.value.push(cmd)
    if (history.value.length > 50) history.value.shift()
    saveHistory()
  }
  historyIdx.value = -1

  const lower = cmd.toLowerCase()
  const parts = cmd.trim().split(/\s+/)
  const first = lower.split(/\s/)[0]
  const rest = cmd.slice(cmd.indexOf(' ') + 1).trim()

  // URL 编解码
  if (first === 'urlencode' || first === 'ue') {
    if (!rest) { ElMessage.warning('用法: urlencode <文本>'); return }
    input.value = encodeURIComponent(rest)
    return
  }
  if (first === 'urldecode' || first === 'ud') {
    if (!rest) { ElMessage.warning('用法: urldecode <文本>'); return }
    try {
      input.value = decodeURIComponent(rest)
    } catch {
      ElMessage.warning(t('quickexec.invalidUrl'))
    }
    return
  }

  // Base64 编码/解码
  if (first === 'base64' || first === 'b64') {
    if (!rest) { ElMessage.warning('用法: base64 <文本>'); return }
    try {
      input.value = btoa(unescape(encodeURIComponent(rest)))
      ElMessage.success('Base64 编码成功')
    } catch {
      ElMessage.error('Base64 编码失败')
    }
    return
  }
  if (first === 'b64d' || first === 'base64d') {
    if (!rest) { ElMessage.warning('用法: b64d <文本>'); return }
    try {
      input.value = decodeURIComponent(escape(atob(rest)))
      ElMessage.success('Base64 解码成功')
    } catch {
      ElMessage.error('Base64 解码失败：无效的输入')
    }
    return
  }

  // JSON 格式化
  if (first === 'json' || first === 'fmt') {
    if (!rest) { ElMessage.warning('用法: json <JSON文本>'); return }
    try {
      const obj = JSON.parse(rest)
      input.value = JSON.stringify(obj, null, 2)
      ElMessage.success('JSON 格式化成功')
    } catch (e) {
      ElMessage.error('JSON 解析失败：' + (e as Error).message)
    }
    return
  }

  // Hex 编码/解码
  if (first === 'hex') {
    if (!rest) { ElMessage.warning('用法: hex <文本>'); return }
    try {
      const arr: string[] = []
      for (const c of unescape(encodeURIComponent(rest))) {
        arr.push(c.charCodeAt(0).toString(16).padStart(2, '0'))
      }
      input.value = arr.join(' ')
      ElMessage.success('Hex 编码成功')
    } catch {
      ElMessage.error('Hex 编码失败')
    }
    return
  }
  if (first === 'unhex' || first === 'fromhex') {
    if (!rest) { ElMessage.warning('用法: unhex <hex文本>'); return }
    try {
      const hexStr = rest.replace(/\s+/g, '')
      if (!/^[0-9a-fA-F]*$/.test(hexStr)) throw new Error('Invalid hex')
      const arr = []
      for (let i = 0; i < hexStr.length; i += 2) {
        arr.push(String.fromCharCode(parseInt(hexStr.substr(i, 2), 16)))
      }
      input.value = arr.join('')
      ElMessage.success('Hex 解码成功')
    } catch {
      ElMessage.error('Hex 解码失败：无效的输入')
    }
    return
  }

  // MD5/SHA 哈希
  if (['md5', 'sha1', 'sha256', 'sha512'].includes(first)) {
    if (!rest) { ElMessage.warning(`用法: ${first} <文本>`); return }
    const algoMap: Record<string, string> = { md5: 'MD5', sha1: 'SHA-1', sha256: 'SHA-256', sha512: 'SHA-512' }
    const algo = algoMap[first]
    // SHA 系列使用 Web Crypto API
    if (algo.startsWith('SHA')) {
      textToHash(rest, algo).then(hash => {
        input.value = hash
        ElMessage.success(`${algo} 计算成功`)
      }).catch(() => {
        ElMessage.error(`${algo} 计算失败`)
      })
    } else {
      // MD5 不被 Web Crypto 支持，显示提示
      ElMessage.info('MD5 计算请使用编解码工具页')
    }
    return
  }

  // 时间戳转换
  if (first === 'time' || first === 'timestamp') {
    if (!rest) {
      // 显示当前时间戳
      const now = Date.now()
      input.value = `${now} (${new Date(now).toLocaleString('zh-CN')})`
    } else {
      // 尝试将输入转为时间戳或日期
      const num = parseInt(rest, 10)
      if (!isNaN(num) && String(num).length >= 10) {
        // 毫秒或秒级时间戳
        const ts = num > 9999999999 ? num : num * 1000
        input.value = new Date(ts).toLocaleString('zh-CN')
      } else {
        // 尝试解析为日期
        const d = new Date(rest)
        if (!isNaN(d.getTime())) {
          input.value = `${d.getTime()} (毫秒) / ${Math.floor(d.getTime() / 1000)} (秒)`
        } else {
          ElMessage.warning('无法解析日期，请输入有效的时间戳或日期字符串')
        }
      }
    }
    return
  }

  // 清除当前会话
  if (lower === 'clear' || lower === 'cls') {
    capture.clearSessions().then(() => {
      ElMessage.success(t('quickexec.cleared'))
    })
    input.value = ''
    return
  }

  // 切换会话
  if (first === 'session' || first === 'sessions') {
    if (!rest) {
      // 显示当前会话信息
      const sid = capture.status.session_id
      const count = flows.flows.length
      input.value = `当前会话: ${sid || '无'}, 流量数: ${count}`
    } else {
      // 切换到指定会话
      const sid = parseInt(rest, 10)
      if (!isNaN(sid)) {
        flows.loadFlows(sid).then(() => {
          ElMessage.success(`已切换到会话 ${sid}`)
        }).catch((e: any) => {
          ElMessage.error('切换会话失败: ' + (e?.message || e))
        })
      } else {
        ElMessage.warning('请输入有效的会话 ID')
      }
    }
    input.value = ''
    return
  }

  // 跳转到指定页面
  if (first === 'goto' || first === 'open') {
    if (!rest) { ElMessage.warning('用法: goto <页面路径>'); return }
    window.location.hash = '#' + rest
    input.value = ''
    return
  }

  // rep N：重放选中请求 N 次
  const repMatch = lower.match(/^rep(?:eat)?\s+(\d+)$/)
  if (repMatch) {
    const count = parseInt(repMatch[1], 10)
    if (!flows.selectedId) {
      ElMessage.warning(t('quickexec.noSelection'))
      return
    }
    doRepeat(flows.selectedId, count)
    input.value = ''
    return
  }

  // help：显示帮助
  if (lower === 'help' || lower === '?') {
    showHelp.value = !showHelp.value
    input.value = ''
    return
  }

  // copy：复制当前选中请求/响应到剪贴板
  if (first === 'copy' || first === 'cp') {
    const target = rest || 'all'
    const selectedFlow = flows.flows.find(f => f.id === flows.selectedId)
    if (!selectedFlow) {
      ElMessage.warning(t('quickexec.noSelection'))
      return
    }
    let text = ''
    if (target === 'req' || target === 'request') {
      text = selectedFlow.request_body || ''
    } else if (target === 'resp' || target === 'response') {
      text = selectedFlow.response_body || ''
    } else {
      text = JSON.stringify(selectedFlow, null, 2)
    }
    navigator.clipboard.writeText(text).then(() => {
      ElMessage.success(t('common.copied'))
    }).catch(() => {
      ElMessage.error(t('common.copyFailed'))
    })
    input.value = ''
    return
  }

  // echo：回显输入内容
  if (first === 'echo') {
    input.value = rest
    return
  }

  // len：显示选中请求体长度
  if (first === 'len') {
    const selectedFlow = flows.flows.find(f => f.id === flows.selectedId)
    if (!selectedFlow) {
      ElMessage.warning(t('quickexec.noSelection'))
      return
    }
    const reqLen = selectedFlow.request_body?.length || 0
    const respLen = selectedFlow.response_body?.length || 0
    input.value = `req: ${reqLen} bytes / resp: ${respLen} bytes`
    return
  }

  // last：跳转到最新的包
  if (first === 'last') {
    if (flows.flows.length === 0) {
      ElMessage.warning('暂无流量')
      return
    }
    const latest = flows.flows[flows.flows.length - 1]
    flows.selectFlow(latest)
    window.dispatchEvent(new CustomEvent('telnix:scroll-to-flow', { detail: latest.id }))
    input.value = ''
    return
  }

  // export：导出流量
  if (first === 'export' || lower === 'export all') {
    const selectedFlow = flows.flows.find(f => f.id === flows.selectedId)
    if (lower === 'export all') {
      // 导出全部
      window.dispatchEvent(new CustomEvent('telnix:export-flows', { detail: { all: true } }))
      ElMessage.info('正在导出全部流量...')
    } else if (selectedFlow) {
      window.dispatchEvent(new CustomEvent('telnix:export-flows', { detail: { id: selectedFlow.id } }))
      ElMessage.info('正在导出流量...')
    } else {
      ElMessage.warning('请先选择要导出的流量')
    }
    input.value = ''
    return
  }

  // size：显示流量大小统计
  if (first === 'size') {
    const total = flows.flows.reduce((sum, f) => {
      return sum + (f.request_body?.length || 0) + (f.response_body?.length || 0)
    }, 0)
    const count = flows.flows.length
    input.value = `共 ${count} 个流量，总计 ${formatBytes(total)}`
    return
  }

  // drop：删除选中的流量
  if (first === 'drop') {
    if (!flows.selectedId) {
      ElMessage.warning(t('quickexec.noSelection'))
      return
    }
    window.dispatchEvent(new CustomEvent('telnix:delete-flow', { detail: flows.selectedId }))
    input.value = ''
    return
  }

  // 默认：作为过滤条件发送给 FlowList
  window.dispatchEvent(new CustomEvent('telnix:set-flow-filter', { detail: cmd }))
}

async function textToHash(text: string, algo: string): Promise<string> {
  const encoder = new TextEncoder()
  const data = encoder.encode(text)
  const hashBuffer = await crypto.subtle.digest(algo as any, data)
  const hashArray = Array.from(new Uint8Array(hashBuffer))
  return hashArray.map(b => b.toString(16).padStart(2, '0')).join('')
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB'
}

async function doRepeat(flowId: number, count: number) {
  try {
    const res = await api.repeatFlow(flowId, count, 1, 0)
    const ok = res.stats.success
    const fail = res.stats.fail
    ElMessage.success(t('quickexec.repeatDone', { ok, fail, total: count }))
  } catch (e: any) {
    ElMessage.error(t('quickexec.repeatFail') + (e.message || e))
  }
}

function copyCmd(cmd: string) {
  navigator.clipboard.writeText(cmd).then(() => {
    ElMessage.success(t('common.copied'))
  }).catch(() => {
    ElMessage.error(t('common.copyFailed'))
  })
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter') {
    e.preventDefault()
    execute()
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    if (history.value.length === 0) return
    if (historyIdx.value === -1) historyIdx.value = history.value.length - 1
    else if (historyIdx.value > 0) historyIdx.value--
    input.value = history.value[historyIdx.value] || ''
  } else if (e.key === 'ArrowDown') {
    e.preventDefault()
    if (historyIdx.value === -1) return
    if (historyIdx.value < history.value.length - 1) {
      historyIdx.value++
      input.value = history.value[historyIdx.value] || ''
    } else {
      historyIdx.value = -1
      input.value = ''
    }
  } else if (e.key === 'Escape') {
    input.value = ''
    showHelp.value = false
    inputRef.value?.blur()
  }
}

// 全局 / 快捷键聚焦
function onGlobalKeydown(e: KeyboardEvent) {
  if (e.key === '/' && document.activeElement?.tagName !== 'INPUT' && document.activeElement?.tagName !== 'TEXTAREA') {
    // 不在输入框时，/ 聚焦 QuickExec
    e.preventDefault()
    nextTick(() => inputRef.value?.focus())
  }
}

onMounted(() => {
  loadHistory()
  window.addEventListener('keydown', onGlobalKeydown)
})
onUnmounted(() => {
  window.removeEventListener('keydown', onGlobalKeydown)
})
</script>

<template>
  <div class="quickexec-bar">
    <span class="quickexec-prompt">›</span>
    <input
      ref="inputRef"
      v-model="input"
      class="quickexec-input"
      :placeholder="t('quickexec.placeholder')"
      spellcheck="false"
      autocomplete="off"
      @keydown="onKeydown"
      @input="updateSuggestions"
      @focus="updateSuggestions"
    />
    <span class="quickexec-hint" @click="showHelp = !showHelp">?</span>
    <!-- 补全建议 -->
    <div v-if="suggestions.length" class="quickexec-suggestions">
      <div
        v-for="s in suggestions"
        :key="s"
        class="suggestion-item"
        @mousedown.prevent="input = s + ' '"
      >{{ s }}</div>
    </div>
    <div v-if="showHelp" class="quickexec-help no-select">
      <div class="help-title">{{ t('quickexec.helpTitle') }}</div>
      <div class="help-row"><code @click="copyCmd('keyword')">keyword</code> <span>{{ t('quickexec.helpFilter') }}</span></div>
      <div class="help-row"><code @click="copyCmd('clear')">clear / cls</code> <span>{{ t('quickexec.helpClear') }}</span></div>
      <div class="help-row"><code @click="copyCmd('rep N')">rep N</code> <span>{{ t('quickexec.helpRep') }}</span></div>
      <div class="help-row"><code @click="copyCmd('urlencode')">urlencode / ue text</code> <span>{{ t('quickexec.helpUrlencode') }}</span></div>
      <div class="help-row"><code @click="copyCmd('urldecode')">urldecode / ud text</code> <span>{{ t('quickexec.helpUrldecode') }}</span></div>
      <div class="help-row"><code @click="copyCmd('base64')">base64 / b64 text</code> <span>{{ t('quickexec.helpBase64') }}</span></div>
      <div class="help-row"><code @click="copyCmd('b64d')">b64d text</code> <span>{{ t('quickexec.helpBase64Decode') }}</span></div>
      <div class="help-row"><code @click="copyCmd('json')">json / fmt text</code> <span>{{ t('quickexec.helpJson') }}</span></div>
      <div class="help-row"><code @click="copyCmd('hex')">hex text</code> <span>{{ t('quickexec.helpHex') }}</span></div>
      <div class="help-row"><code @click="copyCmd('unhex')">unhex text</code> <span>{{ t('quickexec.helpUnhex') }}</span></div>
      <div class="help-row"><code @click="copyCmd('md5')">md5 text</code> <span>{{ t('quickexec.helpMd5') }}</span></div>
      <div class="help-row"><code @click="copyCmd('sha1')">sha1 text</code> <span>{{ t('quickexec.helpSha1') }}</span></div>
      <div class="help-row"><code @click="copyCmd('sha256')">sha256 text</code> <span>{{ t('quickexec.helpSha256') }}</span></div>
      <div class="help-row"><code @click="copyCmd('sha512')">sha512 text</code> <span>{{ t('quickexec.helpSha512') }}</span></div>
      <div class="help-row"><code @click="copyCmd('time')">time [ts|date]</code> <span>{{ t('quickexec.helpTime') }}</span></div>
      <div class="help-row"><code @click="copyCmd('session')">session [id]</code> <span>{{ t('quickexec.helpSession') }}</span></div>
      <div class="help-row"><code @click="copyCmd('sessions')">sessions</code> <span>{{ t('quickexec.helpSessions') }}</span></div>
      <div class="help-row"><code @click="copyCmd('goto')">goto / open path</code> <span>{{ t('quickexec.helpGoto') }}</span></div>
      <div class="help-row"><code @click="copyCmd('copy')">copy [req/resp]</code> <span>{{ t('quickexec.helpCopy') }}</span></div>
      <div class="help-row"><code @click="copyCmd('req')">req / resp</code> <span>{{ t('quickexec.helpReq') }}</span></div>
      <div class="help-row"><code @click="copyCmd('len')">len</code> <span>{{ t('quickexec.helpLen') }}</span></div>
      <div class="help-row"><code @click="copyCmd('last')">last</code> <span>{{ t('quickexec.helpLast') }}</span></div>
      <div class="help-row"><code @click="copyCmd('size')">size</code> <span>{{ t('quickexec.helpSize') }}</span></div>
      <div class="help-row"><code @click="copyCmd('export')">export</code> <span>{{ t('quickexec.helpExport') }}</span></div>
      <div class="help-row"><code @click="copyCmd('export all')">export all</code> <span>{{ t('quickexec.helpExportAll') }}</span></div>
      <div class="help-row"><code @click="copyCmd('grep')">grep pattern</code> <span>{{ t('quickexec.helpGrep') }}</span></div>
      <div class="help-row"><code @click="copyCmd('drop')">drop</code> <span>{{ t('quickexec.helpDrop') }}</span></div>
      <div class="help-row"><code @click="copyCmd('echo')">echo text</code> <span>{{ t('quickexec.helpEcho') }}</span></div>
      <div class="help-row"><code @click="copyCmd('help')">help / ?</code> <span>{{ t('quickexec.helpHelp') }}</span></div>
      <div class="help-tip">{{ t('quickexec.helpTip') }}</div>
    </div>
  </div>
</template>

<style scoped>
.quickexec-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 12px;
  border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
  position: relative;
}
.quickexec-prompt {
  color: var(--on-accent);
  font-weight: 700;
  font-size: 14px;
  flex-shrink: 0;
}
.quickexec-input {
  flex: 1;
  border: none;
  outline: none;
  background: transparent;
  color: var(--on-text);
  font-size: 12px;
  font-family: 'Consolas', 'Monaco', monospace;
  padding: 2px 0;
}
.quickexec-input::placeholder {
  color: var(--on-text-dim, #888);
}
.quickexec-hint {
  color: var(--on-text-dim, #888);
  cursor: pointer;
  font-size: 12px;
  padding: 0 4px;
  flex-shrink: 0;
}
.quickexec-hint:hover {
  color: var(--on-accent);
}
.quickexec-help {
  position: absolute;
  top: 100%;
  right: 12px;
  z-index: 100;
  background: var(--on-bg-card, var(--on-bg-elevated));
  border: 1px solid var(--on-border);
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 11px;
  min-width: 280px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  color: var(--on-text);
}
.quickexec-suggestions {
  position: absolute;
  top: 100%;
  right: 36px;
  z-index: 100;
  background: var(--on-bg-card, var(--on-bg-elevated));
  border: 1px solid var(--on-border);
  border-radius: 6px;
  padding: 4px 0;
  min-width: 140px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.12);
}
.suggestion-item {
  padding: 3px 12px;
  font-size: 11px;
  color: var(--on-accent);
  cursor: pointer;
  font-family: 'Consolas', monospace;
  white-space: nowrap;
}
.suggestion-item:hover {
  background: var(--on-bg-hover);
}
.help-title {
  font-weight: 600;
  margin-bottom: 6px;
  color: var(--on-text);
}
.help-row {
  display: flex;
  gap: 8px;
  margin: 3px 0;
  align-items: baseline;
}
.help-row code {
  color: var(--on-accent);
  font-family: 'Consolas', 'Monaco', monospace;
  white-space: nowrap;
  min-width: 120px;
}
.help-row span {
  color: var(--on-text-dim, #888);
}
.help-tip {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px solid var(--on-border-light);
  color: var(--on-text-muted);
  font-size: 10px;
}
.no-select {
  user-select: none;
  -webkit-user-select: none;
}
.help-row code {
  cursor: pointer;
  transition: opacity 0.15s;
}
.help-row code:hover {
  opacity: 0.7;
}
</style>
