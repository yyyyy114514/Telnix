"""验证修复效果：新建规则弹窗是否还有空白/不居中"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:5173/#/auto-reply', wait_until='load', timeout=30000)
    page.wait_for_timeout(5000)

    # 找"新建规则"按钮
    for btn in page.locator('button').all():
        try:
            txt = btn.inner_text(timeout=500).strip()
            if txt == '新建规则':
                btn.click()
                break
        except:
            pass

    page.wait_for_timeout(3000)
    page.wait_for_selector('.rule-editor-dialog', timeout=5000)
    d = page.locator('.rule-editor-dialog').first

    info = d.evaluate("""el => {
      const cs = window.getComputedStyle(el);
      function nodeInfo(n) {
        const c = window.getComputedStyle(n);
        return {
          tag: n.tagName,
          csH: c.height,
          clientH: n.clientHeight,
          display: c.display,
          flex: c.flex,
          alignSelf: c.alignSelf,
        };
      }
      const body = el.querySelector('.el-dialog__body');
      const form = el.querySelector('.el-form');
      return {
        dialog: {
          h: cs.height,
          clientH: el.clientHeight,
          display: cs.display,
          flex: cs.flex,
          alignSelf: cs.alignSelf,
          boxSizing: cs.boxSizing,
        },
        body: body ? {
          h: window.getComputedStyle(body).height,
          clientH: body.clientHeight,
          display: window.getComputedStyle(body).display,
        } : null,
        form: form ? {
          w: form.clientWidth,
          display: window.getComputedStyle(form).display,
          marginLeft: window.getComputedStyle(form).marginLeft,
          marginRight: window.getComputedStyle(form).marginRight,
          maxW: window.getComputedStyle(form).maxWidth,
          clientH: form.clientHeight,
        } : null,
      };
    }""")
    import json
    print("=== Dialog info ===")
    print(json.dumps(info, indent=2, default=str))

    # 判断是否有空白：dialog clientH 应该接近 body clientH + header + footer
    page.screenshot(path="d:/Desktop/telnix v2/rule_editor_after_fix.png", full_page=True)
    print("\nScreenshot saved")

    browser.close()
