<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { api } from '../api/client'

// Clash 集成页：显示连接状态 + 集成开关（控制流量是否走 Mihomo 代理）
const status = ref<any>(null)
const loading = ref(false)
const toggling = ref(false)

async function refresh() {
  loading.value = true
  try {
    status.value = await api.clashStatus()
    // 集成已启用但 Mihomo 不可达：流量自动直连（不走代理）
    if (status.value?.integrated && !status.value?.reachable) {
      ElMessage.warning('Mihomo 未连接，流量已自动直连')
    }
  } catch (e: any) {
    ElMessage.error('获取状态失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

async function onToggle(val: boolean | string | number) {
  toggling.value = true
  try {
    if (val) {
      // 后端探测可达性，不可达返回 err（1 秒只发生在用户主动点开关时）
      await api.clashEnable({})
      ElMessage.success('Clash 集成已启用，Mihomo 在线')
    } else {
      await api.clashDisable()
      ElMessage.info('Clash 集成已禁用（流量直连）')
    }
    await refresh()
  } catch (e: any) {
    // 启用失败（Mihomo 未连接）：refresh 让开关回弹到真实状态
    ElMessage.error('操作失败: ' + e.message)
    await refresh()
  } finally {
    toggling.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <div class="clash-view">
    <div class="clash-card" v-loading="loading">
      <div class="clash-header">
        <el-icon class="clash-icon" :class="{ ok: status?.reachable, off: !status?.reachable }">
          <ClashIcon />
        </el-icon>
        <div class="clash-title">
          <div class="title-text">Clash 集成</div>
          <div class="title-sub">外接模式 · 启用后流量经 Mihomo 转发</div>
        </div>
        <el-button
          class="refresh-btn"
          size="small"
          circle
          :loading="loading"
          @click="refresh"
          title="刷新状态"
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

      <div class="clash-status">
        <template v-if="status?.reachable">
          <div class="status-row">
            <span class="status-label">连接状态</span>
            <span class="status-val ok">已连接</span>
          </div>
          <div class="status-row">
            <span class="status-label">版本</span>
            <span class="status-val mono">{{ status.version || '—' }}</span>
          </div>
          <div class="status-row">
            <span class="status-label">混合代理端口</span>
            <span class="status-val mono">{{ status.mixed_port || '—' }}</span>
          </div>
          <div class="status-row">
            <span class="status-label">运行模式</span>
            <span class="status-val">{{ status.mode || '—' }}</span>
          </div>
          <div class="status-row" v-if="status.upstream_proxy">
            <span class="status-label">上游代理</span>
            <span class="status-val mono">{{ status.upstream_proxy }}</span>
          </div>
        </template>
        <template v-else>
          <div class="status-row">
            <span class="status-label">连接状态</span>
            <span class="status-val fail">未连接</span>
          </div>
          <div class="status-hint">
            <template v-if="status?.integrated">
              Clash 集成已启用，但 Mihomo 未连接。流量将自动直连（不走代理）。<br/>
              请启动 Clash/Mihomo 客户端，连接恢复后流量将自动走代理。
            </template>
            <template v-else>
              请确认 Clash/Mihomo 客户端已启动，并在上方开启集成开关让流量走代理。
            </template>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.clash-view {
  padding: 24px;
  display: flex;
  justify-content: center;
}
.clash-card {
  width: 100%;
  max-width: 560px;
  background: var(--el-bg-color, #1a1a1a);
  border: 1px solid var(--el-border-color, #333);
  border-radius: 12px;
  padding: 24px;
}
.clash-header {
  display: flex;
  align-items: center;
  gap: 16px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--el-border-color, #333);
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
.refresh-btn {
  margin-right: 8px;
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
.clash-status {
  padding-top: 20px;
}
.status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 0;
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
  background: var(--el-fill-color-light, #262626);
  border-radius: 8px;
  font-size: 13px;
  color: var(--el-text-color-secondary, #888);
  line-height: 1.6;
}
</style>
