import { expect, test } from '@jupyterlab/galata';

/**
 * A connected peer on ANOTHER origin, under the Content-Security-Policy the
 * test server puts on every page (`default-src 'self'`, as galaxahub does):
 * the panel reads the peer's manifest and files through its own server, so
 * the policy cannot stop it (DEF-PEER-72). The peer is this same lab reached
 * by another host name - 127.0.0.1 instead of localhost - which is another
 * origin to the browser and one server to the test.
 */

const API = '/jupyterlab-share-files-extension/api';
const PANEL = '#jupyterlab-share-files-extension-panel';
const PORT = process.env.JUPYTER_TEST_PORT || '8888';

async function api(
  page: any,
  method: string,
  path: string,
  body?: unknown
): Promise<{ status: number; data: any }> {
  return page.evaluate(
    async ([m, p, b]: [string, string, string | null]) => {
      const init: RequestInit = {
        method: m,
        headers: { 'Content-Type': 'application/json' }
      };
      if (b !== null) {
        init.body = b;
      }
      const r = await fetch(p, init);
      const text = await r.text();
      let data: any = text;
      try {
        data = JSON.parse(text);
      } catch {
        // not JSON
      }
      return { status: r.status, data };
    },
    [method, path, body === undefined ? null : JSON.stringify(body)]
  );
}

/** Set the download limit through the settings registry, the way the
 * Settings editor does - the panel picks it up on the registry's changed
 * signal. */
async function setDownloadLimit(page: any, gb: number): Promise<void> {
  await page.evaluate(async (g: number) => {
    const registry = await (window as any).galata.getPlugin(
      '@jupyterlab/apputils-extension:settings'
    );
    await registry.set(
      'jupyterlab_share_files_extension:plugin',
      'peerDownloadMaxGb',
      g
    );
  }, gb);
}

test('a peer on another origin lists, saves and downloads its files under a default-src self policy, with the download limit setting', async ({
  page,
  tmpPath
}) => {
  const name = `peer-origin-${Date.now()}`;
  await page.contents.uploadContent(
    'peer bytes\n',
    'text',
    `${tmpPath}/peer.txt`
  );
  const share = await api(page, 'POST', `${API}/shares`, {
    name,
    paths: [`${tmpPath}/peer.txt`]
  });
  expect(share.status).toBe(200);
  const link = `http://127.0.0.1:${PORT}/jupyterlab-share-files-extension/public/share/${share.data.id}`;
  // the policy is on the page, and it refuses the browser's own read of the peer
  const csp = await page.evaluate(async () =>
    (await fetch('/lab')).headers.get('content-security-policy')
  );
  expect(csp).toContain("default-src 'self'");
  const direct = await page.evaluate(async (u: string) => {
    try {
      await fetch(`${u}/manifest`);
      return 'allowed';
    } catch {
      return 'refused';
    }
  }, link);
  expect(direct).toBe('refused');
  const conn = await api(page, 'POST', `${API}/connections`, { link });
  expect(conn.status).toBe(200);
  try {
    await page.sidebar.openTab('jupyterlab-share-files-extension-panel');
    const refresh = page.locator(`${PANEL} button[title="Refresh"]`);
    await refresh.click();
    await expect(refresh).not.toHaveClass(/jp-mod-spinning/, {
      timeout: 30000
    });
    // the connection row carries the peer's name and no offline badge, and
    // lists the file - all read through our server
    const connected = page.locator(`${PANEL} .jp-ShareFilesPanel-section`, {
      has: page.locator('.jp-ShareFilesPanel-sectionTitle', {
        hasText: 'Connected'
      })
    });
    const item = connected.locator('.jp-ShareFilesPanel-item', {
      hasText: name
    });
    await expect(item).toBeVisible();
    await expect(item.locator('.jp-ShareFilesPanel-offline')).toHaveCount(0);
    await item.locator('.jp-ShareFilesPanel-itemHeader').click();
    const entry = item.locator('.jp-ShareFilesPanel-entry', {
      hasText: 'peer.txt'
    });
    await expect(entry).toBeVisible();
    const viaServer = await page.evaluate(async (key: string) => {
      const r = await fetch(
        `/jupyterlab-share-files-extension/api/connections/${encodeURIComponent(key)}/manifest`
      );
      return { status: r.status, cache: r.headers.get('cache-control') };
    }, conn.data.key);
    expect(viaServer).toEqual({ status: 200, cache: 'no-store' });
    // the Settings editor's download limit travels with the save, which
    // lands the file through our server's spool
    await setDownloadLimit(page, 2);
    await entry.click({ button: 'right' });
    const saving = page.waitForResponse((r: any) =>
      /\/connections\/[^/]+\/save(\?|$)/.test(r.url())
    );
    await page
      .locator('.lm-Menu .lm-Menu-item', { hasText: 'Save to Current Folder' })
      .click();
    const saveResponse = await saving;
    expect(saveResponse.request().postDataJSON().max_gb).toBe(2);
    expect(saveResponse.status()).toBe(200);
    const saved = (await saveResponse.json()).saved[0];
    expect(await page.contents.fileExists(saved)).toBe(true);
    await page.contents.deleteFile(saved);
    // Download from the entry's menu arrives through our server as well,
    // under the same limit. The browser fetches the anchor itself, so the
    // download event is the only trace of the request
    await entry.click({ button: 'right' });
    const downloading = page.waitForEvent('download');
    await page
      .locator('.lm-Menu .lm-Menu-item', { hasText: 'Download' })
      .click();
    const download = await downloading;
    expect(download.url()).toContain('/connections/');
    expect(download.url()).toContain('&max_gb=2');
    expect(download.suggestedFilename()).toBe('peer.txt');
    // the event fires for a 4xx answer too: only a finished file proves it
    expect(await download.failure()).toBeNull();
  } finally {
    await api(
      page,
      'DELETE',
      `${API}/connections/${encodeURIComponent(conn.data.key)}`
    );
    await api(page, 'DELETE', `${API}/shares/${share.data.id}`);
  }
});
