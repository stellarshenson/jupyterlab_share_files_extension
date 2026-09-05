/**
 * Typed API client for the share-files server extension.
 */

import { URLExt } from '@jupyterlab/coreutils';
import { ServerConnection } from '@jupyterlab/services';

import {
  IConnection,
  IRemoteRequest,
  IRemoteShare,
  IRequest,
  IShare
} from './types';

const NAMESPACE = 'jupyterlab-share-files-extension';

/** Low-level request to the server extension's API namespace. */
async function requestAPI<T>(
  endPoint: string,
  serverSettings: ServerConnection.ISettings,
  init: RequestInit = {}
): Promise<T> {
  const requestUrl = URLExt.join(serverSettings.baseUrl, NAMESPACE, endPoint);
  let response: Response;
  try {
    response = await ServerConnection.makeRequest(
      requestUrl,
      init,
      serverSettings
    );
  } catch (error) {
    throw new ServerConnection.NetworkError(error as any);
  }
  let data: any = await response.text();
  if (data.length > 0) {
    try {
      data = JSON.parse(data);
    } catch {
      // not JSON
    }
  }
  if (!response.ok) {
    const message =
      (data && typeof data.reason === 'string' && hubReasonText(data.reason)) ||
      (data && data.error) ||
      (data && data.message) ||
      data;
    throw new ServerConnection.ResponseError(response, message);
  }
  return data as T;
}

function jsonBody(body: any): RequestInit {
  return {
    method: 'POST',
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' }
  };
}

// --------------------------------------------------------------------------- //
// Info
// --------------------------------------------------------------------------- //

/** Hub mode: what the hub allows this user, from GET capabilities. */
export interface IHubInfo {
  /** The hub answered; false carries reason and message instead. */
  available: boolean;
  allow_share?: boolean;
  allow_request?: boolean;
  /** Refusal slug (closed set, see hubReasonText) - '' when nothing refuses */
  reason?: string;
  message?: string;
  /** The hub's fileshare app is serving recipients */
  serving?: boolean;
  /** The group policy requires a password on every share and request */
  password_required?: boolean;
  max_share_bytes?: number | null;
  max_upload_bytes?: number | null;
  max_shares?: number | null;
  retention_days?: number | null;
}

export interface IExtensionInfo {
  storage_path: string;
  shares_subdir: string;
  requests_subdir: string;
  /** 'hub' when the lab was spawned by galaxahub with SHARE_FILES_PUBLIC_ZONE=hub;
   * absent or 'standalone' otherwise. */
  mode?: 'standalone' | 'hub';
  /** Hub mode only */
  hub?: IHubInfo;
  /** Configured external origin (e.g. a Cloudflare tunnel host) links are
   * rewritten to; empty when none is configured OR the tunnel is off. */
  public_base_url?: string;
  /** A tunnel is set up (cloudflare setup ran), regardless of the toggle. */
  tunnel_configured?: boolean;
  /** Tunnel toggle: public links when true, private when false. */
  tunnel_active?: boolean;
  /** Server brings the tunnel up at startup. */
  tunnel_autostart?: boolean;
  /** The cloudflared daemon process is running. */
  tunnel_running?: boolean;
}

export function getInfo(
  s: ServerConnection.ISettings
): Promise<IExtensionInfo> {
  return requestAPI('api/info', s);
}

export interface ITunnelState {
  tunnel_configured: boolean;
  tunnel_active: boolean;
  tunnel_autostart: boolean;
  tunnel_running: boolean;
}

/** Toggle the Cloudflare tunnel (active: public vs private links) or
 * persist the autostart preference. Returns the new state. In hub mode
 * `active` flips the Cloudflare switch on every share and request and
 * sets the default for the next one. */
export function setTunnel(
  s: ServerConnection.ISettings,
  body: { active?: boolean; autostart?: boolean }
): Promise<ITunnelState> {
  return requestAPI('api/tunnel', s, jsonBody(body));
}

