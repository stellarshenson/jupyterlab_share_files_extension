import { expect, test } from '@jupyterlab/galata';

import { expectDangerItem } from './helpers/danger';

/**
 * Panel rows: the indent of a request upload, the size of the collapse
 * triangles, Remove in the right-click menu of an own share, folder and
 * request upload, and the transfer layer a row wears while its download
 * runs (ACC-ROWS-235, ACC-ROWS-236, ACC-ROWS-238, ACC-ROWS-239,
 * ACC-ROWS-242). Standalone mode only.
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

async function createShare(
  page: any,
  name: string,
  paths: string[]
): Promise<string> {
  return page.evaluate(
    async ([api, n, ps]: [string, string, string[]]) => {
      const r = await fetch(`${api}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n, paths: ps })
      });
      return (await r.json()).id as string;
    },
    [API, name, paths]
  );
}

async function createRequest(page: any, name: string): Promise<string> {
  return page.evaluate(
    async ([api, n]: [string, string]) => {
      const r = await fetch(`${api}/requests`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n })
      });
      return (await r.json()).id as string;
    },
    [API, name]
  );
}

/** Another uploader's identity for one request: the server keys an uploader
 * by its `sf_uploader_<id>` cookie, so replacing the cookie before the next
 * upload makes the page a different person. */
async function becomeUploader(
  page: any,
  id: string,
  hash: string
): Promise<void> {
  await page.context().addCookies([
    {
      name: `sf_uploader_${id}`,
      value: hash,
      url: new URL(page.url()).origin
    }
  ]);
}

/** One recipient upload: the first response mints the uploader cookie, the
 * next same-origin fetch reuses it. */
async function uploadToRequest(
  page: any,
  id: string,
  name: string,
  body: string,
  uploader = 'probe'
): Promise<void> {
  await page.evaluate(
    async ([id, name, body, uploader]: [string, string, string, string]) => {
      const r = await fetch(
        `/jupyterlab-share-files-extension/public/request/${id}/upload?uploader=${encodeURIComponent(uploader)}`,
        {
          method: 'POST',
          headers: { 'X-Filename': encodeURIComponent(name) },
          body
        }
      );
      if (!r.ok) {
        throw new Error(await r.text());
      }
    },
    [id, name, body, uploader]
  );
}

/** The menu entries a right-click on `target` opens, closed again. */
async function menuOf(page: any, target: any): Promise<string[]> {
  await target.click({ button: 'right' });
  const items = page.locator('.lm-Menu .lm-Menu-itemLabel');
  await expect(items.first()).toBeVisible();
  const labels = (await items.allTextContents()).filter((t: string) => t);
  await page.keyboard.press('Escape');
  return labels;
}

test('the collapse triangles paint at 14 px in a 20 px box and the rows keep their height', async ({
  page,
  tmpPath
}) => {
  // ACC-ROWS-236, DEF-PANEL-147
  await page.contents.uploadContent('x\n', 'text', `${tmpPath}/t.txt`);
  await createShare(page, 'Twisty', [`${tmpPath}/t.txt`]);
  await openPanel(page);
  const size = async (locator: any) => {
    const box = await locator.boundingBox();
    const fontSize = await locator.evaluate(
      (el: Element) => getComputedStyle(el).fontSize
    );
    return { box, fontSize: parseFloat(fontSize) };
  };
  // a section header's caret
  const section = page
    .locator(`${PANEL} .jp-ShareFilesPanel-sectionTwisty`)
    .first();
  const caret = await size(section);
  expect(caret.box.width).toBeGreaterThanOrEqual(20);
  expect(caret.box.height).toBeGreaterThanOrEqual(20);
  expect(caret.fontSize).toBeGreaterThanOrEqual(12);
  // a share row's toggle
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Twisty'
  });
  const toggle = await size(row.locator('.jp-ShareFilesPanel-itemTwisty'));
  expect(toggle.box.width).toBeGreaterThanOrEqual(20);
  expect(toggle.box.height).toBeGreaterThanOrEqual(20);
  expect(toggle.fontSize).toBeGreaterThanOrEqual(12);
  // the header did not grow out of line with the 24 px rows
  const header = await page
    .locator(`${PANEL} .jp-ShareFilesPanel-itemHeader`)
    .filter({ hasText: 'Twisty' })
    .boundingBox();
  const strip = await page
    .locator(`${PANEL} .jp-ShareFilesPanel-sectionHeader`)
    .first()
    .boundingBox();
  expect(Math.round(header.height)).toBe(24);
  expect(Math.round(strip.height)).toBe(24);
});

