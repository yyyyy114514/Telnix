"""验证：form 居中 + 无下方空白"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:5173/#/auto-reply', wait_until='load', timeout=30000)
    page.wait_for_timeout(5000)

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
      const form = el.querySelector('.el-form');
      const body = el.querySelector('.el-dialog__body');
      const fcs = window.getComputedStyle(form);
      return {
        dialog_clientH: el.clientHeight,
        dialog_csHeight: window.getComputedStyle(el).height,
        body_clientH: body.clientHeight,
        body_csHeight: window.getComputedStyle(body).height,
        form_clientH: form.clientHeight,
        form_display: fcs.display,
        form_marginLeft: fcs.marginLeft,
        form_marginRight: fcs.marginRight,
        form_maxW: fcs.maxWidth,
        form_offsetLeft: form.offsetLeft,
        left_space: form.offsetLeft,
        right_space: body.clientWidth - form.offsetLeft - form.clientWidth,
        // form-item margins
        lastFormItem_mb: el.querySelector('.el-form-item:last-child') ?
          window.getComputedStyle(el.querySelector('.el-form-item:last-child')).marginBottom : 'N/A',
        firstFormItem_mb: el.querySelector('.el-form-item:first-child') ?
          window.getComputedStyle(el.querySelector('.el-form-item:first-child')).marginBottom : 'N/A',
      };
    }""")
    import json
    print(json.dumps(info, indent=2, default=str))

    # 截图
    page.screenshot(path="d:/Desktop/telnix v2/rule_editor_final.png", full_page=True)
    print("\nScreenshot saved")

    browser.close()
