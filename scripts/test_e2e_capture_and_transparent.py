"""E2E 测试：抓包 + 透明代理 + 系统代理 同时运行，验证不超时、无 checksum warning。

用户报告：「开启抓包就不行了，超时」。
怀疑路径：raw_capture.py 用 WinDivert SNIFF 模式抓包，transparent_proxy.py 用 WinDivert
NETWORK 层拦截改写。两者各自持有独立的 WinDivert 句柄，但 filter 互相影响 ——
raw_capture 的 filter 是 "tcp or udp"，会拦截 transparent_proxy 改写后的包，
可能导致死循环或包被错误丢弃。

测试步骤：
1. 启动 raw capture（/api/raw/start）
2. 启动 transparent proxy（/api/transparent-proxy/start）
3. 启动系统代理（/api/system/proxy/start 或类似）—— 用户说「开启抓包同时还会开系统代理」
4. 发 HTTP 80 请求（走透明代理重定向）
5. 发 HTTPS 443 请求（走透明代理 raw tunnel）
6. 检查后端日志无 checksum warning
7. 检查 watchdog 没有触发（系统代理未被自动关闭）
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

API = "http://127.0.0.1:18901"
LOG_FILE = r"c:\Users\Administrator\Downloads\Telnix-trae-agent-DnHAtu\telnix_elevated.log"
WATCHDOG_LOG = r"D:\Desktop\opennet\data\proxy_watchdog.log"


def api_get(path: str):
    import urllib.request as ur
    with ur.urlopen(f"{API}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def api_post(path: str, body: dict | None = None):
    import urllib.request as ur
    data = json.dumps(body or {}).encode("utf-8")
    req = ur.Request(f"{API}{path}", data=data, method="POST",
                     headers={"Content-Type": "application/json"})
    with ur.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def log_section(s: str):
    print(f"\n{'=' * 70}\n{s}\n{'=' * 70}")


def tail_log_lines(n: int = 20) -> list[str]:
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except Exception:
        return []


def count_checksum_warnings() -> int:
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return content.count("校验和重算失败") + content.count("校验和重算异常")
    except Exception:
        return 0


def watchdog_trips_since(ts: float) -> int:
    """统计 watchdog 日志在 ts 时间戳后出现 'ping 不通' 的次数。"""
    try:
        with open(WATCHDOG_LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return 0
    count = 0
    for line in lines:
        # 解析 [2026-07-28 11:21:15] 时间戳
        if line.startswith("[") and "]" in line:
            ts_str = line[1:line.index("]")]
            try:
                line_ts = time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S"))
                if line_ts >= ts and "ping 不通" in line:
                    count += 1
            except ValueError:
                continue
    return count


def http_request(url: str, timeout: int = 15) -> tuple[bool, str, float]:
    """发起 HTTP 请求，返回 (成功, 摘要, 耗时秒)。"""
    import urllib.request as ur
    start = time.time()
    try:
        req = ur.Request(url, headers={"User-Agent": "Telnix-E2E-Test/1.0"})
        with ur.urlopen(req, timeout=timeout) as r:
            body = r.read(500).decode("utf-8", errors="replace")
            elapsed = time.time() - start
            return True, f"HTTP {r.status} | {elapsed:.2f}s | body[:80]={body[:80]!r}", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, f"FAILED after {elapsed:.2f}s: {type(e).__name__}: {e}", elapsed


def main() -> int:
    test_start_ts = time.time()
    log_section("E2E 测试：抓包 + 透明代理 + 系统代理 同时运行")

    # 0. 前置状态
    log_section("[0/6] 前置状态检查")
    try:
        raw_status = api_get("/api/raw/status")
        tp_status = api_get("/api/transparent-proxy/status")
        print(f"raw capture: running={raw_status['data']['running']}, is_admin={raw_status['data']['is_admin']}")
        print(f"transparent proxy: running={tp_status['data']['running']}, is_admin={tp_status['data']['is_admin']}")
    except Exception as e:
        print(f"× 后端不可达: {e}")
        return 1

    # 记录测试前的 watchdog 触发次数，用于后续对比
    pre_test_trips = watchdog_trips_since(test_start_ts - 1)
    pre_checksum_warns = count_checksum_warnings()
    print(f"测试前 watchdog 触发数（含历史）: {pre_test_trips}")
    print(f"测试前 checksum warning 数: {pre_checksum_warns}")

    # 1. 启动 raw capture
    log_section("[1/6] 启动 raw capture（WinDivert SNIFF 模式）")
    try:
        r = api_post("/api/raw/start", {})
        print(f"raw start: code={r.get('code')} msg={r.get('msg')}")
        if r.get("code") != 0:
            print(f"× 启动 raw capture 失败: {r}")
            return 1
    except Exception as e:
        print(f"× 启动 raw capture 异常: {e}")
        return 1
    time.sleep(1)

    # 2. 启动 transparent proxy
    log_section("[2/6] 启动 transparent proxy（WinDivert NETWORK 拦截）")
    try:
        r = api_post("/api/transparent-proxy/start")
        print(f"tp start: code={r.get('code')} msg={r.get('msg')}")
        if r.get("code") != 0:
            print(f"× 启动 transparent proxy 失败: {r}")
            return 1
    except Exception as e:
        print(f"× 启动 transparent proxy 异常: {e}")
        return 1
    time.sleep(1)

    # 3. 启动系统代理（用户说开启抓包会同时开系统代理）
    log_section("[3/6] 启动系统代理 (/api/system/enable-proxy)")
    sys_proxy_started = False
    try:
        r = api_post("/api/system/enable-proxy")
        print(f"enable-proxy: code={r.get('code')} msg={r.get('msg')}")
        if r.get("code") == 0:
            sys_proxy_started = True
    except Exception as e:
        print(f"enable-proxy: 异常 {e}")
    if not sys_proxy_started:
        print("⚠ 系统代理启动失败，继续测试（透明代理应仍能工作）")
    time.sleep(2)

    # 4. HTTP 80 请求（透明代理重定向到本地代理）
    log_section("[4/6] HTTP 80 请求测试（example.com）")
    ok, summary, elapsed = http_request("http://example.com/")
    print(summary)
    if not ok:
        print("× HTTP 80 请求失败 → 复现用户超时问题")
    time.sleep(1)

    # 5. HTTPS 443 请求（透明代理 raw tunnel）
    log_section("[5/6] HTTPS 443 请求测试（example.com）")
    ok2, summary2, elapsed2 = http_request("https://example.com/")
    print(summary2)
    if not ok2:
        print("× HTTPS 443 请求失败 → 复现用户超时问题")
    time.sleep(1)

    # 6. 检查日志和 watchdog
    log_section("[6/6] 日志 + watchdog 检查")
    time.sleep(2)  # 等待日志落盘

    post_checksum_warns = count_checksum_warnings()
    new_checksum_warns = post_checksum_warns - pre_checksum_warns
    print(f"checksum warning 新增: {new_checksum_warns}（前: {pre_checksum_warns} 后: {post_checksum_warns}）")

    watchdog_trips = watchdog_trips_since(test_start_ts)
    print(f"测试期间 watchdog 触发: {watchdog_trips} 次")

    # 检查系统代理是否仍开着
    import subprocess
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings').ProxyEnable"],
            capture_output=True, text=True, timeout=5)
        proxy_enable = result.stdout.strip()
        print(f"系统代理 ProxyEnable: {proxy_enable}")
    except Exception:
        proxy_enable = "?"

    # 检查 transparent proxy 状态
    try:
        tp = api_get("/api/transparent-proxy/status")
        d = tp["data"]
        print(f"transparent proxy: running={d['running']} redirected={d['redirected_count']} "
              f"passed={d['passed_count']} nat_size={d['nat_table_size']} last_error={d['last_error']!r}")
    except Exception as e:
        print(f"× 查询 transparent proxy 状态异常: {e}")

    # 检查 raw capture 状态
    try:
        rs = api_get("/api/raw/status")
        print(f"raw capture: running={rs['data']['running']}")
    except Exception as e:
        print(f"× 查询 raw capture 状态异常: {e}")

    print("\n--- 后端日志最后 15 行 ---")
    for line in tail_log_lines(15):
        print(f"  {line}")

    # 汇总
    log_section("汇总")
    rc = 0
    if new_checksum_warns > 0:
        print(f"× checksum warning 仍出现 {new_checksum_warns} 次 → 修复未生效")
        rc = 1
    else:
        print("✓ 无 checksum warning")
    if watchdog_trips > 0:
        print(f"× watchdog 触发 {watchdog_trips} 次 → 网络崩溃了")
        rc = 1
    else:
        print("✓ watchdog 未触发，网络稳定")
    if not ok:
        print("× HTTP 80 超时/失败")
        rc = 1
    else:
        print(f"✓ HTTP 80 正常（{elapsed:.2f}s）")
    if not ok2:
        print("× HTTPS 443 超时/失败")
        rc = 1
    else:
        print(f"✓ HTTPS 443 正常（{elapsed2:.2f}s）")

    print()
    if rc == 0:
        print("✓✓✓ 全部通过：抓包 + 透明代理 + 系统代理 同时运行无问题")
    else:
        print("××× 存在失败项")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
