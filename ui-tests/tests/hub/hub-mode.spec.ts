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
// the mock's tunnel base: a second origin on the same loopback port
const TUNNEL = `http://localhost:${process.env.MOCK_HUB_PORT || '8765'}`;
const CLOUD = `${PANEL} .jp-ShareFilesPanel-cloudIndicator`;

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

/** How many notifications of `type` the page raised whose text holds `text`
 * - read from JupyterLab's notification manager, not the rendered toast. */
async function notes(page: any, type: string, text: string): Promise<number> {
  return (await page.notifications).filter(
    (n: any) => n.type === type && n.message.includes(text)
  ).length;
}

/** The hub stops answering, as the panel sees it: api/info reports the hub
 * unavailable and the lists answer 502 hub_unavailable. */
async function hubOutage(page: any): Promise<void> {
  const down = { error: 'could not reach the hub', reason: 'hub_unavailable' };
  await page.route(`**${API}/info*`, (route: any) =>
    route.fulfill({
      json: {
        mode: 'hub',
        storage_path: '',
        shares_subdir: '',
        requests_subdir: '',
        tunnel_configured: false,
        tunnel_active: false,
        hub: { available: false, ...down }
      }
    })
  );
  for (const list of ['shares', 'requests']) {
    await page.route(`**${API}/${list}*`, (route: any) =>
      route.fulfill({ status: 502, json: down })
    );
  }
}

test.beforeEach(async ({ request }) => {
  await request.post(`${HUB}/_control/reset`);
  // the lab keeps the cloud default in its config file from test to test;
  // with no record on the hub this stores the default alone
  await request.post(`${API}/tunnel`, { data: { active: false } });
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
  // the refused row says why in one line; the hover adds the slug
  const refusedMeta = item('refuse-one').locator(
    '.jp-ShareFilesPanel-itemMeta'
  );
  const overCap = 'The files are larger than your group allows for one share.';
  await expect(refusedMeta).toHaveText(overCap);
  await expect(refusedMeta).toHaveAttribute(
    'title',
    `${overCap}\nrefused: over_cap`
  );
  expect((await refusedMeta.boundingBox())!.height).toBeLessThanOrEqual(24);
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

test('the link dialog opens the link itself, whatever the hub serving verdict', async ({
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
  // only a tunnel link is probed; the mock tunnel registers at once
  await api(page, 'POST', `${API}/requests/${created.data.id}/cloud`, {
    cloud: true
  });
  const tunnel = `http://localhost:${process.env.MOCK_HUB_PORT || '8765'}`;
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Dialog inbox'
  });
  await row.locator('button[title="Copy link"]').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('input[readonly]')).toHaveValue(
    `${tunnel}/s/${created.data.id}`
  );
  await expect(dialog).not.toContainText("hub's network only");
  // the recipient page answers, so the link is reachable while the hub's
  // cached verdict still reads serving false
  await expect(dialog.locator('[data-reach]')).toHaveText(
    '✓  Link is reachable'
  );
  await expect(
    dialog.locator('a', { hasText: 'Reset Cloudflare' })
  ).toHaveCount(0);
  await dialog.locator('button', { hasText: 'Close' }).click();
  await expect(dialog).toBeHidden();
  // the page fails: the dialog opened again checks again and says what
  // answered, without repeating the link it shows
  await request.post(`${HUB}/_control/page`, { data: { status: 503 } });
  await row.locator('button[title="Copy link"]').click();
  await expect(dialog.locator('[data-reach]')).toHaveText(
    '✗  Link is not reachable - the lab server got HTTP 503'
  );
  await dialog.locator('button', { hasText: 'Close' }).click();
});

