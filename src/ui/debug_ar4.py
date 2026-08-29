"""审查 el-dialog 内部 DOM 结构 + 空白来源"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:5173/#/auto-reply', wait_until='load', timeout=30000)
    page.wait_for_timeout(5000)

    # 点击新建规则（第3个 primary 按钮）
    buttons = page.locator('button').all()
    for btn in buttons:
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

    # 查看 dialog 内部所有直接子元素
    info = d.evaluate("""el => {
      const cs = window.getComputedStyle(el);
      const children = Array.from(el.children);
      const childInfo = children.map(c => {
        const ccs = window.getComputedStyle(c);
        return {
          tag: c.tagName,
          cls: c.className ? c.className.slice(0, 120) : '',
          styleH: c.style.height,
          csH: ccs.height,
          csW: ccs.width,
          csDisplay: ccs.display,
          csFlex: ccs.flex,
          csFlexGrow: ccs.flexGrow,
          clientH: c.clientHeight,
          clientW: c.clientWidth,
        };
      });
      return {
        dialog_h: cs.height,
        dialog_clientH: el.clientHeight,
        dialog_display: cs.display,
        dialog_flex: cs.flex,
        dialog_flexDir: cs.flexDirection,
        dialog_alignContent: cs.alignContent,
        dialog_alignItems: cs.alignItems,
        dialog_justContent: cs.justifyContent,
        dialog_minH: cs.minHeight,
        dialog_padding: cs.padding,
        dialog_boxSizing: cs.boxSizing,
        children: childInfo,
      };
    }""")
    import json
    print("=== dialog info ===")
    print(json.dumps(info, indent=2, default=str))

    # 查看 el-form 高度
    form = page.locator('.rule-editor-dialog .el-form')
    if form.count() > 0:
        f = form.first
        f_info = f.evaluate("""el => {
          const cs = window.getComputedStyle(el);
          return {
            h: cs.height,
            clientH: el.clientHeight,
            scrollH: el.scrollHeight,
            display: cs.display,
            width: cs.width,
            marginLeft: cs.marginLeft,
            marginRight: cs.marginRight,
            paddingLeft: cs.paddingLeft,
            paddingRight: cs.paddingRight,
            maxHeight: cs.maxHeight,
          };
        }""")
        print("\n=== form ===")
        print(json.dumps(f_info, indent=2))

        # form 内部子元素
        f_children = f.evaluate("""el => {
          return Array.from(el.children).map(c => {
            const cs = window.getComputedStyle(c);
            return {
              tag: c.tagName,
              cls: c.className ? c.className.slice(0, 80) : '',
              clientH: c.clientHeight,
              csH: cs.height,
            };
          });
        }""")
        print("\n=== form children ===")
        print(json.dumps(f_children, indent=2, default=str))

    # 截图
    page.screenshot(path="d:/Desktop/telnix v2/rule_editor_inspect.png", full_page=True)
    print("\nScreenshot saved: rule_editor_inspect.png")

    browser.close()
