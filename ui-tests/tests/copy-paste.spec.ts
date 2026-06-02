import { expect, test } from '@jupyterlab/galata';

/**
 * Integration tests for the panel <-> file browser copy/paste feature
 * (acceptance criteria AC7/AC8 in docs/ACCEPTANCE_CONNECTED_ENTRIES.md).
 *
 * The native file-browser clipboard is private, so copy/paste is bridged
 * through the public `commands.commandExecuted` signal:
 *  - native `filebrowser:copy`/`cut` are mirrored into the extension clipboard,
 *    then pasted into a share via `share-files-panel:paste-into-share` (AC8)
 *  - a panel-copied entry is pasted into the file browser by piggybacking on
 *    native `filebrowser:paste` (AC7)
 *
 * These drive the real command registry (exposed by the galata server) and the
 * real extension HTTP API, so they exercise the full bridge end to end. The
 * remote-peer cases (saving a connected peer's entry, uploading to a connected
 * request) need a second server and are covered by the live verification log;
 * here we cover the local panel-origin paste path, which shares the same hook.
 */

const API = '/jupyterlab-share-files-extension/api';

/** Create a share through the extension API, return its id. */
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

/** Return the entry names currently in a share. */
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

test('New Share command label has no "(empty)" suffix', async ({ page }) => {
  const label = await page.evaluate(() =>
    (window as any).jupyterapp.commands.label('share-files-panel:new-share')
  );
  expect(label).toBe('New Share');
});

test('AC8 - native Copy then paste into a share adds the file (original kept)', async ({
  page
}) => {
  await page.contents.uploadContent('copy me', 'text', 'ac8-copy.txt');
  const shareId = await createShare(page, 'ac8-copy-share');

  // Reveal then select the file so `filebrowser:copy` reads it as the selection.
  // openHomeDirectory forces a fresh listing so the just-uploaded file shows.
  await page.filebrowser.openHomeDirectory();
  await page.filebrowser.revealFileInBrowser('ac8-copy.txt');
  await page
    .getByRole('region', { name: 'File Browser Section' })
    .getByRole('listitem', { name: /^Name: ac8-copy\.txt/ })
    .click();

  // Native copy -> mirrored into our clipboard by the commandExecuted bridge.
  await page.evaluate(() =>
    (window as any).jupyterapp.commands.execute('filebrowser:copy')
  );
  // Paste into the share via the panel command.
  await page.evaluate(
    id =>
      (window as any).jupyterapp.commands.execute(
        'share-files-panel:paste-into-share',
        { id }
      ),
    shareId
  );

  await expect
    .poll(() => shareEntryNames(page, shareId), { timeout: 15000 })
    .toContain('ac8-copy.txt');

  // Copy leaves the original in place.
  const stillThere = await page.evaluate(async () => {
    try {
      await (window as any).jupyterapp.serviceManager.contents.get(
        'ac8-copy.txt',
        {
          content: false
        }
      );
      return true;
    } catch {
      return false;
    }
  });
  expect(stillThere).toBe(true);
});

test('AC8 - native Cut then paste into a share adds the file and removes the original', async ({
  page
}) => {
  await page.contents.uploadContent('cut me', 'text', 'ac8-cut.txt');
  const shareId = await createShare(page, 'ac8-cut-share');

  await page.filebrowser.openHomeDirectory();
  await page.filebrowser.revealFileInBrowser('ac8-cut.txt');
  await page
    .getByRole('region', { name: 'File Browser Section' })
    .getByRole('listitem', { name: /^Name: ac8-cut\.txt/ })
    .click();

  await page.evaluate(() =>
    (window as any).jupyterapp.commands.execute('filebrowser:cut')
  );
  await page.evaluate(
    id =>
      (window as any).jupyterapp.commands.execute(
        'share-files-panel:paste-into-share',
        { id }
      ),
    shareId
  );

  await expect
    .poll(() => shareEntryNames(page, shareId), { timeout: 15000 })
    .toContain('ac8-cut.txt');

  // Cut removes the original after the add succeeds (the paste command runs the
  // add then the delete in the background, so allow it time to land).
  await expect
    .poll(
      async () =>
        page.evaluate(async () => {
          try {
            await (window as any).jupyterapp.serviceManager.contents.get(
              'ac8-cut.txt',
              { content: false }
            );
            return true;
          } catch {
            return false;
          }
        }),
      { timeout: 15000 }
    )
    .toBe(false);
});

test('AC7 - a panel-copied entry pastes into the file browser via native paste', async ({
  page
}) => {
  // A local source file and a destination folder to paste into.
  await page.contents.uploadContent('pick me up', 'text', 'ac7-src.txt');
  await page.contents.createDirectory('ac7-dest');

  // Copy the local entry onto the extension clipboard (panel origin).
  await page.evaluate(() =>
    (window as any).jupyterapp.commands.execute(
      'share-files-panel:copy-local-entry',
      {
        path: 'ac7-src.txt'
      }
    )
  );

  // Move the file browser into the destination folder, then native paste -
  // the commandExecuted hook performs the copy because the clip is panel-origin.
  await page.filebrowser.openDirectory('ac7-dest');
  await page.evaluate(() =>
    (window as any).jupyterapp.commands.execute('filebrowser:paste')
  );

  await expect
    .poll(async () =>
      page.evaluate(async () => {
        try {
          await (window as any).jupyterapp.serviceManager.contents.get(
            'ac7-dest/ac7-src.txt',
            { content: false }
          );
          return true;
        } catch {
          return false;
        }
      })
    )
    .toBe(true);
});
