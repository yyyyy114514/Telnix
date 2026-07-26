/**
 * WinDivert 风险提示对话框的全局状态。
 *
 * 独立于 api/client.ts，避免循环依赖（client.ts 的拦截器会调用 waitForWindivertAck，
 * 而 Dialog 组件会引用此 store 的响应式状态）。
 *
 * 流程：
 * 1. axios 响应拦截器检测到 403 + need_ack=true（或 code=-1 + need_ack=true）
 * 2. 调用 waitForWindivertAck() 弹出全局对话框，返回 Promise
 * 3. 用户点「了解，不再显示此提示」→ resolve(true)，拦截器调 ack API 持久化 + 重试原请求
 * 4. 用户点「取消」→ resolve(false)，拦截器拒绝原请求
 */
import { ref } from 'vue'

/** 弹窗可见性（Dialog 组件 v-model 绑定） */
export const visible = ref(false)

/** 弹窗正文（来自后端 /system/windivert-warning 的 message 字段） */
export const message = ref('')

/** 弹窗简短描述（brief 字段，用于弹窗顶部摘要） */
export const brief = ref('')

/** 内部 resolver：用户响应后调用 */
let _resolver: ((accepted: boolean) => void) | null = null

/** 当前是否有弹窗正在等待用户响应（防并发：同时只弹一个） */
export const pending = ref(false)

/**
 * 弹出 WinDivert 风险提示对话框，返回 Promise。
 *
 * @param msg 风险说明正文（来自后端）
 * @param briefMsg 简短描述（可选）
 * @returns true=用户确认（应调 ack API 持久化 + 重试原请求），false=用户取消
 */
export function waitForWindivertAck(msg: string, briefMsg?: string): Promise<boolean> {
  // 若已有弹窗在等待，复用同一个 Promise（避免并发请求触发多个弹窗）
  if (pending.value && _resolver) {
    return new Promise<boolean>((resolve) => {
      // 链式：等当前弹窗结束后，把结果也传给后到的等待者
      const prevResolver = _resolver
      _resolver = (accepted: boolean) => {
        prevResolver?.(accepted)
        resolve(accepted)
      }
    })
  }
  message.value = msg || ''
  brief.value = briefMsg || ''
  visible.value = true
  pending.value = true
  return new Promise<boolean>((resolve) => {
    _resolver = resolve
  })
}

/** 用户点「了解，不再显示此提示」按钮时调用 */
export function acceptWindivertWarning(): void {
  visible.value = false
  pending.value = false
  const r = _resolver
  _resolver = null
  r?.(true)
}

/** 用户点「取消」按钮或关闭弹窗时调用 */
export function cancelWindivertWarning(): void {
  visible.value = false
  pending.value = false
  const r = _resolver
  _resolver = null
  r?.(false)
}
