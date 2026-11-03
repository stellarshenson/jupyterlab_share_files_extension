import { expect, test } from '@jupyterlab/galata';

import { dragOnto, dragOver } from './helpers/drag';

/**
 * Dropping onto an existing share (ACC-DRAG-156, DEF-PANEL-84) and onto a
 * folder of it (ACC-EDIT-232, ACC-EDIT-233). The drag itself is in
 * `helpers/drag.ts` - the hub suite drives the same one.
 */

const API = '/jupyterlab-share-files-extension/api';
const PANEL = '#jupyterlab-share-files-extension-panel';

async function openPanel(page: any): Promise<void> {
  await page.sidebar.openTab('jupyterlab-share-files-extension-panel');
  const refresh = page.locator(`${PANEL} button[title="Refresh"]`);
  await refresh.click();
  await expect(refresh).not.toHaveClass(/jp-mod-spinning/, { timeout: 30000 });
}

async function createShare(page: any, name: string): Promise<string> {
  return page.evaluate(
    async ([api, n]: [string, string]) => {
      const r = await fetch(`${api}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n, paths: [] })
      });
      return (await r.json()).id as string;
    },
    [API, name]
  );
}

async function shareEntryNames(page: any, id: string): Promise<string[]> {
  return page.evaluate(
    async ([api, sid]: [string, string]) => {
      const r = await fetch(`${api}/shares/${sid}`);
      const data = await r.json();
      return (data.entries || []).map((e: any) => e.name);
    },
    [API, id]
  );
}

test('a file dropped on a share row is added to that share', async ({
  page
}) => {
  await page.contents.uploadContent('dropped', 'text', 'drag-file.txt');
  const shareId = await createShare(page, 'drag-file-share');
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'drag-file-share'
  });
  await expect(row).toBeVisible();

  await dragOnto(page, 'drag-file.txt', row);

  await expect
    .poll(() => shareEntryNames(page, shareId), { timeout: 20000 })
    .toContain('drag-file.txt');
});

test('a folder dropped on a share row is added with its contents', async ({
  page
}) => {
  await page.contents.createDirectory('drag-dir');
  await page.contents.uploadContent('inside', 'text', 'drag-dir/inner.txt');
  const shareId = await createShare(page, 'drag-folder-share');
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'drag-folder-share'
  });
  await expect(row).toBeVisible();

  await dragOnto(page, 'drag-dir', row);

  await expect
    .poll(() => shareEntryNames(page, shareId), { timeout: 20000 })
    .toContain('drag-dir');
  // the manifest lists top-level entries only, so the folder's contents are
  // read through its size - the copy carried inner.txt if the folder is not
  // empty on disk
  const size = await page.evaluate(
    async ([api, sid]: [string, string]) => {
      const r = await fetch(`${api}/shares/${sid}`);
      const data = await r.json();
      const dir = (data.entries || []).find((e: any) => e.name === 'drag-dir');
      return dir ? dir.size : -1;
    },
    [API, shareId]
  );
  expect(size).toBeGreaterThan(0);
});

test('the drop zone keeps 10px on three sides and 7px, half its old gap, above the link input', async ({
  page
}) => {
  // ACC-DRAG-157: the zone used to sit with 8px above, 10px at the sides and
  // 4px below, and its text with 14px above and below against 10px at the
  // sides; the gap down to the link input was 14px and is now half of that
  // (owner, 2026-09-24)
  await openPanel(page);
  const zone = page.locator(`${PANEL} .jp-ShareFilesPanel-dropZone`);
  await expect(zone).toBeVisible();
  await expect(zone).toHaveText('Drag files here to share');
  const spacing = await zone.evaluate((el: HTMLElement) => {
    const s = getComputedStyle(el);
    const input = document.querySelector('.jp-ShareFilesPanel-connectInput')!;
    return {
      margin: [s.marginTop, s.marginRight, s.marginLeft],
      padding: [s.paddingTop, s.paddingRight, s.paddingBottom, s.paddingLeft],
      gap: input.getBoundingClientRect().top - el.getBoundingClientRect().bottom
    };
  });
  expect(spacing.margin).toEqual(['10px', '10px', '10px']);
  expect(new Set(spacing.padding).size).toBe(1);
  expect(spacing.gap).toBe(7);
});

/** A share made from workspace folders, open in the panel: its id, its
 * folder as a workspace path, its row and a locator of one of its rows. */
async function openShareOf(
  page: any,
  name: string,
  paths: string[]
): Promise<any> {
  const made = await page.evaluate(
    async ([api, n, p]: [string, string, string[]]) => {
      const r = await fetch(`${api}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n, paths: p })
      });
      return r.json();
    },
    [API, name, paths]
  );
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: name
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  return {
    id: made.id as string,
    path: made.path as string,
    row,
    entry: (text: string) =>
      row.locator('.jp-ShareFilesPanel-entry', { hasText: text })
  };
}

