/**
 * Type definitions matching the backend JSON shape.
 */

export interface IShareEntry {
  name: string;
  type: 'file' | 'directory';
  size: number;
  /** Workspace-relative path of the entry on disk (when within workspace) */
  path?: string;
  /** Filesystem modification time in unix seconds (for the hover tooltip) */
  mtime?: number;
  /** Hub mode: the hub's id of a recipient upload, used to fetch it */
  upload_id?: string;
}

/** Hub mode: where a share's bytes are. Standalone rows carry no state. */
export type IShareState = 'staging' | 'ready' | 'refused';

export interface IShare {
  id: string;
  name: string;
  slug: string;
  kind: 'share';
  created_at: number;
  entries: IShareEntry[];
  link: string;
  /** Workspace-relative path of the share's data directory */
  path?: string;
  /** Public access requires a password */
  has_password?: boolean;
  /** Hub mode: staging while the hub copies, ready, or refused */
  state?: IShareState;
  /** Hub mode: the refusal slug when state is refused */
  reason?: string;
  /** Hub mode: unix seconds the hub removes the row at */
  expires_at?: number;
  /** Hub mode: the record's tunnel switch - the link is the hub's
   * Cloudflare address while on, the hub's own address while off */
  tunnel?: boolean;
  /** Hub mode, on a freshly created row only: why the tunnel switch could
   * not be applied to it (a refusal slug) */
  tunnel_reason?: string;
  /** Hub mode: the hub is copying an add (its `last_add` reads running) */
  adding?: boolean;
  /** Hub mode: entries the hub left out of this share, create and adds */
  skipped?: number;
  /** Hub mode: the reason slug of the last add, when the hub refused it */
  add_reason?: string;
  /** Hub mode: bytes copied and to copy, only while the hub carries bytes */
  progress?: { copied: number; total: number } | null;
}

export interface IUploaderEntry {
  /** Server-issued identity hash - the stable key for this uploader's pool */
  hash: string;
  /** Display label the uploader typed; many uploaders may share a name */
  name: string;
  entries: IShareEntry[];
}

export interface IRequest {
  id: string;
  name: string;
  slug: string;
  kind: 'request';
  created_at: number;
  upload_count: number;
  last_upload_at: number;
  last_seen_upload_at: number;
  uploaders: IUploaderEntry[];
  link: string;
  /** Workspace-relative path of the request's uploads directory */
  path?: string;
  /** Public access requires a password */
  has_password?: boolean;
  /** Hub mode: ready, or refused with a reason */
  state?: IShareState;
  reason?: string;
  expires_at?: number;
  tunnel?: boolean;
  tunnel_reason?: string;
}

export interface IConnection {
  key: string;
  kind: 'share' | 'request';
  id: string;
  host: string;
  name: string;
  owner: string;
  added_at: number;
  link?: string;
  /** Stored password for a protected remote resource (owner-side only) */
  password?: string;
  /** PEM of the certificate the user trusted for this connection */
  certificate?: string;
}

/** A peer certificate the system does not trust, as the trust dialog shows
 * it before the user decides. */
export interface ICertificate {
  /** host[:port] of the link */
  host: string;
  /** SHA-256, colon-separated hex */
  fingerprint: string;
  /** why the system's check failed, in OpenSSL's words */
  reason: string;
}

/** Remote share manifest as returned by /public/share/<id>/manifest */
export interface IRemoteShare {
  id: string;
  name: string;
  slug: string;
  kind: 'share';
  created_at: number;
  entries: IShareEntry[];
  link: string;
}

/** Remote request manifest as returned by /public/request/<id>/manifest.
 * On a hub the manifest also follows this user's own upload into the
 * request: running with the bytes this lab has sent, then landed or
 * refused. */
export interface IRemoteRequest {
  id: string;
  name: string;
  slug: string;
  kind: 'request';
  created_at: number;
  link: string;
  uploading?: boolean;
  uploaded?: number;
  upload_reason?: string;
  progress?: { copied: number; total: number } | null;
}
