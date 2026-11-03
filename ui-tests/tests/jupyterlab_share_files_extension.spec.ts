import { expect, test } from '@jupyterlab/galata';

/**
 * Don't load JupyterLab webpage before running the tests.
 * This is required to ensure we capture all log messages.
 */
test.use({ autoGoto: false });

test('should emit an activation console message', async ({ page }) => {
  const logs: string[] = [];

  page.on('console', message => {
    logs.push(message.text());
  });

  await page.goto();

  expect(
    logs.filter(
      s =>
        s ===
        'JupyterLab extension @stellarshenson/jupyterlab-share-files-extension is activated!'
    )
  ).toHaveLength(1);
});

test('a standalone lab opens no change stream and reads on the timer', async ({
  page
}) => {
  // ACC-HUBM-231-STREAM-SOCKET: the change stream is the hub's
  const API = '/jupyterlab-share-files-extension/api';
  const sockets: string[] = [];
  const reads: string[] = [];
  page.on('websocket', (ws: any) => {
    if (ws.url().includes(`${API}/stream`)) {
      sockets.push(ws.url());
    }
  });
  page.on('request', (r: any) => {
    if (r.method() === 'GET' && r.url().includes(`${API}/shares`)) {
      reads.push(r.url());
    }
  });
  await page.goto();
  await page.sidebar.openTab('jupyterlab-share-files-extension-panel');
  await expect(
    page.locator('#jupyterlab-share-files-extension-panel')
  ).toBeVisible();
  await page.evaluate(async () => {
    const registry = await (window as any).galata.getPlugin(
      '@jupyterlab/apputils-extension:settings'
    );
    await registry.set(
      '@stellarshenson/jupyterlab-share-files-extension:plugin',
      'pollIntervalSeconds',
      2
    );
  });
  const seen = reads.length;
  await page.waitForTimeout(5000);
  expect(sockets).toEqual([]);
  expect(reads.length - seen).toBeGreaterThanOrEqual(2);
});

test('the test lab reads no Cloudflare configuration', async ({ request }) => {
  // the developer's own config would let the suite start a real tunnel
  const response = await request.get(
    '/jupyterlab-share-files-extension/api/info'
  );
  expect((await response.json()).tunnel_configured).toBe(false);
});
