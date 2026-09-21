import { expect } from '@jupyterlab/galata';

/**
 * Dragging a file-browser row onto a panel drop target.
 *
 * The file browser drags with Lumino, which reads real mouse events: a
 * mousedown on the row, a move past the 5px threshold that starts the drag,
 * then moves that carry `lm-dragenter` / `lm-dragover` to whatever sits under
 * the cursor, and a mouseup that fires `lm-drop` there. Playwright's
 * `dragAndDrop` helper sends the HTML5 drag events instead, which Lumino
 * ignores, so this drives the mouse directly.
 *
 * The release waits for the target's own highlight class: Lumino sets its
 * current drop target from the `lm-dragenter` it dispatches under the
 * cursor, and under parallel load that can land several frames after the
 * move. Releasing on a timer instead dropped on nothing about one run in
 * three.
 */
export async function dragOnto(
  page: any,
  name: string,
  target: any,
  others: string[] = []
): Promise<void> {
  await page.filebrowser.openHomeDirectory();
  await page.filebrowser.revealFileInBrowser(name);
  const source = page
    .getByRole('region', { name: 'File Browser Section' })
    .getByRole('listitem', { name: new RegExp(`^Name: ${name}`) });
  await source.click();
  // a multi-selection: the drag of one selected row carries them all
  for (const other of others) {
    await page
      .getByRole('region', { name: 'File Browser Section' })
      .getByRole('listitem', { name: new RegExp(`^Name: ${other}`) })
      .click({ modifiers: ['ControlOrMeta'] });
  }
  const from = await source.boundingBox();
  const to = await target.boundingBox();
  if (!from || !to) {
    throw new Error(`no bounding box for ${name}`);
  }
  const toX = to.x + to.width / 2;
  const toY = to.y + Math.min(to.height / 2, 12);
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  // past Lumino's 5px drag threshold first, then onto the target
  await page.mouse.move(from.x + from.width / 2 + 12, from.y + from.height / 2);
  await page.mouse.move(toX, toY, { steps: 12 });
  // nudge until the target takes the highlight, so the release lands on it
  for (let i = 0; i < 20; i++) {
    const lit = await target.evaluate((el: HTMLElement) =>
      el.classList.contains('jp-mod-dropTarget')
    );
    if (lit) {
      break;
    }
    await page.mouse.move(toX, toY + (i % 2 === 0 ? 1 : -1));
  }
  await expect(target).toHaveClass(/jp-mod-dropTarget/);
  await page.mouse.up();
}
