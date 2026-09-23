import { expect, test } from '@jupyterlab/galata';

/**
 * Keyboard access to the panel: the header cloud icon is a toggle button,
 * and a row header, a connection row and a connected peer's entry take focus
 * and open their context menu with Shift+F10 or the ContextMenu key, and a
 * row's toggle button expands it. Also the row buttons while they hold the
 * focus, the focus after a re-render or
 * a dialog, and the cloud icon tooltip and colour in each state. The tunnel
 * state and the peer are answered in the browser, so no tunnel starts and no
 * second lab is needed.
 */

const API = '/jupyterlab-share-files-extension/api';
const PANEL = '#jupyterlab-share-files-extension-panel';

async function openPanel(page: any): Promise<void> {
  await page.sidebar.openTab('jupyterlab-share-files-extension-panel');
  const refresh = page.locator(`${PANEL} button[title="Refresh"]`);
  await refresh.click();
  // tests run side by side; a refresh can outlast the 5 s default wait
  await expect(refresh).not.toHaveClass(/jp-mod-spinning/, { timeout: 30000 });
}

async function createShare(page: any, name: string): Promise<void> {
  await page.evaluate(
    async ([api, n]: [string, string]) => {
      await fetch(`${api}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n, paths: [] })
      });
    },
    [API, name]
  );
}

test('the cloud icon switches from the keyboard', async ({ page }) => {
  let active = false;
  await page.route(`**${API}/info*`, async (route: any) => {
    const response = await route.fetch();
    const json = await response.json();
    await route.fulfill({
      response,
      json: { ...json, tunnel_configured: true, tunnel_active: active }
    });
  });
  await page.route(`**${API}/tunnel*`, async (route: any) => {
    active = !!route.request().postDataJSON().active;
    await route.fulfill({
      json: {
        tunnel_configured: true,
        tunnel_active: active,
        tunnel_autostart: false,
        tunnel_running: active
      }
    });
  });
  await openPanel(page);
  const cloud = page.locator(`${PANEL} .jp-ShareFilesPanel-cloudIndicator`);
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
});

test('a share row opens its context menu from the keyboard and gets the focus back', async ({
  page
}) => {
  const name = `keyboard-share-${Date.now()}`;
  await createShare(page, name);
  await openPanel(page);
  const header = page
    .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name })
    .locator('.jp-ShareFilesPanel-itemHeader');
  await expect(header).toHaveAttribute('tabindex', '0');
  const remove = page.locator('.lm-Menu .lm-Menu-item', {
    hasText: 'Delete Share'
  });
  for (const key of ['Shift+F10', 'ContextMenu']) {
    await header.focus();
    await page.keyboard.press(key);
    await expect(remove, key).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(remove, key).toBeHidden();
    await expect(header, key).toBeFocused();
  }
});

test('a share row expands from the keyboard through its toggle button', async ({
  page
}) => {
  const name = `keyboard-toggle-${Date.now()}`;
  await createShare(page, name);
  await openPanel(page);
  const item = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: name
  });
  const header = item.locator('.jp-ShareFilesPanel-itemHeader');
  // the glyph is a named button, so a reader speaks its name and state,
  // not the triangle
  const toggle = header.getByRole('button', { name: 'Expand' });
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(item.locator('.jp-ShareFilesPanel-entryList')).toHaveCount(0);
  await header.focus();
  await page.keyboard.press('Tab');
  await expect(toggle).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(item.locator('.jp-ShareFilesPanel-entryList')).toHaveCount(1);
  // the rebuilt row hands the focus back to its toggle
  const collapse = header.getByRole('button', { name: 'Collapse' });
  await expect(collapse).toHaveAttribute('aria-expanded', 'true');
  await expect(collapse).toBeFocused();
  await page.keyboard.press('Space');
  await expect(item.locator('.jp-ShareFilesPanel-entryList')).toHaveCount(0);
  await expect(header.getByRole('button', { name: 'Expand' })).toBeFocused();
});

test('the row buttons show while they have keyboard focus', async ({
  page
}) => {
  const name = `keyboard-buttons-${Date.now()}`;
  await createShare(page, name);
  await openPanel(page);
  const header = page
    .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name })
    .locator('.jp-ShareFilesPanel-itemHeader');
  await header.focus();
  // the toggle sits before the two icon buttons
  await page.keyboard.press('Tab');
  await expect(header.getByRole('button', { name: 'Expand' })).toBeFocused();
  for (const title of ['Copy link', 'Delete share']) {
    await page.keyboard.press('Tab');
    const button = header.locator(`button[title="${title}"]`);
    await expect(button).toBeFocused();
    await expect(button).toHaveCSS('opacity', '1');
  }
});

test('the entry buttons show while they have keyboard focus', async ({
  page
}) => {
  const name = `keyboard-entry-${Date.now()}`;
  const file = `${name}.txt`;
  await page.contents.uploadContent('x', 'text', file);
  await page.evaluate(
    async ([api, n, p]: [string, string, string]) => {
      await fetch(`${api}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n, paths: [p] })
      });
    },
    [API, name, file]
  );
  await openPanel(page);
  const item = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: name
  });
  const header = item.locator('.jp-ShareFilesPanel-itemHeader');
  await header.click();
  const remove = item.locator(
    '.jp-ShareFilesPanel-entry button[title="Remove"]'
  );
  await expect(remove).toHaveCount(1);
  await header.focus();
  // the row's toggle, Copy link and Delete share, then the entry row itself
  // (DEF-PANEL-60: it takes keyboard focus), then the entry's Remove
  for (let i = 0; i < 4; i++) {
    await page.keyboard.press('Tab');
  }
  const entryRow = item.locator('.jp-ShareFilesPanel-entry');
  await expect(entryRow).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(remove).toBeFocused();
  await expect(remove).toHaveCSS('opacity', '1');
});

