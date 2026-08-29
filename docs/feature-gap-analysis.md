# Telnix 功能差距分析报告

> 基于三份竞品调研报告 + Telnix 源码调研，筛选 Telnix 尚未实现且价值高的特性。
> 生成日期：2026-08-03

---

## 一、竞品特性总览

### 1.1 商业抓包软件（17 款，4 大类别）

#### 企业级网络性能监控平台（5 款）

| 工具 | 差异化特性 |
|---|---|
| **SolarWinds NPM** | NetPath 逐跳路径可视化、PerfStack 交叉栈关联、AIOps 异常检测 |
| **PRTG** | Packet Sniffer 传感器、250+ 预配置传感器、带宽趋势 |
| **ManageEngine NFA** | DPI + NBAR 应用识别、ML 行为分析 + MITRE ATT&CK、Flow Publisher 扩展维度 |
| **WhatsUp Gold NTA+** | NTA+ Collector 集中架构、SSL 证书监控、95th percentile 计费报告 |
| **Colasoft Capsa** | 内置网络工具套件（Packet Builder/Packet Player/IP Geolocation）、自定义协议分析、VoIP 图形化分析 |

#### 企业级网络分析平台（5 款）

| 工具 | 差异化特性 |
|---|---|
| **Riverbed AppResponse/Observer** | 2.4 PB 存储、全保真 50+ Gbps 零采样、响应时间组成图、Riverbed IQ AI 四类引擎、加密流量实时取证 |
| **NetScout nGeniusONE** | ASI 自适应服务智能采集（Smart Data）、SD-WAN 覆盖映射、VPN 链路健康监控 |
| **ExtraHop Reveal(x)** | 无代理被动捕获 + covert decryption、5000+ L2-L7 特征 ML 分析、AI Search Assistant（加速 6 倍）、NDR+NPM+IDS 三合一 |
| **Viavi Observer** | GigaStor 60 Gbps（Tolly 验证最快）、Back-in-Time 回溯、EUE 用户体验评分 |
| **Corvil Analytics** | **200 Gbps 持续无损**（行业最高之一）、600+ 协议解码器、纳秒级时间戳金标准、AI 自然语言描述业务影响 |

#### 云/SaaS 网络监控（4 款）

| 工具 | 差异化特性 |
|---|---|
| **Kentik** | AI Advisor 自然语言多步骤调查、BGP 路由与流量路径关联、Universal Data Explorer |
| **ThousandEyes (Cisco)** | Internet 与 WAN 端到端可视化、BGP 路由监控、Browser Synthetics、全球客户聚合遥测 |
| **Datadog Network** | DASH 2026 逐步推理引擎、SNMP Traps + Device Profile 自动发现 |
| **Catchpoint** | Internet Stack Map 互联网栈地图、Internet Sonar 持续扫描、全球 vantage points |

#### 专业抓包与协议分析（3 款）

| 工具 | 差异化特性 |
|---|---|
| **LiveAction OmniPeek** | 100 Gigabit 实时协议分析、VoIP/多媒体分析、WiFi 多信道、专家分析实时告警 |
| **Fluke OptiView/LinkSprinter** | 千兆线速 + 触发式捕获、LinkSprinter 口袋型现场验证（PoE/链路/交换机/DHCP/IP/DNS/网关）、Link-Live 云端报告 |
| **Wireshark + Riverbed Packet Analyzer Plus** | 全球最大协议解码器生态、Lua Dissector、百万级包定位、100μs 微突发视图、conversation ring、back-in-time、Wireshark Certified Analyst 认证 |

### 1.2 GitHub 开源抓包/网络代理（12 款）

