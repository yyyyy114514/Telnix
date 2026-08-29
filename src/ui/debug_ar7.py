"""精确审计：检查 form 的实际 CSS 应用状态"""
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
      const fcs = window.getComputedStyle(form);

      // 收集 form 上所有生效的 CSS 规则（含来源）
      const rules = [];
      for (const sheet of document.styleSheets) {
        try {
          for (const r of sheet.cssRules) {
            if (r.type !== 1) continue;
            if (r.selectorText && r.selectorText.includes('.el-form') && r.selectorText.includes('.rule-editor-dialog')) {
              // 检查这个 rule 是否匹配当前 form
              if (form.matches(r.selectorText)) {
                rules.push({
                  selector: r.selectorText,
                  display: r.style.display,
                  margin: r.style.margin,
                  marginLeft: r.style.marginLeft,
                  marginRight: r.style.marginRight,
                  maxWidth: r.style.maxWidth,
                  padding: r.style.padding,
                  sheet: sheet.ownerNode ? sheet.ownerNode.className : 'stylesheet',
                });
              }
            }
          }
        } catch(e) {
          // cross-origin 限制跳过
        }
      }

      return {
        formComputed: {
          width: fcs.width,
          display: fcs.display,
          marginLeft: fcs.marginLeft,
          marginRight: fcs.marginRight,
          maxWidth: fcs.maxWidth,
          padding: fcs.padding,
          paddingLeft: fcs.paddingLeft,
          paddingRight: fcs.paddingRight,
          boxSizing: fcs.boxSizing,
        },
        formOffsetLeft: form.offsetLeft,
        formClientWidth: form.clientWidth,
        bodyClientWidth: el.querySelector('.el-dialog__body').clientWidth,
        // 左右空白
        leftSpace: form.offsetLeft,
        rightSpace: el.querySelector('.el-dialog__body').clientWidth - form.offsetLeft - form.clientWidth,
        matchingRules: rules,
      };
    }""")
    import json
    print(json.dumps(info, indent=2, default=str))

    browser.close()
