"""直接检查 RuleEditor 弹窗的实际渲染状态（手动触发）"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.on("pageerror", lambda err: print(f"PAGE ERROR: {err}"))

    # 先导航到 auto-reply，确保 RuleEditor 被加载
    page.goto('http://localhost:5173/#/auto-reply', wait_until='load', timeout=30000)
    page.wait_for_timeout(5000)

    # 先检查 auto-reply-view 是否被正确挂载
    ar_exists = page.evaluate("!!document.querySelector('.auto-reply-view')")
    print(f"auto-reply-view exists: {ar_exists}")

    if not ar_exists:
        # 尝试强制刷新
        print("\nTrying navigation again...")
        page.goto('http://localhost:5173/#/auto-reply', wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(5000)
        ar_exists = page.evaluate("!!document.querySelector('.auto-reply-view')")
        print(f"auto-reply-view exists (retry): {ar_exists}")

    # 查看当前页面内容
    print(f"\nURL: {page.url}")

    # 找 auto-reply 页的按钮
    buttons = page.locator('button').all()
    for btn in buttons:
        try:
            txt = btn.inner_text(timeout=1000).strip()
            if '新建' in txt or '规则' in txt or 'Rule' in txt:
                print(f"FOUND: '{txt}'")
        except:
            pass

    # 直接通过 JS 触发：找到 app 组件实例并调用 newRule
    # 更好的方法：直接定位 DOM 元素
    # 先看看 DOM 结构
    body = page.locator('.auto-reply-view')
    if body.count() > 0:
        # 查找 primary 按钮
        primary = body.locator('.el-button--primary').first
        if primary.count() > 0:
            print(f"\nClicking primary button: {primary.inner_text(timeout=2000)}")
            primary.click()
            page.wait_for_timeout(3000)
        else:
            # 找所有 el-button 元素
            all_btns = body.locator('.el-button').all()
            print(f"\nAll el-button elements in auto-reply: {len(all_btns)}")
            for i, b in enumerate(all_btns):
                try:
                    txt = b.inner_text(timeout=500)
                    print(f"  [{i}] '{txt[:40]}'")
                except:
                    print(f"  [{i}] <no text>")

    # 检查弹窗
    dialogs = page.locator('.rule-editor-dialog').all()
    print(f"\nRuleEditor dialogs: {len(dialogs)}")

    if dialogs:
        d = dialogs[0]
        info = d.evaluate("""el => {
          const cs = window.getComputedStyle(el);
          const body = el.querySelector('.el-dialog__body');
          const form = el.querySelector('.el-form');
          const wrapper = document.querySelector('.rule-editor-dialog-wrapper') || 
                          el.parentElement;
          return {
            dialog: {
              w: cs.width, h: cs.height,
              display: cs.display,
              flexDirection: cs.flexDirection,
              alignItems: cs.alignItems,
              justifyContent: cs.justifyContent,
              padding: cs.padding,
              margin: cs.margin,
              boxSizing: cs.boxSizing,
              minHeight: cs.minHeight,
              maxHeight: cs.maxHeight,
            },
            body: body ? {
              w: body.clientWidth, h: body.clientHeight,
              scrollH: body.scrollHeight,
              flex: body.style.flex,
              display: window.getComputedStyle(body).display,
              overflow: window.getComputedStyle(body).overflow,
              padding: window.getComputedStyle(body).padding,
              flexGrow: window.getComputedStyle(body).flexGrow,
            } : null,
            form: form ? {
              w: form.clientWidth,
              display: window.getComputedStyle(form).display,
              margin: window.getComputedStyle(form).margin,
              maxWidth: window.getComputedStyle(form).maxWidth,
            } : null,
          };
        }""")
        print("\n=== Dialog computed info ===")
        import json
        print(json.dumps(info, indent=2, default=str))

        page.screenshot(path="d:/Desktop/telnix v2/rule_editor_full.png", full_page=True)
        print("\nScreenshot saved")
    else:
        # 检查 el-overlay
        overlay = page.locator('.el-overlay').count()
        print(f"el-overlay count: {overlay}")
        page.screenshot(path="d:/Desktop/telnix v2/auto_reply_page.png", full_page=True)

    browser.close()