| 工具 | Stars | 差异化特性 |
|---|---|---|
| **mitmproxy** | 44.5k | 三种界面模式（TUI/Web/CLI）、HTTP/2 + QUIC、5200+ 项目依赖 |
| **Whistle** | 19k | 类 hosts 语法但功能强大的规则系统、内置 Weinre（远程 DOM 检查）、Console 日志 |
| **AnyProxy** | 18k | 阿里出品、中文生态友好、完全可编程 rule 模块 |
| **GoReplay** | 19.2k | libpcap 旁路监听（非侵入）、流量录制回放到测试环境、速率倍数/流量过滤表达式 |
| **LightProxy** | 2.5k | 零配置自动证书安装 + 系统代理设置 |
| **Bettercap** | 18k | ARP/DNS/DHCP 欺骗、WiFi/BLE/HID 监控、Caplets 自动化攻击脚本、Metasploit 集成 |
| **httpx** | 10k | 极速批量探测、Tech Detect 自动识别技术栈、Pipeline 模式 |
| **Clash** | 48.2k | 规则分流引擎（域名/IP CIDR/进程名）、GeoIP/GeoSite 数据库、规则组策略路由 |
| **Wireshark (mirror)** | 7k | 全球最流行协议分析器、数千协议深度解析 |
| **Nmap** | 10k | NSE Lua 脚本引擎（数千社区脚本）、Ncat/Nping/Ndiff |

### 1.3 GitHub 抓包软件全景（27 款，Star 总量 194K+）

#### 值得关注的差异化特性

| 工具 | Stars | 差异化特性 |
|---|---|---|
| **sniffnet** | 40.2k | 6000+ 上层服务/协议识别（含木马/蠕虫）、远程主机地理定位 + ASN 识别、进程级归属 |
| **kyanos** | 5k | eBPF 多协议请求捕获（HTTP/Redis/MySQL/Kafka/MongoDB/DNS）、多维过滤（进程/容器/L7 协议/字节/延迟）、内核级延迟详情可视化 |
| **ecapture** | 15.4k | **无 CA 证书捕获 SSL/TLS 明文**（eBPF hook 8 大加密库）、MySQL/PostgreSQL SQL 审计、Bash/Zsh 命令审计、8 大模块体系 |
| **kubeshark** | 12k | eBPF 内核级全集群流量索引、加密流量明文解密（TLS/mTLS 无需密钥）、AI 集成（MCP → Claude/Copilot）、KFL 查询语言 |
| **arkime** | 7.4k | 大规模全包捕获与索引（横向扩展到数十 Gbps）、SPI View 字段细分、多集群统一前端 |
| **kyanos** | 5k | eBPF 多协议请求捕获、高级聚合统计、自动 SSL 解密 |
| **PcapXray** | 1.9k | 网络拓扑可视化（Graphviz + pyvis）、Tor 流量检测、DEF CON 27 Demo Labs |
| **scapy** | 12.4k | 数据包构造/解析/发送完整闭环、交互式 Shell + 库双模式 |
| **fq** | 10.5k | 二进制数据"jq+hexdump+dd+gdb"合一、上百种格式解码、交互式 REPL |
| **webshark** | 292 | 类 Wireshark 的 Web 应用 + WASM RTP 回放 |
| **Malcolm (CISA)** | 2.5k | 双可视化（OpenSearch Dashboards + Arkime）、ICS 可见性扩展、多源数据接入 |
| **ostinato** | 773 | L2/L3 流量生成（400Gbps 线速）、Python 自动化 API、逐包/逐字段/逐大小构造 |

---

## 二、Telnix 现有能力边界调研

