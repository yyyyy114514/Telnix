/**
 * Telnix 统一颜色常量
 * 所有颜色必须使用 CSS 变量或此文件中的语义化颜色，
 * 禁止在组件中硬编码颜色值。
 *
 * 使用方式：
 * - 组件内: color: var(--on-accent)
 * - JS 中: import { STATUS_COLORS } from '../utils/colors'
 */

export const STATUS_COLORS = {
  // HTTP 状态码
  ok: 'var(--on-ok)',         // 2xx
  redirect: 'var(--on-redirect)', // 3xx
  warn: 'var(--on-warn)',     // 4xx
  error: 'var(--on-error)',   // 5xx
  pending: 'var(--on-pending)', // 无状态
}

export const SEVERITY_COLORS = {
  high: 'var(--on-error)',      // 严重
  medium: 'var(--on-warn)',     // 中等
  low: 'var(--on-amber)',       // 低
  info: 'var(--on-text-muted)', // 提示
}

export const SEVERITY_BG = {
  high: 'var(--on-rose-glow)',
  medium: 'var(--on-amber-glow)',
  low: 'rgba(245, 158, 11, 0.12)',
  info: 'var(--on-bg-hover)',
}

// 分析视图饼图/柱状图配色（10 色，确保亮暗对比度足够）
export const CHART_COLORS = [
  'var(--on-accent)',   // 青绿
  'var(--on-amber)',    // 琥珀
  'var(--on-purple)',   // 紫
  'var(--on-rose)',     // 玫瑰
  'var(--on-blue)',     // 蓝
  'var(--on-emerald)',  // 绿
  'var(--on-indigo)',   // 靛蓝
  'var(--on-cyan)',     // 青
  'var(--on-pink)',     // 粉
  'var(--on-text-dim)', // 灰
]

/**
 * 根据 HTTP 状态码返回语义化颜色
 */
export function getStatusColor(code: number | null): string {
  if (code === null) return STATUS_COLORS.pending
  if (code >= 200 && code < 300) return STATUS_COLORS.ok
  if (code >= 300 && code < 400) return STATUS_COLORS.redirect
  if (code >= 400 && code < 500) return STATUS_COLORS.warn
  if (code >= 500) return STATUS_COLORS.error
  return STATUS_COLORS.pending
}

/**
 * 根据严重程度返回颜色
 */
export function getSeverityColor(severity: 'high' | 'medium' | 'low' | 'info'): string {
  return SEVERITY_COLORS[severity] || SEVERITY_COLORS.info
}

/**
 * 根据严重程度返回背景色
 */
export function getSeverityBg(severity: 'high' | 'medium' | 'low' | 'info'): string {
  return SEVERITY_BG[severity] || SEVERITY_BG.info
}
