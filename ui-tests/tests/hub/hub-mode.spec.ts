import { expect, test } from '@jupyterlab/galata';

/**
 * Hub mode end to end: a JupyterLab spawned with galaxahub's contract
 * (`SHARE_FILES_PUBLIC_ZONE=hub`, the hub API address, the lab token) against
 * the mock hub in `../../mock_hub.py`. Covers what the lab and its extension
 * do - the recipient routes are not mounted, every panel action goes through
 * the hub API - and nothing that needs a real hub or a live tunnel.
 */

const API = '/jupyterlab-share-files-extension/api';
const PANEL = '#jupyterlab-share-files-extension-panel';
const HUB = `http://127.0.0.1:${process.env.MOCK_HUB_PORT || '8765'}`;

/** Call the extension API from the page so the request carries the lab's
 * own XSRF cookie, exactly as the panel does. */
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

async function openPanel(page: any): Promise<void> {
  await page.sidebar.openTab('jupyterlab-share-files-extension-panel');
  await expect(page.locator(PANEL)).toBeVisible();
}

/** Click Refresh and wait for that refresh to land - the button spins
 * while the fetch is in flight. */
async function refreshPanel(page: any): Promise<void> {
  const button = page.locator(`${PANEL} button[title="Refresh"]`);
  await button.click();
  await expect(button).not.toHaveClass(/jp-mod-spinning/);
}

/** How many panel refreshes reached the hub: each one reads the
 * capabilities once (then the items route, once per list). */
async function refreshes(request: any): Promise<number> {
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  return calls.filter((c: any) => c.path.endsWith('/capabilities')).length;
}

/** Set the panel's poll interval through the settings registry, the way
 * the Settings editor does - the panel picks it up on the registry's
 * changed signal. */
async function setPollInterval(page: any, seconds: number): Promise<void> {
  await page.evaluate(async (s: number) => {
    const registry = await (window as any).galata.getPlugin(
      '@jupyterlab/apputils-extension:settings'
    );
    await registry.set(
      'jupyterlab_share_files_extension:plugin',
      'pollIntervalSeconds',
      s
    );
  }, seconds);
}

test.beforeEach(async ({ request }) => {
  await request.post(`${HUB}/_control/reset`);
});

test('api/info reports hub mode and the hub capabilities', async ({ page }) => {
  const { status, data } = await api(page, 'GET', `${API}/info`);
  expect(status).toBe(200);
  expect(data.mode).toBe('hub');
  expect(data.storage_path).toBe('');
  expect(data.hub.available).toBe(true);
  expect(data.hub.allow_share).toBe(true);
  expect(data.hub.serving).toBe(true);
});

test('recipient, static and peer routes are not mounted', async ({ page }) => {
  for (const path of [
    '/jupyterlab-share-files-extension/public/share/AAAAAAAA',
    '/jupyterlab-share-files-extension/public/share/AAAAAAAA/manifest',
    '/jupyterlab-share-files-extension/public/request/AAAAAAAA',
    '/jupyterlab-share-files-extension/static/standalone.html',
    `${API}/connections`
  ]) {
    const { status } = await api(page, 'GET', path);
    expect(status, path).toBe(404);
  }
});

test('the panel hides the peer controls', async ({ page }) => {
  await openPanel(page);
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-sectionTitle`, {
      hasText: 'My Shares'
    })
  ).toBeVisible();
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-sectionTitle`, {
      hasText: 'Connected'
    })
  ).toHaveCount(0);
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-connectInput`)
  ).toBeHidden();
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-dropZone`)
  ).toHaveText('Drag files here to share');
});

test('share rows show staging, ready and refused states from the hub', async ({
  page
}) => {
  await openPanel(page);
  const ready = await api(page, 'POST', `${API}/shares`, {
    name: 'ready-one',
    paths: ['notes/report.csv']
  });
  expect(ready.status).toBe(200);
  expect(ready.data.state).toBe('staging');
  expect(ready.data.link).toMatch(/^http:\/\/localhost:\d+\/s\/MockId_/);
  await api(page, 'POST', `${API}/shares`, {
    name: 'stay-staging-one',
    paths: ['a.txt']
  });
  await api(page, 'POST', `${API}/shares`, {
    name: 'refuse-one',
    paths: ['b.txt']
  });
  await refreshPanel(page);
  const item = (name: string) =>
    page.locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name });
  await expect(
    item('ready-one').locator('.jp-ShareFilesPanel-itemMeta')
  ).toHaveText('1 item');
  await expect(
    item('stay-staging-one').locator('.jp-ShareFilesPanel-itemMeta')
  ).toContainText('staging');
  await expect(
    item('refuse-one').locator('.jp-ShareFilesPanel-itemMeta')
  ).toHaveText('refused: over_cap');
  // the ready row lists the file the hub reports, with nothing to remove
  await item('ready-one').locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(
    item('ready-one').locator('.jp-ShareFilesPanel-entryName')
  ).toHaveText('report.csv');
  await expect(
    item('ready-one').locator('.jp-ShareFilesPanel-entryRemove')
  ).toHaveCount(0);
});