| 序号 | 特性 | 状态 | 主要证据 |
|---:|---|:---:|---|
| 1 | eBPF 内核级抓包 | ❌ | raw_capture.py（WinDivert + AF_PACKET/BPF 设备，非 eBPF） |
| 2 | 远程主机 ASN 识别 | ❌ | ip_region.py 仅属地 + ISP 名称，无 AS 编号 |
| 3 | TLS 无证书明文解密 | ❌ | ssl_bump.py 必须 CA 证书 MITM |
| 4 | 流量热力图 | ❌ | AnalyzeView.vue 仅 1D 柱状图 |
| 5 | 网络拓扑图 | 🟡 | SiteMapView.vue 是 URL 树（Burp 风格），非网络连接图 |
| 6 | traceroute / mtr 逐跳追踪 | ❌ | 无任何实现 |
| 7 | 回溯时间分析（Back-in-Time） | 🟡 | 数据已持久化，无时间滑块回放 UI |
| 8 | 流量录制回放（GoReplay 模式） | 🟡 | record_replay.py 有，但非全双工镜像 |
| 9 | 进程级流量归属 | ✅ | process_lookup.py 完整实现（Windows 原生 API） |
| 10 | 多维聚合统计 | ✅ | db.py get_flows_stats 支持 6 维度 |
| 11 | DNS 劫持 | ✅ | dns_hijack.py（劫持工具，非检测） |
| 12 | AI 自然语言查询 | ✅ | MCP Server + DeepSeek Agent（双轨并行） |
| 13 | Lua Dissector | ❌ | 无任何实现 |
| 14 | 流量生成/数据包构造 | 🟡 | send.py 仅 HTTP Composer，无底层包构造 |
| 15 | 长时间保留/云存储 | ❌ | 仅本地 SQLite |
| 16 | Wireshark 生态集成 | ❌ | export.py 无 pcap，无 tshark/sharkd 调用 |
| 17 | WebSocket 抓包与构造 | 🟡 | websocket_relay.py 仅中继，无构造 |
| 18 | HTTP/2 + QUIC | 🟡 | h2_forward.py H2 完整，QUIC/HTTP3 完全无 |
| 19 | 弱网模拟（延迟/限速/丢包） | ✅ | throttle.py 三维弱网 |
| 20 | 移动端抓包（Android 免 root） | 🟡 | WiFi 代理 + CA 方案，非免 root |
| 21 | 触发式捕获 | ❌ | 无条件捕获逻辑 |
| 22 | TCP 时序图 | 🟡 | TimelineView.vue 仅 HTTP 瀑布流，无 TCP 时序 |

---

## 三、筛选结论：Telnix 没有且价值高的特性

### 🏆 第一梯队（高价值 + 可落地，强烈推荐）

#### 3.1 pcap / pcapng 导出（打通 Wireshark 生态）

| 维度 | 内容 |
|---|---|
| **竞品参考** | Wireshark、Riverbed、PCAPdroid、arkime、sniffnet 均支持 |
| **Telnix 现状** | export.py 仅 json/har/curl/postman/csv，**完全无 pcap** |
| **价值评分** | ⭐⭐⭐⭐⭐ |
| **价值分析** | 打通 Wireshark 数千种协议 Dissector 生态，等于"借力行业标准"，一举弥补 Telnix 协议解析深度不足 |
| **实现难度** | 中 |
| **实现思路** | flow 表已记录 L7 数据，用 scapy 或手写 pcap-ng 构造 HTTP 层报文，添加假的 Ethernet/IP/TCP 头。不需要抓 L2 帧 |
| **关键决策** | 导出"HTTP 重建版"pcap（非真实网络捕获），在 UI 中清晰标注 |

#### 3.2 回溯时间分析 / 时间滑块回放（Back-in-Time）

| 维度 | 内容 |
|---|---|
| **竞品参考** | Viavi Observer、Riverbed Packet Analyzer Plus |
| **Telnix 现状** | db.py 已持久化所有 flows，TimelineView.vue 只有当前瀑布流，**无历史回放** |
| **价值评分** | ⭐⭐⭐⭐⭐ |
| **价值分析** | 用户能"倒回 5 分钟前/昨天某时刻"看流量状态，做问题复盘和根因定位 |
| **实现难度** | 低 |
| **实现思路** | 前端加时间轴滑块组件，后端按 timestamp 范围查询已有数据 |
| **关键决策** | session 维度回放（已有 sessions 表），加"会话对比"功能对比两个时间段 |

#### 3.3 流量热力图（2D 矩阵：时间 × 维度）

