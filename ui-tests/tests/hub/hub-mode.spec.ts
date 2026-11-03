import { expect, test } from '@jupyterlab/galata';
import * as fs from 'fs';
import * as path from 'path';

import { readTrough, watchTrough } from '../helpers/breath';
import { dragOnto, dragOver } from '../helpers/drag';

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
// the group's policy id, the first segment of every public link
const POLICY = 'mockpolicy';
const CLOUD = `${PANEL} .jp-ShareFilesPanel-cloudIndicator`;
// a listed entry takes the keyboard focus, a file still in transit does not
const LISTED =
  '.jp-ShareFilesPanel-entry[data-row-key] .jp-ShareFilesPanel-entryName';

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

/** Count each time the cloud icon's one-shot arrival class APPEARS. Counting
 * appearances, not presence: the class stays on for its 700 ms envelope, so a
 * check that only asks whether it is there now reports one arrival again on
 * any later DOM change (DEF-PANEL-104). */
function watchArrivals(page: any): Promise<void> {
  return page.evaluate((sel: string) => {
    const w = window as any;
    w.__arrivals = 0;
    w.__wasOn = false;
    w.__glow = '';
    new MutationObserver(() => {
      const on = !!document.querySelector(`${sel}.jp-mod-arrived`);
      if (on && !w.__wasOn) {
        w.__arrivals += 1;
        // the cue is a glow of the glyph, not a ring around the element
        const icon = document.querySelector(sel)!;
        w.__glow =
          getComputedStyle(icon.querySelector('svg')!).animationName +
          ' ' +
          getComputedStyle(icon).boxShadow;
      }
      w.__wasOn = on;
    }).observe(document.documentElement, {
      subtree: true,
      attributes: true,
      attributeFilter: ['class']
    });
  }, CLOUD);
}

function arrivals(page: any): Promise<number> {
  return page.evaluate(() => (window as any).__arrivals as number);
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
      '@stellarshenson/jupyterlab-share-files-extension:plugin',
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

/** Record each text the named row's meta shows, for a test of what it must
 * not show between two reads. */
function watchMeta(page: any, title: string): Promise<void> {
  return page.evaluate(
    ([panel, name]: [string, string]) => {
      const w = window as any;
      w.__meta = [];
      const read = () => {
        const text = Array.from(
          document.querySelectorAll(`${panel} .jp-ShareFilesPanel-item`)
        )
          .find(el => el.textContent?.includes(name))
          ?.querySelector('.jp-ShareFilesPanel-itemMeta')
          ?.textContent?.trim();
        if (text && text !== w.__meta[w.__meta.length - 1]) {
          w.__meta.push(text);
        }
      };
      new MutationObserver(read).observe(document.querySelector(panel)!, {
        subtree: true,
        childList: true,
        characterData: true
      });
      read();
    },
    [PANEL, title]
  );
}

/** Run the panel's add on `paths`, as a drop on the share's row does: the file
 * browser lists no dot-named row to drag. */
async function addThroughPanel(
  page: any,
  shareId: string,
  paths: string[]
): Promise<void> {
  await page.evaluate(
    async ([panelId, id, chosen]: [string, string, string[]]) => {
      const { shell } = (window as any).jupyterapp;
      const panel = Array.from<any>(shell.widgets('right')).find(
        w => w.id === panelId
      );
      await panel.addToShareFlow(id, chosen);
    },
    [PANEL.slice(1), shareId, paths]
  );
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

/** The panel's stream ends at the test, as behind a proxy that buffers it:
 * no ring arrives, the browser reports no failure and no timer takes over.
 * Returns the sockets the panel opened. The page loads again because the
 * panel opens its stream at startup. */
async function blockStream(page: any): Promise<any[]> {
  const sockets: any[] = [];
  await page.routeWebSocket(`**${API}/stream*`, (ws: any) => {
    sockets.push(ws);
  });
  await page.reload();
  await openPanel(page);
  await expect.poll(() => sockets.length).toBe(1);
  // the open's own refresh lands before a test counts or holds a read
  await page.waitForTimeout(1000);
  return sockets;
}

/** The panel's reads of the share list from now on, as a list that grows. */
function countShareReads(page: any): string[] {
  const reads: string[] = [];
  page.on('request', (r: any) => {
    if (r.method() === 'GET' && /\/api\/shares(\?|$)/.test(r.url())) {
      reads.push(r.url());
    }
  });
  return reads;
}

/** A new share whose add of `name` the hub copies until the test posts
 * `_control/release`. Returns the row, which reads adding. */
async function aHeldAdd(
  page: any,
  request: any,
  title: string,
  name: string
): Promise<any> {
  await request.post(`${HUB}/_control/add`, { data: { hold: true } });
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: title,
    paths: []
  });
  const add = await api(page, 'POST', `${API}/shares/${made.data.id}/items`, {
    paths: [name]
  });
  expect(add.status).toBe(200);
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: title
  });
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    /adding/
  );
  return row;
}

/** The next read of the share list reads the lab at once and answers the
 * panel only when the test calls `answer`. */
async function holdNextRead(page: any): Promise<any> {
  const state = { held: false, answered: false };
  let answer = (): void => undefined;
  const gate = new Promise<void>(resolve => (answer = resolve));
  await page.route(`**${API}/shares*`, async (route: any) => {
    if (state.held || route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    state.held = true;
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
    state.answered = true;
  });
  return { state, answer: () => answer() };
}

test.beforeEach(async ({ request }) => {
  await request.post(`${HUB}/_control/reset`);
  // the lab keeps the cloud default in its config file from test to test;
  // with no record on the hub this stores the default alone
  await request.post(`${API}/tunnel`, { data: { active: false } });
  // and its connections, which the reset hub no longer knows
  const listed = await (await request.get(`${API}/connections`)).json();
  for (const conn of listed.connections || []) {
    await request.delete(`${API}/connections/${encodeURIComponent(conn.key)}`);
  }
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

test('recipient, static and peer download routes are not mounted', async ({
  page
}) => {
  for (const path of [
    '/jupyterlab-share-files-extension/public/share/AAAAAAAA',
    '/jupyterlab-share-files-extension/public/share/AAAAAAAA/manifest',
    '/jupyterlab-share-files-extension/public/request/AAAAAAAA',
    '/jupyterlab-share-files-extension/static/standalone.html',
    // a connected entry is saved into the workspace, never handed to the
    // browser (ACC-HUBM-178)
    `${API}/connections/share:hub:AAAAAAAA/download`
  ]) {
    const { status } = await api(page, 'GET', path);
    expect(status, path).toBe(404);
  }
});

test('the panel offers the connect row and the Connected section', async ({
  page
}) => {
  // ACC-HUBM-173: another user's record connects on a hub as a peer does
  // standalone
  await openPanel(page);
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-sectionTitle`, {
      hasText: 'Connected'
    })
  ).toBeVisible();
  const input = page.locator(`${PANEL} .jp-ShareFilesPanel-connectInput`);
  await expect(input).toBeVisible();
  await expect(input).toHaveAttribute(
    'placeholder',
    'Paste a share or request link'
  );
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-connectButton`)
  ).toBeVisible();
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-dropZone`)
  ).toHaveText('Drag files here to share');
});

test('the Connect button is the right end of the input and is disabled while the input is empty or holds only spaces', async ({
  page,
  request
}) => {
  // ACC-SHARE-183
  const made = await foreign(request, {
    title: 'Button State',
    names: ['a.csv'],
    password: 'pw'
  });
  await openPanel(page);
  const input = page.locator(`${PANEL} .jp-ShareFilesPanel-connectInput`);
  const button = page.locator(`${PANEL} .jp-ShareFilesPanel-connectButton`);
  const fill = () =>
    button.evaluate((b: Element) => getComputedStyle(b).backgroundColor);
  const faded = async () =>
    Number(await button.evaluate((b: Element) => getComputedStyle(b).opacity));
  const clear = 'rgba(0, 0, 0, 0)';
  // one control: the button starts on the input's right border, as tall
  const i = (await input.boundingBox())!;
  const b = (await button.boundingBox())!;
  expect(Math.abs(b.x - (i.x + i.width - 1))).toBeLessThan(0.5);
  expect(Math.abs(b.y - i.y)).toBeLessThan(0.5);
  expect(Math.abs(b.height - i.height)).toBeLessThan(0.5);
  // empty, or only spaces: a faded outline that does nothing
  await expect(button).toBeDisabled();
  expect(await fill()).toBe(clear);
  expect(await faded()).toBeLessThan(1);
  await input.fill('   ');
  await expect(button).toBeDisabled();
  // a link fills it
  await input.fill(made.url);
  await expect(button).toBeEnabled();
  expect(await fill()).not.toBe(clear);
  expect(await faded()).toBe(1);
  // disabled while the connection is being made, whatever is typed, and
  // still filled, faded, so the row does not look empty
  const dialog = page.locator('.jp-Dialog');
  await button.click();
  await expect(dialog).toContainText('This link is password protected');
  await expect(button).toBeDisabled();
  await expect(button).toHaveAttribute('aria-busy', 'true');
  expect(await fill()).not.toBe(clear);
  expect(await faded()).toBeLessThan(1);
  // the dialog holds the keyboard focus, so the edit is an input event
  await input.evaluate((el: HTMLInputElement, url: string) => {
    el.value = url;
    el.dispatchEvent(new Event('input'));
  }, made.url);
  await expect(button).toBeDisabled();
  // a connection that ends without a row keeps the link and the button
  await dialog.locator('button', { hasText: 'Cancel' }).click();
  await expect(dialog).toBeHidden();
  // the click focused the button; the dialog gives the focus back to the
  // input, not to the page
  await expect(input).toBeFocused();
  await expect(input).toHaveValue(made.url);
  await expect(button).toBeEnabled();
  await expect(button).toHaveAttribute('aria-busy', 'false');
  // a connected link leaves the input, and the button is an outline again
  await button.click();
  await dialog.locator('input[type="password"]').fill('pw');
  await dialog.locator('button', { hasText: 'Connect' }).click();
  await expect(connected(page, 'Button State')).toHaveCount(1);
  await expect(input).toHaveValue('');
  await expect(button).toBeDisabled();
  expect(await fill()).toBe(clear);
});

test('a link on the lab page host is read through the hub proxy', async ({
  page,
  request
}) => {
  // DEF-HUB-129: a net-isolated lab cannot open the hub's public address,
  // so a link on the host the browser reached the lab on is read at the hub
  // API origin - the mock hub here - with its own path
  const made = await foreign(request, { title: 'Same Host', names: ['a.csv'] });
  await openPanel(page);
  const link = new URL(new URL(made.url).pathname, page.url()).href;
  await page.locator(`${PANEL} .jp-ShareFilesPanel-connectInput`).fill(link);
  await page.locator(`${PANEL} .jp-ShareFilesPanel-connectButton`).click();
  await expect(connected(page, 'Same Host')).toHaveCount(1);
  // the panel keeps the link as pasted
  const { data } = await api(page, 'GET', `${API}/connections`);
  expect(data.connections.map((c: any) => c.link)).toEqual([link]);
});

test('a hub share is renamed from its menu and keeps its id and link', async ({
  page,
  request
}) => {
  // ACC-EDIT-193: the hub holds the title; the lab relays the new one
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'Before Name',
    paths: []
  });
  const id = made.data.id;
  const listed = async () =>
    (await api(page, 'GET', `${API}/shares`)).data.shares.find(
      (s: any) => s.id === id
    );
  const hubTitle = async () =>
    (await (await request.get(`${HUB}/_control/items`)).json()).items.find(
      (i: any) => i.id === id
    ).title;
  const before = await listed();
  await openPanel(page);
  await refreshPanel(page);
  const row = (name: string) =>
    page.locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name });
  const dialog = page.locator('.jp-Dialog');
  const rename = async (from: string, to: string) => {
    await row(from)
      .locator('.jp-ShareFilesPanel-itemHeader')
      .click({ button: 'right' });
    await page
      .locator('.lm-Menu .lm-Menu-item', { hasText: 'Rename Share...' })
      .click();
    // the dialog opens on the current name
    await expect(dialog.locator('input')).toHaveValue(from);
    await dialog.locator('input').fill(to);
    await dialog.locator('button', { hasText: 'Rename' }).click();
  };

  await rename('Before Name', 'After Name');
  await expect(row('After Name')).toBeVisible();
  await expect(row('Before Name')).toHaveCount(0);
  expect(await hubTitle()).toBe('After Name');
  const after = await listed();
  expect(after.name).toBe('After Name');
  expect(after.link).toBe(before.link);

  // spaces alone are refused and the title stays
  await rename('After Name', '   ');
  await expect(
    page.locator('.Toastify__toast', { hasText: "Missing 'name'" })
  ).toBeVisible();
  expect(await hubTitle()).toBe('After Name');
});

test('every entry of the share, request and New menus carries an icon', async ({
  page
}) => {
  await api(page, 'POST', `${API}/shares`, { name: 'Iconed Share', paths: [] });
  await api(page, 'POST', `${API}/requests`, { name: 'Iconed Request' });
  await openPanel(page);
  await refreshPanel(page);
  const header = (name: string) =>
    page
      .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name })
      .locator('.jp-ShareFilesPanel-itemHeader');
  // menuOf fails on an entry without an icon
  expect(await menuOf(page, header('Iconed Share'))).toEqual([
    'Copy Link',
    'Open in Browser',
    'Download to Current Folder',
    'Save as Zip',
    'Rename Share...',
    'Set Password...',
    'Delete Share'
  ]);
  expect(await menuOf(page, header('Iconed Request'))).toEqual([
    'Copy Link',
    'Open in Browser',
    'Download to Current Folder',
    'Save as Zip',
    'Set Password...',
    'Delete Request'
  ]);
  await page.locator(`${PANEL} button[title="New share or request"]`).click();
  const items = page.locator('.lm-Menu .lm-Menu-item[data-type="command"]');
  await expect(items).toHaveCount(2);
  await expect(items.locator('.lm-Menu-itemIcon svg')).toHaveCount(2);
  await page.keyboard.press('Escape');
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
  expect(ready.data.link).toMatch(
    new RegExp(`^http://localhost:\\d+/s/${POLICY}/MockId_`)
  );
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
  // ACC-PROG-172: the staging row wears the fraction the hub reports, as a
  // layer over the whole row; a row with nothing in flight wears none
  const fill = item('stay-staging-one').locator(
    '.jp-ShareFilesPanel-itemProgress'
  );
  await expect(fill).toHaveAttribute('aria-valuenow', '50');
  const staging = item('stay-staging-one').locator(
    '.jp-ShareFilesPanel-itemHeader'
  );
  const fillBox = (await fill.boundingBox())!;
  const headerBox = (await staging.boundingBox())!;
  expect(Math.abs(fillBox.width - headerBox.width / 2)).toBeLessThan(2);
  expect(fillBox.height).toBe(headerBox.height);
  for (const name of ['ready-one', 'refuse-one']) {
    await expect(
      item(name).locator('.jp-ShareFilesPanel-itemProgress')
    ).toHaveCount(0);
  }
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
  // while the hub copies, the row says what it is doing and wears how far
  await refreshPanel(page);
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    /adding/
  );
  await expect(row.locator('.jp-ShareFilesPanel-itemProgress')).toHaveAttribute(
    'aria-valuenow',
    '50'
  );
  // the hub rings on a refused add, so the row says it with no click
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '0 items - add refused'
  );
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(
    row.locator('.jp-ShareFilesPanel-empty', {
      hasText: 'refused an add'
    })
  ).toContainText('larger than your group allows');
});

