<script setup lang="ts">
import { onMounted, ref, computed, onUnmounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { Refresh, Lightning, Connection, Clock, Close, Loading, Check } from '@element-plus/icons-vue'
import { api } from '../api/client'

const { t } = useI18n()

// ============ 状态定义 ============

interface ClashStatus {
  enabled: boolean
  integrated: boolean
  reachable: boolean
  version: string | null
  mixed_port: number | null
  mode: string | null
  upstream_proxy: string | null
}

interface ProxyNode {
  name: string
  type: string  // ss, ssr, vmess, trojan, hysteria2, wireguard, ssh, snell, vless, http, https
  protocol?: string
  alive?: boolean
  history?: number[]
}

interface ProxyGroup {
  name: string
  type: string  // Selector, URLTest, Fallback, LoadBalance, etc.
  now?: string   // 当前选中的节点名
  all?: string[] // 所有可用节点
}

interface ClashProxies {
  proxies: Record<string, ProxyNode | ProxyGroup>
}

interface ConnectionInfo {
  id: string
  metadata: {
    host: string
    destinationIP?: string
    destinationPort?: number
    sourceIP?: string
    sourcePort?: number
    network: string
    type: string
    processPath?: string
    processName?: string
  }
  upload: number
  download: number
  start: string
  chains: string[]
  rule: string
  // 上行/下行速率（字节每秒）
  uploadSpeed?: number
  downloadSpeed?: number
}

interface DelayTestResult {
  name: string
  delay: number | null
  error?: string
}

// ============ 响应式状态 ============

const status = ref<ClashStatus | null>(null)
const proxiesData = ref<ClashProxies | null>(null)
const connections = ref<ConnectionInfo[]>([])
const loading = ref(false)
const toggling = ref(false)
const switchingNode = ref<string | null>(null)
const testingDelay = ref<Set<string>>(new Set())
const allDelayResults = ref<Map<string, number | null>>(new Map())
const testingAllDelay = ref(false)

// URL Test 自动选择
const autoSelectEnabled = ref(false)
const autoSelectInterval = ref(30) // 秒
const autoSelectTimer = ref<ReturnType<typeof setInterval> | null>(null)
const currentFastestNode = ref<string | null>(null)

// 连接监控
const connectionFilterProcess = ref('')
const connectionFilterNode = ref('')
const connectionsLastUpdate = ref<Map<string, { upload: number; download: number; ts: number }>>(new Map())
const connectionUpdateTimer = ref<ReturnType<typeof setInterval> | null>(null)
const lastConnectionsSnapshot = ref<{ data: ConnectionInfo[]; ts: number } | null>(null)

// ============ 计算属性 ============

// 策略组列表（排除普通节点）
const policyGroups = computed(() => {
  if (!proxiesData.value?.proxies) return []
  return Object.entries(proxiesData.value.proxies)
    .filter(([_, data]) => 'type' in data && ['Selector', 'URLTest', 'Fallback', 'LoadBalance'].includes(data.type as string))
    .map(([name, data]) => {
      const group = data as ProxyGroup
      return {
        name,
        type: group.type,
        now: group.now,
        all: group.all || []
      }
    })
})

// URLTest 组
const urlTestGroups = computed(() => {
  return policyGroups.value.filter(g => g.type === 'URLTest')
})

// 普通节点列表
const proxyNodes = computed(() => {
  if (!proxiesData.value?.proxies) return []
  return Object.entries(proxiesData.value.proxies)
    .filter(([_, data]) => 'type' in data && !['Selector', 'URLTest', 'Fallback', 'LoadBalance'].includes(data.type as string))
    .map(([name, data]) => {
      const node = data as ProxyNode
      return {
        name,
        type: node.type || 'unknown',
        alive: node.alive !== false,
        delay: allDelayResults.value.get(name) ?? null,
        testing: testingDelay.value.has(name)
      }
    })
})

// 过滤后的连接列表
const filteredConnections = computed(() => {
  let result = connections.value

  if (connectionFilterProcess.value) {
    const filter = connectionFilterProcess.value.toLowerCase()
    result = result.filter(c => {
      const processName = c.metadata.processName?.toLowerCase() || ''
      return processName.includes(filter)
    })
  }

  if (connectionFilterNode.value) {
    const filter = connectionFilterNode.value.toLowerCase()
    result = result.filter(c => {
      const chain = c.chains?.join(',').toLowerCase() || ''
      return chain.includes(filter)
    })
  }

  return result
})

// 活跃连接数
const activeConnectionCount = computed(() => connections.value.length)

// 获取可用节点列表（用于筛选）
const availableProcesses = computed(() => {
  const processes = new Set<string>()
  connections.value.forEach(c => {
    if (c.metadata.processName) {
      processes.add(c.metadata.processName)
    }
  })
  return Array.from(processes).sort()
})

const availableNodes = computed(() => {
  const nodes = new Set<string>()
  connections.value.forEach(c => {
    c.chains?.forEach(n => nodes.add(n))
  })
  return Array.from(nodes).sort()
})

// ============ 工具函数 ============

function getNodeTypeIcon(type: string): string {
  const iconMap: Record<string, string> = {
    ss: '🔐',
    ssr: '🔒',
    vmess: '⚡',
    vless: '⚡',
    trojan: '🔱',
    hysteria2: '🚀',
    hysteria: '🚀',
    wireguard: '🛡️',
    ssh: '💻',
    snell: '🐍',
    http: '🌐',
    https: '🔗',
  }
  return iconMap[type.toLowerCase()] || '📡'
}

function getNodeTypeLabel(type: string): string {
  const labelMap: Record<string, string> = {
    ss: 'Shadowsocks',
    ssr: 'ShadowsocksR',
    vmess: 'Vmess',
    vless: 'VLESS',
    trojan: 'Trojan',
    hysteria2: 'Hysteria2',
    hysteria: 'Hysteria',
    wireguard: 'WireGuard',
    ssh: 'SSH',
    snell: 'Snell',
    http: 'HTTP',
    https: 'HTTPS',
  }
  return labelMap[type.toLowerCase()] || type
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatSpeed(bytesPerSec: number): string {
  if (bytesPerSec < 1024) return `${bytesPerSec.toFixed(0)} B/s`
  if (bytesPerSec < 1024 * 1024) return `${(bytesPerSec / 1024).toFixed(1)} KB/s`
  return `${(bytesPerSec / (1024 * 1024)).toFixed(1)} MB/s`
}

function getDelayClass(delay: number | null): string {
  if (delay === null) return 'delay-unknown'
  if (delay < 100) return 'delay-excellent'
  if (delay < 200) return 'delay-good'
  if (delay < 500) return 'delay-normal'
  return 'delay-poor'
}

// 防抖函数
function debounce<T extends (...args: any[]) => any>(fn: T, delay: number): T {
  let timer: ReturnType<typeof setTimeout> | null = null
  return ((...args: any[]) => {
    if (timer) clearTimeout(timer)
    timer = setTimeout(() => fn(...args), delay)
  }) as T
}

// ============ API 调用 ============

async function refresh() {
  loading.value = true
  try {
    const [statusData, proxiesResult, connectionsResult] = await Promise.all([
      api.clashStatus().catch(() => null),
      api.clashProxies().catch(() => null),
      status.value?.reachable ? api.clashConnections().catch(() => null) : Promise.resolve(null)
    ])

    status.value = statusData
    proxiesData.value = proxiesResult

    if (connectionsResult) {
      // 计算速率
      const now = Date.now()
      const newConnections: ConnectionInfo[] = []

      for (const conn of connectionsResult.connections || []) {
        const connId = conn.id || JSON.stringify(conn.metadata)

        // 查找上一次的记录
        const lastRecord = connectionsLastUpdate.value.get(connId)
        if (lastRecord && lastRecord.ts > 0) {
          const timeDiff = (now - lastRecord.ts) / 1000 // 秒
          if (timeDiff > 0) {
            conn.uploadSpeed = Math.max(0, (conn.upload - lastRecord.upload) / timeDiff)
            conn.downloadSpeed = Math.max(0, (conn.download - lastRecord.download) / timeDiff)
          }
        }

        // 更新记录
        connectionsLastUpdate.value.set(connId, {
          upload: conn.upload,
          download: conn.download,
          ts: now
        })

        newConnections.push(conn)
      }

      connections.value = newConnections
      lastConnectionsSnapshot.value = { data: newConnections, ts: now }
    }

    // 集成已启用但 Mihomo 不可达
    if (statusData?.integrated && !statusData?.reachable) {
      ElMessage.warning(t('clash.mihomoNotConnectedAutoDirect'))
    }
  } catch (e: any) {
    ElMessage.error(t('clash.getStatusFailed', { msg: e.message }))
  } finally {
    loading.value = false
  }
}

// 防抖版本的刷新
const debouncedRefresh = debounce(refresh, 500)

async function onToggle(val: boolean | string | number) {
  toggling.value = true
  try {
    if (val) {
      await api.clashEnable({})
      ElMessage.success(t('clash.integrationEnabledOnline'))
    } else {
      await api.clashDisable()
      ElMessage.info(t('clash.integrationDisabledDirect'))
    }
    await refresh()
  } catch (e: any) {
    ElMessage.error(t('clash.operationFailed', { msg: e.message }))
    await refresh()
  } finally {
    toggling.value = false
  }
}

// 测试单个节点延迟（防抖）
async function testNodeDelay(nodeName: string) {
  if (testingDelay.value.has(nodeName)) return

  testingDelay.value.add(nodeName)
  try {
    const result = await api.clashProxyDelay(nodeName)
    const delay = result?.delay
    allDelayResults.value.set(nodeName, delay)
  } catch (e: any) {
    allDelayResults.value.set(nodeName, null)
    console.error(`Delay test failed for ${nodeName}:`, e)
  } finally {
    testingDelay.value.delete(nodeName)
  }
}

// 防抖版本的延迟测试（500ms）
const debouncedTestDelay = debounce((nodeName: string) => testNodeDelay(nodeName), 500)

// 测试全部节点延迟
async function testAllNodesDelay() {
  if (testingAllDelay.value) return
  testingAllDelay.value = true
  allDelayResults.value.clear()

  try {
    // 对每个 URLTest 组测试全部节点
    for (const group of urlTestGroups.value) {
      if (group.all && group.all.length > 0) {
        // 使用 group_delay 批量测试
        try {
          const result = await api.clashGroupDelay(group.name)
          if (result?.delays) {
            for (const [name, delay] of Object.entries(result.delays)) {
              allDelayResults.value.set(name, delay as number)
            }
          }
        } catch (e) {
          // 如果 group_delay 失败，逐个测试
          for (const nodeName of group.all) {
            await testNodeDelay(nodeName)
          }
        }
      }
    }

    // 也测试非 URLTest 组的节点
    for (const node of proxyNodes.value) {
      if (!allDelayResults.value.has(node.name)) {
        await testNodeDelay(node.name)
      }
    }

    ElMessage.success(t('clash.testAllDelayComplete'))
  } catch (e: any) {
    ElMessage.error(t('clash.testDelayFailed', { msg: e.message }))
  } finally {
    testingAllDelay.value = false
  }
}

// 切换节点
async function switchNode(groupName: string, nodeName: string) {
  switchingNode.value = nodeName
  try {
    await api.clashSelectProxy(groupName, nodeName)
    ElMessage.success(t('clash.switchNodeSuccess', { name: nodeName }))
    await debouncedRefresh()
  } catch (e: any) {
    ElMessage.error(t('clash.switchNodeFailed', { msg: e.message }))
  } finally {
    switchingNode.value = null
  }
}

// ============ URL Test 自动选择 ============

async function startAutoSelect() {
  if (autoSelectTimer.value) return

  autoSelectTimer.value = setInterval(async () => {
    if (!autoSelectEnabled.value || urlTestGroups.value.length === 0) {
      stopAutoSelect()
      return
    }

    testingAllDelay.value = true
    try {
      // 对每个 URLTest 组找到最快节点并切换
      for (const group of urlTestGroups.value) {
        if (!group.all || group.all.length === 0) continue

        // 清空延迟结果
        for (const name of group.all) {
          allDelayResults.value.delete(name)
        }

        // 批量测试
        try {
          const result = await api.clashGroupDelay(group.name)
          if (result?.delays) {
            for (const [name, delay] of Object.entries(result.delays)) {
              allDelayResults.value.set(name, delay as number)
            }
          }
        } catch {
          // 逐个测试
          const promises = group.all.map(async (name) => {
            await testNodeDelay(name)
          })
          await Promise.all(promises)
        }

        // 找最快节点
        let fastestNode: string | null = null
        let fastestDelay = Infinity

        for (const name of group.all) {
          const delay = allDelayResults.value.get(name)
          if (delay !== null && delay !== undefined && delay < fastestDelay) {
            fastestDelay = delay
            fastestNode = name
          }
        }

        // 如果找到更快节点且不是当前节点，切换
        if (fastestNode && fastestNode !== group.now) {
          await switchNode(group.name, fastestNode)
          currentFastestNode.value = fastestNode
        }
      }
    } finally {
      testingAllDelay.value = false
    }
  }, autoSelectInterval.value * 1000)
}

function stopAutoSelect() {
  if (autoSelectTimer.value) {
    clearInterval(autoSelectTimer.value)
    autoSelectTimer.value = null
  }
}

function toggleAutoSelect() {
  autoSelectEnabled.value = !autoSelectEnabled.value
  if (autoSelectEnabled.value) {
    startAutoSelect()
    ElMessage.success(t('clash.selectFastestEnabled'))
  } else {
    stopAutoSelect()
    ElMessage.success(t('clash.selectFastestDisabled'))
  }
}

// ============ 连接监控 ============

// 节流更新（每秒最多1次）
let connectionUpdatePending = false
const THROTTLE_INTERVAL = 1000 // 1秒

function startConnectionMonitor() {
  if (connectionUpdateTimer.value) return

  connectionUpdateTimer.value = setInterval(async () => {
    if (connectionUpdatePending) return
    connectionUpdatePending = true

    try {
      if (status.value?.reachable) {
        await refreshConnections()
      }
    } finally {
      connectionUpdatePending = false
    }
  }, THROTTLE_INTERVAL)
}

function stopConnectionMonitor() {
  if (connectionUpdateTimer.value) {
    clearInterval(connectionUpdateTimer.value)
    connectionUpdateTimer.value = null
  }
}

async function refreshConnections() {
  try {
    const result = await api.clashConnections()
    if (result?.connections) {
      // 计算速率
      const now = Date.now()
      const newConnections: ConnectionInfo[] = []

      for (const conn of result.connections) {
        const connId = conn.id || JSON.stringify(conn.metadata)
        const lastRecord = connectionsLastUpdate.value.get(connId)

        if (lastRecord && lastRecord.ts > 0) {
          const timeDiff = (now - lastRecord.ts) / 1000
          if (timeDiff > 0) {
            conn.uploadSpeed = Math.max(0, (conn.upload - lastRecord.upload) / timeDiff)
            conn.downloadSpeed = Math.max(0, (conn.download - lastRecord.download) / timeDiff)
          }
        }

        connectionsLastUpdate.value.set(connId, {
          upload: conn.upload,
          download: conn.download,
          ts: now
        })

        newConnections.push(conn)
      }

      connections.value = newConnections
    }
  } catch (e: any) {
    console.error('Failed to refresh connections:', e)
  }
}

async function closeConnection(connId: string) {
  try {
    await api.clashCloseConnection(connId)
    await debouncedRefresh()
    ElMessage.success(t('clash.connectionClosed', { id: connId }))
  } catch (e: any) {
    ElMessage.error(t('clash.closeConnectionFailed', { msg: e.message }))
  }
}

async function closeAllConnections() {
  try {
    await api.clashCloseAllConnections()
    await debouncedRefresh()
    ElMessage.success(t('clash.connectionsClosed', { n: connections.value.length }))
  } catch (e: any) {
    ElMessage.error(t('clash.closeAllFailed', { msg: e.message }))
  }
}

// ============ 生命周期 ============

onMounted(() => {
  refresh()
  startConnectionMonitor()
})

onUnmounted(() => {
  stopAutoSelect()
  stopConnectionMonitor()
})

// 当 Mihomo 连接状态变化时，更新连接监控
watch(() => status.value?.reachable, (reachable) => {
  if (reachable) {
    startConnectionMonitor()
  } else {
    stopConnectionMonitor()
  }
})
</script>

<template>
  <div class="clash-view">
    <!-- 集成状态卡片 -->
    <div class="clash-card">
      <div class="clash-header">
        <el-icon class="clash-icon no-select" :class="{ ok: status?.reachable, off: !status?.reachable }">
          <ClashIcon />
        </el-icon>
        <div class="clash-title">
          <div class="title-text no-select">{{ t('clash.integration') }}</div>
          <div class="title-sub no-select">{{ t('clash.externalModeHint') }}</div>
        </div>
        <el-button
          class="refresh-btn"
          size="small"
          circle
          :loading="loading"
          @click="debouncedRefresh"
          :title="t('clash.refreshStatus')"
        >
          <el-icon v-if="!loading"><Refresh /></el-icon>
        </el-button>
        <el-switch
          :model-value="!!status?.integrated"
          :loading="toggling"
          @change="onToggle"
          class="clash-switch"
        />
      </div>

      <div class="clash-status no-select" v-if="status?.reachable">
        <div class="status-row">
          <span class="status-label">{{ t('clash.connectionStatus') }}</span>
          <span class="status-val ok">{{ t('clash.connected') }}</span>
        </div>
        <div class="status-row">
          <span class="status-label">{{ t('clash.version') }}</span>
          <span class="status-val mono">{{ status.version || '—' }}</span>
        </div>
        <div class="status-row">
          <span class="status-label">{{ t('clash.mixedProxyPort') }}</span>
          <span class="status-val mono">{{ status.mixed_port || '—' }}</span>
        </div>
        <div class="status-row">
          <span class="status-label">{{ t('clash.runningMode') }}</span>
          <span class="status-val">{{ status.mode || '—' }}</span>
        </div>
        <div class="status-row" v-if="status.upstream_proxy">
          <span class="status-label">{{ t('clash.upstreamProxy') }}</span>
          <span class="status-val mono">{{ status.upstream_proxy }}</span>
        </div>
      </div>

      <div class="clash-status no-select" v-else>
        <div class="status-row">
          <span class="status-label">{{ t('clash.connectionStatus') }}</span>
          <span class="status-val fail">{{ t('clash.notConnected') }}</span>
        </div>
        <div class="status-hint">
          <template v-if="status?.integrated">
            {{ t('clash.hintIntegratedLine1') }}<br/>
            {{ t('clash.hintIntegratedLine2') }}
          </template>
          <template v-else>
            {{ t('clash.hintNotIntegrated') }}
          </template>
        </div>
      </div>
    </div>

    <!-- URL Test 自动选择 -->
    <div class="clash-card" v-if="status?.reachable && urlTestGroups.length > 0">
      <div class="section-header">
        <el-icon><Lightning /></el-icon>
        <span>{{ t('clash.autoSelect') }}</span>
        <el-tag :type="autoSelectEnabled ? 'success' : 'info'" size="small">
          {{ autoSelectEnabled ? t('clash.autoSelectEnabled') : t('clash.autoSelectDisabled') }}
        </el-tag>
      </div>

      <div class="auto-select-content">
        <div class="auto-select-row">
          <el-switch
            v-model="autoSelectEnabled"
            @change="toggleAutoSelect"
            :disabled="testingAllDelay"
          />
          <span class="auto-select-label">{{ t('clash.selectFastest') }}</span>
          <span class="auto-select-hint">{{ t('clash.autoSelectHint') }}</span>
        </div>

        <div class="auto-select-row" v-if="autoSelectEnabled">
          <span class="auto-select-label">{{ t('clash.interval') }}:</span>
          <el-select v-model="autoSelectInterval" size="small" style="width: 100px" @change="() => { stopAutoSelect(); startAutoSelect() }">
            <el-option :label="t('clash.intervalSec', { n: 15 })" :value="15" />
            <el-option :label="t('clash.intervalSec', { n: 30 })" :value="30" />
            <el-option :label="t('clash.intervalSec', { n: 60 })" :value="60" />
            <el-option :label="t('clash.intervalSec', { n: 120 })" :value="120" />
          </el-select>
        </div>

        <div class="auto-select-row" v-if="currentFastestNode && autoSelectEnabled">
          <span class="auto-select-label">{{ t('clash.fastestNode') }}:</span>
          <span class="fastest-node">{{ currentFastestNode }}</span>
        </div>

        <div class="auto-select-actions">
          <el-button
            size="small"
            type="primary"
            :loading="testingAllDelay"
            @click="testAllNodesDelay"
          >
            <el-icon v-if="!testingAllDelay"><Clock /></el-icon>
            {{ testingAllDelay ? t('clash.testingAll') : t('clash.testAllDelay') }}
          </el-button>
        </div>
      </div>
    </div>

    <!-- 节点管理器 -->
    <div class="clash-card" v-if="status?.reachable">
      <div class="section-header">
        <el-icon><Connection /></el-icon>
        <span>{{ t('clash.nodeManager') }}</span>
        <span class="node-count" v-if="proxyNodes.length > 0">{{ proxyNodes.length }} {{ t('clash.nodeList') }}</span>
      </div>

      <!-- 策略组 -->
      <div class="policy-groups" v-if="policyGroups.length > 0">
        <div class="policy-group" v-for="group in policyGroups" :key="group.name">
          <div class="policy-group-header">
            <span class="policy-group-name">{{ group.name }}</span>
            <el-tag size="small" :type="group.type === 'URLTest' ? 'primary' : 'info'">
              {{ group.type }}
            </el-tag>
          </div>
          <div class="policy-group-nodes">
            <div
              v-for="nodeName in group.all"
              :key="nodeName"
              class="node-item"
              :class="{
                'node-selected': nodeName === group.now,
                'node-testing': testingDelay.has(nodeName)
              }"
              @click="group.type !== 'URLTest' ? switchNode(group.name, nodeName) : null"
            >
              <span class="node-icon">{{ getNodeTypeIcon(nodeName.includes('-') ? nodeName.split('-')[0] : 'ss') }}</span>
              <span class="node-name">{{ nodeName }}</span>
              <span
                v-if="allDelayResults.has(nodeName)"
                class="node-delay"
                :class="getDelayClass(allDelayResults.get(nodeName) ?? null)"
              >
                {{ allDelayResults.get(nodeName) !== null ? t('clash.nodeDelayMs', { ms: allDelayResults.get(nodeName) }) : t('clash.nodeOffline') }}
              </span>
              <el-icon
                v-if="testingDelay.has(nodeName)"
                class="is-loading node-loading"
              >
                <Loading />
              </el-icon>
              <el-button
                v-else-if="group.type !== 'URLTest'"
                size="small"
                circle
                @click.stop="debouncedTestDelay(nodeName)"
                :title="t('clash.testDelay')"
              >
                <el-icon><Clock /></el-icon>
              </el-button>
              <el-icon
                v-if="nodeName === group.now"
                class="node-check"
                color="var(--on-ok)"
              >
                <Check />
              </el-icon>
            </div>
          </div>
        </div>
      </div>

      <!-- 普通节点列表 -->
      <div class="node-list" v-if="proxyNodes.length > 0">
        <div
          v-for="node in proxyNodes"
          :key="node.name"
          class="node-item"
          :class="{
            'node-offline': !node.alive,
            'node-testing': node.testing
          }"
        >
          <span class="node-icon">{{ getNodeTypeIcon(node.type) }}</span>
          <span class="node-type-label">{{ getNodeTypeLabel(node.type) }}</span>
          <span class="node-name">{{ node.name }}</span>
          <span
            v-if="node.delay !== null"
            class="node-delay"
            :class="getDelayClass(node.delay)"
          >
            {{ t('clash.nodeDelayMs', { ms: node.delay }) }}
          </span>
          <span v-else-if="!node.alive" class="node-delay delay-unknown">
            {{ t('clash.nodeOffline') }}
          </span>
          <el-icon
            v-if="node.testing"
            class="is-loading node-loading"
          >
            <Loading />
          </el-icon>
          <el-button
            v-else
            size="small"
            circle
            @click="debouncedTestDelay(node.name)"
            :title="t('clash.testDelay')"
          >
            <el-icon><Clock /></el-icon>
          </el-button>
        </div>
      </div>

      <div class="empty-state" v-if="proxyNodes.length === 0 && policyGroups.length === 0">
        {{ t('clash.noNodes') }}
      </div>
    </div>

    <!-- 连接监控 -->
    <div class="clash-card" v-if="status?.reachable">
      <div class="section-header">
        <el-icon><Connection /></el-icon>
        <span>{{ t('clash.connectionMonitor') }}</span>
        <el-badge :value="activeConnectionCount" :max="999" type="primary" />
      </div>

      <!-- 筛选器 -->
      <div class="connection-filters" v-if="connections.length > 0">
        <el-select
          v-model="connectionFilterProcess"
          :placeholder="t('clash.filterByProcess')"
          clearable
          size="small"
          style="width: 150px"
        >
          <el-option :label="t('clash.allProcesses')" value="" />
          <el-option
            v-for="proc in availableProcesses"
            :key="proc"
            :label="proc"
            :value="proc"
          />
        </el-select>

        <el-select
          v-model="connectionFilterNode"
          :placeholder="t('clash.filterByNode')"
          clearable
          size="small"
          style="width: 150px"
        >
          <el-option :label="t('clash.allNodes')" value="" />
          <el-option
            v-for="node in availableNodes"
            :key="node"
            :label="node"
            :value="node"
          />
        </el-select>

        <el-button
          size="small"
          type="danger"
          plain
          @click="closeAllConnections"
          :disabled="connections.length === 0"
        >
          <el-icon><Close /></el-icon>
          {{ t('clash.closeAllConnections') }}
        </el-button>
      </div>

      <!-- 连接列表 -->
      <div class="connection-list" v-if="filteredConnections.length > 0">
        <div class="connection-header">
          <span class="conn-col process">{{ t('clash.processName') }}</span>
          <span class="conn-col target">{{ t('clash.targetHost') }}</span>
          <span class="conn-col upload">{{ t('clash.uploadSpeed') }}</span>
          <span class="conn-col download">{{ t('clash.downloadSpeed') }}</span>
          <span class="conn-col node">{{ t('clash.currentNodeLabel') }}</span>
          <span class="conn-col action">{{ t('clash.action') }}</span>
        </div>

        <div
          v-for="conn in filteredConnections"
          :key="conn.id"
          class="connection-row"
        >
          <span class="conn-col process" :title="conn.metadata.processPath">
            {{ conn.metadata.processName || t('common.unknown') }}
          </span>
          <span class="conn-col target">
            {{ conn.metadata.host || conn.metadata.destinationIP || '—' }}
            <span class="conn-port" v-if="conn.metadata.destinationPort">
              :{{ conn.metadata.destinationPort }}
            </span>
          </span>
          <span class="conn-col upload">
            <span v-if="conn.uploadSpeed">{{ formatSpeed(conn.uploadSpeed) }}</span>
            <span v-else class="speed-zero">{{ formatBytes(conn.upload) }}</span>
          </span>
          <span class="conn-col download">
            <span v-if="conn.downloadSpeed">{{ formatSpeed(conn.downloadSpeed) }}</span>
            <span v-else class="speed-zero">{{ formatBytes(conn.download) }}</span>
          </span>
          <span class="conn-col node" :title="conn.chains?.join(' → ')">
            {{ conn.chains?.[conn.chains.length - 1] || '—' }}
          </span>
          <span class="conn-col action">
            <el-button
              size="small"
              type="danger"
              plain
              circle
              @click="closeConnection(conn.id)"
              :title="t('clash.closeConnection')"
            >
              <el-icon><Close /></el-icon>
            </el-button>
          </span>
        </div>
      </div>

      <div class="empty-state" v-else-if="connections.length === 0">
        {{ t('clash.noConnections') }}
      </div>

      <div class="empty-state" v-else>
        {{ t('clash.noConnections') }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.clash-view {
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  background: var(--on-bg);
  max-width: 900px;
  margin: 0 auto;
}

.clash-card {
  background: var(--on-bg-card);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-lg);
  padding: 20px;
  position: relative;
  overflow: hidden;
  transition: border-color .2s, box-shadow .2s;
}

