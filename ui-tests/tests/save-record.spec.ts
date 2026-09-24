import { expect, test } from '@jupyterlab/galata';

/**
 * ACC-SAVE-171 - a whole share or request written back into the file
 * browser's current folder, as the files themselves or as one zip.
 *
 * Standalone only: this lab holds the bytes, so the save is a copy out of the
 * store and the archive is built here. On a hub the bytes are the hub's, it
 * packs no archive, so the lab zips what the hub wrote - that half is in
 * `hub/hub-mode.spec.ts`.
 */

const API = '/jupyterlab-share-files-extension/api';
const PANEL = '#jupyterlab-share-files-extension-panel';

/** Create a share of one uploaded file through the extension API. */
async function shareOf(page: any, name: string, path: string): Promise<string> {
  return page.evaluate(
    async ([api, n, p]: [string, string, string]) => {
      const r = await fetch(`${api}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n, paths: [p] })
      });
      return (await r.json()).id as string;
    },
    [API, name, path]
  );
}

async function openPanel(page: any): Promise<void> {
  await page.sidebar.openTab('jupyterlab-share-files-extension-panel');
  await expect(page.locator(PANEL)).toBeVisible();
}

/** Right-click a record row and pick one of its menu entries. */
async function rowMenu(page: any, name: string, entry: string): Promise<void> {
  await page
    .locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name })
    .locator('.jp-ShareFilesPanel-itemHeader')
    .click({ button: 'right' });
  await page.locator('.lm-Menu .lm-Menu-item', { hasText: entry }).click();
}

test('a whole share saves into the current folder as its files', async ({
  page,
  tmpPath
}) => {
  await page.contents.uploadContent('quarter\n', 'text', `${tmpPath}/q.txt`);
  await shareOf(page, 'Quarter Report', `${tmpPath}/q.txt`);
  await openPanel(page);

  await rowMenu(page, 'Quarter Report', 'Download to Current Folder');

  // the folder is named after the record and holds the record's own files
  await expect
    .poll(() => page.contents.fileExists(`${tmpPath}/Quarter-Report/q.txt`), {
      timeout: 15000,
      message: 'the record lands as a folder of its files'
    })
    .toBe(true);
  // and the share itself is untouched
  expect(await page.contents.fileExists(`${tmpPath}/q.txt`)).toBe(true);
});

test('a whole share saves into the current folder as one zip', async ({
  page,
  tmpPath
}) => {
  await page.contents.uploadContent('zipped\n', 'text', `${tmpPath}/z.txt`);
  await shareOf(page, 'Zipped Report', `${tmpPath}/z.txt`);
  await openPanel(page);

  await rowMenu(page, 'Zipped Report', 'Save as Zip');

  await expect
    .poll(() => page.contents.fileExists(`${tmpPath}/Zipped-Report.zip`), {
      timeout: 15000,
      message: 'the record lands as one archive named after it'
    })
    .toBe(true);
});

test('a second save lands beside the first, never over it', async ({
  page,
  tmpPath
}) => {
  await page.contents.uploadContent('twice\n', 'text', `${tmpPath}/t.txt`);
  await shareOf(page, 'Twice Over', `${tmpPath}/t.txt`);
  await openPanel(page);

  await rowMenu(page, 'Twice Over', 'Download to Current Folder');
  await expect
    .poll(() => page.contents.fileExists(`${tmpPath}/Twice-Over/t.txt`), {
      timeout: 15000
    })
    .toBe(true);
  await rowMenu(page, 'Twice Over', 'Download to Current Folder');

  // the first save is what the owner already looked at; the second takes the
  // next free name rather than writing over it
  await expect
    .poll(() => page.contents.fileExists(`${tmpPath}/Twice-Over-2/t.txt`), {
      timeout: 15000,
      message: 'the second save takes the next free name'
    })
    .toBe(true);
  expect(await page.contents.fileExists(`${tmpPath}/Twice-Over/t.txt`)).toBe(
    true
  );
});

test('every entry of a share row and a share entry menu carries an icon', async ({
  page,
  tmpPath
}) => {
  await page.contents.uploadContent('icon\n', 'text', `${tmpPath}/i.txt`);
  await shareOf(page, 'Iconed', `${tmpPath}/i.txt`);
  await openPanel(page);
  const item = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: 'Iconed'
  });
  const header = item.locator('.jp-ShareFilesPanel-itemHeader');
  const items = page.locator('.lm-Menu .lm-Menu-item[data-type="command"]');
  const bare = items.filter({ hasNot: page.locator('.lm-Menu-itemIcon svg') });
  // the download entries carry the download arrow, not the save disk
  // (ACC-SAVE-187)
  const pathOf = (label: string) =>
    items
      .filter({ hasText: label })
      .locator('.lm-Menu-itemIcon svg path')
      .first()
      .getAttribute('d');
  const ARROW = 'M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z';
  await header.click({ button: 'right' });
  await expect(items.first()).toBeVisible();
  expect(await bare.allTextContents()).toEqual([]);
  expect(await pathOf('Download to Current Folder')).toBe(ARROW);
  // the lock of Set Password is painted like the other menu icons, where
  // JupyterLab's own lockIcon reads darker (ACC-SHARE-188)
  const fillOf = (label: string) =>
    items
      .filter({ hasText: label })
      .locator('.lm-Menu-itemIcon svg path')
      .first()
      .evaluate((el: Element) => getComputedStyle(el).fill);
  expect(await fillOf('Set Password')).toBe(await fillOf('Copy Link'));
  await page.keyboard.press('Escape');
  await header.click();
  await item
    .locator('.jp-ShareFilesPanel-entry', { hasText: 'i.txt' })
    .click({ button: 'right' });
  await expect(items).toHaveText([
    'Download to Current Folder',
    'Show in File Browser',
    'Copy'
  ]);
  expect(await bare.allTextContents()).toEqual([]);
  expect(await pathOf('Download to Current Folder')).toBe(ARROW);
  await page.keyboard.press('Escape');
});

test('a share is renamed from its menu and keeps its id and link', async ({
  page,
  tmpPath
}) => {
  // ACC-EDIT-189, ACC-EDIT-190, ACC-EDIT-192
  await page.contents.uploadContent('r\n', 'text', `${tmpPath}/r.txt`);
  const id = await shareOf(page, 'Before Name', `${tmpPath}/r.txt`);
  const listed = () =>
    page.evaluate(async (api: string) => {
      const r = await fetch(`${api}/shares`);
      return (await r.json()).shares as {
        id: string;
        name: string;
        link: string;
      }[];
    }, API);
  const before = (await listed()).find(s => s.id === id)!;
  await openPanel(page);
  const row = (name: string) =>
    page.locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name });

  await rowMenu(page, 'Before Name', 'Rename Share...');
  const dialog = page.locator('.jp-Dialog');
  const input = dialog.locator('input');
  // the dialog opens on the current name
  await expect(input).toHaveValue('Before Name');
  await input.fill('After Name');
  await dialog.locator('button', { hasText: 'Rename' }).click();
  await expect(row('After Name')).toBeVisible();
  await expect(row('Before Name')).toHaveCount(0);
  const after = (await listed()).find(s => s.id === id)!;
  expect(after.name).toBe('After Name');
  expect(after.link).toBe(before.link);
  // a recipient holding the link still opens it
  expect(
    await page.evaluate(
      async (link: string) => (await fetch(link)).status,
      after.link
    )
  ).toBe(200);

  // spaces alone are refused and the name stays
  await rowMenu(page, 'After Name', 'Rename Share...');
  await input.fill('   ');
  await dialog.locator('button', { hasText: 'Rename' }).click();
  await expect(
    page.locator('.Toastify__toast', { hasText: "Missing 'name'" })
  ).toBeVisible();
  expect((await listed()).find(s => s.id === id)!.name).toBe('After Name');
});
