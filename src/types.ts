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
  /** Hub mode: the record's Cloudflare switch - the link is the hub's
   * Cloudflare address while on, the hub's own address while off */
  cloud?: boolean;
  /** Hub mode, on a freshly created row only: why the cloud toggle could
   * not be applied to it (a refusal slug) */
  cloud_reason?: string;
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
  cloud?: boolean;
  cloud_reason?: string;
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

/** Remote request manifest as returned by /public/request/<id>/manifest */
export interface IRemoteRequest {
  id: string;
  name: string;
  slug: string;
  kind: 'request';
  created_at: number;
  link: string;
}
