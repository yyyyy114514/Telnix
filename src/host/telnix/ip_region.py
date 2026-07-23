"""IP 属地查询模块（基于 ip2region 离线库）。

数据文件：data/geoip/ip2region_v4.xdb（约 11MB，从 ip2region 项目下载）
查询格式：国家|省|市|ISP|国家代码（如 "中国|广东省|深圳市|电信|CN"）

本模块封装：
- 全局 searcher 单例（线程安全，全内存查询，微秒级）
- 格式化输出（中国去掉"中国"前缀，海外带国家代码）
- 内网/保留 IP 识别

性能：全内存查询（load_content_from_file 一次性加载到内存），
单次查询 ~200μs，可承受高并发抓包场景。
"""
import os
import threading
from typing import Optional

from .config import get_data_dir


_lock = threading.Lock()
_searcher = None  # 全局 searcher 单例
_load_failed = False  # 加载失败标记（避免每次都重试）


def _get_searcher():
    """懒加载全局 searcher。失败返回 None。"""
    global _searcher, _load_failed
    if _searcher is not None:
        return _searcher
    if _load_failed:
        return None
    with _lock:
        if _searcher is not None:
            return _searcher
        if _load_failed:
            return None
        try:
            import ip2region.searcher as xdb
            import ip2region.util as util
            db_path = os.path.join(get_data_dir(), "geoip", "ip2region_v4.xdb")
            if not os.path.exists(db_path):
                _load_failed = True
                return None
            c_buffer = util.load_content_from_file(db_path)
            _searcher = xdb.new_with_buffer(util.IPv4, c_buffer)
            return _searcher
        except Exception:  # noqa: BLE001
            _load_failed = True
            return None


def _is_private_ip(ip: str) -> bool:
    """判断是否内网/保留 IP。"""
    if not ip:
        return True
    try:
        parts = ip.split(".")
        if len(parts) != 4:
            return True
        a, b = int(parts[0]), int(parts[1])
        # 10.0.0.0/8
        if a == 10:
            return True
        # 172.16.0.0/12
        if a == 172 and 16 <= b <= 31:
            return True
        # 192.168.0.0/16
        if a == 192 and b == 168:
            return True
        # 127.0.0.0/8 (loopback)
        if a == 127:
            return True
        # 169.254.0.0/16 (link-local)
        if a == 169 and b == 254:
            return True
        # 0.0.0.0
        if a == 0:
            return True
        return False
    except (ValueError, IndexError):
        return True


def _format_region(raw: str) -> str:
    """把 ip2region 原始输出格式化为展示用属地字符串。

    输入格式：国家|省|市|ISP|国家代码
    输出：
    - 内网/保留：'内网'
    - 中国：'省份简称+城市简称'（如 '广东深圳'、'浙江徐州'；直辖市只显示一次如 '上海'）
    - 海外：'国家代码'（如 'US'、'JP'）
    - 未知：''
    """
    if not raw:
        return ""
    parts = raw.split("|")
    # 长度不足 5 说明数据异常
    if len(parts) < 5:
        return raw
    country, province, city, _isp, code = parts[0], parts[1], parts[2], parts[3], parts[4]

    # 内网/保留
    if country in ("Reserved", "0") or code == "0":
        return "内网"

    # 中国：省份简称+城市简称
    if country == "中国" or code == "CN":
        prov = province if province and province != "0" else ""
        ct = city if city and city != "0" else ""
        # 直辖市：省和市相同（如 上海市==上海市），只显示一次并去掉"市"后缀
        if prov and ct and prov == ct:
            return prov[:-1] if prov.endswith("市") else prov
        # 省份简称：去掉 省/壮族自治区/回族自治区/维吾尔自治区/自治区 后缀
        prov_short = prov
        for suffix in ("壮族自治区", "回族自治区", "维吾尔自治区", "自治区", "省"):
            if prov_short.endswith(suffix):
                prov_short = prov_short[:-len(suffix)]
                break
        # 城市简称：去掉 市 后缀
        ct_short = ct[:-1] if ct.endswith("市") else ct
        if prov_short and ct_short:
            return f"{prov_short}{ct_short}"
        if prov_short:
            return prov_short
        if ct_short:
            return ct_short
        return "中国"

    # 海外：只显示国家代码
    if code and code != "0":
        return code
    return country


def lookup(ip: str) -> str:
    """查询 IP 属地，返回格式化后的字符串。

    返回值示例：
    - '广东深圳'
    - '上海'（直辖市不重复）
    - 'US'
    - '内网'
    - ''（查询失败或数据文件缺失）
    """
    if not ip:
        return ""
    # 内网 IP 直接返回"内网"，不查库
    if _is_private_ip(ip):
        return "内网"
    s = _get_searcher()
    if s is None:
        return ""
    try:
        raw = s.search(ip)
        return _format_region(raw or "")
    except Exception:  # noqa: BLE001
        return ""


def lookup_region(ip: str) -> tuple[str, str]:
    """查询 IP 属地，返回 (格式化属地, 原始完整字符串)。

    原始完整字符串用于详情页展示（含 ISP 等完整信息）。
    """
    if not ip:
        return "", ""
    if _is_private_ip(ip):
        return "内网", "内网IP"
    s = _get_searcher()
    if s is None:
        return "", ""
    try:
        raw = s.search(ip) or ""
        return _format_region(raw), raw
    except Exception:  # noqa: BLE001
        return "", ""


def is_available() -> bool:
    """检查 IP 属地查询是否可用（数据文件已加载）。"""
    return _get_searcher() is not None