test('the cloud toggle flips every record and the next one, and a row can be switched on its own', async ({
  page,
  request
}) => {
  await openPanel(page);
  // born on the hub's own address
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
  expect(listed.data.shares[0].link).toBe(`${TUNNEL}/s/${first.data.id}`);
  const second = await api(page, 'POST', `${API}/requests`, {
    name: 'second-one'
  });
  expect(second.data.cloud).toBe(true);
  expect(second.data.link).toBe(`${TUNNEL}/s/${second.data.id}`);
  // every record is on, and no row carries a cloud mark - the header icon
  // alone shows Cloudflare on
  await refreshPanel(page);
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: 'second-one' })
  ).toBeVisible();
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-itemCloud`)
  ).toHaveCount(0);
  await expect(cloud).toHaveClass(/jp-mod-active/);
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
  await expect
    .poll(
      async () => (await api(page, 'GET', `${API}/requests`)).data.requests[0]
    )
    .toMatchObject({ cloud: false });
  const requests = await api(page, 'GET', `${API}/requests`);
  expect(requests.data.requests[0].cloud).toBe(false);
  expect(requests.data.requests[0].link).toMatch(
    /^http:\/\/localhost:\d+\/s\//
  );
  // its link dialog says so; the switched-on share's does not
  await row.locator('button[title="Copy link"]').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText("Link works on the hub's network only");
  // the lab cannot open the hub's own address, so it is not probed
  await expect(dialog.locator('[data-reach]')).toHaveCount(0);
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

test('a group policy that requires a password keeps the change dialog from removing it', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/capabilities`, {
    data: { password_required: true }
  });
  await api(page, 'POST', `${API}/requests`, {
    name: 'Locked inbox',
    password: 'correct horse'
  });
  await openPanel(page);
  await refreshPanel(page);
  await page
    .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: 'Locked inbox' })
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: 'Change Password' })
    .click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toBeVisible();
  // no removal on offer, and an emptied field keeps Save disabled
  await expect(dialog).not.toContainText('Leave empty');
  const password = dialog.locator('input[placeholder="Password (required)"]');
  await expect(password).toHaveValue('correct horse');
  await password.fill('');
  await expect(dialog.locator('button', { hasText: 'Save' })).toBeDisabled();
  await dialog.locator('button', { hasText: 'Cancel' }).click();
  await expect(dialog).toBeHidden();
  const listed = await api(page, 'GET', `${API}/requests`);
  expect(listed.data.requests[0].has_password).toBe(true);
});

test('switching Cloudflare on pulses the icon until the hub confirms, then every open panel shows the tunnel link', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/tunnel`, { data: { delay: 3 } });
  const created = await api(page, 'POST', `${API}/shares`, {
    name: 'pulse-one',
    paths: ['a.txt']
  });
  await openPanel(page);
  // a second page of the same lab, open before the switch
  const other = await page.context().newPage();
  await other.goto(page.url());
  const tab = other.locator(
    '.lm-TabBar.jp-SideBar li.lm-TabBar-tab[data-id="jupyterlab-share-files-extension-panel"]'
  );
  await tab.waitFor({ timeout: 60000 });
  if (
    !(await tab.evaluate((el: Element) =>
      el.classList.contains('lm-mod-current')
    ))
  ) {
    await tab.click();
  }
  const item = (p: any) =>
    p.locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: 'pulse-one' });
  await expect(item(other)).toBeVisible();
  const cloud = page.locator(CLOUD);
  const otherCloud = other.locator(CLOUD);
  await cloud.click();
  // no on state before the hub confirms; the other page reads the wait
  // from the lab
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  await expect(otherCloud).toHaveClass(/jp-mod-connecting/);
  // the tunnel registers without a ring; the lab's next check confirms it
  // and rings both panels
  await expect(cloud).toHaveClass(/jp-mod-active/, { timeout: 15000 });
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/);
  await expect(otherCloud).toHaveClass(/jp-mod-active/);
  // neither page pressed Refresh
  for (const p of [page, other]) {
    await item(p).locator('button[title="Copy link"]').click();
    const dialog = p.locator('.jp-Dialog');
    await expect(dialog.locator('input[readonly]')).toHaveValue(
      `${TUNNEL}/s/${created.data.id}`
    );
    await dialog.locator('button', { hasText: 'Close' }).click();
  }
  await other.close();
});

test('a switch-on the hub does not confirm goes back off with a warning', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/tunnel`, { data: { registers: false } });
  await api(page, 'POST', `${API}/shares`, {
    name: 'lonely-one',
    paths: ['a.txt']
  });
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  // SHARE_FILES_CLOUD_CONFIRM_SECONDS bounds the wait to 10 s in this suite
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/, { timeout: 20000 });
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  await expect
    .poll(() =>
      notes(page, 'warning', 'did not bring up its Cloudflare address')
    )
    .toBe(1);
  const state = await api(page, 'GET', `${API}/tunnel`);
  expect(state.data.tunnel_active).toBe(false);
  expect(state.data.tunnel_waiting).toBe(false);
  expect(state.data.tunnel_reason).toBe('cloud_not_confirmed');
  const shares = await api(page, 'GET', `${API}/shares`);
  expect(shares.data.shares[0].cloud).toBe(false);
  // the reason stays in the icon's tooltip after the toast closes
  await expect(cloud).toHaveAttribute(
    'title',
    'Cloudflare not confirmed - click to switch on\nHub did not bring up its Cloudflare address'
  );
});

