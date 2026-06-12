/**
 * Type definitions matching the backend JSON shape.
 */

export interface IShareEntry {
  name: string;
  type: 'file' | 'directory';
  size: number;
  /** Workspace-relative path of the entry on disk (when within workspace) */
  path?: string;
}

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