test('two drops during a running add both land, with no busy notice and adding on the row until the last lands', async ({
  page,
  request
}) => {
  // ACC-EDIT-219, ACC-EDIT-220, DEF-HUB-138: the hub answers busy to the
  // second drop and the lab holds both, one copy after the other
  await request.post(`${HUB}/_control/add`, { data: { seconds: 4 } });
  for (const name of ['held-a.txt', 'held-b.txt', 'held-c.txt']) {
    await page.contents.uploadContent(name, 'text', name);
  }
  await openPanel(page);
  await api(page, 'POST', `${API}/shares`, {
    name: 'back-to-back',
    paths: []
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'back-to-back'
  });
  const meta = row.locator('.jp-ShareFilesPanel-itemMeta');
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await watchMeta(page, 'back-to-back');

  await dragOnto(page, 'held-a.txt', row);
  await expect(meta).toHaveText(/adding/);
  await dragOnto(page, 'held-b.txt', row);
  await dragOnto(page, 'held-c.txt', row);

  await expect(row.locator(LISTED)).toHaveText(
    ['held-a.txt', 'held-b.txt', 'held-c.txt'],
    { timeout: 30000 }
  );
  await expect(meta).toHaveText('3 items');
  expect(await notes(page, 'warning', 'busy copying')).toBe(0);
  // from the first adding on, the row counts its items once, at the end
  const seen: string[] = await page.evaluate(() => (window as any).__meta);
  expect(
    seen
      .slice(seen.findIndex(text => text.includes('adding')))
      .filter(text => text.includes('item'))
  ).toEqual(['3 items']);
});

test('each item of an add tints its own row by the fraction copied', async ({
  page,
  request
}) => {
  // ACC-PROG-217, ACC-PROG-218: the hub reports one copied and total for the
  // whole add, and the lab divides it over the items in the order it sent them
  await request.post(`${HUB}/_control/add`, { data: { hold: true } });
  await request.post(`${HUB}/_control/progress`, {
    data: { copied: 50, total: 500 }
  });
  await page.contents.uploadContent('a'.repeat(100), 'text', 'tint-a.txt');
  await page.contents.uploadContent('b'.repeat(300), 'text', 'tint-b.txt');
  await page.contents.uploadContent('c'.repeat(100), 'text', 'tint-c.txt');
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'tinted-items',
    paths: []
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'tinted-items'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const entry = (name: string) =>
    row.locator('.jp-ShareFilesPanel-entry', { hasText: name });
  const layer = (name: string) =>
    entry(name).locator('.jp-ShareFilesPanel-itemProgress');

  await addThroughPanel(page, made.data.id, [
    'tint-a.txt',
    'tint-b.txt',
    'tint-c.txt'
  ]);

  await expect(layer('tint-a.txt')).toHaveAttribute('aria-valuenow', '50');
  await expect(layer('tint-a.txt')).toHaveAttribute(
    'aria-label',
    'Copying tint-a.txt'
  );
  await expect(layer('tint-b.txt')).toHaveAttribute('aria-valuenow', '0');
  await expect(layer('tint-c.txt')).toHaveAttribute('aria-valuenow', '0');
  // half the row's width for half the item
  const half = (await layer('tint-a.txt').boundingBox())!;
  const whole = (await entry('tint-a.txt').boundingBox())!;
  expect(Math.abs(half.width - whole.width / 2)).toBeLessThan(2);
  expect(half.height).toBe(whole.height);
  // mid-transfer: the first item is copied and wears no layer, the second
  // is half copied, the third waits with a layer 2px wide (the min-width)
  await request.post(`${HUB}/_control/progress`, {
    data: { copied: 250, total: 500 }
  });
  await expect(layer('tint-b.txt')).toHaveAttribute('aria-valuenow', '50');
  await expect(entry('tint-a.txt')).toHaveCount(1);
  await expect(layer('tint-a.txt')).toHaveCount(0);
  await expect(layer('tint-c.txt')).toHaveAttribute('aria-valuenow', '0');
  await expect(layer('tint-c.txt')).toHaveCSS('width', '2px');

  await request.post(`${HUB}/_control/release`);
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '3 items'
  );
  await expect(row.locator(LISTED)).toHaveText([
    'tint-a.txt',
    'tint-b.txt',
    'tint-c.txt'
  ]);
  await expect(row.locator('.jp-ShareFilesPanel-itemProgress')).toHaveCount(0);
  for (const name of ['tint-a.txt', 'tint-b.txt', 'tint-c.txt']) {
    await page.contents.deleteFile(name);
  }
});

test('a file in transit has no remove button and takes no focus', async ({
  page,
  request
}) => {
  // a transfer row is there to be read: nothing on it acts on a file the
  // share does not hold yet
  await request.post(`${HUB}/_control/add`, { data: { hold: true } });
  await page.contents.uploadContent('x'.repeat(10), 'text', 'in-transit.txt');
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'no-remove',
    paths: []
  });
  await request.post(`${HUB}/_control/files`, {
    data: { id: made.data.id, names: ['listed.txt'] }
  });
  const add = await api(page, 'POST', `${API}/shares/${made.data.id}/items`, {
    paths: ['in-transit.txt']
  });
  expect(add.status).toBe(200);
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'no-remove'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const listed = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'listed.txt'
  });
  const transit = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'in-transit.txt'
  });

  await expect(
    transit.locator('.jp-ShareFilesPanel-itemProgress')
  ).toHaveAttribute('aria-valuenow', '50');
  // a file in transit comes after the entries the share lists
  await expect(row.locator('.jp-ShareFilesPanel-entryName')).toHaveText([
    'listed.txt',
    'in-transit.txt'
  ]);
  await expect(listed.locator('.jp-ShareFilesPanel-entryRemove')).toHaveCount(
    1
  );
  await expect(transit.locator('.jp-ShareFilesPanel-entryRemove')).toHaveCount(
    0
  );
  // no tab stop, so no Delete, F2 or menu key reaches it
  await expect(listed).toHaveAttribute('tabindex', '0');
  expect(await transit.getAttribute('tabindex')).toBeNull();
  // a right-click opens no menu of the panel
  await transit.click({ button: 'right' });
  await expect(
    page.locator('.lm-Menu-item', { hasText: 'Remove from Share' })
  ).toHaveCount(0);
  await page.keyboard.press('Escape');
  // a name the share lists already is drawn once, as the listed entry
  await page.route(`**${API}/shares*`, async (route: any) => {
    if (route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    const body = await response.json();
    for (const share of body.shares) {
      share.pending = [
        ...(share.pending || []),
        { name: 'listed.txt', size: 7, held: true, fraction: 0 }
      ];
    }
    await route.fulfill({ response, json: body });
  });
  await refreshPanel(page);
  await expect(listed).toHaveCount(1);
  await expect(listed.locator('.jp-ShareFilesPanel-itemProgress')).toHaveCount(
    0
  );
  await page.unroute(`**${API}/shares*`);
  // once the file has landed it is a listed entry, which can be removed
  await request.post(`${HUB}/_control/release`);
  await expect(transit.locator('.jp-ShareFilesPanel-entryRemove')).toHaveCount(
    1
  );
  await page.contents.deleteFile('in-transit.txt');
});

test('a file in transit is drawn at the top of the share and not in its folders', async ({
  page,
  request
}) => {
  // an added item lands at the top of the share, so an open folder lists
  // only what the hub holds in it
  await request.post(`${HUB}/_control/add`, { data: { hold: true } });
  await page.contents.uploadContent('x'.repeat(10), 'text', 'top-transit.txt');
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'transit-top',
    paths: []
  });
  await request.post(`${HUB}/_control/files`, {
    data: { id: made.data.id, names: ['inner/kept.txt'] }
  });
  const add = await api(page, 'POST', `${API}/shares/${made.data.id}/items`, {
    paths: ['top-transit.txt']
  });
  expect(add.status).toBe(200);
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'transit-top'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const transit = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'top-transit.txt'
  });
  await expect(
    transit.locator('.jp-ShareFilesPanel-itemProgress')
  ).toHaveAttribute('aria-valuenow', '50');

  await row
    .locator('.jp-ShareFilesPanel-entry', { hasText: 'inner' })
    .dblclick();
  await expect(row.locator(LISTED)).toHaveText(['..', 'kept.txt']);
  await expect(transit).toHaveCount(0);
  await row.locator('.jp-ShareFilesPanel-entry', { hasText: '..' }).dblclick();
  await expect(
    transit.locator('.jp-ShareFilesPanel-itemProgress')
  ).toHaveAttribute('aria-valuenow', '50');
  await request.post(`${HUB}/_control/release`);
  await expect(
    row.locator(LISTED).filter({ hasText: 'top-transit.txt' })
  ).toHaveCount(1);
  await page.contents.deleteFile('top-transit.txt');
});

test('a share the hub still stages lists its files in transit', async ({
  page
}) => {
  // ACC-PROG-217: a create's files are copied as an add's are, and the row
  // lists them instead of a line saying it is empty
  await page.contents.uploadContent('x'.repeat(10), 'text', 'staged.txt');
  await openPanel(page);
  await api(page, 'POST', `${API}/shares`, {
    name: 'stay-staging-rows',
    paths: ['staged.txt']
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'stay-staging-rows'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(
    row
      .locator('.jp-ShareFilesPanel-entry', { hasText: 'staged.txt' })
      .locator('.jp-ShareFilesPanel-itemProgress')
  ).toHaveAttribute('aria-valuenow', '50');
  await expect(row.locator('.jp-ShareFilesPanel-empty')).toHaveCount(0);
  await page.contents.deleteFile('staged.txt');
});

test('a drop on a collapsed share opens the row on the file it copies', async ({
  page,
  request
}) => {
  // ACC-PROG-229: the row the transfer goes to shows its files, at the top of
  // the share where they are drawn, even when the owner closed it in a folder
  await request.post(`${HUB}/_control/add`, { data: { hold: true } });
  await page.contents.uploadContent('x'.repeat(10), 'text', 'opens-row.txt');
  await page.contents.uploadContent('y'.repeat(10), 'text', 'opens-held.txt');
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'opens-on-drop',
    paths: []
  });
  await request.post(`${HUB}/_control/files`, {
    data: { id: made.data.id, names: ['inner/kept.txt'] }
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'opens-on-drop'
  });
  const header = row.locator('.jp-ShareFilesPanel-itemHeader');
  const twisty = row.locator('.jp-ShareFilesPanel-itemTwisty');
  const layer = (name: string) =>
    row
      .locator('.jp-ShareFilesPanel-entry', { hasText: name })
      .locator('.jp-ShareFilesPanel-itemProgress');
  await header.click();
  await row
    .locator('.jp-ShareFilesPanel-entry', { hasText: 'inner' })
    .dblclick();
  await expect(row.locator(LISTED)).toHaveText(['..', 'kept.txt']);
  await header.click();
  await expect(twisty).toHaveAttribute('aria-expanded', 'false');

  await dragOnto(page, 'opens-row.txt', row);

  await expect(twisty).toHaveAttribute('aria-expanded', 'true');
  await expect(layer('opens-row.txt')).toHaveAttribute('aria-valuenow', '50');
  // a drop the lab holds behind the running add opens the row as well
  await header.click();
  await expect(twisty).toHaveAttribute('aria-expanded', 'false');
  await dragOnto(page, 'opens-held.txt', row);
  await expect(twisty).toHaveAttribute('aria-expanded', 'true');
  await expect(layer('opens-held.txt')).toHaveAttribute('aria-valuenow', '0');
  await request.post(`${HUB}/_control/release`);
  // the lab sends the held add once the first lands, and the hub copies it
  await expect(layer('opens-held.txt')).toHaveAttribute('aria-valuenow', '50', {
    timeout: 15000
  });
  await request.post(`${HUB}/_control/release`);
  await expect(row.locator(LISTED)).toHaveText([
    'inner/',
    'opens-row.txt',
    'opens-held.txt'
  ]);
  await page.contents.deleteFile('opens-row.txt');
  await page.contents.deleteFile('opens-held.txt');
});

test('a row the owner closes during a transfer stays closed', async ({
  page,
  request
}) => {
  // ACC-PROG-229: the transfer opens the row once, and the reads that follow
  // it do not open it again
  await request.post(`${HUB}/_control/add`, { data: { hold: true } });
  await page.contents.uploadContent('x'.repeat(10), 'text', 'stays-shut.txt');
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'stays-shut',
    paths: []
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'stays-shut'
  });
  const twisty = row.locator('.jp-ShareFilesPanel-itemTwisty');
  await addThroughPanel(page, made.data.id, ['stays-shut.txt']);
  await expect(twisty).toHaveAttribute('aria-expanded', 'true');

  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(twisty).toHaveAttribute('aria-expanded', 'false');
  const reads = countShareReads(page);
  await request.post(`${HUB}/_control/progress`, {
    data: { copied: 30, total: 42 }
  });
  // the row follows the copy once a second, and each read renders it
  await expect.poll(() => reads.length, { timeout: 5000 }).toBeGreaterThan(2);
  await expect(twisty).toHaveAttribute('aria-expanded', 'false');
  await request.post(`${HUB}/_control/release`);
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '1 item'
  );
  await expect(twisty).toHaveAttribute('aria-expanded', 'false');
  await page.contents.deleteFile('stays-shut.txt');
});

test('a selection holding a dot-named folder adds the plain file and names the folder', async ({
  page,
  request
}) => {
  // ACC-EDIT-207, ACC-EDIT-208, DEF-HUB-140: the hub refuses a whole add over
  // one such name, so the lab sends the rest
  await page.contents.uploadContent('plain', 'text', 'dot-selection.md');
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'dot-selection',
    paths: []
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'dot-selection'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();

  await addThroughPanel(page, made.data.id, [
    '.images-mockup',
    'dot-selection.md'
  ]);

  await expect(row.locator('.jp-ShareFilesPanel-entryName')).toHaveText(
    'dot-selection.md'
  );
  expect(await notes(page, 'warning', 'Not added: .images-mockup')).toBe(1);
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  const added = calls.filter((c: any) => c.path.endsWith('/content'));
  expect(added.map((c: any) => JSON.parse(c.body).paths)).toEqual([
    ['dot-selection.md']
  ]);
});