test('a file dropped on a folder row of a share lands in that folder, and only the row is marked', async ({
  page
}) => {
  // ACC-EDIT-232, ACC-EDIT-233
  await page.contents.uploadContent('k', 'text', 'fr-docs/keep.txt');
  await page.contents.uploadContent('a', 'text', 'fr-a.txt');
  const share = await openShareOf(page, 'folder-row-share', ['fr-docs']);
  const folder = share.entry('fr-docs/');

  await dragOver(page, 'fr-a.txt', folder);
  // the file browser's mark: the folder row under the pointer, not the share
  await expect(share.row).not.toHaveClass(/jp-mod-dropTarget/);
  const outline = await folder.evaluate((el: HTMLElement) => {
    const s = getComputedStyle(el);
    return `${s.outlineStyle} ${s.outlineWidth}`;
  });
  expect(outline).toBe('solid 2px');
  await page.mouse.up();

  await expect
    .poll(() => page.contents.fileExists(`${share.path}/fr-docs/fr-a.txt`), {
      timeout: 20000
    })
    .toBe(true);
  expect(await shareEntryNames(page, share.id)).toEqual(['fr-docs']);
  await expect(folder).not.toHaveClass(/jp-mod-dropTarget/);
});

test('a selected folder row under a drag looks different from a selected row', async ({
  page
}) => {
  // ACC-EDIT-233: one plain click selects the row, whose background is the drop outline's colour
  await page.contents.uploadContent('k', 'text', 'sd-docs/keep.txt');
  await page.contents.uploadContent('x', 'text', 'sd-x.txt');
  const share = await openShareOf(page, 'selected-drop-share', ['sd-docs']);
  const folder = share.entry('sd-docs/');
  await folder.click();
  await expect(folder).toHaveClass(/jp-mod-selected/);
  const background = (el: HTMLElement) => getComputedStyle(el).backgroundColor;
  const selected = await folder.evaluate(background);

  await dragOver(page, 'sd-x.txt', folder);
  await expect(folder).toHaveClass(/jp-mod-selected/);
  expect(await folder.evaluate(background)).not.toBe(selected);
  await page.mouse.up();

  await expect
    .poll(() => page.contents.fileExists(`${share.path}/sd-docs/sd-x.txt`), {
      timeout: 20000
    })
    .toBe(true);
});

test('while a folder of a share is open, a drop on a file row lands in that folder and a drop on a folder row in it', async ({
  page
}) => {
  // ACC-EDIT-232, ACC-EDIT-233
  await page.contents.uploadContent('k', 'text', 'of-docs/keep.txt');
  await page.contents.uploadContent('d', 'text', 'of-docs/deep/d.txt');
  await page.contents.uploadContent('b', 'text', 'of-b.txt');
  await page.contents.uploadContent('c', 'text', 'of-c.txt');
  const share = await openShareOf(page, 'open-folder-share', ['of-docs']);
  await share.entry('of-docs/').dblclick();
  await expect(share.entry('keep.txt')).toBeVisible();

  // a file row passes the drag on to the share, which shows the open folder
  await dragOver(page, 'of-b.txt', share.entry('keep.txt'), [], share.row);
  await expect(share.entry('keep.txt')).not.toHaveClass(/jp-mod-dropTarget/);
  await page.mouse.up();
  await expect
    .poll(() => page.contents.fileExists(`${share.path}/of-docs/of-b.txt`), {
      timeout: 20000
    })
    .toBe(true);

  // a folder row inside the open folder takes the drop into itself
  await expect(share.entry('of-b.txt')).toBeVisible();
  await dragOver(page, 'of-c.txt', share.entry('deep/'));
  await expect(share.row).not.toHaveClass(/jp-mod-dropTarget/);
  await page.mouse.up();
  await expect
    .poll(
      () => page.contents.fileExists(`${share.path}/of-docs/deep/of-c.txt`),
      { timeout: 20000 }
    )
    .toBe(true);
  expect(await shareEntryNames(page, share.id)).toEqual(['of-docs']);
});

