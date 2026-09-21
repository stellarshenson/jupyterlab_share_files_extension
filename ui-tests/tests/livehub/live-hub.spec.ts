import { expect, test } from '@jupyterlab/galata';

/**
 * Hub mode against the REAL hub: a JupyterLab spawned from inside a
 * hub-managed lab with the contract galaxahub injected into that lab, so the
 * extension talks to the hub the developer's own lab talks to. Nothing here is
 * mocked - a record exists on the hub until a test deletes it, and a
 * switch-on brings the hub's real tunnel up. Every fixture carries
 * the run's prefix; each test removes its own, and the first sweeps whatever
 * an aborted run left behind. The failure paths a live hub cannot be asked to
 * produce stay in `../hub`.
 */

const API = '/jupyterlab-share-files-extension/api';
const PANEL = '#jupyterlab-share-files-extension-panel';
const PREFIX = 'livehub-';
const RUN = `${PREFIX}${Date.now().toString(36)}`;

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

async function rows(page: any, kind: 'shares' | 'requests'): Promise<any[]> {
  const r = await api(page, 'GET', `${API}/${kind}`);
  expect(r.status).toBe(200);
  return r.data[kind];
}

async function row(page: any, kind: 'shares' | 'requests', id: string) {
  return (await rows(page, kind)).find((r: any) => r.id === id);
}

/** Delete every record on the hub whose name starts with `prefix`. */
async function sweep(page: any, prefix: string): Promise<void> {
  for (const kind of ['shares', 'requests'] as const) {
    for (const r of await rows(page, kind)) {
      if (r.name.startsWith(prefix)) {
        await api(page, 'DELETE', `${API}/${kind}/${r.id}`);
      }
    }
  }
}

function item(page: any, name: string) {
  return page.locator(`${PANEL} .jp-ShareFilesPanel-item`, { hasText: name });
}

/** Create a share of one small file in the test folder and wait until the
 * hub's transfer has copied it and the record is ready. */
async function readyShare(
  page: any,
  tmpPath: string,
  name: string
): Promise<any> {
  await page.contents.uploadContent(
    'live hub fixture\n',
    'text',
    `${tmpPath}/report.txt`
  );
  const created = await api(page, 'POST', `${API}/shares`, {
    name,
    paths: [`${tmpPath}/report.txt`]
  });
  expect(created.status).toBe(200);
  expect(['staging', 'ready']).toContain(created.data.state);
  await expect
    .poll(async () => (await row(page, 'shares', created.data.id))?.state, {
      timeout: 150000,
      message: 'the hub copies the file and marks the share ready'
    })
    .toBe('ready');
  return created.data;
}

// records an aborted run left behind, then this test's own
test.beforeEach(async ({ page }) => {
  await sweep(page, PREFIX);
});
test.afterEach(async ({ page }) => {
  await sweep(page, RUN);
});

test('api/info reports hub mode against the real hub and mounts no recipient or peer route', async ({
  page
}) => {
  const info = await api(page, 'GET', `${API}/info`);
  expect(info.status).toBe(200);
  expect(info.data.mode).toBe('hub');
  expect(info.data.hub).toMatchObject({ available: true });
  expect(typeof info.data.hub.serving).toBe('boolean');
  // the hub's own verdicts; the lab-side wait they replaced is gone
  expect(typeof info.data.tunnel_available).toBe('boolean');
  expect(typeof info.data.tunnel_ready).toBe('boolean');
  expect(info.data.tunnel_waiting).toBeUndefined();
  expect(info.data.tunnel_reason).toBeUndefined();
  for (const path of [
    '/jupyterlab-share-files-extension/public/share/AAAAAAAA',
    '/jupyterlab-share-files-extension/public/request/AAAAAAAA',
    `${API}/connections`
  ]) {
    expect((await api(page, 'GET', path)).status).toBe(404);
  }
});