test('create is refused with the hub reason and the New menu greys it out', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/capabilities`, {
    data: { allow_share: false, reason: 'share_not_granted' }
  });
  await openPanel(page);
  await refreshPanel(page);
  const refused = await api(page, 'POST', `${API}/shares`, {
    name: 'x',
    paths: ['a.txt']
  });
  expect(refused.status).toBe(403);
  expect(refused.data.reason).toBe('share_not_granted');
  await page.locator(`${PANEL} button[title="New share or request"]`).click();
  const menu = page.locator('.lm-Menu');
  await expect(
    menu.locator('.lm-Menu-item', { hasText: 'New Share' })
  ).toHaveClass(/lm-mod-disabled/);
  await expect(
    menu.locator('.lm-Menu-item', { hasText: 'New Request' })
  ).not.toHaveClass(/lm-mod-disabled/);
  await page.keyboard.press('Escape');
});

test('a recipient upload is fetched into the workspace through the hub', async ({
  page,
  request
}) => {
  await openPanel(page);
  const created = await api(page, 'POST', `${API}/requests`, { name: 'Inbox' });
  expect(created.status).toBe(200);
  await request.post(`${HUB}/_control/upload`, {
    data: {
      request_id: created.data.id,
      upload_id: 'u1',
      filename: 'report.csv'
    }
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Inbox'
  });
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '1 upload'
  );
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(row.locator('.jp-ShareFilesPanel-entryName')).toHaveText(
    'report.csv'
  );
  await row.locator('button[title="Fetch to current folder"]').click();
  // The proof is the hub's side: the fetch arrived with the lab token and a
  // fresh destination under the file browser's folder. (The success toast
  // is not asserted - a notification extension may render it differently.)
  const fetchCalls = async () => {
    const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
      .calls;
    return calls.filter((c: any) => c.path.endsWith('/uploads/u1/fetch'));
  };
  await expect.poll(async () => (await fetchCalls()).length).toBe(1);
  const fetchCall = (await fetchCalls())[0];
  // galata runs each test in its own folder, which is the file browser's
  // current directory the panel names as the target
  expect(JSON.parse(fetchCall.body).dest).toMatch(/(^|\/)Inbox$/);
  expect(fetchCall.auth).toBe('token test-token');
});

test('the link dialog shows the hub link and the hub serving verdict', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/capabilities`, {
    data: { serving: false, reason: 'sidecar_not_serving' }
  });
  await openPanel(page);
  const created = await api(page, 'POST', `${API}/requests`, {
    name: 'Dialog inbox'
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Dialog inbox'
  });
  await row.locator('button[title="Copy link"]').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('input[readonly]')).toHaveValue(
    created.data.link
  );
  await expect(dialog.locator('[data-reach]')).toContainText('not reachable');
  await expect(dialog.locator('[data-reach]')).toContainText('not serving');
  await expect(
    dialog.locator('a', { hasText: 'Reset Cloudflare' })
  ).toHaveCount(0);
  await dialog.locator('button', { hasText: 'Close' }).click();
});

