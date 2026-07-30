# F14 — 抓包页 i18n 未翻译问题审查与修复

- 日期：2026-07-30
- 范围：抓包页（`CaptureView`、`RawCaptureView`）及其子组件（`FlowList`、`ReplayDialog` 等）
- 结论：截图中的"未翻译"并非 i18n key 缺失，而是**硬编码英文文本**在中文界面下直接显示英文；已补齐 locale key 并将硬编码改为 `t()` 调用，复验 0 真实缺失。

## 1. 问题背景

用户反馈抓包页截图显示大量文本未翻译（显示为英文）。项目 i18n 默认语言为中文（`src/ui/src/i18n.ts` 中 `fallbackLocale: 'zh'`），缺 key 会直接显示 key 原文；若显示的是可读英文而非 key 原文，则说明是硬编码英文。

## 2. 审查方法

1. 使用 `i18n_check.mjs` 对 `src/ui/src` 下所有 `.vue`/`.ts` 做静态扫描：
   - 提取所有 `t('x.y')` 静态 key 与数据字面量 key；
   - 比对 `locales/zh.ts`、`locales/en.ts` 是否双向存在；
   - 扫描"值等于 key"或空值等异常。
2. 对抓包页相关组件人工排查硬编码英文文本。

## 3. 根因

扫描结果显示静态/数据字面量 key 在 zh/en 中均存在且无异常值（仅 `flows.autoScroll`、`flows.autoScrollDelay`、`settings.json` 三项误报，分别是 Pinia store 属性名与文件名字面量，非 `t()` 调用）。

因此"未翻译"的来源是抓包页及其子组件中的**硬编码英文**：

| 位置 | 硬编码英文 |
|------|-----------|
| `FlowList.vue` 列头 | `Host` / `URL` / `Content-Type` / `PID` / `Path` / 等 |
| `FlowList.vue` 复制菜单 | `URL` / `cURL` / `Host` / `Path` / `Content-Type` / `PID` |
| `CaptureView.vue` 导出下拉 | `Postman Collection` |
| `RawCaptureView.vue` 预设按钮 | `HTTP 80` / `HTTPS 443` / `DNS 53` / `NTP 123` |
| `RawCaptureView.vue` 单选 / 树根 | `Hex` / `root` |
| `ReplayDialog.vue` 表单标签 | `Method` / `Host` / `Port` / `Body` / `Headers` |

## 4. 修复内容

### 4.1 新增 locale key（`locales/zh.ts`、`locales/en.ts` 双份）

- `capture.exportPostman` → `Postman Collection` / `Postman Collection`
- `raw.root` → `root` / `root`
- `raw.presetHttp80/HTTPS443/Dns53/Ntp123` → `HTTP 80` / `HTTPS 443` / `DNS 53` / `NTP 123`
- `raw.hex` → `十六进制` / `Hex`
- `flowList.colHost/colUrl/colContentType/colPid/colPath/colCurl` → `主机/网址/内容类型/进程ID/路径/cURL` 及英文对应
- `replay.methodLabel/hostLabel/portLabel/bodyLabel/headersLabel` → `方法/主机/端口/请求体/请求头` 及英文对应

### 4.2 组件改造（硬编码英文 → `t()` 调用）

- `CaptureView.vue`：导出项 `Postman Collection` → `{{ t('capture.exportPostman') }}`
- `RawCaptureView.vue`：4 个预设按钮 → `{{ t('raw.presetHttp80') }}` 等；`Hex` 单选 → `{{ t('raw.hex') }}`；`'root'` 字面量（4 处）→ `t('raw.root')`
- `ReplayDialog.vue`：5 个表单标签 → `{{ t('replay.methodLabel') }}` 等
- `FlowList.vue`：
  - `COL_DEFS` 列头 `label` 由英文改为 key 字符串（`flowList.colHost` / `flowList.colUrl` / `flowList.colContentType` / `flowList.colPid` / `flowList.colPath` / `common.method` / `common.protocol` / `common.size` / `common.duration` / `flowList.colResult` 等）
  - `COPY_FIELD_DEFS` 复制菜单 `label` 由英文改为 key 字符串
  - 表头渲染 `{{ c.label }}` → `{{ t(c.label) }}`
  - 复制菜单渲染 `{{ item.label }}` → `{{ t(item.label) }}`

## 5. 验证

- `node i18n_check.mjs`：USED KEYS 1137，缺失仅 3 项误报（`flows.autoScroll`/`flows.autoScrollDelay` 为 store 属性，`settings.json` 为文件名），无真实缺失 key；异常值扫描为空。
- `read_lints` 对 `FlowList.vue`：0 错误。

## 6. 涉及文件

- `src/ui/src/locales/zh.ts`、`src/ui/src/locales/en.ts`
- `src/ui/src/views/CaptureView.vue`
- `src/ui/src/views/RawCaptureView.vue`
- `src/ui/src/components/FlowList.vue`
- `src/ui/src/components/ReplayDialog.vue`
- `i18n_check.mjs`（检测脚本）
