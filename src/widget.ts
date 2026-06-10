/**
 * ShareFilesPanel - Lumino side panel widget for the share-files extension.
 *
 * Vanilla DOM rendering, polling-based refresh, drag-drop targets, and
 * row-level context menus via Lumino's Menu and CommandRegistry.
 */

import { Dialog, Notification, showDialog } from '@jupyterlab/apputils';
import { IDocumentManager } from '@jupyterlab/docmanager';
import { Contents, ServerConnection } from '@jupyterlab/services';
import { filterIcon } from '@jupyterlab/ui-components';
import { CommandRegistry } from '@lumino/commands';
import { MimeData } from '@lumino/coreutils';
import { Drag } from '@lumino/dragdrop';
import { Menu, Widget } from '@lumino/widgets';
import qrcode from 'qrcode-generator';

import {
  addConnection,
  addShareItems,
  checkLink,
  createRequest,
  createShare,
  deleteRequest,
  deleteShare,
  fetchRemoteRequest,
  fetchRemoteShare,
  getInfo,
  IExtensionInfo,
  listConnections,
  listRequests,
  listShares,
  removeConnection,
  removeRequestUpload,
  removeShareItems,
  resetTunnel,
  saveFromConnection,
  setTunnel,
  setupTunnel,
  uploadToConnection
} from './api';
import { clearClip, getClip, setClip } from './clipboard';
import {
  addIcon,
  closeIcon,
  cloudIcon,
  cloudOffIcon,
  disconnectIcon,
  fileIcon,
  folderIcon,
  linkIcon,
  refreshIcon,
  shareIcon,
  trashIcon
} from './icons';
import {
  IConnection,
  IRemoteRequest,
  IRemoteShare,
  IRequest,
  IShare,
  IShareEntry
} from './types';

const DEFAULT_POLL_INTERVAL_SECONDS = 15;
const MIN_POLL_INTERVAL_SECONDS = 2;
const CONTENTS_MIME = 'application/x-jupyter-icontents';
// Our own MIME for dragging a connected (remote) share entry out of the panel.
// Carries `[{ key, name, type }]`; the file browser drop patch resolves it to a
// server-side `saveFromConnection` (the bytes live on a peer, not locally).
const REMOTE_MIME = 'application/x-share-files-remote';
// Lumino dock-panel drop MIME: data is a thunk returning the Widget to dock.
// Setting this lets share entries be dropped onto the tab bar / dock area to
// open the file - the same mechanism the file browser uses for its own drags.
const FACTORY_MIME = 'application/vnd.lumino.widget-factory';

interface IPanelState {
  shares: IShare[];
  requests: IRequest[];
  connections: IConnection[];
  connectionData: Map<string, IRemoteShare | IRemoteRequest | null>;
  busyKeys: Set<string>;
  offlineKeys: Set<string>;
  expanded: { shares: boolean; requests: boolean; connected: boolean };
  expandedItems: Set<string>;
  lastSeenUploads: Map<string, number>;
  info: IExtensionInfo | null;
  /** Sub-path inside each owned share's data dir (workspace-relative). Missing key = at root. */
  shareSubPath: Map<string, string>;
  /** Cached entries by workspace-relative folder path (Contents API result). */
  subEntries: Map<string, IShareEntry[]>;
}

export interface IShareFilesSettings {
  enableShares: boolean;
  enableRequests: boolean;
  showHiddenFiles: boolean;
  /** Bring the Cloudflare tunnel up at server startup (public links). */
  tunnelAutostart: boolean;
  /** Panel poll interval in seconds (one tick refreshes all shares/requests). */
  pollIntervalSeconds: number;
}

export interface IShareFilesPanelOptions {
  serverSettings: ServerConnection.ISettings;
  commands: CommandRegistry;
  settings: IShareFilesSettings;
  /** JupyterLab contents service for copy-to-file-browser */
  contents: Contents.IManager;
  /** Document manager - opens a share entry in a tab when dropped on the dock */
  docManager: IDocumentManager;
  /** Returns the file browser's current relative directory */
  getCurrentDir: () => string;
  /** Navigate the file browser to a given relative directory and select a name */
  revealInFileBrowser: (dirPath: string, name?: string) => Promise<void>;
}

export class ShareFilesPanel extends Widget {
  constructor(options: IShareFilesPanelOptions) {
    super();
    this.id = 'jupyterlab-share-files-extension-panel';
    this.title.caption = 'Share Files';
    this.title.icon = shareIcon;
    this.addClass('jp-ShareFilesPanel');

    this._serverSettings = options.serverSettings;
    this._commands = options.commands;
    this._settings = { ...options.settings };
    this._contents = options.contents;
    this._docManager = options.docManager;
    this._getCurrentDir = options.getCurrentDir;
    this._revealInFileBrowser = options.revealInFileBrowser;
    this._state = {
      shares: [],
      requests: [],
      connections: [],
      connectionData: new Map(),
      busyKeys: new Set(),
      offlineKeys: new Set(),
      expanded: { shares: true, requests: true, connected: true },
      expandedItems: new Set(),
      lastSeenUploads: new Map(),
      info: null,
      shareSubPath: new Map(),
      subEntries: new Map()
    };
    this._registerCommands();
    this._buildShell();
    this._render();
    void this.refresh();
  }

  // ------------------------------------------------------------------ //
  // Public API
  // ------------------------------------------------------------------ //

  /** Current effective settings - read-only view for outside callers. */
  get settings(): Readonly<IShareFilesSettings> {
    return this._settings;
  }

  /** Apply new settings from the SettingRegistry. */
  updateSettings(next: IShareFilesSettings): void {
    const prevInterval = this._settings.pollIntervalSeconds;
    const prevAutostart = this._settings.tunnelAutostart;
    this._settings = { ...next };
    this._render();
    // Restart the poll loop if the interval changed and we are attached.
    if (next.pollIntervalSeconds !== prevInterval && this._pollHandle) {
      this._restartPolling();
    }
    // Persist the autostart preference server-side - the server reads it at
    // startup to decide whether to bring the tunnel up.
    if (next.tunnelAutostart !== prevAutostart) {
      void setTunnel(this._serverSettings, {
        autostart: next.tunnelAutostart
      }).catch(() => {
        /* not configured yet - nothing to persist */
      });
    }
  }

  /**
   * Create a share from the given workspace-relative paths.
   * Used by both the context menu and the drop zone flow.
   */
  async createShareFlow(paths: string[]): Promise<void> {
    if (!this._settings.enableShares) {
      Notification.warning(
        'File sharing is disabled in Settings. Enable it to create a share.'
      );
      return;
    }
    const suggested = this._suggestName(paths);
    const name = await this._promptForName('Create new share', suggested);
    if (!name) {
      return;
    }
    const tempKey = '__pending_share';
    this._state.busyKeys.add(tempKey);
    this._render();
    // creating the link can take a moment - show a spinner toast meanwhile
    const pending = Notification.emit(
      `Creating share "${name}"...`,
      'in-progress',
      {
        autoClose: false
      }
    );
    try {
      const share = await createShare(this._serverSettings, name, paths);
      await this._copyLinkToClipboard(share.link);
      Notification.update({
        id: pending,
        message: `Share "${share.name}" created - link copied`,
        type: 'success',
        autoClose: 5000
      });
    } catch (err: any) {
      Notification.update({
        id: pending,
        message: `Could not create share: ${err.message || err}`,
        type: 'error',
        autoClose: 5000
      });
    } finally {
      this._state.busyKeys.delete(tempKey);
      await this.refresh();
    }
  }

  /** Create a new file request via the [+] menu. */
  async createRequestFlow(): Promise<void> {
    if (!this._settings.enableRequests) {
      Notification.warning(
        'File requests are disabled in Settings. Enable them to create a request.'
      );
      return;
    }
    const name = await this._promptForName('Create new file request', '');
    if (!name) {
      return;
    }
    const pending = Notification.emit(
      `Creating request "${name}"...`,
      'in-progress',
      { autoClose: false }
    );
    try {
      const req = await createRequest(this._serverSettings, name);
      await this._copyLinkToClipboard(req.link);
      Notification.update({
        id: pending,
        message: `Request "${req.name}" created - link copied`,
        type: 'success',
        autoClose: 5000
      });
    } catch (err: any) {
      Notification.update({
        id: pending,
        message: `Could not create request: ${err.message || err}`,
        type: 'error',
        autoClose: 5000
      });
    } finally {
      await this.refresh();
    }
  }

  /** Add files (workspace-relative paths) to an existing share by id. */
  async addToShareFlow(shareId: string, paths: string[]): Promise<void> {
    this._state.busyKeys.add(shareId);
    this._render();
    try {
      await addShareItems(this._serverSettings, shareId, paths);
      Notification.success(`${paths.length} item(s) added`);
    } catch (err: any) {
      Notification.error(`Could not add items: ${err.message || err}`);
    } finally {
      this._state.busyKeys.delete(shareId);
      await this.refresh();
    }
  }

  /** Upload local files to a connected request. */
  async uploadToConnectionFlow(
    connectionKey: string,
    paths: string[]
  ): Promise<void> {
    this._state.busyKeys.add(connectionKey);
    this._render();
    try {
      await uploadToConnection(this._serverSettings, connectionKey, paths, '');
      Notification.success(`${paths.length} item(s) uploaded`);
    } catch (err: any) {
      Notification.error(`Upload failed: ${err.message || err}`);
    } finally {
      this._state.busyKeys.delete(connectionKey);
      await this.refresh();
    }
  }

  /** Force-refresh from the server. The icon animation is feedback for an
   * explicit click only (spin=true) - background polls and programmatic
   * refreshes run without it. */
  async refresh(spin = false): Promise<void> {
    if (spin && this._refreshBtn) {
      this._refreshBtn.classList.add('jp-mod-spinning');
    }
    // Local fetches return in <100 ms - without a floor the spinner runs for
    // a single frame and reads as "nothing happened". Hold for at least
    // ~600 ms so the click visibly registers.
    const minSpin = new Promise<void>(resolve =>
      window.setTimeout(resolve, spin ? 600 : 0)
    );
    const work = (async () => {
      try {
        const [s, r, c] = await Promise.all([
          listShares(this._serverSettings),
          listRequests(this._serverSettings),
          listConnections(this._serverSettings)
        ]);
        this._state.shares = s.shares || [];
        this._state.requests = r.requests || [];
        this._state.connections = c.connections || [];
        // Drilled-in folder listings come from the Contents API and can go
        // stale after files are added/removed - drop the cache each refresh.
        this._state.subEntries.clear();
        this._detectNewUploads();
        // refetch info each tick - public_base_url can change at runtime
        // (cloudflare --setup / --reset apply without a server restart)
        try {
          this._state.info = await getInfo(this._serverSettings);
        } catch {
          // keep the previous value - we just won't update the hints
        }
        this._updateCloudIndicator();
        // refresh connection data in parallel
        await Promise.all(
          this._state.connections.map(conn => this._refreshConnection(conn))
        );
      } catch (err: any) {
        console.error('Share Files: refresh failed', err);
      }
      this._render();
    })();
    await Promise.all([work, minSpin]);
    if (spin && this._refreshBtn) {
      this._refreshBtn.classList.remove('jp-mod-spinning');
    }
  }

