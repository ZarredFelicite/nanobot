import { chromium } from 'playwright';

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();

try {
  await page.goto('http://127.0.0.1:4096/', { waitUntil: 'domcontentloaded', timeout: 120000 });

  await page.getByText('Nanobot').waitFor({ timeout: 30000 });
  const textarea = page.getByPlaceholder('Message nanobot...');
  await textarea.waitFor({ timeout: 30000 });

  await textarea.fill('Reply with the exact phrase PLAYWRIGHT_OK and one short sentence after it. No tools.');
  await page.getByLabel('Send message').click();

  await page.getByText(/PLAYWRIGHT_OK/).waitFor({ timeout: 120000 });
  await page.screenshot({ path: '/tmp/nanobot-webui-playwright.png', fullPage: true });
  console.log('playwright_webui_ok');
} finally {
  await browser.close();
}
