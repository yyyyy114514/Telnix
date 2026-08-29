# 用户需求记录 - 待完成

## 待完成任务列表

### 1. WS包分组和raw选项卡修复
- **状态：待完成** - 抓包页WS不拆分send/recv、WS页分send和recv

### 2. Hex视图优化
- **状态：已完成** ✅ - 禁用Ctrl+F搜索

### 3. 断点性能问题
- **状态：已完成** ✅ - 断点放行后立即更新高亮

### 4. 抓包/断点开关优化
- **状态：已完成** ✅ - 保护期延长至3s避免闪烁

### 5. 时序图修复
- **状态：已完成** ✅ - 箭头渲染修复，默认关闭（已在optionalTabs中）

### 6. 触发式捕获弹窗
- **状态：已完成** ✅ - 改为筛选按钮风格的条件配置

### 7. 分隔线限制
- **状态：已完成** ✅ - 抓包页/WSS/TCP详情分隔线可拖动，有比例限制

### 8. TCP/UDP忽略功能
- **状态：已完成** ✅ - raw_capture.py 和 raw_capture_unix.py 均添加忽略检查

### 9. TCP/UDP详情优化
- **状态：已完成** ✅ - 移除raw_data选项，修复按钮换行

### 10. 启动加载优化
- **状态：已完成** ✅ - loadAllFlows用requestAnimationFrame异步执行

### 11. 竞品调研
- **状态：已完成** ✅ - 写入 docs/COMPETITIVE_ANALYSIS.md

## 已完成（可删除时清理）
✅ Hex禁用Ctrl+F
✅ 断点放行后立即更新
✅ 抓包/断点开关保护期3s
✅ 时序图箭头修复+默认关闭
✅ 触发式捕获弹窗改版
✅ 分隔线可拖动+比例限制
✅ TCP/UDP忽略功能
✅ TCP/UDP详情优化
✅ 启动加载优化
✅ 竞品调研报告
✅ 前端构建测试通过
✅ 类型检查零错误
✅ 请求重写增强（header条件、正则匹配）— 后端proxy_tools.py/前端ToolsView.vue
✅ 证书信息增强（复制、状态标签、30天过期警告）— CertInfoView.vue/locales
✅ 多会话管理增强（会话颜色标记）— DB迁移/后端API/前端工具栏/CLI/MCP
✅ 后端构建验证（proxy_tools.py/db.py/sessions.py/cli.py/mcp_server.py）
✅ 触发式捕获支持通配符（后端 DSL 正则 ~op）
✅ 触发式捕获按钮白字，select 不关闭弹窗
✅ 触发式捕获设置启用开关，条件持久化 DB
✅ WS/TCP/UDP 右键菜单 hover 子菜单样式
✅ 拓扑图 SVG 动态高度 + 节点间距优化
✅ Clash is_reachable 改为 /version API 调用（纯端口检测）
✅ WS 详情 raw 用 RawView 组件（样式一致）
✅ WS 页 1 秒延迟修复
✅ 抓包页详情分割线 32%-68% 限位（不遮挡多选/自动滚动按钮）
✅ 前端构建验证零错误

## 待完成（删除前需确认）
❌ WS包分组（抓包页不拆分send/recv，WS页分send和recv）
❌ 触发式捕获条件持久化 DB（后端 trigger.py 已完成，前端需完成）