test('a drop or a paste on a collapsed share lands at the top, not in the folder it showed before', async ({
  page
}) => {
  // ACC-EDIT-232: a collapsed share shows no folder
  await page.contents.uploadContent('k', 'text', 'cs-docs/keep.txt');
  await page.contents.uploadContent('d', 'text', 'cs-drop.txt');
  await page.contents.uploadContent('p', 'text', 'cs-paste.txt');
  const share = await openShareOf(page, 'collapsed-share', ['cs-docs']);
  await share.entry('cs-docs/').dblclick();
  await expect(share.entry('keep.txt')).toBeVisible();
  const header = share.row.locator('.jp-ShareFilesPanel-itemHeader');
  await header.click();
  await expect(share.entry('keep.txt')).toHaveCount(0);

  await dragOver(page, 'cs-drop.txt', header, [], share.row);
  await page.mouse.up();
  await expect
    .poll(() => shareEntryNames(page, share.id), { timeout: 20000 })
    .toEqual(['cs-docs', 'cs-drop.txt']);

  await page.filebrowser.openHomeDirectory();
  await page.filebrowser.revealFileInBrowser('cs-paste.txt');
  await page
    .getByRole('region', { name: 'File Browser Section' })
    .getByRole('listitem', { name: /^Name: cs-paste\.txt/ })
    .click();
  await page.evaluate(() =>
    (window as any).jupyterapp.commands.execute('filebrowser:copy')
  );
  await header.click({ button: 'right' });
  await page.locator('.lm-Menu .lm-Menu-item', { hasText: 'Paste' }).click();
  await expect
    .poll(() => shareEntryNames(page, share.id), { timeout: 20000 })
    .toEqual(['cs-docs', 'cs-drop.txt', 'cs-paste.txt']);
  expect(
    await page.contents.fileExists(`${share.path}/cs-docs/cs-drop.txt`)
  ).toBe(false);
});

test("the share menu's paste, while a folder of the share is open, lands in that folder", async ({
  page
}) => {
  // ACC-EDIT-232: a cut deletes its original once the add is in
  await page.contents.uploadContent('k', 'text', 'pf-docs/keep.txt');
  await page.contents.uploadContent('c', 'text', 'pf-cut.txt');
  const share = await openShareOf(page, 'paste-folder-share', ['pf-docs']);
  await share.entry('pf-docs/').dblclick();
  await expect(share.entry('keep.txt')).toBeVisible();

  await page.filebrowser.openHomeDirectory();
  await page.filebrowser.revealFileInBrowser('pf-cut.txt');
  await page
    .getByRole('region', { name: 'File Browser Section' })
    .getByRole('listitem', { name: /^Name: pf-cut\.txt/ })
    .click();
  await page.evaluate(() =>
    (window as any).jupyterapp.commands.execute('filebrowser:cut')
  );
  await share.row
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  await page.locator('.lm-Menu .lm-Menu-item', { hasText: 'Paste' }).click();

  await expect
    .poll(() => page.contents.fileExists(`${share.path}/pf-docs/pf-cut.txt`), {
      timeout: 20000
    })
    .toBe(true);
  expect(await shareEntryNames(page, share.id)).toEqual(['pf-docs']);
  await expect
    .poll(() => page.contents.fileExists('pf-cut.txt'), { timeout: 20000 })
    .toBe(false);
});
