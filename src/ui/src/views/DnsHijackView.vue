<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Key, Plus, Refresh } from '@element-plus/icons-vue'
import { api, type DnsHijackStatus } from '../api/client'

// DNS 劫持页：基于 WinDivert 的本地 DNS 响应篡改
// 卡片式布局：顶部状态卡 + 规则编辑表 + 实时日志
const status = ref<DnsHijackStatus | null>(null)
const loading = ref(false)
const toggling = ref(false)
const saving = ref(false)
// 管理员重启中（调用 /system/restart-as-admin 期间）
const restartingAsAdmin = ref(false)

// 规则编辑（前端独立维护，保存时整体提交）
interface Rule {
  domain: string
  ip: string
}
const rules = ref<Rule[]>([])
const defaultIp = ref('')

// dirty 标志：本地有未保存修改时为 true，避免被轮询/刷新覆盖
const dirty = ref(false)

// 实时日志（从后端 status.log 同步）
const logs = ref<DnsHijackStatus['log']>([])

let pollTimer: number | null = null

// 拉取后端状态；poll=false 时会同步规则到本地编辑器（仅在无 dirty 时）
async function refresh(opts: { silent?: boolean; syncRules?: boolean } = {}) {
  const { silent = false, syncRules = false } = opts
  if (!silent) loading.value = true
  try {
    const s = await api.dnsHijackStatus()
    // 状态字段始终更新（轮询只更新这些）
    status.value = s
    logs.value = s.log || []
    // 仅在显式请求且无本地未保存修改时同步规则
    if (syncRules && !dirty.value) {
      const entries = Object.entries(s.rules || {})
      rules.value = entries.map(([domain, ip]) => ({ domain, ip }))
      defaultIp.value = s.default_ip || ''
    }
  } catch (e: any) {
    ElMessage.error('获取状态失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

async function onToggle(val: boolean | string | number) {
  if (val === true) {
    await startHijack()
  } else {
    await stopHijack()
  }
}

async function startHijack() {
  toggling.value = true
  try {
    const body = buildBody()
    await api.dnsHijackStart(body)
    ElMessage.success('DNS 劫持已启动')
    dirty.value = false // 启动时已提交，本地与后端一致
    await refresh({ syncRules: true })
  } catch (e: any) {
    ElMessage.error('启动失败: ' + e.message)
    await refresh({ syncRules: true })
  } finally {
    toggling.value = false
  }
}

async function stopHijack() {
  toggling.value = true
  try {
    await api.dnsHijackStop()
    ElMessage.info('DNS 劫持已停止')
    dirty.value = false
    await refresh({ syncRules: true })
  } catch (e: any) {
    ElMessage.error('停止失败: ' + e.message)
  } finally {
    toggling.value = false
  }
}

function buildBody() {
  // 把规则数组转为 {domain: ip} 字典，过滤空行
  const dict: Record<string, string> = {}
  for (const r of rules.value) {
    const d = r.domain.trim()
    const ip = r.ip.trim()
    if (d && ip) dict[d] = ip
  }
  return { rules: dict, default_ip: defaultIp.value.trim() }
}

async function saveRules() {
  saving.value = true
  try {
    const body = buildBody()
    const r = await api.dnsHijackSetRules(body)
    ElMessage.success(`已保存 ${Object.keys(body.rules).length} 条规则` + (body.default_ip ? `，默认 IP: ${body.default_ip}` : ''))
    // 保存成功后从后端回填规范化的规则（小写化、去空白）
    const entries = Object.entries(r.rules || {})
    rules.value = entries.map(([domain, ip]) => ({ domain, ip }))
    defaultIp.value = r.default_ip || ''
    status.value = r
    logs.value = r.log || []
    dirty.value = false
  } catch (e: any) {
    ElMessage.error('保存失败: ' + e.message)
  } finally {
    saving.value = false
  }
}

function addRule() {
  rules.value.push({ domain: '', ip: '' })
  dirty.value = true
}

function removeRule(idx: number) {
  rules.value.splice(idx, 1)
  dirty.value = true
}

// 输入框修改时标记 dirty
function onRuleInput() {
  dirty.value = true
}

// 主动刷新按钮：有未保存修改时提示丢弃，否则正常同步规则
async function onRefreshClick() {
  if (dirty.value) {
    try {
      await ElMessageBox.confirm('有未保存的修改，是否丢弃本地改动并从后端重新加载？', '丢弃修改', {
        confirmButtonText: '丢弃并刷新', cancelButtonText: '取消', type: 'warning',
      })
    } catch {
      return
    }
    dirty.value = false
  }
  await refresh({ syncRules: true })
}

// 以管理员身份重启 Telnix（WinDivert 需要管理员权限）
async function restartAsAdmin() {
  restartingAsAdmin.value = true
  try {
    await api.restartAsAdmin()
    ElMessage.success('正在以管理员身份重启，请稍候...')
  } catch (e: any) {
    ElMessage.error('重启失败：' + (e?.message || e))
    restartingAsAdmin.value = false
  }
}

async function clearLog() {
  try {
    await ElMessageBox.confirm('确定清空劫持日志和统计计数器？', '清空日志', {
      confirmButtonText: '清空', cancelButtonText: '取消', type: 'warning',
    })
  } catch {
    return
  }
  try {
    await api.dnsHijackClearLog()
    ElMessage.success('已清空')
    await refresh({ silent: true })
  } catch (e: any) {
    ElMessage.error('清空失败: ' + e.message)
  }
}

const isAdmin = computed(() => status.value?.is_admin ?? false)
const isWindows = computed(() => status.value?.is_windows ?? true)
const running = computed(() => status.value?.running ?? false)
const stats = computed(() => status.value?.stats || { total_packets: 0, hijacked_packets: 0, skipped_no_match: 0, errors: 0 })
const backend = computed(() => status.value?.backend || (isWindows.value ? 'windivert' : 'none'))
const supported = computed(() => status.value?.supported ?? isWindows.value)

// 后端友好显示名
const backendDisplayName = computed(() => {
  const b = backend.value
  if (b === 'windivert') return 'WinDivert'
  if (b === 'iptables+local_dns') return 'iptables + 本地 DNS'
  if (b === 'pf+local_dns') return 'pf + 本地 DNS'
  return 'DNS 劫持'
})

// 权限术语：Windows 用"管理员"，Unix 用"root"
const adminTerm = computed(() => isWindows.value ? '管理员' : 'root')

const canStart = computed(() => supported.value && isAdmin.value)

function formatTime(ts: string) {
  if (!ts) return ''
  return new Date(ts).toLocaleTimeString('zh-CN', { hour12: false })
}

onMounted(() => {
  refresh({ syncRules: true })
  // 3 秒轮询：只更新 stats/logs/running 等状态字段，不触碰 rules
  pollTimer = window.setInterval(() => {
    if (running.value) refresh({ silent: true })
  }, 3000)
})

onUnmounted(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
})
</script>

<template>
  <div class="dns-view full flex flex-col">
    <div class="dns-toolbar">
      <span class="dns-title">DNS 劫持</span>
      <span v-if="dirty" class="dirty-badge">未保存</span>
      <div class="flex-1"></div>
      <!-- 非管理员/root：提供提权重启按钮 -->
      <el-button
        v-if="supported && !isAdmin"
        size="small"
        type="warning"
        :loading="restartingAsAdmin"
        @click="restartAsAdmin"
      >
        <el-icon><Key /></el-icon>&nbsp;{{ isWindows ? '管理员重启' : '提权重启' }}
      </el-button>
      <el-button size="small" @click="onRefreshClick" :loading="loading">
        <el-icon><Refresh /></el-icon>&nbsp;刷新
      </el-button>
      <el-button size="small" type="danger" @click="clearLog">
        <el-icon><Delete /></el-icon>&nbsp;清空日志
      </el-button>
    </div>

    <div class="dns-body flex-1 overflow-auto">
      <!-- 顶部状态卡 -->
      <div class="dns-card" v-loading="loading">
        <div class="dns-card-header">
          <div class="dns-card-title">
            <div class="title-text">DNS 劫持开关</div>
            <div class="title-sub">{{ backendDisplayName }} · 修改本机 DNS 响应中的 A 记录</div>
          </div>
          <el-switch
            :model-value="running"
            :loading="toggling"
            :disabled="!canStart"
            @change="onToggle"
            class="dns-switch"
          />
        </div>
        <div class="dns-card-body">
          <div class="dns-status-tags">
            <el-tag size="small" :type="isAdmin ? 'success' : 'danger'">
              {{ isAdmin ? adminTerm : `非${adminTerm}` }}
            </el-tag>
            <el-tag v-if="backend !== 'none'" size="small" type="info">
              {{ backendDisplayName }}
            </el-tag>
            <el-tag v-if="running" size="small" type="success">劫持中</el-tag>
          </div>
          <div v-if="!supported" class="dns-hint warn" style="margin-top: 10px">
            <el-icon><WarningFilled /></el-icon>&nbsp;当前平台不支持 DNS 劫持
          </div>
          <div v-else-if="!isAdmin" class="dns-hint warn" style="margin-top: 10px">
            <el-icon><WarningFilled /></el-icon>&nbsp;需要{{ adminTerm }}权限，请用{{ adminTerm }}身份重启 Telnix
          </div>
          <div v-else-if="status?.last_error" class="dns-hint err" style="margin-top: 10px">
            <el-icon><CircleCloseFilled /></el-icon>&nbsp;{{ status.last_error }}
          </div>
          <div v-else-if="running" class="dns-hint ok" style="margin-top: 10px">
            <el-icon><CircleCheckFilled /></el-icon>&nbsp;劫持运行中，规则已生效
          </div>
          <div v-else class="dns-hint" style="margin-top: 10px">
            <el-icon><InfoFilled /></el-icon>&nbsp;已就绪，编辑规则后点击开关启动
          </div>

          <!-- 统计 -->
          <div class="dns-stats" v-if="running || stats.total_packets > 0">
            <div class="stat-item">
              <div class="stat-label">截获包</div>
              <div class="stat-val mono">{{ stats.total_packets }}</div>
            </div>
            <div class="stat-item hl">
              <div class="stat-label">已劫持</div>
              <div class="stat-val mono">{{ stats.hijacked_packets }}</div>
            </div>
            <div class="stat-item">
              <div class="stat-label">未匹配</div>
              <div class="stat-val mono">{{ stats.skipped_no_match }}</div>
            </div>
            <div class="stat-item">
              <div class="stat-label">错误</div>
              <div class="stat-val mono">{{ stats.errors }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 规则编辑区 -->
      <div class="dns-card">
        <div class="dns-card-header">
          <div class="dns-card-title">
            <div class="title-text">劫持规则</div>
            <div class="title-sub">域名 → IP 映射；支持 <code>*.example.com</code>、<code>*baidu*</code> 通配；留空则跳过</div>
          </div>
          <div class="dns-card-actions">
            <el-button size="small" @click="addRule" type="primary" plain>
              <el-icon><Plus /></el-icon>&nbsp;新增
            </el-button>
            <el-button size="small" @click="saveRules" :loading="saving" type="primary">
              <el-icon><Check /></el-icon>&nbsp;保存
            </el-button>
          </div>
        </div>
        <div class="dns-card-body">
          <div class="rules-table">
            <div class="rule-row rule-header">
              <div class="rule-cell c-domain">域名</div>
              <div class="rule-cell c-ip">劫持 IP</div>
              <div class="rule-cell c-op">操作</div>
            </div>
            <div v-if="!rules.length" class="empty-text text-dim rule-empty">
              （无规则，点击「新增」添加；或仅设置默认 IP 劫持所有域名）
            </div>
            <div v-for="(r, i) in rules" :key="i" class="rule-row">
              <div class="rule-cell c-domain">
                <el-input v-model="r.domain" size="small" placeholder="example.com / *.baidu.com / *baidu*" @input="onRuleInput" />
              </div>
              <div class="rule-cell c-ip">
                <el-input v-model="r.ip" size="small" placeholder="127.0.0.1" class="mono" @input="onRuleInput" />
              </div>
              <div class="rule-cell c-op">
                <el-button size="small" circle @click="removeRule(i)" type="danger" plain>
                  <el-icon><Delete /></el-icon>
                </el-button>
              </div>
            </div>
          </div>

          <div class="default-ip-row">
            <label class="default-ip-label">默认劫持 IP</label>
            <el-input v-model="defaultIp" size="small" placeholder="留空则只劫持规则中明确列出的域名" class="mono default-ip-input" @input="onRuleInput" />
            <span class="text-dim default-ip-hint">未匹配规则的 A 记录查询都会返回此 IP</span>
          </div>
        </div>
      </div>

      <!-- 劫持日志 -->
      <div class="dns-card">
        <div class="dns-card-header">
          <div class="dns-card-title">
            <div class="title-text">劫持日志</div>
            <div class="title-sub">最近 200 条劫持记录</div>
          </div>
        </div>
        <div class="dns-card-body">
          <div v-if="!logs.length" class="empty-text text-dim">（暂无劫持记录）</div>
          <div v-else class="log-list">
            <div v-for="(l, i) in logs" :key="i" class="log-row">
              <span class="log-ts mono text-dim">{{ formatTime(l.ts) }}</span>
              <span class="log-domain mono">{{ l.domain }}</span>
              <span class="log-arrow">→</span>
              <span class="log-new mono hl">{{ l.new_ip }}</span>
              <span class="log-old text-dim mono">（原 {{ l.original_ips.join(', ') }}）</span>
              <span class="log-src text-dim mono">{{ l.dns_server }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.dns-view { background: var(--on-bg); display: flex; flex-direction: column; }
.dns-toolbar {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px; border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated);
}
.dns-title { font-size: 14px; font-weight: 600; color: var(--on-text); }
.dirty-badge {
  font-size: 11px;
  padding: 1px 7px;
  border-radius: 10px;
  background: var(--on-amber-glow, rgba(245, 158, 11, 0.15));
  color: var(--on-amber, #fbbf24);
  border: 1px solid var(--on-amber, #fbbf24);
  font-weight: 500;
  user-select: none;
}
.dns-body { padding: 12px 16px; display: flex; flex-direction: column; gap: 14px; }

.dns-card {
  background: var(--on-bg-card);
  border: 1px solid var(--on-border-light);
  border-radius: 8px;
  overflow: hidden;
}
.dns-card-header {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--on-border-light);
  background: var(--on-bg-elevated, var(--on-bg-card));
}
.dns-card-title { flex: 1; min-width: 0; }
.title-text { font-size: 14px; font-weight: 600; color: var(--on-text); }
.title-sub { font-size: 12px; color: var(--on-text-dim); margin-top: 2px; }
.dns-card-actions { display: flex; gap: 8px; flex-shrink: 0; }
.dns-card-body { padding: 14px 16px; }

.dns-status-tags {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 4px;
}

.dns-hint {
  display: flex; align-items: center;
  padding: 8px 12px; border-radius: 4px;
  background: rgba(107, 114, 128, 0.08);
  color: var(--on-text-muted);
  font-size: 13px;
}
.dns-hint.ok { background: rgba(46, 160, 67, 0.1); color: var(--on-success); }
.dns-hint.warn { background: rgba(210, 153, 34, 0.1); color: var(--on-warn); }
.dns-hint.err { background: rgba(248, 81, 73, 0.1); color: var(--on-error); }

.dns-stats {
  display: flex; gap: 12px;
  margin-top: 14px;
  padding: 10px 12px;
  background: rgba(0, 0, 0, 0.18);
  border-radius: 6px;
}
.stat-item { flex: 1; text-align: center; }
.stat-item.hl .stat-val { color: var(--on-accent, #2dd4bf); }
.stat-label { font-size: 11px; color: var(--on-text-dim); margin-bottom: 2px; }
.stat-val { font-size: 18px; font-weight: 700; color: var(--on-text); }

/* 规则表 */
.rules-table { display: flex; flex-direction: column; gap: 6px; }
.rule-row {
  display: grid;
  grid-template-columns: 1fr 180px 50px;
  gap: 8px;
  align-items: center;
}
.rule-header {
  font-size: 11px; color: var(--on-text-dim);
  padding: 0 4px 6px;
  border-bottom: 1px solid var(--on-border-light);
}
.rule-empty { padding: 20px 0; text-align: center; }
.default-ip-row {
  display: flex; align-items: center; gap: 10px;
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px dashed var(--on-border-light);
}
.default-ip-label { font-size: 13px; color: var(--on-text-muted); white-space: nowrap; }
.default-ip-input { width: 220px; }
.default-ip-hint { font-size: 12px; }

/* 日志 */
.log-list {
  max-height: 320px; overflow-y: auto;
  display: flex; flex-direction: column;
  gap: 2px;
}
.log-row {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 8px;
  font-size: 12px;
  border-radius: 3px;
}
.log-row:hover { background: var(--on-bg-hover); }
.log-ts { width: 70px; font-size: 11px; flex-shrink: 0; }
.log-domain { flex: 1; min-width: 0; color: var(--on-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.log-arrow { color: var(--on-text-dim); flex-shrink: 0; }
.log-new { color: var(--on-accent, #2dd4bf); font-weight: 600; flex-shrink: 0; }
.log-old { flex-shrink: 0; font-size: 11px; }
.log-src { flex-shrink: 0; font-size: 11px; margin-left: auto; }

.empty-text { text-align: center; padding: 30px; }
</style>