test('a request upload row sits at its uploader group indent and no row takes more than one indent step', async ({
  page
}) => {
  // ACC-ROWS-235, DEF-PANEL-146
  const id = await page.evaluate(async (api: string) => {
    const r = await fetch(`${api}/requests`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'Indented' })
    });
    return (await r.json()).id as string;
  }, API);
  const name = 'INTERNAL_Hackathon_Dataset.csv';
  await uploadToRequest(page, id, name, 'a name of 30 characters\n');
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Indented'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const entry = row.locator('.jp-ShareFilesPanel-entry', { hasText: name });
  await expect(entry).toBeVisible();
  // the row's icon starts where its uploader group's icon starts
  const groupIcon = await row
    .locator('.jp-ShareFilesPanel-uploaderHeader .jp-ShareFilesPanel-entryIcon')
    .boundingBox();
  const entryIcon = await entry
    .locator('.jp-ShareFilesPanel-entryIcon')
    .boundingBox();
  expect(entryIcon.x).toBeCloseTo(groupIcon.x, 0);
  // no entry row takes more than one indent step
  const indents = await row
    .locator('.jp-ShareFilesPanel-entry')
    .evaluateAll((rows: Element[]) =>
      rows.map(
        r => r.querySelectorAll('.jp-ShareFilesPanel-entryIndent').length
      )
    );
  expect(indents.length).toBeGreaterThan(0);
  for (const count of indents) {
    expect(count).toBeLessThanOrEqual(1);
  }
});

test('Remove on a request upload leaves the other upload', async ({ page }) => {
  // ACC-ROWS-239
  // a title no other row's count line matches: 'Uploads' alone also matches
  // '0 uploads' on a request this test did not make
  const title = `Uploads ${Date.now().toString(36)}`;
  const id = await page.evaluate(
    async ([api, name]: [string, string]) => {
      const r = await fetch(`${api}/requests`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
      });
      return (await r.json()).id as string;
    },
    [API, title]
  );
  await uploadToRequest(page, id, 'keep.txt', 'keep\n');
  await uploadToRequest(page, id, 'drop.txt', 'drop\n');
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: title
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const entry = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'drop.txt'
  });
  expect(await menuOf(page, entry)).toEqual([
    'Download to Current Folder',
    'Show in File Browser',
    'Copy',
    'Remove'
  ]);
  // cancel once: both uploads stay
  const dialog = page.locator('.jp-Dialog');
  await entry.click({ button: 'right' });
  await page.locator('.lm-Menu .lm-Menu-item', { hasText: /^Remove$/ }).click();
  await expect(dialog).toContainText('Remove "drop.txt" from the request?');
  await dialog.locator('button.jp-mod-reject').click();
  await expect(dialog).toHaveCount(0);
  await expect(entry).toHaveCount(1);
  // accept: only that upload goes, the other keeps its place
  await entry.click({ button: 'right' });
  await page.locator('.lm-Menu .lm-Menu-item', { hasText: /^Remove$/ }).click();
  await expect(dialog).toContainText('Remove "drop.txt" from the request?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(entry).toHaveCount(0);
  const keep = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'keep.txt'
  });
  await expect(keep).toHaveCount(1);
  const held = await page.evaluate(
    async ([api, id]: [string, string]) => {
      const r = await fetch(`${api}/requests/${id}`);
      const data = await r.json();
      return data.uploaders.flatMap((u: any) =>
        u.entries.map((e: any) => e.name)
      );
    },
    [API, id]
  );
  expect(held).toEqual(['keep.txt']);
});

