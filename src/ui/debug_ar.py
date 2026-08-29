"""审查 auto-reply 页面 + 弹窗布局"""
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
    print(f"URL: {page.url}")
    print(f"Page errors: {page_errors}")

    # 检查 auto-reply 页内容
    has_ar = page.evaluate("!!document.querySelector('.auto-reply-view')")
    print(f"auto-reply-view exists: {has_ar}")

    # 找所有 button
    buttons = page.locator('button').all()
    print(f"\n=== Buttons ({len(buttons)}) ===")
    for btn in buttons:
        try:
            txt = btn.inner_text(timeout=1000).strip()
            if txt:
                print(f"  '{txt[:60]}'")
        except:
            pass

    # 查找"新建规则"按钮（通过文本内容匹配）
    print("\n=== Looking for 新建规则 button ===")
    candidates = page.locator('button').filter(has_text="规则").all()
    print(f"buttons with '规则': {len(candidates)}")

    # 尝试点击"新建规则"按钮（通过坐标）
    # 先截图定位
    page.screenshot(path="d:/Desktop/telnix v2/ar1.png", full_page=True)
    print("\nScreenshot 1 saved")

    # 直接调用 JS 触发 newRule
    print("\n=== Triggering editor via JS ===")
    # 先检查 editorVisible 状态
    vis = page.evaluate("""
      () => {
        const app = document.querySelector('#app');
        if (!app) return 'no app';
        // 找所有 RuleEditor 实例
        const dialogs = document.querySelectorAll('.rule-editor-dialog');
        return dialogs.length;
      }
    """)
    print(f"dialogs on page: {vis}")

    # 通过点击带"Plus"图标的按钮触发
    plus_btn = page.locator('button').filter(has_text="Plus").first
    if plus_btn.count() == 0:
        # 尝试通过 el-icon > Plus 查找
        # 找到所有带 title="新建规则" 的按钮
        title_btn = page.locator('[title*="新建规则"]').first
        print(f"title contains 新建规则: {title_btn.count()}")
        if title_btn.count() > 0:
            title_btn.click()
        else:
            # 找 primary 按钮
            primary = page.locator('button.el-button--primary').first
            print(f"primary buttons: {primary.count()}")
            if primary.count() > 0:
                primary.click()
            else:
                # 最后手段：直接通过点击坐标
                print("Clicking by position near page header")
                page.click('[class*="page-header"] .el-button--primary', timeout=3000)

    page.wait_for_timeout(3000)

    # 检查弹窗
    dcount = page.locator('.rule-editor-dialog').count()
    print(f"\n.rule-editor-dialog count: {dcount}")

    if dcount > 0:
        d = page.locator('.rule-editor-dialog').first
        info = d.evaluate("""el => {
          const cs = window.getComputedStyle(el);
          const body = el.querySelector('.el-dialog__body');
          const bodyCs = body ? window.getComputedStyle(body) : {};
          const form = el.querySelector('.el-form');
          const formCs = form ? window.getComputedStyle(form) : {};
          return {
            dialog: {
              w: cs.width, h: cs.height, display: cs.display,
              flexDir: cs.flexDirection, alignContent: cs.alignContent,
              padding: cs.padding,
            },
            body: bodyCs ? {
              w: bodyCs.width, h: bodyCs.height,
              flex: bodyCs.flex, flexGrow: bodyCs.flexGrow,
              display: bodyCs.display, overflow: bodyCs.overflow,
              padding: bodyCs.padding,
            } : null,
            bodyScrollH: body ? body.scrollHeight : null,
            bodyClientH: body ? body.clientHeight : null,
            form: formCs ? {
              w: formCs.width, maxWidth: formCs.maxWidth,
              marginLeft: formCs.marginLeft, marginRight: formCs.marginRight,
            } : null,
          };
        }""")
        print("\n=== Dialog computed styles ===")
        import json
        print(json.dumps(info, indent=2))

        page.screenshot(path="d:/Desktop/telnix v2/ar2_dialog.png", full_page=False)
        print("Dialog screenshot saved")
    else:
        print("\nNo dialog found, showing page")
        page.screenshot(path="d:/Desktop/telnix v2/ar3_no_dialog.png", full_page=True)

    browser.close()