.clash-card:hover {
  border-color: var(--on-border);
  box-shadow: var(--on-shadow-sm);
}

.clash-card::before {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  top: 0;
  height: 2px;
  background: linear-gradient(90deg, var(--on-accent) 0%, var(--on-accent-dim) 50%, transparent 100%);
  opacity: 0.7;
}

.clash-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--on-border);
}

.clash-icon {
  font-size: 36px;
  color: var(--el-text-color-secondary, #888);
}

.clash-icon.ok {
  color: var(--on-ok);
}

.clash-icon.off {
  color: var(--on-text-dim);
}

.clash-title {
  flex: 1;
}

.title-text {
  font-size: 18px;
  font-weight: 600;
  color: var(--el-text-color-primary, #eee);
}

.title-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary, #888);
  margin-top: 4px;
}

.refresh-btn {
  margin-right: 8px;
}

.clash-status {
  padding-top: 16px;
}

.status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 0;
}

.status-label {
  color: var(--el-text-color-secondary, #888);
  font-size: 14px;
}

.status-val {
  color: var(--el-text-color-primary, #eee);
  font-size: 14px;
}

.status-val.ok {
  color: var(--on-ok);
  font-weight: 600;
}

.status-val.fail {
  color: var(--on-error);
  font-weight: 600;
}

.mono {
  font-family: 'JetBrains Mono', 'Cascadia Code', Consolas, monospace;
}

.status-hint {
  margin-top: 12px;
  padding: 12px 16px;
  background: var(--on-bg-hover);
  border-radius: var(--on-radius-lg);
  font-size: 13px;
  color: var(--on-text-muted);
  line-height: 1.6;
}

/* Section header */
.section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  color: var(--el-text-color-primary, #eee);
  margin-bottom: 16px;
}

.section-header .el-icon {
  font-size: 20px;
  color: var(--on-accent);
}

.node-count {
  font-size: 12px;
  color: var(--el-text-color-secondary, #888);
  font-weight: normal;
  margin-left: auto;
}

/* Auto Select */
.auto-select-content {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.auto-select-row {
  display: flex;
  align-items: center;
  gap: 12px;
}

.auto-select-label {
  font-size: 14px;
  color: var(--el-text-color-primary, #eee);
}

.auto-select-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary, #888);
}

.fastest-node {
  color: var(--on-ok);
  font-weight: 600;
}

.auto-select-actions {
  display: flex;
  gap: 12px;
  margin-top: 8px;
}

/* Policy Groups */
.policy-groups {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.policy-group {
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius);
  padding: 12px;
  background: var(--on-bg-hover);
}

.policy-group-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--on-border-light);
}

.policy-group-name {
  font-weight: 600;
  font-size: 14px;
}

.policy-group-nodes {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

/* Node Items */
.node-list,
.policy-group-nodes {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.node-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: var(--on-radius);
  background: var(--on-bg-card);
  border: 1px solid var(--on-border-light);
  cursor: pointer;
  transition: all .2s;
  font-size: 13px;
}

.node-item:hover {
  border-color: var(--on-accent);
  background: var(--on-bg-hover);
}

.node-item.node-selected {
  border-color: var(--on-ok);
  background: rgba(76, 175, 80, 0.1);
}

.node-item.node-testing {
  opacity: 0.7;
}

.node-item.node-offline {
  opacity: 0.5;
  cursor: not-allowed;
}

.node-icon {
  font-size: 14px;
}

.node-type-label {
  font-size: 10px;
  color: var(--el-text-color-secondary, #888);
  text-transform: uppercase;
}

.node-name {
  color: var(--el-text-color-primary, #eee);
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-delay {
  font-size: 12px;
  font-weight: 500;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--on-bg-hover);
}

.node-delay.delay-excellent {
  color: #67c23a;
  background: rgba(103, 194, 58, 0.1);
}

.node-delay.delay-good {
  color: #85ce61;
  background: rgba(133, 206, 97, 0.1);
}

.node-delay.delay-normal {
  color: #e6a23c;
  background: rgba(230, 162, 60, 0.1);
}

.node-delay.delay-poor {
  color: #f56c6c;
  background: rgba(245, 108, 108, 0.1);
}

.node-delay.delay-unknown {
  color: var(--el-text-color-secondary, #888);
  background: var(--on-bg-hover);
}

.node-loading {
  font-size: 12px;
  color: var(--on-accent);
}

.node-check {
  font-size: 14px;
}

/* Connection Monitor */
.connection-filters {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.connection-list {
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius);
  overflow: hidden;
}

.connection-header {
  display: flex;
  align-items: center;
  padding: 10px 12px;
  background: var(--on-bg-hover);
  border-bottom: 1px solid var(--on-border-light);
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary, #888);
  text-transform: uppercase;
}

.connection-row {
  display: flex;
  align-items: center;
  padding: 10px 12px;
  border-bottom: 1px solid var(--on-border-light);
  font-size: 13px;
  transition: background .15s;
}

.connection-row:last-child {
  border-bottom: none;
}

.connection-row:hover {
  background: var(--on-bg-hover);
}

.conn-col {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding-right: 8px;
}

.conn-col.process {
  width: 100px;
  flex-shrink: 0;
}

.conn-col.target {
  flex: 1;
  min-width: 120px;
}

.conn-col.upload,
.conn-col.download {
  width: 80px;
  flex-shrink: 0;
  text-align: right;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
}

.conn-col.node {
  width: 100px;
  flex-shrink: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.conn-col.action {
  width: 40px;
  flex-shrink: 0;
  display: flex;
  justify-content: center;
}

.conn-port {
  color: var(--el-text-color-secondary, #888);
  font-size: 11px;
}

.speed-zero {
  color: var(--el-text-color-secondary, #888);
}

/* Empty state */
.empty-state {
  text-align: center;
  padding: 24px;
  color: var(--el-text-color-secondary, #888);
  font-size: 14px;
}
</style>