test('a switch back off the hub answers with an error leaves the header on, as the records stayed on', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/tunnel`, { data: { registers: false } });
  await request.post(`${HUB}/_control/cloudoff`, { data: { status: 500 } });
  await api(page, 'POST', `${API}/shares`, {
    name: 'stuck-one',
    paths: ['a.txt']
  });
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  // SHARE_FILES_CLOUD_CONFIRM_SECONDS bounds the wait to 10 s in this suite
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/, { timeout: 20000 });
  // the record and the default stayed on at the hub, so the header reads on:
  // it never claims off while the hub keeps the records switched on
  await expect(cloud).toHaveClass(/jp-mod-active/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'true');
  await expect(cloud).toHaveAttribute(
    'title',
    'Cloudflare sharing on - click to switch off\nThe hub did not switch Cloudflare off'
  );
  await expect
    .poll(() =>
      notes(
        page,
        'warning',
        'The hub did not switch Cloudflare off - those links may still be on Cloudflare.'
      )
    )
    .toBe(1);
  expect(await notes(page, 'warning', 'The hub could not be reached')).toBe(0);
  const state = await api(page, 'GET', `${API}/tunnel`);
  expect(state.data.tunnel_active).toBe(true);
  expect(state.data.tunnel_reason).toBe('cloud_not_switched_off');
  const shares = await api(page, 'GET', `${API}/shares`);
  expect(shares.data.shares[0].cloud).toBe(true);
});

test('during the wait the cloud icon reads pressed and a click ends the wait and switches off', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/tunnel`, { data: { registers: false } });
  const share = await api(page, 'POST', `${API}/shares`, {
    name: 'waiting-one',
    paths: ['a.txt']
  });
  await api(page, 'POST', `${API}/requests`, { name: 'other-one' });
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  // one row switched on while the header default is off
  await api(page, 'POST', `${API}/shares/${share.data.id}/cloud`, {
    cloud: true
  });
  await refreshPanel(page);
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'mixed');
  await expect(cloud).toHaveAttribute(
    'title',
    'Switching Cloudflare on - waiting for the hub\nClick to end the wait and switch it off'
  );
  await cloud.click();
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/);
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'false');
  const state = await api(page, 'GET', `${API}/tunnel`);
  expect(state.data.tunnel_waiting).toBe(false);
  expect(state.data.tunnel_active).toBe(false);
  const shares = await api(page, 'GET', `${API}/shares`);
  const requests = await api(page, 'GET', `${API}/requests`);
  expect(shares.data.shares[0].cloud).toBe(false);
  expect(requests.data.requests[0].cloud).toBe(false);
});

