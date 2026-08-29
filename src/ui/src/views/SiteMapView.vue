<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { api, type SiteMap, type SiteMapHost, type SiteMapNode } from '../api/client'
import { useFlowsStore } from '../stores/flows'

// 站点地图：以树形结构展示所有访问过的 URL 路径（Burp Suite 风格）
// - 顶层节点为主机（host），下层按 URL 路径段逐层展开
// - 叶子节点（有 methods 的路径）单击可跳转抓包页并按该 URL 过滤
const { t } = useI18n()
const router = useRouter()
const flows = useFlowsStore()

const treeRef = ref<any>(null)
const siteMap = ref<SiteMap | null>(null)
const loading = ref(false)
const searchText = ref('')

// el-tree 需要统一的 children 字段，host 节点和 path 节点都有 children
const treeData = computed(() => siteMap.value?.hosts || [])

// 过滤函数：搜索文本匹配 host 或 path
function filterNode(value: string, data: any) {
  if (!value) return true
  const v = value.toLowerCase()
  if (data.host) return data.host.toLowerCase().includes(v)
  if (data.path) return data.path.toLowerCase().includes(v)
  return false
}

watch(searchText, (v) => {
  treeRef.value?.filter(v)
})

// 统计信息
const totalHosts = computed(() => siteMap.value?.hosts.length || 0)
const totalPaths = computed(() => {
  let n = 0
  for (const h of siteMap.value?.hosts || []) n += h.path_count
  return n
})
const totalRequests = computed(() => {
  let n = 0
  for (const h of siteMap.value?.hosts || []) n += h.request_count
  return n
})

async function loadSiteMap() {
  loading.value = true
  try {
    siteMap.value = await api.getSiteMap()
    await nextTick()
    treeRef.value?.filter(searchText.value)
  } catch (e: any) {
    ElMessage.error(t('siteMap.loadFailed') + (e?.message || e))
  } finally {
    loading.value = false
  }
}

// 展开/收起所有节点（el-tree 没有内置方法，需遍历 store 节点）
function expandAll() {
  const tree = treeRef.value
  if (!tree) return
  const expand = (nodes: any[]) => {
    nodes.forEach((n: any) => {
      n.expanded = true
      if (n.childNodes?.length) expand(n.childNodes)
    })
  }
  expand(tree.store.root.childNodes)
}
function collapseAll() {
  const tree = treeRef.value
  if (!tree) return
  const collapse = (nodes: any[]) => {
    nodes.forEach((n: any) => {
      n.expanded = false
      if (n.childNodes?.length) collapse(n.childNodes)
    })
  }
  collapse(tree.store.root.childNodes)
}

// 判断是否为叶子节点（有 methods 记录的路径，可跳转抓包页过滤）
function isLeaf(data: any): boolean {
  return !data.host && data.request_count > 0
}

// 判断是否为主机节点
function isHost(data: any): boolean {
  return !!data.host
}

// 方法颜色映射（与抓包页 FlowList 方法着色保持一致）
function methodClass(method: string): string {
  return 'm-' + (method || '').toLowerCase()
}

// 单击节点：
// - 叶子节点（有 methods 的路径）跳转抓包页并按 URL 过滤
// - 父节点（host / 有子路径的中间节点）点击整行展开/收起
function onNodeClick(data: any, nodeObj?: any) {
  // 父节点：点击整行展开/收起（不只能点三角）
  const tree = treeRef.value
  if (tree && nodeObj && nodeObj.childNodes?.length) {
    tree.store.toggleExpanded(nodeObj)
    return
  }
  if (tree && nodeObj && !nodeObj.isLeaf && (isHost(data) || !isLeaf(data))) {
    tree.store.toggleExpanded(nodeObj)
    return
  }
  // 叶子节点：跳转抓包页并按 URL 过滤
  if (isHost(data) || !isLeaf(data)) return
  const path: string = data.path || ''
  if (!path) return
  // DSL 语法：~u <path> 表示按 URL 路径包含过滤
  const dsl = `~u ${path}`
  // 设置 store 待应用过滤器（FlowList onMounted 时消费，解决跨页时序问题）
  flows.pendingDslFilter = dsl
  // 同时派发全局事件（如果 FlowList 已挂载，如用户已在抓包页）
  window.dispatchEvent(new CustomEvent('telnix:set-flow-filter', {
    detail: { dsl }
  }))
  router.push('/capture')
}

onMounted(() => {
  loadSiteMap()
})
</script>