| 维度 | 内容 |
|---|---|
| **竞品参考** | Datadog DASH 2026、商业监控平台普遍 |
| **Telnix 现状** | AnalyzeView.vue 只有 1D 柱状图，**无 2D 热力矩阵** |
| **价值评分** | ⭐⭐⭐⭐ |
| **价值分析** | 一屏看到"哪个时段哪个 host 流量异常"，是现有 AnalyzeView 最直接的增强 |
| **实现难度** | 低 |
| **实现思路** | 纯前端组件 + db.py get_flows_stats(group_by=...) 已有 SQL 支持 |
| **关键决策** | 时间分桶 60s 对齐，维度可切 host/status/content_type/method |

#### 3.4 触发式捕获 + DSL 条件断点

| 维度 | 内容 |
|---|---|
| **竞品参考** | Fluke OptiView（触发式捕获）、Wireshark（capture filter）、kyanos（多维过滤） |
| **Telnix 现状** | 仅手动开始/停止 + 端口过滤 |
| **价值评分** | ⭐⭐⭐⭐ |
| **价值分析** | 调试偶发问题/间歇性故障时，"出现 hostname=xxx.com 或 status=500 时才开始记录"能节省海量噪声 |
| **实现难度** | 中 |
| **实现思路** | 前端加触发条件 UI，后端在 record_flow 前加触发表达式判断 |
| **关键决策** | 复用现有 flowfilter.ts DSL 作为触发表达式，后端同步实现 matchFlow |

### 🥈 第二梯队（差异化 / 价值中高，按需选做）

#### 3.5 响应时间组成图（全链路时延分解）

| 维度 | 内容 |
|---|---|
| **竞品参考** | Riverbed AppResponse（行业标杆） |
| **Telnix 现状** | flows 表只有 duration_ms 总耗时 |
| **价值评分** | ⭐⭐⭐⭐ |
| **价值分析** | 精准定位是 DNS 慢、SSL 慢、还是服务端慢，前端/web 性能优化的关键能力 |
| **实现难度** | 中 |
| **实现思路** | server.py transparent_proxy 主循环埋 timing 点：dns_start/end、tcp_connect_start/end、ssl_handshake_start/end、first_byte、last_byte |

#### 3.6 网络拓扑图（IP 维度连接关系图）

| 维度 | 内容 |
|---|---|
| **竞品参考** | PcapXray（Graphviz + pyvis）、skydive、Catchpoint Internet Stack Map |
| **Telnix 现状** | SiteMapView.vue 是 Burp Suite 风格 URL 路径树，**非网络连接图** |
| **价值评分** | ⭐⭐⭐ |
| **价值分析** | 洞察"哪些 IP 在互相通信"、"哪些进程连了哪些外部服务器"，安全审计/异常发现有价值 |
| **实现难度** | 中 |
| **实现思路** | ECharts Graph 或 D3-force 布局，process→remote_ip→host 三方图，数据源是 flows 表 + process_lookup |

#### 3.7 远程主机 ASN 识别