test('a share of a workspace file is staged by the hub, becomes ready with its file listed, and delete removes it', async ({
  page,
  tmpPath
}) => {
  test.setTimeout(240000);
  const name = `${RUN}-share`;
  const created = await readyShare(page, tmpPath, name);
  expect(created.tunnel).toBe(false);
  expect(created.link).toMatch(new RegExp(`/s/${created.id}$`));
  const ready = await row(page, 'shares', created.id);
  expect(ready.entries.map((e: any) => e.name)).toEqual(['report.txt']);
  expect(ready.bytes).toBeGreaterThan(0);
  await openPanel(page);
  await refreshPanel(page);
  await expect(
    item(page, name).locator('.jp-ShareFilesPanel-itemMeta')
  ).toHaveText('1 item');
  await item(page, name).locator('.jp-ShareFilesPanel-itemHeader').click();
  await expect(
    item(page, name).locator('.jp-ShareFilesPanel-entryName')
  ).toHaveText('report.txt');
  const gone = await api(page, 'DELETE', `${API}/shares/${created.id}`);
  expect(gone.status).toBe(200);
  expect(await row(page, 'shares', created.id)).toBeUndefined();
  await refreshPanel(page);
  await expect(item(page, name)).toHaveCount(0);
});

test('an empty share is filled, and its file is renamed into a folder and removed, on the real hub', async ({
  page,
  tmpPath
}) => {
  test.setTimeout(240000);
  await page.contents.uploadContent(
    'added later\n',
    'text',
    `${tmpPath}/late.txt`
  );
  const created = await api(page, 'POST', `${API}/shares`, {
    name: `${RUN}-edit`,
    paths: []
  });
  expect(created.status).toBe(200);
  expect(created.data.state).toBe('ready');
  const items = `${API}/shares/${created.data.id}/items`;
  const names = async () =>
    (await row(page, 'shares', created.data.id)).entries.map(
      (e: any) => e.name
    );

  expect(
    (await api(page, 'POST', items, { paths: [`${tmpPath}/late.txt`] })).status
  ).toBe(200);
  // the hub's row carries last_add, which the lab relays as adding
  await expect
    .poll(names, { timeout: 150000, message: 'the add lands' })
    .toEqual(['late.txt']);
  await expect
    .poll(async () => (await row(page, 'shares', created.data.id)).adding)
    .toBe(false);
  expect((await row(page, 'shares', created.data.id)).add_reason).toBe('');

  // an add of a path that is not there is accepted, then refused on the row
  expect(
    (await api(page, 'POST', items, { paths: [`${tmpPath}/not-there.txt`] }))
      .status
  ).toBe(200);
  await expect
    .poll(async () => (await row(page, 'shares', created.data.id)).add_reason, {
      timeout: 60000
    })
    .not.toBe('');

  // a name the share holds is refused by name before the hub is asked
  const again = await api(page, 'POST', items, {
    paths: [`${tmpPath}/late.txt`]
  });
  expect(again.status).toBe(400);
  expect(again.data.error).toContain('already holds late.txt');

  const moved = await api(page, 'PUT', items, {
    name: 'late.txt',
    new_name: 'kept/later.txt'
  });
  expect(moved.status).toBe(200);
  expect(await names()).toEqual(['kept/later.txt']);
  const taken = await api(page, 'PUT', items, {
    name: 'kept/later.txt',
    new_name: 'kept'
  });
  expect(taken.status).toBe(400);
  expect(taken.data.reason).toBe('name_taken');

  const removed = await api(page, 'DELETE', `${items}?name=kept`);
  expect(removed.status).toBe(200);
  expect(await names()).toEqual([]);
});

test('a request is created on the hub and deleted', async ({ page }) => {
  const name = `${RUN}-request`;
  const created = await api(page, 'POST', `${API}/requests`, { name });
  expect(created.status).toBe(200);
  expect(created.data.tunnel).toBe(false);
  expect(created.data.link).toMatch(new RegExp(`/s/${created.data.id}$`));
  await openPanel(page);
  await refreshPanel(page);
  await expect(item(page, name)).toBeVisible();
  const gone = await api(page, 'DELETE', `${API}/requests/${created.data.id}`);
  expect(gone.status).toBe(200);
  expect(await row(page, 'requests', created.data.id)).toBeUndefined();
  await refreshPanel(page);
  await expect(item(page, name)).toHaveCount(0);
});