test('a held add the hub refuses shows its reason and the next held add lands', async ({
  page,
  request
}) => {
  // ACC-EDIT-221: the name is free at the drop and taken when the add is sent
  await request.post(`${HUB}/_control/add`, { data: { seconds: 2 } });
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'held-refusal',
    paths: []
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'held-refusal'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const items = `${API}/shares/${made.data.id}/items`;
  for (const name of ['running.txt', 'clashes.txt', 'held-last.txt']) {
    const add = await api(page, 'POST', items, { paths: [name] });
    expect(add.status).toBe(200);
  }
  await request.post(`${HUB}/_control/files`, {
    data: { id: made.data.id, names: ['clashes.txt'] }
  });

  const entry = (name: string) => row.locator(LISTED, { hasText: name });
  await expect(entry('held-last.txt')).toHaveCount(1, { timeout: 15000 });
  await expect(entry('running.txt')).toHaveCount(1);
  await expect(
    row.locator('.jp-ShareFilesPanel-empty', {
      hasText: 'refused an add'
    })
  ).toContainText('already holds an entry');
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '3 items - add refused'
  );
  expect(await notes(page, 'warning', 'refused an add')).toBe(1);
});

test('a held add refused for the password says how to set it again', async ({
  page,
  request
}) => {
  // ACC-EDIT-221: the lab lost the password, and no group rule is the cause
  await request.post(`${HUB}/_control/add`, { data: { seconds: 2 } });
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'held-password',
    paths: []
  });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'held-password'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const items = `${API}/shares/${made.data.id}/items`;
  for (const name of ['pw-running.txt', 'refuse-password.txt']) {
    const add = await api(page, 'POST', items, { paths: [name] });
    expect(add.status).toBe(200);
  }

  await expect(
    row.locator('.jp-ShareFilesPanel-empty', { hasText: 'refused an add' })
  ).toContainText('open Change Password', { timeout: 15000 });
  await page
    .locator('.Toastify__toast', { hasText: 'refused an add' })
    .getByRole('button', { name: 'Show all' })
    .click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText('then add the files');
  await dialog.locator('.jp-mod-accept').click();
});

test('a row follows the hub once a second with no ring, and the reads stop when it settles', async ({
  page,
  request
}) => {
  // ACC-PROG-230, DEF-HUB-141: the row depends on no ring from the stream
  await blockStream(page);
  const row = await aHeldAdd(page, request, 'no-ring', 'no-ring.txt');
  // a second read of the copying row arms no second timer
  await refreshPanel(page);
  const reads = countShareReads(page);

  await page.waitForTimeout(3000);
  expect(reads.length).toBeGreaterThanOrEqual(2);
  expect(reads.length).toBeLessThanOrEqual(4);
  await request.post(`${HUB}/_control/release`);

  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '1 item',
    { timeout: 3000 }
  );
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(row.locator('.jp-ShareFilesPanel-entryName')).toHaveText(
    'no-ring.txt'
  );
  const settled = reads.length;
  await page.waitForTimeout(3000);
  expect(reads.length).toBe(settled);
});

test("a create's staging row follows the hub with no ring", async ({
  page,
  request
}) => {
  // ACC-PROG-230: a create reads staging, and the hub lands it by itself
  await blockStream(page);
  await request.post(`${HUB}/_control/staging`, { data: { seconds: 2 } });
  await openPanel(page);
  await api(page, 'POST', `${API}/shares`, {
    name: 'no-ring-create',
    paths: ['no-ring-create.txt']
  });
  await refreshPanel(page);
  const meta = page
    .locator(`${PANEL} .jp-ShareFilesPanel-item`, {
      hasText: 'no-ring-create'
    })
    .locator('.jp-ShareFilesPanel-itemMeta');
  await expect(meta).toContainText('staging');
  await expect(meta).toHaveText('1 item', { timeout: 5000 });
});

test('an older read answering late leaves the row as the newer read showed it', async ({
  page,
  request
}) => {
  // ACC-PROG-230: the read of adding answers after the read of the landed file
  await blockStream(page);
  const row = await aHeldAdd(page, request, 'late-read', 'late-read.txt');
  const meta = row.locator('.jp-ShareFilesPanel-itemMeta');
  const older = await holdNextRead(page);

  const refresh = page.locator(`${PANEL} button[title="Refresh"]`);
  await refresh.click();
  await expect.poll(() => older.state.held).toBe(true);
  await request.post(`${HUB}/_control/release`);
  await refresh.click();
  await expect(meta).toHaveText('1 item');
  // a read that heals the row a second later must not hide one wrong render
  await watchMeta(page, 'late-read');
  older.answer();
  await expect.poll(() => older.state.answered).toBe(true);
  await page.waitForTimeout(2500);
  expect(await page.evaluate(() => (window as any).__meta)).toEqual(['1 item']);
});

test('a read that throws leaves the reads going while the row copies', async ({
  page,
  request
}) => {
  // ACC-PROG-230: on a stream that rings nothing, no click on Refresh is
  // needed after a failed read
  await blockStream(page);
  const row = await aHeldAdd(page, request, 'read-fails', 'read-fails.txt');
  let failed = 0;
  await page.route(`**${API}/shares*`, (route: any) => {
    failed += 1;
    return route.fulfill({ status: 500, json: { error: 'failed' } });
  });
  await expect.poll(() => failed).toBe(1);
  await page.waitForTimeout(2500);
  // one read each second, as before the failure
  expect(failed).toBeGreaterThanOrEqual(2);
  expect(failed).toBeLessThanOrEqual(4);
  await page.unroute(`**${API}/shares*`);

  await request.post(`${HUB}/_control/release`);
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '1 item',
    { timeout: 3000 }
  );
});

test('an accepted add is read again a second later, whatever the read after its answer shows', async ({
  page,
  request
}) => {
  // ACC-PROG-230: the hub reports an add 0.01 to 0.02 s after its answer, so
  // the panel's read after that answer can still show the row settled
  await blockStream(page);
  await request.post(`${HUB}/_control/add`, { data: { hold: true } });
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'early-read',
    paths: []
  });
  await refreshPanel(page);
  const meta = page
    .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: 'early-read' })
    .locator('.jp-ShareFilesPanel-itemMeta');
  await expect(meta).toHaveText('0 items');
  let early = true;
  await page.route(`**${API}/shares*`, async (route: any) => {
    if (!early || route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    early = false;
    // the row as the hub shows it before it reports the add
    const response = await route.fetch();
    const body = await response.json();
    for (const share of body.shares) {
      share.adding = false;
      share.pending = [];
    }
    await route.fulfill({ response, json: body });
  });

  await addThroughPanel(page, made.data.id, ['early-read.txt']);
  await expect(meta).toHaveText('0 items');
  await expect(meta).toHaveText(/adding/, { timeout: 2500 });
  await request.post(`${HUB}/_control/release`);
  await expect(meta).toHaveText('1 item', { timeout: 3000 });
});

test('a detached panel stops reading the rows', async ({ page, request }) => {
  // ACC-PROG-230: no timer outlives the panel's place in the shell, and the
  // close the detach asks for does not start the timer of a lost socket
  const [socket] = await blockStream(page);
  await setPollInterval(page, 2);
  await aHeldAdd(page, request, 'detached', 'detached.txt');
  let closed = false;
  socket.onClose(() => {
    closed = true;
    return socket.close();
  });
  await page.evaluate((panelId: string) => {
    const { shell } = (window as any).jupyterapp;
    Array.from<any>(shell.widgets('right')).find(w => w.id === panelId).parent =
      null;
  }, PANEL.slice(1));
  const reads = countShareReads(page);
  await expect.poll(() => closed).toBe(true);
  await page.waitForTimeout(2500);
  expect(reads.length).toBe(0);
});

test('a read under way at a detach starts no further read', async ({
  page,
  request
}) => {
  // ACC-PROG-230: the read answers on a panel that has left the shell
  await blockStream(page);
  await aHeldAdd(page, request, 'detached-read', 'detached-read.txt');
  const held = await holdNextRead(page);
  await expect.poll(() => held.state.held).toBe(true);
  await page.evaluate((panelId: string) => {
    const { shell } = (window as any).jupyterapp;
    Array.from<any>(shell.widgets('right')).find(w => w.id === panelId).parent =
      null;
  }, PANEL.slice(1));
  held.answer();
  await expect.poll(() => held.state.answered).toBe(true);
  const reads = countShareReads(page);
  await page.waitForTimeout(3000);
  expect(reads.length).toBe(0);
});

test('a follow read answering after a newer refresh changes nothing on the row', async ({
  page,
  request
}) => {
  // ACC-PROG-230, ACC-EDIT-221: the older read still shows the add copying,
  // and taking it would raise the refusal's notice a second time
  await blockStream(page);
  const row = await aHeldAdd(page, request, 'older-follow', 'refuse-add.txt');
  const held = await holdNextRead(page);
  await expect.poll(() => held.state.held).toBe(true);
  await request.post(`${HUB}/_control/release`);
  await refreshPanel(page);
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '0 items - add refused'
  );
  await watchMeta(page, 'older-follow');
  held.answer();
  await expect.poll(() => held.state.answered).toBe(true);
  await page.waitForTimeout(2500);
  expect(await page.evaluate(() => (window as any).__meta)).toEqual([
    '0 items - add refused'
  ]);
  expect(await notes(page, 'warning', 'refused an add')).toBe(1);
});

test('an add the lab could not send says so, and not that the hub refused it', async ({
  page
}) => {
  // the lab's hold task stopped: the hub refused nothing
  await openPanel(page);
  await api(page, 'POST', `${API}/shares`, { name: 'lab-failed', paths: [] });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'lab-failed'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await page.route(`**${API}/shares*`, async (route: any) => {
    if (route.request().method() !== 'GET') {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    const body = await response.json();
    for (const share of body.shares) {
      share.add_reason = 'lab_failed';
    }
    await route.fulfill({ response, json: body });
  });
  await refreshPanel(page);

  await expect(
    row.locator('.jp-ShareFilesPanel-empty', { hasText: 'could not send' })
  ).toHaveText('The lab could not send this add.');
  expect(
    await notes(page, 'warning', 'lab-failed: The lab could not send this add.')
  ).toBe(1);
  expect(await notes(page, 'warning', 'hub refused')).toBe(0);
});

test('a refused add read by the follow raises its notice once', async ({
  page,
  request
}) => {
  // ACC-PROG-230, ACC-EDIT-221: the refusal arrives with no ring
  await blockStream(page);
  const row = await aHeldAdd(
    page,
    request,
    'no-ring-refusal',
    'refuse-add.txt'
  );
  await request.post(`${HUB}/_control/release`);

  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    '0 items - add refused',
    { timeout: 3000 }
  );
  await refreshPanel(page);
  expect(await notes(page, 'warning', 'refused an add')).toBe(1);
});

test('a new share from a selection holding a dot-named folder names the folder', async ({
  page
}) => {
  // ACC-EDIT-208, ACC-EDIT-209: the create says what it left out
  await page.contents.uploadContent('plain', 'text', 'dot-create.md');
  await openPanel(page);
  await page.evaluate(
    ([panelId, chosen]: [string, string[]]) => {
      const { shell } = (window as any).jupyterapp;
      void Array.from<any>(shell.widgets('right'))
        .find(w => w.id === panelId)
        .createShareFlow(chosen);
    },
    [PANEL.slice(1), ['.images-mockup', 'dot-create.md']]
  );
  await page.locator('.jp-Dialog .jp-mod-accept').click();

  await expect
    .poll(() => notes(page, 'warning', 'Not added: .images-mockup'))
    .toBe(1);
  await page.contents.deleteFile('dot-create.md');
});

test('a new share of only refused names makes no share and a long list can be read whole', async ({
  page,
  request
}) => {
  // ACC-EDIT-223: nothing is left to share, so the lab answers and the hub
  // is not called
  await openPanel(page);
  const names = [0, 1, 2, 3, 4].map(n => `.create-backup-${n}`);
  await page.evaluate(
    ([panelId, chosen]: [string, string[]]) => {
      const { shell } = (window as any).jupyterapp;
      void Array.from<any>(shell.widgets('right'))
        .find(w => w.id === panelId)
        .createShareFlow(chosen);
    },
    [PANEL.slice(1), names]
  );
  await page.locator('.jp-Dialog .jp-mod-accept').click();

  await expect
    .poll(() => notes(page, 'error', `Not added: ${names.join(', ')}`))
    .toBe(1);
  await page
    .locator('.Toastify__toast', { hasText: 'Could not create share' })
    .getByRole('button', { name: 'Show all' })
    .click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText(names[4]);
  await dialog.locator('.jp-mod-accept').click();
  await expect(
    page.locator('.Toastify__toast', { hasText: 'Creating share' })
  ).toHaveCount(0);
  await expect(page.locator(`${PANEL} .jp-ShareFilesPanel-item`)).toHaveCount(
    0
  );
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  expect(
    calls.filter((c: any) => c.method === 'POST' && c.path.endsWith('/shares'))
  ).toEqual([]);
});

test('a long list of left-out names can be read whole', async ({ page }) => {
  // ACC-EDIT-208: the toast cuts a message at 140 characters
  await page.contents.uploadContent('plain', 'text', 'long-left-out.md');
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'long-left-out',
    paths: []
  });
  await refreshPanel(page);
  const names = [0, 1, 2, 3, 4].map(n => `.config-backup-${n}`);
  const readWhole = async (toast: string, last: string) => {
    await page
      .locator('.Toastify__toast', { hasText: toast })
      .getByRole('button', { name: 'Show all' })
      .click();
    const dialog = page.locator('.jp-Dialog');
    await expect(dialog).toContainText(last);
    await dialog.locator('.jp-mod-accept').click();
    await expect(dialog).toHaveCount(0);
  };

  // some names taken, then none
  await addThroughPanel(page, made.data.id, [...names, 'long-left-out.md']);
  await readWhole('Not added', names[4]);
  await addThroughPanel(page, made.data.id, names);
  await readWhole('Could not add items', names[4]);
  await page.contents.deleteFile('long-left-out.md');
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

test('a workspace drag over a hub folder row marks the share, not the row, and the add lands at the top', async ({
  page,
  request
}) => {
  // ACC-EDIT-232, ACC-EDIT-233: the hub lands every add at the top until it
  // takes a target folder, so no folder row may promise otherwise
  await page.contents.uploadContent('w', 'text', 'over-hub-folder.txt');
  const { id, row, entry, hubNames } = await selectionShare(
    page,
    request,
    'hub-folder-top',
    ['docs/a.txt', 'b.txt']
  );
  const sent: any[] = [];
  page.on('request', (r: any) => {
    if (r.method() === 'POST' && r.url().includes(`/shares/${id}/items`)) {
      sent.push(r.postDataJSON());
    }
  });

  await dragOver(page, 'over-hub-folder.txt', entry('docs/'), [], row);
  await expect(entry('docs/')).not.toHaveClass(/jp-mod-dropTarget/);
  await page.mouse.up();

  await expect
    .poll(hubNames, { timeout: 20000 })
    .toContain('over-hub-folder.txt');
  expect(await hubNames()).not.toContain('docs/over-hub-folder.txt');
  expect(sent).toEqual([{ paths: ['over-hub-folder.txt'] }]);
});

