"""复现 transparent_proxy 校验和重算失败的根因。

根因假设：pydivert 3.x 把方法从 recalc_checksums() 重命名为 recalculate_checksums()，
旧代码里所有 fallback 都用了不存在的 API 名，导致最终落到 warning 分支。
本脚本在纯用户态构造包验证假设，无需启动 WinDivert 驱动。
"""
from __future__ import annotations

import sys
import traceback


def build_mock_tcp_ipv4_packet() -> bytes:
    """构造一个最小的 IPv4+TCP 包（无 payload），用于校验和重算测试。

    结构参考 pydivert/tests/test_checksum_verification.py 的 mock。
    """
    raw = bytearray(40)  # 20 字节 IPv4 + 20 字节 TCP，无 payload
    raw[0] = 0x45  # version=4, IHL=5 (20 字节)
    raw[1] = 0x00  # DSCP/ECN
    raw[2:4] = (40).to_bytes(2, "big")  # total length
    raw[4:6] = (0x1234).to_bytes(2, "big")  # id
    raw[6:8] = (0).to_bytes(2, "big")  # flags+frag
    raw[8] = 64  # TTL
    raw[9] = 6  # protocol = TCP
    raw[10:12] = b"\x00\x00"  # IP checksum（先 0，recalc 时填）
    raw[12:16] = bytes([192, 168, 1, 100])  # src
    raw[16:20] = bytes([93, 184, 216, 34])  # dst (example.com)
    # TCP header
    raw[20:22] = (54321).to_bytes(2, "big")  # src port
    raw[22:24] = (80).to_bytes(2, "big")  # dst port
    raw[24:28] = (1).to_bytes(4, "big")  # seq
    raw[28:32] = (0).to_bytes(4, "big")  # ack
    raw[32] = 0x50  # data offset = 5 (20 字节)
    raw[33] = 0x02  # SYN
    raw[34:36] = (65535).to_bytes(2, "big")  # window
    raw[36:38] = b"\x00\x00"  # TCP checksum（先 0）
    raw[38:40] = (0).to_bytes(2, "big")  # urg ptr
    return bytes(raw)


def main() -> int:
    from pydivert.packet import Packet

    raw = bytearray(build_mock_tcp_ipv4_packet())
    pkt = Packet(raw)
    pkt.ipv4.packet_len = 40

    # 1. 旧 API 名（当前 transparent_proxy.py 用的）
    print("[1] 旧 API: packet.recalc_checksums()")
    try:
        pkt.recalc_checksums()  # type: ignore[attr-defined]
        print("    结果：成功（与预期不符）")
    except AttributeError as e:
        print(f"    AttributeError: {e}")
        print("    → 确认根因：pydivert 3.x 没有 recalc_checksums 方法")
    except Exception as e:  # noqa: BLE001
        print(f"    其他异常: {type(e).__name__}: {e}")

    # 2. fallback 1: pydivert.WinDivertHelper / pydivert.Helper
    print("\n[2] fallback 1: pydivert.WinDivertHelper / Helper")
    import pydivert
    helper = getattr(pydivert, "WinDivertHelper", None) or getattr(pydivert, "Helper", None)
    print(f"    pydivert.WinDivertHelper / Helper = {helper}")
    print(f"    pydivert 顶层 attrs: {[x for x in dir(pydivert) if 'elper' in x or 'Calc' in x]}")

    # 3. fallback 2: divert.recalc_checksums
    print("\n[3] fallback 2: divert.recalc_checksums(packet)")
    # 模拟 self._divert，pydivert.WinDivert 实例
    try:
        with pydivert.WinDivert("false") as w:
            try:
                w.recalc_checksums(pkt)  # type: ignore[attr-defined]
                print("    结果：成功（与预期不符）")
            except AttributeError as e:
                print(f"    AttributeError: {e}")
                print("    → 确认：WinDivert 实例也没有 recalc_checksums 方法")
            # 列出 WinDivert 实例方法
            print(f"    WinDivert 实例方法: {[x for x in dir(w) if not x.startswith('_') and 'checksum' in x.lower() or 'calc' in x.lower()]}")
    except Exception as e:  # noqa: BLE001
        print(f"    打开 WinDivert 失败（可接受，仅查方法存在性）: {type(e).__name__}: {e}")
        # 用类直接查
        methods = [x for x in dir(pydivert.WinDivert) if not x.startswith('_')]
        print(f"    WinDivert 类方法/属性: {methods}")

    # 4. 正确 API: packet.recalculate_checksums()
    print("\n[4] 正确 API: packet.recalculate_checksums()")
    # 故意破坏 IP/TCP checksum，模拟修改后的状态
    pkt.ipv4.dst_addr = "1.2.3.4"
    pkt.tcp.dst_port = 443
    pkt.ipv4.cksum = 0
    pkt.tcp.cksum = 0
    try:
        n = pkt.recalculate_checksums()
        print(f"    结果：成功，重算了 {n} 个校验和")
    except Exception as e:  # noqa: BLE001
        print(f"    异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    # 5. 验证 is_checksum_valid
    valid = pkt.is_checksum_valid
    print(f"\n[5] packet.is_checksum_valid = {valid}")
    if not valid:
        print("    ✗ 校验和仍然无效")
        return 1

    # 6. 再次修改 + recalc，验证幂等
    pkt.ipv4.ttl = 32
    pkt.recalculate_checksums()
    valid2 = pkt.is_checksum_valid
    print(f"\n[6] 修改 ttl=32 后再 recalc，is_checksum_valid = {valid2}")
    if not valid2:
        return 1

    print("\n✓ 根因确认：旧代码用的 API 名全部错误，正确名是 packet.recalculate_checksums()")
    print("  另外 send() 默认 recalculate_checksum=True，会自动重算，无需手动调用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
