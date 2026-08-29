<script setup lang="ts">
/**
 * DoH (DNS over HTTPS) 检测提示对话框。
 *
 * 功能：
 * 1. 检测用户浏览器是否启用了 DoH，绕过 DNS 劫持
 * 2. 提供浏览器特定的禁用 DoH 教程链接
 * 3. 引导用户启用透明代理来拦截 DoH 流量
 *
 * 触发时机：
 * - DNS 劫持启用后检测到 DoH 流量
 * - 用户主动点击 DoH 检测按钮
 */
import { computed, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { api, type DoHBatchDetectionResult } from '../api/client'

const { t } = useI18n()

// 对话框显示状态
const dialogVisible = computed({
  get: () => visible.value,
  set: (v: boolean) => {
    if (!v) {
      visible.value = false
    }
  },
})

// 检测结果
const detectionResult = shallowRef<DoHBatchDetectionResult | null>(null)
const detecting = ref(false)
const lastCheckTime = ref<string | null>(null)

// DoH 提供商中文名映射
const providerNames: Record<string, string> = {
  'cloudflare-dns.com': 'Cloudflare (1.1.1.1)',
  'one.one.one.one': 'Cloudflare (1.1.1.1)',
  'dns.google': 'Google Public DNS',
  'dns.quad9.net': 'Quad9',
  'dns.adguard.com': 'AdGuard DNS',
  'dns.adguard-dns.com': 'AdGuard DNS',
  'family.adguard-dns.com': 'AdGuard Family DNS',
  'dns.nextdns.io': 'NextDNS',
  'anycast.dns.nextdns.io': 'NextDNS',
  'doh.opendns.com': 'OpenDNS',
  'doh.cleanbrowsing.org': 'CleanBrowsing',
  'security-filter-dns.cleanbrowsing.org': 'CleanBrowsing Security',
  'family-filter-dns.cleanbrowsing.org': 'CleanBrowsing Family',
  'dns.mullvad.net': 'Mullvad VPN DNS',
  'dns.controld.com': 'ControlD',
  'doh.privacy.xyz': 'Privacy.xyz DNS',
  'dns.twnic.tw': 'Quad101 (台湾)',
}

// 浏览器对应的禁用 DoH 教程链接
const disableDohGuides: Record<string, { url: string; labelKey: string }> = {
  chrome: {
    url: 'https://chrome.google.com/search?channel=nrower&q=disable+doh+chrome',
    labelKey: 'doh.guideChrome',
  },
  edge: {
    url: 'https://support.microsoft.com/en-us/microsoft-edge/security-privacy-and-dns-in-microsoft-edge-2d7e8ed9-b714-6c77-0cbf-8c91c6c6fe8c',
    labelKey: 'doh.guideEdge',
  },
  firefox: {
    url: 'https://support.mozilla.org/en-US/kb/doh-privacy-enhanced-dns-firefox',
    labelKey: 'doh.guideFirefox',
  },
  safari: {
    url: 'https://support.apple.com/en-us/102103',
    labelKey: 'doh.guideSafari',
  },
  brave: {
    url: 'https://brave.com/help/dns-over-https/',
    labelKey: 'doh.guideBrave',
  },
  opera: {
    url: 'https://help.opera.com/en/latest/security-privacy/#dns-over-https',
    labelKey: 'doh.guideOpera',
  },
  default: {
    url: 'https://www.google.com/search?q=how+to+disable+dns+over+https+in+browser',
    labelKey: 'doh.guideDefault',
  },
}

// 检测 DoH 流量
async function checkForDoh(flows: any[]) {
  if (!flows.length) return null

  detecting.value = true
  try {
    const result = await api.dohDetect(flows)
    detectionResult.value = result
    lastCheckTime.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })

    // 如果检测到 DoH 流量，显示对话框
    if (result.doh_count > 0 || result.dot_count > 0) {
      visible.value = true
    }

    return result
  } catch (e) {
    console.error('DoH detection failed:', e)
    return null
  } finally {
    detecting.value = false
  }
}

// 手动检测（需要传入当前抓到的流量）
async function manualCheck(flows: any[]) {
  await checkForDoh(flows)
}

// 打开教程链接
function openDisableGuide(browser: string) {
  const guide = disableDohGuides[browser] || disableDohGuides.default
  window.open(guide.url, '_blank')
}