test('an uploader group folds with its twisty, the keyboard, and survives a panel refresh', async ({
  page
}) => {
  // ACC-ROWS-244
  const title = `Fold ${Date.now().toString(36)}`;
  const id = await createRequest(page, title);
  await uploadToRequest(page, id, 'ada.txt', 'ada\n', 'Ada');
  await becomeUploader(page, id, 'MZXW');
  await uploadToRequest(page, id, 'bob.txt', 'bob\n', 'Bob');
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: title
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const ada = row.locator('.jp-ShareFilesPanel-uploaderHeader', {
    hasText: 'Ada'
  });
  const bob = row.locator('.jp-ShareFilesPanel-uploaderHeader', {
    hasText: 'Bob'
  });
  const adaToggle = ada.locator('.jp-ShareFilesPanel-itemTwisty');
  const bobToggle = bob.locator('.jp-ShareFilesPanel-itemTwisty');
  const adaRow = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'ada.txt'
  });
  const bobRow = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'bob.txt'
  });
  await expect(adaToggle).toHaveAttribute('aria-expanded', 'true');
  // one click folds Ada's group alone; Bob's group keeps its row
  await adaToggle.click();
  await expect(adaToggle).toHaveAttribute('aria-expanded', 'false');
  await expect(adaRow).toHaveCount(0);
  await expect(bobRow).toHaveCount(1);
  await expect(bobToggle).toHaveAttribute('aria-expanded', 'true');
  // Enter expands, Space folds again
  await adaToggle.focus();
  await page.keyboard.press('Enter');
  await expect(adaToggle).toHaveAttribute('aria-expanded', 'true');
  await expect(adaRow).toHaveCount(1);
  await page.keyboard.press('Space');
  await expect(adaToggle).toHaveAttribute('aria-expanded', 'false');
  await expect(adaRow).toHaveCount(0);
  // a panel refresh keeps the folded group folded
  const refresh = page.locator(`${PANEL} button[title="Refresh"]`);
  await refresh.click();
  await expect(refresh).not.toHaveClass(/jp-mod-spinning/, { timeout: 30000 });
  await expect(adaToggle).toHaveAttribute('aria-expanded', 'false');
  await expect(adaRow).toHaveCount(0);
  await expect(bobRow).toHaveCount(1);
});

test('Remove All Uploads from one uploader leaves the other group', async ({
  page
}) => {
  // ACC-ROWS-245
  const title = `Remove All ${Date.now().toString(36)}`;
  const id = await createRequest(page, title);
  await uploadToRequest(page, id, 'ada-1.txt', 'one\n', 'Ada');
  await uploadToRequest(page, id, 'ada-2.txt', 'two\n', 'Ada');
  await becomeUploader(page, id, 'MZXW');
  await uploadToRequest(page, id, 'bob.txt', 'bob\n', 'Bob');
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: title
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const ada = row.locator('.jp-ShareFilesPanel-uploaderHeader', {
    hasText: 'Ada'
  });
  const bob = row.locator('.jp-ShareFilesPanel-uploaderHeader', {
    hasText: 'Bob'
  });
  // the menu entry is destructive, last under a separator
  await ada.click({ button: 'right' });
  const item = page.locator(
    '.lm-Menu .lm-Menu-item.jp-ShareFilesPanelContextMenu-danger'
  );
  await expect(item).toHaveCount(1);
  await expect(item.locator('.lm-Menu-itemLabel')).toHaveText(
    /^Remove All Uploads from Ada/
  );
  await expectDangerItem(item);
  await item.click();
  // one confirmation names the person and the two uploads
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText('Remove all uploads from Ada');
  await expect(dialog).toContainText('2 upload');
  await dialog.locator('button.jp-mod-accept').click();
  // Ada's header and both rows go; Bob's group stays
  await expect(ada).toHaveCount(0);
  await expect(
    row.locator('.jp-ShareFilesPanel-entry', { hasText: 'ada-' })
  ).toHaveCount(0);
  await expect(bob).toHaveCount(1);
  await expect(
    row.locator('.jp-ShareFilesPanel-entry', { hasText: 'bob.txt' })
  ).toHaveCount(1);
  const held = await page.evaluate(
    async ([api, id]: [string, string]) => {
      const r = await fetch(`${api}/requests/${id}`);
      const data = await r.json();
      return data.uploaders.flatMap((u: any) =>
        u.entries.map((e: any) => e.name)
      );
    },
    [API, id]
  );
  expect(held).toEqual(['bob.txt']);
});

