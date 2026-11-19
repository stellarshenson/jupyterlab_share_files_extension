import { expect } from '@jupyterlab/galata';

/**
 * A destructive menu entry (design system, "Destructive menu entry"): the
 * label and the icon take the error colour, at rest and while hovered, and
 * the entry stands alone at the end of its menu under a separator.
 */
export async function expectDangerItem(item: any): Promise<void> {
  const page = item.page();
  const label = item.locator('.lm-Menu-itemLabel');
  const icon = item.locator('.lm-Menu-itemIcon svg');
  const colours = async () => ({
    label: await label.evaluate((el: Element) => getComputedStyle(el).color),
    icon: await icon.evaluate((el: SVGElement) => getComputedStyle(el).fill)
  });
  const error = await page.evaluate(() => {
    const probe = document.createElement('span');
    probe.style.color = 'var(--jp-error-color1)';
    document.body.appendChild(probe);
    const value = getComputedStyle(probe).color;
    probe.remove();
    return value;
  });
  expect(await colours()).toEqual({ label: error, icon: error });
  // selected by pointer, both keep the error colour
  await item.hover();
  await expect(item).toHaveClass(/lm-mod-active/);
  expect(await colours()).toEqual({ label: error, icon: error });
  // the item stands in its own group at the end, under a separator
  expect(
    await item.evaluate((el: Element) => ({
      last: el === el.parentElement!.lastElementChild,
      afterSeparator:
        el.previousElementSibling?.getAttribute('data-type') === 'separator'
    }))
  ).toEqual({ last: true, afterSeparator: true });
}
