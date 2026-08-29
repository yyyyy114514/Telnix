"""精确审计：form 高度的来源，找到 58px 差异"""
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
      const alert = el.querySelector('.el-alert');
      const fcs = window.getComputedStyle(form);
      const bcs = window.getComputedStyle(body);
      const acs = window.getComputedStyle(alert);

      const items = form.querySelectorAll('.el-form-item');
      let itemsTotal = 0;
      let marginsTotal = 0;
      const itemDetails = [];
      items.forEach(fi => {
        const fic = window.getComputedStyle(fi);
        const mt = parseFloat(fic.marginTop) || 0;
        const mb = parseFloat(fic.marginBottom) || 0;
        itemsTotal += fi.clientHeight;
        marginsTotal += mt + mb;
        itemDetails.push({
          h: fi.clientHeight,
          marginTop: mt,
          marginBottom: mb,
        });
      });

      return {
        dialog: {
          clientH: el.clientHeight,
          scrollH: el.scrollHeight,
        },
        body: {
          clientH: body.clientHeight,
          scrollH: body.scrollHeight,
          paddingTop: bcs.paddingTop,
          paddingBottom: bcs.paddingBottom,
        },
        alert: {
          clientH: alert.clientHeight,
          marginTop: acs.marginTop,
          marginBottom: acs.marginBottom,
        },
        form: {
          clientH: form.clientHeight,
          scrollTop: form.scrollTop,
          scrollHeight: form.scrollHeight,
        },
        formItemsTotalH: itemsTotal,
        formItemsTotalMargins: marginsTotal,
        formItemsCount: items.length,
        itemDetails: itemDetails,
        // 计算 form 理论高度
        formExpected: itemsTotal + marginsTotal,
        formActual: form.clientHeight,
        formDiff: form.clientHeight - (itemsTotal + marginsTotal),
        // 空白：body 高度 - (alert + alert_mb + form)
        bodyHeight: body.clientHeight,
        contentHeight: alert.clientHeight + (parseFloat(acs.marginBottom)||0) + form.clientHeight,
        blankInBody: body.clientHeight - (alert.clientHeight + (parseFloat(acs.marginBottom)||0) + form.clientHeight),
      };
    }""")
    import json
    print(json.dumps(info, indent=2, default=str))

    browser.close()
