# Telnix 前端 UX 审计报告 (Batch 1)

> 范围：src/ui/src 全部 .vue/store/router；忽略 i18n。整体质量偏高。

## 汇总表

| # | 级别 | 文件:行/组件 | 类别 |
|---|------|------|------|
| UX1 | P1 | AnalyzeView.vue:277-492 | 大列表无虚拟化/分页 |
| UX2 | P1 | FlowList.vue:1680-1701 / RawCaptureView.vue:974-998 | 大列表无虚拟滚动 |
| UX3 | P2 | flows.ts:247/269/335、capture.ts:35/43 | 静默失败无提示 |
| UX4 | P2 | RawCaptureView.vue(工具栏) | 整页清空无二次确认 |
| UX5 | P2 | RawCaptureView.vue:249 | 初始加载无 loading 态 |
| UX6 | P3 | CertBanner.vue:30-34 | a11y 无 role/aria |
| UX7 | P3 | FlowList/AnalyzeView 行 | 键盘可访问性弱 |
| UX8 | P3 | SettingsView 各 switch | 自动保存无未保存指示 |

## 详情

### UX1 [P1] AnalyzeView 无分页/虚拟滚动
AnalyzeView.vue:277 注释"一页显示全部流量，不分页"，pageSize=100000，分组展开时 g.flows 全量渲染(:911-913)。数千条时 DOM 爆炸，展开分组卡顿。建议分组内列表虚拟滚动或恢复分页/懒加载。

### UX2 [P1] 流量列表原生渲染无虚拟滚动
FlowList.vue:1680 与 RawCaptureView.vue:974 用 v-for 直接渲染 displayFlows(store 上限 5000~10000)，仅 v-memo 单元格优化，无窗口化。大数据量滚动持续重渲染。建议引入虚拟滚动(与 UX1 同源)。

### UX3 [P2] 多处异步失败静默
flows.ts:247/269/335(loadFlows/loadAllFlows/pollNewFlows/选中拉取完整 flow)、capture.ts:35/43(fetchStatus/fetchBpStatus) 异常均静默。列表加载可静默，但 select() 拉完整 flow 失败静默(:335) 导致 Inspector 空白无反馈。建议选中详情失败给 ElMessage 轻提示。

### UX4 [P2] 原始抓包页整页清空缺二次确认
RawCaptureView.vue 工具栏含批量删除确认(:511)与子悬浮窗确认(:1035)，但未发现整体"清空全部流量"按钮的 ElMessageBox.confirm(对比 CaptureView onClear:154 有确认)。若复用 flows.clearLocal() 无确认，误点即清空。建议整页清空加 ElMessageBox.confirm。

### UX5 [P2] 原始抓包页初始加载无 loading
RawCaptureView.vue 无任何 v-loading/loading 标志(0 匹配)，首次进入拉取状态与流量时用户面对空白列表区无法区分"加载中"与"确实无包"。建议初始 flows 空且未加载完成显示 loading 骨架/spinner。

### UX6 [P3] 证书横幅缺无障碍语义
CertBanner.vue:30 仅视觉警告条，无 role="alert"/aria-live，屏幕阅读器用户不知证书未安装。建议加 role="alert"。

### UX7 [P3] 流量行键盘导航缺失
FlowList.vue 行 div + @click，未实现 tabindex/role="row"/方向键导航，纯键盘无法遍历/选中(仅鼠标)。建议支持上下键+Enter 选中。

### UX8 [P3] 设置自动保存无未保存态
SettingsView.vue:655-664 多数输入 autoSave 500ms 防抖静默保存，仅成功闪 "saved"(:700)，失败才报错。输入后无"保存中/未保存"可见标记。建议输入框旁显示小标记。

## 总体评价
核心交互(实时流量、筛选、多选、危险操作确认、空态)扎实；短板集中在**大列表性能(UX1/UX2 P1)**与**少数静默失败/缺确认/缺 loading 边界态(UX3/UX4/UX5 P2)**，以及细粒度 a11y(UX6/UX7/UX8 P3)。建议优先 P1 虚拟滚动与 P2 清空确认。