test('a hub entry dragged over a folder row of its share outlines that row', async ({
  page,
  request
}) => {
  // ACC-EDIT-233: the move target is drawn, not only marked by class
  const { entry, hubNames } = await selectionShare(
    page,
    request,
    'hub-folder-outline',
    ['docs/a.txt', 'b.txt']
  );
  const from = (await entry('b.txt').boundingBox())!;
  const to = (await entry('docs/').boundingBox())!;
  await page.mouse.move(from.x + 40, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(from.x + 55, from.y + from.height / 2);
  await page.mouse.move(to.x + 40, to.y + to.height / 2, { steps: 10 });
  await expect(entry('docs/')).toHaveClass(/jp-mod-dropTarget/);
  const outline = await entry('docs/').evaluate((el: HTMLElement) => {
    const s = getComputedStyle(el);
    return `${s.outlineStyle} ${s.outlineWidth}`;
  });
  expect(outline).toBe('solid 2px');
  await page.mouse.up();

  await expect.poll(hubNames, { timeout: 20000 }).toContain('docs/b.txt');
});

/** A hub share holding `names`, open in the panel: its row, a locator of one
 * entry, the selected entries' names, the names the hub holds, and the names
 * of each remove call the panel sends from now on. */
async function selectionShare(
  page: any,
  request: any,
  title: string,
  names: string[]
): Promise<any> {
  await openPanel(page);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: title,
    paths: []
  });
  const id = made.data.id;
  await request.post(`${HUB}/_control/files`, { data: { id, names } });
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: title
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const removals: string[][] = [];
  page.on('request', (r: any) => {
    if (r.method() === 'DELETE' && r.url().includes(`/shares/${id}/items`)) {
      removals.push(new URL(r.url()).searchParams.getAll('name'));
    }
  });
  return {
    id,
    row,
    entry: (name: string) =>
      row.locator('.jp-ShareFilesPanel-entry', { hasText: name }),
    selected: row.locator(
      '.jp-ShareFilesPanel-entry.jp-mod-selected .jp-ShareFilesPanel-entryName'
    ),
    hubNames: async () =>
      (await (await request.get(`${HUB}/_control/items`)).json()).items
        .find((i: any) => i.id === id)
        .files.map((f: any) => f.name),
    removals
  };
}

test('ctrl-click toggles an entry, shift-click takes a range and a plain click takes one entry alone', async ({
  page,
  request
}) => {
  // ACC-EDIT-210, ACC-EDIT-211, ACC-EDIT-224
  const { entry, selected } = await selectionShare(page, request, 'picked', [
    'a1.txt',
    'b2.txt',
    'c3.txt',
    'd4.txt'
  ]);
  await expect(entry('a1.txt')).toHaveAttribute('aria-selected', 'false');
  // ctrl-click two entries, ctrl-click the first again: only the second
  await entry('a1.txt').click({ modifiers: ['Control'] });
  await entry('b2.txt').click({ modifiers: ['Control'] });
  await expect(selected).toHaveText(['a1.txt', 'b2.txt']);
  await entry('a1.txt').click({ modifiers: ['Control'] });
  await expect(selected).toHaveText(['b2.txt']);
  await expect(entry('b2.txt')).toHaveAttribute('aria-selected', 'true');
  await expect(entry('a1.txt')).toHaveAttribute('aria-selected', 'false');
  // the row the owner selects is an option of a list that takes several
  await expect(entry('b2.txt')).toHaveAttribute('role', 'option');
  await expect(entry('b2.txt').locator('..')).toHaveAttribute(
    'aria-multiselectable',
    'true'
  );
  // cmd-click is the same
  await entry('c3.txt').click({ modifiers: ['Meta'] });
  await expect(selected).toHaveText(['b2.txt', 'c3.txt']);
  // select two entries, click a third with no modifier: only the third
  await entry('d4.txt').click();
  await expect(selected).toHaveText(['d4.txt']);
  // click the first of four, shift-click the third: three rows, and no text
  await entry('a1.txt').click();
  await entry('c3.txt').click({ modifiers: ['Shift'] });
  await expect(selected).toHaveText(['a1.txt', 'b2.txt', 'c3.txt']);
  expect(await page.evaluate(() => String(window.getSelection()))).toBe('');
  // a range upwards from the same anchor replaces it
  await entry('a1.txt').click({ modifiers: ['Shift'] });
  await expect(selected).toHaveText(['a1.txt']);
  // shift with ctrl adds the range to the selection
  await entry('d4.txt').click();
  await entry('a1.txt').click({ modifiers: ['Control'] });
  await entry('b2.txt').click({ modifiers: ['Control', 'Shift'] });
  await expect(selected).toHaveText(['a1.txt', 'b2.txt', 'd4.txt']);
});

test('ctrl-click selects a folder without entering it, and another folder clears the selection', async ({
  page,
  request
}) => {
  // ACC-EDIT-210, ACC-EDIT-213
  const { row, entry, selected } = await selectionShare(
    page,
    request,
    'folders',
    ['a1.txt', 'sub/inside.txt', 'sub/more.txt']
  );
  const up = row.locator('.jp-ShareFilesPanel-entry', { hasText: '..' });
  await entry('sub/').click({ modifiers: ['Control'] });
  await expect(selected).toHaveText(['sub/']);
  // a ctrl double-click does not open the folder either
  await entry('sub/').dblclick({ modifiers: ['Control'] });
  await expect(up).toHaveCount(0);
  await expect(entry('a1.txt')).toBeVisible();
  // a selection in the top folder is cleared inside another folder
  await entry('a1.txt').click();
  await expect(selected).toHaveText(['a1.txt']);
  await entry('sub/').dblclick();
  await expect(up).toBeVisible();
  await expect(selected).toHaveCount(0);
  // the .. row takes no selection, an entry inside the folder does
  await up.click({ modifiers: ['Control'] });
  await expect(up).not.toHaveClass(/jp-mod-selected/);
  expect(await up.getAttribute('aria-selected')).toBeNull();
  await entry('inside.txt').click({ modifiers: ['Control'] });
  await expect(selected).toHaveText(['inside.txt']);
  await up.dblclick();
  await expect(entry('a1.txt')).toBeVisible();
  await expect(selected).toHaveCount(0);
});

test('remove on a selected entry asks once and removes the selection, and on another entry removes that entry alone', async ({
  page,
  request
}) => {
  // ACC-EDIT-212, ACC-EDIT-228
  const { entry, selected, hubNames, removals } = await selectionShare(
    page,
    request,
    'removed',
    ['a1.txt', 'b2.txt', 'c3.txt', 'd4.txt', 'e5.txt']
  );
  const dialog = page.locator('.jp-Dialog');
  const removeOn = (name: string) =>
    entry(name).locator('.jp-ShareFilesPanel-entryRemove').click();
  await entry('a1.txt').click();
  await entry('c3.txt').click({ modifiers: ['Shift'] });
  await expect(selected).toHaveText(['a1.txt', 'b2.txt', 'c3.txt']);
  // select three entries, choose remove: one dialog naming 3; cancel removes nothing
  await removeOn('b2.txt');
  await expect(dialog).toHaveCount(1);
  await expect(dialog).toContainText('Remove 3 entries from the share?');
  await dialog.locator('button.jp-mod-reject').click();
  await expect(dialog).toHaveCount(0);
  expect(removals).toEqual([]);
  expect(await hubNames()).toHaveLength(5);
  await expect(selected).toHaveText(['a1.txt', 'b2.txt', 'c3.txt']);
  // remove on an entry outside the selection removes it alone, as before
  await removeOn('e5.txt');
  await expect(dialog).toContainText('Remove "e5.txt" from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(entry('e5.txt')).toHaveCount(0);
  await expect(selected).toHaveText(['a1.txt', 'b2.txt', 'c3.txt']);
  // select three of four entries, choose remove on one: one entry stays
  await removeOn('b2.txt');
  await expect(dialog).toContainText('Remove 3 entries from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(entry('a1.txt')).toHaveCount(0);
  await expect(entry('c3.txt')).toHaveCount(0);
  expect(await hubNames()).toEqual(['d4.txt']);
  expect(removals).toEqual([['e5.txt'], ['a1.txt', 'b2.txt', 'c3.txt']]);
  // the render after the removal dropped the removed names and the anchor: a
  // shift-click takes one
  await entry('d4.txt').click({ modifiers: ['Shift'] });
  await expect(selected).toHaveText(['d4.txt']);
});

test('Space selects a focused entry, and Delete and the row menu remove the selection', async ({
  page,
  request
}) => {
  // ACC-EDIT-227, ACC-EDIT-212
  const { row, entry, selected, hubNames } = await selectionShare(
    page,
    request,
    'keyed',
    ['a1.txt', 'b2.txt', 'c3.txt', 'd4.txt', 'e5.txt']
  );
  const dialog = page.locator('.jp-Dialog');
  const scroll = () =>
    page.evaluate(
      (sel: string) => document.querySelector(sel)!.scrollTop,
      `${PANEL} .jp-ShareFilesPanel-body`
    );
  // Tab to an entry, Space: highlighted and aria-selected; Space again: not
  await entry('a1.txt').focus();
  await page.keyboard.press('Tab'); // the row's remove button
  await page.keyboard.press('Tab');
  await expect(entry('b2.txt')).toBeFocused();
  const top = await scroll();
  await page.keyboard.press('Space');
  await expect(entry('b2.txt')).toHaveAttribute('aria-selected', 'true');
  await expect(selected).toHaveText(['b2.txt']);
  await expect(entry('b2.txt')).toBeFocused();
  expect(await scroll()).toBe(top);
  await page.keyboard.press('Space');
  await expect(entry('b2.txt')).toHaveAttribute('aria-selected', 'false');
  await expect(selected).toHaveCount(0);
  await expect(entry('b2.txt')).toBeFocused();
  // Delete on a selected entry removes the selection
  await page.keyboard.press('Space');
  await entry('c3.txt').click({ modifiers: ['Control'] });
  await entry('c3.txt').focus();
  await page.keyboard.press('Delete');
  await expect(dialog).toContainText('Remove 2 entries from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(entry('b2.txt')).toHaveCount(0);
  await expect(entry('c3.txt')).toHaveCount(0);
  expect(await hubNames()).toEqual(['a1.txt', 'd4.txt', 'e5.txt']);
  // the menu of an entry outside the selection removes it alone
  const menuRemove = page.locator('.lm-Menu-item', {
    hasText: 'Remove from Share'
  });
  await entry('a1.txt').click({ modifiers: ['Control'] });
  await entry('d4.txt').click({ modifiers: ['Control'] });
  await entry('e5.txt').click({ button: 'right' });
  await menuRemove.click();
  await expect(dialog).toContainText('Remove "e5.txt" from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(entry('e5.txt')).toHaveCount(0);
  await expect(selected).toHaveText(['a1.txt', 'd4.txt']);
  // and the menu of a selected entry removes the selection
  await entry('a1.txt').click({ button: 'right' });
  await menuRemove.click();
  await expect(dialog).toContainText('Remove 2 entries from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(row.locator(LISTED)).toHaveCount(0);
  expect(await hubNames()).toEqual([]);
});

test('a selection keeps its entries through a ring and a refresh, and an entry gone from the share leaves it', async ({
  page,
  request
}) => {
  // ACC-EDIT-213
  const { id, row, entry, selected, removals } = await selectionShare(
    page,
    request,
    'stale',
    ['a1.txt', 'b2.txt', 'c3.txt']
  );
  await entry('a1.txt').click({ modifiers: ['Control'] });
  await entry('b2.txt').click({ modifiers: ['Control'] });
  await expect(selected).toHaveText(['a1.txt', 'b2.txt']);
  // mark the rows so the test sees the ring rebuild them
  const marked = row.locator('.jp-ShareFilesPanel-entry[data-old]');
  await row
    .locator('.jp-ShareFilesPanel-entry')
    .evaluateAll((rows: HTMLElement[]) =>
      rows.forEach(r => (r.dataset.old = '1'))
    );
  await request.post(`${HUB}/_control/nudge`);
  await expect(marked).toHaveCount(0);
  await expect(selected).toHaveText(['a1.txt', 'b2.txt']);
  await refreshPanel(page);
  await expect(selected).toHaveText(['a1.txt', 'b2.txt']);
  // select an entry, remove it through the API, refresh: remove on another
  // entry sends only that name
  const gone = await api(
    page,
    'DELETE',
    `${API}/shares/${id}/items?name=a1.txt`
  );
  expect(gone.status).toBe(200);
  await refreshPanel(page);
  await expect(entry('a1.txt')).toHaveCount(0);
  await expect(selected).toHaveText(['b2.txt']);
  removals.length = 0;
  await entry('b2.txt').locator('.jp-ShareFilesPanel-entryRemove').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText('Remove "b2.txt" from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(entry('b2.txt')).toHaveCount(0);
  expect(removals).toEqual([['b2.txt']]);
});

test('a removal the hub refuses part way names the refusal once and lists what remains', async ({
  page,
  request
}) => {
  // ACC-EDIT-214: the mock refuses a name holding refuse-remove
  const { row, entry, selected, hubNames } = await selectionShare(
    page,
    request,
    'partial',
    ['a1.txt', 'b2-refuse-remove.txt', 'c3.txt']
  );
  await entry('a1.txt').click();
  await entry('c3.txt').click({ modifiers: ['Shift'] });
  await expect(selected).toHaveCount(3);
  await entry('a1.txt').locator('.jp-ShareFilesPanel-entryRemove').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText('Remove 3 entries from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(entry('a1.txt')).toHaveCount(0);
  // the hub stopped at the refusal, so the name after it stays listed
  await expect(row.locator(LISTED)).toHaveText([
    'b2-refuse-remove.txt',
    'c3.txt'
  ]);
  await expect(selected).toHaveText(['b2-refuse-remove.txt', 'c3.txt']);
  expect(await hubNames()).toEqual(['b2-refuse-remove.txt', 'c3.txt']);
  const busy = () => notes(page, 'warning', 'busy copying other files');
  await expect.poll(busy).toBe(1);
  // and it stays one once the panel's refresh after the removal has landed
  await refreshPanel(page);
  expect(await busy()).toBe(1);
});

test('a removal of 51 entries goes in two calls and names may hold a space, a non-ASCII letter or an ampersand', async ({
  page,
  request
}) => {
  const names = Array.from(
    { length: 51 },
    (_, i) => `f${String(i).padStart(2, '0')} &é.txt`
  );
  const { row, entry, hubNames, removals } = await selectionShare(
    page,
    request,
    'many',
    names
  );
  await entry('f00 &é.txt').click();
  await entry('f50 &é.txt').click({ modifiers: ['Shift'] });
  await entry('f50 &é.txt').locator('.jp-ShareFilesPanel-entryRemove').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText('Remove 51 entries from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(row.locator(LISTED)).toHaveCount(0);
  expect(await hubNames()).toEqual([]);
  expect(removals).toEqual([names.slice(0, 50), names.slice(50)]);
});

test('a file in transit takes no selection', async ({ page, request }) => {
  // ACC-EDIT-210: rows drawn from the share's pending adds are not entries
  await request.post(`${HUB}/_control/add`, { data: { hold: true } });
  await page.contents.uploadContent('x'.repeat(10), 'text', 'unpicked.txt');
  const { id, entry, selected } = await selectionShare(
    page,
    request,
    'transit',
    ['listed.txt']
  );
  const add = await api(page, 'POST', `${API}/shares/${id}/items`, {
    paths: ['unpicked.txt']
  });
  expect(add.status).toBe(200);
  await refreshPanel(page);
  const transit = entry('unpicked.txt');
  // the row in transit is drawn, and is not a listed entry
  await expect(transit).toHaveCount(1);
  expect(await transit.getAttribute('data-row-key')).toBeNull();
  await entry('listed.txt').click({ modifiers: ['Control'] });
  await expect(selected).toHaveText(['listed.txt']);
  await transit.click({ modifiers: ['Control'] });
  await transit.click({ modifiers: ['Shift'] });
  await transit.click();
  await expect(selected).toHaveText(['listed.txt']);
  expect(await transit.getAttribute('aria-selected')).toBeNull();
  await request.post(`${HUB}/_control/release`);
  await page.contents.deleteFile('unpicked.txt');
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
  const upload = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'report.csv'
  });
  await expect(upload).toBeVisible();
  // the save is the row's menu item, not a button on the row
  await expect(upload.locator('button')).toHaveCount(0);
  expect(await menuOf(page, upload)).toEqual(['Download to Current Folder']);
  await upload.click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', {
      hasText: 'Download to Current Folder'
    })
    .click();
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
  await api(page, 'POST', `${API}/tunnel`, { active: true });
  const tunnel = `http://localhost:${process.env.MOCK_HUB_PORT || '8765'}`;
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Dialog inbox'
  });
  await row.locator('button[title="Copy link"]').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('input[readonly]')).toHaveValue(
    `${tunnel}/s/${POLICY}/${created.data.id}`
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
    new RegExp(`^http://localhost:\\d+/s/${POLICY}/${first.data.id}$`)
  );
  const cloud = page.locator(`${PANEL} .jp-ShareFilesPanel-cloudIndicator`);
  await expect(cloud).toHaveAttribute('title', /hub network only/);
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  // the header toggle switches the standing record on and sets the default
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-active/);
  const listed = await api(page, 'GET', `${API}/shares`);
  expect(listed.data.shares[0].tunnel).toBe(true);
  expect(listed.data.shares[0].link).toBe(
    `${TUNNEL}/s/${POLICY}/${first.data.id}`
  );
  const second = await api(page, 'POST', `${API}/requests`, {
    name: 'second-one'
  });
  expect(second.data.tunnel).toBe(true);
  expect(second.data.link).toBe(`${TUNNEL}/s/${POLICY}/${second.data.id}`);
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

  // the header toggle off takes every record back to the hub network. The
  // icon reads off the moment it is pressed, before the switch-off reaches
  // the records, so the records are read until they report it
  // (DEF-TESTS-108)
  await cloud.click();
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  await expect
    .poll(
      async () =>
        (await api(page, 'GET', `${API}/shares`)).data.shares[0].tunnel
    )
    .toBe(false);
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

test('a share the hub is still copying leaves pending and the panel poll gets its Cloudflare link', async ({
  page,
  request
}) => {
  // DEF-HUB-135: the hub refuses a switch on with busy while it copies the
  // new share's files; the create makes one attempt and leaves the record
  // pending. A refused switch changes nothing on the hub, so no ring comes:
  // the panel's own list poll is the probe that sees the copy land, switches
  // the record on and picks up the Cloudflare link.
  await openPanel(page);
  await setPollInterval(page, 2);
  await request.post(`${HUB}/_control/staging`, {
    data: { seconds: 2, busy: true }
  });
  await request.post(`${API}/tunnel`, { data: { active: true } });
  // create through the panel with a path, so the copy runs and the switch on
  // is refused while it does
  await page.contents.uploadContent('a', 'text', 'still-copying.txt');
  await page.filebrowser.openHomeDirectory();
  await page.filebrowser.revealFileInBrowser('still-copying.txt');
  await page
    .getByRole('region', { name: 'File Browser Section' })
    .getByRole('listitem', { name: /^Name: still-copying.txt/ })
    .click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: 'Share Files' })
    .click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toBeVisible();
  await dialog.locator('input[placeholder="Name"]').fill('still-copying');
  await dialog.locator('button.jp-mod-accept').click();
  // the create says the switch is not applied yet, and claims no copied link
  await expect.poll(() => notes(page, 'warning', 'has not applied')).toBe(1);
  expect(await notes(page, 'success', 'link copied')).toBe(0);
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  const switched = calls.find(
    (c: any) => c.method === 'PUT' && /\/shares\/[^/]+\/tunnel$/.test(c.path)
  );
  expect(switched).toBeTruthy();
  const path = switched.path.split('/');
  const id = path[path.length - 2];
  const afterCreate = await refreshes(request);
  // nobody presses Refresh: the pending row keeps the panel's own poll
  // running, the copy lands, and the read switches the record on
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'still-copying'
  });
  const shownLink = async (): Promise<string> => {
    await row.locator('button[title="Copy link"]').click();
    const linkDialog = page.locator('.jp-Dialog');
    await expect(linkDialog).toBeVisible();
    const shown = await linkDialog.locator('input[readonly]').inputValue();
    await linkDialog.locator('button', { hasText: 'Close' }).click();
    await expect(linkDialog).toBeHidden();
    return shown;
  };
  await expect
    .poll(shownLink, { timeout: 20000 })
    .toBe(`${TUNNEL}/s/${POLICY}/${id}`);
  expect(await refreshes(request)).toBeGreaterThan(afterCreate);
});