  /** Submit a link to connect to. Used by the connect input. */
  async connectToLink(link: string): Promise<void> {
    if (!link) {
      return;
    }
    // Refuse to connect to ourselves - that produces a loop and is never
    // useful. On JupyterHub, two users share the same host but live at
    // different `/user/<name>/` prefixes, so a plain host compare would
    // wrongly flag every link from another user on the same hub as our
    // own. Compare the full extension URL prefix instead: our own
    // share-files-extension lives under `serverSettings.baseUrl +
    // jupyterlab-share-files-extension/`; any link with that exact prefix
    // is ours, links with a different `/user/<name>/` prefix belong to
    // someone else and must go through to the backend.
    try {
      const ownPrefix = new URL(
        'jupyterlab-share-files-extension/',
        new URL(this._serverSettings.baseUrl, window.location.origin)
      ).href;
      if (link.startsWith(ownPrefix)) {
        await showDialog({
          title: 'Cannot connect to your own link',
          body:
            'This share or request is hosted on your own server, so you ' +
            'already own it. Look for it in My Shares or My Requests, ' +
            'or paste a link from someone else.',
          buttons: [Dialog.okButton({ label: 'OK' })]
        });
        return;
      }
    } catch {
      // malformed URL - let the backend handle the error message
    }
    try {
      await addConnection(this._serverSettings, link);
    } catch (err: any) {
      Notification.error(`Could not connect: ${err.message || err}`);
      return;
    }
    await this.refresh();
  }

  // ------------------------------------------------------------------ //
  // Lifecycle
  // ------------------------------------------------------------------ //

  protected onAfterAttach(): void {
    this._startPolling();
  }

  protected onBeforeDetach(): void {
    this._stopPolling();
  }

  /** (Re)start the single poll loop. One tick refreshes all shares/requests. */
  private _startPolling(): void {
    if (this._pollHandle) {
      return;
    }
    const seconds = Math.max(
      MIN_POLL_INTERVAL_SECONDS,
      this._settings.pollIntervalSeconds || DEFAULT_POLL_INTERVAL_SECONDS
    );
    this._pollHandle = window.setInterval(() => {
      void this.refresh();
    }, seconds * 1000);
  }

  private _stopPolling(): void {
    if (this._pollHandle) {
      window.clearInterval(this._pollHandle);
      this._pollHandle = null;
    }
  }

  private _restartPolling(): void {
    this._stopPolling();
    this._startPolling();
  }

  // ------------------------------------------------------------------ //
  // Internal: rendering
  // ------------------------------------------------------------------ //

  private _buildShell(): void {
    const root = this.node;
    root.innerHTML = '';

    // header
    const header = document.createElement('div');
    header.className = 'jp-ShareFilesPanel-header';
    const title = document.createElement('span');
    title.className = 'jp-ShareFilesPanel-headerTitle';
    title.textContent = 'Share Files';
    header.appendChild(title);

    const addBtn = this._makeIconButton(
      addIcon.svgstr,
      'New share or request',
      evt => {
        this._openNewMenu(evt);
      }
    );
    header.appendChild(addBtn);

    this._filterBtn = this._makeIconButton(
      filterIcon.svgstr,
      'Toggle filter',
      () => {
        this._toggleFilter();
      }
    );
    // cloud indicator - shown when a Cloudflare tunnel is configured.
    // Green when the tunnel is on (public links), dashed silhouette when
    // off (private links); clicking toggles between the two.
    this._cloudIndicator = document.createElement('span');
    this._cloudIndicator.className = 'jp-ShareFilesPanel-cloudIndicator';
    this._cloudIndicator.style.display = 'none';
    this._cloudIndicator.appendChild(this._svgNode(cloudIcon.svgstr));
    this._cloudIndicator.addEventListener('click', () => {
      void this._toggleTunnel();
    });
    header.appendChild(this._cloudIndicator);

    header.appendChild(this._filterBtn);

    this._refreshBtn = this._makeIconButton(
      refreshIcon.svgstr,
      'Refresh',
      () => {
        void this.refresh(true);
      }
    );
    header.appendChild(this._refreshBtn);

    root.appendChild(header);

    // collapsible filter input - hidden by default, toggled by the toolbar
    // filter button. matches the file browser's "Toggle File Filter" behaviour.
    this._filterBox = document.createElement('div');
    this._filterBox.className = 'jp-ShareFilesPanel-filterBox';
    this._filterBox.style.display = 'none';
    this._filterInput = document.createElement('input');
    this._filterInput.type = 'search';
    this._filterInput.className = 'jp-ShareFilesPanel-filterInput';
    this._filterInput.placeholder = 'Filter shares and requests';
    this._filterInput.addEventListener('input', () => {
      this._filterText = this._filterInput!.value.trim().toLowerCase();
      this._render();
    });
    this._filterBox.appendChild(this._filterInput);
    root.appendChild(this._filterBox);

    // body
    this._body = document.createElement('div');
    this._body.className = 'jp-ShareFilesPanel-body';
    root.appendChild(this._body);

    // bottom drop zone
    this._dropZone = document.createElement('div');
    this._dropZone.className = 'jp-ShareFilesPanel-dropZone';
    this._dropZone.textContent =
      'Drag files here to share, or paste a link below';
    root.appendChild(this._dropZone);

    // connect row
    const connectRow = document.createElement('div');
    connectRow.className = 'jp-ShareFilesPanel-connect';
    const connectInput = document.createElement('input');
    connectInput.type = 'text';
    connectInput.placeholder = 'Paste a share or request link';
    connectInput.className = 'jp-ShareFilesPanel-connectInput';
    const connectBtn = document.createElement('button');
    connectBtn.className = 'jp-ShareFilesPanel-connectButton';
    connectBtn.textContent = 'Connect';
    connectBtn.addEventListener('click', () => {
      const link = connectInput.value.trim();
      if (link) {
        connectBtn.disabled = true;
        void this.connectToLink(link).then(() => {
          connectInput.value = '';
          connectBtn.disabled = false;
        });
      }
    });
    connectInput.addEventListener('keydown', ev => {
      if (ev.key === 'Enter') {
        connectBtn.click();
      }
    });
    connectRow.appendChild(connectInput);
    connectRow.appendChild(connectBtn);
    root.appendChild(connectRow);

    this._attachDropTargetOnZone();
  }

  private _render(): void {
    if (!this._body) {
      return;
    }
    this._body.innerHTML = '';
    const visibleShares = this._applyNameFilter(this._state.shares);
    const visibleRequests = this._applyNameFilter(this._state.requests);
    const visibleConnections = this._applyNameFilter(this._state.connections);
    if (this._settings.enableShares) {
      this._renderSection('shares', 'My Shares', visibleShares.length, () =>
        this._renderShares(visibleShares)
      );
    }
    if (this._settings.enableRequests) {
      this._renderSection(
        'requests',
        'My Requests',
        visibleRequests.length,
        () => this._renderRequests(visibleRequests)
      );
    }
    this._renderSection(
      'connected',
      'Connected',
      visibleConnections.length,
      () => this._renderConnections(visibleConnections)
    );
    // The drop zone is for creating a new share - hide it if shares are off.
    if (this._dropZone) {
      this._dropZone.style.display = this._settings.enableShares ? '' : 'none';
    }
  }

  private _renderSection(
    key: 'shares' | 'requests' | 'connected',
    label: string,
    count: number,
    renderBody: () => HTMLElement
  ): void {
    const section = document.createElement('div');
    section.className = 'jp-ShareFilesPanel-section';
    const expanded = this._state.expanded[key];
    if (!expanded) {
      section.classList.add('jp-mod-collapsed');
    }

    const header = document.createElement('div');
    header.className = 'jp-ShareFilesPanel-sectionHeader';
    header.title = expanded ? 'Click to collapse' : 'Click to expand';
    const caret = document.createElement('span');
    caret.className = 'jp-ShareFilesPanel-sectionTwisty';
    caret.textContent = expanded ? '▾' : '▸'; // ▾ / ▸
    header.appendChild(caret);
    const title = document.createElement('span');
    title.className = 'jp-ShareFilesPanel-sectionTitle';
    title.textContent = label;
    header.appendChild(title);
    const counter = document.createElement('span');
    counter.className = 'jp-ShareFilesPanel-sectionCount';
    counter.textContent = `(${count})`;
    header.appendChild(counter);
    header.addEventListener('click', () => {
      this._state.expanded[key] = !this._state.expanded[key];
      this._render();
    });
    section.appendChild(header);

    if (expanded) {
      section.appendChild(renderBody());
    }
    this._body!.appendChild(section);
  }

  private _renderShares(shares: IShare[]): HTMLElement {
    const list = document.createElement('div');
    list.className = 'jp-ShareFilesPanel-list';
    if (shares.length === 0) {
      list.appendChild(
        this._renderEmpty(
          this._filterText
            ? 'No shares match the filter.'
            : 'No shares yet. Drag files in to start.'
        )
      );
      return list;
    }
    for (const share of shares) {
      list.appendChild(this._renderShareItem(share));
    }
    return list;
  }

  private _renderShareItem(share: IShare): HTMLElement {
    const item = document.createElement('div');
    item.className = 'jp-ShareFilesPanel-item';
    item.dataset.shareId = share.id;
    this._attachDropTargetOnItem(item, 'share', share.id);

    const expanded = this._state.expandedItems.has(`share:${share.id}`);
    const header = document.createElement('div');
    header.className = 'jp-ShareFilesPanel-itemHeader';

    const twisty = document.createElement('span');
    twisty.className = 'jp-ShareFilesPanel-itemTwisty';
    twisty.textContent = expanded ? '▾' : '▸';
    header.appendChild(twisty);

    const name = document.createElement('span');
    name.className = 'jp-ShareFilesPanel-itemName';
    name.textContent = share.name;
    header.appendChild(name);

    const meta = document.createElement('span');
    meta.className = 'jp-ShareFilesPanel-itemMeta';
    if (this._state.busyKeys.has(share.id)) {
      meta.appendChild(this._spinnerNode());
    } else {
      meta.textContent = `${share.entries.length} item${share.entries.length === 1 ? '' : 's'}`;
    }
    header.appendChild(meta);

    const copyBtn = this._makeRowIconButton(
      linkIcon.svgstr,
      'Copy link',
      evt => {
        evt.stopPropagation();
        void this._copyLinkWithFeedback(share.link, copyBtn);
      }
    );
    header.appendChild(copyBtn);

    const trashBtn = this._makeRowIconButton(
      trashIcon.svgstr,
      'Delete share',
      evt => {
        evt.stopPropagation();
        void this._deleteShare(share.id);
      }
    );
    trashBtn.classList.add('jp-mod-danger');
    header.appendChild(trashBtn);

    header.addEventListener('click', () => {
      const key = `share:${share.id}`;
      if (this._state.expandedItems.has(key)) {
        this._state.expandedItems.delete(key);
      } else {
        this._state.expandedItems.add(key);
      }
      this._render();
    });
    header.addEventListener('contextmenu', evt => {
      evt.preventDefault();
      this._openShareContextMenu(evt, share);
    });

    item.appendChild(header);

    if (expanded) {
      item.appendChild(this._renderShareEntries(share));
    }

    return item;
  }

