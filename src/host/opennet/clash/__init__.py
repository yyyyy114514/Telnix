"""Clash/Mihomo 集成模块。

外接模式：OpenNet 不启动 Mihomo 进程，只通过 external-controller API 连接用户已运行的 Clash/Mihomo。
OpenNet 代理把流量转发到 Mihomo 的 mixed-port，实现抓包 + 节点代理。
"""
