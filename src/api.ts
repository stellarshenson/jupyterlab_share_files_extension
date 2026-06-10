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
    const message = (data && data.error) || (data && data.message) || data;
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

export interface IExtensionInfo {
  storage_path: string;
  shares_subdir: string;
  requests_subdir: string;
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
 * persist the autostart preference. Returns the new state. */
export function setTunnel(
  s: ServerConnection.ISettings,
  body: { active?: boolean; autostart?: boolean }
): Promise<ITunnelState> {
  return requestAPI('api/tunnel', s, jsonBody(body));
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
  link: string;
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

/** Fetch a remote share's manifest directly from the source server.
 * `token` is the unlock token for password-protected resources. */
export async function fetchRemoteShare(
  link: string,
  token = ''
): Promise<IRemoteShare> {
  const url = link.replace(/\/$/, '') + '/manifest';
  const r = await fetch(url, {
    credentials: 'omit',
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
    credentials: 'omit',
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