  private _renderShareEntries(share: IShare): HTMLElement {
    const list = document.createElement('div');
    list.className = 'jp-ShareFilesPanel-entryList';
    const subPath = this._state.shareSubPath.get(share.id) || '';
    // At the share root we use share.entries (synced via the extension's own
    // /api/shares listing). Inside a sub-folder we query JupyterLab's
    // Contents API on demand and cache the result for the current render.
    const drillIn = (entry: IShareEntry) => {
      if (entry.path) {
        this._state.shareSubPath.set(share.id, entry.path);
        this._render();
      }
    };
    if (!subPath) {
      const visible = this._applyHiddenFilter(share.entries);
      if (visible.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'jp-ShareFilesPanel-empty';
        empty.textContent =
          share.entries.length === 0
            ? 'Empty - drag files onto this share to add'
            : 'No visible files (hidden ones filtered)';
        list.appendChild(empty);
        return list;
      }
      for (const entry of visible) {
        list.appendChild(
          this._renderEntryRow(
            entry,
            () => {
              void this._removeEntryFromShare(share.id, entry.name);
            },
            0,
            drillIn
          )
        );
      }
      return list;
    }
    // Inside a sub-folder: prepend a `..` row, then list cached entries.
    list.appendChild(this._renderUpRow(share.id, subPath));
    const cached = this._state.subEntries.get(subPath);
    if (cached === undefined) {
      // Trigger a fetch; re-render lands when the cache fills.
      void this._fetchSubEntries(subPath);
      const loading = document.createElement('div');
      loading.className = 'jp-ShareFilesPanel-empty';
      loading.textContent = 'Loading...';
      list.appendChild(loading);
      return list;
    }
    const visibleCached = this._applyHiddenFilter(cached);
    if (visibleCached.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'jp-ShareFilesPanel-empty';
      empty.textContent =
        cached.length === 0
          ? 'Folder is empty'
          : 'No visible files (hidden ones filtered)';
      list.appendChild(empty);
      return list;
    }
    for (const entry of visibleCached) {
      list.appendChild(this._renderEntryRow(entry, undefined, 0, drillIn));
    }
    return list;
  }

  private _toggleFilter(): void {
    this._filterVisible = !this._filterVisible;
    if (this._filterBox) {
      this._filterBox.style.display = this._filterVisible ? '' : 'none';
    }
    if (this._filterBtn) {
      this._filterBtn.classList.toggle('jp-mod-active', this._filterVisible);
    }
    if (this._filterVisible) {
      this._filterInput?.focus();
    } else if (this._filterText) {
      // Hiding clears the filter so rows are not silently hidden.
      this._filterText = '';
      if (this._filterInput) {
        this._filterInput.value = '';
      }
      this._render();
    }
  }

  private _applyNameFilter<T extends { name: string }>(items: T[]): T[] {
    if (!this._filterText) {
      return items;
    }
    return items.filter(i => i.name.toLowerCase().includes(this._filterText));
  }

  private _applyHiddenFilter(entries: IShareEntry[]): IShareEntry[] {
    if (this._settings.showHiddenFiles) {
      return entries;
    }
    return entries.filter(e => !e.name.startsWith('.'));
  }

  private _renderUpRow(shareId: string, currentSubPath: string): HTMLElement {
    const row = document.createElement('div');
    row.className = 'jp-ShareFilesPanel-entry jp-mod-clickable';
    row.title = 'Double-click to go up one level';
    const indent = document.createElement('span');
    indent.className = 'jp-ShareFilesPanel-entryIndent';
    row.appendChild(indent);
    const icon = this._svgNode(
      folderIcon.svgstr,
      'jp-ShareFilesPanel-entryIcon'
    );
    row.appendChild(icon);
    const name = document.createElement('span');
    name.className = 'jp-ShareFilesPanel-entryName';
    name.textContent = '..';
    row.appendChild(name);
    const handler = (ev: MouseEvent) => {
      ev.preventDefault();
      ev.stopPropagation();
      this._goUp(shareId, currentSubPath);
    };
    row.addEventListener('dblclick', handler);
    return row;
  }

  private _goUp(shareId: string, currentSubPath: string): void {
    const share = this._state.shares.find(s => s.id === shareId);
    if (!share) {
      return;
    }
    // Trim the last path segment. If that lands us at or above the share's
    // data dir (or we can no longer detect one because the backend has not
    // been restarted to ship share.path), drop back to root.
    const slash = currentSubPath.lastIndexOf('/');
    const parent = slash > 0 ? currentSubPath.slice(0, slash) : '';
    const dataRoot = share.path || '';
    if (!parent || (dataRoot && parent === dataRoot)) {
      this._state.shareSubPath.delete(shareId);
    } else if (dataRoot && !parent.startsWith(dataRoot)) {
      this._state.shareSubPath.delete(shareId);
    } else {
      this._state.shareSubPath.set(shareId, parent);
    }
    this._render();
  }

  private async _fetchSubEntries(subPath: string): Promise<void> {
    try {
      const model = await this._contents.get(subPath, {
        content: true,
        type: 'directory'
      });
      const children = Array.isArray(model.content) ? model.content : [];
      const entries: IShareEntry[] = children
        .map(
          (c: any): IShareEntry => ({
            name: c.name,
            type: c.type === 'directory' ? 'directory' : 'file',
            size: typeof c.size === 'number' ? c.size : 0,
            path: c.path
          })
        )
        .sort((a: IShareEntry, b: IShareEntry) => {
          // directories first, then alphabetical
          if (a.type !== b.type) {
            return a.type === 'directory' ? -1 : 1;
          }
          return a.name.localeCompare(b.name);
        });
      this._state.subEntries.set(subPath, entries);
    } catch (err: any) {
      this._state.subEntries.set(subPath, []);
      Notification.error(`Could not list folder: ${err.message || err}`);
    }
    this._render();
  }

  private _renderRequests(requests: IRequest[]): HTMLElement {
    const list = document.createElement('div');
    list.className = 'jp-ShareFilesPanel-list';
    if (requests.length === 0) {
      list.appendChild(
        this._renderEmpty(
          this._filterText
            ? 'No requests match the filter.'
            : 'No requests yet. Use [+] to create one.'
        )
      );
      return list;
    }
    for (const req of requests) {
      list.appendChild(this._renderRequestItem(req));
    }
    return list;
  }

  private _renderRequestItem(req: IRequest): HTMLElement {
    const item = document.createElement('div');
    item.className = 'jp-ShareFilesPanel-item';

    const expanded = this._state.expandedItems.has(`request:${req.id}`);
    const header = document.createElement('div');
    header.className = 'jp-ShareFilesPanel-itemHeader';

    const twisty = document.createElement('span');
    twisty.className = 'jp-ShareFilesPanel-itemTwisty';
    twisty.textContent = expanded ? '▾' : '▸';
    header.appendChild(twisty);

    const name = document.createElement('span');
    name.className = 'jp-ShareFilesPanel-itemName';
    name.textContent = req.name;
    header.appendChild(name);

    const meta = document.createElement('span');
    meta.className = 'jp-ShareFilesPanel-itemMeta';
    if (this._state.busyKeys.has(req.id)) {
      meta.appendChild(this._spinnerNode());
    } else {
      meta.textContent = `${req.upload_count} upload${req.upload_count === 1 ? '' : 's'}`;
    }
    header.appendChild(meta);

    const copyBtn = this._makeRowIconButton(
      linkIcon.svgstr,
      'Copy link',
      evt => {
        evt.stopPropagation();
        void this._copyLinkWithFeedback(req.link, copyBtn);
      }
    );
    header.appendChild(copyBtn);

    const trashBtn = this._makeRowIconButton(
      trashIcon.svgstr,
      'Delete request',
      evt => {
        evt.stopPropagation();
        void this._deleteRequest(req.id);
      }
    );
    trashBtn.classList.add('jp-mod-danger');
    header.appendChild(trashBtn);

    header.addEventListener('click', () => {
      const key = `request:${req.id}`;
      if (this._state.expandedItems.has(key)) {
        this._state.expandedItems.delete(key);
      } else {
        this._state.expandedItems.add(key);
      }
      this._render();
    });
    header.addEventListener('contextmenu', evt => {
      evt.preventDefault();
      this._openRequestContextMenu(evt, req);
    });

    item.appendChild(header);

    if (expanded) {
      item.appendChild(this._renderRequestUploads(req));
    }
    return item;
  }

  private _renderRequestUploads(req: IRequest): HTMLElement {
    const list = document.createElement('div');
    list.className = 'jp-ShareFilesPanel-entryList';
    if (!req.uploaders || req.uploaders.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'jp-ShareFilesPanel-empty';
      empty.textContent = 'No uploads yet. Share the link to invite people.';
      list.appendChild(empty);
      return list;
    }
    for (const uploader of req.uploaders) {
      const groupRow = document.createElement('div');
      groupRow.className = 'jp-ShareFilesPanel-uploaderHeader';
      const iconNode = this._svgNode(
        folderIcon.svgstr,
        'jp-ShareFilesPanel-entryIcon'
      );
      groupRow.appendChild(iconNode);
      const nameNode = document.createElement('span');
      nameNode.className = 'jp-ShareFilesPanel-uploaderName';
      nameNode.textContent = uploader.name;
      groupRow.appendChild(nameNode);
      list.appendChild(groupRow);

      for (const entry of uploader.entries) {
        const row = this._renderEntryRow(
          entry,
          () => {
            void this._removeUpload(req.id, uploader.name, entry.name);
          },
          1
        );
        list.appendChild(row);
      }
    }
    return list;
  }

  private _renderConnections(connections: IConnection[]): HTMLElement {
    const list = document.createElement('div');
    list.className = 'jp-ShareFilesPanel-list';
    if (connections.length === 0) {
      list.appendChild(
        this._renderEmpty(
          this._filterText
            ? 'No connections match the filter.'
            : 'No connections yet. Paste a link below.'
        )
      );
      return list;
    }
    for (const conn of connections) {
      list.appendChild(this._renderConnectionItem(conn));
    }
    return list;
  }