<template>
  <div class="sitemap-page full flex flex-col">
    <!-- 顶部工具栏 -->
    <div class="sitemap-toolbar">
      <el-button size="small" @click="() => router.push(flows.lastPage || '/analyze')" :title="t('common.back')">
        <el-icon><ArrowLeft /></el-icon>&nbsp;{{ t('common.back') }}
      </el-button>
      <span class="page-title">
        <el-icon><Share /></el-icon>&nbsp;{{ t('nav.siteMap') }}
      </span>
      <el-input
        v-model="searchText"
        :placeholder="t('siteMap.searchPlaceholder')"
        size="small"
        clearable
        style="width: 240px"
      >
        <template #prefix>
          <el-icon><Search /></el-icon>
        </template>
      </el-input>
      <el-button size="small" :loading="loading" @click="loadSiteMap">
        <el-icon><Refresh /></el-icon>&nbsp;{{ t('common.refresh') }}
      </el-button>
      <el-button size="small" @click="expandAll">{{ t('siteMap.expandAll') }}</el-button>
      <el-button size="small" @click="collapseAll">{{ t('siteMap.collapseAll') }}</el-button>
      <div class="flex-1"></div>
      <span class="text-dim summary">
        {{ t('siteMap.summary', { hosts: totalHosts, paths: totalPaths, requests: totalRequests }) }}
      </span>
    </div>
    <!-- 树形主体 -->
    <div class="sitemap-body flex-1 overflow-auto">
      <div v-if="!loading && !treeData.length" class="empty-text text-dim">
        {{ t('siteMap.emptyHint') }}
      </div>
      <el-tree
        v-else
        ref="treeRef"
        :data="treeData"
        :props="{ children: 'children', label: (d: any) => d.host || d.path }"
        :filter-node-method="filterNode"
        node-key="path"
        :default-expanded-keys="[]"
        highlight-current
        :expand-on-click-node="false"
        @node-click="onNodeClick"
      >
        <template #default="{ data }">
          <!-- 主机节点 -->
          <span v-if="isHost(data)" class="tree-node host-node">
            <el-icon class="node-icon"><Connection /></el-icon>
            <span class="node-label mono">{{ data.host }}</span>
            <span class="node-badge">{{ t('siteMap.pathCount', { n: data.path_count }) }}</span>
            <span class="node-count">{{ data.request_count }}</span>
          </span>
          <!-- 路径节点 -->
          <span v-else class="tree-node path-node" :class="{ leaf: isLeaf(data) }">
            <span class="node-label mono text-truncate">{{ data.path }}</span>
            <!-- 方法标签（仅叶子节点显示） -->
            <template v-if="data.methods && data.methods.length">
              <span
                v-for="m in data.methods"
                :key="m.method"
                class="method-tag mono"
                :class="methodClass(m.method)"
              >{{ m.method }} {{ m.count }}</span>
            </template>
            <span v-if="data.request_count" class="node-count">{{ data.request_count }}</span>
            <el-icon v-if="isLeaf(data)" class="jump-icon" :title="t('siteMap.jumpHint')"><Right /></el-icon>
          </span>
        </template>
      </el-tree>
    </div>
  </div>
</template>

<style scoped>
.sitemap-page { background: var(--on-bg); padding: 12px; }
.sitemap-toolbar {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding-bottom: 10px; border-bottom: 1px solid var(--on-border-light);
}
.page-title { font-size: 14px; font-weight: 600; color: var(--on-text); display: flex; align-items: center; }
.summary { font-size: 11px; white-space: nowrap; }
.sitemap-body { padding-top: 8px; }

.tree-node {
  display: flex; align-items: center; gap: 5px;
  padding: 1px 3px; font-size: 12px;
  flex: 1; min-width: 0;
}
.node-icon { font-size: 12px; color: var(--on-accent); flex-shrink: 0; }
.node-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.host-node .node-label { font-weight: 600; color: var(--on-text); }
.host-node { cursor: pointer; }
.host-node:hover { background: var(--on-bg-hover); border-radius: 3px; }
.path-node .node-label { color: var(--on-text-muted); }
.path-node.leaf { cursor: pointer; }
.path-node.leaf:hover .node-label { color: var(--on-accent); }
.path-node.leaf:hover { background: var(--on-bg-hover); border-radius: 3px; }

.node-badge {
  font-size: 9px; padding: 0 4px; border-radius: 8px;
  background: var(--on-bg-hover); color: var(--on-text-dim);
  flex-shrink: 0;
}
.node-count {
  font-size: 10px; color: var(--on-text-dim);
  flex-shrink: 0; min-width: 24px; text-align: right;
}
.method-tag {
  font-size: 9px; padding: 0 4px; border-radius: 3px;
  background: var(--on-bg-elevated); border: 1px solid var(--on-border-light);
  flex-shrink: 0; font-weight: 600;
}
.m-get { color: var(--on-ok); }
.m-post { color: var(--on-redirect); }
.m-put { color: var(--on-warn); }
.m-delete { color: var(--on-error); }
.m-patch { color: var(--on-purple, var(--on-accent)); }
.m-options, .m-head { color: var(--on-text-dim); }
.jump-icon {
  font-size: 11px; color: var(--on-text-dim);
  flex-shrink: 0; margin-left: auto;
}
.path-node.leaf:hover .jump-icon { color: var(--on-accent); }

.text-truncate { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.empty-text { text-align: center; padding: 40px; line-height: 1.8; }

/* el-tree 节点高度与间距微调（紧凑模式） */
:deep(.el-tree-node__content) { height: 24px; }
:deep(.el-tree-node) { margin-bottom: 0; }
:deep(.el-tree-node__content) { font-size: 12px; }
</style>