/** Hub mode: one record's Cloudflare switch. The hub refuses a switch on
 * with `cloud_not_configured` while the group policy has Cloudflare off. */
export function setCloud(
  s: ServerConnection.ISettings,
  kind: 'share' | 'request',
  id: string,
  cloud: boolean
): Promise<{ id: string; cloud: boolean }> {
  const plural = kind === 'share' ? 'shares' : 'requests';
  return requestAPI(`api/${plural}/${id}/cloud`, s, jsonBody({ cloud }));
}

/** Hub mode: the panel's change stream (Server-Sent Events). The browser
 * cannot set headers on an EventSource, so the token rides the query
 * string when the server settings say it must. */
export function streamUrl(s: ServerConnection.ISettings): string {
  const url = URLExt.join(s.baseUrl, NAMESPACE, 'api', 'stream');
  return s.appendToken && s.token
    ? `${url}?token=${encodeURIComponent(s.token)}`
    : url;
}

/** Provision Cloudflare sharing from the panel - same inputs and sequence
 * as `cloudflare setup` (token, account id, hostname, private_base_url). */
export function setupTunnel(
  s: ServerConnection.ISettings,
  body: {
    token: string;
    account_id: string;
    hostname: string;
    private_base_url: string;
  }
): Promise<ITunnelState> {
  return requestAPI('api/tunnel/setup', s, jsonBody(body));
}

/** Reset Cloudflare sharing - same as `cloudflare reset`: credentials,
 * tunnel state and base URLs cleared; Cloudflare-side resources kept. */
export function resetTunnel(
  s: ServerConnection.ISettings
): Promise<ITunnelState & { reset: string[] }> {
  return requestAPI('api/tunnel/reset', s, jsonBody({}));
}

export interface ILinkCheck {
  reachable: boolean;
  status?: number;
  error?: string;
}

/** Server-side probe of a generated public link - the server fetches its
 * own link (through the Cloudflare edge when configured) and reports
 * whether it answers. A frontend fetch would be blocked by CORS. */
export function checkLink(
  s: ServerConnection.ISettings,
  kind: 'share' | 'request',
  id: string
): Promise<ILinkCheck> {
  return requestAPI(`api/link-check?kind=${kind}&id=${id}`, s);
}

// --------------------------------------------------------------------------- //
// Shares
// --------------------------------------------------------------------------- //

export function listShares(
  s: ServerConnection.ISettings
): Promise<{ shares: IShare[] }> {
  return requestAPI('api/shares', s);
}

export function createShare(
  s: ServerConnection.ISettings,
  name: string,
  paths: string[],
  password = ''
): Promise<IShare> {
  return requestAPI('api/shares', s, jsonBody({ name, paths, password }));
}

export function getShare(
  s: ServerConnection.ISettings,
  id: string
): Promise<IShare> {
  return requestAPI(`api/shares/${id}`, s);
}

export function deleteShare(
  s: ServerConnection.ISettings,
  id: string
): Promise<{ ok: boolean }> {
  return requestAPI(`api/shares/${id}`, s, { method: 'DELETE' });
}

export function addShareItems(
  s: ServerConnection.ISettings,
  id: string,
  paths: string[]
): Promise<IShare> {
  return requestAPI(`api/shares/${id}/items`, s, jsonBody({ paths }));
}

export function removeShareItems(
  s: ServerConnection.ISettings,
  id: string,
  names: string[]
): Promise<IShare> {
  // Names go in query params - DELETE bodies are unreliable through proxies
  const qs = names.map(n => `name=${encodeURIComponent(n)}`).join('&');
  return requestAPI(`api/shares/${id}/items?${qs}`, s, { method: 'DELETE' });
}

// --------------------------------------------------------------------------- //
// Requests
// --------------------------------------------------------------------------- //

export function listRequests(
  s: ServerConnection.ISettings
): Promise<{ requests: IRequest[] }> {
  return requestAPI('api/requests', s);
}

