# 手机抓包教程（安卓）

Telnix 通过 HTTP 代理 + SSL Bump 抓取安卓 App 流量，原理与 Charles 相同。
不依赖 VPNService，需要手机配 WiFi 代理指向电脑。

> 适用：HTTP/HTTPS 抓包。TCP/UDP/SSH 等非代理协议不在此方案覆盖范围。

---

## 准备工作

1. 电脑和手机连同一 WiFi
2. 电脑已启动 Telnix，且 HTTPS 根证书已安装（设置页「HTTPS 证书」段显示已安装）
3. 手机和电脑能互相 ping 通（部分公共 WiFi 隔离客户端，需用热点）

## 步骤 1：开启局域网监听

设置页 →「手机抓包（安卓）」段 → 开启「允许局域网设备连接」开关。

开关会写 `proxy_listen_host=0.0.0.0`，需要**重启后端**生效：
- 侧边栏底部点「重启服务」，或
- CLI：`python -m Telnix.cli system restart`

> 默认 127.0.0.1 只允许本机连，开启后手机才能连上代理。

## 步骤 2：放行防火墙端口

Windows 防火墙默认会拦外部连接。以管理员身份执行：

```powershell
python -m Telnix.cli system firewall-allow
```

会添加两条入站规则：
- `Telnix-Proxy-8888`（代理端口 8888）
- `Telnix-API-18901`（API 端口 18901，证书下载用）

查询状态：`python -m Telnix.cli system firewall-status`

## 步骤 3：手机配 WiFi 代理

1. 手机连同一 WiFi
2. 长按已连接的网络 → 修改网络 → 高级选项 → 代理 → 手动
3. 主机：电脑 IP（在设置页「手机抓包向导」对话框里显示，如 `192.168.1.100`）
4. 端口：`8888`
5. 保存

验证：手机浏览器访问 `http://example.com`，Telnix 抓包页能看到流量，说明代理通了。

## 步骤 4：下载并安装根证书

设置页 →「手机抓包（安卓）」→ 点「手机抓包向导」按钮 → 弹窗里有二维码和 URL。

两种方式下载证书到手机：
- **扫码**：手机浏览器扫码，直接下载 `Telnix_root.pem`
- **手动访问**：手机浏览器打开 `http://电脑IP:18901/api/cert/root.pem`

安装证书（不同品牌路径略有差异）：
- 小米/Redmi：设置 → 密码与安全 → 系统安全 → 加密与凭据 → 安装证书 → CA 证书
- 华为/荣耀：设置 → 安全 → 更多安全设置 → 加密与凭据 → 从存储设备安装
- OPPO/vivo：设置 → 其他设置 → 设备与隐私 → 加密与凭据 → 从 SD 卡安装
- 原生 Android：设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书

## 步骤 5：开始抓包

1. Telnix 抓包页点「开始」
2. 手机操作目标 App
3. 抓包页可看到 HTTPS 解密后的明文请求/响应

---

## 安卓 7+ 系统证书问题（重要）

Android 7.0（API 24）开始，App 默认只信任系统证书，不再信任用户安装的证书。
上面步骤 4 装的证书属于「用户证书」，对大多数 App 来说 HTTPS 抓包会失败（TLS 握手错误）。

三种解决方案：

### 方案 A：root 后导入系统证书（推荐，一劳永逸）

需要手机已 root：

```bash
# 1. 把证书转为系统证书格式（文件名是证书 hash）
openssl x509 -inform PEM -subject_hash_old -in Telnix_root.pem | head -1
# 假设输出 c8750f0d
cp Telnix_root.pem c8750f0d.0

# 2. adb push 到临时目录
adb push c8750f0d.0 /sdcard/

# 3. 进入 shell 挂载 system 可写并移动
adb shell
su
mount -o remount,rw /system
cp /sdcard/c8750f0d.0 /system/etc/security/cacerts/
chmod 644 /system/etc/security/cacerts/c8750f0d.0
mount -o remount,ro /system
reboot
```

### 方案 B：Magisk 模块（root 但不想改 system）

搜「Magisk Move User Certificates」模块，安装后重启，用户证书自动转系统证书。

### 方案 C：改 App 的 networkSecurityConfig（无需 root，需重打包）

适合目标 App 是自己的或可重打包的情况。在 `res/xml/network_security_config.xml` 加：

```xml
<network-security-config>
    <base-config>
        <trust-anchors>
            <certificates src="system"/>
            <certificates src="user"/>
        </trust-anchors>
    </base-config>
</network-security-config>
```

`AndroidManifest.xml` 的 `<application>` 标签加 `android:networkSecurityConfig="@xml/network_security_config"`。
然后重新打包签名。

---

## 常见问题

### 手机连不上代理

1. 确认电脑 `proxy_listen_host=0.0.0.0` 且已重启后端
2. 确认防火墙已放行 8888（`firewall-status` 检查）
3. 电脑和手机互相 ping 测试（公共 WiFi 可能隔离客户端，用手机热点）
4. 关闭电脑上的其他安全软件（360、火绒等可能拦入站连接）

### 代理通了但 HTTPS 抓不到

1. 确认根证书已下载并安装
2. 确认 Telnix 设置页显示「根证书已安装」
3. 安卓 7+ 大概率是系统证书问题，参考上面的方案 A/B/C

### App 报「网络连接失败」但 HTTP 能抓

App 可能开启了证书 pinning（如银行、支付宝、微信）。这种 App 拒绝任何中间人证书，Telnix 无法解密。
只能：
- 抓 HTTP 明文部分（如果有）
- 用 Frida 等工具绕过 pinning（超出本教程范围）

### 抓到自己电脑的包

把 Telnix 自身的进程加入忽略列表，避免循环抓包：
- 设置页 →「忽略规则」→ 添加进程名 `python.exe`、`Trae Solo CN.exe` 等

---

## API 参考

| 接口 | 用途 |
|---|---|
| `GET /api/mobile/setup` | 获取本机 IP、代理端口、证书下载 URL |
| `GET /api/cert/root.pem` | 下载根证书（手机浏览器直接访问） |
| `PUT /api/settings` | 更新 `proxy_listen_host` 设置 |
| CLI `system firewall-allow` | 防火墙放行 8888/18901 端口 |
| CLI `system firewall-status` | 查询防火墙规则状态 |
