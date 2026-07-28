"""transparent_proxy._recalc_checksums 回归测试。

复现用户报告的 bug：
> Win透明代理出现校验和重算失败
> packet.recalc_checksums / pydivert.Helper.calc_checksums / divert.recalc_checksums 均失败

根因：pydivert 3.x 把方法名从 recalc_checksums() 改为 recalculate_checksums()，
旧代码三个 fallback 都用了不存在的 API 名，导致 _recalc_checksums 静默落到 warning 分支。

红绿测试设计：
- 构造一个 mock TCP/IPv4 包，模拟 transparent_proxy._loop 中的改写流程
  （改 dst_addr + dst_port，破坏原校验和）
- 调用 TransparentProxy._recalc_checksums(packet)
- 断言 packet.is_checksum_valid == True

修复前（旧 API 名）：recalc 失败 → 校验和未更新 → is_checksum_valid = False → 测试红
修复后（新 API 名）：recalc 成功 → 校验和更新 → is_checksum_valid = True → 测试绿

无需启动 WinDivert 驱动（不开 start()），仅测 _recalc_checksums 这个纯方法。
"""
from __future__ import annotations

import sys
import os

# 加载项目模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "host"))

import traceback


def build_synthetic_tcp_ipv4_packet() -> bytes:
    """构造一个最小的 IPv4+TCP SYN 包（模拟 WinDivert recv 到的出站包）。

    结构：
    - IPv4 头 20 字节（src=192.168.1.100, dst=93.184.216.34=example.com）
    - TCP 头 20 字节（src_port=54321, dst_port=80, SYN）
    - 无 payload

    校验和字段先填 0，让后续 recalculate_checksums 计算。
    """
    raw = bytearray(40)
    # IPv4 header
    raw[0] = 0x45  # version=4, IHL=5
    raw[1] = 0x00  # DSCP/ECN
    raw[2:4] = (40).to_bytes(2, "big")  # total length
    raw[4:6] = (0x1234).to_bytes(2, "big")  # id
    raw[6:8] = (0x4000).to_bytes(2, "big")  # flags=DF, frag off=0
    raw[8] = 64  # TTL
    raw[9] = 6  # protocol = TCP
    raw[10:12] = b"\x00\x00"  # IP checksum (placeholder)
    raw[12:16] = bytes([192, 168, 1, 100])  # src
    raw[16:20] = bytes([93, 184, 216, 34])  # dst
    # TCP header
    raw[20:22] = (54321).to_bytes(2, "big")  # src port
    raw[22:24] = (80).to_bytes(2, "big")  # dst port
    raw[24:28] = (1).to_bytes(4, "big")  # seq
    raw[28:32] = (0).to_bytes(4, "big")  # ack
    raw[32] = 0x50  # data offset = 5
    raw[33] = 0x02  # SYN
    raw[34:36] = (65535).to_bytes(2, "big")  # window
    raw[36:38] = b"\x00\x00"  # TCP checksum (placeholder)
    raw[38:40] = (0).to_bytes(2, "big")  # urg ptr
    return bytes(raw)


