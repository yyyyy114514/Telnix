<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { useCaptureStore } from '../stores/capture'

// 证书未安装提示条
const capture = useCaptureStore()

async function install() {
  try {
    await ElMessageBox.confirm(
      '将向系统证书存储安装 Telnix 根证书以启用 HTTPS 解密（SSL bump）。继续？',
      '安装证书',
      { confirmButtonText: '安装', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await capture.installCert()
    ElMessage.success('证书安装请求已提交')
  } catch (e: any) {
    ElMessage.error('证书安装失败：' + (e?.message || e))
  }
}
</script>

<template>
  <div v-if="!capture.status.cert_installed" class="cert-banner">
    <el-icon class="banner-icon"><WarningFilled /></el-icon>
    <span class="banner-text">HTTPS 解密未启用 — 未安装根证书</span>
    <el-button size="small" type="primary" @click="install">点击安装证书</el-button>
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