test('a share left on Cloudflare while the switch is off goes off at the next listing', async ({
  page,
  request
}) => {
  // DEF-HUB-130: a lab start with autostart off finds the records of the
  // last session still on the hub tunnel; the tunnel is one state, so the
  // panel's listing takes them off
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'left-public',
    paths: ['a.txt']
  });
  await request.put(`${HUB}/hub/api/fileshare/shares/${made.data.id}/tunnel`, {
    headers: { Authorization: 'token test-token' },
    data: { tunnel: true }
  });
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'left-public'
  });
  await expect(row).toBeVisible();
  await expect(page.locator(CLOUD)).toHaveAttribute('aria-pressed', 'false');
  const items = (await api(page, 'GET', `${API}/shares/${made.data.id}`)).data;
  expect(items.tunnel).toBe(false);
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  expect(
    calls.some(
      (c: any) =>
        c.method === 'PUT' &&
        c.path.endsWith(`/shares/${made.data.id}/tunnel`) &&
        JSON.parse(c.body || '{}').tunnel === false
    )
  ).toBe(true);
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
  const refused = await api(page, 'POST', `${API}/tunnel`, { active: true });
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

test('the change stream is one WebSocket and sends no HTTP request', async ({
  page,
  request
}) => {
  // ACC-HUBM-231-STREAM-SOCKET: an open request would hold one of the six
  // connections per origin, in every tab
  const held: string[] = [];
  const sockets: string[] = [];
  page.on('request', (r: any) => {
    if (r.url().includes(`${API}/stream`)) {
      held.push(r.url());
    }
  });
  page.on('websocket', (ws: any) => {
    if (ws.url().includes(`${API}/stream`)) {
      sockets.push(ws.url());
    }
  });
  await page.reload();
  await openPanel(page);
  await expect.poll(() => sockets.length).toBe(1);
  expect(sockets[0]).toMatch(/^ws:/);
  await expect
    .poll(
      async () =>
        (await (await request.get(`${HUB}/_control/streams`)).json()).open
    )
    .toBe(1);
  await refreshPanel(page);
  await page.waitForTimeout(1000);
  expect(sockets.length).toBe(1);
  expect(held).toEqual([]);
});

test('a message on the socket refreshes the rows', async ({ page }) => {
  // ACC-HUBM-231-STREAM-SOCKET: nothing else reads the rows while it stands
  const [socket] = await blockStream(page);
  await api(page, 'POST', `${API}/requests`, { name: 'Rung' });
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Rung'
  });
  await page.waitForTimeout(1000);
  await expect(row).toHaveCount(0);
  socket.send('changed');
  await expect(row).toBeVisible();
});

test('a closed socket falls back to the timer and the next refresh opens a new one', async ({
  page
}) => {
  // ACC-HUBM-231-STREAM-SOCKET: a WebSocket does not reconnect by itself
  const sockets = await blockStream(page);
  await setPollInterval(page, 2);
  const reads = countShareReads(page);
  await page.waitForTimeout(3000);
  expect(reads.length).toBe(0);
  await sockets[0].close();
  await expect.poll(() => sockets.length).toBe(2);
  // the timer's read, then the new socket's own; after those the timer is off
  await expect.poll(() => reads.length).toBe(2);
  await page.waitForTimeout(3000);
  expect(reads.length).toBe(2);
});

test('a socket that closes as it opens is opened again at the pace of the timer', async ({
  page
}) => {
  // ACC-HUBM-231-STREAM-SOCKET: a refused socket does not loop
  const opened: number[] = [];
  await page.routeWebSocket(`**${API}/stream*`, (ws: any) => {
    opened.push(Date.now());
    void ws.close();
  });
  await page.reload();
  await openPanel(page);
  await setPollInterval(page, 2);
  await page.waitForTimeout(1000);
  const before = opened.length;
  await page.waitForTimeout(6000);
  expect(opened.length - before).toBeGreaterThanOrEqual(2);
  expect(opened.length - before).toBeLessThanOrEqual(4);
});

test('a refresh during the handshake opens no second socket', async ({
  page
}) => {
  // ACC-HUBM-231-STREAM-SOCKET: a socket still connecting is the panel's stream
  await page.addInitScript((path: string) => {
    const w = window as any;
    w.__handshakes = 0;
    w.WebSocket = new Proxy(w.WebSocket, {
      construct(target: any, args: any[]) {
        if (!String(args[0]).includes(path)) {
          return new target(...args);
        }
        w.__handshakes += 1;
        // a handshake that never answers: the socket stays CONNECTING
        return { readyState: 0, close: () => undefined };
      }
    });
  }, `${API}/stream`);
  await page.reload();
  await openPanel(page);
  const handshakes = () =>
    page.evaluate(() => (window as any).__handshakes as number);
  await expect.poll(handshakes).toBe(1);
  await refreshPanel(page);
  await refreshPanel(page);
  expect(await handshakes()).toBe(1);
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
  // DEF-PANEL-104: the moment the tunnel lands is marked once
  await watchArrivals(page);
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
  await expect
    .poll(() => arrivals(page), { message: 'the arrival is marked once' })
    .toBe(1);
  expect(await page.evaluate(() => (window as any).__glow)).toBe(
    'share-files-arrive none'
  );
  // and both hand out the tunnel link
  for (const p of [page, other]) {
    await item(p).locator('button[title="Copy link"]').click();
    const dialog = p.locator('.jp-Dialog');
    await expect(dialog.locator('input[readonly]')).toHaveValue(
      `${TUNNEL}/s/${POLICY}/${created.data.id}`
    );
    await dialog.locator('button', { hasText: 'Close' }).click();
  }
  await other.close();
  // and a redraw of an icon already on marks nothing: the cue is keyed on
  // the change, so Refresh leaves it still
  // and a redraw of an icon already on marks nothing: the cue is keyed on the
  // arrival itself, so Refresh adds no second one
  await refreshPanel(page);
  await expect(cloud).toHaveClass(/jp-mod-active/);
  expect(await arrivals(page)).toBe(1);
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
  expect(on.data.shares[0].link).toBe(
    `${TUNNEL}/s/${POLICY}/${created.data.id}`
  );
  const filled = await cloud.locator('svg').innerHTML();

  await request.post(`${HUB}/_control/tunnel`, { data: { ready: false } });
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'mixed');
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  // waiting is the filled cloud of on, in the accent (owner, 2026-09-24)
  expect(await cloud.locator('svg').innerHTML()).toBe(filled);
  const [waitColor, accent] = await page.evaluate(() => {
    const probe = document.createElement('span');
    probe.style.color = 'var(--jp-brand-color1)';
    document.body.appendChild(probe);
    const value = getComputedStyle(probe).color;
    probe.remove();
    const el = document.querySelector('.jp-ShareFilesPanel-cloudIndicator')!;
    return [getComputedStyle(el).color, value];
  });
  expect(waitColor).toBe(accent);
  // waiting for a tunnel breathes, one breath every 1.6 s rather than a
  // blink: the hub takes about 90 seconds to bring one up, and a fast
  // flicker over that long reads as an alarm rather than as waiting
  const breath = await cloud.evaluate(el => {
    const cs = getComputedStyle(el);
    return {
      name: cs.animationName,
      seconds: Number.parseFloat(cs.animationDuration),
      timing: cs.animationTimingFunction
    };
  });
  expect(breath.name).toBe('share-files-breathe');
  expect(breath.seconds).toBeGreaterThanOrEqual(1.6);
  expect(breath.timing).toContain('cubic-bezier');
  // the glyph itself breathes, sharp and down to nothing, with no ring
  // around it: a pulsed ring read as a box (owner, 2026-09-24)
  await expect
    .poll(() => cloud.evaluate(el => Number(getComputedStyle(el).opacity)), {
      message: 'the glyph fades to nothing as it breathes'
    })
    .toBeLessThan(0.05);
  expect(await cloud.evaluate(el => getComputedStyle(el).boxShadow)).toBe(
    'none'
  );
  // the panel has no reduced-motion look (owner, 2026-09-24): where the
  // system asks for reduced motion the cloud breathes all the same
  await page.emulateMedia({ reducedMotion: 'reduce' });
  expect(await cloud.evaluate(el => getComputedStyle(el).animationName)).toBe(
    'share-files-breathe'
  );
  await page.emulateMedia({ reducedMotion: null });
  // the record is still switched on at the hub - only the hub's tunnel
  // moved - and its link fell back to the hub's own address
  const dropped = await api(page, 'GET', `${API}/shares`);
  expect(dropped.data.shares[0].tunnel).toBe(true);
  expect(dropped.data.shares[0].link).toBe(
    `${new URL(page.url()).origin}/s/${POLICY}/${created.data.id}`
  );

  // the tunnel returns and the icon reads on again, still with no click
  await request.post(`${HUB}/_control/tunnel`, { data: { ready: true } });
  await expect(cloud).toHaveClass(/jp-mod-active/);
  await expect(cloud).toHaveAttribute('aria-pressed', 'true');
  await expect(cloud).not.toHaveClass(/jp-mod-connecting/);
  // established is a still glyph in the grey the panel's other header icons
  // wear; only the switch in flight takes the accent (owner, 2026-09-24).
  // The arrival cue runs once over 700 ms as the tunnel lands
  // (DEF-PANEL-104), so what is asserted is that the icon comes to rest, not
  // that it never moved
  await expect
    .poll(
      () =>
        page.evaluate(() => {
          const el = document.querySelector(
            '.jp-ShareFilesPanel-cloudIndicator'
          )!;
          return [
            getComputedStyle(el).animationName,
            getComputedStyle(el.querySelector('svg')!).animationName
          ].join(' ');
        }),
      { message: 'the icon settles once the arrival cue has run' }
    )
    .toBe('none none');
  // the colour the other header icons are painted in, read from the New
  // button's own icon: the button's text colour is a darker grey that no
  // icon wears
  const [iconColor, headerColor] = await page.evaluate(() => {
    const painted = document.querySelector(
      'button[title="New share or request"] .jp-icon3'
    )!;
    const el = document.querySelector(
      '.jp-ShareFilesPanel-cloudIndicator svg'
    )!;
    return [getComputedStyle(el).fill, getComputedStyle(painted).fill];
  });
  expect(iconColor).toBe(headerColor);
});

