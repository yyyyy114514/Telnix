"""审查 el-dialog 完整渲染（修复超时问题）"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:5173/#/auto-reply', wait_until='load', timeout=30000)
    page.wait_for_timeout(5000)

    # 找"新建规则"按钮
    print("All buttons:")
    for btn in page.locator('button').all():
        try:
            txt = btn.inner_text(timeout=500).strip()
            if txt:
                print(f"  '{txt}'")
        except:
            pass

    # 直接找 el-button--primary（新建规则）
    primary = page.locator('button.el-button--primary')
    print(f"\nprimary count: {primary.count()}")

    if primary.count() > 0:
        primary.click()
        page.wait_for_timeout(2000)

    # 等 dialog 出现
    page.wait_for_selector('.rule-editor-dialog', timeout=5000)
    d = page.locator('.rule-editor-dialog').first

    info = d.evaluate("""el => {
      function nodeInfo(n) {
        const cs = window.getComputedStyle(n);
        return {
          tag: n.tagName,
          cls: n.className ? n.className.slice(0, 150) : '',
          cs_width: cs.width,
          cs_height: cs.height,
          cs_display: cs.display,
          cs_flex: cs.flex,
          cs_flexDir: cs.flexDirection,
          cs_alignContent: cs.alignContent,
          cs_alignItems: cs.alignItems,
          cs_justContent: cs.justifyContent,
          cs_boxSizing: cs.boxSizing,
          cs_padding: cs.padding,
          cs_margin: cs.margin,
          cs_minH: cs.minHeight,
          cs_maxH: cs.maxHeight,
          clientH: n.clientHeight,
          clientW: n.clientWidth,
        };
      }
      const dialog = el;
      const header = dialog.querySelector('.el-dialog__header');
      const body = dialog.querySelector('.el-dialog__body');
      const footer = dialog.querySelector('.el-dialog__footer');
      const overlay = document.querySelector('.el-overlay');

      return {
        overlay: overlay ? nodeInfo(overlay) : null,
        overlayDialog: overlay ? nodeInfo(overlay.lastChild) : null,
        dialog: nodeInfo(dialog),
        header: header ? nodeInfo(header) : null,
        body: body ? nodeInfo(body) : null,
        footer: footer ? nodeInfo(footer) : null,
      };
    }""")
    import json
    print(json.dumps(info, indent=2, default=str))

    # 也检查 .el-dialog__wrapper
    wrapper = page.locator('.el-dialog__wrapper')
    if wrapper.count() > 0:
        winfo = wrapper.evaluate("""el => {
          const cs = window.getComputedStyle(el);
          return {
            w: cs.width, h: cs.height,
            display: cs.display,
            flexDir: cs.flexDirection,
            alignContent: cs.alignContent,
            alignItems: cs.alignItems,
            justify: cs.justifyContent,
            position: cs.position,
            top: cs.top,
            overflow: cs.overflow,
          };
        }""")
        print("\n=== el-dialog__wrapper ===")
        print(json.dumps(winfo, indent=2))

    browser.close()
