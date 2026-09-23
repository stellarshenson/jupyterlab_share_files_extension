/**
 * ShareFilesPanel - Lumino side panel widget for the share-files extension.
 *
 * Vanilla DOM rendering, refresh on a timer (standalone) or on the hub's
 * change stream (hub mode), drag-drop targets, and
 * row-level context menus via Lumino's Menu and CommandRegistry.
 */

import { Dialog, Notification, showDialog } from '@jupyterlab/apputils';
import { IDocumentManager } from '@jupyterlab/docmanager';
import { Contents, ServerConnection } from '@jupyterlab/services';
import { checkIcon, copyIcon, filterIcon } from '@jupyterlab/ui-components';
import { CommandRegistry } from '@lumino/commands';
import { MimeData } from '@lumino/coreutils';
import { Drag } from '@lumino/dragdrop';
import { Menu, Widget } from '@lumino/widgets';
import qrcode from 'qrcode-generator';

import {
  addConnection,
  addShareItems,
  checkLink,
  connectionDownloadUrl,
  createRequest,
  createShare,
  deleteRequest,
  deleteShare,
  fetchConnectionManifest,
  fetchRequestUpload,
  generatePassword,
  getInfo,
  getPassword,
  hubReasonText,
  hubTunnelLook,
  IExtensionInfo,
  linkCheckText,
  linkRef,
  listConnections,
  listRequests,
  listShares,
  offlineReason,
  removeConnection,
  removeRequestUpload,
  removeShareItems,
  renameShareItem,
  resetTunnel,
  saveFromConnection,
  saveRecord,
  setPassword,
  setTunnel,
  streamUrl,
  setupTunnel,
  uploadToConnection
} from './api';
import { clearClip, getClip, setClip } from './clipboard';
import {
  addIcon,
  closeIcon,
  cloudIcon,
  cloudOffIcon,
  cloudUnreachableIcon,
  disconnectIcon,
  downloadIcon,
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
/** Hub mode: how long the panel waits after a ring before fetching, so a
 * burst of rings costs one fetch. */
const STREAM_REFRESH_DELAY_MS = 300;
const CONTENTS_MIME = 'application/x-jupyter-icontents';
// Our own MIME for dragging a connected (remote) share entry out of the panel.
// Carries `[{ key, name, type }]`; the file browser drop patch resolves it to a
// server-side `saveFromConnection` (the bytes live on a peer, not locally).
const REMOTE_MIME = 'application/x-share-files-remote';
// Lumino dock-panel drop MIME: data is a thunk returning the Widget to dock.
// Setting this lets share entries be dropped onto the tab bar / dock area to
// open the file - the same mechanism the file browser uses for its own drags.
const FACTORY_MIME = 'application/vnd.lumino.widget-factory';
/** A file or folder of a hub share, dragged inside the panel to move it */
const HUB_ENTRY_MIME = 'application/x-share-files-hub-entry';

interface IPanelState {
  shares: IShare[];
  requests: IRequest[];
  connections: IConnection[];
  connectionData: Map<string, IRemoteShare | IRemoteRequest | null>;
  busyKeys: Set<string>;
  offlineKeys: Set<string>;
  /** Why a connection is offline, keyed by conn.key - shown in the badge
   * tooltip so a user can report the actual cause, not just "offline". */
  offlineReasons: Map<string, string>;
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
  /** Panel poll interval in seconds (one tick refreshes all shares/requests).
   * Standalone only: a hub-managed lab refreshes on the hub's change stream
   * and polls only while the browser has closed that stream. */
  pollIntervalSeconds: number;
  /** GB one download from a connected share may carry - a save into the
   * workspace or an item handed to the browser; sent with each request, the
   * server enforces it. */
  peerDownloadMaxGb: number;
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

/** A dialog body that makes the dialog check its fields as soon as it opens.
 * `Dialog` validates on input events only, so an empty required field would
 * leave the accept button enabled until the field is edited. */
class ValidatedBody extends Widget {
  constructor(node: HTMLElement, field: HTMLInputElement) {
    super({ node });
    this._field = field;
  }

  protected onAfterAttach(): void {
    // the dialog starts listening for input events in its own after-attach,
    // which runs after this one - the microtask lands once it listens
    queueMicrotask(() =>
      this._field.dispatchEvent(new Event('input', { bubbles: true }))
    );
  }

  private _field: HTMLInputElement;
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
      offlineReasons: new Map(),
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

  /** True on a hub-managed lab: shares and requests live on the hub, the
   * lab mounts no recipient route, and peers cannot be connected. */
  private get _hubMode(): boolean {
    return this._state.info?.mode === 'hub';
  }

  /** Why the hub refuses to create `kind` right now, in plain words; '' when
   * it allows it, and always '' outside hub mode. */
  private _hubRefusal(kind: 'share' | 'request'): string {
    const hub = this._state.info?.hub;
    if (!this._hubMode || !hub) {
      return '';
    }
    if (!hub.available) {
      return hubReasonText(hub.reason || 'hub_unavailable');
    }
    const allowed = kind === 'share' ? hub.allow_share : hub.allow_request;
    return allowed ? '' : hubReasonText(hub.reason || '');
  }

  /** Hub mode: the group policy requires a password on every share and
   * request, so the create dialog asks for one up front. */
  private get _passwordRequired(): boolean {
    return this._hubMode && !!this._state.info?.hub?.password_required;
  }

  /** A freshly created row the cloud toggle could not be applied to: say
   * why its link stayed on the hub's network. The server already turned the
   * toggle off when the group policy refused it; the next info fetch shows
   * the icon accordingly. */
  private _noteCloudRefusal(reason?: string): void {
    if (reason) {
      // the tunnel_ sentences each name the consequence
      // themselves - prefixing one would state 'hub network only' twice
      Notification.warning(
        reason.startsWith('tunnel_')
          ? hubReasonText(reason)
          : `Link works on the hub network only - ${hubReasonText(reason)}`,
        { autoClose: 8000 }
      );
    }
  }

  /** A share edit the server did not apply: a refusal the hub named is a
   * warning in its own words; anything else carries the server's sentence. */
  private _noteEditFailure(what: string, err: any): void {
    if (err?.reason) {
      Notification.warning(hubReasonText(err.reason), { autoClose: 8000 });
    } else {
      Notification.error(`Could not ${what}: ${err?.message || err}`, {
        autoClose: 8000
      });
    }
  }

  /** A Cloudflare switch the server did not apply: a refusal the hub named
   * (a group policy, the hub unreachable) is a warning in its own words;
   * anything else is an error. */
  private _noteCloudSwitchFailure(err: any): void {
    if (err?.reason) {
      Notification.warning(hubReasonText(err.reason), { autoClose: 8000 });
    } else {
      Notification.error(
        `Could not switch Cloudflare sharing: ${err?.message || err}`,
        { autoClose: 8000 }
      );
    }
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
        'File sharing is disabled in Settings. Enable it to create a share.',
        { autoClose: 5000 }
      );
      return;
    }
    const refusedShare = this._hubRefusal('share');
    if (refusedShare) {
      Notification.warning(refusedShare, { autoClose: 5000 });
      return;
    }
    const suggested = this._suggestName(paths);
    const spec = await this._promptForNameAndPassword(
      'Create new share',
      suggested,
      this._passwordRequired
    );
    if (!spec) {
      return;
    }
    const { name, password } = spec;
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
      const share = await createShare(
        this._serverSettings,
        name,
        paths,
        password
      );
      const copied = await this._copyLinkToClipboard(share.link);
      Notification.update({
        id: pending,
        message: `Share "${share.name}" created${copied ? ' - link copied' : ''}`,
        type: 'success',
        autoClose: 5000
      });
      this._noteCloudRefusal(share.tunnel_reason);
    } catch (err: any) {
      Notification.update({
        id: pending,
        message: `Could not create share: ${err.message || err}`,
        type: 'error',
        autoClose: 8000
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
        'File requests are disabled in Settings. Enable them to create a request.',
        { autoClose: 5000 }
      );
      return;
    }
    const refusedRequest = this._hubRefusal('request');
    if (refusedRequest) {
      Notification.warning(refusedRequest, { autoClose: 5000 });
      return;
    }
    const spec = await this._promptForNameAndPassword(
      'Create new file request',
      '',
      this._passwordRequired
    );
    if (!spec) {
      return;
    }
    const { name, password } = spec;
    try {
      const req = await createRequest(this._serverSettings, name, password);
      // no success toast - the new row appearing in the panel is feedback
      // enough and the link lands on the clipboard silently
      await this._copyLinkToClipboard(req.link);
      this._noteCloudRefusal(req.tunnel_reason);
    } catch (err: any) {
      Notification.error(`Could not create request: ${err.message || err}`, {
        autoClose: 8000
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
      // on a hub the copy runs after the answer: the row says the hub is
      // copying, says when the hub refused, and counts what the hub left out
      if (!this._hubMode) {
        Notification.success(`${paths.length} item(s) added`, {
          autoClose: 5000
        });
      }
    } catch (err: any) {
      this._noteEditFailure('add items', err);
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
      // on a hub the copy runs after the answer: the row carries it, and its
      // landing is announced by _refreshConnection. The copy may settle
      // before the next read, so the row reads it as running until then.
      const cached = this._state.connectionData.get(connectionKey);
      if (!this._hubMode) {
        Notification.success(`${paths.length} item(s) uploaded`, {
          autoClose: 5000
        });
      } else if (cached?.kind === 'request') {
        this._uploadAcceptedAt.set(connectionKey, ++this._reads);
        this._state.connectionData.set(connectionKey, {
          ...cached,
          uploading: true
        });
      }
    } catch (err: any) {
      Notification.error(`Upload failed: ${err.message || err}`, {
        autoClose: 8000
      });
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
        // info first: it names the mode (standalone or hub) the lists depend
        // on, and it is refetched each tick because public_base_url can
        // change at runtime (cloudflare --setup / --reset apply without a
        // server restart)
        try {
          const info = await getInfo(this._serverSettings);
          this._state.info = info;
        } catch {
          // keep the previous value - we just won't update the hints
        }
        const [s, r, c] = await Promise.all([
          listShares(this._serverSettings),
          listRequests(this._serverSettings),
          listConnections(this._serverSettings)
        ]);
        this._state.shares = s.shares || [];
        this._state.requests = r.requests || [];
        this._state.connections = c.connections || [];
        // Drop per-connection bookkeeping for connections that no longer
        // exist, so a re-added peer never shows the previous one's failure.
        const liveKeys = new Set(this._state.connections.map(x => x.key));
        for (const m of [
          this._state.offlineReasons,
          this._loggedOfflineKeys,
          this._uploadAcceptedAt
        ]) {
          for (const k of Array.from(m.keys())) {
            if (!liveKeys.has(k)) {
              m.delete(k);
            }
          }
        }
        for (const k of Array.from(this._state.offlineKeys)) {
          if (!liveKeys.has(k)) {
            this._state.offlineKeys.delete(k);
          }
        }
        // Drilled-in folder listings come from the Contents API and can go
        // stale after files are added/removed - drop the cache each refresh.
        this._state.subEntries.clear();
        this._detectNewUploads();
        this._updateCloudIndicator();
        // refresh connection data in parallel
        await Promise.all(
          this._state.connections.map(conn => this._refreshConnection(conn))
        );
        if (this._networkOffline) {
          this._networkOffline = false;
          console.debug('Share Files: server reachable again');
        }
      } catch (err: any) {
        // A dropped fetch (offline, tab suspended, server restarting) throws
        // ServerConnection.NetworkError - a TypeError subclass - whereas a real
        // HTTP error is a ServerConnection.ResponseError (extends Error). Ride
        // out the transient case quietly: the last-good lists are still in
        // `_state` (only assigned on success above), so the panel keeps showing
        // them and the next poll recovers. Log once per offline streak, not
        // every tick, and reserve console.error for genuine failures.
        if (this._isTransientNetworkError(err)) {
          if (!this._networkOffline) {
            this._networkOffline = true;
            console.debug(
              'Share Files: server unreachable, keeping last view; will retry'
            );
          }
        } else {
          // A real error means the server answered - we are not offline.
          this._networkOffline = false;
          console.error('Share Files: refresh failed', err);
          if (err?.reason === 'hub_unavailable') {
            // the icon shows the hub unreachable; a clicked Refresh says so
            this._updateCloudIndicator();
            if (spin) {
              Notification.warning(hubReasonText('hub_unavailable'), {
                autoClose: 5000
              });
            }
          }
        }
      }
      this._render();
      this._syncLiveness();
    })();
    await Promise.all([work, minSpin]);
    if (spin && this._refreshBtn) {
      this._refreshBtn.classList.remove('jp-mod-spinning');
    }
  }

  /** True for a transient loss of connectivity (offline, tab suspended, server
   * restarting) as opposed to a real server error. The API layer wraps a failed
   * fetch in ServerConnection.NetworkError; a real HTTP error is a
   * ServerConnection.ResponseError (extends Error), which must still surface.
   * Key on the wrapper type only - matching the whole TypeError hierarchy would
   * swallow ordinary code bugs, and `!navigator.onLine` would misclassify a real
   * error whenever the browser reports the WAN down (e.g. a localhost 500). */
  private _isTransientNetworkError(err: any): boolean {
    return err instanceof ServerConnection.NetworkError;
  }

  /** Submit a link to connect to. Used by the connect input. Resolves true
   * when the connection was stored, false when it was refused, failed or
   * the password prompt was cancelled - the input keeps the link then. */
  async connectToLink(link: string): Promise<boolean> {
    if (!link) {
      return false;
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
        return false;
      }
    } catch {
      // malformed URL - let the backend handle the error message
    }
    // Protected resources answer 401 password_required on connect - prompt
    // and retry with the password. A wrong password comes back as another
    // 401 so the prompt repeats until cancel (rate limited server-side).
    let password = '';
    let added: IConnection;
    for (;;) {
      try {
        added = await addConnection(this._serverSettings, link, password);
        break;
      } catch (err: any) {
        const status = err?.response?.status;
        if (status === 401) {
          const entered = await this._promptForConnectPassword(
            password
              ? 'Wrong password - try again'
              : 'This link is password protected'
          );
          if (entered === null) {
            return false; // cancelled
          }
          password = entered;
          continue;
        }
        Notification.error(`Could not connect: ${err.message || err}`, {
          autoClose: 8000
        });
        return false;
      }
    }
    await this.refresh();
    // the new row may sit in a collapsed section or below the fold
    this._state.expanded.connected = true;
    this._render();
    this._byFocusKey(`conn:${added.key}`)?.scrollIntoView({ block: 'nearest' });
    return true;
  }

  /** Prompt for the password of a protected link being connected to. */
  private async _promptForConnectPassword(
    title: string
  ): Promise<string | null> {
    const input = document.createElement('input');
    input.type = 'password';
    input.placeholder = 'Password';
    input.autocomplete = 'off';
    input.style.cssText = 'width: 100%; padding: 6px 8px; font-size: 14px;';
    const widget = new Widget({ node: input });
    const result = await showDialog({
      title,
      body: widget,
      buttons: [Dialog.cancelButton(), Dialog.okButton({ label: 'Connect' })]
    });
    if (!result.button.accept) {
      return null;
    }
    return input.value;
  }

  // ------------------------------------------------------------------ //
  // Lifecycle
  // ------------------------------------------------------------------ //

  protected onAfterAttach(): void {
    // The mode may not be known yet (the first info fetch is in flight):
    // start on the timer and let _syncLiveness swap it for the hub's change
    // stream - now, when the mode is already known, else after that fetch.
    this._startPolling();
    this._syncLiveness();
  }

  protected onBeforeDetach(): void {
    this._stopPolling();
    this._closeStream();
  }

  /** Pick the refresh source for the mode the server reported: the hub's
   * change stream on a hub-managed lab, the timer everywhere else. Called
   * after every refresh, so a mode change takes effect on the next tick. */
  private _syncLiveness(): void {
    if (!this.isAttached) {
      return;
    }
    if (this._hubMode && typeof EventSource !== 'undefined') {
      this._openStream();
    } else {
      this._closeStream();
      this._startPolling();
    }
  }

  /** Hub mode: one EventSource to the lab's `api/stream`. The lab holds one
   * stream to the hub for all its panels and relays each ring as `changed`;
   * the panel fetches its lists once per ring and once per (re)open, so a
   * ring lost while disconnected is covered by the next open. */
  private _openStream(): void {
    if (this._stream) {
      return;
    }
    this._stopPolling();
    const stream = new EventSource(streamUrl(this._serverSettings));
    stream.addEventListener('changed', () => this._scheduleRefresh());
    // fetch on open and on every reconnect (EventSource retries by itself)
    stream.onopen = () => this._scheduleRefresh();
    // A non-200 or non-event-stream answer (the lab restarting behind the
    // proxy, a lapsed session) closes the EventSource for good - only network
    // drops are retried by the browser. Fall back to the timer; its next
    // refresh reopens the stream through _syncLiveness.
    stream.onerror = () => {
      if (stream.readyState === EventSource.CLOSED) {
        this._closeStream();
        this._startPolling();
      }
    };
    this._stream = stream;
  }

  private _closeStream(): void {
    if (this._stream) {
      this._stream.close();
      this._stream = null;
    }
    if (this._refreshTimer) {
      window.clearTimeout(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  /** Coalesce a burst of rings (the hub rings once per changed record, and
   * the open rings too) into one fetch. */
  private _scheduleRefresh(): void {
    if (this._refreshTimer) {
      return;
    }
    this._refreshTimer = window.setTimeout(() => {
      this._refreshTimer = null;
      void this.refresh();
    }, STREAM_REFRESH_DELAY_MS);
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
    // The header accent when the tunnel is on (public links), dashed
    // silhouette when off (private links); clicking toggles between the two.
    this._cloudIndicator = document.createElement('span');
    this._cloudIndicator.className = 'jp-ShareFilesPanel-cloudIndicator';
    this._cloudIndicator.style.display = 'none';
    this._cloudIndicator.appendChild(this._svgNode(cloudIcon.svgstr));
    // a toggle button for the keyboard and screen readers: Enter and Space
    // act like a click, aria-pressed follows the on/off state
    this._cloudIndicator.tabIndex = 0;
    this._cloudIndicator.setAttribute('role', 'button');
    this._cloudIndicator.setAttribute('aria-label', 'Cloudflare sharing');
    this._cloudIndicator.setAttribute('aria-pressed', 'false');
    this._cloudIndicator.addEventListener('click', () => {
      void this._toggleTunnel();
    });
    this._cloudIndicator.addEventListener('keydown', evt => {
      if (evt.key === 'Enter' || evt.key === ' ') {
        evt.preventDefault();
        void this._toggleTunnel();
      }
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
        void this.connectToLink(link).then(connected => {
          if (connected) {
            connectInput.value = '';
          }
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
    this._connectRow = connectRow;

    this._attachDropTargetOnZone();
  }

  private _render(): void {
    if (!this._body) {
      return;
    }
    // a refresh must not take an open rename field, and the typed name with
    // it; closing the field renders
    if (this._body.querySelector('.jp-ShareFilesPanel-entryRename')) {
      return;
    }
    // every row is rebuilt below - remember what held the focus (a row
    // header, a row button or an entry button) and whether the keyboard
    // put it there
    const active = document.activeElement as HTMLElement | null;
    const focusedKey = this._body.contains(active)
      ? active?.dataset.rowKey || active?.dataset.focusKey
      : undefined;
    const byKeyboard = !!active?.matches(':focus-visible');
    // the focused row's neighbours, by key, so a row the rebuild drops (a
    // confirmed delete) can hand the focus to the one that took its place -
    // a position would move when rows above it came or went meanwhile
    let neighbourKeys: string[] = [];
    // and the section it sits in, for a delete that leaves no row at all
    let focusedSection = '';
    if (focusedKey) {
      const row = active?.closest<HTMLElement>('[data-row-key]');
      if (row) {
        const rows = Array.from(
          this._body.querySelectorAll<HTMLElement>('[data-row-key]')
        );
        const at = rows.indexOf(row);
        neighbourKeys = [rows[at + 1], rows[at - 1]]
          .map(r => r?.dataset.rowKey || '')
          .filter(Boolean);
      }
      focusedSection =
        active
          ?.closest('.jp-ShareFilesPanel-section')
          ?.querySelector<HTMLElement>('.jp-ShareFilesPanel-sectionHeader')
          ?.dataset.focusKey || '';
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
    // a hub record is named by its id alone as well as by its link
    const connectInput = this._connectRow?.querySelector('input');
    if (connectInput) {
      connectInput.placeholder = this._hubMode
        ? 'Paste a share or request link, or its id'
        : 'Paste a share or request link';
    }
    if (focusedKey) {
      this._restoreFocus(focusedKey, byKeyboard, neighbourKeys, focusedSection);
    }
  }

  /** The element in the list that `key` names - a re-render replaces the
   * node, the key survives it. */
  private _byFocusKey(key: string): HTMLElement | null {
    const all = this._body?.querySelectorAll<HTMLElement>(
      '[data-row-key], [data-focus-key]'
    );
    for (const el of Array.from(all || [])) {
      if (el.dataset.rowKey === key || el.dataset.focusKey === key) {
        return el;
      }
    }
    return null;
  }

  /** Put the focus back on what `key` names after a re-render - the element
   * itself, else the row that took a deleted row's place, else the header of
   * the section the row sat in, so a keyboard user who deletes the last row
   * stays in the panel (DEF-PANEL-70). Keyboard focus also scrolls back into
   * view when rows added above have pushed the element out of the list; a
   * clicked row keeps the scroll position. */
  private _restoreFocus(
    key: string,
    byKeyboard: boolean,
    neighbourKeys: string[] = [],
    fallbackSection = ''
  ): void {
    let el = this._byFocusKey(key);
    for (const neighbour of neighbourKeys) {
      // the named row is gone (deleted) - the one that took its place, else
      // the one above it
      el = el || this._byFocusKey(neighbour);
    }
    if (!el && fallbackSection) {
      // no row at all - the section header, by its own focus key
      el = this._byFocusKey(fallbackSection);
    }
    if (!el) {
      return;
    }
    el.focus({ preventScroll: true });
    if (!byKeyboard) {
      return;
    }
    const row = el.getBoundingClientRect();
    const list = this._body!.getBoundingClientRect();
    if (row.top < list.top || row.bottom > list.bottom) {
      el.scrollIntoView({ block: 'nearest' });
    }
  }

  /** Remember what holds the keyboard focus before a dialog opens and give
   * it back once the dialog closes: the element itself while it is still in
   * the list, else the one that replaced it, else its row header. A dialog
   * leaves the focus on the body when a re-render replaced its opener. */
  private _keepFocus(): () => void {
    const opener = document.activeElement as HTMLElement | null;
    const key = opener?.dataset.rowKey || opener?.dataset.focusKey;
    const rowKey =
      opener?.closest<HTMLElement>('[data-row-key]')?.dataset.rowKey;
    return () => {
      const back =
        opener && opener.isConnected
          ? opener
          : (key && this._byFocusKey(key)) ||
            (rowKey && this._byFocusKey(rowKey));
      if (back) {
        back.focus({ preventScroll: true });
      }
    };
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
    // focusable by script only (see `_restoreFocus`) and named across a
    // re-render; Enter and Space toggle it like a click
    header.tabIndex = -1;
    header.dataset.focusKey = `section:${key}`;
    header.setAttribute('role', 'button');
    header.setAttribute('aria-expanded', String(expanded));
    header.addEventListener('keydown', evt => {
      if (evt.key === 'Enter' || evt.key === ' ') {
        evt.preventDefault();
        this._state.expanded[key] = !this._state.expanded[key];
        this._render();
      }
    });
    const caret = document.createElement('span');
    caret.className = 'jp-ShareFilesPanel-sectionTwisty';
    caret.textContent = expanded ? '▾' : '▸'; // ▾ / ▸
    // decoration: aria-expanded carries the state, the glyph has no name
    caret.setAttribute('aria-hidden', 'true');
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
    // The row is a drop target in both modes: a drop adds to the share.
    this._attachDropTargetOnItem(item, 'share', share.id);

    const expanded = this._state.expandedItems.has(`share:${share.id}`);
    const header = document.createElement('div');
    header.className = 'jp-ShareFilesPanel-itemHeader';

    // the row's toggle is its own button: the header holds buttons, so it
    // cannot be one itself, and a name on the button keeps the glyph
    // unspoken. Its click bubbles to the header's toggle below.
    const twisty = document.createElement('button');
    twisty.className = 'jp-ShareFilesPanel-itemTwisty';
    twisty.textContent = expanded ? '▾' : '▸';
    twisty.setAttribute('aria-label', expanded ? 'Collapse' : 'Expand');
    twisty.setAttribute('aria-expanded', String(expanded));
    twisty.dataset.focusKey = `share:${share.id}/toggle`;
    header.appendChild(twisty);

    const name = document.createElement('span');
    name.className = 'jp-ShareFilesPanel-itemName';
    name.textContent = share.name;
    header.appendChild(name);

    const meta = document.createElement('span');
    meta.className = 'jp-ShareFilesPanel-itemMeta';
    if (
      this._state.busyKeys.has(share.id) ||
      share.state === 'staging' ||
      share.adding
    ) {
      meta.appendChild(this._spinnerNode());
      // what is happening, in one word; how far along is the row's own tint
      if (share.state === 'staging') {
        meta.appendChild(document.createTextNode(' staging'));
        meta.title = 'The hub is copying the files';
      } else if (share.adding) {
        meta.appendChild(document.createTextNode(' adding'));
        meta.title = 'The hub is copying the new files';
      }
    } else if (share.state === 'refused') {
      // the reason sentence on one line, cut when the row is narrow (see
      // base.css); the hover holds the whole sentence and the slug, which is
      // where a slug this version does not know stays readable
      const slug = share.reason || 'unknown';
      const sentence = hubReasonText(slug);
      meta.classList.add('jp-mod-refused');
      meta.textContent = sentence;
      meta.title = `${sentence}\nrefused: ${slug}`;
    } else {
      meta.textContent = `${share.entries.length} item${share.entries.length === 1 ? '' : 's'}`;
      if (share.add_reason) {
        // a collapsed row must still say the last add did not happen
        meta.classList.add('jp-mod-refused');
        meta.textContent += ' - add refused';
        meta.title = `${this._addRefusedText(share.add_reason)}\nrefused: ${share.add_reason}`;
      }
    }
    header.appendChild(meta);

    const copyBtn = this._makeRowIconButton(
      linkIcon.svgstr,
      'Copy link',
      `share:${share.id}/copy`,
      evt => {
        evt.stopPropagation();
        void this._copyLinkWithFeedback(share.link, copyBtn, {
          kind: 'share',
          id: share.id
        });
      }
    );
    header.appendChild(copyBtn);

    const trashBtn = this._makeRowIconButton(
      trashIcon.svgstr,
      'Delete share',
      `share:${share.id}/delete`,
      evt => {
        evt.stopPropagation();
        void this._deleteShare(share.id);
      }
    );
    trashBtn.classList.add('jp-mod-danger');
    header.appendChild(trashBtn);

    // ACC-PROG-172: a transfer in flight tints its own row instead of
    // carrying a bar beside it. The row must be transferring, not merely
    // carry the hub's numbers: a row the hub has settled keeps no tint even
    // if its `progress` is left behind. Appended last so the layer paints
    // over the name, the meta and the buttons.
    if ((share.state === 'staging' || share.adding) && share.progress?.total) {
      header.appendChild(
        this._progressOverlay(
          share.progress.copied / share.progress.total,
          `Copying ${share.name}`
        )
      );
    }

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
    this._attachKeyboardMenu(header, `share:${share.id}`, evt =>
      this._openShareContextMenu(evt, share)
    );

    item.appendChild(header);

    if (expanded) {
      item.appendChild(this._renderShareEntries(share));
    }

    return item;
  }

  private _renderShareEntries(share: IShare): HTMLElement {
    const list = document.createElement('div');
    list.className = 'jp-ShareFilesPanel-entryList';
    if (this._hubMode) {
      return this._renderHubShareEntries(share, list);
    }
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
            drillIn,
            undefined,
            `share:${share.id}/${entry.name}`
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

  /** Hub mode: the files live on the hub, which lists them flat with `/` in
   * a nested name. The folder view is derived here and every edit goes
   * through the hub's content route. */
  private _renderHubShareEntries(
    share: IShare,
    list: HTMLElement
  ): HTMLElement {
    let folder = this._state.shareSubPath.get(share.id) || '';
    if (folder && !share.entries.some(e => e.name.startsWith(`${folder}/`))) {
      // the folder went with its last file
      folder = '';
      this._state.shareSubPath.delete(share.id);
    }
    const prefix = folder ? `${folder}/` : '';
    const rows = new Map<string, IShareEntry>();
    for (const e of share.entries) {
      if (!e.name.startsWith(prefix)) {
        continue;
      }
      const rest = e.name.slice(prefix.length);
      const cut = rest.indexOf('/');
      const name = cut < 0 ? rest : rest.slice(0, cut);
      const row = rows.get(name);
      if (row) {
        row.size += e.size;
      } else {
        rows.set(name, {
          name,
          type: cut < 0 ? 'file' : 'directory',
          size: e.size
        });
      }
    }
    const visible = this._applyHiddenFilter([...rows.values()]);
    if (folder) {
      list.appendChild(this._renderUpRow(share.id, folder));
    }
    if (share.adding) {
      list.appendChild(this._renderEmpty('The hub is copying the new files'));
    }
    if (share.add_reason) {
      list.appendChild(
        this._renderEmpty(this._addRefusedText(share.add_reason))
      );
    }
    if (share.skipped && !share.adding) {
      list.appendChild(
        this._renderEmpty(
          `The hub left out ${share.skipped} ${share.skipped === 1 ? 'entry' : 'entries'} - for example symbolic links and names starting with . or ~`
        )
      );
    }
    if (visible.length === 0 && !share.adding) {
      list.appendChild(
        this._renderEmpty(
          share.state === 'staging'
            ? 'Staging - the hub is copying the files'
            : share.state === 'refused'
              ? `Refused - ${hubReasonText(share.reason || '')}`
              : rows.size === 0
                ? 'Empty - drag files onto this share to add'
                : 'No visible files (hidden ones filtered)'
        )
      );
    }
    for (const entry of visible) {
      list.appendChild(
        this._renderHubEntryRow(share.id, prefix + entry.name, entry)
      );
    }
    return list;
  }

  /** The hub keeps the refusal of the last add until the next add. */
  private _addRefusedText(reason: string): string {
    return `The hub refused the last add - ${hubReasonText(reason)}`;
  }

  /** Why an upload into another user's request was refused. Its size limit
   * is the request's, not the uploader's group policy hubReasonText names. */
  private _uploadReasonText(reason: string): string {
    return reason === 'over_cap'
      ? 'The files are larger than this request accepts.'
      : hubReasonText(reason);
  }

  private _uploadRefusedText(reason: string): string {
    return `The hub refused the last upload - ${this._uploadReasonText(reason)}`;
  }

  /** One file or folder of a hub share: remove, rename (F2 and the menu),
   * open a folder, and move by dragging a row onto a folder row. */
  private _renderHubEntryRow(
    shareId: string,
    fullName: string,
    entry: IShareEntry
  ): HTMLElement {
    const key = `share:${shareId}/${fullName}`;
    const remove = () => void this._removeHubEntry(shareId, fullName);
    // a hub entry carries no workspace path, so its context menu has no file
    // to reach and the row's own button is the save (ACC-SAVE-170)
    const save =
      entry.type === 'directory'
        ? undefined
        : () => void this.saveHubEntryFlow(shareId, fullName);
    const row = this._renderEntryRow(entry, remove, 0, undefined, save, key);
    row.classList.add('jp-mod-clickable');
    const menu = (evt: MouseEvent): Menu => {
      const m = new Menu({ commands: this._commands });
      m.addItem({
        command: 'share-files-panel:rename-hub-entry',
        args: { id: shareId, name: fullName }
      });
      m.addItem({
        command: 'share-files-panel:remove-hub-entry',
        args: { id: shareId, name: fullName }
      });
      m.open(evt.clientX, evt.clientY);
      return m;
    };
    row.addEventListener('contextmenu', evt => {
      evt.preventDefault();
      evt.stopPropagation();
      menu(evt);
    });
    this._attachKeyboardMenu(row, key, menu);
    const openFolder = (evt: Event) => {
      if (entry.type === 'directory') {
        evt.preventDefault();
        evt.stopPropagation();
        this._state.shareSubPath.set(shareId, fullName);
        this._render();
      }
    };
    row.addEventListener('dblclick', openFolder);
    row.addEventListener('keydown', evt => {
      if (evt.target !== row) {
        return;
      }
      if (evt.key === 'Enter') {
        openFolder(evt);
      } else if (evt.key === 'F2') {
        evt.preventDefault();
        this._renameHubEntry(shareId, fullName);
      } else if (evt.key === 'Delete') {
        evt.preventDefault();
        remove();
      }
    });
    this._attachHubEntryDrag(row, shareId, fullName);
    if (entry.type === 'directory') {
      this._attachHubFolderDrop(row, shareId, fullName);
    }
    return row;
  }

  /** Rename in place, the way the file browser does: Enter or leaving the
   * field commits, Escape abandons. */
  private _renameHubEntry(shareId: string, fullName: string): void {
    const key = `share:${shareId}/${fullName}`;
    const label = this._byFocusKey(key)?.querySelector(
      '.jp-ShareFilesPanel-entryName'
    );
    if (!label) {
      return;
    }
    const slash = fullName.lastIndexOf('/');
    const parent = fullName.slice(0, slash + 1);
    const base = fullName.slice(slash + 1);
    const input = document.createElement('input');
    input.className = 'jp-ShareFilesPanel-entryRename';
    input.value = base;
    input.spellcheck = false;
    input.setAttribute('aria-label', `New name for ${base}`);
    // the move syntax is the keyboard's only way to move, so it is on
    // screen while the field is open, not in a hover
    const hint = this._renderEmpty(
      'Enter renames, Escape abandons. folder/name moves it into that ' +
        'folder, /name to the top of the share'
    );
    hint.id = 'jp-ShareFilesPanel-renameHint';
    input.setAttribute('aria-describedby', hint.id);
    label.replaceWith(input);
    input.closest('.jp-ShareFilesPanel-entry')?.after(hint);
    input.focus();
    const dot = base.lastIndexOf('.');
    input.setSelectionRange(0, dot > 0 ? dot : base.length);
    let done = false;
    const finish = (commit: boolean): boolean => {
      if (done) {
        return false;
      }
      done = true;
      const typed = input.value.trim();
      // _render holds still while the field is on screen
      input.replaceWith(label);
      hint.remove();
      if (!commit || !typed || typed === base) {
        // the label is back in place, so nothing is rebuilt under a pointer
        // that closed the field with a click (DEF-PANEL-96), and the blur
        // path never moves the focus; a refresh held back while the field
        // was open is fetched now
        void this.refresh();
        return false;
      }
      const target = typed.startsWith('/') ? typed.slice(1) : parent + typed;
      void this._moveHubEntry(shareId, fullName, target);
      return true;
    };
    input.addEventListener('keydown', evt => {
      evt.stopPropagation();
      // a keyboard close that sends nothing gives the focus back to the row;
      // a sent rename is focused by _moveHubEntry under its new key
      if (evt.key === 'Enter') {
        if (!finish(true)) {
          this._byFocusKey(key)?.focus();
        }
      } else if (evt.key === 'Escape') {
        finish(false);
        this._byFocusKey(key)?.focus();
      }
    });
    for (const type of ['mousedown', 'dblclick', 'contextmenu']) {
      input.addEventListener(type, evt => evt.stopPropagation());
    }
    input.addEventListener('blur', () => finish(true));
  }

  private async _moveHubEntry(
    shareId: string,
    name: string,
    target: string
  ): Promise<void> {
    try {
      await renameShareItem(this._serverSettings, shareId, name, target);
    } catch (err: any) {
      this._noteEditFailure('rename', err);
    }
    await this.refresh();
    // the row under its new name, the row that stayed after a refusal, or -
    // when the entry left the folder on screen - the share's own row
    const row =
      this._byFocusKey(`share:${shareId}/${target}`) ||
      this._byFocusKey(`share:${shareId}/${name}`) ||
      this._byFocusKey(`share:${shareId}`);
    // the owner may have clicked elsewhere while the hub answered, and that
    // focus stays
    if (document.activeElement === document.body) {
      row?.focus();
    }
  }

  private async _removeHubEntry(shareId: string, name: string): Promise<void> {
    const restore = this._keepFocus();
    const result = await showDialog({
      title: `Remove "${name}" from the share?`,
      body: 'The hub deletes its copy. The file in your workspace stays.',
      buttons: [Dialog.cancelButton(), Dialog.warnButton({ label: 'Remove' })]
    });
    restore();
    if (result.button.accept) {
      await this._removeEntryFromShare(shareId, name);
    }
  }

  /** A hub entry row is dragged inside the panel only - the bytes are on the
   * hub, so there is nothing to drop on the file browser. */
  private _attachHubEntryDrag(
    row: HTMLElement,
    shareId: string,
    name: string
  ): void {
    row.addEventListener('mousedown', down => {
      const target = down.target as HTMLElement;
      if (down.button !== 0 || target.closest('button, input')) {
        return;
      }
      const onMove = (move: MouseEvent) => {
        if (
          Math.abs(move.clientX - down.clientX) < 5 &&
          Math.abs(move.clientY - down.clientY) < 5
        ) {
          return;
        }
        onUp();
        const mimeData = new MimeData();
        mimeData.setData(HUB_ENTRY_MIME, { shareId, name });
        void new Drag({
          mimeData,
          dragImage: this._createEntryDragImage(row),
          proposedAction: 'move',
          supportedActions: 'move',
          source: this
        }).start(move.clientX, move.clientY);
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove, true);
        document.removeEventListener('mouseup', onUp, true);
      };
      document.addEventListener('mousemove', onMove, true);
      document.addEventListener('mouseup', onUp, true);
    });
  }

  /** A folder row of a hub share takes an entry of the same share. A drop
   * onto the folder the entry already sits in does nothing. */
  private _attachHubFolderDrop(
    row: HTMLElement,
    shareId: string,
    folder: string
  ): void {
    const dragged = (event: any): string | null => {
      const data = event.mimeData?.getData(HUB_ENTRY_MIME);
      return data && data.shareId === shareId && data.name !== folder
        ? data.name
        : null;
    };
    const over = (event: any) => {
      if (dragged(event) === null) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      event.dropAction = 'move';
      row.classList.add('jp-mod-dropTarget');
    };
    row.addEventListener('lm-dragenter', over);
    row.addEventListener('lm-dragover', over);
    row.addEventListener('lm-dragleave', () =>
      row.classList.remove('jp-mod-dropTarget')
    );
    row.addEventListener('lm-drop', (event: any) => {
      const name = dragged(event);
      if (name === null) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      row.classList.remove('jp-mod-dropTarget');
      const target = `${folder}/${name.slice(name.lastIndexOf('/') + 1)}`;
      if (target !== name) {
        void this._moveHubEntry(shareId, name, target);
      }
    });
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
    row.title = 'Double-click or Enter to go up one level';
    // the keyboard's double-click: the row takes focus and Enter goes up;
    // the key lets the re-render after _goUp give the focus back to the
    // row ('..' is not an entry name, so it collides with no entry key)
    row.tabIndex = 0;
    row.dataset.rowKey = `share:${shareId}/..`;
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
    const handler = (ev: Event) => {
      ev.preventDefault();
      ev.stopPropagation();
      this._goUp(shareId, currentSubPath);
    };
    row.addEventListener('dblclick', handler);
    row.addEventListener('keydown', ev => {
      if (ev.key === 'Enter') {
        handler(ev);
      }
    });
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
        .map((c: any): IShareEntry => ({
          name: c.name,
          type: c.type === 'directory' ? 'directory' : 'file',
          size: typeof c.size === 'number' ? c.size : 0,
          path: c.path,
          // Contents API gives an ISO string; the tooltip wants unix seconds
          mtime: c.last_modified
            ? Math.floor(new Date(c.last_modified).getTime() / 1000)
            : undefined
        }))
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
      Notification.error(`Could not list folder: ${err.message || err}`, {
        autoClose: 8000
      });
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

    // the row's toggle, as the share row
    const twisty = document.createElement('button');
    twisty.className = 'jp-ShareFilesPanel-itemTwisty';
    twisty.textContent = expanded ? '▾' : '▸';
    twisty.setAttribute('aria-label', expanded ? 'Collapse' : 'Expand');
    twisty.setAttribute('aria-expanded', String(expanded));
    twisty.dataset.focusKey = `request:${req.id}/toggle`;
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
      `request:${req.id}/copy`,
      evt => {
        evt.stopPropagation();
        void this._copyLinkWithFeedback(req.link, copyBtn, {
          kind: 'request',
          id: req.id
        });
      }
    );
    header.appendChild(copyBtn);

    const trashBtn = this._makeRowIconButton(
      trashIcon.svgstr,
      'Delete request',
      `request:${req.id}/delete`,
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
    this._attachKeyboardMenu(header, `request:${req.id}`, evt =>
      this._openRequestContextMenu(evt, req)
    );

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
      // identity is the hash; show a short suffix so several uploaders with
      // the same display name (multiple "anonymous") stay distinguishable
      nameNode.textContent =
        uploader.hash && uploader.hash !== uploader.name
          ? `${uploader.name} (${uploader.hash.slice(0, 4).toLowerCase()})`
          : uploader.name;
      groupRow.appendChild(nameNode);
      list.appendChild(groupRow);

      for (const entry of uploader.entries) {
        const key = `request:${req.id}/${uploader.hash || uploader.name}/${entry.name}`;
        const row = this._hubMode
          ? // the bytes sit on the hub's volume: fetch them in, never remove
            this._renderEntryRow(
              entry,
              undefined,
              1,
              undefined,
              () => {
                void this._fetchUploadFlow(req, entry);
              },
              key
            )
          : this._renderEntryRow(
              entry,
              () => {
                void this._removeUpload(
                  req.id,
                  uploader.hash || uploader.name,
                  entry.name
                );
              },
              1,
              undefined,
              undefined,
              key
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

    // the row's toggle, as the share row
    const twisty = document.createElement('button');
    twisty.className = 'jp-ShareFilesPanel-itemTwisty';
    twisty.textContent = expanded ? '▾' : '▸';
    twisty.setAttribute('aria-label', expanded ? 'Collapse' : 'Expand');
    twisty.setAttribute('aria-expanded', String(expanded));
    twisty.dataset.focusKey = `conn:${conn.key}/toggle`;
    header.appendChild(twisty);

    const data = this._state.connectionData.get(conn.key);
    const name = document.createElement('span');
    name.className = 'jp-ShareFilesPanel-itemName';
    name.textContent = (data && data.name) || conn.name || conn.id;
    header.appendChild(name);

    if (this._state.offlineKeys.has(conn.key)) {
      const offline = document.createElement('span');
      offline.className = 'jp-ShareFilesPanel-offline';
      const reason = this._state.offlineReasons.get(conn.key);
      // a changed password is a step for the user, not an outage to wait out
      offline.textContent =
        this._hubMode && reason === hubReasonText('password_changed')
          ? 'password changed'
          : 'offline';
      // "unavailable", not "could not reach" - a 401 or 404 means the peer
      // answered fine and the fault is the password or a deleted resource.
      // A hub record has an owner, not a peer, and its reason says it all.
      offline.title = this._hubMode
        ? reason || 'Unavailable'
        : reason
          ? `Peer unavailable: ${reason}`
          : 'Peer unavailable';
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
      const requestData = data as IRemoteRequest | undefined;
      const reason = requestData?.upload_reason;
      if (requestData?.uploading) {
        meta.textContent = 'uploading';
      } else if (reason) {
        // a collapsed row must still say the last upload did not happen
        meta.classList.add('jp-mod-refused');
        meta.textContent = 'request - upload refused';
        meta.title = `${this._uploadRefusedText(reason)}\nrefused: ${reason}`;
      } else {
        meta.textContent = 'request';
      }
    }
    header.appendChild(meta);

    const disconnectBtn = this._makeRowIconButton(
      disconnectIcon.svgstr,
      'Disconnect',
      `conn:${conn.key}/disconnect`,
      evt => {
        evt.stopPropagation();
        void this._disconnect(conn);
      }
    );
    header.appendChild(disconnectBtn);

    // ACC-HUBM-180: an upload in flight tints the row it goes to, as every
    // other transfer does (ACC-PROG-172)
    const upload = data as IRemoteRequest | undefined;
    if (upload?.uploading && upload.progress?.total) {
      header.appendChild(
        this._progressOverlay(
          upload.progress.copied / upload.progress.total,
          `Uploading to ${name.textContent}`
        )
      );
    }

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
    this._attachKeyboardMenu(header, `conn:${conn.key}`, evt =>
      this._openConnectionContextMenu(evt, conn)
    );

    item.appendChild(header);

    if (expanded) {
      if (conn.kind === 'share' && data) {
        const shareData = data as IRemoteShare;
        item.appendChild(this._renderConnectedShareEntries(conn, shareData));
      } else if (conn.kind === 'request') {
        const reason = (data as IRemoteRequest | undefined)?.upload_reason;
        if (reason && !(data as IRemoteRequest).uploading) {
          item.appendChild(this._renderEmpty(this._uploadRefusedText(reason)));
        }
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
      name.textContent = entry.name + (entry.type === 'directory' ? '/' : '');
      row.appendChild(name);
      const size = document.createElement('span');
      size.className = 'jp-ShareFilesPanel-entrySize';
      size.textContent = this._formatSize(entry.size);
      row.appendChild(size);
      const url = connectionDownloadUrl(
        this._serverSettings,
        conn.key,
        entry.name,
        this._settings.peerDownloadMaxGb
      );
      row.title = this._entryTooltip(entry, false);
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
      this._attachKeyboardMenu(row, `conn:${conn.key}/${entry.name}`, evt =>
        this._openConnectedEntryContextMenu(evt, conn, entry, url)
      );
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
  /** Save every entry of a connected share into the file browser's current
   * folder (ACC-SAVE-171). `names: null` is the server's "all"; the bytes
   * come from the peer through this server, never through the browser. */
  async saveConnectionFlow(
    connKey: string,
    name: string,
    archive: '' | 'zip' = ''
  ): Promise<void> {
    const target = this._getCurrentDir();
    this._state.busyKeys.add(connKey);
    this._render();
    const pending = Notification.emit(`Saving ${name}...`, 'in-progress');
    try {
      const { saved } = await saveFromConnection(
        this._serverSettings,
        connKey,
        target,
        null,
        this._settings.peerDownloadMaxGb,
        archive
      );
      Notification.update({
        id: pending,
        message: `Saved ${name} to ./${saved[0]}`,
        type: 'success',
        autoClose: 5000
      });
      await this._revealInFileBrowser(target);
    } catch (err: any) {
      Notification.update({
        id: pending,
        message: `Could not save ${name}: ${err?.message || err}`,
        type: 'error',
        autoClose: 8000
      });
    } finally {
      this._state.busyKeys.delete(connKey);
      this._render();
    }
  }

  async saveRemoteEntryTo(
    connKey: string,
    name: string,
    destDir: string
  ): Promise<void> {
    this._state.busyKeys.add(connKey);
    this._render();
    try {
      await saveFromConnection(
        this._serverSettings,
        connKey,
        destDir,
        [name],
        this._settings.peerDownloadMaxGb
      );
      Notification.success(`Saved "${name}"`, { autoClose: 5000 });
      await this._revealInFileBrowser(destDir);
    } catch (err: any) {
      Notification.error(`Could not save "${name}": ${err.message || err}`, {
        autoClose: 8000
      });
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
        [entry.name],
        this._settings.peerDownloadMaxGb
      );
      const saved = res.saved && res.saved[0];
      if (entry.type === 'directory') {
        Notification.success(`Saved "${entry.name}"`, { autoClose: 5000 });
        await this._revealInFileBrowser(destDir);
      } else if (saved) {
        await this._commands.execute('docmanager:open', { path: saved });
      }
    } catch (err: any) {
      Notification.error(
        `Could not open "${entry.name}": ${err.message || err}`,
        { autoClose: 8000 }
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
  ): Menu {
    const menu = new Menu({ commands: this._commands });
    // on a hub the bytes go from the hub into the workspace, never through
    // the lab to the browser (ACC-HUBM-175)
    if (!this._hubMode) {
      menu.addItem({
        command: 'share-files-panel:download-remote-entry',
        args: {
          url: downloadUrl,
          filename: entry.name + (entry.type === 'directory' ? '.zip' : '')
        }
      });
    }
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
    return menu;
  }

  /**
   * Download a file of a connected peer through our server's download route
   * (see `connectionDownloadUrl`) by clicking a hidden anchor: the browser
   * streams it to disk itself, and lists a failed download on a 4xx/5xx.
   */
  private _downloadRemote(url: string, filename: string): void {
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }

  private _renderEntryRow(
    entry: IShareEntry,
    onRemove?: () => void,
    indentLevel = 0,
    onOpenFolder?: (entry: IShareEntry) => void,
    onFetch?: () => void,
    // names the row's button across a re-render (see `_byFocusKey`); rows
    // without a button need none
    focusKey?: string
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
    row.title = this._entryTooltip(entry);
    if (onRemove) {
      const btn = document.createElement('button');
      btn.className = 'jp-ShareFilesPanel-entryRemove';
      btn.title = 'Remove';
      btn.dataset.focusKey = `${focusKey}/remove`;
      btn.appendChild(this._svgNode(closeIcon.svgstr));
      btn.addEventListener('click', ev => {
        ev.stopPropagation();
        onRemove();
      });
      row.appendChild(btn);
    }
    if (onFetch) {
      const btn = document.createElement('button');
      btn.className = 'jp-ShareFilesPanel-entryRemove jp-mod-fetch';
      btn.title = 'Save to Current Folder';
      btn.dataset.focusKey = `${focusKey}/fetch`;
      btn.appendChild(this._svgNode(downloadIcon.svgstr));
      btn.addEventListener('click', ev => {
        ev.stopPropagation();
        onFetch();
      });
      row.appendChild(btn);
    }
    if (entry.path) {
      row.classList.add('jp-mod-clickable');
      row.addEventListener('contextmenu', evt => {
        evt.preventDefault();
        evt.stopPropagation();
        this._openEntryContextMenu(evt, entry);
      });
      this._attachKeyboardMenu(
        row,
        focusKey ?? `entry:${entry.path}`,
        // the attach sits inside `if (entry.path)`, so the opener never
        // fires without one
        evt => this._openEntryContextMenu(evt, entry) as Menu
      );
      const open = (evt: Event) => {
        evt.preventDefault();
        evt.stopPropagation();
        if (entry.type === 'directory' && onOpenFolder) {
          onOpenFolder(entry);
        } else {
          void this._openEntry(entry);
        }
      };
      row.addEventListener('dblclick', open);
      // the keyboard's double-click: Enter on the focused row itself, not on
      // a button inside it (a button's Enter is its click)
      row.addEventListener('keydown', evt => {
        if (evt.key === 'Enter' && evt.target === row) {
          open(evt);
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
      Notification.error(`Could not open: ${err.message || err}`, {
        autoClose: 8000
      });
    }
  }

  private _openEntryContextMenu(
    evt: MouseEvent,
    entry: IShareEntry
  ): Menu | undefined {
    if (!entry.path) {
      return undefined;
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
    return menu;
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
          const kind = String(args.kind || '');
          const id = String(args.id || '');
          const ref =
            (kind === 'share' || kind === 'request') && id
              ? { kind: kind as 'share' | 'request', id }
              : undefined;
          const restore = this._keepFocus();
          void this._copyLinkToClipboard(link).then(async ok => {
            await this._showLinkDialog(link, ok, ref);
            restore();
          });
        }
      });
      c.addCommand('share-files-panel:set-password', {
        label: args =>
          args.hasPassword ? 'Change Password...' : 'Set Password...',
        execute: args => {
          const kind = String(args.kind || 'share') as 'share' | 'request';
          const id = String(args.id || '');
          if (id) {
            void this._changePasswordFlow(kind, id);
          }
        }
      });
      c.addCommand('share-files-panel:delete-share', {
        label: 'Delete Share',
        execute: args => {
          const id = String(args.id || '');
          void this._deleteShare(id);
        }
      });
      c.addCommand('share-files-panel:rename-hub-entry', {
        label: 'Rename',
        execute: args =>
          this._renameHubEntry(String(args.id || ''), String(args.name || ''))
      });
      c.addCommand('share-files-panel:remove-hub-entry', {
        label: 'Remove from Share',
        execute: args => {
          void this._removeHubEntry(
            String(args.id || ''),
            String(args.name || '')
          );
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
            this._downloadRemote(url, filename);
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
        // offered whatever the grant: a refused request answers with the
        // hub's own sentence from createRequestFlow, which the owner can
        // read, where a greyed entry carried its reason in a caption nothing
        // renders (DEF-PANEL-86)
        execute: () => {
          void this.createRequestFlow();
        }
      });
      c.addCommand('share-files-panel:copy-entry-to-cwd', {
        label: 'Save to Current Folder',
        execute: args => {
          const path = String(args.path || '');
          const name = String(args.name || '');
          if (!path) {
            return;
          }
          void this._copyEntryToCurrentDir(path, name);
        }
      });
      c.addCommand('share-files-panel:save-connection', {
        label: 'Save Record to Current Folder',
        execute: args => {
          void this.saveConnectionFlow(
            String(args.key || ''),
            String(args.name || '')
          );
        }
      });
      c.addCommand('share-files-panel:connect-again', {
        label: 'Connect Again...',
        execute: args => {
          void this.connectToLink(String(args.link || ''));
        }
      });
      c.addCommand('share-files-panel:save-connection-zip', {
        label: 'Save Record as Zip',
        execute: args => {
          void this.saveConnectionFlow(
            String(args.key || ''),
            String(args.name || ''),
            'zip'
          );
        }
      });
      c.addCommand('share-files-panel:save-record', {
        label: 'Save Record to Current Folder',
        execute: args => {
          void this.saveRecordFlow(
            args.kind === 'requests' ? 'requests' : 'shares',
            String(args.id || ''),
            String(args.name || ''),
            ''
          );
        }
      });
      c.addCommand('share-files-panel:save-record-zip', {
        label: 'Save Record as Zip',
        execute: args => {
          void this.saveRecordFlow(
            args.kind === 'requests' ? 'requests' : 'shares',
            String(args.id || ''),
            String(args.name || ''),
            'zip'
          );
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
          const clip = getClip();
          if (id && this._hubMode && clip?.kind === 'local') {
            // the hub copies after its answer, so a cut is pasted as a copy:
            // nothing is deleted on the strength of a 202
            void this.addToShareFlow(id, clip.paths);
            if (clip.mode === 'cut') {
              clearClip();
              Notification.info(
                'A cut is pasted as a copy - the originals stay in your workspace',
                { autoClose: 8000 }
              );
            }
          } else if (id) {
            void this._pasteIntoShare(id);
          }
        }
      });
      c.addCommand('share-files-panel:paste-into-request', {
        label: 'Paste',
        execute: args => {
          const key = String(args.key || '');
          const clip = getClip();
          if (key && this._hubMode && clip?.kind === 'local') {
            // as a paste into a hub share: the hub copies after its answer,
            // so nothing is deleted on the strength of a 202
            void this.uploadToConnectionFlow(key, clip.paths);
            if (clip.mode === 'cut') {
              clearClip();
              Notification.info(
                'A cut is pasted as a copy - the originals stay in your workspace',
                { autoClose: 8000 }
              );
            }
          } else if (key) {
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
      Notification.success(`${paths.length} item(s) added`, {
        autoClose: 5000
      });
    } catch (err: any) {
      Notification.error(`Could not paste: ${err.message || err}`, {
        autoClose: 8000
      });
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
      Notification.success(`${paths.length} item(s) uploaded`, {
        autoClose: 5000
      });
    } catch (err: any) {
      Notification.error(`Could not paste: ${err.message || err}`, {
        autoClose: 8000
      });
    } finally {
      this._state.busyKeys.delete(connKey);
      await this.refresh();
    }
  }

  /** Save one entry of a hub share into the file browser's current folder
   * (ACC-SAVE-170). The hub reads no single entry, so the server fetches the
   * record and moves this one out of it. */
  async saveHubEntryFlow(shareId: string, name: string): Promise<void> {
    const pending = Notification.emit(`Saving ${name}...`, 'in-progress');
    try {
      const { path } = await saveRecord(
        this._serverSettings,
        'shares',
        shareId,
        this._getCurrentDir(),
        '',
        name
      );
      Notification.update({
        id: pending,
        message: `Saved ${name} to ./${path}`,
        type: 'success',
        autoClose: 5000
      });
    } catch (err: any) {
      Notification.update({
        id: pending,
        message: `Could not save ${name}: ${err?.message || err}`,
        type: 'error',
        autoClose: 8000
      });
    }
  }

  /** Write a whole share or request into the file browser's current folder
   * (ACC-SAVE-171). `archive` empty puts the files in a folder named after
   * the record, 'zip' puts one archive of that name there instead. */
  async saveRecordFlow(
    kind: 'shares' | 'requests',
    id: string,
    name: string,
    archive: '' | 'zip'
  ): Promise<void> {
    const target = this._getCurrentDir();
    const pending = Notification.emit(`Saving ${name}...`, 'in-progress');
    try {
      const { path } = await saveRecord(
        this._serverSettings,
        kind,
        id,
        target,
        archive
      );
      Notification.update({
        id: pending,
        message: `Saved ${name} to ./${path}`,
        type: 'success',
        autoClose: 5000
      });
    } catch (err: any) {
      Notification.update({
        id: pending,
        message: `Could not save ${name}: ${err?.message || err}`,
        type: 'error',
        autoClose: 8000
      });
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
      Notification.success(`Copied ${entryName} → ${dest}`, {
        autoClose: 5000
      });
    } catch (err: any) {
      Notification.error(`Could not copy: ${err.message || err}`, {
        autoClose: 8000
      });
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
      Notification.error(`Could not navigate: ${err.message || err}`, {
        autoClose: 8000
      });
    }
  }

  /** Make a row header or an entry row a keyboard stop: Shift+F10 or the
   * ContextMenu key opens its context menu below it. `key` names the row so
   * a re-render puts the focus back on it. */
  private _attachKeyboardMenu(
    header: HTMLElement,
    key: string,
    open: (evt: MouseEvent) => Menu
  ): void {
    header.tabIndex = 0;
    header.dataset.rowKey = key;
    header.addEventListener('keydown', evt => {
      if (evt.key === 'ContextMenu' || (evt.shiftKey && evt.key === 'F10')) {
        evt.preventDefault();
        evt.stopPropagation();
        const box = header.getBoundingClientRect();
        const menu = open(
          new MouseEvent('contextmenu', {
            clientX: box.left,
            clientY: box.bottom
          })
        );
        // Lumino leaves the focus on the body when the menu closes - give it
        // back to the row header, or to its replacement after a re-render
        menu.aboutToClose.connect(() => {
          const row = header.isConnected ? header : this._byFocusKey(key);
          row?.focus();
        });
      }
    });
  }

  private _openShareContextMenu(evt: MouseEvent, share: IShare): Menu {
    const menu = new Menu({ commands: this._commands });
    menu.addItem({
      command: 'share-files-panel:copy-link',
      args: { link: share.link, kind: 'share', id: share.id }
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
      command: 'share-files-panel:save-record',
      args: { kind: 'shares', id: share.id, name: share.name }
    });
    menu.addItem({
      command: 'share-files-panel:save-record-zip',
      args: { kind: 'shares', id: share.id, name: share.name }
    });
    menu.addItem({ type: 'separator' });
    menu.addItem({
      command: 'share-files-panel:set-password',
      args: { kind: 'share', id: share.id, hasPassword: !!share.has_password }
    });
    menu.addItem({ type: 'separator' });
    menu.addItem({
      command: 'share-files-panel:delete-share',
      args: { id: share.id }
    });
    menu.open(evt.clientX, evt.clientY);
    return menu;
  }

  private _openRequestContextMenu(evt: MouseEvent, req: IRequest): Menu {
    const menu = new Menu({ commands: this._commands });
    menu.addItem({
      command: 'share-files-panel:copy-link',
      args: { link: req.link, kind: 'request', id: req.id }
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
      command: 'share-files-panel:save-record',
      args: { kind: 'requests', id: req.id, name: req.name }
    });
    menu.addItem({
      command: 'share-files-panel:save-record-zip',
      args: { kind: 'requests', id: req.id, name: req.name }
    });
    menu.addItem({ type: 'separator' });
    menu.addItem({
      command: 'share-files-panel:set-password',
      args: { kind: 'request', id: req.id, hasPassword: !!req.has_password }
    });
    menu.addItem({ type: 'separator' });
    menu.addItem({
      command: 'share-files-panel:delete-request',
      args: { id: req.id }
    });
    menu.open(evt.clientX, evt.clientY);
    return menu;
  }

  private _openConnectionContextMenu(evt: MouseEvent, conn: IConnection): Menu {
    const menu = new Menu({ commands: this._commands });
    const data = this._state.connectionData.get(conn.key);
    // the stored link when no read has succeeded since the page loaded
    const link = (data && data.link) || conn.link || '';
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
    // only a share holds entries to save; a connected request is an inbox
    // this user uploads to
    if (conn.kind === 'share') {
      menu.addItem({ type: 'separator' });
      menu.addItem({
        command: 'share-files-panel:save-connection',
        args: { key: conn.key, name: conn.name }
      });
      menu.addItem({
        command: 'share-files-panel:save-connection-zip',
        args: { key: conn.key, name: conn.name }
      });
    }
    menu.addItem({ type: 'separator' });
    if (this._state.offlineKeys.has(conn.key)) {
      // a password the owner set or changed since: the prompt asks for it
      menu.addItem({
        command: 'share-files-panel:connect-again',
        args: { link: conn.link || conn.id }
      });
    }
    menu.addItem({
      command: 'share-files-panel:disconnect',
      args: { key: conn.key }
    });
    menu.open(evt.clientX, evt.clientY);
    return menu;
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
      this._noteEditFailure('remove', err);
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
      Notification.error(`Could not remove: ${err.message || err}`, {
        autoClose: 8000
      });
    } finally {
      this._state.busyKeys.delete(requestId);
      await this.refresh();
    }
  }

  /** Hub mode: copy one recipient upload into the file browser's current
   * folder through the hub's transfer job - the bytes never pass the panel. */
  private async _fetchUploadFlow(
    req: IRequest,
    entry: IShareEntry
  ): Promise<void> {
    if (!entry.upload_id) {
      return;
    }
    this._state.busyKeys.add(req.id);
    this._render();
    try {
      const res = await fetchRequestUpload(
        this._serverSettings,
        req.id,
        entry.upload_id,
        this._getCurrentDir(),
        req.name
      );
      Notification.success(`Fetched ${entry.name} to ${res.path}`, {
        autoClose: 5000
      });
    } catch (err: any) {
      Notification.error(`Could not fetch: ${err.message || err}`, {
        autoClose: 8000
      });
    } finally {
      this._state.busyKeys.delete(req.id);
      this._render();
    }
  }

  private async _deleteShare(id: string): Promise<void> {
    const restore = this._keepFocus();
    // the dialog names the share so an owner with similar names is not
    // confirming blind
    const name = this._state.shares.find(s => s.id === id)?.name;
    const result = await showDialog({
      title: name ? `Delete share "${name}"?` : 'Delete share?',
      body: 'This will remove the share permanently. The link will stop working.',
      buttons: [Dialog.cancelButton(), Dialog.warnButton({ label: 'Delete' })]
    });
    restore();
    if (!result.button.accept) {
      return;
    }
    try {
      await deleteShare(this._serverSettings, id);
    } catch (err: any) {
      Notification.error(`Could not delete: ${err.message || err}`, {
        autoClose: 8000
      });
    }
    await this.refresh();
  }

  private async _deleteRequest(id: string): Promise<void> {
    const restore = this._keepFocus();
    const name = this._state.requests.find(r => r.id === id)?.name;
    const result = await showDialog({
      title: name ? `Delete request "${name}"?` : 'Delete request?',
      body: 'This will remove the request and any uploads permanently.',
      buttons: [Dialog.cancelButton(), Dialog.warnButton({ label: 'Delete' })]
    });
    restore();
    if (!result.button.accept) {
      return;
    }
    try {
      await deleteRequest(this._serverSettings, id);
    } catch (err: any) {
      Notification.error(`Could not delete: ${err.message || err}`, {
        autoClose: 8000
      });
    }
    await this.refresh();
  }

  private async _disconnect(conn: IConnection): Promise<void> {
    try {
      await removeConnection(this._serverSettings, conn.key);
    } catch (err: any) {
      Notification.error(`Could not disconnect: ${err.message || err}`, {
        autoClose: 8000
      });
    }
    await this.refresh();
  }

  // ------------------------------------------------------------------ //
  // Internal: connections - remote manifest refresh
  // ------------------------------------------------------------------ //

  private async _refreshConnection(conn: IConnection): Promise<void> {
    if (!conn.link && !this._linkFor(conn)) {
      // No link to poll - do not leave a previous failure pinned on the badge
      // forever, since no later poll can ever clear it.
      this._state.offlineKeys.delete(conn.key);
      this._state.offlineReasons.delete(conn.key);
      return;
    }
    const link = conn.link || this._linkFor(conn);
    const sent = ++this._reads;
    try {
      // read by our own server - same-origin, whatever policy the page carries
      const data = await fetchConnectionManifest(
        this._serverSettings,
        conn.key
      );
      if (sent < (this._uploadAcceptedAt.get(conn.key) || 0)) {
        // sent before an upload was accepted, so it describes the record
        // without that upload: the reads sent after it replace it
        return;
      }
      const before = this._state.connectionData.get(conn.key);
      this._state.connectionData.set(conn.key, data);
      this._noteUploadLanded(before, data);
      this._state.offlineKeys.delete(conn.key);
      this._state.offlineReasons.delete(conn.key);
      this._loggedOfflineKeys.delete(conn.key);
    } catch (err: any) {
      this._state.offlineKeys.add(conn.key);
      // Every distinct failure collapses into one "offline" badge - our
      // server's 502 for a peer it could not reach, its 401 for a rejected
      // password, its 404 for a removed record, or our server not answering
      // at all. Without the reason a user report of "it just shows offline"
      // cannot be diagnosed, so record it (once per streak) and keep it for
      // the badge tooltip.
      const reason = offlineReason(err);
      this._state.offlineReasons.set(conn.key, reason);
      // Log once per (link, reason) so a 15s poll does not flood the console,
      // while a peer that starts failing differently - or the same failure on
      // a new link - still gets its own line.
      const logKey = `${link}|${reason}`;
      if (this._loggedOfflineKeys.get(conn.key) !== logKey) {
        this._loggedOfflineKeys.set(conn.key, logKey);
        console.warn(
          `Share Files: peer unavailable (${conn.key}) via ${link} -`,
          err
        );
      }
    }
  }

  /** On a hub an upload into a connected request lands after its answer:
   * say so once, when the manifest stops reporting it running. */
  private _noteUploadLanded(
    before: IRemoteShare | IRemoteRequest | null | undefined,
    now: IRemoteShare | IRemoteRequest
  ): void {
    if (
      before?.kind !== 'request' ||
      !before.uploading ||
      now.kind !== 'request' ||
      now.uploading
    ) {
      return;
    }
    if (now.upload_reason) {
      Notification.error(
        `The upload to ${now.name} was refused: ${this._uploadReasonText(now.upload_reason)}`,
        { autoClose: 8000 }
      );
    } else {
      Notification.success(
        `${now.uploaded || 0} item(s) uploaded to ${now.name}`,
        { autoClose: 5000 }
      );
    }
  }

  private _linkFor(conn: IConnection): string {
    // The connection's full link is persisted server-side (see
    // ConnectionStore.add). We must NOT reconstruct it from host + id here:
    // on JupyterHub the owner lives under `/user/<name>/`, which the client
    // cannot know, so a reconstructed URL drops that prefix and JupyterHub
    // bounces it to `/hub/...` (404), wrongly marking an online share offline.
    // Return the stored link only; the server refuses an entry without one
    // until it is disconnected and connected again.
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
        console.warn('Share Files: getPaths error', err);
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

  /** Build a password input row with a "Generate" button (xkcdpass-style
   * passphrase from the server). Returns the row and the input element. */
  private _passwordRow(initial = ''): {
    row: HTMLElement;
    input: HTMLInputElement;
  } {
    const row = document.createElement('div');
    row.style.cssText = 'display: flex; gap: 6px; align-items: stretch;';
    const input = document.createElement('input');
    input.type = 'text';
    input.value = initial;
    input.placeholder = 'Password (optional)';
    input.autocomplete = 'off';
    input.spellcheck = false;
    // Height matches the standard dialog button (32px) so the input and the
    // Generate button line up evenly in the row.
    input.style.cssText =
      'flex: 1; box-sizing: border-box; height: 32px; padding: 0 8px;' +
      ' font-size: 14px; font-family: var(--jp-code-font-family, monospace);';
    const gen = document.createElement('button');
    gen.type = 'button';
    gen.textContent = 'Generate';
    gen.title = 'Generate a memorable passphrase';
    // Small inline button, same height as the input (see .jp-ShareFiles-miniBtn
    // in base.css - resets the inherited button line-height and keeps the focus
    // ring inside so it is not clipped at the dialog edge).
    gen.className = 'jp-ShareFiles-miniBtn';
    gen.addEventListener('click', evt => {
      evt.preventDefault();
      evt.stopPropagation();
      void generatePassword(this._serverSettings)
        .then(r => {
          input.value = r.password || '';
          // assigning the value raises no input event of its own, and the
          // dialog re-checks its fields on that event alone
          input.dispatchEvent(new Event('input', { bubbles: true }));
        })
        .catch((err: any) => {
          Notification.error(
            `Could not generate a password: ${err?.message || err}`,
            { autoClose: 8000 }
          );
        });
    });
    row.appendChild(input);
    row.appendChild(gen);
    return { row, input };
  }

  /** The create dialog: name and an optional password. With `required`
   * (a hub group policy) the password field starts with a generated
   * passphrase and an empty one refuses the create instead of letting the
   * hub refuse it after the fact. */
  private async _promptForNameAndPassword(
    title: string,
    suggested: string,
    required = false
  ): Promise<{ name: string; password: string } | null> {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'display: flex; flex-direction: column; gap: 8px;';
    const input = document.createElement('input');
    input.type = 'text';
    input.value = suggested;
    input.placeholder = 'Name';
    input.style.cssText =
      'width: 100%; box-sizing: border-box; height: 32px; padding: 0 8px;' +
      ' font-size: 14px;';
    wrap.appendChild(input);
    const { row: pwRow, input: pwInput } = this._passwordRow();
    wrap.appendChild(pwRow);
    const hint = document.createElement('div');
    hint.textContent = required
      ? 'Your group requires a password: recipients must enter it before ' +
        'they can see or access the files.'
      : 'With a password set, recipients must enter it before they can ' +
        'see or access the files.';
    hint.style.cssText =
      'font-size: var(--jp-ui-font-size0);' +
      ' color: var(--jp-ui-font-color2);';
    wrap.appendChild(hint);
    if (required) {
      pwInput.placeholder = 'Password (required)';
      pwInput.required = true;
      try {
        pwInput.value =
          (await generatePassword(this._serverSettings)).password || '';
      } catch {
        // the field stays empty - the check below still holds
      }
    }
    // the change dialog's rule holds here too: when the policy requires a
    // password and the field opened empty (generate-password failed), the
    // dialog checks it while it is open, not after Create
    const widget = required
      ? new ValidatedBody(wrap, pwInput)
      : new Widget({ node: wrap });
    const result = await showDialog({
      title,
      body: widget,
      buttons: [Dialog.cancelButton(), Dialog.okButton({ label: 'Create' })]
    });
    if (!result.button.accept) {
      return null;
    }
    const name = input.value.trim();
    if (!name) {
      return null;
    }
    const password = pwInput.value.trim();
    if (required && !password) {
      Notification.warning(hubReasonText('password_required'), {
        autoClose: 5000
      });
      return null;
    }
    return { name, password };
  }

  /** Set / change / clear the password of an existing share or request
   * (context menu). Pre-filled with the current password. */
  private async _changePasswordFlow(
    kind: 'share' | 'request',
    id: string
  ): Promise<void> {
    let current = '';
    try {
      current = (await getPassword(this._serverSettings, kind, id)).password;
    } catch {
      // resource may have vanished; the save below will surface the error
    }
    const restore = this._keepFocus();
    const wrap = document.createElement('div');
    wrap.style.cssText = 'display: flex; flex-direction: column; gap: 8px;';
    const { row: pwRow, input: pwInput } = this._passwordRow(current);
    wrap.appendChild(pwRow);
    // a group policy that requires a password refuses its removal: the
    // field is required, so the dialog keeps Save disabled while it is empty
    const required = this._passwordRequired;
    if (required) {
      pwInput.placeholder = 'Password (required)';
      pwInput.required = true;
    }
    const hint = document.createElement('div');
    // the invalidation sentence names a consequence that exists only when a
    // password does - a first-time setter has no old unlocks
    const invalidates = current
      ? ' Changing it invalidates everyone who already unlocked with the old one.'
      : '';
    hint.textContent = required
      ? 'Your group requires a password: recipients must enter it before ' +
        'they can see or access the files.' +
        invalidates
      : (current
          ? 'Leave empty to remove the password.'
          : 'Leave empty for no password.') + invalidates;
    hint.style.cssText =
      'font-size: var(--jp-ui-font-size0);' +
      ' color: var(--jp-ui-font-color2);';
    wrap.appendChild(hint);
    // a record that predates the policy opens with an empty field: the
    // dialog checks it before the user types, not after Save
    const widget = required
      ? new ValidatedBody(wrap, pwInput)
      : new Widget({ node: wrap });
    const result = await showDialog({
      title: current ? 'Change password' : 'Set password',
      body: widget,
      buttons: [Dialog.cancelButton(), Dialog.okButton({ label: 'Save' })]
    });
    restore();
    if (!result.button.accept) {
      return;
    }
    const password = pwInput.value.trim();
    if (required && !password) {
      // the dialog checks the field only once it is edited: an empty field
      // it never checked is refused here, not by the hub
      Notification.warning(hubReasonText('password_required'), {
        autoClose: 5000
      });
      return;
    }
    try {
      await setPassword(this._serverSettings, kind, id, password);
      Notification.success(password ? 'Password set' : 'Password removed', {
        autoClose: 4000
      });
    } catch (err: any) {
      Notification.error(`Could not save password: ${err.message || err}`, {
        autoClose: 8000
      });
    }
    await this.refresh();
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
    btn: HTMLElement,
    ref?: { kind: 'share' | 'request'; id: string }
  ): Promise<void> {
    const restore = this._keepFocus();
    const ok = await this._copyLinkToClipboard(link);
    if (btn) {
      btn.classList.add('jp-mod-copied');
      window.setTimeout(() => btn.classList.remove('jp-mod-copied'), 700);
    }
    await this._showLinkDialog(link, ok, ref);
    restore();
  }

  /**
   * Show a dialog with the link visible and a Copy button. Used by the
   * right-click "Copy Link" command so the user sees what they're copying.
   */
  private async _showLinkDialog(
    link: string,
    alreadyCopied: boolean,
    ref?: { kind: 'share' | 'request'; id: string }
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
      ' padding: 8px 34px 8px 10px;' +
      ' font-family: var(--jp-code-font-family, monospace);' +
      ' font-size: var(--jp-ui-font-size1);' +
      ' color: var(--jp-ui-font-color1);' +
      ' background: var(--jp-layout-color1);' +
      ' border: 1px solid var(--jp-border-color2);' +
      ' border-radius: 2px;' +
      ' box-sizing: border-box;';
    input.addEventListener('focus', () => input.select());
    // Link row: the input with a copy icon embedded at its right edge
    // (browser-URL-bar style) - the auto-copy at creation time is lost as
    // soon as the user copies anything else, so the dialog must offer the
    // link again on demand.
    const linkRow = document.createElement('div');
    linkRow.style.cssText = 'position: relative; width: 100%;';
    linkRow.appendChild(input);
    const linkCopyBtn = document.createElement('button');
    linkCopyBtn.type = 'button';
    linkCopyBtn.title = 'Copy link';
    // CSS class, not inline styles - the dialog auto-applies jp-mod-styled
    // (32px line-height, min-width) to buttons present at build time; see
    // .jp-ShareFiles-copyEmbed in style/base.css
    linkCopyBtn.className = 'jp-ShareFiles-copyEmbed';
    linkCopyBtn.appendChild(this._svgNode(copyIcon.svgstr));
    linkCopyBtn.addEventListener('click', evt => {
      evt.preventDefault();
      void this._copyLinkToClipboard(link).then(ok => {
        const feedback = this._svgNode(ok ? checkIcon.svgstr : copyIcon.svgstr);
        // JupyterLab icon paths carry fill attributes (jp-icon* classes),
        // not currentColor - recolor them directly for the feedback flash
        feedback.querySelectorAll('[fill]').forEach(el => {
          el.setAttribute(
            'fill',
            ok ? 'var(--jp-success-color1)' : 'var(--jp-error-color1)'
          );
        });
        linkCopyBtn.replaceChildren(feedback);
        window.setTimeout(() => {
          linkCopyBtn.replaceChildren(this._svgNode(copyIcon.svgstr));
        }, 1200);
      });
    });
    linkRow.appendChild(linkCopyBtn);
    wrap.appendChild(linkRow);

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

    // Kind and id: given by the caller, else read off the link itself (the
    // standalone /public/<kind>/<id> form or the hub's /s/<id> form).
    const m = ref ?? linkRef(link);
    const hub = this._hubMode;

    // Password: protected resources show the password next to the link (the
    // owner hands both to the recipient) with its own copy button. Fetched
    // owner-side; appears only when one is set.
    if (m) {
      void getPassword(this._serverSettings, m.kind, m.id).then(
        res => {
          if (!res.password) {
            return;
          }
          const pwLine = statusLine('--jp-warn-color1');
          pwLine.style.fontStyle = 'normal';
          const label = document.createElement('span');
          label.textContent = 'Password:';
          pwLine.appendChild(label);
          const value = document.createElement('code');
          value.textContent = res.password;
          value.style.cssText =
            'font-family: var(--jp-code-font-family, monospace);' +
            ' color: var(--jp-ui-font-color1);' +
            ' user-select: all;';
          pwLine.appendChild(value);
          const copyBtn = document.createElement('button');
          copyBtn.type = 'button';
          copyBtn.textContent = 'Copy';
          // Plain small button - jp-Dialog-button is dialog-action sized and
          // dwarfs an inline status row.
          copyBtn.style.cssText =
            'margin-left: auto; flex: 0 0 auto; padding: 1px 8px;' +
            ' font-size: var(--jp-ui-font-size0);' +
            ' color: var(--jp-ui-font-color1);' +
            ' background: var(--jp-layout-color2);' +
            ' border: 1px solid var(--jp-border-color1);' +
            ' border-radius: 2px; cursor: pointer;';
          copyBtn.addEventListener('click', evt => {
            evt.preventDefault();
            void this._copyLinkToClipboard(res.password).then(ok => {
              copyBtn.textContent = ok ? 'Copied' : 'Copy failed';
              window.setTimeout(() => {
                copyBtn.textContent = 'Copy';
              }, 1200);
            });
          });
          pwLine.appendChild(copyBtn);
          // ordering: link, password, copied-status, reachability, QR -
          // the password sits directly below the link row itself
          wrap.insertBefore(pwLine, linkRow.nextSibling);
        },
        () => {
          /* not the owner or gone - no password line */
        }
      );
    }

    const tunnelConfigured = !!this._state.info?.tunnel_configured;
    const tunnelActive = !!this._state.info?.tunnel_active;

    // Cloudflare configured but switched off: the link is private-only -
    // say so instead of probing reachability (which only makes sense for
    // a public Cloudflare link). In hub mode the link itself tells: the
    // server restores the browser origin only for the hub's own address, so
    // a link on this page's origin works on the hub's network only - whether
    // the record is off or the hub's tunnel is not up yet. A record switched
    // on while the tunnel is still coming up says its link moves.
    const hubOnly =
      hub &&
      new URL(link, window.location.origin).origin === window.location.origin;
    if (hub ? hubOnly : tunnelConfigured && !tunnelActive) {
      const moving =
        hub &&
        !this._state.info?.tunnel_ready &&
        [...this._state.shares, ...this._state.requests].some(
          r => r.id === m?.id && r.tunnel
        );
      const offLine = statusLine('--jp-warn-color1');
      offLine.textContent = moving
        ? "Works on the hub's network only - moves to the Cloudflare hostname once the hub confirms"
        : hub
          ? "Link works on the hub's network only"
          : 'Cloudflare sharing is not running - link works on this network only';
      wrap.appendChild(offLine);
    }

    // Reachability: the server probes its own public link (a frontend fetch
    // would be blocked by CORS). Kind and id come from the link itself, so
    // every caller gets the check without extra plumbing. A spinner runs
    // while the probe is in flight, and every opening checks again. Only
    // meaningful for Cloudflare sharing - without an active tunnel there is
    // no public link to probe. In hub mode only a tunnel link is probed: the
    // lab cannot open the hub's own address (its API port redirects /s/<id>).
    if (m && (hub ? !hubOnly : tunnelConfigured && tunnelActive)) {
      const reach = statusLine('--jp-border-color2');
      reach.setAttribute('data-reach', '1');
      reach.appendChild(this._spinnerNode());
      reach.appendChild(document.createTextNode('Checking link reachability…'));
      wrap.appendChild(reach);
      void checkLink(this._serverSettings, m.kind, m.id).then(
        res => {
          reach.style.borderLeftColor = res.reachable
            ? 'var(--jp-success-color1)'
            : 'var(--jp-error-color1)';
          reach.textContent = linkCheckText(res, link);
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
    // Render to a canvas and export PNG - createDataURL() emits GIF, which
    // pastes poorly from the browser's "Copy Image"; PNG is lossless and
    // universally accepted by paste targets.
    const cell = 4;
    const margin = 8;
    const count = qr.getModuleCount();
    const size = count * cell + margin * 2;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx2d = canvas.getContext('2d')!;
    ctx2d.fillStyle = '#fff';
    ctx2d.fillRect(0, 0, size, size);
    ctx2d.fillStyle = '#000';
    for (let r = 0; r < count; r++) {
      for (let c = 0; c < count; c++) {
        if (qr.isDark(r, c)) {
          ctx2d.fillRect(margin + c * cell, margin + r * cell, cell, cell);
        }
      }
    }
    const qrImg = document.createElement('img');
    qrImg.src = canvas.toDataURL('image/png');
    qrImg.alt = 'QR code for the link';
    qrImg.style.cssText =
      'align-self: center;' +
      ' width: 180px;' +
      ' height: 180px;' +
      ' image-rendering: pixelated;' +
      ' background: #fff;' +
      ' padding: 8px;' +
      ' border-radius: 2px;';
    // Let the browser's native context menu (Copy Image etc.) work on the
    // QR code. JupyterLab's Dialog registers a capture-phase contextmenu
    // listener on its own node that calls preventDefault + stopPropagation
    // unconditionally, so a listener on the img fires too late - intercept
    // at document capture (runs before the dialog node's capture handler)
    // and stop propagation so the event reaches the browser untouched.
    const allowNativeMenu = (evt: MouseEvent) => {
      if (evt.target === qrImg) {
        evt.stopPropagation();
      }
    };
    document.addEventListener('contextmenu', allowNativeMenu, true);
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
    if (!hub && this._state.info?.tunnel_configured) {
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

    try {
      await dialog.launch();
    } finally {
      document.removeEventListener('contextmenu', allowNativeMenu, true);
    }
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
        Notification.info(`New upload to "${req.name}"`, { autoClose: 5000 });
        this._state.lastSeenUploads.set(req.id, last);
      }
    }
  }

  /**
   * Hover tooltip for a file/folder row: full name, path, size and date.
   * `includePath` is off for connected (peer) shares so a remote server's
   * workspace path is not surfaced to the recipient.
   */
  private _entryTooltip(entry: IShareEntry, includePath = true): string {
    const lines = [entry.name];
    if (includePath && entry.path) {
      lines.push('Path: ' + entry.path);
    }
    lines.push('Size: ' + this._formatSize(entry.size));
    if (typeof entry.mtime === 'number' && entry.mtime > 0) {
      lines.push('Modified: ' + new Date(entry.mtime * 1000).toLocaleString());
    }
    return lines.join('\n');
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
    focusKey: string,
    onClick: (evt: MouseEvent) => void
  ): HTMLElement {
    const btn = document.createElement('button');
    btn.className = 'jp-ShareFilesPanel-rowIconButton';
    btn.title = title;
    // names the button across a re-render, so the focus comes back to it
    btn.dataset.focusKey = focusKey;
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
   * hidden when no tunnel is configured; the accent cloud when the tunnel is
   * on (public links); dashed silhouette when off (private links); the
   * breathing accent silhouette while connecting - on and connecting carry
   * the same accent, so the glyph is what separates them where the motion is
   * suppressed. Hub mode draws the looks of `hubTunnelLook`. */
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
    if (this._hubMode) {
      const wanted = [...this._state.shares, ...this._state.requests].some(
        r => r.tunnel
      );
      this._drawHubCloud(hubTunnelLook(info, '', wanted));
      return;
    }
    const configured = !!info.tunnel_configured;
    if (!configured) {
      this._cloudIndicator.classList.remove('jp-mod-active');
      this._cloudIndicator.classList.remove('jp-mod-connecting');
      this._cloudIndicator.setAttribute('aria-pressed', 'false');
      this._cloudIndicator.innerHTML = '';
      this._cloudIndicator.appendChild(this._svgNode(cloudOffIcon.svgstr));
      this._cloudIndicator.title =
        'Cloudflare sharing not set up\nClick to set up public links';
      return;
    }
    const active = !!info?.tunnel_active;
    this._cloudIndicator.classList.toggle('jp-mod-active', active);
    this._cloudIndicator.classList.remove('jp-mod-connecting');
    this._cloudIndicator.setAttribute('aria-pressed', String(active));
    this._cloudIndicator.innerHTML = '';
    this._cloudIndicator.appendChild(
      this._svgNode(active ? cloudIcon.svgstr : cloudOffIcon.svgstr)
    );
    this._cloudIndicator.title = active
      ? 'Cloudflare sharing on - public links\nClick to switch it off'
      : 'Cloudflare sharing off - private links\nClick to switch it on';
  }

  /** Hub mode: draw one look of the header cloud icon. A group policy with
   * no tunnel has nothing to switch, so the icon is not shown at all. */
  private _drawHubCloud(look: ReturnType<typeof hubTunnelLook>): void {
    const el = this._cloudIndicator!;
    if (look.look === 'hidden') {
      el.style.display = 'none';
      return;
    }
    el.style.display = 'flex';
    // The wait for a hub tunnel runs about 90 seconds, and until now the only
    // signal at the end of it was the breathing stopping and the glyph
    // changing shape - an owner who looked away had nothing to look back at
    // (DEF-PANEL-104). One ring expands and fades when the tunnel lands.
    // Keyed on the arrival itself, `pending` to `on`, and on nothing else: a
    // panel opened with the tunnel already up has no arrival to mark, and a
    // Refresh redraws the icon through whatever look the rebuilt rows give
    // it, so a wider rule fires the cue on a click that changed nothing.
    // `animationend` takes the class off so the next arrival plays again.
    if (look.look === 'on' && this._cloudLook === 'pending') {
      el.classList.add('jp-mod-arrived');
      el.addEventListener(
        'animationend',
        () => el.classList.remove('jp-mod-arrived'),
        { once: true }
      );
    } else if (look.look !== 'on') {
      // a tunnel that drops inside the envelope leaves the icon connecting,
      // and the cue has nothing left to mark
      el.classList.remove('jp-mod-arrived');
    }
    this._cloudLook = look.look;
    el.classList.toggle('jp-mod-active', look.look === 'on');
    el.classList.toggle('jp-mod-connecting', look.look === 'pending');
    el.classList.toggle('jp-mod-armed', look.look === 'armed');
    el.classList.toggle('jp-mod-unreachable', look.look === 'unreachable');
    el.setAttribute('aria-pressed', String(look.pressed));
    el.innerHTML = '';
    const icon =
      look.look === 'on'
        ? cloudIcon
        : look.look === 'unreachable'
          ? cloudUnreachableIcon
          : cloudOffIcon;
    el.appendChild(this._svgNode(icon.svgstr));
    el.title = look.title;
  }

  /** Click on the cloud icon: switch between public links (tunnel up) and
   * private links (tunnel down). Blinks blue while connecting; in hub mode
   * also while switching off, and while the hub's tunnel is coming up. */
  private async _toggleTunnel(): Promise<void> {
    if (this._tunnelToggling) {
      return;
    }
    if (this._hubMode) {
      if (!this._state.info?.hub?.available) {
        // The switch lives on the hub; it is offered whenever the hub answers.
        Notification.warning(
          hubReasonText(this._state.info?.hub?.reason || 'hub_unavailable'),
          { autoClose: 5000 }
        );
        return;
      }
    } else if (this._state.info && !this._state.info.tunnel_configured) {
      // No tunnel yet - the icon is the entry point to configure one.
      return this._showTunnelSetupDialog();
    }
    // hub mode: a click switches off while the icon reads pressed - on, or on
    // with the hub's tunnel still coming up - and on otherwise
    const active = this._hubMode
      ? hubTunnelLook(this._state.info!, '').pressed
      : !!this._state.info?.tunnel_active;
    this._tunnelToggling = true;
    if (this._hubMode) {
      this._drawHubCloud(
        hubTunnelLook(this._state.info!, active ? 'off' : 'on')
      );
    } else {
      // stopping the connector takes up to five seconds: the icon shows the
      // switch in flight both ways
      this._cloudIndicator!.classList.remove('jp-mod-active');
      this._cloudIndicator!.classList.add('jp-mod-connecting');
      this._cloudIndicator!.innerHTML = '';
      this._cloudIndicator!.appendChild(this._svgNode(cloudOffIcon.svgstr));
      this._cloudIndicator!.title = active
        ? 'Switching Cloudflare sharing off'
        : 'Switching Cloudflare sharing on';
    }
    try {
      const state = await setTunnel(this._serverSettings, { active: !active });
      if (this._state.info) {
        this._state.info = { ...this._state.info, ...state };
      }
    } catch (err: any) {
      this._noteCloudSwitchFailure(err);
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
      // label and input are siblings; the for/id pair is what names the
      // input for a screen reader
      inp.id = `jp-ShareFilesPanel-setup-${label.replace(/\W+/g, '-').toLowerCase()}`;
      lab.htmlFor = inp.id;
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

  /**
   * The layer a row in transfer wears: an accent tint lying over the row
   * from its left edge to `fraction`, transparent enough to read the row
   * through (ACC-PROG-172). It is the only place the fraction is stated, so
   * it says it as a progressbar for a screen reader too.
   */
  private _progressOverlay(fraction: number, label: string): HTMLElement {
    const pct = Math.max(0, Math.min(100, Math.floor(100 * fraction)));
    const fill = document.createElement('div');
    fill.className = 'jp-ShareFilesPanel-itemProgress';
    fill.style.width = `${pct}%`;
    fill.setAttribute('role', 'progressbar');
    fill.setAttribute('aria-valuemin', '0');
    fill.setAttribute('aria-valuemax', '100');
    fill.setAttribute('aria-valuenow', String(pct));
    fill.setAttribute('aria-label', label);
    return fill;
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
  private _connectRow: HTMLElement | null = null;
  private _refreshBtn: HTMLElement | null = null;
  private _filterBtn: HTMLElement | null = null;
  private _cloudIndicator: HTMLElement | null = null;
  private _tunnelToggling = false;
  /** Hub mode: the look the cloud icon was last drawn in. The arrival cue
   * marks the moment the tunnel lands, so it fires on a change and not on
   * the first draw of a panel opened with the tunnel already up. */
  private _cloudLook = '';
  private _filterBox: HTMLElement | null = null;
  private _filterInput: HTMLInputElement | null = null;
  private _filterText = '';
  private _filterVisible = false;
  private _pollHandle: number | null = null;
  /** Hub mode: the change stream, open while the panel is attached. */
  private _stream: EventSource | null = null;
  /** Hub mode: a pending ring-driven refresh, so a burst costs one fetch. */
  private _refreshTimer: number | null = null;
  /** True while the server is unreachable (offline / suspended / restarting),
   * so a poll storm is logged once instead of every tick. */
  private _networkOffline = false;
  /** Last (link, reason) logged per connection, so a 15s poll does not flood
   * the console while a changed failure still gets its own line. */
  private _loggedOfflineKeys = new Map<string, string>();
  /** Manifest reads of connected records sent so far, and per connection the
   * count when its last upload was accepted - a read sent before that
   * describes the record without the upload. */
  private _reads = 0;
  private _uploadAcceptedAt = new Map<string, number>();
}
