"""IP geolocation lookup module (based on the ip2region offline database).

Data file: data/geoip/ip2region_v4.xdb (about 11MB, downloaded from the ip2region project)
Query format: Country|Province|City|ISP|CountryCode (e.g. "中国|广东省|深圳市|电信|CN")

This module encapsulates:
- A global searcher singleton (thread-safe, in-memory query, microsecond-level)
- Formatted output (strips the "中国" prefix for Chinese IPs, includes country code for overseas IPs)
- Private/reserved IP identification

Performance: in-memory query (load_content_from_file loads everything into memory at once),
~200μs per query, can sustain high-concurrency capture scenarios.
"""
import os
import threading
from typing import Optional

from .config import get_data_dir


_lock = threading.Lock()
_searcher = None  # 全局 searcher 单例
_load_failed = False  # 加载失败标记（避免每次都重试）

# ASN 查询：maxminddb + GeoLite2-ASN.mmdb
_asn_lock = threading.Lock()
_asn_reader = None
_asn_load_failed = False


def _get_searcher():
    """Lazily initialize the global searcher. Returns None on failure."""
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
        except Exception as e:  # noqa: BLE001
            _load_failed = True
            # 静默吞异常会导致属地永久空白且无日志，改为打印告警便于排查
            try:
                from . import logger
                logger.warning("ip_region", f"ip2region failed to load: {e}", "")
            except Exception:  # noqa: BLE001
                print(f"[Telnix] ip2region failed to load: {e}", flush=True)
            return None


def _get_asn_reader():
    """Lazily initialize the global ASN reader. Returns None on failure.

    Uses maxminddb library + GeoLite2-ASN.mmdb data file.
    Data file path: data/geoip/GeoLite2-ASN.mmdb
    Gracefully degrades if library or file missing.
    """
    global _asn_reader, _asn_load_failed
    if _asn_reader is not None:
        return _asn_reader
    if _asn_load_failed:
        return None
    with _asn_lock:
        if _asn_reader is not None:
            return _asn_reader
        if _asn_load_failed:
            return None
        try:
            import maxminddb
            db_path = os.path.join(get_data_dir(), "geoip", "GeoLite2-ASN.mmdb")
            if not os.path.exists(db_path):
                _asn_load_failed = True
                return None
            _asn_reader = maxminddb.open_database(db_path)
            return _asn_reader
        except Exception as e:  # noqa: BLE001
            _asn_load_failed = True
            try:
                from . import logger
                logger.warning("ip_region", f"GeoLite2-ASN failed to load: {e}", "")
            except Exception:  # noqa: BLE001
                print(f"[Telnix] GeoLite2-ASN failed to load: {e}", flush=True)
            return None


def _is_private_ip(ip: str) -> bool:
    """Check whether the IP is a private/reserved IP."""
    if not ip:
        return True
    # 兼容 IPv4 映射的 IPv6 地址（::ffff:x.x.x.x），提取其中的 IPv4 部分
    # Windows getaddrinfo 在双栈环境可能返回这种格式，原逻辑会因 split(".") 段数≠4
    # 而误判为内网，导致属地始终为空
    if ip.startswith("::ffff:"):
        ip = ip[7:]
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


def _normalize_ip(ip: str) -> str:
    """Normalize an IP address to IPv4 format. Returns empty string for pure IPv6."""
    if not ip:
        return ""
    if ip.startswith("::ffff:"):
        return ip[7:]
    # 纯 IPv6 地址（非 IPv4 映射），ip2region_v4 不支持，返回空
    if ":" in ip:
        return ""
    return ip