test('during the wait the link dialog says a switched-on link moves to the Cloudflare hostname', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/tunnel`, { data: { registers: false } });
  const share = await api(page, 'POST', `${API}/shares`, {
    name: 'moving-one',
    paths: ['a.txt']
  });
  await api(page, 'POST', `${API}/requests`, { name: 'staying-one' });
  await openPanel(page);
  await refreshPanel(page);
  await api(page, 'POST', `${API}/shares/${share.data.id}/cloud`, {
    cloud: true
  });
  await refreshPanel(page);
  await expect(page.locator(CLOUD)).toHaveClass(/jp-mod-connecting/);
  const dialog = page.locator('.jp-Dialog');
  const open = async (name: string) => {
    await page
      .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name })
      .locator('button[title="Copy link"]')
      .click();
    await expect(dialog).toBeVisible();
  };
  await open('moving-one');
  await expect(dialog).toContainText(
    "Works on the hub's network only - moves to the Cloudflare hostname once the hub confirms"
  );
  await dialog.locator('button', { hasText: 'Close' }).click();
  await expect(dialog).toBeHidden();
  // a record that is off does not move
  await open('staying-one');
  await expect(dialog).toContainText("Link works on the hub's network only");
  await dialog.locator('button', { hasText: 'Close' }).click();
  await api(page, 'POST', `${API}/tunnel`, { active: false });
});

test('switching Cloudflare off shows the busy icon while the lab switches the records', async ({
  page
}) => {
  await api(page, 'POST', `${API}/shares`, {
    name: 'busy-one',
    paths: ['a.txt']
  });
  await api(page, 'POST', `${API}/tunnel`, { active: true });
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await expect(cloud).toHaveClass(/jp-mod-active/);
  // hold the switch-off in the browser so its in-flight state stays visible
  let release: () => void = () => undefined;
  const held = new Promise<void>(resolve => (release = resolve));
  await page.route(`**${API}/tunnel*`, async (route: any) => {
    await held;
    await route.continue();
  });
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  await expect(cloud).toHaveAttribute(
    'title',
    'Switching Cloudflare sharing off'
  );
  release();
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/);
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  await page.unroute(`**${API}/tunnel*`);
});

test('a policy refusal on a Cloudflare switch is a warning', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/policy`, {
    data: { cloudflare_enabled: false }
  });
  await api(page, 'POST', `${API}/requests`, { name: 'policy-one' });
  await openPanel(page);
  await refreshPanel(page);
  const refusal = 'Your group policy has Cloudflare turned off';
  await page.locator(CLOUD).click();
  await expect.poll(() => notes(page, 'warning', refusal)).toBe(1);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'policy-one'
  });
  await row
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: 'Share Through Cloudflare' })
    .click();
  await expect.poll(() => notes(page, 'warning', refusal)).toBe(2);
  expect(await notes(page, 'error', '')).toBe(0);
});