export function createRequest(
  s: ServerConnection.ISettings,
  name: string,
  password = ''
): Promise<IRequest> {
  return requestAPI('api/requests', s, jsonBody({ name, password }));
}

export function getRequest(
  s: ServerConnection.ISettings,
  id: string
): Promise<IRequest> {
  return requestAPI(`api/requests/${id}`, s);
}

export function deleteRequest(
  s: ServerConnection.ISettings,
  id: string
): Promise<{ ok: boolean }> {
  return requestAPI(`api/requests/${id}`, s, { method: 'DELETE' });
}

export function removeRequestUpload(
  s: ServerConnection.ISettings,
  id: string,
  uploader: string,
  name: string
): Promise<IRequest> {
  const qs = `uploader=${encodeURIComponent(uploader)}&name=${encodeURIComponent(name)}`;
  return requestAPI(`api/requests/${id}/uploads?${qs}`, s, {
    method: 'DELETE'
  });
}

/** Hub mode: copy one recipient upload into the workspace through the
 * hub's transfer job. `targetDir` is the folder the panel names (the file
 * browser's current directory); the server picks a fresh directory under it
 * named after `name`, and answers with the landed path. */
export function fetchRequestUpload(
  s: ServerConnection.ISettings,
  id: string,
  uploadId: string,
  targetDir: string,
  name: string
): Promise<{ ok: boolean; path: string }> {
  return requestAPI(
    `api/requests/${id}/uploads/${encodeURIComponent(uploadId)}/fetch`,
    s,
    jsonBody({ target_dir: targetDir, name })
  );
}

/** The hub's refusal slugs (a closed set, `fileshare/reasons.py` on the
 * hub) in plain words. An unknown slug is returned as-is. */
export function hubReasonText(slug: string): string {
  const text: Record<string, string> = {
    not_granted: 'Your group does not grant file sharing on this hub.',
    share_not_granted: 'Your group does not allow sharing files out.',
    request_not_granted: 'Your group does not allow requesting files.',
    downloads_blocked:
      'Your group blocks file downloads, so files cannot be shared out.',
    hub_unavailable: 'The hub could not be reached.',
    no_capacity: 'You have reached the number of shares your group allows.',
    volume_watermark: 'The hub has no free space for new shares.',
    sidecar_image_absent:
      'The hub has no file sharing service image - ask the administrator.',
    sidecar_not_serving: 'The hub file sharing service is not serving yet.',
    tunnel_not_ready: "The hub's Cloudflare tunnel is not connected yet.",
    busy: 'The hub is busy copying other files - try again shortly.',
    source_unreadable: 'The hub could not read the files from your workspace.',
    grant_revoked: 'The grant this share was created under was revoked.',
    expired: 'The retention period has passed.',
    over_cap: 'The files are larger than your group allows for one share.',
    bad_filename: 'A file name was rejected by the hub.',
    password_required:
      'Your group requires a password on every share and request.',
    cloud_not_configured:
      'Your group policy has Cloudflare turned off - links work on the hub network only.',
    policy_conflict:
      'Two groups claim file sharing on this hub - ask the administrator.'
  };
  return text[slug] || slug;
}

/** Kind and id from a share or request link: the standalone
 * `/public/<kind>/<id>` form, or the hub's `/s/<id>` form where a request id
 * carries the `r_` prefix. Null for anything else. */
export function linkRef(
  link: string
): { kind: 'share' | 'request'; id: string } | null {
  const own = link.match(/\/public\/(share|request)\/([A-Z2-7]{6,16})$/);
  if (own) {
    return { kind: own[1] as 'share' | 'request', id: own[2] };
  }
  const hub = link.match(/\/s\/([A-Za-z0-9_-]{6,64})$/);
  if (hub) {
    return { kind: hub[1].startsWith('r_') ? 'request' : 'share', id: hub[1] };
  }
  return null;
}