  private _renderConnectionItem(conn: IConnection): HTMLElement {
    const item = document.createElement('div');
    item.className = 'jp-ShareFilesPanel-item';
    if (conn.kind === 'request') {
      this._attachDropTargetOnItem(item, 'connection-request', conn.key);
    }

    const expanded = this._state.expandedItems.has(`conn:${conn.key}`);
    const header = document.createElement('div');
    header.className = 'jp-ShareFilesPanel-itemHeader';

    const twisty = document.createElement('span');
    twisty.className = 'jp-ShareFilesPanel-itemTwisty';
    twisty.textContent = expanded ? '▾' : '▸';
    header.appendChild(twisty);

    const data = this._state.connectionData.get(conn.key);
    const name = document.createElement('span');
    name.className = 'jp-ShareFilesPanel-itemName';
    name.textContent = (data && data.name) || conn.name || conn.id;
    header.appendChild(name);

    if (this._state.offlineKeys.has(conn.key)) {
      const offline = document.createElement('span');
      offline.className = 'jp-ShareFilesPanel-offline';
      offline.textContent = 'offline';
      header.appendChild(offline);
    }

    const meta = document.createElement('span');
    meta.className = 'jp-ShareFilesPanel-itemMeta';
    if (this._state.busyKeys.has(conn.key)) {
      meta.appendChild(this._spinnerNode());
    } else if (data && conn.kind === 'share') {
      const shareData = data as IRemoteShare;
      meta.textContent = `${shareData.entries.length} item${shareData.entries.length === 1 ? '' : 's'}`;
    } else if (conn.kind === 'request') {
      meta.textContent = 'request';
    }
    header.appendChild(meta);

    const disconnectBtn = this._makeRowIconButton(
      disconnectIcon.svgstr,
      'Disconnect',
      evt => {
        evt.stopPropagation();
        void this._disconnect(conn);
      }
    );
    header.appendChild(disconnectBtn);

    header.addEventListener('click', () => {
      const k = `conn:${conn.key}`;
      if (this._state.expandedItems.has(k)) {
        this._state.expandedItems.delete(k);
      } else {
        this._state.expandedItems.add(k);
      }
      this._render();
    });
    header.addEventListener('contextmenu', evt => {
      evt.preventDefault();
      this._openConnectionContextMenu(evt, conn);
    });

    item.appendChild(header);

    if (expanded) {
      if (conn.kind === 'share' && data) {
        const shareData = data as IRemoteShare;
        item.appendChild(this._renderConnectedShareEntries(conn, shareData));
      } else if (conn.kind === 'request') {
        const hint = document.createElement('div');
        hint.className = 'jp-ShareFilesPanel-empty';
        hint.textContent = 'Drag files here to upload to this request';
        item.appendChild(hint);
      } else if (!data) {
        const hint = document.createElement('div');
        hint.className = 'jp-ShareFilesPanel-empty';
        hint.textContent = 'Loading...';
        item.appendChild(hint);
      }
    }
    return item;
  }

  private _renderConnectedShareEntries(
    conn: IConnection,
    share: IRemoteShare
  ): HTMLElement {
    const list = document.createElement('div');
    list.className = 'jp-ShareFilesPanel-entryList';
    if (share.entries.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'jp-ShareFilesPanel-empty';
      empty.textContent = 'Share is empty.';
      list.appendChild(empty);
      return list;
    }
    const baseLink = (share.link || '').replace(/\/$/, '');
    for (const entry of share.entries) {
      const row = document.createElement('div');
      row.className = 'jp-ShareFilesPanel-entry jp-mod-clickable';
      const indent = document.createElement('span');
      indent.className = 'jp-ShareFilesPanel-entryIndent';
      row.appendChild(indent);
      const iconNode = this._svgNode(
        entry.type === 'directory' ? folderIcon.svgstr : fileIcon.svgstr,
        'jp-ShareFilesPanel-entryIcon'
      );
      row.appendChild(iconNode);
      const name = document.createElement('span');
      name.className = 'jp-ShareFilesPanel-entryName';
      name.textContent = entry.name;
      row.appendChild(name);
      const size = document.createElement('span');
      size.className = 'jp-ShareFilesPanel-entrySize';
      size.textContent = this._formatSize(entry.size);
      row.appendChild(size);
      const url = baseLink + '/download/' + encodeURIComponent(entry.name);
      row.title =
        entry.type === 'directory'
          ? 'Double-click to save folder here; drag into the file browser to save; right-click to download'
          : 'Double-click to open; drag into the file browser to save; right-click to download';
      row.classList.add('jp-mod-clickable');
      row.addEventListener('dblclick', evt => {
        evt.preventDefault();
        evt.stopPropagation();
        void this._openConnectedEntry(conn, entry);
      });
      row.addEventListener('contextmenu', evt => {
        evt.preventDefault();
        evt.stopPropagation();
        this._openConnectedEntryContextMenu(evt, conn, entry, url);
      });
      this._attachRemoteEntryDragSource(row, conn, entry);
      list.appendChild(row);
    }
    return list;
  }

