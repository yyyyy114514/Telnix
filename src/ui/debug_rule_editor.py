"""审查新建规则弹窗的布局问题"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()

    page_errors = []
    page.on("pageerror", lambda err: page_errors.append(str(err)))

    try:
        page.goto('http://localhost:5173/auto-reply', wait_until='load', timeout=30000)
    except Exception as e:
        print(f"Navigation error: {e}")

    page.wait_for_timeout(3000)
    print(f"Page errors: {page_errors}")

    # 定位新建按钮
    buttons = page.locator('button').all()
    print(f"\n=== All buttons (text) ===")
    for i, btn in enumerate(buttons[:30]):
        txt = btn.inner_text(timeout=2000)
        if txt.strip():
            print(f"  [{i}] '{txt.strip()[:60]}'")

    # 找"新建规则"按钮
    new_btn = page.locator('button:has-text("新建规则")').first
    if new_btn.count() > 0:
        print("\n=== Clicking 新建规则 ===")
        new_btn.click(timeout=5000)
        page.wait_for_timeout(2000)
    else:
        # 尝试 el-button type=primary
        primary_btn = page.locator('el-button:has-text("新建")').first
        if primary_btn.count() > 0:
            print("\n=== Clicking primary 新建 ===")
            primary_btn.click(timeout=5000)
            page.wait_for_timeout(2000)
        else:
            print("\n=== Could not find 新建 rule button, checking dialog state ===")

    page.wait_for_timeout(1000)

    # 检查是否存在 rule-editor-dialog
    dialog_exists = page.locator('.rule-editor-dialog').count()
    print(f"\n=== .rule-editor-dialog count: {dialog_exists} ===")

    if dialog_exists > 0:
        d = page.locator('.rule-editor-dialog').first
        # 检查 dialog 整体布局
        style = d.evaluate("""el => {
          const cs = window.getComputedStyle(el);
          return {
            width: cs.width,
            height: cs.height,
            maxHeight: cs.maxHeight,
            minHeight: cs.minHeight,
            display: cs.display,
            flexDir: cs.flexDirection,
            boxSizing: cs.boxSizing,
            padding: cs.padding,
            margin: cs.margin,
          };
        }""")
        print(f"\n=== .rule-editor-dialog computed style ===")
        print(style)

        # 检查 el-dialog__body
        body = page.locator('.rule-editor-dialog .el-dialog__body')
        if body.count() > 0:
            bstyle = body.evaluate("""el => {
              const cs = window.getComputedStyle(el);
              return {
                width: cs.width,
                height: cs.height,
                maxHeight: cs.maxHeight,
                flex: cs.flex,
                flexGrow: cs.flexGrow,
                display: cs.display,
                overflow: cs.overflow,
                padding: cs.padding,
              };
            }""")
            print(f"\n=== .el-dialog__body computed style ===")
            print(bstyle)

            # 检查 body 内部的实际内容高度
            body_content_h = body.evaluate("el => el.scrollHeight")
            body_client_h = body.evaluate("el => el.clientHeight")
            body_child_count = body.evaluate("el => el.children.length")
            print(f"\nbody scrollHeight={body_content_h}, clientHeight={body_client_h}, children={body_child_count}")

        # 检查 el-form
        form = page.locator('.rule-editor-dialog .el-form')
        if form.count() > 0:
            fstyle = form.evaluate("""el => {
              const cs = window.getComputedStyle(el);
              return {
                width: cs.width,
                maxWidth: cs.maxWidth,
                marginLeft: cs.marginLeft,
                marginRight: cs.marginRight,
                marginTop: cs.marginTop,
                marginBottom: cs.marginBottom,
              };
            }""")
            print(f"\n=== .el-form computed style ===")
            print(fstyle)

        # 截图
        page.screenshot(path="d:/Desktop/telnix v2/rule_editor_debug.png", full_page=False)
        print("\nScreenshot saved: rule_editor_debug.png")

    # 也检查页面整体
    page.screenshot(path="d:/Desktop/telnix v2/rule_editor_page.png", full_page=True)
    print("Full page screenshot saved: rule_editor_page.png")

    browser.close()
