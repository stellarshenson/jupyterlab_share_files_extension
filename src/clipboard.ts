/**
 * Extension-owned clipboard shared by the panel and the plugin.
 *
 * JupyterLab's file-browser clipboard (`DirListing._clipboard`/`_isCut`) is
 * private - it cannot be read or written from another extension. To bridge
 * copy/paste between the file browser and the Share Files panel we keep our own
 * clipboard here and mirror the native file-browser copy/cut/paste through the
 * `commands.commandExecuted` signal (see `src/index.ts`).
 *
 * `origin` records who filled the clipboard so the file-browser paste hook
 * knows whether native paste already handled the item (`fb`) or whether we must
 * perform the transfer ourselves (`panel`).
 */

export type ShareClip =
  | {
      kind: 'local';
      origin: 'fb' | 'panel';
      mode: 'copy' | 'cut';
      paths: string[];
    }
  | {
      kind: 'remote';
      origin: 'panel';
      items: { connKey: string; name: string; type: string }[];
    }
  | null;

let _clip: ShareClip = null;

export function getClip(): ShareClip {
  return _clip;
}

export function setClip(clip: ShareClip): void {
  _clip = clip;
}

/** Empty the clipboard if it still holds `clip`: a paste that ends after the
 * user cut or copied something newer leaves the newer clip in place. */
export function clearClip(clip: ShareClip): void {
  if (_clip === clip) {
    _clip = null;
  }
}
