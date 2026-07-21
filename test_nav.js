const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 375, height: 812 },
    userAgent: 'Mozilla/5.0 (Linux; Android 10; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 SamsungBrowser/23.0 Mobile Safari/537.36'
  });
  const page = await context.newPage();

  const problems = [];

  try {
    await page.goto('http://localhost:5000/', { timeout: 15000, waitUntil: 'networkidle' });
    
    // Screenshot before clicking menu
    await page.screenshot({ path: 'D:/teslausb/a7z/test_before_menu.png', fullPage: false });
    console.log('Screenshot: before menu saved');

    // Check initial nav state
    const menuInitial = await page.evaluate(() => {
      const m = document.getElementById('navMenu');
      return {
        display: window.getComputedStyle(m).display,
        height: m.offsetHeight,
        zIndex: window.getComputedStyle(m).zIndex,
        visible: m.offsetHeight > 0,
        children: m.children.length
      };
    });
    console.log('Initial menu state:', JSON.stringify(menuInitial));

    // Check CSS applied
    const navStyles = await page.evaluate(() => {
      const m = document.getElementById('navMenu');
      const cs = window.getComputedStyle(m);
      return {
        display: cs.display,
        position: cs.position,
        zIndex: cs.zIndex,
        width: cs.width,
        height: cs.height,
        overflow: cs.overflow,
        overflowY: cs.overflowY,
        clipPath: cs.clipPath,
        contain: cs.contain,
        backgroundColor: cs.backgroundColor,
        left: cs.left,
        top: cs.top,
        transform: cs.transform,
        visibility: cs.visibility,
        opacity: cs.opacity,
        paddingTop: cs.paddingTop,
        flexDirection: cs.flexDirection
      };
    });
    console.log('Nav-menu computed styles:', JSON.stringify(navStyles));

    // Check the nav container
    const navContainerStyles = await page.evaluate(() => {
      const n = document.querySelector('.nav');
      const ni = document.querySelector('.nav-inner');
      return {
        nav_overlow: window.getComputedStyle(n).overflow,
        nav_contain: window.getComputedStyle(n).contain,
        nav_zIndex: window.getComputedStyle(n).zIndex,
        navClipPath: window.getComputedStyle(n).clipPath,
        navInner_height: window.getComputedStyle(ni).height,
        navInner_overflow: window.getComputedStyle(ni).overflow,
      };
    });
    console.log('Nav container styles:', JSON.stringify(navContainerStyles));

    // Click menu button
    const menuBtn = await page.$('.menu-btn');
    if (menuBtn) {
      console.log('Menu button found, clicking...');
      await menuBtn.click();
      await page.waitForTimeout(500);
    } else {
      problems.push('Menu button NOT FOUND');
    }

    // Check after click
    const menuAfter = await page.evaluate(() => {
      const m = document.getElementById('navMenu');
      return {
        display: window.getComputedStyle(m).display,
        height: m.offsetHeight,
        visible: m.offsetHeight > 0,
        openClass: m.classList.contains('open'),
        children: m.children.length,
        firstChildText: m.children[0]?.textContent?.trim() || 'NONE'
      };
    });
    console.log('After click menu state:', JSON.stringify(menuAfter));

    // Check overlay
    const overlayState = await page.evaluate(() => {
      const o = document.getElementById('overlay');
      return {
        showClass: o.classList.contains('show'),
        display: window.getComputedStyle(o).display,
        zIndex: window.getComputedStyle(o).zIndex
      };
    });
    console.log('Overlay state:', JSON.stringify(overlayState));

    // List all visible nav-link texts
    const linkTexts = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('.nav-link')).map(el => ({
        text: el.textContent.trim(),
        visible: el.offsetHeight > 0,
        color: window.getComputedStyle(el).color,
        bg: window.getComputedStyle(el).backgroundColor
      }));
    });
    console.log('Nav links:', JSON.stringify(linkTexts));

    // Screenshot after menu open
    await page.screenshot({ path: 'D:/teslausb/a7z/test_after_menu.png', fullPage: false });
    console.log('Screenshot: after menu saved');

  } catch (e) {
    console.error('Error:', e.message);
    problems.push('EXCEPTION: ' + e.message);
  }

  console.log('\n=== PROBLEMS FOUND ===');
  if (problems.length === 0) {
    console.log('No problems detected.');
  } else {
    problems.forEach(p => console.log('  - ' + p));
  }

  await browser.close();
})();
