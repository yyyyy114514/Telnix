# Telnix 功能正确性审计报告 (Batch 1)

> 范围：src/host/telnix/ 后端代理/协议/DB；忽略 i18n。代码中带 Fxx 注释的修复项不重复报告。

## 汇总表

| 编号 | 严重 | 文件:行 | 类别 | 结论 |
|------|------|---------|------|------|
| B1 | P2 | decode.py:532 | 协议/边界 | 可疑 |
| B2 | P2 | websocket_relay.py:360-364 | 竞态/协议 | 确凿(部分) |
| B3 | P1 | server.py:2988-2999 | 协议处理 | 确凿 |
| B4 | P2 | server.py:473-488 | 协议边界 | 可疑 |
| B5 | P2 | server.py:2706-2723 | 资源泄露 | 可疑 |
| B6 | P3 | replay.py:80-83 | 逻辑 | 可疑 |
| B7 | P3 | h2_forward.py:341-346 | 并发竞态 | 可疑 |
| B8 | P2 | server.py:2094-2099 | 逻辑一致性 | 可疑 |

## 详情

### B1 [P2 可疑] decode.py:532 decode_flow 字段取用
`val = flow.get(field) or ""`；当 field 默认 raw_data 且 HTTP flow 的 raw_data 为 None 时得 ""，返回 protocol:"empty"。无崩溃，但用户想看明文 body 却显示空，体验误判。建议区分字段不存在与字段为空。

### B2 [P2 确凿] websocket_relay.py:360-364 双向关闭竞态
`_direction` 的 finally 中 `stop_flag.set()` + `dst.shutdown(SHUT_WR)`。c2s 读到 close 帧后 break→finally 会 set stop_flag，导致 s2c 方向立即退出(`while not stop_flag.is_set()`)，但 server→client 可能还有已到达未转发帧被丢弃。应仅两方向都结束后统一关闭。

### B3 [P1 确凿] server.py:2988-2999 brotli 解压失败 Content-Encoding 未剥离
当 br 编码且 brotli 库缺失/解压失败时，`_decompress_body` 直接 return body, headers，**未移除 Content-Encoding: br 头**。下游把仍带 Content-Encoding:br 的原始(未解压)字节发回客户端 → 浏览器无法解码 → 白屏/响应解析失败。
修复：失败分支也 `headers.remove("Content-Encoding")`，或彻底从 accept-encoding 剥离 br。

### B4 [P2 可疑] server.py:473-488 read_chunked 截断处理
`read_exactly(size)` 在连接提前关闭时返回不足 size 字节(不抛异常)，随后 `read_line()` 消费尾部 CRLF 会读到下一 chunk size 行首部字节，导致 size 解析错乱、body 错位。`read_line` 在 EOF 返回剩余缓冲不报错。建议读到 EOF 且未达 chunk size 时直接结束。

### B5 [P2 可疑] server.py:2706-2723 重试失败分支端口注销
h2 回退/重试路径复用连接失败清理已覆盖；但 `_forward` 多分支 close 散落，建议统一 try/finally 保证端口注销(基本完善)。无确凿泄漏。

### B6 [P3 可疑] replay.py:80-83 SSRF 校验逻辑
`_resolve_safe_target` 对 getaddrinfo 返回每个候选 IP 逐一检查，存在即拒绝；但返回 `infos[0][4][0]`(第一个)，而第一个可能恰是公网通过校验，却未再确认它就是通过校验的那个。建议对选中的 resolved_ip 单独再校验一次。

### B7 [P3 可疑] h2_forward.py:341-346 超时后清理与 reader 竞态
request() 超时调用 `_cleanup_stream` pop+reset_stream，reader 线程在锁外仍持有 stream 对象引用并向其写入(对象存活安全)，但建议 `_lock` 内完成 pop+reset 原子性。

### B8 [P2 可疑] server.py:2094-2099 响应阶段规则重匹配副作用
响应阶段 `_match_auto_reply` 重匹配仅处理 modify_response/script，请求已发出则请求体/头无法再改。与请求阶段 break 一致，但配置 status_filter 可能误以为请求也被改。建议文档明确 status_filter 仅作用于响应阶段。

## 未发现确凿问题的模块
db.py(SSE缓存/批量写入/删除一致性)、transparent_proxy.py(端口集合 stop clear)、breakpoint.py、async_proxy.py、h2_forward.py 池清理、raw_capture.py(WinDivert handle stop) 资源释放与并发保护基本完善。
