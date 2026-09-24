/**
 * The lowest opacity the header cloud icon shows while it reads switching.
 *
 * One breath of the switching cloud lasts 2.4 s, and the glyph is nearly
 * invisible for only about half a second of it. A poll from the test runner
 * lands a few times a second and misses that window, so the page records the
 * opacity itself on every animation frame.
 */
export function watchTrough(page: any, selector: string): Promise<void> {
  return page.evaluate((sel: string) => {
    const w = window as any;
    w.__trough = 1;
    cancelAnimationFrame(w.__troughFrame);
    const sample = () => {
      const el = document.querySelector(sel);
      if (el?.classList.contains('jp-mod-connecting')) {
        w.__trough = Math.min(w.__trough, Number(getComputedStyle(el).opacity));
      }
      w.__troughFrame = requestAnimationFrame(sample);
    };
    sample();
  }, selector);
}

/** The lowest opacity recorded since the last read, and the record reset. */
export function readTrough(page: any): Promise<number> {
  return page.evaluate(() => {
    const w = window as any;
    const trough = w.__trough;
    w.__trough = 1;
    return trough;
  });
}
