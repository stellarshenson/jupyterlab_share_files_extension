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
    response = await ServerConnection.makeRequest(requestUrl, init, serverSettings);
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
}

export function getInfo(s: ServerConnection.ISettings): Promise<IExtensionInfo> {
  return requestAPI('api/info', s);
}

// --------------------------------------------------------------------------- //
// Shares
// --------------------------------------------------------------------------- //

export function listShares(s: ServerConnection.ISettings): Promise<{ shares: IShare[] }> {
  return requestAPI('api/shares', s);
}

export function createShare(
  s: ServerConnection.ISettings,
  name: string,
  paths: string[]
): Promise<IShare> {
  return requestAPI('api/shares', s, jsonBody({ name, paths }));
}

export function getShare(s: ServerConnection.ISettings, id: string): Promise<IShare> {
  return requestAPI(`api/shares/${id}`, s);
}

export function deleteShare(s: ServerConnection.ISettings, id: string): Promise<{ ok: boolean }> {
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

export function listRequests(s: ServerConnection.ISettings): Promise<{ requests: IRequest[] }> {
  return requestAPI('api/requests', s);
}

export function createRequest(s: ServerConnection.ISettings, name: string): Promise<IRequest> {
  return requestAPI('api/requests', s, jsonBody({ name }));
}

export function getRequest(s: ServerConnection.ISettings, id: string): Promise<IRequest> {
  return requestAPI(`api/requests/${id}`, s);
}

export function deleteRequest(s: ServerConnection.ISettings, id: string): Promise<{ ok: boolean }> {
  return requestAPI(`api/requests/${id}`, s, { method: 'DELETE' });
}

export function removeRequestUpload(
  s: ServerConnection.ISettings,
  id: string,
  uploader: string,
  name: string
): Promise<IRequest> {
  const qs =
    `uploader=${encodeURIComponent(uploader)}&name=${encodeURIComponent(name)}`;
  return requestAPI(`api/requests/${id}/uploads?${qs}`, s, { method: 'DELETE' });
}

export function markRequestSeen(
  s: ServerConnection.ISettings,
  id: string
): Promise<{ ok: boolean }> {
  return requestAPI(`api/requests/${id}/seen`, s, { method: 'POST', body: '{}' });
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
  link: string
): Promise<IConnection> {
  return requestAPI('api/connections', s, jsonBody({ link }));
}

export function removeConnection(
  s: ServerConnection.ISettings,
  key: string
): Promise<{ ok: boolean }> {
  return requestAPI(`api/connections/${encodeURIComponent(key)}`, s, { method: 'DELETE' });
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

/** Fetch a remote share's manifest directly from the source server. */
export async function fetchRemoteShare(link: string): Promise<IRemoteShare> {
  const url = link.replace(/\/$/, '') + '/manifest';
  const r = await fetch(url, { credentials: 'omit' });
  if (!r.ok) {
    throw new Error(`Could not load share (status ${r.status})`);
  }
  return r.json();
}

export async function fetchRemoteRequest(link: string): Promise<IRemoteRequest> {
  const url = link.replace(/\/$/, '') + '/manifest';
  const r = await fetch(url, { credentials: 'omit' });
  if (!r.ok) {
    throw new Error(`Could not load request (status ${r.status})`);
  }
  return r.json();
}

/** Build a direct download URL for a remote share entry. */
export function remoteDownloadUrl(link: string, entryName: string): string {
  return link.replace(/\/$/, '') + '/download/' + encodeURIComponent(entryName);
}

export function remoteDownloadAllUrl(link: string): string {
  return link.replace(/\/$/, '') + '/download-all';
}