// 导出方法供外部调用
defineExpose({
  checkForDoh,
  manualCheck,
})

// 响应式状态
const visible = ref(false)
const dohFlows = computed(() => detectionResult.value?.doh_flows || [])
const dotFlows = computed(() => detectionResult.value?.dot_flows || [])
const dohProviders = computed(() => detectionResult.value?.doh_providers || {})

// 获取提供商显示名
function getProviderName(provider: string): string {
  return providerNames[provider] || provider
}

// 获取浏览器检测提示（基于 DoH 流量特征）
const suggestedBrowser = computed(() => {
  const flows = dohFlows.value
  if (!flows.length) return null

  // 简单检测：基于 URL 或 SNI 模式
  const firstFlow = flows[0]
  const url = firstFlow?.url || ''
  const sni = firstFlow?.sni || ''

  if (url.includes('chrome') || sni.includes('clients')) return 'chrome'
  if (url.includes('edge') || sni.includes('msft')) return 'edge'
  if (url.includes('firefox') || sni.includes('mozilla')) return 'firefox'
  if (url.includes('safari') || sni.includes('apple')) return 'safari'
  if (url.includes('brave')) return 'brave'

  return 'default'
})

// 总的 DoH/DoT 数量
const totalCount = computed(() => {
  const r = detectionResult.value
  if (!r) return 0
  return r.doh_count + r.dot_count
})
</script>

<template>
  <el-dialog
    v-model="dialogVisible"
    :title="t('doh.dialogTitle')"
    width="620px"
    :close-on-click-modal="true"
    align-center
    class="doh-warning-dialog"
  >
    <div class="doh-body">
      <!-- 顶部图标 + 标题 -->
      <div class="doh-header">
        <div class="doh-icon">
          <el-icon><WarningFilled /></el-icon>
        </div>
        <div class="doh-title-block">
          <div class="doh-title">{{ t('doh.detected') }}</div>
          <div class="doh-subtitle">
            {{ t('doh.subtitle', { count: totalCount }) }}
          </div>
        </div>
      </div>

      <!-- 检测结果统计 -->
      <div class="doh-stats" v-if="detectionResult">
        <div class="stat-item" :class="{ 'has-doh': detectionResult.doh_count > 0 }">
          <div class="stat-num">{{ detectionResult.doh_count }}</div>
          <div class="stat-label">DoH</div>
        </div>
        <div class="stat-divider"></div>
        <div class="stat-item" :class="{ 'has-dot': detectionResult.dot_count > 0 }">
          <div class="stat-num">{{ detectionResult.dot_count }}</div>
          <div class="stat-label">DoT</div>
        </div>
        <div class="stat-divider"></div>
        <div class="stat-item">
          <div class="stat-num">{{ detectionResult.total_checked }}</div>
          <div class="stat-label">{{ t('doh.totalChecked') }}</div>
        </div>
      </div>

      <!-- DoH 提供商列表 -->
      <div class="provider-list" v-if="Object.keys(dohProviders).length > 0">
        <div class="provider-title">{{ t('doh.providers') }}</div>
        <div class="provider-tags">
          <el-tag
            v-for="(count, provider) in dohProviders"
            :key="provider"
            type="warning"
            size="small"
          >
            {{ getProviderName(provider) }} ({{ count }})
          </el-tag>
        </div>
      </div>

      <!-- 问题说明 -->
      <div class="doh-explanation">
        <p>{{ t('doh.explanation') }}</p>
      </div>

      <!-- 解决方案 -->
      <div class="doh-solutions">
        <div class="solution-title">{{ t('doh.solutions') }}</div>

        <!-- 方案1：禁用 DoH -->
        <div class="solution-item">
          <div class="solution-icon">
            <el-icon><Monitor /></el-icon>
          </div>
          <div class="solution-content">
            <div class="solution-label">{{ t('doh.solution1Title') }}</div>
            <div class="solution-desc">{{ t('doh.solution1Desc') }}</div>
            <div class="solution-actions">
              <el-button
                v-if="suggestedBrowser"
                size="small"
                type="primary"
                @click="openDisableGuide(suggestedBrowser)"
              >
                {{ t('doh.openGuide') }} ({{ t(disableDohGuides[suggestedBrowser]?.labelKey) }})
              </el-button>
              <el-button size="small" @click="openDisableGuide('default')">
                {{ t(disableDohGuides.default.labelKey) }}
              </el-button>
            </div>
          </div>
        </div>

        <!-- 方案2：启用透明代理 -->
        <div class="solution-item">
          <div class="solution-icon">
            <el-icon><Connection /></el-icon>
          </div>
          <div class="solution-content">
            <div class="solution-label">{{ t('doh.solution2Title') }}</div>
            <div class="solution-desc">{{ t('doh.solution2Desc') }}</div>
            <div class="solution-desc hint">
              <el-icon><InfoFilled /></el-icon>
              {{ t('doh.solution2Hint') }}
            </div>
          </div>
        </div>
      </div>

      <!-- 检测时间 -->
      <div class="check-time" v-if="lastCheckTime">
        {{ t('doh.lastCheck', { time: lastCheckTime }) }}
      </div>
    </div>

    <template #footer>
      <div class="dialog-footer">
        <el-button @click="dialogVisible = false">{{ t('common.close') }}</el-button>
        <el-button type="primary" @click="manualCheck([])">
          {{ t('doh.recheck') }}
        </el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.doh-body {
  max-height: 65vh;
  overflow-y: auto;
  padding-right: 4px;
}