test('the hub refuses what it cannot do and the lab relays the reason', async ({
  page,
  tmpPath
}) => {
  test.setTimeout(240000);
  // an unknown record: the hub's 404 is relayed with its message
  const missing = await api(page, 'POST', `${API}/shares/ZZZZZZZZ/tunnel`, {
    tunnel: true
  });
  expect(missing.status).toBe(404);
  expect(typeof missing.data.error).toBe('string');
  expect(missing.data.error.length).toBeGreaterThan(0);
  expect((await api(page, 'DELETE', `${API}/requests/ZZZZZZZZ`)).status).toBe(
    404
  );
  // a path outside the workspace never reaches the hub
  const unsafe = await api(page, 'POST', `${API}/shares`, {
    name: `${RUN}-unsafe`,
    paths: ['../etc/passwd']
  });
  expect(unsafe.status).toBe(400);
  expect(unsafe.data.error).toContain('Unsafe path');
  // a file the workspace does not hold: the hub accepts the record, its
  // transfer cannot read the file, and the record ends refused with the
  // hub's reason, which the row shows
  const name = `${RUN}-nofile`;
  const nofile = await api(page, 'POST', `${API}/shares`, {
    name,
    paths: [`${tmpPath}/does-not-exist.txt`]
  });
  expect(nofile.status).toBe(200);
  await expect
    .poll(async () => (await row(page, 'shares', nofile.data.id))?.state, {
      timeout: 150000,
      message: 'the hub gives up on the transfer and refuses the record'
    })
    .toBe('refused');
  const refused = await row(page, 'shares', nofile.data.id);
  expect(refused.reason).not.toBe('');
  await openPanel(page);
  await refreshPanel(page);
  await expect(
    item(page, name).locator('.jp-ShareFilesPanel-itemMeta')
  ).toHaveAttribute('title', new RegExp(`refused: ${refused.reason}$`));
});

test("a tunnel switch-on brings the hub's tunnel up, the link moves to the tunnel host, and off brings it back", async ({
  page,
  tmpPath
}) => {
  test.setTimeout(480000);
  const name = `${RUN}-tunnel`;
  const created = await readyShare(page, tmpPath, name);
  const before = new URL(created.link);
  const on = await api(page, 'POST', `${API}/shares/${created.id}/tunnel`, {
    tunnel: true
  });
  expect(on.status).toBe(200);
  expect(on.data).toEqual({ id: created.id, tunnel: true });
  // the hub provisions its tunnel on demand and composes the record's link
  // on the tunnel hostname once its connector serves; the lab polls nothing
  // and only reads what the hub reports
  await expect
    .poll(
      async () => {
        const r = await row(page, 'shares', created.id);
        if (!r || !r.tunnel) {
          return '';
        }
        const url = new URL(r.link);
        return url.hostname === before.hostname ? '' : url.protocol;
      },
      {
        timeout: 240000,
        message: "the record's link moves to the hub's tunnel host over https"
      }
    )
    .toBe('https:');
  // the hub's readiness reaches api/info on the capabilities tick
  await expect
    .poll(
      async () => (await api(page, 'GET', `${API}/info`)).data.tunnel_ready,
      {
        timeout: 60000,
        message: 'the hub reports its tunnel ready once the connector serves'
      }
    )
    .toBe(true);
  // one row switched on its own: the header toggle (the stored default)
  // stays off, and the row's link dialog shows the tunnel address
  await openPanel(page);
  await refreshPanel(page);
  await expect(
    page.locator(`${PANEL} .jp-ShareFilesPanel-cloudIndicator`)
  ).not.toHaveClass(/jp-mod-active/);
  await item(page, name).locator('button[title="Copy link"]').click();
  await expect(page.locator('.jp-Dialog input[readonly]')).toHaveValue(
    /^https:\/\//
  );
  await page.locator('.jp-Dialog button', { hasText: 'Close' }).click();
  // off: the link returns to the hub's own address
  const off = await api(page, 'POST', `${API}/shares/${created.id}/tunnel`, {
    tunnel: false
  });
  expect(off.status).toBe(200);
  await expect
    .poll(
      async () => {
        const r = await row(page, 'shares', created.id);
        return r ? [r.tunnel, new URL(r.link).hostname] : null;
      },
      { timeout: 60000 }
    )
    .toEqual([false, before.hostname]);
});
