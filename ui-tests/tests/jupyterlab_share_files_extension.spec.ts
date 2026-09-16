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
        'JupyterLab extension jupyterlab_share_files_extension is activated!'
    )
  ).toHaveLength(1);
});

test('the test lab reads no Cloudflare configuration', async ({ request }) => {
  // the developer's own config would let the suite start a real tunnel
  const response = await request.get(
    '/jupyterlab-share-files-extension/api/info'
  );
  expect((await response.json()).tunnel_configured).toBe(false);
});
