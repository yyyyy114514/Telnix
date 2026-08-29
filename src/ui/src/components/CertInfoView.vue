<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'

const { t } = useI18n()
const props = defineProps<{
  certInfo: string | null | undefined
}>()

interface CertInfo {
  subject?: string
  issuer?: string
  not_before?: string
  not_after?: string
  san?: string[]
  serial_number?: string
  fingerprint_sha256?: string
  is_expired?: boolean
  days_remaining?: number
}

const info = computed<CertInfo | null>(() => {
  if (!props.certInfo) return null
  try {
    return JSON.parse(props.certInfo) as CertInfo
  } catch {
    return null
  }
})

function fmtTime(s?: string): string {
  if (!s) return ''
  return s.replace('T', ' ')
}

type CertStatus = 'success' | 'warning' | 'danger' | 'info'
const certStatus = computed<{ type: CertStatus; text: string }>(() => {
  if (!info.value) return { type: 'info', text: '' }
  if (info.value.is_expired) return { type: 'danger', text: t('cert.expired') }
  const days = info.value.days_remaining ?? 0
  if (days <= 30) return { type: 'warning', text: t('cert.expiringSoon', { n: days }) }
  return { type: 'success', text: t('cert.daysRemaining', { n: days }) }
})

function copy(text: string) {
  if (!text) return
  navigator.clipboard.writeText(text).then(() => {
    ElMessage.success(t('common.copied'))
  }).catch(() => {})
}
</script>

<template>
  <div class="cert-info-view overflow-auto">
    <div v-if="!info" class="empty-text text-dim">{{ t('cert.noCertInfo') }}</div>
    <div v-else class="cert-content">
      <div class="cert-status-row">
        <el-tag :type="certStatus.type" effect="dark">{{ certStatus.text }}</el-tag>
      </div>
      <table class="kv-table mono">
        <tbody>
          <tr>
            <td class="k">{{ t('cert.subject') }}</td>
            <td class="v copyable" @click="copy(info.subject || '')">{{ info.subject || '-' }}</td>
          </tr>
          <tr>
            <td class="k">{{ t('cert.issuer') }}</td>
            <td class="v copyable" @click="copy(info.issuer || '')">{{ info.issuer || '-' }}</td>
          </tr>
          <tr>
            <td class="k">{{ t('cert.notBefore') }}</td>
            <td class="v">{{ fmtTime(info.not_before) || '-' }}</td>
          </tr>
          <tr>
            <td class="k">{{ t('cert.notAfter') }}</td>
            <td class="v">
              <div>{{ fmtTime(info.not_after) || '-' }}</div>
              <el-tag :type="certStatus.type" size="small" effect="plain" style="margin-top: 3px">
                {{ certStatus.text }}
              </el-tag>
            </td>
          </tr>
          <tr v-if="info.san && info.san.length">
            <td class="k">{{ t('cert.san') }}</td>
            <td class="v">
              <div class="san-list">
                <span v-for="(d, i) in info.san" :key="i" class="san-item copyable" :title="d" @click="copy(d)">{{ d }}</span>
              </div>
            </td>
          </tr>
          <tr>
            <td class="k">{{ t('cert.serialNumber') }}</td>
            <td class="v copyable" @click="copy(info.serial_number || '')">{{ info.serial_number || '-' }}</td>
          </tr>
          <tr>
            <td class="k">{{ t('cert.sha256Fingerprint') }}</td>
            <td class="v fingerprint copyable" @click="copy(info.fingerprint_sha256 || '')">
              <span>{{ info.fingerprint_sha256 || '-' }}</span>
              <span v-if="info.fingerprint_sha256" class="copy-hint">{{ t('common.copy') }}</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.cert-info-view { padding: 8px; }
.cert-content { display: flex; flex-direction: column; gap: 10px; }
.cert-status-row { display: flex; }
.empty-text { text-align: center; padding: 18px; }
.kv-table { width: 100%; border-collapse: collapse; font-size: 12px; table-layout: fixed; }
.kv-table td { padding: 6px 8px; border-bottom: 1px solid var(--on-border-light); vertical-align: top; word-break: break-all; }
.kv-table td.k {
  width: 110px; color: var(--on-text-muted); white-space: nowrap;
  font-weight: 500;
}
.kv-table td.v { color: var(--on-text); }
.copyable { cursor: pointer; border-radius: 3px; padding: 1px 4px; }
.copyable:hover { background: var(--on-bg-hover); }
.copy-hint { display: none; font-size: 10px; color: var(--on-text-dim); margin-left: 4px; }
.copyable:hover .copy-hint { display: inline; }
.san-list { display: flex; flex-wrap: wrap; gap: 4px; }
.san-item {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  background: var(--on-bg); border: 1px solid var(--on-border-light);
  font-size: 11px; color: var(--on-text-muted); max-width: 200px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.san-item:hover { background: var(--on-bg-hover); color: var(--on-text); }
.fingerprint { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; line-height: 1.6; }
</style>
