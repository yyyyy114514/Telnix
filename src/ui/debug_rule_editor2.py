"""审查 auto-reply 页面的全部按钮和新规则弹窗"""
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

    page.wait_for_timeout(3000)

    # 查找所有带文字的 button 和 el-button 容器
    print("=== ALL buttons (full page) ===")
    all_btns = page.locator('button').all()
    for i, btn in enumerate(all_btns):
        txt = btn.inner_text(timeout=2000)
        if txt.strip():
            print(f"  [{i}] '{txt.strip()[:80]}'")

    # 查找任何包含"新建"元素的元素
    print("\n=== Elements with '新建' text ===")
    els = page.locator('text=/新建/').all()
    print(f"  count: {len(els)}")
    for e in els:
        tag = e.evaluate("el => el.tagName + (el.className ? '.' + el.className.split(' ')[0] : '')")
        print(f"  {tag}: '{e.inner_text(timeout=1000)[:60]}'")

    # 尝试直接找"新建规则"按钮
    new_btn = page.locator('button').filter(has_text="新建规则").first
    cnt = new_btn.count()
    print(f"\n=== 新建规则 button count: {cnt} ===")
    if cnt > 0:
        new_btn.click()
        page.wait_for_timeout(3000)

        # 检查弹窗
        dialog = page.locator('.rule-editor-dialog')
        print(f"\n=== .rule-editor-dialog count: {dialog.count()} ===")
        if dialog.count() > 0:
            print(dialog.evaluate("""el => {
              const cs = window.getComputedStyle(el);
              return JSON.stringify({
                width: cs.width, height: cs.height,
                display: cs.display, flexDir: cs.flexDirection,
                alignContent: cs.alignContent,
              });
            }"""))
            page.screenshot(path="d:/Desktop/telnix v2/rule_editor_dialog.png", full_page=False)
            print("Dialog screenshot saved")
        else:
            page.screenshot(path="d:/Desktop/telnix v2/rule_editor_after_click.png", full_page=True)
            print("Screenshot saved")

    else:
        # 找不到，检查 AutoReplyView 是否有 dialog
        # 用更宽松的定位
        candidates = page.locator('.auto-reply-page button, [class*="auto-reply"] button').all()
        print(f"\n=== Candidate buttons in auto-reply: {len(candidates)} ===")
        page.screenshot(path="d:/Desktop/telnix v2/auto_reply_page.png", full_page=True)
        print("Screenshot saved")

    browser.close()