| 维度 | 内容 |
|---|---|
| **竞品参考** | sniffnet（40.2k Star）、PCAPdroid |
| **Telnix 现状** | ip_region.py 只有属地 + ISP 名称，**无 AS 编号** |
| **价值评分** | ⭐⭐⭐ |
| **价值分析** | 识别"这个 IP 属于阿里云/腾讯云/CDN 还是真实企业用户"，对反爬/风控/审计有意义 |
| **实现难度** | 低 |
| **实现思路** | 下载 [GeoLite2-ASN.mmdb](https://github.com/P3TERX/GeoLite.mmdb) + `maxminddb` 库，与 ip2region 并行查询 |

#### 3.8 TCP 时序图（seq/ack/重传/RTT 分析）

| 维度 | 内容 |
|---|---|
| **竞品参考** | Wireshark、Colasoft Capsa、Riverbed |
| **Telnix 现状** | TimelineView.vue 只是 HTTP 瀑布流 |
| **价值评分** | ⭐⭐⭐ |
| **价值分析** | 调试 TCP 拥塞、丢包、乱序、慢启动等底层问题 |
| **实现难度** | 高 |
| **实现思路** | 从 raw_capture 抓 TCP 字节流并解析 seq/ack，先做基础版（仅 SYN/SYN-ACK/FIN/RST 时序） |

### 🥉 第三梯队（高价值但难落地，长期规划）

#### 3.9 TLS 无证书明文解密（eBPF hook，ecapture 模式）

| 维度 | 内容 |
|---|---|
| **竞品参考** | ecapture（15.4k Star）、kyanos、kubeshark |
| **Telnix 现状** | ssl_bump.py 必须 CA 证书 MITM |
| **价值评分** | ⭐⭐⭐⭐（价值高但...） |
| **实现难度** | **极高** |
| **不推荐理由** | eBPF 仅 Linux 内核 6.10+，Windows 完全不支持，macOS 无 eBPF 驱动。Telnix 定位 Windows 优先，跨平台实现几乎不可能。**建议放弃，保持 MITM 模式** |

#### 3.10 Lua Dissector 自定义协议

| 维度 | 内容 |
|---|---|
| **竞品参考** | Wireshark、Nmap NSE |
| **Telnix 现状** | 协议解析器全部硬编码 |
| **价值评分** | ⭐⭐⭐ |
| **实现难度** | **极高** |
| **不推荐理由** | 需要嵌入 Lua 解释器或写 DSL。优先做 **pcap 导出**（借力 Wireshark 数千个 Dissector）比自己做 Dissector 更聪明 |

---

### ❌ 明确不推荐做的特性

以下特性在竞品中存在但**不适合 Telnix 定位**：

| 特性 | 参考竞品 | 不推荐理由 |
|---|---|---|
| 200 Gbps / 60 Gbps 全保真捕获 | Corvil / Viavi | 硬件方案，Telnix 是应用层调试工具 |
| 5 PB 存储 / S3 云归档 | Viavi / Riverbed | Telnix 是单机调试工具，目标用户不是运营商 |
| 金融合规审计（MiFID II/CAT） | Corvil | 行业过窄 |
| BGP 路由监控 / Internet Stack Map | Catchpoint / ThousandEyes | 需要全球探测网络，非 Telnix 定位 |
| 网络取证 + MITRE ATT&CK 映射 | ManageEngine NFA | 过度工程化，Telnix 用户是开发者不是 SOC |

---

## 四、推荐落地顺序

### 近期可马上做的（纯前端 + 简单 SQL，1-3 天）

| 顺序 | 特性 | 工作量 | 产出 |
|:---:|---|:---:|---|
| 1 | **流量热力图** | 1 天 | AnalyzeView 增加 2D 矩阵视图 |
| 2 | **时间滑块回放** | 1-2 天 | 基于 sessions 表 + 时间轴组件 |
| 3 | **ASN 识别** | 0.5 天 | 下载 GeoLite2-ASN.mmdb，ip_region.py 并行查询 |

### 中期（需后端动一点，3-7 天）

| 顺序 | 特性 | 工作量 | 产出 |
|:---:|---|:---:|---|
| 4 | **pcap 导出** | 3-5 天 | 打通 Wireshark 生态，价值最大 |
| 5 | **触发式捕获 + DSL** | 3-5 天 | 复用 flowfilter DSL，新增 UI |
| 6 | **响应时间组成图** | 3-7 天 | server.py 埋 timing 点，前端渲染 |

### 长期

| 顺序 | 特性 | 工作量 |
|:---:|---|:---:|
| 7 | 网络拓扑图 | 5-10 天 |
| 8 | TCP 时序图 | 10-20 天（底层重） |

---

## 附录：三份报告原始数据来源

- 报告 1：商业抓包软件功能全景调研报告（17 款，2026 年 7-8 月资料）
- 报告 2：GitHub 开源抓包/网络代理工具全景调研（12 款）
- 报告 3：GitHub 抓包软件全景调研报告（27 款，Star 总量 194K+）

原始 HTML 文件位于：`D:\Desktop\telnix v2\Telnix-trae-agent-DnHAtu\ck\`
