"""审计 RuleEditor 弹窗的实际渲染尺寸，定位空白和不居中的根因"""
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
      function cs(n) { return window.getComputedStyle(n); }
      const header = el.querySelector('.el-dialog__header');
      const body = el.querySelector('.el-dialog__body');
      const footer = el.querySelector('.el-dialog__footer');
      const form = el.querySelector('.el-form');
      const items = Array.from(el.querySelectorAll('.el-form-item'));
      const bcs = cs(body);
      const fcs = cs(form);

      const itemDetails = items.map(fi => {
        const c = cs(fi);
        return {
          h: fi.clientHeight,
          mt: parseFloat(c.marginTop) || 0,
          mb: parseFloat(c.marginBottom) || 0,
        };
      });

      const itemsTotal = itemDetails.reduce((s, x) => s + x.h, 0);
      const marginsTotal = itemDetails.reduce((s, x) => s + x.mt + x.mb, 0);

      return {
        dialog: {
          clientH: el.clientHeight,
          scrollH: el.scrollHeight,
          csHeight: cs(el).height,
        },
        header: { clientH: header.clientHeight },
        body: {
          clientH: body.clientHeight,
          scrollH: body.scrollHeight,
          paddingTop: bcs.paddingTop,
          paddingBottom: bcs.paddingBottom,
          csHeight: bcs.height,
          csFlex: bcs.flex,
        },
        form: {
          clientH: form.clientHeight,
          scrollH: form.scrollHeight,
          display: fcs.display,
          marginLeft: fcs.marginLeft,
          marginRight: fcs.marginRight,
          maxWidth: fcs.maxWidth,
          padding: fcs.padding,
          offsetLeft: form.offsetLeft,
        },
        footer: { clientH: footer.clientHeight },
        itemTotal: itemsTotal,
        marginsTotal: marginsTotal,
        items: itemDetails,
        blankInForm: form.clientHeight - (itemsTotal + marginsTotal),
        blankInBody: body.clientHeight - (form.clientHeight + 40 + 12), // alert + alert margin
        leftSpace: form.offsetLeft,
        rightSpace: body.clientWidth - form.offsetLeft - form.clientWidth,
      };
    }""")
    import json
    print(json.dumps(info, indent=2, default=str))

    page.screenshot(path="d:/Desktop/telnix v2/debug_rule_editor.png", full_page=True)
    print("\nScreenshot saved")

    browser.close()
