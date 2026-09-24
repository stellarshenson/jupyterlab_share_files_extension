import { expect, test } from '@jupyterlab/galata';

import { dragOnto } from './helpers/drag';

/**
 * Dropping onto an existing share (ACC-DRAG-156, DEF-PANEL-84). The drag
 * itself is in `helpers/drag.ts` - the hub suite drives the same one.
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