export function markRequestSeen(
  s: ServerConnection.ISettings,
  id: string
): Promise<{ ok: boolean }> {
  return requestAPI(`api/requests/${id}/seen`, s, {
    method: 'POST',
    body: '{}'
  });
}

// --------------------------------------------------------------------------- //
// Passwords
// --------------------------------------------------------------------------- //

/** Owner-side: read the stored plaintext password ('' when none is set). */
export function getPassword(
  s: ServerConnection.ISettings,
  kind: 'share' | 'request',
  id: string
): Promise<{ id: string; password: string }> {
  const plural = kind === 'share' ? 'shares' : 'requests';
  return requestAPI(`api/${plural}/${id}/password`, s);
}

/** Owner-side: set, change, or clear (empty string) the password. */
export function setPassword(
  s: ServerConnection.ISettings,
  kind: 'share' | 'request',
  id: string,
  password: string
): Promise<IShare | IRequest> {
  const plural = kind === 'share' ? 'shares' : 'requests';
  return requestAPI(`api/${plural}/${id}/password`, s, jsonBody({ password }));
}

/** Generate an xkcd-style passphrase (server-side, via xkcdpass). */
export function generatePassword(
  s: ServerConnection.ISettings
): Promise<{ password: string }> {
  return requestAPI('api/generate-password', s);
}

/** Public: trade a password for an unlock token on a remote (or own) link.
 * Throws on a wrong password (401) or rate limit (429). */
export async function unlockRemote(
  link: string,
  password: string
): Promise<string> {
  const url = link.replace(/\/$/, '') + '/unlock';
  const r = await fetch(url, {
    method: 'POST',
    credentials: 'omit',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password })
  });
  if (r.status === 429) {
    throw new Error('Too many password attempts - wait before retrying');
  }
  if (!r.ok) {
    throw new Error('Wrong password');
  }
  const data = await r.json();
  return data.token || '';
}

// --------------------------------------------------------------------------- //
// Connections
// --------------------------------------------------------------------------- //

export function listConnections(
  s: ServerConnection.ISettings
): Promise<{ connections: IConnection[] }> {
  return requestAPI('api/connections', s);
}

export function addConnection(
  s: ServerConnection.ISettings,
  link: string,
  password = ''
): Promise<IConnection> {
  return requestAPI('api/connections', s, jsonBody({ link, password }));
}

export function removeConnection(
  s: ServerConnection.ISettings,
  key: string
): Promise<{ ok: boolean }> {
  return requestAPI(`api/connections/${encodeURIComponent(key)}`, s, {
    method: 'DELETE'
  });
}

/**
 * Save items from a connected share into the user's workspace.
 *
 * @param names - undefined or null means "save all" (downloads the whole share
 *   into a folder named after the share). Otherwise a list of entry names from
 *   the remote share's top level.
 */
export function saveFromConnection(
  s: ServerConnection.ISettings,
  key: string,
  targetDir: string,
  names: string[] | null
): Promise<{ ok: boolean; saved: string[] }> {
  const body: any = { target_dir: targetDir };
  if (names !== null) {
    body.names = names;
  }
  return requestAPI(
    `api/connections/${encodeURIComponent(key)}/save`,
    s,
    jsonBody(body)
  );
}

/**
 * Upload local items into a connected request.
 */
export function uploadToConnection(
  s: ServerConnection.ISettings,
  key: string,
  paths: string[],
  uploader: string
): Promise<{ ok: boolean; uploaded: string[] }> {
  return requestAPI(
    `api/connections/${encodeURIComponent(key)}/upload`,
    s,
    jsonBody({ paths, uploader })
  );
}

// --------------------------------------------------------------------------- //
// Cross-peer (direct fetch of remote manifests / downloads / uploads)
// --------------------------------------------------------------------------- //

