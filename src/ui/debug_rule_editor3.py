"""直接审查弹窗，无需先点击按钮：手动打开并审查"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page_errors = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    try:
        page.goto('http://localhost:5173/auto-reply', wait_until='load', timeout=30000)
    except Exception as e:
        print(f"Nav error: {e}")

    page.wait_for_timeout(5000)

    print(f"Page URL: {page.url}")
    print(f"Page errors: {page_errors}")

    # 检查当前页面内容
    body_html = page.evaluate("document.querySelector('.auto-reply-view') ? 'FOUND' : 'NOT FOUND'")
    print(f"auto-reply-view class: {body_html}")

    # 找所有 el-button
    buttons = page.locator('el-button, button').all()
    print(f"\n=== All interactive elements ({len(buttons)}) ===")
    for btn in buttons:
        try:
            txt = btn.inner_text(timeout=1000)
            cls = btn.get_attribute('class', timeout=1000) or ''
            if txt.strip():
                print(f"  [{cls[:40]}] '{txt.strip()[:60]}'")
        except:
            pass

    # 检查 RuleEditor 是否在页面中
    re_exists = page.locator('RuleEditor, .rule-editor-dialog').count()
    print(f"\nRuleEditor/.rule-editor-dialog count: {re_exists}")

    # 看 App.vue 侧边栏 autoReply 是否渲染
    sidebar_items = page.locator('.sidebar-item, .el-menu-item').all()
    print(f"\n=== Sidebar items ===")
    for item in sidebar_items:
        try:
            txt = item.inner_text(timeout=1000)
            if txt.strip():
                print(f"  '{txt.strip()[:50]}'")
        except:
            pass

    page.screenshot(path="d:/Desktop/telnix v2/auto_reply_full.png", full_page=True)
    print("\nScreenshot saved")

    browser.close()