test('every cloud icon tooltip is two lines at most', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/tunnel`, { data: { registers: false } });
  await api(page, 'POST', `${API}/shares`, {
    name: 'tip-one',
    paths: ['a.txt']
  });
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  const titles: string[] = [];
  const read = async () => titles.push((await cloud.getAttribute('title'))!);
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  await read(); // off
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  await read(); // waiting for the hub
  await cloud.click(); // off again, ending the wait
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/);
  await request.post(`${HUB}/_control/tunnel`, { data: { registers: true } });
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-active/);
  await read(); // on
  await hubOutage(page);
  await refreshPanel(page);
  await expect(cloud).toHaveClass(/jp-mod-unreachable/);
  await read(); // hub unreachable
  expect(titles).toHaveLength(4);
  for (const title of titles) {
    expect(title.split('\n').length, title).toBeLessThanOrEqual(2);
  }
});

test('a clicked Refresh during a hub outage warns once and the icon shows the hub unreachable', async ({
  page,
  request
}) => {
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await expect(cloud).not.toHaveClass(/jp-mod-unreachable/);
  await hubOutage(page);
  await refreshPanel(page);
  await expect
    .poll(() => notes(page, 'warning', 'The hub could not be reached'))
    .toBe(1);
  // its own look: not the off silhouette, not on
  await expect(cloud).toHaveClass(/jp-mod-unreachable/);
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  await expect(cloud).toHaveAttribute('title', /^Hub unavailable/);
  // and its own colour: the icon paints in the state colour, not the grey
  expect(
    await cloud.locator('svg').evaluate((el: SVGElement) => {
      return getComputedStyle(el).fill;
    })
  ).toBe(await cloud.evaluate((el: HTMLElement) => getComputedStyle(el).color));
  // a background refresh (a ring from the hub) stays quiet
  await request.post(`${HUB}/_control/nudge`);
  await page.waitForTimeout(1500);
  expect(await notes(page, 'warning', 'The hub could not be reached')).toBe(1);
});

test('the keyboard switches the cloud icon and opens a row context menu', async ({
  page,
  request
}) => {
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await expect(cloud).toHaveAttribute('role', 'button');
  await expect(cloud).toHaveAttribute('aria-label', 'Cloudflare sharing');
  await expect(cloud).toHaveAttribute('aria-pressed', 'false');
  // the New button sits just before the icon in the header
  await page.locator(`${PANEL} button[title="New share or request"]`).focus();
  await page.keyboard.press('Tab');
  await expect(cloud).toBeFocused();
  await expect(cloud).toHaveCSS('outline-style', 'solid');
  await page.keyboard.press('Enter');
  await expect(cloud).toHaveClass(/jp-mod-active/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'true');
  await page.keyboard.press('Space');
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'false');

  const created = await api(page, 'POST', `${API}/requests`, {
    name: 'keyboard-one'
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'keyboard-one'
  });
  const header = row.locator('.jp-ShareFilesPanel-itemHeader');
  // Refresh, focused by its click, is the last header control; the row
  // header is the next stop
  await page.keyboard.press('Tab');
  await expect(header).toBeFocused();
  // a ring from the hub renders the rows again; the focus stays on the row
  await request.post(`${HUB}/_control/upload`, {
    data: { request_id: created.data.id, upload_id: 'u1', filename: 'k.csv' }
  });
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '1 upload'
  );
  await expect(header).toBeFocused();
  // Shift+F10 opens the row's menu; the arrow keys and Enter run the switch
  await page.keyboard.press('Shift+F10');
  const on = page.locator('.lm-Menu .lm-Menu-item', {
    hasText: 'Share Through Cloudflare'
  });
  await expect(on).toBeVisible();
  for (let i = 0; i < 8; i++) {
    if (await on.evaluate(el => el.classList.contains('lm-mod-active'))) {
      break;
    }
    await page.keyboard.press('ArrowDown');
  }
  await expect(on).toHaveClass(/lm-mod-active/);
  await page.keyboard.press('Enter');
  await expect
    .poll(
      async () => (await api(page, 'GET', `${API}/requests`)).data.requests[0]
    )
    .toMatchObject({ cloud: true });
  // the ContextMenu key opens the same menu, now offering the way back
  await refreshPanel(page);
  await header.focus();
  await page.keyboard.press('ContextMenu');
  const off = page.locator('.lm-Menu .lm-Menu-item', {
    hasText: 'Hub Network Only'
  });
  await expect(off).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(off).toBeHidden();
});

test('a record without a password shows the rule and keeps Save disabled', async ({
  page,
  request
}) => {
  // the record predates the policy: the hub refuses a bare create once the
  // policy is on, so it is created first
  const made = await api(page, 'POST', `${API}/requests`, {
    name: 'Open inbox'
  });
  // the mock hub numbers ids from its reset, so this record carries the id
  // an earlier test gave a password: clear it while the policy still allows
  await api(page, 'POST', `${API}/requests/${made.data.id}/password`, {
    password: ''
  });
  await request.post(`${HUB}/_control/capabilities`, {
    data: { password_required: true }
  });
  await openPanel(page);
  await refreshPanel(page);
  await page
    .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: 'Open inbox' })
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: 'Set Password' })
    .click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toBeVisible();
  // the rule is readable in the dialog, not only in the placeholder, and an
  // untouched empty field holds Save back before the dialog closes
  await expect(dialog).toContainText('Your group requires a password');
  await expect(dialog).not.toContainText('Leave empty');
  const save = dialog.locator('button', { hasText: 'Save' });
  await expect(save).toBeDisabled();
  await dialog.locator('input[placeholder="Password (required)"]').fill('nine');
  await expect(save).toBeEnabled();
  await dialog.locator('button', { hasText: 'Cancel' }).click();
  await expect(dialog).toBeHidden();
  const listed = await api(page, 'GET', `${API}/requests`);
  expect(listed.data.requests[0].has_password).toBe(false);
});
