"""验证修复效果 + 截图"""
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
        dialog_clientH: el.clientHeight,
        body_clientH: body.clientHeight,
        body_csHeight: bcs.height,
        form_clientH: form.clientHeight,
        form_display: fcs.display,
        form_marginLeft: fcs.marginLeft,
        form_marginRight: fcs.marginRight,
        form_maxWidth: fcs.maxWidth,
        form_offsetLeft: form.offsetLeft,
        leftSpace: form.offsetLeft,
        rightSpace: body.clientWidth - form.offsetLeft - form.clientWidth,
        form_item_first_mb: el.querySelector('.el-form-item:first-child') ?
          window.getComputedStyle(el.querySelector('.el-form-item:first-child')).marginBottom : 'N/A',
        form_item_last_mb: el.querySelector('.el-form-item:last-child') ?
          window.getComputedStyle(el.querySelector('.el-form-item:last-child')).marginBottom : 'N/A',
      };
    }""")
    import json
    print(json.dumps(info, indent=2, default=str))

    # 计算空白
    d_height = info['dialog_clientH']
    body_height = info['body_clientH']
    form_height = info['form_clientH']
    left_space = info['leftSpace']
    right_space = info['rightSpace']

    # 理想状态：dialog 高度 ≈ 40(header) + 40(alert) + form_height(342) + 50(footer) ≈ 472
    ideal_dialog = 40 + 40 + form_height + 50
    extra_blank = d_height - ideal_dialog
    print(f"\n=== 结果 ===")
    print(f"Dialog 总高度: {d_height}px")
    print(f"Body 高度: {body_height}px")
    print(f"Form 高度: {form_height}px")
    print(f"Form 水平居中: left={left_space}px, right={right_space}px (应接近相等)")
    print(f"理想 Dialog 高度: ~{ideal_dialog}px")
    print(f"多余空白: {extra_blank}px")
    print(f"Form item margin-bottom: {info['form_item_first_mb']}")
    print(f"最后 item margin-bottom: {info['form_item_last_mb']}")

    page.screenshot(path="d:/Desktop/telnix v2/rule_editor_v2.png", full_page=True)
    print("\nScreenshot saved: rule_editor_v2.png")

    browser.close()
