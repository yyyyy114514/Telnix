<script setup lang="ts">
import { computed } from 'vue'

// TLS 证书信息展示
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

// 格式化时间：2024-01-01T00:00:00 → 2024-01-01 00:00:00
function fmtTime(s?: string): string {
  if (!s) return ''
  return s.replace('T', ' ')
}

// 指纹分行展示（每行 16 字节，每组 2 字节）
function fmtFingerprint(fp?: string): string {
  if (!fp) return ''
  // 已经是 AB:CD:EF 格式，直接返回
  return fp
}

// 截断长字符串
function truncate(s: string, n = 80): string {
  return s.length > n ? s.slice(0, n) + '...' : s
}
</script>

<template>
  <div class="cert-info-view overflow-auto">
    <div v-if="!info" class="empty-text text-dim">（无证书信息）</div>
    <table v-else class="kv-table mono">
      <tbody>
        <tr>
          <td class="k">主题</td>
          <td class="v" :title="info.subject">{{ info.subject || '-' }}</td>
        </tr>
        <tr>
          <td class="k">颁发者</td>
          <td class="v" :title="info.issuer">{{ info.issuer || '-' }}</td>
        </tr>
        <tr>
          <td class="k">生效时间</td>
          <td class="v">{{ fmtTime(info.not_before) || '-' }}</td>
        </tr>
        <tr>
          <td class="k">过期时间</td>
          <td class="v">
            <div>{{ fmtTime(info.not_after) || '-' }}</div>
            <div v-if="info.is_expired" class="tag tag-expired">已过期</div>
            <div v-else class="tag tag-valid">
              剩余 {{ info.days_remaining ?? 0 }} 天
            </div>
          </td>
        </tr>
        <tr v-if="info.san && info.san.length">
          <td class="k">SAN 域名</td>
          <td class="v">
            <div class="san-list">
              <span v-for="(d, i) in info.san" :key="i" class="san-item">{{ d }}</span>
            </div>
          </td>
        </tr>
        <tr>
          <td class="k">序列号</td>
          <td class="v">{{ info.serial_number || '-' }}</td>
        </tr>
        <tr>
          <td class="k">SHA256 指纹</td>
          <td class="v fingerprint">{{ fmtFingerprint(info.fingerprint_sha256) || '-' }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.cert-info-view { padding: 8px; }
.empty-text { text-align: center; padding: 18px; }
.kv-table { width: 100%; border-collapse: collapse; font-size: 12px; table-layout: fixed; }
.kv-table td { padding: 6px 8px; border-bottom: 1px solid var(--on-border-light); vertical-align: top; word-break: break-all; }
.kv-table td.k {
  width: 90px; color: var(--on-text-muted); white-space: nowrap;
  font-weight: 500;
}
.kv-table td.v { color: var(--on-text); }
.tag {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  font-size: 11px; margin-top: 4px; font-weight: 600;
}
.tag-expired {
  color: var(--on-error); background: rgba(248,81,73,0.12);
  border: 1px solid rgba(248,81,73,0.4);
}
.tag-valid {
  color: var(--on-ok); background: rgba(63,185,80,0.12);
  border: 1px solid rgba(63,185,80,0.4);
}
.san-list { display: flex; flex-wrap: wrap; gap: 4px; }
.san-item {
  display: inline-block; padding: 1px 6px; border-radius: 3px;
  background: var(--on-bg); border: 1px solid var(--on-border-light);
  font-size: 11px; color: var(--on-text-muted);
}
.fingerprint { word-break: break-all; line-height: 1.5; }
</style>
