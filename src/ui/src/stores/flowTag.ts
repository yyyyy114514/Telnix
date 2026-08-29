import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export interface FlowTag {
  id: string
  name: string
  color: string
  created_at: string
}

export interface AutoTagRule {
  id: string
  name: string
  tag_id: string
  // DSL 格式的条件
  dsl: string
  enabled: boolean
}

const STORAGE_KEY = 'telnix_flow_tags'
const AUTO_RULES_KEY = 'telnix_auto_tag_rules'

// 预设颜色
export const TAG_COLORS = [
  '#f43f5e', // 红色
  '#f97316', // 橙色
  '#eab308', // 黄色
  '#22c55e', // 绿色
  '#06b6d4', // 青色
  '#3b82f6', // 蓝色
  '#8b5cf6', // 紫色
  '#ec4899', // 粉色
  '#64748b', // 灰色
]

export const useFlowTagStore = defineStore('flowTag', () => {
  // 标记列表
  const tags = ref<FlowTag[]>([])

  // 自动标记规则
  const autoRules = ref<AutoTagRule[]>([])

  // 持久化
  function persist() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(tags.value))
    localStorage.setItem(AUTO_RULES_KEY, JSON.stringify(autoRules.value))
  }

  // 加载
  function load() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY)
      if (saved) {
        tags.value = JSON.parse(saved)
      } else {
        // 默认标记
        tags.value = [
          { id: 'important', name: '重要', color: '#f43f5e', created_at: new Date().toISOString() },
          { id: 'reviewed', name: '已审查', color: '#22c55e', created_at: new Date().toISOString() },
          { id: 'bug', name: 'Bug', color: '#f97316', created_at: new Date().toISOString() },
        ]
      }
      const savedRules = localStorage.getItem(AUTO_RULES_KEY)
      if (savedRules) {
        autoRules.value = JSON.parse(savedRules)
      }
    } catch { /* ignore */ }
  }

  // 添加标记
  function addTag(name: string, color: string): FlowTag {
    const tag: FlowTag = {
      id: 'tag_' + Date.now(),
      name,
      color,
      created_at: new Date().toISOString(),
    }
    tags.value.push(tag)
    persist()
    return tag
  }

  // 更新标记
  function updateTag(id: string, updates: Partial<Omit<FlowTag, 'id' | 'created_at'>>) {
    const tag = tags.value.find(t => t.id === id)
    if (tag) {
      Object.assign(tag, updates)
      persist()
    }
  }

  // 删除标记
  function deleteTag(id: string) {
    tags.value = tags.value.filter(t => t.id !== id)
    // 同时删除关联的自动规则
    autoRules.value = autoRules.value.filter(r => r.tag_id !== id)
    persist()
  }

  // 获取标记颜色
  function getTagColor(id: string): string {
    const tag = tags.value.find(t => t.id === id)
    return tag?.color || '#64748b'
  }

  // 获取标记名称
  function getTagName(id: string): string {
    const tag = tags.value.find(t => t.id === id)
    return tag?.name || id
  }

  // 自动标记规则
  function addAutoRule(name: string, tagId: string, dsl: string): AutoTagRule {
    const rule: AutoTagRule = {
      id: 'rule_' + Date.now(),
      name,
      tag_id: tagId,
      dsl,
      enabled: true,
    }
    autoRules.value.push(rule)
    persist()
    return rule
  }

  function updateAutoRule(id: string, updates: Partial<Omit<AutoTagRule, 'id'>>) {
    const rule = autoRules.value.find(r => r.id === id)
    if (rule) {
      Object.assign(rule, updates)
      persist()
    }
  }

  function deleteAutoRule(id: string) {
    autoRules.value = autoRules.value.filter(r => r.id !== id)
    persist()
  }

  // 根据自动规则检测 flow 是否匹配
  function matchAutoRules(flow: any): string[] {
    const matchedTagIds: string[] = []
    for (const rule of autoRules.value) {
      if (!rule.enabled) continue
      // TODO: 实现 DSL 解析匹配逻辑
      // 简单实现：支持 host/method/status 等字段匹配
      // const conditions = parseAutoRuleDsl(rule.dsl)
      // if (evaluateConditions(flow, conditions)) {
      //   matchedTagIds.push(rule.tag_id)
      // }
    }
    return matchedTagIds
  }

  // 计算属性：活跃规则数
  const activeRulesCount = computed(() => autoRules.value.filter(r => r.enabled).length)

  // 初始化
  load()

  return {
    tags,
    autoRules,
    addTag,
    updateTag,
    deleteTag,
    getTagColor,
    getTagName,
    addAutoRule,
    updateAutoRule,
    deleteAutoRule,
    matchAutoRules,
    activeRulesCount,
  }
})