test('a re-render keeps the scroll position after a row was clicked', async ({
  page
}) => {
  const prefix = `keyboard-scroll-${Date.now()}`;
  for (let i = 0; i < 30; i++) {
    await createShare(page, `${prefix}-${String(i).padStart(2, '0')}`);
  }
  await openPanel(page);
  const body = page.locator(`${PANEL} .jp-ShareFilesPanel-body`);
  await page
    .locator(`${PANEL} .jp-ShareFilesPanel-itemHeader`, {
      hasText: `${prefix}-00`
    })
    .click();
  await body.evaluate((el: HTMLElement) => (el.scrollTop = el.scrollHeight));
  const before = await body.evaluate((el: HTMLElement) => el.scrollTop);
  expect(before).toBeGreaterThan(0);
  // a click from script leaves the focus on the row header
  const refresh = page.locator(`${PANEL} button[title="Refresh"]`);
  await refresh.evaluate((el: HTMLElement) => el.click());
  await expect(refresh).not.toHaveClass(/jp-mod-spinning/, { timeout: 30000 });
  expect(await body.evaluate((el: HTMLElement) => el.scrollTop)).toBe(before);
});

test('every cloud icon tooltip is short and two lines at most', async ({
  page
}) => {
  let configured = false;
  let active = false;
  let answer = () => {};
  const answered = new Promise<void>(resolve => (answer = resolve));
  await page.route(`**${API}/info*`, async (route: any) => {
    const response = await route.fetch();
    const json = await response.json();
    await route.fulfill({
      response,
      json: { ...json, tunnel_configured: configured, tunnel_active: active }
    });
  });
  await page.route(`**${API}/tunnel*`, async (route: any) => {
    // held until the test has read the connecting tooltip
    await answered;
    active = !!route.request().postDataJSON().active;
    await route.fulfill({
      json: {
        tunnel_configured: true,
        tunnel_active: active,
        tunnel_autostart: false,
        tunnel_running: active
      }
    });
  });
  await openPanel(page);
  const cloud = page.locator(`${PANEL} .jp-ShareFilesPanel-cloudIndicator`);
  const titles: string[] = [];
  const read = async () => titles.push((await cloud.getAttribute('title'))!);
  await read(); // not configured
  configured = true;
  await openPanel(page);
  await expect(cloud).not.toHaveAttribute('title', titles[0]);
  await read(); // off
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-connecting/);
  await read(); // connecting
  // ACC-CLOUD-162: connecting and on carry the same accent, so the glyph is
  // what separates them - the dashed silhouette against the filled cloud
  const switching = await cloud.locator('svg').innerHTML();
  answer();
  await expect(cloud).toHaveClass(/jp-mod-active/);
  await read(); // on
  expect(await cloud.locator('svg').innerHTML()).not.toBe(switching);
  expect(new Set(titles).size).toBe(4);
  for (const title of titles) {
    const lines = title.split('\n');
    expect(lines.length, title).toBeLessThanOrEqual(2);
    for (const line of lines) {
      expect(line.length, line).toBeLessThanOrEqual(45);
    }
  }
});

