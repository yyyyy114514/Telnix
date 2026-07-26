<script setup lang="ts">
/**
 * WinDivert 风险提示全局对话框。
 *
 * 触发逻辑：axios 响应拦截器检测到后端返回 403 + need_ack=true（或 code=-1 + need_ack=true）
 * 时，调用 stores/windivertWarning.ts 的 waitForWindivertAck() 弹出此对话框。
 *
 * 用户操作：
 * - 「了解，不再显示此提示」（primary）：标记 ack=1（永久不再提示）+ 重试原请求
 * - 「取消」（default）：不开功能，下次再触发时再弹
 *
 * 暗色模式适配：用 var(--on-*) CSS 变量，自动跟随主题。
 * 弹窗居中显示，仅内部可滚动（长文本不撑破布局）。
 */
import { computed } from 'vue'
import {
  visible,
  message,
  acceptWindivertWarning,
  cancelWindivertWarning,
} from '../stores/windivertWarning'

const dialogVisible = computed({
  get: () => visible.value,
  set: (v: boolean) => {
    // 用户点 X / 遮罩 / ESC 关闭时按「取消」处理
    if (!v) cancelWindivertWarning()
  },
})

function onAccept() {
  acceptWindivertWarning()
}

function onCancel() {
  cancelWindivertWarning()
}
</script>

<template>
  <el-dialog
    v-model="dialogVisible"
    title="WinDivert 驱动风险提示"
    width="560px"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    align-center
    class="windivert-warning-dialog"
  >
    <div class="warning-body">
      <!-- 顶部图标 + 标题 -->
      <div class="warning-header">
        <div class="warning-icon">
          <el-icon><WarningFilled /></el-icon>
        </div>
        <div class="warning-title-block">
          <div class="warning-title">即将加载 WinDivert 内核驱动</div>
        </div>
      </div>

      <!-- 风险说明正文（来自后端，保持换行） -->
      <div class="warning-message">{{ message }}</div>

      <!-- 兜底提示：驱动被拦截的处理 -->
      <div class="warning-points">
        <div class="point">
          <el-icon class="point-icon ok"><CircleCheckFilled /></el-icon>
          <span>若驱动加载被拦截，请将 Telnix 目录与 WinDivert64.sys 加入杀软白名单后重试。</span>
        </div>
      </div>
    </div>

    <template #footer>
      <div class="dialog-footer">
        <el-button @click="onCancel">取消</el-button>
        <el-button type="primary" @click="onAccept">了解，不再显示此提示</el-button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
/* 弹窗整体：圆角 + 阴影 + 暗色模式适配 */
:deep(.windivert-warning-dialog) {
  border-radius: var(--on-radius-lg);
  overflow: hidden;
  box-shadow: var(--on-shadow-lg);
}

.warning-body {
  max-height: 60vh;
  overflow-y: auto;
  padding-right: 4px;
}

/* 顶部图标 + 摘要 */
.warning-header {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 14px;
}
.warning-icon {
  font-size: 28px;
  color: var(--on-warn);
  flex-shrink: 0;
  line-height: 1;
  filter: drop-shadow(0 0 6px rgba(154, 103, 0, 0.25));
}
.warning-title-block {
  flex: 1;
  min-width: 0;
}
.warning-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--on-text);
  margin-bottom: 4px;
}

/* 风险说明正文（保持换行） */
.warning-message {
  font-size: 13px;
  color: var(--on-text);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  padding: 10px 12px;
  background: var(--on-bg-elevated);
  border: 1px solid var(--on-border-light);
  border-radius: var(--on-radius-md);
  margin-bottom: 12px;
}

/* 提示要点列表 */
.warning-points {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.point {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12.5px;
  color: var(--on-text-muted);
  line-height: 1.6;
}
.point-icon {
  font-size: 13px;
  flex-shrink: 0;
  margin-top: 3px;
}
.point-icon.warn { color: var(--on-warn); }
.point-icon.ok { color: var(--on-ok); }
.point b {
  color: var(--on-ok);
  font-weight: 600;
}

/* 底部按钮：右对齐，主按钮在右 */
.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

/* 滚动条样式（暗色模式下可见） */
.warning-body::-webkit-scrollbar {
  width: 6px;
}
.warning-body::-webkit-scrollbar-thumb {
  background: var(--on-border);
  border-radius: 3px;
}
.warning-body::-webkit-scrollbar-track {
  background: transparent;
}
</style>