test('the cloud toggle flips every record and the next one, and a row can be switched on its own', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/capabilities`, {
    data: { public_base_url: 'https://share.example.com' }
  });
  await openPanel(page);
  // born on the hub's own address, whatever the policy prefers
  const first = await api(page, 'POST', `${API}/shares`, {
    name: 'first-one',
    paths: ['a.txt']
  });
  expect(first.data.cloud).toBe(false);
  expect(first.data.link).toMatch(
    new RegExp(`^http://localhost:\\d+/s/${first.data.id}$`)
  );
  const cloud = page.locator(`${PANEL} .jp-ShareFilesPanel-cloudIndicator`);
  await expect(cloud).toHaveAttribute('title', /hub network only/);
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  // the header toggle switches the standing record on and sets the default
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-active/);
  const listed = await api(page, 'GET', `${API}/shares`);
  expect(listed.data.shares[0].cloud).toBe(true);
  expect(listed.data.shares[0].link).toBe(
    `https://share.example.com/s/${first.data.id}`
  );
  const second = await api(page, 'POST', `${API}/requests`, {
    name: 'second-one'
  });
  expect(second.data.cloud).toBe(true);
  expect(second.data.link).toBe(
    `https://share.example.com/s/${second.data.id}`
  );
  // switched-on rows carry the cloud mark
  await refreshPanel(page);
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-itemCloud`)
  ).toHaveCount(2);
  // one row back to the hub network through its context menu
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'second-one'
  });
  await row
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: 'Hub Network Only' })
    .click();
  await expect(row.locator('.jp-ShareFilesPanel-itemCloud')).toHaveCount(0);
  const requests = await api(page, 'GET', `${API}/requests`);
  expect(requests.data.requests[0].cloud).toBe(false);
  expect(requests.data.requests[0].link).toMatch(
    /^http:\/\/localhost:\d+\/s\//
  );
  // its link dialog says so; the switched-on share's does not
  await row.locator('button[title="Copy link"]').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText(
    'Cloudflare sharing is off for this request'
  );
  await dialog.locator('button', { hasText: 'Close' }).click();
  // the header toggle off takes every record back
  await cloud.click();
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  const after = await api(page, 'GET', `${API}/shares`);
  expect(after.data.shares[0].cloud).toBe(false);
});

test('a group policy with Cloudflare off refuses the switch and the toggle stays off', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/policy`, {
    data: { cloudflare_enabled: false }
  });
  await openPanel(page);
  await api(page, 'POST', `${API}/shares`, {
    name: 'local-one',
    paths: ['a.txt']
  });
  const cloud = page.locator(`${PANEL} .jp-ShareFilesPanel-cloudIndicator`);
  await cloud.click();
  // the refusal is relayed as a toast (not asserted - a notification
  // extension may render it differently); the toggle stays off
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  const state = await api(page, 'GET', `${API}/tunnel`);
  expect(state.data.tunnel_active).toBe(false);
  const refused = await api(
    page,
    'POST',
    `${API}/shares/${(await api(page, 'GET', `${API}/shares`)).data.shares[0].id}/cloud`,
    {
      cloud: true
    }
  );
  expect(refused.status).toBe(403);
  expect(refused.data.reason).toBe('cloud_not_configured');
});

test('the panel refreshes on the hub change stream, not on a timer', async ({
  page,
  request
}) => {
  await openPanel(page);
  const created = await api(page, 'POST', `${API}/requests`, { name: 'Inbox' });
  await refreshPanel(page);
  // one hub stream stands for this lab, and the open fetched once
  await expect
    .poll(
      async () =>
        (await (await request.get(`${HUB}/_control/streams`)).json()).open
    )
    .toBe(1);
  await page.waitForTimeout(1000);
  // a short timer that must NOT fire while the stream is up
  await setPollInterval(page, 2);
  const before = await refreshes(request);
  await page.waitForTimeout(5000);
  expect(await refreshes(request)).toBe(before);
  // a change on the hub rings the stream and the panel fetches once - the
  // new upload shows without a click on Refresh
  await request.post(`${HUB}/_control/upload`, {
    data: { request_id: created.data.id, upload_id: 'u1', filename: 'late.csv' }
  });
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Inbox'
  });
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '1 upload'
  );
  await page.waitForTimeout(1000);
  expect(await refreshes(request)).toBe(before + 1);
});

test('an older hub without the stream route puts the panel back on its timer', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/policy`, {
    data: { stream_supported: false }
  });
  // the page loaded (and its stream opened) before the policy flip - load
  // it again so the panel meets the older hub from the start
  await page.goto();
  await openPanel(page);
  await setPollInterval(page, 2);
  const before = await refreshes(request);
  await expect
    .poll(async () => await refreshes(request), { timeout: 10000 })
    .toBeGreaterThan(before + 1);
  await expect
    .poll(
      async () =>
        (await (await request.get(`${HUB}/_control/streams`)).json()).open
    )
    .toBe(0);
});

test('a group policy that requires a password makes the create dialog ask for one', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/capabilities`, {
    data: { password_required: true }
  });
  await openPanel(page);
  await refreshPanel(page);
  await page.locator(`${PANEL} button[title="New share or request"]`).click();
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: 'New Request' })
    .click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText('Your group requires a password');
  const password = dialog.locator('input[placeholder="Password (required)"]');
  // pre-filled with a generated passphrase, so the default create complies
  await expect(password).not.toHaveValue('');
  // an emptied field holds the create back before the hub is asked - the
  // field is required, so the dialog keeps Create disabled
  await dialog.locator('input[placeholder="Name"]').fill('Guarded inbox');
  await password.fill('');
  await expect(dialog.locator('button', { hasText: 'Create' })).toBeDisabled();
  await dialog.locator('button', { hasText: 'Cancel' }).click();
  await expect(dialog).toBeHidden();
  const none = await api(page, 'GET', `${API}/requests`);
  expect(none.data.requests).toEqual([]);
  // the hub itself refuses a bare create with the same reason
  const bare = await api(page, 'POST', `${API}/requests`, { name: 'x' });
  expect(bare.status).toBe(400);
  expect(bare.data.reason).toBe('password_required');
  const kept = await api(page, 'POST', `${API}/requests`, {
    name: 'y',
    password: 'correct horse'
  });
  expect(kept.status).toBe(200);
  expect(kept.data.has_password).toBe(true);
});