test('both row buttons stay drawn while one of them has the focus', async ({
  page
}) => {
  const name = `keyboard-pair-${Date.now()}`;
  await createShare(page, name);
  await openPanel(page);
  const header = page
    .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name })
    .locator('.jp-ShareFilesPanel-itemHeader');
  await header.focus();
  await page.keyboard.press('Tab'); // the toggle
  await page.keyboard.press('Tab');
  await expect(header.locator('button[title="Copy link"]')).toBeFocused();
  await expect(header.locator('button[title="Delete share"]')).toHaveCSS(
    'opacity',
    '1'
  );
});

test('a re-render keeps the focus on a row button', async ({ page }) => {
  const name = `keyboard-rerender-${Date.now()}`;
  await createShare(page, name);
  await openPanel(page);
  const header = page
    .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name })
    .locator('.jp-ShareFilesPanel-itemHeader');
  await header.focus();
  await page.keyboard.press('Tab'); // the toggle
  await page.keyboard.press('Tab');
  const copy = header.locator('button[title="Copy link"]');
  await expect(copy).toBeFocused();
  // a click from script leaves the focus where the keyboard put it
  const refresh = page.locator(`${PANEL} button[title="Refresh"]`);
  await refresh.evaluate((el: HTMLElement) => el.click());
  await expect(refresh).not.toHaveClass(/jp-mod-spinning/, { timeout: 30000 });
  await expect(copy).toBeFocused();
});

test('the focus returns to the button a dialog was opened from', async ({
  page
}) => {
  const name = `keyboard-dialog-${Date.now()}`;
  await createShare(page, name);
  await openPanel(page);
  const header = page
    .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name })
    .locator('.jp-ShareFilesPanel-itemHeader');
  await header.focus();
  await page.keyboard.press('Tab'); // the toggle
  await page.keyboard.press('Tab');
  const copy = header.locator('button[title="Copy link"]');
  await expect(copy).toBeFocused();
  const dialog = page.locator('.jp-Dialog');
  await page.keyboard.press('Enter');
  await expect(dialog).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(copy).toBeFocused();
});

test('the cloud icon draws its state colour', async ({ page }) => {
  let active = false;
  await page.route(`**${API}/info*`, async (route: any) => {
    const response = await route.fetch();
    const json = await response.json();
    await route.fulfill({
      response,
      json: { ...json, tunnel_configured: true, tunnel_active: active }
    });
  });
  await page.route(`**${API}/tunnel*`, async (route: any) => {
    active = !!route.request().postDataJSON().active;
    await route.fulfill({
      json: {
        tunnel_configured: true,
        tunnel_active: active,
        tunnel_autostart: false,
        tunnel_running: active
      }
    });
  });
  await openPanel(page);
  const cloud = page.locator(`${PANEL} .jp-ShareFilesPanel-cloudIndicator`);
  await cloud.click();
  await expect(cloud).toHaveClass(/jp-mod-active/);
  // the icon paints in the state colour of its container, not the themed grey
  const colour = await cloud.evaluate(
    (el: HTMLElement) => getComputedStyle(el).color
  );
  const fill = await cloud
    .locator('svg')
    .evaluate((el: SVGElement) => getComputedStyle(el).fill);
  expect(fill).toBe(colour);
  // ACC-CLOUD-160: that colour is the accent the panel's other header icons
  // take when active, not a success green no other control carries
  const resolve = (name: string) =>
    page.evaluate((v: string) => {
      const probe = document.createElement('span');
      probe.style.color = `var(${v})`;
      document.body.appendChild(probe);
      const value = getComputedStyle(probe).color;
      probe.remove();
      return value;
    }, name);
  expect(colour).toBe(await resolve('--jp-brand-color1'));
  expect(colour).not.toBe(await resolve('--jp-success-color1'));
});

/** A connected peer share, answered in the browser: the connection list and
 * the peer's manifest. A real peer needs a second lab - the server refuses a
 * link that points at itself. */
const PEER =
  `http://localhost:${process.env.JUPYTER_TEST_PORT || '8888'}` +
  '/jupyterlab-share-files-extension/public/share/peer-one';

async function routePeer(page: any, entry: string): Promise<void> {
  await page.route(`**${API}/connections*`, async (route: any) => {
    if (route.request().method() !== 'GET') {
      return route.continue();
    }
    await route.fulfill({
      json: {
        connections: [
          {
            key: 'peer-one',
            kind: 'share',
            id: 'peer-one',
            host: 'localhost',
            name: 'Peer share',
            owner: 'peer',
            added_at: 0,
            link: PEER
          }
        ]
      }
    });
  });
  // the panel reads the peer through its own server (DEF-PEER-72)
  await page.route(`**${API}/connections/peer-one/manifest*`, (route: any) =>
    route.fulfill({
      json: {
        id: 'peer-one',
        name: 'Peer share',
        slug: 'peer-share',
        kind: 'share',
        created_at: 0,
        link: PEER,
        entries: [{ name: entry, type: 'file', size: 3 }]
      }
    })
  );
}

