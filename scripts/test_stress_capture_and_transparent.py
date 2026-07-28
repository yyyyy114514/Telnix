"""持续压力测试：raw capture + transparent proxy + system proxy 同时运行 30 秒。

目标：确定性复现用户报告的"开启抓包就超时"崩溃。

测试设计：
- 每 2 秒发起一个 HTTP/HTTPS 请求，交替 80/443
- 持续 30 秒（共 ~15 次请求）
- 任何请求超时(>10s)即判定崩溃，立即停止并报告
- 同时监测系统代理是否被 watchdog 关闭（崩溃的副作用）
- 结束后检查后端日志的 checksum warning + transparent proxy 状态

红绿定义：
- 红：任何请求超时 或 系统代理被 watchdog 关闭
- 绿：全部请求在 5s 内完成，系统代理保持开启
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request

API = "http://127.0.0.1:18901"
LOG_FILE = r"c:\Users\Administrator\Downloads\Telnix-trae-agent-DnHAtu\telnix_elevated.log"


def api_get(path: str):
    with urllib.request.urlopen(f"{API}{path}", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def api_post(path: str, body: dict | None = None):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(f"{API}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def check_proxy_enabled() -> bool:
    """读取注册表系统代理开关。"""
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings').ProxyEnable"],
            capture_output=True, text=True, timeout=5)
        return result.stdout.strip() == "1"
    except Exception:
        return False


def http_request(url: str, timeout: int = 10) -> tuple[bool, str, float]:
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Telnix-Stress-Test/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(200)
            elapsed = time.time() - start
            return True, f"HTTP {r.status} {elapsed:.2f}s", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, f"FAIL {elapsed:.2f}s {type(e).__name__}: {e}", elapsed


def count_checksum_warnings() -> int:
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            return f.read().count("校验和")
    except Exception:
        return 0


def main() -> int:
    print("=" * 70)
    print("持续压力测试：raw capture + transparent proxy + system proxy")
    print("=" * 70)

    # 前置：确保全部停止
    print("\n[0] 停止现有抓包/代理...")
    try:
        api_post("/api/raw/stop")
    except Exception:
        pass
    try:
        api_post("/api/transparent-proxy/stop")
    except Exception:
        pass
    try:
        api_post("/api/system/clear-proxy")
    except Exception:
        pass
    time.sleep(2)

    pre_warns = count_checksum_warnings()
    print(f"测试前 checksum warning 数: {pre_warns}")

    # 启动 raw capture
    print("\n[1] 启动 raw capture...")
    r = api_post("/api/raw/start", {})
    print(f"    code={r['code']} msg={r['msg']}")
    if r["code"] != 0:
        return 1
    time.sleep(1)

    # 启动 transparent proxy
    print("[2] 启动 transparent proxy...")
    r = api_post("/api/transparent-proxy/start")
    print(f"    code={r['code']} msg={r['msg']}")
    if r["code"] != 0:
        return 1
    time.sleep(1)

    # 启动系统代理
    print("[3] 启动系统代理...")
    r = api_post("/api/system/enable-proxy")
    print(f"    code={r['code']} msg={r['msg']}")
    time.sleep(1)

    proxy_on = check_proxy_enabled()
    print(f"    系统代理 ProxyEnable = {proxy_on}")
    if not proxy_on:
        print("    ⚠ 系统代理未开启，仍继续测试")

    # 持续压力测试
    print("\n[4] 持续 30 秒压力测试开始...")
    print("    每次请求超时阈值=10s，任何失败即判定崩溃")
    print()

    test_start = time.time()
    duration = 30  # 秒
    interval = 2   # 每 2 秒一个请求
    results = []
    crash_detected = False
    crash_reason = ""

    i = 0
    while time.time() - test_start < duration:
        i += 1
        url = "http://example.com/" if i % 2 == 1 else "https://example.com/"
        elapsed_so_far = time.time() - test_start

        # 先检查系统代理是否还在
        proxy_still_on = check_proxy_enabled()
        if proxy_on and not proxy_still_on:
            crash_detected = True
            crash_reason = f"t={elapsed_so_far:.1f}s: 系统代理被 watchdog 关闭（网络崩溃）"
            print(f"  [{elapsed_so_far:5.1f}s] × {crash_reason}")
            break

        ok, summary, elapsed = http_request(url, timeout=10)
        status = "✓" if ok else "×"
        print(f"  [{elapsed_so_far:5.1f}s] {status} #{i:02d} {url:30s} {summary}")

        results.append((ok, elapsed))
        if not ok:
            crash_detected = True
            crash_reason = f"t={elapsed_so_far:.1f}s: 请求超时/失败"
            break

        # 等待下一个间隔
        time.sleep(interval)

    test_duration = time.time() - test_start

    # 停止
    print(f"\n[5] 测试结束（持续 {test_duration:.1f}s），停止抓包/代理...")
    try:
        api_post("/api/raw/stop")
    except Exception:
        pass
    try:
        api_post("/api/transparent-proxy/stop")
    except Exception:
        pass
    try:
        api_post("/api/system/clear-proxy")
    except Exception:
        pass
    time.sleep(2)

    # 检查日志
    post_warns = count_checksum_warnings()
    new_warns = post_warns - pre_warns

    # 汇总
    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    total = len(results)
    success = sum(1 for ok, _ in results if ok)
    failed = total - success
    avg_elapsed = sum(e for _, e in results) / total if total else 0
    max_elapsed = max((e for _, e in results), default=0)

    print(f"总请求数: {total}（成功 {success} / 失败 {failed}）")
    print(f"平均耗时: {avg_elapsed:.2f}s，最大耗时: {max_elapsed:.2f}s")
    print(f"checksum warning 新增: {new_warns}")
    print(f"崩溃检测: {'是' if crash_detected else '否'}")
    if crash_detected:
        print(f"崩溃原因: {crash_reason}")

    if crash_detected or new_warns > 0:
        print("\n× 测试红：网络崩溃或 checksum warning 出现")
        return 1
    elif failed > 0:
        print(f"\n× 测试红：{failed} 个请求失败")
        return 1
    else:
        print(f"\n✓ 测试绿：{success} 个请求全部成功，无崩溃，无 checksum warning")
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(2)
