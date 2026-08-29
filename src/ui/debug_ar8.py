"""精确审计：找到 dialog 高度 660px 来源（非 550px 内容）"""
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

    # 检查 wrapper 高度来源
    info = page.evaluate("""() => {
      const dialog = document.querySelector('.rule-editor-dialog');
      const wrapper = document.querySelector('.el-dialog__wrapper');
      const overlay = document.querySelector('.el-overlay');
      const overlayDialog = overlay ? overlay.querySelector('.el-overlay-dialog') : null;

      function dim(n) {
        if (!n) return null;
        const cs = window.getComputedStyle(n);
        return {
          tag: n.tagName,
          cls: n.className ? n.className.slice(0, 100) : '',
          height: cs.height,
          clientH: n.clientHeight,
          offsetH: n.offsetHeight,
          display: cs.display,
          flex: cs.flex,
          flexGrow: cs.flexGrow,
          minHeight: cs.minHeight,
          padding: cs.padding,
          boxSizing: cs.boxSizing,
        };
      }

      return {
        overlay: dim(overlay),
        overlayDialog: dim(overlayDialog),
        wrapper: dim(wrapper),
        dialog: dim(dialog),
      };
    }""")
    import json
    print(json.dumps(info, indent=2, default=str))

    # 检查 el-dialog 上有没有 min-height
    d = page.locator('.rule-editor-dialog').first
    dh = d.evaluate("""el => {
      const cs = window.getComputedStyle(el);
      return {
        height: cs.height,
        minHeight: cs.minHeight,
        clientH: el.clientHeight,
        scrollH: el.scrollHeight,
        // 检查子元素
        headerH: el.querySelector('.el-dialog__header').clientHeight,
        bodyH: el.querySelector('.el-dialog__body').clientHeight,
        bodyScrollH: el.querySelector('.el-dialog__body').scrollHeight,
        footerH: el.querySelector('.el-dialog__footer').clientHeight,
        // 子元素总高
        sum: (el.querySelector('.el-dialog__header').clientHeight + 
              el.querySelector('.el-dialog__body').clientHeight + 
              el.querySelector('.el-dialog__footer').clientHeight),
      };
    }""")
    print("\n=== Dialog height breakdown ===")
    print(json.dumps(dh, indent=2, default=str))

    # 检查 body 内部有没有额外的 padding 或 margin 导致空白
    body = page.locator('.rule-editor-dialog .el-dialog__body').first
    body_info = body.evaluate("""el => {
      const children = Array.from(el.children);
      return {
        clientH: el.clientHeight,
        scrollH: el.scrollHeight,
        children_count: children.length,
        children: children.map(c => {
          const cs = window.getComputedStyle(c);
          return {
            tag: c.tagName,
            cls: c.className ? c.className.slice(0, 60) : '',
            clientH: c.clientHeight,
            csH: cs.height,
            clientW: c.clientWidth,
          };
        }),
        // 检查空白
        firstChildTop: children[0] ? children[0].offsetTop : null,
        lastChildBottom: children[children.length-1] ? 
          children[children.length-1].offsetTop + children[children.length-1].clientHeight : null,
      };
    }""")
    print("\n=== Body content breakdown ===")
    print(json.dumps(body_info, indent=2, default=str))

    browser.close()
