<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useCaptureStore } from '../stores/capture'

// 证书未安装提示条
const { t } = useI18n()
const capture = useCaptureStore()

async function install() {
  try {
    await ElMessageBox.confirm(
      t('cert.installConfirmMsg'),
      t('cert.installTitle'),
      { confirmButtonText: t('cert.installButton'), cancelButtonText: t('common.cancel'), type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await capture.installCert()
    ElMessage.success(t('cert.installSubmitted'))
  } catch (e: any) {
    ElMessage.error(t('cert.installFailed') + (e?.message || e))
  }
}
</script>

<template>
  <div v-if="!capture.status.cert_installed" class="cert-banner">
    <el-icon class="banner-icon"><WarningFilled /></el-icon>
    <span class="banner-text">{{ t('cert.bannerText') }}</span>
    <el-button size="small" type="primary" @click="install">{{ t('cert.installCertButton') }}</el-button>
  </div>
</template>

<style scoped>
.cert-banner {
  display: flex; align-items: center; gap: 12px;
  padding: 6px 16px;
  background: linear-gradient(90deg, rgba(210, 153, 34, 0.18), rgba(210, 153, 34, 0.08));
  border-bottom: 1px solid rgba(210, 153, 34, 0.35);
  color: var(--on-warn);
  font-size: 12.5px;
}
.banner-icon { font-size: 16px; }
.banner-text { flex: 1; }
</style>