test('a share file and folder download into the folder the file browser shows', async ({
  page,
  tmpPath
}) => {
  // ACC-ROWS-237: an own entry is copied from the share onto the lab
  // filesystem, into the file browser's current folder - the browser's
  // Downloads folder is never used
  await page.contents.createDirectory(`${tmpPath}/share-src`);
  await page.contents.createDirectory(`${tmpPath}/share-src/folder`);
  await page.contents.uploadContent(
    'picked\n',
    'text',
    `${tmpPath}/share-src/picked.txt`
  );
  await page.contents.uploadContent(
    'inner\n',
    'text',
    `${tmpPath}/share-src/folder/inner.txt`
  );
  // a folder of its own, so both saves land somewhere empty
  await page.contents.createDirectory(`${tmpPath}/into`);
  await page.filebrowser.openDirectory(`${tmpPath}/into`);
  await createShare(page, 'Copyable', [
    `${tmpPath}/share-src/picked.txt`,
    `${tmpPath}/share-src/folder`
  ]);
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Copyable'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const pick = async (target: any) => {
    await target.click({ button: 'right' });
    await page
      .locator('.lm-Menu .lm-Menu-item', {
        hasText: 'Download to Current Folder'
      })
      .click();
  };
  // the folder the file browser shows is where both land
  expect(await page.filebrowser.getCurrentDirectory()).toBe(`${tmpPath}/into`);
  await pick(
    row.locator('.jp-ShareFilesPanel-entry', { hasText: 'picked.txt' })
  );
  await expect
    .poll(() => page.contents.fileExists(`${tmpPath}/into/picked.txt`), {
      timeout: 15000,
      message: 'the file lands in the folder the file browser shows'
    })
    .toBe(true);
  await pick(row.locator('.jp-ShareFilesPanel-entry', { hasText: 'folder/' }));
  await expect
    .poll(() => page.contents.fileExists(`${tmpPath}/into/folder/inner.txt`), {
      timeout: 15000,
      message: 'the folder lands in the folder the file browser shows'
    })
    .toBe(true);
  // a row inside an open sub-folder saves the same way
  await row
    .locator('.jp-ShareFilesPanel-entry', { hasText: 'folder/' })
    .dblclick();
  const inner = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'inner.txt'
  });
  await expect(inner).toBeVisible();
  await pick(inner);
  await expect
    .poll(() => page.contents.fileExists(`${tmpPath}/into/inner.txt`), {
      timeout: 15000,
      message: 'a sub-folder row lands in the folder the file browser shows'
    })
    .toBe(true);
  // the share keeps its own copies
  expect(
    await page.contents.fileExists(`${tmpPath}/share-src/picked.txt`)
  ).toBe(true);
});

test('a file and a folder row wear the transfer layer while the copy runs and clear it after', async ({
  page,
  tmpPath
}) => {
  // ACC-ROWS-242: the copy reports no fraction, so the running row carries
  // the fixed wash over its whole width, and clears on success and failure
  await page.contents.uploadContent(
    'picked\n',
    'text',
    `${tmpPath}/picked.txt`
  );
  await page.contents.createDirectory(`${tmpPath}/folder`);
  await page.contents.uploadContent(
    'inner\n',
    'text',
    `${tmpPath}/folder/inner.txt`
  );
  await page.contents.createDirectory(`${tmpPath}/into`);
  await page.filebrowser.openDirectory(`${tmpPath}/into`);
  await createShare(page, 'Layered', [
    `${tmpPath}/picked.txt`,
    `${tmpPath}/folder`
  ]);
  await openPanel(page);
  const item = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Layered'
  });
  await item.locator('.jp-ShareFilesPanel-itemHeader').click();
  const row = (name: string) =>
    item.locator('.jp-ShareFilesPanel-entry', { hasText: name });
  const layer = (name: string) =>
    row(name).locator('.jp-ShareFilesPanel-itemProgress');
  const pick = async (name: string) => {
    await row(name).click({ button: 'right' });
    await page
      .locator('.lm-Menu .lm-Menu-item', {
        hasText: 'Download to Current Folder'
      })
      .click();
  };
  // hold every copy answer so the layer can be read while the copy runs
  const held: any[] = [];
  await page.route('**/api/contents/**', async (route: any) => {
    const body = route.request().postData() || '';
    if (route.request().method() === 'POST' && body.includes('copy_from')) {
      held.push(route);
      return;
    }
    await route.continue();
  });

  await pick('picked.txt');
  await expect(layer('picked.txt')).toHaveCount(1);
  await expect(layer('picked.txt')).toHaveAttribute(
    'aria-label',
    'Downloading picked.txt'
  );
  // no fraction is reported, so no value is stated
  expect(await layer('picked.txt').getAttribute('aria-valuenow')).toBeNull();
  const fileLayer = (await layer('picked.txt').boundingBox())!;
  const fileRow = (await row('picked.txt').boundingBox())!;
  expect(Math.abs(fileLayer.width - fileRow.width)).toBeLessThan(2);
  // the wash lifts the size text to the primary colour (owner, 2026-11-18)
  const sizeColor = await row('picked.txt')
    .locator('.jp-ShareFilesPanel-entrySize')
    .evaluate((el: Element) => getComputedStyle(el).color);
  const primaryColor = await page.evaluate(() => {
    const probe = document.createElement('span');
    probe.style.color = 'var(--jp-ui-font-color1)';
    document.body.appendChild(probe);
    const value = getComputedStyle(probe).color;
    probe.remove();
    return value;
  });
  expect(sizeColor).toBe(primaryColor);
  await expect.poll(() => held.length).toBe(1);
  await held.shift().continue();
  await expect(layer('picked.txt')).toHaveCount(0);
  await expect
    .poll(() => page.contents.fileExists(`${tmpPath}/into/picked.txt`), {
      timeout: 15000
    })
    .toBe(true);

  // the folder row wears it too
  await pick('folder/');
  await expect(layer('folder/')).toHaveCount(1);
  await expect(layer('folder/')).toHaveAttribute(
    'aria-label',
    'Downloading folder'
  );
  await expect.poll(() => held.length).toBe(1);
  await held.shift().continue();
  await expect(layer('folder/')).toHaveCount(0);
  await expect
    .poll(() => page.contents.fileExists(`${tmpPath}/into/folder/inner.txt`), {
      timeout: 15000
    })
    .toBe(true);

  // a refused copy clears the layer as well
  await pick('picked.txt');
  await expect(layer('picked.txt')).toHaveCount(1);
  await expect.poll(() => held.length).toBe(1);
  await held.shift().fulfill({ status: 500, json: { message: 'copy failed' } });
  await expect(layer('picked.txt')).toHaveCount(0);
});