test('a switch the hub answers at once still shows one whole breath', async ({
  page,
  request
}) => {
  // the hub answers a switch in milliseconds when its tunnel is up, and the
  // breath holds near full strength for its first half second, so the owner
  // saw a still blue cloud and then the result
  await request.post(`${HUB}/_control/tunnel`, { data: { ready: true } });
  await api(page, 'POST', `${API}/shares`, {
    name: 'breath-one',
    paths: ['a.txt']
  });
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await expect(cloud).not.toHaveClass(/jp-mod-active/);
  await watchTrough(page, CLOUD);
  const started = Date.now();
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  await expect(cloud).toHaveClass(/jp-mod-active/);
  expect(Date.now() - started).toBeGreaterThanOrEqual(1600);
  // the glyph faded to nothing while the icon still showed the switch
  expect(await readTrough(page)).toBeLessThan(0.05);
});

test('the icon does not wait for a tunnel no record asked for', async ({
  page
}) => {
  // DEF-PANEL-89: the hub starts its tunnel for a record that wants one, so
  // on an empty panel nothing is on the way, so the icon stays still
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
  // and no ring around it (owner, 2026-09-24)
  await expect(cloud).toHaveCSS('box-shadow', 'none');
  // ACC-CLOUD-199: the switch says why no tunnel is on the way
  const NOTHING = 'no share or request to expose yet';
  await expect.poll(() => notes(page, 'warning', NOTHING)).toBe(1);
  // ACC-CLOUD-200: a switch off says nothing
  await cloud.click();
  // the title of the switch in flight gives way to the one of the off look
  await expect(cloud).toHaveAttribute('title', /Click to switch it on/);
  expect(await notes(page, 'warning', NOTHING)).toBe(1);
  // nor does a switch on with a share to expose
  await api(page, 'POST', `${API}/shares`, { name: 'exposed-one', paths: [] });
  await refreshPanel(page);
  await cloud.click();
  // the settled title is drawn by the refresh, which follows the decision
  await expect(cloud).toHaveAttribute('title', /Click to switch it off/);
  await expect(cloud).not.toHaveClass(/jp-mod-armed/);
  expect(await notes(page, 'warning', NOTHING)).toBe(1);
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
  // each state says something of its own, in two lines at most
  expect(new Set(titles).size).toBe(3);
  for (const title of titles) {
    expect(title.split('\n').length, title).toBeLessThanOrEqual(2);
  }
});

test('a clicked Refresh during a hub outage warns once and the icon is hidden', async ({
  page,
  request
}) => {
  await openPanel(page);
  await refreshPanel(page);
  const cloud = page.locator(CLOUD);
  await expect(cloud).toBeVisible();
  await hubOutage(page);
  await refreshPanel(page);
  await expect
    .poll(() => notes(page, 'warning', 'The hub could not be reached'))
    .toBe(1);
  // with no hub there is nothing to switch, so there is no icon
  // (owner, 2026-09-24)
  await expect(cloud).toBeHidden();
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
  // keyboard focus draws nothing around the icon (owner, 2026-09-24)
  await expect(cloud).toHaveCSS('outline-style', 'none');
  await expect(cloud).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
  await expect(cloud).toHaveCSS('box-shadow', 'none');
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

test('a whole hub share is saved by the hub, as files or as a zip', async ({
  page,
  request
}) => {
  // ACC-SAVE-171 on a hub: the hub owns the bytes and writes them itself. It
  // packs no archive, so the lab zips what the hub wrote - both entries are
  // offered, and both reach the hub's own fetch route. What lands on disk is
  // pinned in the python suite, where the fake hub shares this lab's root.
  await api(page, 'POST', `${API}/shares`, { name: 'Whole Record', paths: [] });
  await openPanel(page);
  await refreshPanel(page);
  const row = () =>
    page
      .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: 'Whole Record' })
      .locator('.jp-ShareFilesPanel-itemHeader');
  await row().click({ button: 'right' });
  const menu = page.locator('.lm-Menu');
  await expect(
    menu.locator('.lm-Menu-item', { hasText: 'Download to Current Folder' })
  ).toBeVisible();
  await expect(
    menu.locator('.lm-Menu-item', { hasText: 'Save as Zip' })
  ).toBeVisible();
  await menu
    .locator('.lm-Menu-item', { hasText: 'Download to Current Folder' })
    .click();

  const fetches = async () =>
    (await (await request.get(`${HUB}/_control/calls`)).json()).calls.filter(
      (c: any) => c.path.endsWith('/fetch')
    );
  await expect
    .poll(async () => (await fetches()).length, {
      message: 'the lab asks the hub to write the record'
    })
    .toBe(1);
  // the hub writes into a folder only this save names, which then takes the
  // free name
  expect(JSON.parse((await fetches())[0].body).dest).toMatch(
    /\.Whole-Record-part-[0-9a-f]{8}$/
  );

  // the zip goes through the same route, into a staging folder the lab packs
  // from and then removes
  await row().click({ button: 'right' });
  await menu.locator('.lm-Menu-item', { hasText: 'Save as Zip' }).click();
  await expect
    .poll(async () => (await fetches()).length, {
      message: 'the zip is packed from a second fetch'
    })
    .toBe(2);
  expect(JSON.parse((await fetches())[1].body).dest).toMatch(
    /\.Whole-Record-part-[0-9a-f]{8}$/
  );
});

test('a file of a hub share saves to the current folder from its menu', async ({
  page,
  request
}) => {
  // ACC-SAVE-170: a hub entry has no workspace path, so the row's own button
  // is the save. The hub reads no single entry, so the server fetches the
  // record and moves this one out - the call the hub sees is a record fetch.
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'Has Files',
    paths: []
  });
  await request.post(`${HUB}/_control/files`, {
    data: { id: made.data.id, names: ['picked.csv'] }
  });
  await openPanel(page);
  await refreshPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Has Files'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const entry = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'picked.csv'
  });
  await expect(entry).toBeVisible();
  // the save is the row's menu item; the row's one button removes
  await expect(entry.locator('button')).toHaveCount(1);
  await expect(entry.locator('button[title="Remove"]')).toHaveCount(1);
  expect(await menuOf(page, entry)).toEqual([
    'Download to Current Folder',
    'Rename',
    'Remove from Share'
  ]);
  await entry.click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', {
      hasText: 'Download to Current Folder'
    })
    .click();

  await expect
    .poll(
      async () =>
        (
          await (await request.get(`${HUB}/_control/calls`)).json()
        ).calls.filter((c: any) => c.path.endsWith('/fetch')).length,
      { message: 'the record is fetched so the entry can be moved out' }
    )
    .toBe(1);
});

// --------------------------------------------------------------------------- //
// Records another user owns (ACC-HUBM-173 to ACC-HUBM-180)
// --------------------------------------------------------------------------- //

/** A record another user owns, on the mock hub; returns its id and link. */
async function foreign(
  request: any,
  data: {
    kind?: 'share' | 'request';
    title: string;
    names?: string[];
    password?: string;
    /** reached over https, with a self-signed certificate */
    tls?: boolean;
  }
): Promise<{ id: string; url: string }> {
  return (await request.post(`${HUB}/_control/foreign`, { data })).json();
}

/** Paste `text` into the connect row and connect with Enter. */
async function connect(page: any, text: string): Promise<void> {
  const input = page.locator(`${PANEL} .jp-ShareFilesPanel-connectInput`);
  await input.fill(text);
  await input.press('Enter');
}

function connected(page: any, title: string): any {
  return page
    .locator(`${PANEL} .jp-ShareFilesPanel-section`, { hasText: 'Connected' })
    .locator('.jp-ShareFilesPanel-item', { hasText: title });
}

/** The menu entries a right-click on `target` opens, closed again. */
async function menuOf(page: any, target: any): Promise<string[]> {
  await target.click({ button: 'right' });
  const items = page.locator('.lm-Menu .lm-Menu-itemLabel');
  await expect(items.first()).toBeVisible();
  const labels = (await items.allTextContents()).filter((t: string) => t);
  // every entry of a panel menu carries an icon
  const bare = page
    .locator('.lm-Menu .lm-Menu-item[data-type="command"]')
    .filter({ hasNot: page.locator('.lm-Menu-itemIcon svg') });
  expect(await bare.allTextContents()).toEqual([]);
  await page.keyboard.press('Escape');
  return labels;
}

test('the whole link connects on the hub address and the tunnel hostname, and nothing less does', async ({
  page,
  request
}) => {
  // ACC-HUBM-174: the link is kept as pasted and read as it is
  const forms: [string, (r: { id: string; url: string }) => string][] = [
    ['Form Hub', r => r.url],
    ['Form Tunnel', r => `${TUNNEL}/s/${POLICY}/${r.id}`]
  ];
  await openPanel(page);
  for (const [title, text] of forms) {
    const made = await foreign(request, { title, names: ['a.csv'] });
    await connect(page, text(made));
    await expect(connected(page, title)).toHaveCount(1);
    const stored = (await api(page, 'GET', `${API}/connections`)).data
      .connections;
    expect(stored.find((c: any) => c.name === title).link).toBe(text(made));
  }
  // a bare id, the hub's own page and a workspace path reach no record
  const made = await foreign(request, { title: 'Form None', names: ['a.csv'] });
  for (const text of [
    made.id,
    `${HUB}/hub/s/${POLICY}/${made.id}`,
    'notes/report.csv'
  ]) {
    await connect(page, text);
    await expect
      .poll(() =>
        notes(page, 'error', 'Paste the whole link of a hub share or request')
      )
      .toBeGreaterThan(0);
    await expect(
      page.locator(`${PANEL} .jp-ShareFilesPanel-connectInput`)
    ).toHaveValue(text);
  }
  await expect(connected(page, 'Form None')).toHaveCount(0);
});

