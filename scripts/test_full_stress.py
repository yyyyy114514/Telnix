"""完整 60 秒压力测试 + 验证 502 是否为启动延迟。

测试 1：启动后等 3 秒再发请求 → 验证 502 是否消失
测试 2：60 秒持续压力 → HTTP/HTTPS 交替，验证长期稳定
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

API = "http://127.0.0.1:18901"


def api_post(path: str, body: dict | None = None, timeout: float = 15):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(f"{API}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check_proxy() -> bool:
    try:
        r = subprocess.run(
            ["powershell", "-Command",
             "(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings').ProxyEnable"],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "1"
    except Exception:
        return False


def req_once(url: str, timeout: int = 8) -> tuple[bool, str, float]:
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Telnix-Stress/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(200)
            elapsed = time.time() - start
            return True, f"{r.status} {elapsed:.2f}s", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, f"{elapsed:.2f}s {type(e).__name__}", elapsed


def stop_all():
    for p in ["/api/raw/stop", "/api/transparent-proxy/stop", "/api/system/clear-proxy"]:
        try:
            api_post(p, timeout=5)
        except Exception:
            pass
    time.sleep(2)


def main() -> int:
    print("=" * 60)
    print("完整压力测试：启动延迟验证 + 60 秒持续压力")
    print("=" * 60)

    stop_all()

    # 启动
    print("\n[1] 启动 raw + tp + 系统代理")
    print(f"  raw: {api_post('/api/raw/start', {})['msg']}")
    time.sleep(0.5)
    print(f"  tp:  {api_post('/api/transparent-proxy/start')['msg']}")
    time.sleep(0.5)
    print(f"  sys: {api_post('/api/system/enable-proxy')['msg']}")
    print(f"  ProxyEnable = {check_proxy()}")

    # 测试 1：启动后等 3 秒
    print("\n[2] 等 3 秒让初始化完成...")
    time.sleep(3)

    print("\n[3] 启动延迟验证（5 个 HTTP + 5 个 HTTPS）")
    http_ok = 0
    https_ok = 0
    for i in range(1, 6):
        ok, s, _ = req_once(f"http://example.com/?d={i}")
        print(f"  HTTP  #{i} {'✓' if ok else '×'} {s}")
        if ok:
            http_ok += 1
        time.sleep(0.3)
    for i in range(1, 6):
        ok, s, _ = req_once(f"https://example.com/?d={i}")
        print(f"  HTTPS #{i} {'✓' if ok else '×'} {s}")
        if ok:
            https_ok += 1
        time.sleep(0.3)

    print(f"\n  HTTP: {http_ok}/5, HTTPS: {https_ok}/5")

    # 测试 2：60 秒持续压力
    print("\n[4] 60 秒持续压力测试（HTTP/HTTPS 交替，1s 间隔）")
    print("    超时 8s，任何失败标记 ×")
    test_start = time.time()
    duration = 60
    results = []
    i = 0
    while time.time() - test_start < duration:
        i += 1
        url = f"http://example.com/?t={int(time.time())}" if i % 2 == 1 else f"https://example.com/?t={int(time.time())}"
        proto = "HTTP " if i % 2 == 1 else "HTTPS"
        elapsed_so_far = time.time() - test_start

        # 检查系统代理
        if not check_proxy():
            print(f"  [{elapsed_so_far:5.1f}s] × 系统代理被关闭！")
            break

        ok, s, elapsed = req_once(url, timeout=8)
        mark = "✓" if ok else "×"
        print(f"  [{elapsed_so_far:5.1f}s] #{i:02d} {proto} {mark} {s}")
        results.append((ok, elapsed))
        time.sleep(1)

    test_duration = time.time() - test_start

    # 停止
    print(f"\n[5] 停止（压力测试持续 {test_duration:.1f}s）")
    api_post("/api/transparent-proxy/stop")
    api_post("/api/raw/stop")
    api_post("/api/system/clear-proxy")
    time.sleep(1)

    # 汇总
    total = len(results)
    success = sum(1 for ok, _ in results if ok)
    failed = total - success
    latencies = [e for _, e in results]
    print(f"\n{'='*60}")
    print(f"汇总")
    print(f"{'='*60}")
    print(f"启动延迟验证: HTTP {http_ok}/5, HTTPS {https_ok}/5")
    print(f"60秒压力测试: {total} 请求, 成功 {success}, 失败 {failed}")
    if latencies:
        print(f"延迟: avg={sum(latencies)/len(latencies):.2f}s max={max(latencies):.2f}s min={min(latencies):.2f}s")
    print(f"系统代理: {'保持开启' if check_proxy() else '被关闭'}")

    if failed == 0 and http_ok == 5 and https_ok == 5:
        print("\n✓✓✓ 全部通过")
        return 0
    else:
        print(f"\n××× 有 {failed + (5-http_ok) + (5-https_ok)} 个失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