def run_recalc_test() -> int:
    """主测试入口：返回 0=pass, 1=fail。"""
    try:
        import pydivert
        from pydivert.packet import Packet
        # 不导入整个 telnix 包（避免触发 __init__ 副作用），直接导入目标模块
        from telnix.proxy.transparent_proxy import TransparentProxy
    except ImportError as e:
        print(f"× 依赖缺失，无法运行测试: {e}")
        print("  请确认在项目根目录运行：python scripts/test_transparent_proxy_checksum.py")
        return 1

    pydivert_version = getattr(pydivert, "__version__", "?")
    print(f"pydivert version: {pydivert_version}")

    # ---- 准备：构造一个"已修改但未 recalc"的包，模拟 _loop 中的改写 ----
    raw = bytearray(build_synthetic_tcp_ipv4_packet())
    packet = Packet(raw)
    packet.ipv4.packet_len = 40
    # 先 recalc 一次，得到正确的初始校验和
    packet.recalculate_checksums()
    assert packet.is_checksum_valid, "前置：初始包校验和应有效"
    print("✓ 前置：初始包校验和有效")

    # 模拟 _loop 中改写 dst_addr + dst_port（破坏校验和）
    packet.ipv4.dst_addr = "127.0.0.1"
    packet.tcp.dst_port = 8888
    print(f"  改写后：dst_addr={packet.ipv4.dst_addr}, dst_port={packet.tcp.dst_port}")
    print(f"  改写后 is_checksum_valid = {packet.is_checksum_valid}（应为 False，因校验和未更新）")

    # ---- 测试：调用 TransparentProxy._recalc_checksums ----
    # 不 start()，只构造实例（不打开 WinDivert，无需管理员）
    tp = TransparentProxy(local_port=8888)
    tp._local_host = "127.0.0.1"  # 避免依赖 _detect_local_ip

    # 捕获日志（_recalc_checksums 失败时会写 warning）
    captured_warnings: list[str] = []

    class _CapturingLogger:
        """最小 logger 替身，捕获 warning 调用，避免污染真实日志。"""
        def warning(self, *args):
            captured_warnings.append(" ".join(str(a) for a in args))
        def info(self, *args):
            pass
        def error(self, *args):
            pass

    # 用 monkey-patch 替换 logger
    import telnix.proxy.transparent_proxy as tp_mod
    original_logger = tp_mod.logger
    tp_mod.logger = _CapturingLogger()
    try:
        tp._recalc_checksums(packet)
    finally:
        tp_mod.logger = original_logger

    if captured_warnings:
        print(f"\n× _recalc_checksums 写出 warning 日志：")
        for msg in captured_warnings:
            print(f"    {msg}")

    # ---- 断言：校验和应已重算，is_checksum_valid 应为 True ----
    valid = packet.is_checksum_valid
    print(f"\n[断言] 调用 _recalc_checksums 后 is_checksum_valid = {valid}")

    if not valid:
        print("\n× 测试红：_recalc_checksums 未真正重算校验和")
        print("  原因：pydivert 3.x 没有 recalc_checksums 方法")
        print("  修复：改用 packet.recalculate_checksums()")
        return 1

    if captured_warnings:
        print("\n× 测试红：校验和虽有效，但 _recalc_checksums 仍写出 warning（说明走了 fallback 链）")
        return 1

    print("\n✓ 测试绿：_recalc_checksums 成功重算校验和，无 warning")
    return 0


def run_send_auto_recalc_test() -> int:
    """额外验证：即使不调用 _recalc_checksums，WinDivert.send() 默认也会自动重算。

    这个测试只是文档化 pydivert 3.x 的行为，不依赖驱动（无法真正 send）。
    我们只验证 send() 方法的签名默认值是 recalculate_checksum=True。
    """
    try:
        import inspect
        import pydivert
        sig = inspect.signature(pydivert.WinDivert.send)
        param = sig.parameters.get("recalculate_checksum")
        if param is None:
            print("× WinDivert.send 没有 recalculate_checksum 参数（pydivert 版本不兼容）")
            return 1
        if param.default is not True:
            print(f"× WinDivert.send 的 recalculate_checksum 默认值不是 True: {param.default}")
            return 1
        print(f"✓ WinDivert.send(recalculate_checksum={param.default}) 默认自动重算")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"× 验证 send 签名失败: {e}")
        return 1


def main() -> int:
    print("=" * 70)
    print("transparent_proxy._recalc_checksums 回归测试")
    print("=" * 70)
    rc1 = run_recalc_test()
    print()
    print("=" * 70)
    print("额外验证：WinDivert.send() 是否默认自动重算")
    print("=" * 70)
    rc2 = run_send_auto_recalc_test()
    print()
    if rc1 != 0 or rc2 != 0:
        print("=" * 70)
        print("× 存在失败项")
        print("=" * 70)
        return 1
    print("=" * 70)
    print("✓ 全部通过")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
