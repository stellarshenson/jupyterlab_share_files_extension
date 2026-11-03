import { expect, test } from '@jupyterlab/galata';
import { dragOnto } from '../helpers/drag';

/**
 * Gate for DEF-HUB-141-ADD-SLOW against the REAL hub: a drop on a share's row
 * in a real panel, timed until that row lists the file and no longer reads
 * adding. Each add goes into a new share and prints one `add-timing` line.
 * Prefix and clean-up follow live-hub.spec.ts: a fixture carries the run's
 * prefix, each test removes its own, and each test first sweeps what an
 * aborted run left behind.
 *
 * shortcut: this lab has no hub proxy in front of it, so the owner's own path is not measured, add that leg if this gate passes while the owner still waits a minute
 */

const API = '/jupyterlab-share-files-extension/api';
const PANEL = '#jupyterlab-share-files-extension-panel';
const PREFIX = 'livehub-';
const RUN = `${PREFIX}timing-${Date.now().toString(36)}`;
const KB = `${'x'.repeat(1023)}\n`;
const BAR_SECONDS = 10;
const IDLE_MS = 180000;
// well past the bar, so an add of a minute is measured and not cut off
const WAIT_MS = 150000;

/** Call the extension API from the page, as live-hub.spec.ts does. */
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

/** Delete every record on the hub whose name starts with `prefix`. */
async function sweep(page: any, prefix: string): Promise<void> {
  for (const kind of ['shares', 'requests'] as const) {
    const listed = await api(page, 'GET', `${API}/${kind}`);
    expect(listed.status).toBe(200);
    for (const r of listed.data[kind]) {
      if (r.name.startsWith(prefix)) {
        await api(page, 'DELETE', `${API}/${kind}/${r.id}`);
      }
    }
  }
}

function item(page: any, name: string) {
  return page.locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name });
}

async function openPanel(page: any): Promise<void> {
  await page.sidebar.openTab('jupyterlab-share-files-extension-panel');
  await expect(page.locator(PANEL)).toBeVisible();
}

/** A folder the hub can read: a sandbox runner names the one real folder it
 * lets in, and a lab rooted on the workspace volume has `tmpPath`. */
function hubReadable(tmpPath: string): string {
  return process.env.LIVEHUB_FOLDER ?? tmpPath;
}

/** A new 1 KB file, and a new empty share with its row open in the panel. */
async function prepare(
  page: any,
  folder: string,
  title: string
): Promise<string> {
  const file = `${title}.txt`;
  await page.contents.uploadContent(KB, 'text', `${folder}/${file}`);
  const made = await api(page, 'POST', `${API}/shares`, {
    name: title,
    paths: []
  });
  expect(made.status).toBe(200);
  const refresh = page.locator(`${PANEL} button[title="Refresh"]`);
  await refresh.click();
  await expect(refresh).not.toHaveClass(/jp-mod-spinning/);
  const row = item(page, title);
  await row.locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(row.locator('.jp-ShareFilesPanel-empty')).toHaveText(
    'Empty - drag files onto this share to add'
  );
  return file;
}

/** Drop the file on its share's row, print the seconds until the row lists it
 * and no longer reads adding, and delete the share. */
async function timedAdd(
  page: any,
  folder: string,
  label: string,
  title: string,
  file: string
): Promise<void> {
  const row = item(page, title);
  const listed = row.locator('.jp-ShareFilesPanel-entryName', {
    hasText: file
  });
  const meta = row.locator('.jp-ShareFilesPanel-itemMeta');
  let reads = 0;
  const countRead = (r: any) => {
    if (r.method() === 'GET' && /\/api\/shares(\?|$)/.test(r.url())) {
      reads++;
    }
  };
  await dragOnto(page, `${folder}/${file}`, row);
  page.on('request', countRead);
  const dropped = Date.now();
  await expect
    .poll(
      async () =>
        (await listed.count()) === 1 &&
        !(await meta.innerText()).includes('adding'),
      {
        intervals: [100],
        timeout: WAIT_MS,
        message: `the row lists ${file} and no longer reads adding`
      }
    )
    .toBe(true);
  const seconds = (Date.now() - dropped) / 1000;
  page.off('request', countRead);
  console.log(`add-timing ${JSON.stringify({ case: label, seconds, reads })}`);
  // soft: the adds after a slow one are still measured
  expect.soft(seconds).toBeLessThanOrEqual(BAR_SECONDS);
  await sweep(page, title);
}

test.beforeEach(async ({ page }) => {
  await sweep(page, PREFIX);
});
test.afterEach(async ({ page }) => {
  await sweep(page, RUN);
});

test('T1: ten adds, one after another, each settle within the bar', async ({
  page,
  tmpPath
}) => {
  test.setTimeout(10 * WAIT_MS);
  const folder = hubReadable(tmpPath);
  await openPanel(page);
  for (let n = 1; n <= 10; n++) {
    const title = `${RUN}-t1-${n}`;
    const file = await prepare(page, folder, title);
    await timedAdd(page, folder, 'T1', title, file);
  }
});

test('T2: an add after the panel sat idle for 180 s settles within the bar', async ({
  page,
  tmpPath
}) => {
  test.setTimeout(IDLE_MS + 2 * WAIT_MS);
  const folder = hubReadable(tmpPath);
  const title = `${RUN}-t2`;
  await openPanel(page);
  const file = await prepare(page, folder, title);
  await page.waitForTimeout(IDLE_MS);
  await timedAdd(page, folder, 'T2', title, file);
});

test("T3: with the panel's stream blocked, the once-a-second read carries the row within the bar", async ({
  page,
  tmpPath
}) => {
  test.setTimeout(3 * WAIT_MS);
  const folder = hubReadable(tmpPath);
  let blocked = 0;
  await page.routeWebSocket(`**${API}/stream*`, () => {
    blocked++;
  });
  await page.reload();
  await openPanel(page);
  await expect.poll(() => blocked).toBeGreaterThan(0);
  for (let n = 1; n <= 3; n++) {
    const title = `${RUN}-t3-${n}`;
    const file = await prepare(page, folder, title);
    await timedAdd(page, folder, 'T3', title, file);
  }
});