.doh-header {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 16px;
}

.doh-icon {
  font-size: 32px;
  color: var(--on-warn);
  flex-shrink: 0;
  line-height: 1;
  filter: drop-shadow(0 0 6px rgba(154, 103, 0, 0.25));
}

.doh-title-block {
  flex: 1;
  min-width: 0;
}

.doh-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--on-text);
  margin-bottom: 4px;
}

.doh-subtitle {
  font-size: 13px;
  color: var(--on-text-dim);
}

/* 统计卡片 */
.doh-stats {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 24px;
  padding: 16px;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-md);
  margin-bottom: 16px;
}

.stat-item {
  text-align: center;
}

.stat-num {
  font-size: 28px;
  font-weight: 700;
  color: var(--on-text-dim);
  line-height: 1.2;
}

.stat-label {
  font-size: 12px;
  color: var(--on-text-dim);
  margin-top: 2px;
}

.stat-item.has-doh .stat-num {
  color: var(--on-warn);
}

.stat-item.has-dot .stat-num {
  color: var(--on-accent);
}

.stat-divider {
  width: 1px;
  height: 40px;
  background: var(--on-border-light);
}

/* 提供商标签 */
.provider-list {
  margin-bottom: 16px;
}

.provider-title {
  font-size: 12px;
  color: var(--on-text-dim);
  margin-bottom: 8px;
}

.provider-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

/* 问题说明 */
.doh-explanation {
  padding: 12px;
  background: rgba(154, 103, 0, 0.08);
  border: 1px solid rgba(154, 103, 0, 0.2);
  border-radius: var(--on-radius-md);
  margin-bottom: 16px;
}

.doh-explanation p {
  margin: 0;
  font-size: 13px;
  color: var(--on-text);
  line-height: 1.6;
}

/* 解决方案 */
.doh-solutions {
  margin-bottom: 16px;
}

.solution-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--on-text);
  margin-bottom: 12px;
}

.solution-item {
  display: flex;
  gap: 12px;
  padding: 12px;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-md);
  margin-bottom: 10px;
}

.solution-icon {
  font-size: 20px;
  color: var(--on-accent);
  flex-shrink: 0;
  margin-top: 2px;
}

.solution-content {
  flex: 1;
  min-width: 0;
}

.solution-label {
  font-size: 13px;
  font-weight: 600;
  color: var(--on-text);
  margin-bottom: 4px;
}

.solution-desc {
  font-size: 12px;
  color: var(--on-text-dim);
  line-height: 1.5;
  margin-bottom: 8px;
}

.solution-desc.hint {
  display: flex;
  align-items: flex-start;
  gap: 4px;
  color: var(--on-info, #409eff);
  background: rgba(64, 158, 255, 0.08);
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 11px;
}

.solution-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

/* 检测时间 */
.check-time {
  font-size: 11px;
  color: var(--on-text-dim);
  text-align: center;
}

/* 底部按钮 */
.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

/* 滚动条 */
.doh-body::-webkit-scrollbar {
  width: 6px;
}
.doh-body::-webkit-scrollbar-thumb {
  background: var(--on-border);
  border-radius: 3px;
}
.doh-body::-webkit-scrollbar-track {
  background: transparent;
}
</style>