/** A peer's manifest changes whenever they add or remove a file, and an older
 * peer serves it with an ETag and no `Cache-Control`, so the browser may reuse
 * a stored copy under heuristic freshness - the panel would then show a file
 * list that silently omits a file the peer has already shared. `no-store`
 * keeps every poll a real network read. (This is about staleness, not about a
 * 304: the browser consumes the 304 its own cache solicited and resolves the
 * fetch with the stored 200.) Matches @jupyterlab/services, which already
 * sends `cache: 'no-store'` on every ServerConnection request. */
const MANIFEST_FETCH: RequestInit = { credentials: 'omit', cache: 'no-store' };

/** Explain why a cross-peer fetch failed, for the panel's offline badge.
 *
 * A raw `fetch` rejects with a bare `TypeError` for every transport-level
 * failure, so the type alone cannot name a cause: the peer's server being
 * stopped, this machine being offline, a blocked origin and a malformed link
 * are indistinguishable here. List the candidates rather than assert one - a
 * confidently wrong reason sends the next person to the wrong subsystem,
 * which is worse than no reason. The stopped-server case leads because a
 * share link is served BY the owner's single-user server, JupyterHub stops
 * idle servers, and the hub's reply carries no CORS headers so the browser
 * never lets us read its status.
 *
 * `localOffline` is the panel's own view of this machine's connectivity: when
 * our own server is unreachable too, the peer is not the suspect.
 *
 * An HTTP status did get through, so it is reported as fact.
 */
export function offlineReason(err: any, localOffline = false): string {
  const message = typeof err?.message === 'string' ? err.message.trim() : '';
  const raw =
    message && message !== '[object Object]' && message !== 'undefined'
      ? message
      : '';
  if (err instanceof TypeError) {
    if (localOffline) {
      return `${raw || 'the request failed'} - this machine cannot reach its own server either, so the fault is most likely local connectivity.`;
    }
    return (
      `${raw || 'the request failed'} - the browser could not complete the ` +
      "request. Most often the peer's server is stopped (JupyterHub stops " +
      "idle servers, and a share link only works while its owner's server " +
      'is running); it can also be this machine being offline, or the link ' +
      'being blocked or malformed.'
    );
  }
  if (/\b401\b/.test(raw)) {
    return `${raw} - the peer rejected the stored password; reconnect the link with the current one.`;
  }
  if (/\b404\b/.test(raw)) {
    return `${raw} - the owner has removed this share or request.`;
  }
  return raw;
}

/** Fetch a remote share's manifest directly from the source server.
 * `token` is the unlock token for password-protected resources. */
export async function fetchRemoteShare(
  link: string,
  token = ''
): Promise<IRemoteShare> {
  const url = link.replace(/\/$/, '') + '/manifest';
  const r = await fetch(url, {
    ...MANIFEST_FETCH,
    headers: token ? { 'X-Share-Token': token } : undefined
  });
  if (!r.ok) {
    throw new Error(`Could not load share (status ${r.status})`);
  }
  return r.json();
}

export async function fetchRemoteRequest(
  link: string,
  token = ''
): Promise<IRemoteRequest> {
  const url = link.replace(/\/$/, '') + '/manifest';
  const r = await fetch(url, {
    ...MANIFEST_FETCH,
    headers: token ? { 'X-Share-Token': token } : undefined
  });
  if (!r.ok) {
    throw new Error(`Could not load request (status ${r.status})`);
  }
  return r.json();
}

/** Build a direct download URL for a remote share entry. `token` (unlock
 * token) rides as `?t=` - download links cannot carry headers. */
export function remoteDownloadUrl(
  link: string,
  entryName: string,
  token = ''
): string {
  const base =
    link.replace(/\/$/, '') + '/download/' + encodeURIComponent(entryName);
  return token ? base + '?t=' + encodeURIComponent(token) : base;
}

export function remoteDownloadAllUrl(link: string, token = ''): string {
  const base = link.replace(/\/$/, '') + '/download-all';
  return token ? base + '?t=' + encodeURIComponent(token) : base;
}