test('Remove from Share on one file asks once and leaves the other row', async ({
  page,
  tmpPath
}) => {
  // ACC-ROWS-238
  await page.contents.uploadContent('keep\n', 'text', `${tmpPath}/keep.txt`);
  await page.contents.uploadContent('gone\n', 'text', `${tmpPath}/gone.txt`);
  const id = await createShare(page, 'Removable', [
    `${tmpPath}/keep.txt`,
    `${tmpPath}/gone.txt`
  ]);
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Removable'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const gone = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'gone.txt'
  });
  const keep = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'keep.txt'
  });
  const dialog = page.locator('.jp-Dialog');
  // cancel removes nothing
  await gone.click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: /^Remove from Share$/ })
    .click();
  await expect(dialog).toContainText('Remove "gone.txt" from the share?');
  await dialog.locator('button.jp-mod-reject').click();
  await expect(dialog).toHaveCount(0);
  await expect(gone).toHaveCount(1);
  // accept removes only the one row
  await gone.click({ button: 'right' });
  await page
    .locator('.lm-Menu .lm-Menu-item', { hasText: /^Remove from Share$/ })
    .click();
  await expect(dialog).toContainText('Remove "gone.txt" from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(gone).toHaveCount(0);
  await expect(keep).toHaveCount(1);
  const names = await page.evaluate(
    async ([api, id]: [string, string]) => {
      const r = await fetch(`${api}/shares/${id}`);
      return (await r.json()).entries.map((e: any) => e.name);
    },
    [API, id]
  );
  expect(names).toEqual(['keep.txt']);
});

test('Remove from Share draws its label and icon in the error colour, last under a separator', async ({
  page,
  tmpPath
}) => {
  // ACC-ROWS-243-DESTRUCTIVE, DEF-PANEL-149-DANGER
  await page.contents.uploadContent('red\n', 'text', `${tmpPath}/red.txt`);
  await createShare(page, 'Dangerous', [`${tmpPath}/red.txt`]);
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Dangerous'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await row
    .locator('.jp-ShareFilesPanel-entry', { hasText: 'red.txt' })
    .click({ button: 'right' });
  const item = page.locator(
    '.lm-Menu .lm-Menu-item.jp-ShareFilesPanelContextMenu-danger'
  );
  await expect(item).toHaveCount(1);
  await expectDangerItem(item);
  await page.keyboard.press('Escape');
});

test('a row menu opens from the keyboard with Remove, and Escape returns the focus', async ({
  page,
  tmpPath
}) => {
  // ACC-ROWS-238 keyboard access
  await page.contents.uploadContent('keys\n', 'text', `${tmpPath}/keys.txt`);
  await createShare(page, 'Keyed', [`${tmpPath}/keys.txt`]);
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Keyed'
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  const entry = row.locator('.jp-ShareFilesPanel-entry', {
    hasText: 'keys.txt'
  });
  await entry.focus();
  for (const key of ['Shift+F10', 'ContextMenu']) {
    await page.keyboard.press(key);
    await expect(
      page.locator('.lm-Menu .lm-Menu-item', {
        hasText: /^Remove from Share$/
      }),
      key
    ).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(entry, key).toBeFocused();
  }
});
