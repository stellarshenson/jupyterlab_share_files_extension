import { expect, test } from '@jupyterlab/galata';

import { dragOnto } from '../helpers/drag';

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
  // the hub's own tunnel verdicts ride on api/info, and the fields the
  // confirmation wait reported are gone with it
  expect(data.tunnel_available).toBe(true);
  expect(data.tunnel_ready).toBe(false);
  expect(data.tunnel_default).toBe(false);
  expect(data.tunnel_waiting).toBeUndefined();
  expect(data.tunnel_reason).toBeUndefined();
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
  // ACC-HUBM-169: the staging row carries the indicator inside the width the
  // row already has, and the ready and refused rows carry none
  const spinner = '.jp-ShareFilesPanel-itemMeta .jp-ShareFilesPanel-spinner';
  await expect(item('stay-staging-one').locator(spinner)).toBeVisible();
  await expect(item('ready-one').locator(spinner)).toHaveCount(0);
  await expect(item('refuse-one').locator(spinner)).toHaveCount(0);
  expect(
    (await item('stay-staging-one').boundingBox())!.height
  ).toBeLessThanOrEqual((await item('ready-one').boundingBox())!.height);
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
  // the ready row lists the file the hub reports
  await item('ready-one').locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(
    item('ready-one').locator('.jp-ShareFilesPanel-entryName')
  ).toHaveText('report.csv');
});

