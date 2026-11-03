import { expect, test } from '@jupyterlab/galata';

/**
 * Standalone mode: the entries of a share are selected with ctrl-click,
 * shift-click and a plain click, and the remove button acts on the selection
 * (ACC-EDIT-215, ACC-EDIT-228). What the share holds is read from the server
 * after each step, not from the rows.
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

/** A share of three files the test writes in the workspace, open in the
 * panel: its row, a locator of one entry, the selected entries' names and
 * the names the share holds on the server. */
async function shareOfThree(page: any, title: string): Promise<any> {
  const names = ['a1.txt', 'b2.txt', 'c3.txt'].map(n => `${title}-${n}`);
  for (const name of names) {
    await page.contents.uploadContent('x', 'text', name);
  }
  const id = await page.evaluate(
    async ([api, n, paths]: [string, string, string[]]) => {
      const r = await fetch(`${api}/shares`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: n, paths })
      });
      return (await r.json()).id;
    },
    [API, title, names]
  );
  await openPanel(page);
  const row = page.locator(`${PANEL} .jp-ShareFilesPanel-item`, {
    hasText: title
  });
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  return {
    names,
    row,
    entry: (name: string) =>
      row.locator('.jp-ShareFilesPanel-entry', { hasText: name }),
    selected: row.locator(
      '.jp-ShareFilesPanel-entry.jp-mod-selected .jp-ShareFilesPanel-entryName'
    ),
    held: () =>
      page.evaluate(
        async ([api, i]: [string, string]) =>
          (await (await fetch(`${api}/shares/${i}`)).json()).entries.map(
            (e: any) => e.name
          ),
        [API, id]
      ),
    cleanup: async () => {
      for (const name of names) {
        await page.contents.deleteFile(name);
      }
    }
  };
}

test('ctrl-click and shift-click select standalone entries and remove acts on the selection', async ({
  page
}) => {
  // ACC-EDIT-215
  const title = `select-${Date.now()}`;
  const share = await shareOfThree(page, title);
  const [a, b, c] = share.names;
  const { entry, selected } = share;
  await entry(a).click();
  await entry(c).click({ modifiers: ['Shift'] });
  await expect(selected).toHaveText([a, b, c]);
  await expect(entry(b)).toHaveAttribute('aria-selected', 'true');
  // select two of three entries, choose remove: one entry stays listed
  await entry(c).click({ modifiers: ['Control'] });
  await expect(selected).toHaveText([a, b]);
  await entry(a).locator('.jp-ShareFilesPanel-entryRemove').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toContainText('Remove 2 entries from the share?');
  await dialog.locator('button.jp-mod-accept').click();
  await expect(entry(a)).toHaveCount(0);
  await expect(entry(b)).toHaveCount(0);
  await expect(entry(c)).toHaveCount(1);
  expect(await share.held()).toEqual([c]);
  // one entry is removed as before, with no dialog
  await entry(c).locator('.jp-ShareFilesPanel-entryRemove').click();
  await expect(entry(c)).toHaveCount(0);
  await expect(dialog).toHaveCount(0);
  expect(await share.held()).toEqual([]);
  await share.cleanup();
});

test('removing several standalone entries asks once and cancel removes nothing', async ({
  page
}) => {
  // ACC-EDIT-228
  const title = `confirm-${Date.now()}`;
  const share = await shareOfThree(page, title);
  const [a, b, c] = share.names;
  const { entry, selected } = share;
  await entry(a).click({ modifiers: ['Control'] });
  await entry(b).click({ modifiers: ['Control'] });
  await entry(c).click({ modifiers: ['Control'] });
  await expect(selected).toHaveText([a, b, c]);
  // select three entries, choose remove: one dialog naming 3 entries
  await entry(b).locator('.jp-ShareFilesPanel-entryRemove').click();
  const dialog = page.locator('.jp-Dialog');
  await expect(dialog).toHaveCount(1);
  await expect(dialog).toContainText('Remove 3 entries from the share?');
  await dialog.locator('button.jp-mod-reject').click();
  await expect(dialog).toHaveCount(0);
  expect(await share.held()).toEqual([a, b, c]);
  await expect(selected).toHaveText([a, b, c]);
  await share.cleanup();
});