test('a connected peer share opens its menus from the keyboard', async ({
  page
}) => {
  await routePeer(page, 'peer.txt');
  let saved = 0;
  await page.route(`**${API}/connections/*/save*`, async (route: any) => {
    saved += 1;
    await route.fulfill({ json: { ok: true, saved: ['peer.txt'] } });
  });
  await openPanel(page);
  const item = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Peer share'
  });
  const header = item.locator('.jp-ShareFilesPanel-itemHeader');
  await expect(header).toHaveAttribute('tabindex', '0');
  const disconnect = page.locator('.lm-Menu .lm-Menu-item', {
    hasText: 'Disconnect'
  });
  for (const key of ['Shift+F10', 'ContextMenu']) {
    await header.focus();
    await page.keyboard.press(key);
    await expect(disconnect, key).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(disconnect, key).toBeHidden();
    await expect(header, key).toBeFocused();
  }
  // the row's own button, then the entry rows of the expanded row
  await header.click();
  const entry = item.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'peer.txt'
  });
  await expect(entry).toHaveCount(1);
  await header.focus();
  await page.keyboard.press('Tab'); // the toggle
  await page.keyboard.press('Tab');
  const disconnectBtn = header.locator('button[title="Disconnect"]');
  await expect(disconnectBtn).toBeFocused();
  await expect(disconnectBtn).toHaveCSS('opacity', '1');
  await page.keyboard.press('Tab');
  await expect(entry).toBeFocused();
  // Download, Save to Current Folder, then Copy: the second item saves
  await page.keyboard.press('Shift+F10');
  const save = page.locator('.lm-Menu .lm-Menu-item', {
    hasText: 'Save to Current Folder'
  });
  await expect(save).toBeVisible();
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect.poll(() => saved).toBe(1);
  await expect(entry).toBeFocused();
});

async function createRequest(page: any, name: string): Promise<void> {
  await page.evaluate(
    async ([api, n]: [string, string]) => {
      await fetch(`${api}/requests`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n })
      });
    },
    [API, name]
  );
}

test('a keyboard-focused row stays in view when a re-render adds rows above it', async ({
  page
}) => {
  const prefix = `keyboard-view-${Date.now()}`;
  for (let i = 0; i < 40; i++) {
    await createRequest(page, `${prefix}-${String(i).padStart(2, '0')}`);
  }
  await openPanel(page);
  const body = page.locator(`${PANEL} .jp-ShareFilesPanel-body`);
  // the list is longer than the panel and scrolled to its end
  expect(
    await body.evaluate((el: HTMLElement) => {
      el.scrollTop = el.scrollHeight;
      return el.scrollHeight - el.clientHeight;
    })
  ).toBeGreaterThan(0);
  // the last two rows that are fully visible: click the first (mouse focus),
  // then four Tab presses (the toggle, Copy link, Delete request, the next
  // header) put the keyboard focus on the second
  const visible = await body.evaluate((el: HTMLElement) => {
    const bottom = el.getBoundingClientRect().bottom;
    return Array.from(el.querySelectorAll<HTMLElement>('[data-row-key]'))
      .filter(r => r.getBoundingClientRect().bottom <= bottom)
      .map(r => r.dataset.rowKey!);
  });
  expect(visible.length).toBeGreaterThan(2);
  const above = visible[visible.length - 2];
  const target = visible[visible.length - 1];
  const row = (key: string) => body.locator(`[data-row-key="${key}"]`);
  await row(above).click();
  await row(above).click(); // collapse again: the list is as it was measured
  // the toggle, Copy link, Delete request, then the next row
  for (let i = 0; i < 4; i++) {
    await page.keyboard.press('Tab');
  }
  await expect(row(target)).toBeFocused();
  for (let i = 0; i < 10; i++) {
    await createShare(page, `${prefix}-share-${String(i).padStart(2, '0')}`);
  }
  const refresh = page.locator(`${PANEL} button[title="Refresh"]`);
  await refresh.evaluate((el: HTMLElement) => el.click());
  await expect(refresh).not.toHaveClass(/jp-mod-spinning/, { timeout: 30000 });
  await expect(row(target)).toBeFocused();
  const seen = await body.evaluate((el: HTMLElement) => ({
    row: (document.activeElement as HTMLElement).getBoundingClientRect().bottom,
    list: el.getBoundingClientRect().bottom,
    scrollTop: el.scrollTop
  }));
  expect(seen.row, JSON.stringify(seen)).toBeLessThanOrEqual(seen.list);
});