test('a connected record is read through its link, without the hub token', async ({
  page,
  request
}) => {
  // ACC-HUBM-175
  const made = await foreign(request, {
    title: 'Read By Link',
    names: ['a.csv']
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Read By Link');
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(row.locator('.jp-ShareFilesPanel-entry')).toHaveCount(1);
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  const reads = calls.filter((c: any) =>
    c.path.startsWith(`/s/${POLICY}/${made.id}`)
  );
  expect(reads.length).toBeGreaterThan(0);
  expect(reads.every((c: any) => c.auth === '')).toBe(true);
  // the hub API never hears of the record
  expect(calls.filter((c: any) => c.path.includes(made.id))).toEqual(reads);
});

test('a protected record asks for its password once and retries', async ({
  page,
  request
}) => {
  // ACC-HUBM-176
  const made = await foreign(request, {
    title: 'Locked Record',
    names: ['a.csv'],
    password: 'pw'
  });
  await openPanel(page);
  const dialog = page.locator('.jp-Dialog');
  await connect(page, made.url);
  await expect(dialog).toContainText('This link is password protected');
  await dialog.locator('button', { hasText: 'Cancel' }).click();
  await expect(dialog).toBeHidden();
  await expect(connected(page, 'Locked Record')).toHaveCount(0);

  await connect(page, made.url);
  await dialog.locator('input[type="password"]').fill('nope');
  await dialog.locator('button', { hasText: 'Connect' }).click();
  await expect(dialog).toContainText('Wrong password - try again');
  await dialog.locator('input[type="password"]').fill('pw');
  await dialog.locator('button', { hasText: 'Connect' }).click();
  await expect(dialog).toBeHidden();
  const row = connected(page, 'Locked Record');
  await expect(row).toHaveCount(1);
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(row.locator('.jp-ShareFilesPanel-entryName')).toHaveText(
    'a.csv'
  );
});

test('a certificate the lab does not trust is asked about at connect, and the trust lasts the connection', async ({
  page,
  request
}) => {
  // ACC-SHARE-181
  const made = await foreign(request, {
    title: 'Behind TLS',
    names: ['a.csv'],
    tls: true
  });
  await openPanel(page);
  const dialog = page.locator('.jp-Dialog');
  const print = dialog.locator('.jp-ShareFilesPanel-fingerprint');
  await connect(page, made.url);
  await expect(dialog.locator('.jp-Dialog-header')).toHaveText(
    'Trust this certificate?'
  );
  await expect(dialog).toContainText(
    `${new URL(made.url).host} presents a certificate this lab does not trust: self-signed certificate.`
  );
  await expect(print).toHaveText(/^([0-9A-F]{2}:){31}[0-9A-F]{2}$/);
  const first = await print.textContent();
  // Enter leaves the certificate untrusted, connects nothing and keeps the link
  await page.keyboard.press('Enter');
  await expect(dialog).toBeHidden();
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-connectInput`)
  ).toHaveValue(made.url);
  expect(
    (await api(page, 'GET', `${API}/connections`)).data.connections
  ).toEqual([]);

  await connect(page, made.url);
  await dialog.getByRole('button', { name: 'Trust', exact: true }).click();
  const row = connected(page, 'Behind TLS');
  await expect(row).toHaveCount(1);
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(row.locator('.jp-ShareFilesPanel-entryName')).toHaveText(
    'a.csv'
  );
  const [stored] = (await api(page, 'GET', `${API}/connections`)).data
    .connections;
  expect(stored.certificate).toContain('BEGIN CERTIFICATE');

  // the host presents another certificate: the row names it, and Connect
  // Again asks about the new one
  await setPollInterval(page, 2);
  await request.post(`${HUB}/_control/certificate`, {
    data: { name: 'peer-b' }
  });
  const badge = row.locator('.jp-ShareFilesPanel-offline');
  await expect(badge).toHaveText('untrusted');
  await expect(badge).toHaveAttribute(
    'title',
    /^The host presents a certificate this connection does not trust - choose Connect Again/
  );
  await row
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: 'Connect Again...' })
    .click();
  await expect(print).toHaveText(/^([0-9A-F]{2}:){31}[0-9A-F]{2}$/);
  expect(await print.textContent()).not.toBe(first);
  await dialog.getByRole('button', { name: 'Trust', exact: true }).click();
  await expect(badge).toHaveCount(0);
});

test('an upload the lab stops for a changed certificate stays true once the new one is trusted', async ({
  page,
  request
}) => {
  // ACC-SHARE-181
  await page.contents.uploadContent('t', 'text', 'tls-up.txt');
  const made = await foreign(request, {
    kind: 'request',
    title: 'TLS Inbox',
    tls: true
  });
  await openPanel(page);
  const dialog = page.locator('.jp-Dialog');
  await connect(page, made.url);
  await dialog.getByRole('button', { name: 'Trust', exact: true }).click();
  const row = connected(page, 'TLS Inbox');
  await expect(row).toHaveCount(1);

  // the host presents another certificate before the upload; the panel
  // reads the row again only after the drop, so the drop is sent
  await setPollInterval(page, 3600);
  await request.post(`${HUB}/_control/certificate`, {
    data: { name: 'peer-b' }
  });
  await dragOnto(page, 'tls-up.txt', row);
  await setPollInterval(page, 2);
  const badge = row.locator('.jp-ShareFilesPanel-offline');
  await expect(badge).toHaveText('untrusted');

  await row
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: 'Connect Again...' })
    .click();
  await dialog.getByRole('button', { name: 'Trust', exact: true }).click();
  await expect(badge).toHaveCount(0);
  // the stopped upload is told in the past tense, with no instruction the
  // user has just followed, and the hover says it once
  const meta = row.locator('.jp-ShareFilesPanel-itemMeta');
  await expect(meta).toHaveText('request - upload stopped');
  await expect(meta).toHaveAttribute(
    'title',
    `The last upload stopped - ${new URL(made.url).host} presented a certificate this connection did not trust (self-signed certificate)`
  );
  await page.contents.deleteFile('tls-up.txt');
});

test('your own record is refused by name and listed once', async ({ page }) => {
  // ACC-HUBM-177: the hub names the owner; the lab is spawned for alice
  const made = await api(page, 'POST', `${API}/shares`, {
    name: 'Mine Already',
    paths: []
  });
  await openPanel(page);
  await refreshPanel(page);
  await connect(page, made.data.link);
  await expect
    .poll(() => notes(page, 'error', 'That share is your own'))
    .toBe(1);
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
      hasText: 'Mine Already'
    })
  ).toHaveCount(1);
  await expect(connected(page, 'Mine Already')).toHaveCount(0);
});

test('a connected share saves a file, a folder, the whole and a zip into the current folder', async ({
  page,
  request,
  tmpPath
}) => {
  // ACC-HUBM-178: the lab downloads the bytes through the link
  const made = await foreign(request, {
    title: 'Their Share',
    names: ['a.csv', 'd/x.txt']
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Their Share');
  const header = row.locator('.jp-ShareFilesPanel-itemHeader');
  await header.click();
  const entry = (name: string) =>
    row.locator('.jp-ShareFilesPanel-entry', { hasText: name });
  await expect(row.locator('.jp-ShareFilesPanel-entryName')).toHaveText([
    'a.csv',
    'd/'
  ]);
  // the owner's controls are not the connecting user's; the lab downloads
  // nothing to the browser
  expect(await menuOf(page, header)).toEqual([
    'Open in Browser',
    'Download to Current Folder',
    'Save as Zip',
    'Disconnect'
  ]);
  expect(await menuOf(page, entry('a.csv'))).toEqual([
    'Download to Current Folder',
    'Copy'
  ]);

  const pick = async (target: any, label: string) => {
    await target.click({ button: 'right' });
    await page.locator('.lm-Menu .lm-Menu-item', { hasText: label }).click();
  };
  const lands = (path: string) =>
    expect
      .poll(() => page.contents.fileExists(`${tmpPath}/${path}`), {
        timeout: 15000,
        message: `${path} lands in the current folder`
      })
      .toBe(true);
  await pick(entry('a.csv'), 'Download to Current Folder');
  await lands('a.csv');
  await pick(entry('d/'), 'Download to Current Folder');
  await lands('d/x.txt');
  await pick(header, 'Download to Current Folder');
  await lands('Their-Share/d/x.txt');
  await pick(header, 'Save as Zip');
  await lands('Their-Share.zip');
  // the message names what landed, as the save of an own record does
  await expect
    .poll(() =>
      notes(
        page,
        'success',
        `Saved Their Share to ./${tmpPath}/Their-Share.zip`
      )
    )
    .toBe(1);
  // the owner's bytes landed, and the staging each save wrote into is gone
  const saved = await api(
    page,
    'GET',
    `/api/contents/${tmpPath}/d/x.txt?type=file&format=text&content=1`
  );
  expect(saved.data.content).toBe('d/x.txt\n');
  const listing = await page.contents.getContentMetadata(tmpPath, 'directory');
  expect(
    (listing?.content || [])
      .map((c: any) => c.name)
      .filter((n: string) => n.startsWith('.'))
  ).toEqual([]);
});

test('a connected request takes a dropped file and folder and tints its row while they upload', async ({
  page,
  request
}) => {
  // ACC-HUBM-180, and ACC-PROG-172 for the tint: each file waits 3 s for
  // the link's answer, so the lab has sent half the bytes, then all of them
  await request.post(`${HUB}/_control/add`, { data: { seconds: 3 } });
  await page.contents.uploadContent('a', 'text', 'conn-up.txt');
  await page.contents.uploadContent('b', 'text', 'conn-updir/in.txt');
  const made = await foreign(request, {
    kind: 'request',
    title: 'Their Inbox'
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Their Inbox');
  await expect(row).toHaveCount(1);

  await dragOnto(page, 'conn-up.txt', row, ['conn-updir']);

  const header = row.locator('.jp-ShareFilesPanel-itemHeader');
  const layer = header.locator('.jp-ShareFilesPanel-itemProgress');
  await expect(layer).toHaveAttribute('aria-valuenow', '50');
  // the panel reads the row again while the lab sends, and the tint follows
  await expect(layer).toHaveAttribute('aria-valuenow', '100');
  await expect(header.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    'uploading'
  );

  // both files land: the tint goes and the count is said
  await expect(layer).toHaveCount(0, { timeout: 15000 });
  await expect
    .poll(() => notes(page, 'success', '2 item(s) uploaded to Their Inbox'))
    .toBe(1);
  await expect(header.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    'request'
  );
  // one file per request, under its own name and without the hub token: the
  // folder's file arrives without its folder
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  const sent = calls.filter((c: any) => c.path.endsWith('/u'));
  expect(sent.map((c: any) => c.filename).sort()).toEqual([
    'conn-up.txt',
    'in.txt'
  ]);
  expect(sent.every((c: any) => c.auth === '')).toBe(true);
  await page.contents.deleteFile('conn-up.txt');
  await page.contents.deleteDirectory('conn-updir');
});

test('a second drop on a connected request joins the running upload', async ({
  page,
  request
}) => {
  // ACC-HUBM-222, DEF-HUB-139: one upload, both files, and no refusal
  await request.post(`${HUB}/_control/add`, { data: { seconds: 4 } });
  await page.contents.uploadContent('a', 'text', 'join-one.txt');
  await page.contents.uploadContent('b', 'text', 'join-two.txt');
  const made = await foreign(request, {
    kind: 'request',
    title: 'Joined Inbox'
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Joined Inbox');
  await expect(row).toHaveCount(1);

  await dragOnto(page, 'join-one.txt', row);
  const header = row.locator('.jp-ShareFilesPanel-itemHeader');
  await expect(header.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    'uploading'
  );
  await dragOnto(page, 'join-two.txt', row);

  await expect
    .poll(() => notes(page, 'success', '2 item(s) uploaded to Joined Inbox'), {
      timeout: 20000
    })
    .toBe(1);
  expect(await notes(page, 'error', 'Upload failed')).toBe(0);
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  expect(
    calls.filter((c: any) => c.path.endsWith('/u')).map((c: any) => c.filename)
  ).toEqual(['join-one.txt', 'join-two.txt']);
  await page.contents.deleteFile('join-one.txt');
  await page.contents.deleteFile('join-two.txt');
});

test('each file of an upload tints its own row by the fraction sent', async ({
  page,
  request
}) => {
  // ACC-PROG-216, ACC-PROG-218, ACC-PROG-229: the request reads the large
  // file no further than 4 MB, so the lab has sent part of it and waits; the
  // first file has landed and the third has not started
  const big = 'rows-2-big.bin';
  fs.writeFileSync(
    path.join(process.env.JUPYTERLAB_GALATA_ROOT_DIR!, big),
    Buffer.alloc(64 * 1024 * 1024)
  );
  await page.contents.uploadContent('a', 'text', 'rows-1-first.txt');
  await page.contents.uploadContent('c', 'text', 'rows-3-last.txt');
  await request.post(`${HUB}/_control/receive`, {
    data: { hold: 4 * 1024 * 1024 }
  });
  const made = await foreign(request, {
    kind: 'request',
    title: 'Rows Inbox'
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Rows Inbox');
  await expect(row).toHaveCount(1);
  const twisty = row.locator('.jp-ShareFilesPanel-itemTwisty');
  await expect(twisty).toHaveAttribute('aria-expanded', 'false');
  const [conn] = (await api(page, 'GET', `${API}/connections`)).data
    .connections;
  const entry = (name: string) =>
    row.locator('.jp-ShareFilesPanel-entry', { hasText: name });
  const layer = (name: string) =>
    entry(name).locator('.jp-ShareFilesPanel-itemProgress');

  // the panel's own upload, as a drop calls it, with the files in this order
  await page.evaluate(
    async ([panelId, key, chosen]: [string, string, string[]]) => {
      const { shell } = (window as any).jupyterapp;
      const panel = Array.from<any>(shell.widgets('right')).find(
        w => w.id === panelId
      );
      await panel.uploadToConnectionFlow(key, chosen);
    },
    [PANEL.slice(1), conn.key, ['rows-1-first.txt', big, 'rows-3-last.txt']]
  );

  await expect(twisty).toHaveAttribute('aria-expanded', 'true');
  await expect(layer(big)).toHaveAttribute('aria-valuenow', /^[1-9]\d?$/);
  await expect(layer(big)).toHaveAttribute('aria-label', `Uploading ${big}`);
  await expect(entry('rows-1-first.txt')).toHaveCount(1);
  await expect(layer('rows-1-first.txt')).toHaveCount(0);
  await expect(layer('rows-3-last.txt')).toHaveAttribute('aria-valuenow', '0');
  // the row header wears the whole upload
  await expect(
    row.locator(
      '.jp-ShareFilesPanel-itemHeader .jp-ShareFilesPanel-itemProgress'
    )
  ).toHaveCount(1);
  // a row that reads offline keeps its last manifest and draws no file of
  // the upload from it. The lab answers a running upload's reads from the
  // page it read last, so here the lab itself does not answer
  await page.route('**/manifest*', (route: any) => route.abort());
  const badge = row.locator('.jp-ShareFilesPanel-offline');
  await expect(badge).toHaveText('offline');
  await expect(row.locator('.jp-ShareFilesPanel-entry')).toHaveCount(0);
  await page.unroute('**/manifest*');
  await refreshPanel(page);
  await expect(badge).toHaveCount(0);
  await expect(layer('rows-3-last.txt')).toHaveAttribute('aria-valuenow', '0');

  await request.post(`${HUB}/_control/receive`, { data: { hold: 0 } });
  await expect
    .poll(() => notes(page, 'success', '3 item(s) uploaded to Rows Inbox'), {
      timeout: 20000
    })
    .toBe(1);
  await expect(row.locator('.jp-ShareFilesPanel-entry')).toHaveCount(0);
  for (const name of ['rows-1-first.txt', big, 'rows-3-last.txt']) {
    await page.contents.deleteFile(name);
  }
});

test('a request row the owner closes during an upload stays closed', async ({
  page,
  request
}) => {
  // ACC-PROG-229, as for an add: the upload opens the row once, and the
  // reads that follow it do not open it again. Each file waits 3 s for the
  // link's answer
  await request.post(`${HUB}/_control/add`, { data: { seconds: 3 } });
  await page.contents.uploadContent('a', 'text', 'shut-up-1.txt');
  await page.contents.uploadContent('b', 'text', 'shut-up-2.txt');
  const made = await foreign(request, {
    kind: 'request',
    title: 'Shut Inbox'
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Shut Inbox');
  await expect(row).toHaveCount(1);
  const twisty = row.locator('.jp-ShareFilesPanel-itemTwisty');
  await expect(twisty).toHaveAttribute('aria-expanded', 'false');

  await dragOnto(page, 'shut-up-1.txt', row, ['shut-up-2.txt']);
  await expect(twisty).toHaveAttribute('aria-expanded', 'true');

  const header = row.locator('.jp-ShareFilesPanel-itemHeader');
  await header.click();
  await expect(twisty).toHaveAttribute('aria-expanded', 'false');
  const reads: string[] = [];
  page.on('request', (r: any) => {
    if (r.method() === 'GET' && /\/manifest(\?|$)/.test(r.url())) {
      reads.push(r.url());
    }
  });
  // the row is read again each second while the lab sends, and each read
  // renders it
  await expect.poll(() => reads.length, { timeout: 5000 }).toBeGreaterThan(1);
  await expect(header.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    'uploading'
  );
  await expect(twisty).toHaveAttribute('aria-expanded', 'false');
  await expect
    .poll(() => notes(page, 'success', '2 item(s) uploaded to Shut Inbox'), {
      timeout: 20000
    })
    .toBe(1);
  await expect(twisty).toHaveAttribute('aria-expanded', 'false');
  await page.contents.deleteFile('shut-up-1.txt');
  await page.contents.deleteFile('shut-up-2.txt');
});

test('an upload the hub refuses says why', async ({ page, request }) => {
  // ACC-HUBM-180
  await page.contents.uploadContent('x', 'text', 'refuse-upload.txt');
  const made = await foreign(request, { kind: 'request', title: 'Full Inbox' });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Full Inbox');
  await expect(row).toHaveCount(1);

  await dragOnto(page, 'refuse-upload.txt', row);

  await expect
    .poll(() => notes(page, 'error', 'The upload to Full Inbox was refused'))
    .toBe(1);
  // the row says it too
  const meta = row.locator('.jp-ShareFilesPanel-itemMeta');
  await expect(meta).toHaveText('request - upload refused');
  await expect(meta).toHaveClass(/jp-mod-refused/);
  // the drop opened the row, which says why - the limit is the request's,
  // not the uploader's group policy
  await expect(row.locator('.jp-ShareFilesPanel-empty').first()).toHaveText(
    'The hub refused the last upload - A file is larger than this request accepts.'
  );
  await page.contents.deleteFile('refuse-upload.txt');
});

test('an upload that ends before the panel reads it is still announced', async ({
  page,
  request
}) => {
  // ACC-HUBM-180: the file lands before the first read after the answer
  await request.post(`${HUB}/_control/add`, { data: { seconds: 0.01 } });
  await page.contents.uploadContent('q', 'text', 'quick-up.txt');
  const made = await foreign(request, {
    kind: 'request',
    title: 'Quick Inbox'
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Quick Inbox');
  await expect(row).toHaveCount(1);

  await dragOnto(page, 'quick-up.txt', row);

  await expect
    .poll(() => notes(page, 'success', '1 item(s) uploaded to Quick Inbox'))
    .toBe(1);
  await page.contents.deleteFile('quick-up.txt');
});

test('a password the owner changes after the connect is asked for again from the row', async ({
  page,
  request
}) => {
  // ACC-HUBM-176: the connection works again once the new password is given
  const made = await foreign(request, {
    title: 'Guarded quarterly figures',
    names: ['a.csv'],
    password: 'first'
  });
  await openPanel(page);
  const dialog = page.locator('.jp-Dialog');
  await connect(page, made.url);
  await dialog.locator('input[type="password"]').fill('first');
  await dialog.locator('button', { hasText: 'Connect' }).click();
  const row = connected(page, 'Guarded quarterly figures');
  await expect(row).toHaveCount(1);

  // the hub rings no stream for another user's record: the panel's poll
  // reads the link again
  await setPollInterval(page, 2);
  await request.post(`${HUB}/_control/password`, {
    data: { id: made.id, password: 'second' }
  });
  // the row itself names the state, and its hover names the step
  const badge = row.locator('.jp-ShareFilesPanel-offline');
  await expect(badge).toHaveText('password changed');
  // a label that wraps in a narrow sidebar stays inside its own row
  const header = await row
    .locator('.jp-ShareFilesPanel-itemHeader')
    .boundingBox();
  const label = await badge.boundingBox();
  expect(label!.y).toBeGreaterThanOrEqual(header!.y);
  expect(label!.y + label!.height).toBeLessThanOrEqual(
    header!.y + header!.height
  );
  await expect(badge).toHaveAttribute(
    'title',
    /^The owner set or changed this record's password - choose Connect Again/
  );
  await row
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: 'Connect Again...' })
    .click();
  await expect(dialog).toContainText('This link is password protected');
  await dialog.locator('input[type="password"]').fill('second');
  await dialog.locator('button', { hasText: 'Connect' }).click();
  await expect(badge).toHaveCount(0);
});

test('an upload stopped by a changed password stays true once the new one is given', async ({
  page,
  request
}) => {
  // ACC-HUBM-176
  await page.contents.uploadContent('p', 'text', 'pw-up.txt');
  const made = await foreign(request, {
    kind: 'request',
    title: 'Guarded Inbox',
    password: 'first'
  });
  await openPanel(page);
  const dialog = page.locator('.jp-Dialog');
  await connect(page, made.url);
  await dialog.locator('input[type="password"]').fill('first');
  await dialog.locator('button', { hasText: 'Connect' }).click();
  const row = connected(page, 'Guarded Inbox');
  await expect(row).toHaveCount(1);

  // the owner changes the password before the upload; the panel reads the
  // row again only after the drop, so the drop is sent
  await setPollInterval(page, 3600);
  await request.post(`${HUB}/_control/password`, {
    data: { id: made.id, password: 'second' }
  });
  await dragOnto(page, 'pw-up.txt', row);
  await setPollInterval(page, 2);
  const badge = row.locator('.jp-ShareFilesPanel-offline');
  await expect(badge).toHaveText('password changed');

  await row
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: 'Connect Again...' })
    .click();
  await dialog.locator('input[type="password"]').fill('second');
  await dialog.locator('button', { hasText: 'Connect' }).click();
  await expect(badge).toHaveCount(0);
  // the refused upload is told in the past tense, with no instruction the
  // user has just followed
  const said = "The owner had set or changed this record's password.";
  const meta = row.locator('.jp-ShareFilesPanel-itemMeta');
  await expect(meta).toHaveText('request - upload refused');
  await expect(meta).toHaveAttribute(
    'title',
    `The hub refused the last upload - ${said}\nrefused: password_changed`
  );
  await expect
    .poll(() =>
      notes(
        page,
        'error',
        `The upload to Guarded Inbox was refused after 0 item(s) uploaded: ${said}`
      )
    )
    .toBe(1);
  await page.contents.deleteFile('pw-up.txt');
});

test('a read sent before an upload was accepted does not announce it', async ({
  page,
  request
}) => {
  // ACC-HUBM-180: a read already on its way when the hub takes the upload
  // describes the record without it
  await request.post(`${HUB}/_control/add`, { data: { seconds: 3 } });
  await page.contents.uploadContent('r', 'text', 'race-up.txt');
  const made = await foreign(request, { kind: 'request', title: 'Race Inbox' });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Race Inbox');
  await expect(row).toHaveCount(1);

  // hold the next manifest read until the upload has been accepted
  let accepted: () => void = () => undefined;
  const upload = new Promise<void>(resolve => (accepted = resolve));
  let held = false;
  await page.route('**/api/connections/*/manifest*', async (route: any) => {
    if (held) {
      return route.continue();
    }
    held = true;
    const response = await route.fetch();
    await upload;
    await route.fulfill({ response });
  });
  await page.locator(`${PANEL} button[title="Refresh"]`).click();
  await expect.poll(() => held).toBe(true);
  const answered = page.waitForResponse((r: any) =>
    r.url().includes('/upload')
  );
  await dragOnto(page, 'race-up.txt', row);
  await answered;
  accepted();

  await expect
    .poll(() => notes(page, 'success', '1 item(s) uploaded to Race Inbox'), {
      timeout: 15000
    })
    .toBe(1);
  expect(await notes(page, 'success', '0 item(s) uploaded')).toBe(0);
  await page.unroute('**/api/connections/*/manifest*');
  await page.contents.deleteFile('race-up.txt');
});

test('a new connection opens the Connected section and shows its row', async ({
  page,
  request
}) => {
  // ACC-HUBM-173: a connect that lands out of sight says so on screen
  const made = await foreign(request, {
    title: 'Out Of Sight',
    names: ['a.csv']
  });
  await openPanel(page);
  const section = page.locator(`${PANEL} .jp-ShareFilesPanel-section`, {
    hasText: 'Connected'
  });
  const sectionHeader = section.locator('.jp-ShareFilesPanel-sectionHeader');
  await sectionHeader.click();
  await expect(sectionHeader).toHaveAttribute('aria-expanded', 'false');

  await connect(page, made.url);

  await expect(sectionHeader).toHaveAttribute('aria-expanded', 'true');
  await expect(connected(page, 'Out Of Sight')).toBeInViewport();
});

test('disconnecting during an upload says the other files are not sent', async ({
  page,
  request
}) => {
  // ACC-HUBM-179
  await request.post(`${HUB}/_control/add`, { data: { seconds: 2 } });
  await page.contents.uploadContent('1', 'text', 'stop-a.txt');
  await page.contents.uploadContent('2', 'text', 'stop-b.txt');
  const made = await foreign(request, { kind: 'request', title: 'Stop Inbox' });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Stop Inbox');
  await expect(row).toHaveCount(1);

  await dragOnto(page, 'stop-a.txt', row, ['stop-b.txt']);
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    'uploading'
  );
  await row.locator('button[title="Disconnect"]').click();
  await expect(row).toHaveCount(0);
  await expect
    .poll(() =>
      notes(
        page,
        'info',
        'Disconnected from Stop Inbox: a file already on its way is still sent, and no further file of the upload is sent.'
      )
    )
    .toBe(1);
  await page.contents.deleteFile('stop-a.txt');
  await page.contents.deleteFile('stop-b.txt');
});

test('an upload into a request its owner closes meanwhile says it ended', async ({
  page,
  request
}) => {
  // ACC-HUBM-180
  await request.post(`${HUB}/_control/add`, { data: { seconds: 2 } });
  await page.contents.uploadContent('1', 'text', 'gone-a.txt');
  await page.contents.uploadContent('2', 'text', 'gone-b.txt');
  const made = await foreign(request, {
    kind: 'request',
    title: 'Vanishing Inbox'
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Vanishing Inbox');
  await expect(row).toHaveCount(1);

  await dragOnto(page, 'gone-a.txt', row, ['gone-b.txt']);
  await expect(row.locator('.jp-ShareFilesPanel-itemMeta')).toHaveText(
    'uploading'
  );
  await request.post(`${HUB}/_control/close`, { data: { id: made.id } });
  await expect(row.locator('.jp-ShareFilesPanel-offline')).toHaveText('closed');
  await expect
    .poll(() =>
      notes(
        page,
        'error',
        'The upload to Vanishing Inbox ended: The owner closed this share or request, or it expired.'
      )
    )
    .toBe(1);
  // said once, not at every read of the closed row: two more polls read it
  await setPollInterval(page, 2);
  await page.waitForTimeout(4500);
  expect(
    await notes(page, 'error', 'The upload to Vanishing Inbox ended')
  ).toBe(1);
  await page.contents.deleteFile('gone-a.txt');
  await page.contents.deleteFile('gone-b.txt');
});

test('a file over the request limit is named and nothing is sent', async ({
  page,
  request
}) => {
  // ACC-HUBM-180: the request page states its limit; the lab reads it with
  // the row and refuses before sending
  await request.post(`${HUB}/_control/capabilities`, {
    data: { max_upload_bytes: 2 }
  });
  await page.contents.uploadContent('too big', 'text', 'big-up.txt');
  const made = await foreign(request, {
    kind: 'request',
    title: 'Small Inbox'
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Small Inbox');
  await expect(row).toHaveCount(1);

  await dragOnto(page, 'big-up.txt', row);
  await expect
    .poll(() =>
      notes(
        page,
        'warning',
        'The upload was not sent: This request accepts files up to 2 B; big-up.txt is larger'
      )
    )
    .toBe(1);
  expect(await notes(page, 'error', 'Upload failed')).toBe(0);
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  expect(calls.filter((c: any) => c.path.endsWith('/u'))).toEqual([]);
  await page.contents.deleteFile('big-up.txt');
});

test('a drop onto a request that does not answer for now says the upload started', async ({
  page,
  request
}) => {
  // ACC-HUBM-180: a plain 'offline' row still takes the upload
  await page.contents.uploadContent('q', 'text', 'quiet-up.txt');
  const made = await foreign(request, {
    kind: 'request',
    title: 'Quiet Inbox'
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Quiet Inbox');
  await expect(row).toHaveCount(1);
  await setPollInterval(page, 2);
  await request.post(`${HUB}/_control/outage`, {
    data: { id: made.id, status: 503 }
  });
  const badge = row.locator('.jp-ShareFilesPanel-offline');
  await expect(badge).toHaveText('offline');

  await dragOnto(page, 'quiet-up.txt', row);
  await expect
    .poll(() => notes(page, 'info', 'The upload to Quiet Inbox has started.'))
    .toBe(1);
  // the record answers again: the end of the upload is announced
  await request.post(`${HUB}/_control/outage`, {
    data: { id: made.id, status: 200 }
  });
  await expect(badge).toHaveCount(0);
  await expect
    .poll(() => notes(page, 'success', '1 item(s) uploaded to Quiet Inbox'))
    .toBe(1);
  await page.contents.deleteFile('quiet-up.txt');
});

test('a drop onto a closed request says it was not sent', async ({
  page,
  request
}) => {
  // ACC-HUBM-180
  await page.contents.uploadContent('c', 'text', 'late-up.txt');
  const made = await foreign(request, {
    kind: 'request',
    title: 'Closed Inbox'
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Closed Inbox');
  await expect(row).toHaveCount(1);
  await setPollInterval(page, 2);
  await request.post(`${HUB}/_control/close`, { data: { id: made.id } });
  await expect(row.locator('.jp-ShareFilesPanel-offline')).toHaveText('closed');

  await dragOnto(page, 'late-up.txt', row);
  await expect
    .poll(() =>
      notes(
        page,
        'warning',
        'The upload was not sent: The owner closed this share or request, or it expired.'
      )
    )
    .toBe(1);
  // a cut pasted onto the row is refused the same way, and the clip is kept
  // for the next try
  await page.filebrowser.openHomeDirectory();
  await page.filebrowser.revealFileInBrowser('late-up.txt');
  await page
    .getByRole('region', { name: 'File Browser Section' })
    .getByRole('listitem', { name: /^Name: late-up.txt/ })
    .click();
  await page.keyboard.press('ControlOrMeta+x');
  const header = row.locator('.jp-ShareFilesPanel-itemHeader');
  await header.focus();
  await page.keyboard.press('ContextMenu');
  await page.locator('.lm-Menu-item', { hasText: 'Paste' }).click();
  await expect
    .poll(() => notes(page, 'warning', 'The upload was not sent'))
    .toBe(2);
  expect(await notes(page, 'info', 'pasted as a copy')).toBe(0);
  await header.focus();
  await page.keyboard.press('ContextMenu');
  await expect(page.locator('.lm-Menu-item', { hasText: 'Paste' })).toHaveCount(
    1
  );
  await page.keyboard.press('Escape');
  const calls = (await (await request.get(`${HUB}/_control/calls`)).json())
    .calls;
  expect(calls.filter((c: any) => c.path.endsWith('/u'))).toEqual([]);
  await page.contents.deleteFile('late-up.txt');
});

test('disconnecting leaves the record, and a closed record says why', async ({
  page,
  request
}) => {
  // ACC-HUBM-179
  const made = await foreign(request, {
    title: 'Comes And Goes',
    names: ['a.csv']
  });
  await openPanel(page);
  await connect(page, made.url);
  const row = connected(page, 'Comes And Goes');
  await expect(row).toHaveCount(1);
  await row.locator('button[title="Disconnect"]').click();
  await expect(row).toHaveCount(0);
  // the record is still on the hub: it connects again
  await connect(page, made.url);
  await expect(row).toHaveCount(1);

  // the hub rings no stream for another user's record: the panel's poll
  // reads the link again, with no Refresh
  await setPollInterval(page, 2);
  await request.post(`${HUB}/_control/close`, { data: { id: made.id } });
  const badge = row.locator('.jp-ShareFilesPanel-offline');
  await expect(badge).toHaveText('closed');
  await expect(badge).toHaveAttribute(
    'title',
    /The owner closed this share or request, or it expired/
  );
});
