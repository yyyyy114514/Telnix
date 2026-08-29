"""精确审计：form 未居中的根因"""
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
      const bcs = window.getComputedStyle(body);
      return {
        bodyWidth: body.clientWidth,
        bodyPaddingLeft: bcs.paddingLeft,
        bodyPaddingRight: bcs.paddingRight,
        formWidth: form.clientWidth,
        formMarginLeft: fcs.marginLeft,
        formMarginRight: fcs.marginRight,
        formMaxWidth: fcs.maxWidth,
        formDisplay: fcs.display,
        formPaddingLeft: fcs.paddingLeft,
        formPaddingRight: fcs.paddingRight,
        formInnerWidth: form.clientWidth - (parseFloat(fcs.paddingLeft)||0) - (parseFloat(fcs.paddingRight)||0),
        // 可用宽度
        bodyInnerW: body.clientWidth - (parseFloat(bcs.paddingLeft)||0) - (parseFloat(bcs.paddingRight)||0),
        formOffsetLeft: form.offsetLeft,
        bodyOffsetLeft: body.offsetLeft,
        hasElFormItemLabel: form.querySelector('.el-form-item__label') ? true : false,
        firstLabel: form.querySelector('.el-form-item__label') ? 
          window.getComputedStyle(form.querySelector('.el-form-item__label')).width : null,
      };
    }""")
    import json
    print(json.dumps(info, indent=2, default=str))

    browser.close()