test("a file row of the owner's own share opens its menu from the keyboard", async ({
  page
}) => {
  const name = `keyboard-own-entry-${Date.now()}`;
  const file = `${name}.txt`;
  await page.contents.uploadContent('x', 'text', file);
  await page.evaluate(
    async ([api, n, p]: [string, string, string]) => {
      await fetch(`${api}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n, paths: [p] })
      });
    },
    [API, name, file]
  );
  await openPanel(page);
  const item = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: name
  });
  await item.locator('.jp-ShareFilesPanel-itemHeader').click();
  const entry = item.locator('.jp-ShareFilesPanel-entry', { hasText: file });
  await expect(entry).toHaveAttribute('tabindex', '0');
  const copyToCwd = page.locator('.lm-Menu .lm-Menu-item', {
    hasText: 'Save to Current Folder'
  });
  for (const key of ['Shift+F10', 'ContextMenu']) {
    await entry.focus();
    await page.keyboard.press(key);
    await expect(copyToCwd, key).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(copyToCwd, key).toBeHidden();
    await expect(entry, key).toBeFocused();
  }
});

test('a confirmed Delete hands the focus to the neighbouring row', async ({
  page
}) => {
  const prefix = `keyboard-delete-${Date.now()}`;
  await createShare(page, `${prefix}-one`);
  await createShare(page, `${prefix}-two`);
  await openPanel(page);
  const one = page.locator(`${PANEL} .jp-ShareFilesPanel-itemHeader`, {
    hasText: `${prefix}-one`
  });
  const two = page.locator(`${PANEL} .jp-ShareFilesPanel-itemHeader`, {
    hasText: `${prefix}-two`
  });
  await one.focus();
  await page.keyboard.press('Tab'); // the toggle
  await page.keyboard.press('Tab'); // Copy link
  await page.keyboard.press('Tab'); // Delete share
  await page.keyboard.press('Enter');
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toBeVisible();
  // a share another test creates meanwhile sorts above these rows and shifts
  // every position below it (DEF-TESTS-107)
  await createShare(page, `${prefix}-alpha`);
  await dialog.locator('button', { hasText: 'Delete' }).click();
  await expect(dialog).toBeHidden();
  await expect(one).toHaveCount(0);
  // the row that took the deleted row's place holds the focus
  await expect(two).toBeFocused();
});

test('a confirmed Delete of the only row hands the focus to its section header', async ({
  page
}) => {
  const name = `keyboard-delete-last-${Date.now()}`;
  await createShare(page, name);
  await openPanel(page);
  // the other tests' rows are filtered away: this share is the only row
  // (the filter box is hidden until the toolbar's Toggle filter shows it)
  await page.locator(`${PANEL} button[title="Toggle filter"]`).click();
  await page.locator(`${PANEL} .jp-ShareFilesPanel-filterInput`).fill(name);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-itemHeader`, {
    hasText: name
  });
  await expect(page.locator(`${PANEL} [data-row-key]`)).toHaveCount(1);
  await row.focus();
  await page.keyboard.press('Tab'); // the toggle
  await page.keyboard.press('Tab'); // Copy link
  await page.keyboard.press('Tab'); // Delete share
  await page.keyboard.press('Enter');
  const dialog = page.locator('.jp-Dialog');
  await dialog.locator('button', { hasText: 'Delete' }).click();
  await expect(dialog).toBeHidden();
  await expect(row).toHaveCount(0);
  // no row is left: the section header keeps the focus in the panel, and
  // Enter still folds the section
  const header = page.locator(`${PANEL} .jp-ShareFilesPanel-sectionHeader`, {
    hasText: 'My Shares'
  });
  await expect(header).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(header.locator('.jp-ShareFilesPanel-sectionTwisty')).toHaveText(
    '▸'
  );
  await expect(header).toBeFocused();
});

test("a clicked row's buttons hide again once the pointer leaves", async ({
  page
}) => {
  const name = `keyboard-mouse-${Date.now()}`;
  await createShare(page, name);
  await openPanel(page);
  const header = page.locator(`${PANEL} .jp-ShareFilesPanel-itemHeader`, {
    hasText: name
  });
  const copy = header.locator('button[title="Copy link"]');
  await header.click();
  // the click focused the row, but not by keyboard: no rule keeps it lit
  // (settle past the 0.1 s opacity transition before reading)
  await page.mouse.move(0, 0);
  await page.waitForTimeout(300);
  await expect(copy).toHaveCSS('opacity', '0');
  await header.hover();
  await expect(copy).toHaveCSS('opacity', '1');
});