  /**
   * Attach a drag source to a connected (remote) share entry. Unlike own-share
   * entries (which carry a local CONTENTS_MIME path), a connected entry's bytes
   * live on a peer's server, so we tag the drag with REMOTE_MIME and let the
   * file browser drop patch (see `installCopyDrop` in the plugin) resolve it to
   * a server-side `saveFromConnection`. Mirrors `_attachEntryDragSource`: a
   * plain click still downloads; a drag past the threshold starts the Drag.
   */
  private _attachRemoteEntryDragSource(
    row: HTMLElement,
    conn: IConnection,
    entry: IShareEntry
  ): void {
    const DRAG_THRESHOLD = 5;
    const onDown = (down: MouseEvent) => {
      if (down.button !== 0) {
        return;
      }
      down.preventDefault();
      const startX = down.clientX;
      const startY = down.clientY;
      let dragStarted = false;
      const onMove = (move: MouseEvent) => {
        if (dragStarted) {
          return;
        }
        const dx = Math.abs(move.clientX - startX);
        const dy = Math.abs(move.clientY - startY);
        if (dx < DRAG_THRESHOLD && dy < DRAG_THRESHOLD) {
          return;
        }
        dragStarted = true;
        document.removeEventListener('mousemove', onMove, true);
        document.removeEventListener('mouseup', onUp, true);
        this._startRemoteEntryDrag(
          conn,
          entry,
          row,
          move.clientX,
          move.clientY
        );
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove, true);
        document.removeEventListener('mouseup', onUp, true);
      };
      document.addEventListener('mousemove', onMove, true);
      document.addEventListener('mouseup', onUp, true);
    };
    row.addEventListener('mousedown', onDown);
    row.setAttribute('draggable', 'false');
    row.addEventListener('dragstart', e => e.preventDefault());
  }

  private _startRemoteEntryDrag(
    conn: IConnection,
    entry: IShareEntry,
    row: HTMLElement,
    x: number,
    y: number
  ): void {
    const mimeData = new MimeData();
    mimeData.setData(REMOTE_MIME, [
      { key: conn.key, name: entry.name, type: entry.type }
    ]);
    const dragImage = this._createEntryDragImage(row);
    const drag = new Drag({
      mimeData,
      dragImage,
      proposedAction: 'copy',
      supportedActions: 'copy',
      source: this
    });
    void drag.start(x, y);
  }

  /**
   * Save a single entry from a connected share into `destDir` (workspace
   * relative). Invoked by the file browser drop patch when a REMOTE_MIME drag
   * lands. The download + (folder) zip extraction happens server-side in
   * `ConnectionSaveHandler`, so no peer credentials touch the browser.
   */
  async saveRemoteEntryTo(
    connKey: string,
    name: string,
    destDir: string
  ): Promise<void> {
    this._state.busyKeys.add(connKey);
    this._render();
    try {
      await saveFromConnection(this._serverSettings, connKey, destDir, [name]);
      Notification.success(`Saved "${name}"`);
      await this._revealInFileBrowser(destDir);
    } catch (err: any) {
      Notification.error(`Could not save "${name}": ${err.message || err}`);
    } finally {
      this._state.busyKeys.delete(connKey);
      this._render();
    }
  }

  /**
   * Open a connected (remote) entry. The bytes live on a peer, so there is no
   * local path to open: save the entry into the file browser's current folder
   * (server-side, via saveFromConnection), then open the saved copy in a tab.
   * Folders cannot open in a tab - they are just saved (picked up).
   */
  private async _openConnectedEntry(
    conn: IConnection,
    entry: IShareEntry
  ): Promise<void> {
    const destDir = this._getCurrentDir();
    this._state.busyKeys.add(conn.key);
    this._render();
    try {
      const res = await saveFromConnection(
        this._serverSettings,
        conn.key,
        destDir,
        [entry.name]
      );
      const saved = res.saved && res.saved[0];
      if (entry.type === 'directory') {
        Notification.success(`Saved "${entry.name}"`);
        await this._revealInFileBrowser(destDir);
      } else if (saved) {
        await this._commands.execute('docmanager:open', { path: saved });
      }
    } catch (err: any) {
      Notification.error(
        `Could not open "${entry.name}": ${err.message || err}`
      );
    } finally {
      this._state.busyKeys.delete(conn.key);
      this._render();
    }
  }

  private _openConnectedEntryContextMenu(
    evt: MouseEvent,
    conn: IConnection,
    entry: IShareEntry,
    downloadUrl: string
  ): void {
    const menu = new Menu({ commands: this._commands });
    menu.addItem({
      command: 'share-files-panel:download-remote-entry',
      args: {
        url: downloadUrl,
        filename: entry.name + (entry.type === 'directory' ? '.zip' : '')
      }
    });
    menu.addItem({
      command: 'share-files-panel:save-remote-entry',
      args: { key: conn.key, name: entry.name }
    });
    menu.addItem({ type: 'separator' });
    menu.addItem({
      command: 'share-files-panel:copy-remote-entry',
      args: { key: conn.key, name: entry.name, type: entry.type }
    });
    menu.open(evt.clientX, evt.clientY);
  }

  /**
   * Download a file from a connected peer's public endpoint WITHOUT credentials.
   *
   * Uses `fetch(..., { credentials: 'omit' })` then a Blob download instead of
   * navigating the top window. On JupyterHub this is critical: a credentialed
   * navigation (e.g. `window.open`) to a peer's `/user/<owner>/...` link whose
   * server is offline makes the Hub offer to spawn/access the owner's server as
   * the current (admin) user. Omitting credentials means the request can never
   * trigger that flow - it just fails cleanly when the owner is offline.
   */
  private async _downloadRemote(url: string, filename: string): Promise<void> {
    try {
      const r = await fetch(url, { credentials: 'omit' });
      if (!r.ok) {
        throw new Error(`status ${r.status}`);
      }
      const blob = await r.blob();
      const href = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = href;
      a.download = filename;
      a.style.display = 'none';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(href);
    } catch (err: any) {
      Notification.error(
        `Could not download "${filename}" - the owner's server may be offline`
      );
    }
  }

  private _renderEntryRow(
    entry: IShareEntry,
    onRemove?: () => void,
    indentLevel = 0,
    onOpenFolder?: (entry: IShareEntry) => void
  ): HTMLElement {
    const row = document.createElement('div');
    row.className = 'jp-ShareFilesPanel-entry';
    for (let i = 0; i < indentLevel; i++) {
      const ind = document.createElement('span');
      ind.className = 'jp-ShareFilesPanel-entryIndent';
      row.appendChild(ind);
    }
    const indent = document.createElement('span');
    indent.className = 'jp-ShareFilesPanel-entryIndent';
    row.appendChild(indent);
    const iconNode = this._svgNode(
      entry.type === 'directory' ? folderIcon.svgstr : fileIcon.svgstr,
      'jp-ShareFilesPanel-entryIcon'
    );
    row.appendChild(iconNode);
    const name = document.createElement('span');
    name.className = 'jp-ShareFilesPanel-entryName';
    name.textContent = entry.name + (entry.type === 'directory' ? '/' : '');
    row.appendChild(name);
    const size = document.createElement('span');
    size.className = 'jp-ShareFilesPanel-entrySize';
    size.textContent = this._formatSize(entry.size);
    row.appendChild(size);
    if (onRemove) {
      const btn = document.createElement('button');
      btn.className = 'jp-ShareFilesPanel-entryRemove';
      btn.title = 'Remove';
      btn.appendChild(this._svgNode(closeIcon.svgstr));
      btn.addEventListener('click', ev => {
        ev.stopPropagation();
        onRemove();
      });
      row.appendChild(btn);
    }
    if (entry.path) {
      row.title =
        entry.type === 'directory'
          ? 'Double-click to open folder; drag into the file browser to copy; right-click for actions'
          : 'Double-click to open file; drag into the file browser to copy or onto a tab to open; right-click for actions';
      row.classList.add('jp-mod-clickable');
      row.addEventListener('contextmenu', evt => {
        evt.preventDefault();
        evt.stopPropagation();
        this._openEntryContextMenu(evt, entry);
      });
      row.addEventListener('dblclick', evt => {
        evt.preventDefault();
        evt.stopPropagation();
        if (entry.type === 'directory' && onOpenFolder) {
          onOpenFolder(entry);
        } else {
          void this._openEntry(entry);
        }
      });
      this._attachEntryDragSource(row, entry);
    }
    return row;
  }

  /**
   * Attach mousedown-based drag source so entries can be dragged out of the
   * panel. Files and folders both carry `application/x-jupyter-icontents`;
   * dropping into the file browser copies the source path (handled by our
   * capture-phase patch in the plugin, see `installFileBrowserCopyDrop`).
   * Files additionally carry the dock-panel factory MIME so dropping onto a
   * tab opens the file.
   */
  private _attachEntryDragSource(row: HTMLElement, entry: IShareEntry): void {
    const DRAG_THRESHOLD = 5;
    // Use mousedown (not pointerdown) because Lumino's Drag itself listens
    // for mouse* events internally - mixing pointer/mouse on the source can
    // confuse the gesture handover when drag.start() takes over.
    const onDown = (down: MouseEvent) => {
      if (down.button !== 0 || !entry.path) {
        return;
      }
      const target = down.target as HTMLElement | null;
      if (target && target.closest('.jp-ShareFilesPanel-entryRemove')) {
        return;
      }
      // Suppress the browser's native HTML5 drag (would race Lumino's)
      down.preventDefault();
      const startX = down.clientX;
      const startY = down.clientY;
      let dragStarted = false;
      const onMove = (move: MouseEvent) => {
        if (dragStarted) {
          return;
        }
        const dx = Math.abs(move.clientX - startX);
        const dy = Math.abs(move.clientY - startY);
        if (dx < DRAG_THRESHOLD && dy < DRAG_THRESHOLD) {
          return;
        }
        dragStarted = true;
        document.removeEventListener('mousemove', onMove, true);
        document.removeEventListener('mouseup', onUp, true);
        this._startEntryDrag(entry, row, move.clientX, move.clientY);
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove, true);
        document.removeEventListener('mouseup', onUp, true);
      };
      document.addEventListener('mousemove', onMove, true);
      document.addEventListener('mouseup', onUp, true);
    };
    row.addEventListener('mousedown', onDown);
    // Disable native HTML5 drag entirely - we only use Lumino's Drag system
    row.setAttribute('draggable', 'false');
    row.addEventListener('dragstart', e => e.preventDefault());
  }

  private _startEntryDrag(
    entry: IShareEntry,
    row: HTMLElement,
    x: number,
    y: number
  ): void {
    if (!entry.path) {
      return;
    }
    const mimeData = new MimeData();
    // JupyterLab's file browser drop handler iterates this as `string[]`
    // (`paths.map(p => manager.services.contents.localPath(p))`) - sending
    // objects breaks `localPath(obj)` silently. Match that contract.
    mimeData.setData(CONTENTS_MIME, [entry.path]);
    // For files, also advertise the dock-panel factory MIME so the entry can
    // be dropped onto the tab bar / dock area to open it (same thunk pattern
    // the file browser uses). Directories don't open in a doc widget, so they
    // carry only CONTENTS_MIME and remain copy-to-folder drags.
    if (entry.type !== 'directory') {
      const path = entry.path;
      mimeData.setData(FACTORY_MIME, () => {
        const existing = this._docManager.findWidget(path);
        return existing || this._docManager.open(path);
      });
    }
    const dragImage = this._createEntryDragImage(row);
    // proposedAction 'move' matches the file browser's own drag so the cursor
    // is identical (move glyph, no copy badge). The action is cosmetic for our
    // case: the file-browser drop is intercepted in capture phase and always
    // performs a Contents copy, so the shared source is never moved/deleted.
    const drag = new Drag({
      mimeData,
      dragImage,
      proposedAction: 'move',
      supportedActions: 'move',
      source: this
    });
    void drag.start(x, y);
  }

  /**
   * Build a drag image that visually matches the file browser's own drags:
   * clone the source row, replace the entry icon's class with JupyterLab's
   * shared `jp-DragIcon` so the same compact glyph + filename is shown next
   * to the pointer.
   */
  private _createEntryDragImage(row: HTMLElement): HTMLElement {
    const clone = row.cloneNode(true) as HTMLElement;
    clone.style.width = `${row.offsetWidth}px`;
    clone.style.background = 'var(--jp-layout-color1)';
    clone.style.border = '1px solid var(--jp-brand-color1)';
    clone.style.borderRadius = '2px';
    clone.style.opacity = '0.9';
    // Drop the inline trash button from the drag image
    const trash = clone.querySelector('.jp-ShareFilesPanel-entryRemove');
    if (trash) {
      trash.parentElement?.removeChild(trash);
    }
    // Swap the file/folder icon's wrapper class to jp-DragIcon so the same
    // styling as the file browser drag applies (uniform spacing).
    const icon = clone.querySelector('.jp-ShareFilesPanel-entryIcon');
    if (icon) {
      icon.classList.add('jp-DragIcon');
    }
    return clone;
  }

  private async _openEntry(entry: IShareEntry): Promise<void> {
    if (!entry.path) {
      return;
    }
    if (entry.type === 'directory') {
      // Reveal the folder's contents in the file browser
      await this._showEntryInFileBrowser(entry.path, 'directory');
      return;
    }
    // Open via JupyterLab's document manager - it picks the right viewer
    try {
      await this._commands.execute('docmanager:open', { path: entry.path });
    } catch (err: any) {
      Notification.error(`Could not open: ${err.message || err}`);
    }
  }

  private _openEntryContextMenu(evt: MouseEvent, entry: IShareEntry): void {
    if (!entry.path) {
      return;
    }
    const menu = new Menu({ commands: this._commands });
    menu.addItem({
      command: 'share-files-panel:copy-entry-to-cwd',
      args: { path: entry.path, name: entry.name }
    });
    menu.addItem({
      command: 'share-files-panel:show-entry-in-browser',
      args: { path: entry.path, type: entry.type }
    });
    menu.addItem({ type: 'separator' });
    menu.addItem({
      command: 'share-files-panel:copy-local-entry',
      args: { path: entry.path }
    });
    menu.open(evt.clientX, evt.clientY);
  }

  private _renderEmpty(text: string): HTMLElement {
    const empty = document.createElement('div');
    empty.className = 'jp-ShareFilesPanel-empty';
    empty.textContent = text;
    return empty;
  }

  // ------------------------------------------------------------------ //
  // Internal: commands and context menus
  // ------------------------------------------------------------------ //

  private _registerCommands(): void {
    const c = this._commands;
    if (!c.hasCommand('share-files-panel:copy-link')) {
      c.addCommand('share-files-panel:copy-link', {
        label: 'Copy Link',
        execute: args => {
          const link = String(args.link || '');
          if (!link) {
            return;
          }
          void this._copyLinkToClipboard(link).then(ok => {
            void this._showLinkDialog(link, ok);
          });
        }
      });
      c.addCommand('share-files-panel:delete-share', {
        label: 'Delete Share',
        execute: args => {
          const id = String(args.id || '');
          void this._deleteShare(id);
        }
      });
      c.addCommand('share-files-panel:delete-request', {
        label: 'Delete Request',
        execute: args => {
          const id = String(args.id || '');
          void this._deleteRequest(id);
        }
      });
      c.addCommand('share-files-panel:disconnect', {
        label: 'Disconnect',
        execute: args => {
          const key = String(args.key || '');
          void removeConnection(this._serverSettings, key).then(() =>
            this.refresh()
          );
        }
      });
      c.addCommand('share-files-panel:open-link', {
        label: 'Open in Browser',
        execute: args => {
          const link = String(args.link || '');
          if (link) {
            window.open(link, '_blank');
          }
        }
      });
      c.addCommand('share-files-panel:download-remote-entry', {
        label: 'Download',
        execute: args => {
          const url = String(args.url || '');
          const filename = String(args.filename || 'download');
          if (url) {
            void this._downloadRemote(url, filename);
          }
        }
      });
      c.addCommand('share-files-panel:save-remote-entry', {
        label: 'Save to Current Folder',
        execute: args => {
          const key = String(args.key || '');
          const name = String(args.name || '');
          if (key && name) {
            void this.saveRemoteEntryTo(key, name, this._getCurrentDir());
          }
        }
      });
      c.addCommand('share-files-panel:new-share', {
        label: 'New Share',
        execute: () => {
          void this.createShareFlow([]);
        }
      });
      c.addCommand('share-files-panel:new-request', {
        label: 'New Request',
        execute: () => {
          void this.createRequestFlow();
        }
      });
      c.addCommand('share-files-panel:copy-entry-to-cwd', {
        label: 'Copy to Current Folder',
        execute: args => {
          const path = String(args.path || '');
          const name = String(args.name || '');
          if (!path) {
            return;
          }
          void this._copyEntryToCurrentDir(path, name);
        }
      });
      c.addCommand('share-files-panel:show-entry-in-browser', {
        label: 'Show in File Browser',
        execute: args => {
          const path = String(args.path || '');
          if (!path) {
            return;
          }
          const type = String(args.type || '');
          void this._showEntryInFileBrowser(path, type);
        }
      });
      // Copy a connected (remote) share entry onto the extension clipboard.
      // Pasting in the file browser saves it server-side (see the
      // `filebrowser:paste` hook in the plugin). Remote entries are copy-only -
      // their bytes live on a peer, so there is no cut.
      c.addCommand('share-files-panel:copy-remote-entry', {
        label: 'Copy',
        execute: args => {
          const key = String(args.key || '');
          const name = String(args.name || '');
          const type = String(args.type || 'file');
          if (key && name) {
            setClip({
              kind: 'remote',
              origin: 'panel',
              items: [{ connKey: key, name, type }]
            });
          }
        }
      });
      // Copy a local panel entry (own-share file / request upload) onto the
      // clipboard so it can be pasted into the file browser's current folder.
      c.addCommand('share-files-panel:copy-local-entry', {
        label: 'Copy',
        execute: args => {
          const path = String(args.path || '');
          if (path) {
            setClip({
              kind: 'local',
              origin: 'panel',
              mode: 'copy',
              paths: [path]
            });
          }
        }
      });
      c.addCommand('share-files-panel:paste-into-share', {
        label: 'Paste',
        execute: args => {
          const id = String(args.id || '');
          if (id) {
            void this._pasteIntoShare(id);
          }
        }
      });
      c.addCommand('share-files-panel:paste-into-request', {
        label: 'Paste',
        execute: args => {
          const key = String(args.key || '');
          if (key) {
            void this._pasteIntoConnectedRequest(key);
          }
        }
      });
    }
  }

  /**
   * Paste the clipboard's local items into one of my shares. Mirrors the
   * drag-to-share flow (`addToShareFlow`): the server copies each source into
   * the share's own data dir, so a cut can safely delete the originals after
   * the add resolves.
   */
  private async _pasteIntoShare(shareId: string): Promise<void> {
    const clip = getClip();
    if (!clip || clip.kind !== 'local') {
      return;
    }
    const { paths, mode } = clip;
    this._state.busyKeys.add(shareId);
    this._render();
    try {
      await addShareItems(this._serverSettings, shareId, paths);
      if (mode === 'cut') {
        await Promise.all(paths.map(p => this._contents.delete(p)));
        clearClip();
      }
      Notification.success(`${paths.length} item(s) added`);
    } catch (err: any) {
      Notification.error(`Could not paste: ${err.message || err}`);
    } finally {
      this._state.busyKeys.delete(shareId);
      await this.refresh();
    }
  }

  /**
   * Paste the clipboard's local items into a connected request (upload). The
   * bytes are sent to the peer, so a cut can safely delete the local originals
   * after the upload resolves.
   */
  private async _pasteIntoConnectedRequest(connKey: string): Promise<void> {
    const clip = getClip();
    if (!clip || clip.kind !== 'local') {
      return;
    }
    const { paths, mode } = clip;
    this._state.busyKeys.add(connKey);
    this._render();
    try {
      await uploadToConnection(this._serverSettings, connKey, paths, '');
      if (mode === 'cut') {
        await Promise.all(paths.map(p => this._contents.delete(p)));
        clearClip();
      }
      Notification.success(`${paths.length} item(s) uploaded`);
    } catch (err: any) {
      Notification.error(`Could not paste: ${err.message || err}`);
    } finally {
      this._state.busyKeys.delete(connKey);
      await this.refresh();
    }
  }

  private async _copyEntryToCurrentDir(
    sourcePath: string,
    entryName: string
  ): Promise<void> {
    const target = this._getCurrentDir();
    try {
      await this._contents.copy(sourcePath, target);
      const dest = target ? `./${target}/` : './';
      Notification.success(`Copied ${entryName} → ${dest}`);
    } catch (err: any) {
      Notification.error(`Could not copy: ${err.message || err}`);
    }
  }

  private async _showEntryInFileBrowser(
    sourcePath: string,
    type: string
  ): Promise<void> {
    // sourcePath is workspace-relative, e.g. "uploads/shares/foo-AB/data/x.txt"
    // For files we cd into the parent and select the file. For directories we
    // cd into the directory itself so the user can browse its contents.
    try {
      if (type === 'directory') {
        await this._revealInFileBrowser(sourcePath);
      } else {
        const slash = sourcePath.lastIndexOf('/');
        const dir = slash >= 0 ? sourcePath.slice(0, slash) : '';
        const name = slash >= 0 ? sourcePath.slice(slash + 1) : sourcePath;
        await this._revealInFileBrowser(dir, name);
      }
    } catch (err: any) {
      Notification.error(`Could not navigate: ${err.message || err}`);
    }
  }

  private _openShareContextMenu(evt: MouseEvent, share: IShare): void {
    const menu = new Menu({ commands: this._commands });
    menu.addItem({
      command: 'share-files-panel:copy-link',
      args: { link: share.link }
    });
    menu.addItem({
      command: 'share-files-panel:open-link',
      args: { link: share.link }
    });
    if (share.path) {
      menu.addItem({
        command: 'share-files-panel:show-entry-in-browser',
        args: { path: share.path, type: 'directory' }
      });
    }
    const clip = getClip();
    if (clip && clip.kind === 'local') {
      menu.addItem({ type: 'separator' });
      menu.addItem({
        command: 'share-files-panel:paste-into-share',
        args: { id: share.id }
      });
    }
    menu.addItem({ type: 'separator' });
    menu.addItem({
      command: 'share-files-panel:delete-share',
      args: { id: share.id }
    });
    menu.open(evt.clientX, evt.clientY);
  }

  private _openRequestContextMenu(evt: MouseEvent, req: IRequest): void {
    const menu = new Menu({ commands: this._commands });
    menu.addItem({
      command: 'share-files-panel:copy-link',
      args: { link: req.link }
    });
    menu.addItem({
      command: 'share-files-panel:open-link',
      args: { link: req.link }
    });
    if (req.path) {
      menu.addItem({
        command: 'share-files-panel:show-entry-in-browser',
        args: { path: req.path, type: 'directory' }
      });
    }
    menu.addItem({ type: 'separator' });
    menu.addItem({
      command: 'share-files-panel:delete-request',
      args: { id: req.id }
    });
    menu.open(evt.clientX, evt.clientY);
  }

  private _openConnectionContextMenu(evt: MouseEvent, conn: IConnection): void {
    const menu = new Menu({ commands: this._commands });
    const data = this._state.connectionData.get(conn.key);
    const link = (data && data.link) || '';
    menu.addItem({
      command: 'share-files-panel:open-link',
      args: { link }
    });
    const clip = getClip();
    if (conn.kind === 'request' && clip && clip.kind === 'local') {
      menu.addItem({ type: 'separator' });
      menu.addItem({
        command: 'share-files-panel:paste-into-request',
        args: { key: conn.key }
      });
    }
    menu.addItem({ type: 'separator' });
    menu.addItem({
      command: 'share-files-panel:disconnect',
      args: { key: conn.key }
    });
    menu.open(evt.clientX, evt.clientY);
  }

  private _openNewMenu(evt: MouseEvent): void {
    const menu = new Menu({ commands: this._commands });
    if (this._settings.enableShares) {
      menu.addItem({ command: 'share-files-panel:new-share' });
    }
    if (this._settings.enableRequests) {
      menu.addItem({ command: 'share-files-panel:new-request' });
    }
    // if both are disabled, fall back to disconnect-only - but in that case
    // the [+] button itself is pretty pointless. Show a "nothing to add" item.
    if (!this._settings.enableShares && !this._settings.enableRequests) {
      const noop = document.createElement('div');
      noop.style.padding = '8px 12px';
      noop.style.color = 'var(--jp-ui-font-color3)';
      noop.style.fontStyle = 'italic';
      noop.style.fontSize = 'var(--jp-ui-font-size0)';
      noop.textContent = 'Enable shares or requests in Settings';
      menu.node.appendChild(noop);
    }
    menu.open(evt.clientX, evt.clientY);
  }

  // ------------------------------------------------------------------ //
  // Internal: actions
  // ------------------------------------------------------------------ //

  private async _removeEntryFromShare(
    shareId: string,
    name: string
  ): Promise<void> {
    this._state.busyKeys.add(shareId);
    this._render();
    try {
      await removeShareItems(this._serverSettings, shareId, [name]);
    } catch (err: any) {
      Notification.error(`Could not remove: ${err.message || err}`);
    } finally {
      this._state.busyKeys.delete(shareId);
      await this.refresh();
    }
  }

  private async _removeUpload(
    requestId: string,
    uploader: string,
    name: string
  ): Promise<void> {
    this._state.busyKeys.add(requestId);
    this._render();
    try {
      await removeRequestUpload(
        this._serverSettings,
        requestId,
        uploader,
        name
      );
    } catch (err: any) {
      Notification.error(`Could not remove: ${err.message || err}`);
    } finally {
      this._state.busyKeys.delete(requestId);
      await this.refresh();
    }
  }

  private async _deleteShare(id: string): Promise<void> {
    const result = await showDialog({
      title: 'Delete share?',
      body: 'This will remove the share permanently. The link will stop working.',
      buttons: [Dialog.cancelButton(), Dialog.warnButton({ label: 'Delete' })]
    });
    if (!result.button.accept) {
      return;
    }
    try {
      await deleteShare(this._serverSettings, id);
    } catch (err: any) {
      Notification.error(`Could not delete: ${err.message || err}`);
    }
    await this.refresh();
  }

  private async _deleteRequest(id: string): Promise<void> {
    const result = await showDialog({
      title: 'Delete request?',
      body: 'This will remove the request and any uploads permanently.',
      buttons: [Dialog.cancelButton(), Dialog.warnButton({ label: 'Delete' })]
    });
    if (!result.button.accept) {
      return;
    }
    try {
      await deleteRequest(this._serverSettings, id);
    } catch (err: any) {
      Notification.error(`Could not delete: ${err.message || err}`);
    }
    await this.refresh();
  }

  private async _disconnect(conn: IConnection): Promise<void> {
    try {
      await removeConnection(this._serverSettings, conn.key);
    } catch (err: any) {
      Notification.error(`Could not disconnect: ${err.message || err}`);
    }
    await this.refresh();
  }

  // ------------------------------------------------------------------ //
  // Internal: connections - remote manifest refresh
  // ------------------------------------------------------------------ //

  private async _refreshConnection(conn: IConnection): Promise<void> {
    if (!conn.link && !this._linkFor(conn)) {
      return;
    }
    const link = conn.link || this._linkFor(conn);
    try {
      let data: IRemoteShare | IRemoteRequest;
      if (conn.kind === 'share') {
        data = await fetchRemoteShare(link);
      } else {
        data = await fetchRemoteRequest(link);
      }
      this._state.connectionData.set(conn.key, data);
      this._state.offlineKeys.delete(conn.key);
    } catch {
      this._state.offlineKeys.add(conn.key);
    }
  }

  private _linkFor(conn: IConnection): string {
    // The connection's full link is persisted server-side (see
    // ConnectionStore.add). We must NOT reconstruct it from host + id here:
    // on JupyterHub the owner lives under `/user/<name>/`, which the client
    // cannot know, so a reconstructed URL drops that prefix and JupyterHub
    // bounces it to `/hub/...` (404), wrongly marking an online share offline.
    // Return the stored link only; legacy entries without one are repaired by
    // reconnecting (the store backfills the link on re-add).
    return conn.link || '';
  }

  // ------------------------------------------------------------------ //
  // Internal: drag-and-drop targets
  // ------------------------------------------------------------------ //

  private _attachDropTargetOnZone(): void {
    if (!this._dropZone) {
      return;
    }
    this._attachDropTarget(
      this._dropZone,
      paths => {
        void this.createShareFlow(paths);
      },
      'drop-zone'
    );
  }

  private _attachDropTargetOnItem(
    el: HTMLElement,
    kind: 'share' | 'connection-request',
    id: string
  ): void {
    this._attachDropTarget(
      el,
      paths => {
        if (kind === 'share') {
          void this.addToShareFlow(id, paths);
        } else {
          void this.uploadToConnectionFlow(id, paths);
        }
      },
      `${kind}:${id}`
    );
  }

  private _attachDropTarget(
    node: HTMLElement,
    onDrop: (paths: string[]) => void,
    label = 'unknown'
  ): void {
    const getPaths = (event: any): string[] | null => {
      try {
        const data = event.mimeData && event.mimeData.getData(CONTENTS_MIME);
        if (!data) {
          return null;
        }
        if (Array.isArray(data)) {
          // Contents.IModel objects, or plain strings
          return data
            .map((d: any) => (typeof d === 'string' ? d : d?.path))
            .filter((p: any) => typeof p === 'string');
        }
        if (typeof data === 'string') {
          return [data];
        }
        // jupyterlab file browser ships a Contents.IModel directly
        if (data && typeof data.path === 'string') {
          return [data.path];
        }
        return null;
      } catch (err) {
        console.warn('[share-files] getPaths error', err);
        return null;
      }
    };

    node.addEventListener('lm-dragenter', (event: any) => {
      const paths = getPaths(event);
      if (!paths) {
        return;
      }
      event.preventDefault();
      // do NOT stopPropagation here - parent drop zones may need to know too
      node.classList.add('jp-mod-dropTarget');
    });
    node.addEventListener('lm-dragleave', (event: any) => {
      // Only remove highlight if we're actually leaving the node entirely,
      // not just crossing into a child element. Lumino fires lm-dragleave
      // on the parent when moving into a child, which made the highlight
      // flicker and the user think nothing was happening.
      const related = (event as any).relatedTarget as Node | null;
      if (related && node.contains(related)) {
        return;
      }
      node.classList.remove('jp-mod-dropTarget');
    });
    node.addEventListener('lm-dragover', (event: any) => {
      const paths = getPaths(event);
      if (!paths) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      event.dropAction = event.proposedAction;
      // keep the highlight on - we're a valid drop target
      node.classList.add('jp-mod-dropTarget');
    });
    node.addEventListener('lm-drop', (event: any) => {
      const paths = getPaths(event);
      console.log(`[share-files] lm-drop on ${label}, paths:`, paths);
      if (!paths || paths.length === 0) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      node.classList.remove('jp-mod-dropTarget');
      onDrop(paths);
    });
  }

  // ------------------------------------------------------------------ //
  // Internal: utilities
  // ------------------------------------------------------------------ //

  private _suggestName(paths: string[]): string {
    if (paths.length === 0) {
      return 'shared-files';
    }
    if (paths.length === 1) {
      const base = paths[0].split('/').pop() || 'shared';
      return base.replace(/\.[^.]+$/, '');
    }
    return 'shared-files';
  }

  private async _promptForName(
    title: string,
    suggested: string
  ): Promise<string | null> {
    const input = document.createElement('input');
    input.type = 'text';
    input.value = suggested;
    input.placeholder = 'Name';
    input.style.cssText = 'width: 100%; padding: 6px 8px; font-size: 14px;';
    const widget = new Widget({ node: input });
    const result = await showDialog({
      title,
      body: widget,
      buttons: [Dialog.cancelButton(), Dialog.okButton({ label: 'Create' })]
    });
    if (!result.button.accept) {
      return null;
    }
    return input.value.trim() || null;
  }

  private async _copyLinkToClipboard(link: string): Promise<boolean> {
    try {
      await navigator.clipboard.writeText(link);
      return true;
    } catch {
      // try legacy fallback
      try {
        const ta = document.createElement('textarea');
        ta.value = link;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        return ok;
      } catch {
        return false;
      }
    }
  }

  /**
   * Copy a link and show a popup with the link + confirmation. The button
   * also flashes briefly to acknowledge the click. No toast - the popup IS
   * the feedback.
   */
  private async _copyLinkWithFeedback(
    link: string,
    btn: HTMLElement
  ): Promise<void> {
    const ok = await this._copyLinkToClipboard(link);
    if (btn) {
      btn.classList.add('jp-mod-copied');
      window.setTimeout(() => btn.classList.remove('jp-mod-copied'), 700);
    }
    await this._showLinkDialog(link, ok);
  }

  /**
   * Show a dialog with the link visible and a Copy button. Used by the
   * right-click "Copy Link" command so the user sees what they're copying.
   */
  private async _showLinkDialog(
    link: string,
    alreadyCopied: boolean
  ): Promise<void> {
    const wrap = document.createElement('div');
    // Wider than the default ~480px dialog so the full link fits on one line
    wrap.style.cssText =
      'display: flex;' +
      ' flex-direction: column;' +
      ' gap: 8px;' +
      ' min-width: min(720px, 90vw);';

    // Order: the link itself first, then the copy confirmation, then the
    // reachability outcome.
    const input = document.createElement('input');
    input.type = 'text';
    input.value = link;
    input.readOnly = true;
    input.style.cssText =
      'width: 100%;' +
      ' padding: 8px 10px;' +
      ' font-family: var(--jp-code-font-family, monospace);' +
      ' font-size: var(--jp-ui-font-size1);' +
      ' color: var(--jp-ui-font-color1);' +
      ' background: var(--jp-layout-color1);' +
      ' border: 1px solid var(--jp-border-color2);' +
      ' border-radius: 2px;' +
      ' box-sizing: border-box;';
    input.addEventListener('focus', () => input.select());
    wrap.appendChild(input);

    const statusLine = (borderVar: string): HTMLElement => {
      const el = document.createElement('div');
      el.style.cssText =
        'display: flex;' +
        ' align-items: center;' +
        ' gap: 6px;' +
        ' padding: 6px 12px;' +
        ' background: var(--jp-layout-color2);' +
        ' color: var(--jp-ui-font-color2);' +
        ` border-left: 2px solid var(${borderVar});` +
        ' border-radius: 2px;' +
        ' font-size: var(--jp-ui-font-size1);' +
        ' font-family: var(--jp-ui-font-family);' +
        ' font-style: italic;';
      return el;
    };

    if (alreadyCopied) {
      const status = statusLine('--jp-success-color1');
      status.textContent = '✓  Link copied to clipboard';
      wrap.appendChild(status);
    }

    // Reachability: the server probes its own public link (a frontend fetch
    // would be blocked by CORS). Kind and id come from the link itself, so
    // every caller gets the check without extra plumbing. A spinner runs
    // while the probe is in flight.
    const m = link.match(/\/public\/(share|request)\/([A-Z2-7]{6,16})$/);
    if (m) {
      const reach = statusLine('--jp-border-color2');
      reach.appendChild(this._spinnerNode());
      reach.appendChild(document.createTextNode('Checking link reachability…'));
      wrap.appendChild(reach);
      void checkLink(
        this._serverSettings,
        m[1] as 'share' | 'request',
        m[2]
      ).then(
        res => {
          if (res.reachable) {
            reach.style.borderLeftColor = 'var(--jp-success-color1)';
            reach.textContent = '✓  Link is reachable';
          } else {
            reach.style.borderLeftColor = 'var(--jp-error-color1)';
            reach.textContent =
              '✗  Link is not reachable' +
              (res.error
                ? ` - ${res.error}`
                : res.status
                  ? ` (HTTP ${res.status})`
                  : '');
          }
        },
        () => {
          reach.style.borderLeftColor = 'var(--jp-error-color1)';
          reach.textContent = '✗  Link reachability check failed';
        }
      );
    }

    const qr = qrcode(0, 'M');
    qr.addData(link);
    qr.make();
    const qrImg = document.createElement('img');
    qrImg.src = qr.createDataURL(4, 8);
    qrImg.alt = 'QR code for the link';
    qrImg.style.cssText =
      'align-self: center;' +
      ' width: 180px;' +
      ' height: 180px;' +
      ' image-rendering: pixelated;' +
      ' background: #fff;' +
      ' padding: 8px;' +
      ' border-radius: 2px;';
    wrap.appendChild(qrImg);

    const widget = new Widget({ node: wrap });
    // auto-select the URL when the dialog opens so user can Ctrl-C if needed
    window.setTimeout(() => {
      input.focus();
      input.select();
    }, 50);

    const dialog = new Dialog({
      title: 'Share link',
      body: widget,
      buttons: [Dialog.okButton({ label: 'Close' })]
    });

    // Bottom escape hatch: reset Cloudflare sharing (same as `cloudflare
    // reset`) - closes this popup, clears credentials and base URLs; links
    // revert to the private address and the cloud icon goes back to its
    // "click to set up" state.
    if (this._state.info?.tunnel_configured) {
      const reset = document.createElement('a');
      reset.textContent = 'Reset Cloudflare sharing settings';
      reset.href = '#';
      reset.style.cssText =
        'align-self: center;' +
        ' margin-top: 4px;' +
        ' font-size: var(--jp-ui-font-size0);' +
        ' color: var(--jp-ui-font-color2);' +
        ' text-decoration: underline;' +
        ' cursor: pointer;';
      reset.addEventListener('mouseenter', () => {
        reset.style.color = 'var(--jp-error-color1)';
      });
      reset.addEventListener('mouseleave', () => {
        reset.style.color = 'var(--jp-ui-font-color2)';
      });
      reset.addEventListener('click', evt => {
        evt.preventDefault();
        dialog.resolve(0);
        void resetTunnel(this._serverSettings)
          .then(state => {
            if (this._state.info) {
              this._state.info = { ...this._state.info, ...state };
            }
            Notification.info(
              'Cloudflare sharing reset - links use the private address. ' +
                'Click the cloud icon to configure again.',
              { autoClose: 6000 }
            );
          })
          .catch((err: any) => {
            Notification.error(
              `Cloudflare reset failed: ${err?.message || err}`,
              { autoClose: 8000 }
            );
          })
          .then(() => {
            this._updateCloudIndicator();
            return this.refresh();
          });
      });
      wrap.appendChild(reset);
    }

    await dialog.launch();
  }

  private _detectNewUploads(): void {
    for (const req of this._state.requests) {
      const prev = this._state.lastSeenUploads.get(req.id);
      const last = req.last_upload_at || 0;
      const seen = req.last_seen_upload_at || 0;
      // First poll: just record current state
      if (prev === undefined) {
        this._state.lastSeenUploads.set(req.id, last);
        continue;
      }
      if (last > prev && last > seen) {
        Notification.info(`New upload to "${req.name}"`);
        this._state.lastSeenUploads.set(req.id, last);
      }
    }
  }

  private _formatSize(bytes: number): string {
    if (bytes < 1024) {
      return `${bytes} B`;
    }
    if (bytes < 1024 * 1024) {
      return `${(bytes / 1024).toFixed(1)} KB`;
    }
    if (bytes < 1024 * 1024 * 1024) {
      return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    }
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  }

  private _makeIconButton(
    svg: string,
    title: string,
    onClick: (evt: MouseEvent) => void
  ): HTMLElement {
    const btn = document.createElement('button');
    btn.className = 'jp-ShareFilesPanel-iconButton';
    btn.title = title;
    btn.appendChild(this._svgNode(svg));
    btn.addEventListener('click', onClick);
    return btn;
  }

  /** Small inline icon button shown inside item header rows (on hover). */
  private _makeRowIconButton(
    svg: string,
    title: string,
    onClick: (evt: MouseEvent) => void
  ): HTMLElement {
    const btn = document.createElement('button');
    btn.className = 'jp-ShareFilesPanel-rowIconButton';
    btn.title = title;
    btn.appendChild(this._svgNode(svg));
    btn.addEventListener('click', onClick);
    return btn;
  }

  private _svgNode(svgString: string, extraClass?: string): HTMLElement {
    const wrap = document.createElement('span');
    if (extraClass) {
      wrap.className = extraClass;
    }
    wrap.innerHTML = svgString;
    return wrap;
  }

  /** Render the header cloud icon from the server-reported tunnel state:
   * hidden when no tunnel is configured; green filled cloud when the tunnel
   * is on (public links); dashed silhouette when off (private links);
   * blinking blue while connecting. */
  private _updateCloudIndicator(): void {
    if (!this._cloudIndicator) {
      return;
    }
    const info = this._state.info;
    // Always visible once the server has answered: the silhouette doubles
    // as the entry point to configure Cloudflare sharing.
    this._cloudIndicator.style.display = info ? 'flex' : 'none';
    if (!info) {
      return;
    }
    if (this._tunnelToggling) {
      // _toggleTunnel owns the icon while the switch is in flight
      return;
    }
    const configured = !!info.tunnel_configured;
    if (!configured) {
      this._cloudIndicator.classList.remove('jp-mod-active');
      this._cloudIndicator.classList.remove('jp-mod-connecting');
      this._cloudIndicator.innerHTML = '';
      this._cloudIndicator.appendChild(this._svgNode(cloudOffIcon.svgstr));
      this._cloudIndicator.title =
        'Cloudflare sharing not configured - click to set up public links.';
      return;
    }
    const active = !!info?.tunnel_active;
    this._cloudIndicator.classList.toggle('jp-mod-active', active);
    this._cloudIndicator.classList.remove('jp-mod-connecting');
    this._cloudIndicator.innerHTML = '';
    this._cloudIndicator.appendChild(
      this._svgNode(active ? cloudIcon.svgstr : cloudOffIcon.svgstr)
    );
    this._cloudIndicator.title = active
      ? `Cloudflare sharing on - links use ${info?.public_base_url}. ` +
        'Click to switch to private links.'
      : 'Cloudflare sharing off - links use the private address. ' +
        'Click to share publicly.';
  }

  /** Click on the cloud icon: switch between public links (tunnel up) and
   * private links (tunnel down). Blinks blue while connecting. */
  private async _toggleTunnel(): Promise<void> {
    if (this._tunnelToggling) {
      return;
    }
    if (this._state.info && !this._state.info.tunnel_configured) {
      // No tunnel yet - the icon is the entry point to configure one.
      return this._showTunnelSetupDialog();
    }
    const active = !!this._state.info?.tunnel_active;
    this._tunnelToggling = true;
    if (!active) {
      this._cloudIndicator!.classList.remove('jp-mod-active');
      this._cloudIndicator!.classList.add('jp-mod-connecting');
      this._cloudIndicator!.innerHTML = '';
      this._cloudIndicator!.appendChild(this._svgNode(cloudIcon.svgstr));
      this._cloudIndicator!.title = 'Connecting…';
    }
    try {
      const state = await setTunnel(this._serverSettings, { active: !active });
      if (this._state.info) {
        this._state.info = { ...this._state.info, ...state };
      }
    } catch (err: any) {
      Notification.error(
        `Could not switch the Cloudflare tunnel: ${err?.message || err}`,
        { autoClose: 5000 }
      );
    } finally {
      this._tunnelToggling = false;
    }
    this._updateCloudIndicator();
    // Links in the panel change host with the toggle - refresh them.
    await this.refresh();
  }

  /**
   * Configuration popup for Cloudflare sharing - same inputs as the CLI's
   * `cloudflare setup`. Opened by clicking the cloud icon while no tunnel
   * is configured.
   */
  private async _showTunnelSetupDialog(): Promise<void> {
    const wrap = document.createElement('div');
    wrap.style.cssText =
      'display: flex;' +
      ' flex-direction: column;' +
      ' gap: 10px;' +
      ' min-width: min(560px, 90vw);';

    const field = (
      label: string,
      hint: string,
      value: string,
      type = 'text'
    ): HTMLInputElement => {
      const block = document.createElement('div');
      block.style.cssText = 'display: flex; flex-direction: column; gap: 2px;';
      const lab = document.createElement('label');
      lab.textContent = label;
      lab.style.cssText =
        'font-size: var(--jp-ui-font-size1);' +
        ' color: var(--jp-ui-font-color1);' +
        ' font-weight: 600;';
      const inp = document.createElement('input');
      inp.type = type;
      inp.value = value;
      inp.style.cssText =
        'width: 100%;' +
        ' padding: 6px 8px;' +
        ' font-family: var(--jp-code-font-family, monospace);' +
        ' font-size: var(--jp-ui-font-size1);' +
        ' color: var(--jp-ui-font-color1);' +
        ' background: var(--neutral-fill-input-rest,' +
        ' var(--jp-input-background, var(--jp-layout-color1)));' +
        ' border: 1px solid var(--jp-border-color2);' +
        ' border-radius: 2px;' +
        ' box-sizing: border-box;';
      const hintEl = document.createElement('div');
      hintEl.textContent = hint;
      hintEl.style.cssText =
        'font-size: var(--jp-ui-font-size0);' +
        ' color: var(--jp-ui-font-color2);' +
        ' font-style: italic;';
      block.appendChild(lab);
      block.appendChild(inp);
      block.appendChild(hintEl);
      wrap.appendChild(block);
      return inp;
    };

    const intro = document.createElement('div');
    intro.textContent =
      'Share links publicly through a Cloudflare tunnel. ' +
      'Same configuration as "jupyterlab_share_files cloudflare setup".';
    intro.style.cssText =
      'font-size: var(--jp-ui-font-size1);' +
      ' color: var(--jp-ui-font-color2);';
    wrap.appendChild(intro);

    const tokenInp = field(
      'Cloudflare API token',
      'Cloudflare dashboard → My Profile → API Tokens (or Account API ' +
        'Tokens). Needs Account → Cloudflare Tunnel → Edit and zone-scoped ' +
        'DNS → Edit for your domain.',
      '',
      'password'
    );
    const accountInp = field(
      'Account id',
      'Cloudflare dashboard → your domain → Overview, "Account ID" in the ' +
        'right-hand column (32 hex characters).',
      ''
    );
    const hostnameInp = field(
      'Public hostname',
      'The public address links will carry - a subdomain of a domain ' +
        'managed in your Cloudflare account, e.g. share.example.com. ' +
        'Created/updated for you as a DNS record.',
      ''
    );
    const privateInp = field(
      'Private base URL',
      'This Jupyter server as the cloudflared connector reaches it - ' +
        'usually the address in your browser bar (prefilled). Must be ' +
        'https.',
      this._serverSettings.baseUrl
    );

    const widget = new Widget({ node: wrap });
    const result = await showDialog({
      title: 'Set up Cloudflare sharing',
      body: widget,
      buttons: [Dialog.cancelButton(), Dialog.okButton({ label: 'Set up' })]
    });
    if (!result.button.accept) {
      return;
    }
    const notifyId = Notification.emit(
      'Setting up Cloudflare sharing…',
      'in-progress',
      { autoClose: false }
    );
    try {
      const state = await setupTunnel(this._serverSettings, {
        token: tokenInp.value.trim(),
        account_id: accountInp.value.trim(),
        hostname: hostnameInp.value.trim(),
        private_base_url: privateInp.value.trim()
      });
      if (this._state.info) {
        this._state.info = { ...this._state.info, ...state };
      }
      Notification.update({
        id: notifyId,
        message: 'Cloudflare sharing is set up - links are public now.',
        type: 'success',
        autoClose: 5000
      });
    } catch (err: any) {
      Notification.update({
        id: notifyId,
        message: `Cloudflare setup failed: ${err?.message || err}`,
        type: 'error',
        autoClose: 8000
      });
    }
    this._updateCloudIndicator();
    await this.refresh();
  }

  private _spinnerNode(): HTMLElement {
    const s = document.createElement('span');
    s.className = 'jp-ShareFilesPanel-spinner';
    return s;
  }

  // ------------------------------------------------------------------ //
  // State
  // ------------------------------------------------------------------ //

  private _serverSettings: ServerConnection.ISettings;
  private _commands: CommandRegistry;
  private _settings: IShareFilesSettings;
  private _contents: Contents.IManager;
  private _docManager: IDocumentManager;
  private _getCurrentDir: () => string;
  private _revealInFileBrowser: (
    dirPath: string,
    name?: string
  ) => Promise<void>;
  private _state: IPanelState;
  private _body: HTMLElement | null = null;
  private _dropZone: HTMLElement | null = null;
  private _refreshBtn: HTMLElement | null = null;
  private _filterBtn: HTMLElement | null = null;
  private _cloudIndicator: HTMLElement | null = null;
  private _tunnelToggling = false;
  private _filterBox: HTMLElement | null = null;
  private _filterInput: HTMLInputElement | null = null;
  private _filterText = '';
  private _filterVisible = false;
  private _pollHandle: number | null = null;
}