def _format_region(raw: str) -> str:
    """Format the raw ip2region output into a display-friendly region string.

    Input format: Country|Province|City|ISP|CountryCode
    Output:
    - Private/reserved: '内网'
    - China: 'Province short + City short' (e.g. '广东深圳', '浙江徐州'; municipalities shown once, e.g. '上海')
    - Overseas: 'CountryCode' (e.g. 'US', 'JP')
    - Unknown: ''
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
        # 省份简称：去掉 省/壮族自治区/回族自治区/维吾尔自治区/自治区/特别行政区 后缀
        prov_short = prov
        for suffix in ("壮族自治区", "回族自治区", "维吾尔自治区", "自治区", "特别行政区", "省"):
            if prov_short.endswith(suffix):
                prov_short = prov_short[:-len(suffix)]
                break
        # 直辖市/特别行政区：province 也可能带"市"后缀（如 北京市），剥掉
        if prov_short.endswith("市"):
            prov_short = prov_short[:-1]
        # 城市简称：去掉 市 后缀
        ct_short = ct[:-1] if ct.endswith("市") else ct
        # 直辖市/特别行政区：省简称 == 市简称时只显示一次（北京/上海/天津/重庆/香港）
        # 兼容 ip2region 省市不一致的情况（如 "北京市|北京" 或 "北京|北京市"）
        if prov_short and ct_short and prov_short == ct_short:
            return prov_short
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
    """Look up the IP geolocation and return the formatted string.

    Return value examples:
    - '广东深圳'
    - '上海' (municipality, no duplication)
    - 'US'
    - '内网'
    - '' (query failed or data file missing)
    """
    if not ip:
        return ""
    # 内网 IP 直接返回"内网"，不查库
    if _is_private_ip(ip):
        return "内网"
    # 转换为纯 IPv4 格式（处理 ::ffff:x.x.x.x 映射地址）
    ip4 = _normalize_ip(ip)
    if not ip4:
        return ""
    s = _get_searcher()
    if s is None:
        # 添加日志帮助诊断数据库加载问题
        try:
            from . import logger
            db_path = os.path.join(get_data_dir(), "geoip", "ip2region_v4.xdb")
            if not os.path.exists(db_path):
                logger.warning("ip_region", "ip2region database file not found", db_path)
            else:
                logger.warning("ip_region", "ip2region searcher not available", "")
        except Exception:  # noqa: BLE001
            pass
        return ""
    try:
        raw = s.search(ip4)
        return _format_region(raw or "")
    except Exception as e:  # noqa: BLE001
        # 记录查询异常
        try:
            from . import logger
            logger.warning("ip_region", f"ip2region query failed: {e}", ip)
        except Exception:  # noqa: BLE001
            pass
        return ""


def lookup_region(ip: str) -> tuple[str, str]:
    """Look up the IP geolocation, returning (formatted region, raw full string).

    The raw full string is used for detail page display (includes complete info such as ISP).
    """
    if not ip:
        return "", ""
    if _is_private_ip(ip):
        return "内网", "内网IP"
    # 转换为纯 IPv4 格式（处理 ::ffff:x.x.x.x 映射地址）
    ip4 = _normalize_ip(ip)
    if not ip4:
        return "", ""
    s = _get_searcher()
    if s is None:
        return "", ""
    try:
        raw = s.search(ip4) or ""
        return _format_region(raw), raw
    except Exception:  # noqa: BLE001
        return "", ""


def lookup_asn(ip: str) -> tuple[int, str]:
    """Look up the ASN (Autonomous System Number) and organization name.

    Returns (asn_number, asn_org). asn_number=0 means unknown/failed.
    Requires maxminddb library + GeoLite2-ASN.mmdb data file.

    Return value examples:
    - (4134, 'China Telecom')
    - (13335, 'Cloudflare, Inc.')
    - (0, '')  # data file missing or query failed
    """
    if not ip:
        return 0, ""
    if _is_private_ip(ip):
        return 0, "内网"
    ip4 = _normalize_ip(ip)
    if not ip4:
        return 0, ""
    reader = _get_asn_reader()
    if reader is None:
        return 0, ""
    try:
        record = reader.get(ip4)
        if not record:
            return 0, ""
        asn_num = record.get("autonomous_system_number", 0) or 0
        asn_org = record.get("autonomous_system_organization", "") or ""
        return asn_num, asn_org
    except Exception as e:  # noqa: BLE001
        try:
            from . import logger
            logger.warning("ip_region", f"ASN query failed: {e}", ip)
        except Exception:  # noqa: BLE001
            pass
        return 0, ""


def lookup_with_asn(ip: str) -> str:
    """Look up IP geolocation and return region string with ASN suffix.

    Output format: '<region> [AS<number> <org_short>]' or '<region>' if ASN unavailable.
    Examples:
    - '广东深圳 [AS4134 China Telecom]'
    - 'US [AS13335 Cloudflare]'
    - '内网'
    - '' (query failed)
    """
    region = lookup(ip)
    asn_num, asn_org = lookup_asn(ip)
    if asn_num and asn_org:
        # 截断过长的 org 名（有些 ASN org 名超过 100 字符）
        org_short = asn_org[:40]
        return f"{region} [AS{asn_num} {org_short}]"
    if asn_num:
        return f"{region} [AS{asn_num}]"
    return region


def is_available() -> bool:
    """Check whether IP geolocation lookup is available (data file loaded)."""
    return _get_searcher() is not None
