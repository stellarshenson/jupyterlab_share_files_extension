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

/** Low-level request to the server extension's API namespace. A refusal
 * the server names with a slug carries it on the thrown error as `reason`. */
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
    const reason = data && typeof data.reason === 'string' ? data.reason : '';
    const message =
      (reason && hubReasonText(reason)) ||
      (data && data.error) ||
      (data && data.message) ||
      data;
    throw Object.assign(new ServerConnection.ResponseError(response, message), {
      reason
    });
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
  /** Hub mode: a switch-on waits for the hub to confirm its Cloudflare address. */
  tunnel_waiting?: boolean;
  /** Hub mode: why the last switch-on ended unconfirmed - the lab switched it
   * back off, or the switch back off failed (a slug, see hubReasonText); ''
   * otherwise. */
  tunnel_reason?: string;
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
  tunnel_waiting?: boolean;
  tunnel_reason?: string;
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
  /** The address the lab server opened */
  link?: string;
  /** The HTTP status the link answered */
  status?: number;
  /** Why nothing answered, in plain words */
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
      'Two groups claim file sharing on this hub - ask the administrator.',
    // the lab's own slugs: the wait for a switch-on's confirmation ran out,
    // and a switch the hub answered with an error
    cloud_not_confirmed:
      'The hub did not bring up its Cloudflare address - links stay on the hub network.',
    cloud_not_switched_off:
      'The hub did not switch Cloudflare off - those links may still be on Cloudflare.',
    cloud_not_switched_on:
      'The hub did not switch Cloudflare on - this link works on the hub network only.'
  };
  return text[slug] || slug;
}

/** The header tooltip's short form of a refusal reason - the full sentence
 * lives in hubReasonText for the toast; the tooltip line has 45 characters */
export const HUB_REASON_SHORT: Record<string, string> = {
  cloud_not_confirmed: 'Hub did not bring up its Cloudflare address',
  cloud_not_switched_on: 'The hub did not switch Cloudflare on',
  cloud_not_switched_off: 'The hub did not switch Cloudflare off',
  hub_unavailable: 'The hub could not be reached'
};

/** HUB_REASON_SHORT for a known slug, the full sentence for any other */
export function hubReasonShort(slug: string): string {
  return HUB_REASON_SHORT[slug] || hubReasonText(slug);
}

/** The link dialog's reachability line: what answered, in plain words, and
 * what the lab server opened unless it is `shown`, the link the dialog
 * already shows. */
export function linkCheckText(res: ILinkCheck, shown = ''): string {
  if (res.reachable) {
    return '✓  Link is reachable';
  }
  const got = res.status ? `HTTP ${res.status}` : res.error || 'no answer';
  const opened =
    res.link === shown ? '' : ` opened ${res.link || 'the link'} and`;
  return `✗  Link is not reachable - the lab server${opened} got ${got}`;
}

/** Hub mode: the header cloud icon's look, whether it reads pressed, and its
 * tooltip (two lines at most). `switching` is the direction of a switch
 * still in flight. A switch-on reads pressed and shows waiting, not on,
 * until the hub confirms it; one the hub never confirmed shows off with its
 * reason in the second line until the next switch-on. A click switches off
 * when the icon reads pressed, on otherwise. */
export function hubCloudLook(
  info: IExtensionInfo,
  switching: '' | 'on' | 'off'
): {
  look: 'on' | 'off' | 'waiting' | 'unreachable';
  pressed: boolean | 'mixed';
  title: string;
} {
  if (switching) {
    return {
      look: 'waiting',
      pressed: switching === 'on',
      title: `Switching Cloudflare sharing ${switching}`
    };
  }
  if (info.hub?.available && info.tunnel_waiting) {
    return {
      // mixed: the links are not public yet and may never be
      look: 'waiting',
      pressed: 'mixed',
      title:
        'Switching Cloudflare on - waiting for the hub\nClick to end the wait and switch it off'
    };
  }
  if (!info.hub?.available) {
    return {
      look: 'unreachable',
      pressed: false,
      title: 'Hub unavailable\nCloudflare state unknown'
    };
  }
  if (info.tunnel_reason && !info.tunnel_active) {
    return {
      look: 'off',
      pressed: false,
      title: `Cloudflare not confirmed - click to switch on\n${hubReasonShort(info.tunnel_reason)}`
    };
  }
  if (info.tunnel_active) {
    return {
      look: 'on',
      pressed: true,
      title: info.tunnel_reason
        ? `Cloudflare sharing on - click to switch off\n${hubReasonShort(info.tunnel_reason)}`
        : 'Cloudflare sharing on\nClick to switch it off'
    };
  }
  return {
    look: 'off',
    pressed: false,
    title: 'Cloudflare sharing off - hub network only\nClick to switch it on'
  };
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
// Connected peers, read by our own server (api/connections/<key>/...)
// --------------------------------------------------------------------------- //

/** Explain why a peer refresh failed, for the panel's offline badge.
 *
 * The peer is read by our own server (`fetchConnectionManifest`), so the
 * error is either that server's answer - a sentence that already names the
 * peer's fault, reported as fact - or a bare `TypeError`, which `fetch`
 * throws for every transport failure and here can only mean our own server
 * did not answer: nothing is known about the peer until it is back.
 */
export function offlineReason(err: any): string {
  const message = typeof err?.message === 'string' ? err.message.trim() : '';
  const raw =
    message && message !== '[object Object]' && message !== 'undefined'
      ? message
      : '';
  if (err instanceof TypeError) {
    return `${raw || 'the request failed'} - this lab's own server did not answer, so nothing is known about the peer until it is back.`;
  }
  return raw || 'the request failed';
}

/** A connected peer's manifest, read by our server
 * (`api/connections/<key>/manifest`): same-origin for the browser, so a
 * Content-Security-Policy or CORS rule cannot stop it, and the stored
 * password unlocks a protected peer on the server. Never served from a
 * cache: ServerConnection sends `no-store` on every request and the server
 * answers with it, so each poll reads what the peer holds now. */
export async function fetchConnectionManifest(
  serverSettings: ServerConnection.ISettings,
  key: string
): Promise<IRemoteShare | IRemoteRequest> {
  return requestAPI<IRemoteShare | IRemoteRequest>(
    `api/connections/${encodeURIComponent(key)}/manifest`,
    serverSettings
  );
}

/** Same-origin download URL for one entry of a connected share - our server
 * fetches it from the peer (`api/connections/<key>/download`). */
export function connectionDownloadUrl(
  serverSettings: ServerConnection.ISettings,
  key: string,
  entryName: string
): string {
  return (
    URLExt.join(
      serverSettings.baseUrl,
      NAMESPACE,
      'api',
      'connections',
      encodeURIComponent(key),
      'download'
    ) + `?name=${encodeURIComponent(entryName)}`
  );
}
