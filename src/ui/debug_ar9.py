"""精确定位 130px+ 空白来源"""
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
      const alert = el.querySelector('.el-alert');
      const form = el.querySelector('.el-form');
      const body = el.querySelector('.el-dialog__body');
      const bcs = window.getComputedStyle(body);
      const acs = window.getComputedStyle(alert);
      const fcs = window.getComputedStyle(form);

      // form 内部元素
      const formItems = form.querySelectorAll('.el-form-item');
      const itemsInfo = Array.from(formItems).map(fi => {
        const fic = window.getComputedStyle(fi);
        return {
          cls: fi.className ? fi.className.slice(0, 100) : '',
          clientH: fi.clientHeight,
          marginTop: fic.marginTop,
          marginBottom: fic.marginBottom,
          padding: fic.padding,
        };
      });

      return {
        body: {
          clientH: body.clientHeight,
          scrollH: body.scrollHeight,
          padding: bcs.padding,
          paddingTop: bcs.paddingTop,
          paddingBottom: bcs.paddingBottom,
          paddingLeft: bcs.paddingLeft,
          paddingRight: bcs.paddingRight,
        },
        alert: {
          clientH: alert.clientHeight,
          marginBottom: acs.marginBottom,
          marginTop: acs.marginTop,
        },
        form: {
          clientH: form.clientHeight,
          marginTop: fcs.marginTop,
          marginBottom: fcs.marginBottom,
          paddingTop: fcs.paddingTop,
          paddingBottom: fcs.paddingBottom,
        },
        // 空白计算
        gapAfterAlert: (form.offsetTop - (alert.offsetTop + alert.clientHeight)),
        // form 内各项高度累加
        formItems: itemsInfo,
        itemsTotalH: itemsInfo.reduce((s, x) => s + x.clientH, 0),
        formItemsTotalMargin: itemsInfo.reduce((s, x) => 
          s + (parseFloat(x.marginTop) || 0) + (parseFloat(x.marginBottom) || 0), 0),
        formClientH: form.clientHeight,
        formItemsClientHTotal: itemsInfo.reduce((s, x) => s + x.clientH, 0),
        formItemsMarginTotal: itemsInfo.reduce((s, x) => 
          s + (parseFloat(x.marginTop) || 0) + (parseFloat(x.marginBottom) || 0), 0),
        diff: form.clientHeight - itemsInfo.reduce((s, x) => s + x.clientH, 0),
      };
    }""")
    import json
    print(json.dumps(info, indent=2, default=str))

    browser.close()