test('create is refused with the hub reason, and the refusal is a sentence the owner reads', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/capabilities`, {
    data: { allow_request: false, reason: 'request_not_granted' }
  });
  await openPanel(page);
  await refreshPanel(page);
  const refused = await api(page, 'POST', `${API}/requests`, { name: 'x' });
  expect(refused.status).toBe(403);
  expect(refused.data.reason).toBe('request_not_granted');
  // DEF-PANEL-86: New Request stays clickable and answers in words. A greyed
  // entry carried its reason in a caption Lumino never renders, so the owner
  // met a dead entry that said nothing.
  await page.locator(`${PANEL} button[title="New share or request"]`).click();
  const menu = page.locator('.lm-Menu');
  const request_ = menu.locator('.lm-Menu-item', { hasText: 'New Request' });
  await expect(request_).not.toHaveClass(/lm-mod-disabled/);
  await request_.click();
  await expect(page.locator('.jp-toast-message')).toContainText(
    'does not allow requesting files'
  );
});

test('New Share creates an empty share and a dropped file fills it', async ({
  page
}) => {
  // ACC-HUBM-163, ACC-DRAG-156: the hub creates a share with no files and
  // takes files afterwards. The hub's row says nothing while an add runs,
  // so the panel's row says it, and the file arrives with no click.
  await page.contents.uploadContent('dropped', 'text', 'hub-drag.txt');
  await openPanel(page);
  await page.locator(`${PANEL} button[title="New share or request"]`).click();
  await page.locator('.lm-Menu-item', { hasText: 'New Share' }).click();
  const dialog = page.locator('.jp-Dialog');
  await dialog.locator('input[placeholder="Name"]').fill('filled-later');
  await dialog.locator('button.jp-mod-accept').click();
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'filled-later'
  });
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '0 items'
  );
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(row.locator('.jp-ShareFilesPanel-empty')).toHaveText(
    'Empty - drag files onto this share to add'
  );

  await dragOnto(page, 'hub-drag.txt', row);

  await expect(row.locator('.jp-ShareFilesPanel-entryName')).toHaveText(
    'hub-drag.txt'
  );
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '1 item'
  );
});

test('a selection of files and a folder is added to a hub share in one drop', async ({
  page
}) => {
  // ACC-EDIT-167: one drop is one add call, and every item in it lands
  await page.contents.uploadContent('a', 'text', 'multi-a.txt');
  await page.contents.uploadContent('b', 'text', 'multi-b.txt');
  await page.contents.uploadContent('c', 'text', 'multidir/in.txt');
  await openPanel(page);
  await api(page, 'POST', `${API}/shares`, { name: 'takes-many', paths: [] });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'takes-many'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();

  await dragOnto(page, 'multi-a.txt', row, ['multi-b.txt', 'multidir']);

  await expect(row.locator('.jp-ShareFilesPanel-entryName')).toHaveText([
    'multi-a.txt',
    'multi-b.txt',
    'multidir/'
  ]);
});

test('an add the hub refuses shows the hub reason on the row', async ({
  page,
  request
}) => {
  await request.post(`${HUB}/_control/add`, { data: { seconds: 3 } });
  // the hub records a refused add on the row as last_add, with its reason
  await page.contents.uploadContent('x', 'text', 'refuse-add.txt');
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'drops-adds',
    paths: []
  });
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'drops-adds'
  });
  await refreshPanel(page);
  await api(page, 'POST', `${API}/shares/${made.data.id}/items`, {
    paths: ['refuse-add.txt']
  });
  // while the hub copies, the row carries the percentage the hub reports
  await refreshPanel(page);
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    /adding 50%/
  );
  // the hub rings on a refused add, so the row says it with no click
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '0 items - add refused'
  );
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(
    row.locator('.jp-ShareFilesPanel-empty', {
      hasText: 'refused the last add'
    })
  ).toContainText('larger than your group allows');
});

test('a file copied in the file browser is pasted into a hub share by keyboard', async ({
  page
}) => {
  // the keyboard's route to adding: the drop has none
  await page.contents.uploadContent('pasted', 'text', 'hub-paste.txt');
  await openPanel(page);
  await api(page, 'POST', `${API}/shares`, { name: 'pasted-into', paths: [] });
  await refreshPanel(page);
  await page.filebrowser.openHomeDirectory();
  await page.filebrowser.revealFileInBrowser('hub-paste.txt');
  await page
    .getByRole('region', { name: 'File Browser Section' })
    .getByRole('listitem', { name: /^Name: hub-paste.txt/ })
    .click();
  // a cut: the hub copies after its answer, so the original must stay
  await page.keyboard.press('ControlOrMeta+x');
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'pasted-into'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').focus();
  await page.keyboard.press('ContextMenu');
  await page.locator('.lm-Menu-item', { hasText: 'Paste' }).click();
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '1 item'
  );
  // DEF-PANEL-97: the panel says it was a copy, and nothing was deleted
  await expect
    .poll(() => notes(page, 'info', 'pasted as a copy'))
    .toBeGreaterThan(0);
  expect(await page.contents.fileExists('hub-paste.txt')).toBe(true);
});

test('a file in a hub share is renamed with F2, moved by a drag and removed', async ({
  page,
  request
}) => {
  // ACC-EDIT-165, 166, 168
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'edited',
    paths: ['a.txt', 'b.txt', 'sub']
  });
  const id = made.data.id;
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'edited'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const entry = (name: string) =>
    row.locator('.jp-ShareFilesPanel-entry', { hasText: name });
  const hubNames = async () => {
    const items = (await (await request.get(`${HUB}/_control/items`)).json())
      .items;
    return items
      .find((i: any) => i.id === id)
      .files.map((f: any) => f.name)
      .sort();
  };

  // F2 renames in place; Escape leaves the old name
  await entry('a.txt').focus();
  await page.keyboard.press('F2');
  const field = row.locator('.jp-ShareFilesPanel-entryRename');
  await field.fill('never.txt');
  await page.keyboard.press('Escape');
  await expect(entry('a.txt')).toBeFocused();
  // the move syntax is on screen while the field is open
  await page.keyboard.press('F2');
  await expect(row.locator('#jp-ShareFilesPanel-renameHint')).toContainText(
    'folder/name moves it'
  );
  await page.keyboard.press('Escape');
  await expect(row.locator('#jp-ShareFilesPanel-renameHint')).toHaveCount(0);
  // DEF-PANEL-96: a click that closes an unchanged field still lands
  await entry('a.txt').focus();
  await page.keyboard.press('F2');
  await expect(field).toBeVisible();
  const before = await refreshes(request);
  await page.locator(`${PANEL} button[title="Toggle filter"]`).click();
  await expect(field).toHaveCount(0);
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-filterInput`)
  ).toBeVisible();
  // a click into another field closes the rename field and keeps the focus
  await entry('a.txt').focus();
  await page.keyboard.press('F2');
  await expect(field).toBeVisible();
  await page.locator(`${PANEL} .jp-ShareFilesPanel-filterInput`).click();
  await expect(field).toHaveCount(0);
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-filterInput`)
  ).toBeFocused();
  await page.locator(`${PANEL} button[title="Toggle filter"]`).click();
  expect(await refreshes(request)).toBeGreaterThan(before);
  // a sibling's name is refused in words and the row keeps its name
  await entry('a.txt').focus();
  await page.keyboard.press('F2');
  await field.fill('b.txt');
  await page.keyboard.press('Enter');
  await expect
    .poll(() => notes(page, 'warning', 'already holds an entry'))
    .toBeGreaterThan(0);
  await expect(entry('a.txt')).toBeVisible();
  // Enter commits, and the focus follows the renamed row
  await entry('a.txt').focus();
  await page.keyboard.press('F2');
  await field.fill('renamed.txt');
  await page.keyboard.press('Enter');
  await expect(entry('renamed.txt')).toBeFocused();
  expect(await hubNames()).toEqual(['b.txt', 'renamed.txt', 'sub/inside.txt']);

  // a drag onto a folder row of the same share moves the file into it
  const from = (await entry('b.txt').boundingBox())!;
  const to = (await entry('sub/').boundingBox())!;
  await page.mouse.move(from.x + 40, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(from.x + 55, from.y + from.height / 2);
  await page.mouse.move(to.x + 40, to.y + to.height / 2, { steps: 10 });
  await expect(entry('sub/')).toHaveClass(/jp-mod-dropTarget/);
  await page.mouse.up();
  await expect(entry('b.txt')).toHaveCount(0);
  expect(await hubNames()).toContain('sub/b.txt');

  // remove asks once and names the file
  await entry('renamed.txt').focus();
  await page.keyboard.press('Delete');
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText('Remove "renamed.txt" from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(entry('renamed.txt')).toHaveCount(0);
  expect(await hubNames()).not.toContain('renamed.txt');
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
  await row.locator('button[title="Save to Current Folder"]').click();
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
  await api(page, 'POST', `${API}/requests/${created.data.id}/tunnel`, {
    tunnel: true
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

test('the cloud toggle flips every record and the next one, and no row carries a switch of its own', async ({
  page,
  request
}) => {
  await openPanel(page);
  // born on the hub's own address
  const first = await api(page, 'POST', `${API}/shares`, {
    name: 'first-one',
    paths: ['a.txt']
  });
  expect(first.data.tunnel).toBe(false);
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
  expect(listed.data.shares[0].tunnel).toBe(true);
  expect(listed.data.shares[0].link).toBe(`${TUNNEL}/s/${first.data.id}`);
  const second = await api(page, 'POST', `${API}/requests`, {
    name: 'second-one'
  });
  expect(second.data.tunnel).toBe(true);
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
  // ACC-CLOUD-159: the row menu carries no Cloudflare entry - the header
  // icon is the only switch
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'second-one'
  });
  await row
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  const menu = page.locator('.lm-Menu');
  await expect(menu).toBeVisible();
  await expect(menu.locator('.lm-Menu-item', { hasText: /Cloud/ })).toHaveCount(
    0
  );
  await expect(
    menu.locator('.lm-Menu-item', { hasText: 'Hub Network Only' })
  ).toHaveCount(0);
  await page.keyboard.press('Escape');

  // the header toggle off takes every record back to the hub network
  await cloud.click();
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  const after = await api(page, 'GET', `${API}/shares`);
  expect(after.data.shares[0].tunnel).toBe(false);
  const requests = await api(page, 'GET', `${API}/requests`);
  expect(requests.data.requests[0].tunnel).toBe(false);
  expect(requests.data.requests[0].link).toMatch(
    /^http:\/\/localhost:\d+\/s\//
  );
  // a link on the hub's own address says so, and is not probed - the lab
  // cannot open it
  await refreshPanel(page);
  await row.locator('button[title="Copy link"]').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText("Link works on the hub's network only");
  await expect(dialog.locator('[data-reach]')).toHaveCount(0);
  await dialog.locator('button', { hasText: 'Close' }).click();
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
    `${API}/shares/${(await api(page, 'GET', `${API}/shares`)).data.shares[0].id}/tunnel`,
    {
      tunnel: true
    }
  );
  expect(refused.status).toBe(403);
  expect(refused.data.reason).toBe('tunnel_not_available');
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

test('the hub bringing its tunnel up moves the icon from pending to on, with no click', async ({
  page,
  request
}) => {
  // ACC-HUBM-161: the hub rings its change stream when its tunnel verdict
  // flips, and the panel re-reads api/info on the ring. The mock connector
  // comes up five seconds after the first record is switched on, which is
  // long enough to read the pending look before it moves.
  await request.post(`${HUB}/_control/tunnel`, { data: { delay: 5 } });
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
  // switched on while the hub's tunnel is still coming up: the pending look,
  // and an aria state that says on but not yet reaching Cloudflare
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'mixed');
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  // the tunnel comes up: nobody clicks and nobody presses Refresh, and both
  // panels move to on
  await expect(cloud).toHaveClass(/jp-mod-active/, { timeout: 20000 });
  await expect(cloud).toHaveAttribute('aria-pressed', 'true');
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/);
  await expect(otherCloud).toHaveClass(/jp-mod-active/);
  // and both hand out the tunnel link
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

test('the hub dropping its tunnel moves the icon from on to pending, with no click', async ({
  page,
  request
}) => {
  // the other half of ACC-HUBM-161: the record stays switched on, the hub's
  // connector goes away, and the icon says so before anyone follows a link
  const created = await api(page, 'POST', `${API}/shares`, {
    name: 'drop-one',
    paths: ['a.txt']
  });
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-active/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'true');
  const on = await api(page, 'GET', `${API}/shares`);
  expect(on.data.shares[0].link).toBe(`${TUNNEL}/s/${created.data.id}`);

  await request.post(`${HUB}/_control/tunnel`, { data: { ready: false } });
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'mixed');
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  // the record is still switched on at the hub - only the hub's tunnel
  // moved - and its link fell back to the hub's own address
  const dropped = await api(page, 'GET', `${API}/shares`);
  expect(dropped.data.shares[0].tunnel).toBe(true);
  expect(dropped.data.shares[0].link).toBe(
    `${new URL(page.url()).origin}/s/${created.data.id}`
  );

  // the tunnel returns and the icon reads on again, still with no click
  await request.post(`${HUB}/_control/tunnel`, { data: { ready: true } });
  await expect(cloud).toHaveClass(/jp-mod-active/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'true');
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/);
});

test('the icon does not wait for a tunnel no record asked for', async ({
  page
}) => {
  // DEF-PANEL-89: the hub starts its tunnel for a record that wants one, so
  // on an empty panel nothing is on the way and nothing blinks
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await expect(cloud).not.toHaveClass(/jp-mod-armed/);
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-armed/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'true');
  await expect(cloud).toHaveAttribute('title', /no tunnel up yet/);
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/);
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
});

test('a group policy with no tunnel at all does not show the icon', async ({
  page,
  request
}) => {
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await expect(cloud).toBeVisible();
  await request.post(`${HUB}/_control/capabilities`, {
    data: { tunnel_available: false }
  });
  await refreshPanel(page);
  await expect(cloud).toBeHidden();
  // the policy grants a tunnel again and the icon comes back
  await request.post(`${HUB}/_control/capabilities`, {
    data: { tunnel_available: true }
  });
  await refreshPanel(page);
  await expect(cloud).toBeVisible();
});

test('a hub whose tunnel never comes up leaves the icon pending until a click switches it off', async ({
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
  await expect(cloud).toHaveAttribute('aria-pressed', 'mixed');
  // nothing takes the switch back on its own, and nothing is said in a toast
  expect(await notes(page, 'warning', '')).toBe(0);
  const pending = await api(page, 'GET', `${API}/shares`);
  expect(pending.data.shares[0].tunnel).toBe(true);
  // the icon reads pressed, so a click switches off
  await cloud.click();
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'false');
  const off = await api(page, 'GET', `${API}/shares`);
  expect(off.data.shares[0].tunnel).toBe(false);
  const state = await api(page, 'GET', `${API}/tunnel`);
  expect(state.data.tunnel_default).toBe(false);
});

test('the switch rides the hub tunnel route, and the route it replaced is gone', async ({
  page,
  request
}) => {
  const created = await api(page, 'POST', `${API}/shares`, {
    name: 'wire-one',
    paths: ['a.txt']
  });
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-active/);
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  const switches = calls.filter(
    (c: any) => c.method === 'PUT' && /\/(tunnel|cloud)$/.test(c.path)
  );
  expect(switches.map((c: any) => c.path)).toEqual([
    `/hub/api/fileshare/shares/${created.data.id}/tunnel`
  ]);
  expect(JSON.parse(switches[0].body)).toEqual({ tunnel: true });
  // the hub the lab was written against: the old route is not mounted, and
  // the new one takes a boolean
  const headers = { Authorization: 'token test-token' };
  const old = await request.put(
    `${HUB}/hub/api/fileshare/shares/${created.data.id}/cloud`,
    { headers, data: { cloud: true } }
  );
  expect(old.status()).toBe(404);
  const bad = await request.put(
    `${HUB}/hub/api/fileshare/shares/${created.data.id}/tunnel`,
    { headers, data: { tunnel: 'yes' } }
  );
  expect(bad.status()).toBe(400);
  expect((await bad.json()).message).toBe('tunnel must be a boolean');
});

test('a switch off the hub answers with an error leaves the header on, as the records stayed on', async ({
  page,
  request
}) => {
  await api(page, 'POST', `${API}/shares`, {
    name: 'stuck-one',
    paths: ['a.txt']
  });
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-active/);
  await request.post(`${HUB}/_control/cloudoff`, { data: { status: 500 } });
  await cloud.click();
  await expect
    .poll(() => notes(page, 'error', 'Could not switch Cloudflare sharing'))
    .toBe(1);
  // the record and the default stayed on at the hub, so the header reads on:
  // it never claims off while the hub keeps the records switched on
  await expect(cloud).toHaveClass(/jp-mod-active/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'true');
  await expect(cloud).toHaveAttribute(
    'title',
    'Cloudflare sharing on\nClick to switch it off'
  );
  const state = await api(page, 'GET', `${API}/tunnel`);
  expect(state.data.tunnel_default).toBe(true);
  const shares = await api(page, 'GET', `${API}/shares`);
  expect(shares.data.shares[0].tunnel).toBe(true);
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
  // a second click warns again, and nothing is raised as an error
  await page.locator(CLOUD).click();
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
  await expect(cloud).toHaveAttribute('aria-pressed', 'mixed');
  await read(); // on, the hub's tunnel still coming up
  await request.post(`${HUB}/_control/tunnel`, { data: { ready: true } });
  await expect(cloud).toHaveClass(/jp-mod-active/);
  await read(); // on
  await hubOutage(page);
  await refreshPanel(page);
  await expect(cloud).toHaveClass(/jp-mod-unreachable/);
  await read(); // hub unreachable
  // each state says something of its own, in two lines at most
  expect(new Set(titles).size).toBe(4);
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
  // the hub's tunnel is up, so the switched-on icon reads on rather than
  // pending - what is under test here is the keyboard, not the verdict
  await request.post(`${HUB}/_control/tunnel`, { data: { ready: true } });
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
  // Shift+F10 opens the row's menu; the arrow keys and Enter run an entry
  await page.keyboard.press('Shift+F10');
  const change = page.locator('.lm-Menu .lm-Menu-item', {
    hasText: 'Set Password'
  });
  await expect(change).toBeVisible();
  for (let i = 0; i < 8; i++) {
    if (await change.evaluate(el => el.classList.contains('lm-mod-active'))) {
      break;
    }
    await page.keyboard.press('ArrowDown');
  }
  await expect(change).toHaveClass(/lm-mod-active/);
  await page.keyboard.press('Enter');
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toBeVisible();
  await dialog.locator('button', { hasText: 'Cancel' }).click();
  // the ContextMenu key opens the same menu
  await header.focus();
  await page.keyboard.press('ContextMenu');
  await expect(change).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(change).toBeHidden();
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
