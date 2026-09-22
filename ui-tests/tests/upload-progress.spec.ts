import { expect, test } from '@jupyterlab/galata';

/**
 * ACC-PROG-172 on the recipient's upload page: a file being uploaded reports
 * itself by tinting its own row - an accent layer lying over the name and
 * the size, as wide as the fraction sent - and no separate bar is drawn
 * beside it. The recipient page opens in its own tab: the galata page stays
 * on the lab, which its fixture requires.
 */

const API = '/jupyterlab-share-files-extension/api';
const PORT = process.env.JUPYTER_TEST_PORT || '8888';

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

test('an upload tints its own row on the recipient page, with no bar beside it', async ({
  page
}) => {
  const made = await api(page, 'POST', `${API}/requests`, {
    name: 'progress-one'
  });
  expect(made.status).toBe(200);

  const recipient = await page.context().newPage();
  try {
    await recipient.goto(
      `http://localhost:${PORT}/jupyterlab-share-files-extension/public/request/${made.data.id}`
    );
    const card = recipient.locator('.progress');
    await expect(card).toHaveCount(0);
    await recipient.locator('.dropzone input[type=file]').setInputFiles({
      name: 'sent.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('x'.repeat(200000))
    });
    await expect(recipient.locator('.status.ok')).toHaveText(
      'Uploaded: sent.txt'
    );

    // the layer is the whole indicator: the old strip beside the row is gone
    await expect(recipient.locator('.progress-bar')).toHaveCount(0);
    const fill = card.locator('.progress-fill');
    await expect(fill).toHaveAttribute('aria-label', 'Uploading sent.txt');
    await expect(fill).toHaveAttribute('aria-valuenow', '100');

    // it lies over the row, covering it end to end once the file is sent -
    // polled, because the width eases into place over its transition
    await expect
      .poll(async () => {
        const f = (await fill.boundingBox())!;
        const c = (await card.boundingBox())!;
        return c.width - f.width;
      })
      .toBeLessThan(3);
    const fillBox = (await fill.boundingBox())!;
    const cardBox = (await card.boundingBox())!;
    expect(fillBox.height).toBeGreaterThan(cardBox.height - 3);
    expect(fillBox.x).toBeLessThan(cardBox.x + 2);
    // the name stays readable through it: the layer is faint, not solid
    expect(
      Number(await fill.evaluate(el => getComputedStyle(el).opacity))
    ).toBeLessThan(0.5);
  } finally {
    await recipient.close();
  }
});
