import { expect, test } from '@jupyterlab/galata';

/**
 * Regression tests for the public manifest cache contract.
 *
 * A manifest must never be stored by a cache. It is per-caller (a request
 * manifest shows only the caller's own uploads, keyed on a cookie) and it
 * changes whenever a file is added or removed. Pre-fix it carried an ETag,
 * no `Cache-Control` and no `Vary: Cookie`, so a shared cache keyed on URL
 * alone could serve one uploader's file list to another, and a browser could
 * reuse a stored copy under heuristic freshness and show a stale list.
 *
 * These drive the real server and fail against the pre-fix code. Note the
 * conditional-request test below sets `If-None-Match` explicitly: that is how
 * the *contract* is pinned, not a reproduction of a client bug - a browser
 * consumes the 304 its own cache solicited and resolves with the stored 200.
 */

const API = '/jupyterlab-share-files-extension/api';
const PUBLIC = '/jupyterlab-share-files-extension/public';

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

/** Create a request through the extension API, return its id. */
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

/** GET a manifest, returning status plus the cache-relevant headers. */
async function probeManifest(
  page: any,
  url: string,
  ifNoneMatch?: string
): Promise<{
  status: number;
  etag: string | null;
  cacheControl: string | null;
}> {
  return page.evaluate(
    async ([u, inm]: [string, string | null]) => {
      const r = await fetch(u, {
        cache: 'no-store',
        headers: inm ? { 'If-None-Match': inm } : undefined
      });
      return {
        status: r.status,
        etag: r.headers.get('etag'),
        cacheControl: r.headers.get('cache-control')
      };
    },
    [url, ifNoneMatch ?? null]
  );
}

test('public share manifest is served without an ETag', async ({ page }) => {
  const id = await createShare(page, 'cache-share');
  const res = await probeManifest(page, `${PUBLIC}/share/${id}/manifest`);
  expect(res.status).toBe(200);
  // an ETag is what makes the browser revalidate and get a 304 back
  expect(res.etag).toBeNull();
  expect(res.cacheControl).toContain('no-store');
});

test('public share manifest never answers 304 to a conditional request', async ({
  page
}) => {
  const id = await createShare(page, 'cache-share-304');
  const url = `${PUBLIC}/share/${id}/manifest`;
  // Pin the server contract: it must hand out no validator, and must not
  // answer a conditional request with a body-less 304 that any intermediary
  // could turn into a stored, replayable response. Post-fix `first.etag` is
  // null so the fallback validator is what gets sent (and 200 is trivially
  // right); the fallback exists only so that against PRE-fix code the real
  // ETag is echoed back and the server's 304 fails this assertion.
  const first = await probeManifest(page, url);
  expect(first.status).toBe(200);
  const second = await probeManifest(page, url, first.etag ?? '"no-validator"');
  expect(second.status).toBe(200);
});

test('public request manifest is served without an ETag', async ({ page }) => {
  const id = await createRequest(page, 'cache-request');
  const res = await probeManifest(page, `${PUBLIC}/request/${id}/manifest`);
  expect(res.status).toBe(200);
  expect(res.etag).toBeNull();
  expect(res.cacheControl).toContain('no-store');
});

test('repeated manifest polls each return a usable body', async ({ page }) => {
  const id = await createShare(page, 'cache-poll');
  const url = `${PUBLIC}/share/${id}/manifest`;
  // Mirrors the panel's poll: fetch the same manifest twice through the
  // browser's normal cache path and require a usable body each time. This is
  // a sanity check, not the regression guard - whether the browser chooses to
  // revalidate is up to its heuristics, so the 304 is pinned deterministically
  // by the conditional-request test above.
  const results = await page.evaluate(async (u: string) => {
    const out: { status: number; name?: string }[] = [];
    for (let i = 0; i < 2; i++) {
      const r = await fetch(u, { credentials: 'omit' });
      let name: string | undefined;
      try {
        name = (await r.json()).name;
      } catch {
        name = undefined;
      }
      out.push({ status: r.status, name });
    }
    return out;
  }, url);
  for (const r of results) {
    expect(r.status).toBe(200);
    expect(r.name).toBe('cache-poll');
  }
});
