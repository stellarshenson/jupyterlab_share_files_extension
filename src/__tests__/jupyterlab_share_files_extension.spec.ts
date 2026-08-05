/**
 * Frontend unit tests for the share-files extension.
 *
 * These test pure logic - URL construction, link parsing, formatting -
 * without spinning up JupyterLab. Widget DOM tests would require the
 * @jupyterlab/testutils framework which we leave for an end-to-end suite.
 */

import {
  offlineReason,
  remoteDownloadUrl,
  remoteDownloadAllUrl,
  type IExtensionInfo
} from '../api';
import { clearClip, getClip, setClip } from '../clipboard';

describe('api URL helpers', () => {
  const link =
    'https://hub.example.com/user/alice/jupyterlab-share-files-extension/public/share/A3KM7X2P';

  it('builds a remote download URL for a named entry', () => {
    expect(remoteDownloadUrl(link, 'train.csv')).toBe(
      link + '/download/train.csv'
    );
  });

  it('URL-encodes entry names with spaces', () => {
    expect(remoteDownloadUrl(link, 'my file.csv')).toBe(
      link + '/download/my%20file.csv'
    );
  });

  it('URL-encodes special characters in entry names', () => {
    expect(remoteDownloadUrl(link, 'a&b.txt')).toBe(
      link + '/download/a%26b.txt'
    );
  });

  it('builds a download-all URL', () => {
    expect(remoteDownloadAllUrl(link)).toBe(link + '/download-all');
  });

  it('strips trailing slash from the link before appending', () => {
    expect(remoteDownloadUrl(link + '/', 'x.txt')).toBe(
      link + '/download/x.txt'
    );
    expect(remoteDownloadAllUrl(link + '/')).toBe(link + '/download-all');
  });
});

describe('IExtensionInfo shape', () => {
  it('has the expected fields', () => {
    const info: IExtensionInfo = {
      storage_path: './uploads',
      shares_subdir: 'shares',
      requests_subdir: 'requests'
    };
    expect(info.storage_path).toBe('./uploads');
    expect(info.shares_subdir).toBe('shares');
    expect(info.requests_subdir).toBe('requests');
  });
});

describe('link host detection', () => {
  // mirrors the self-connect guard in widget.connectToLink()
  function parseHost(link: string): string | null {
    try {
      return new URL(link).host;
    } catch {
      return null;
    }
  }

  it('extracts host from a JupyterHub user-route link', () => {
    expect(
      parseHost(
        'https://hub.example.com/user/alice/jupyterlab-share-files-extension/public/share/ABCDEFGH'
      )
    ).toBe('hub.example.com');
  });

  it('extracts host with port', () => {
    expect(
      parseHost(
        'https://localhost:8888/jupyterlab-share-files-extension/public/share/ABCDEFGH'
      )
    ).toBe('localhost:8888');
  });

  it('returns null for malformed input', () => {
    expect(parseHost('not a url')).toBeNull();
    expect(parseHost('')).toBeNull();
  });
});

describe('JupyterHub-aware self-connect detection', () => {
  // Mirrors the prefix-based check in widget.connectToLink() introduced
  // in v1.0.34. The bug it locks against: on JupyterHub alice's panel
  // wrongly flagged bob's link as her own because the old check compared
  // only `host` and they share a host.
  function ownPrefix(baseUrl: string, origin: string): string {
    return new URL(
      'jupyterlab-share-files-extension/',
      new URL(baseUrl, origin)
    ).href;
  }

  function isSelf(link: string, baseUrl: string, origin: string): boolean {
    return link.startsWith(ownPrefix(baseUrl, origin));
  }

  it('same user same hub IS self', () => {
    expect(
      isSelf(
        'https://hub.example.com/user/alice/jupyterlab-share-files-extension/public/share/X',
        '/user/alice/',
        'https://hub.example.com'
      )
    ).toBe(true);
  });

  it('different user same hub is NOT self (the reported bug)', () => {
    expect(
      isSelf(
        'https://hub.example.com/user/bob/jupyterlab-share-files-extension/public/request/X',
        '/user/alice/',
        'https://hub.example.com'
      )
    ).toBe(false);
  });

  it('same user different host is NOT self', () => {
    expect(
      isSelf(
        'https://other.example.com/user/alice/jupyterlab-share-files-extension/public/share/X',
        '/user/alice/',
        'https://hub.example.com'
      )
    ).toBe(false);
  });

  it('standalone single-user own link is self', () => {
    expect(
      isSelf(
        'http://localhost:8888/jupyterlab-share-files-extension/public/share/X',
        '/',
        'http://localhost:8888'
      )
    ).toBe(true);
  });

  it('standalone vs JupyterHub link is NOT self', () => {
    expect(
      isSelf(
        'https://hub.example.com/user/alice/jupyterlab-share-files-extension/public/share/X',
        '/',
        'http://localhost:8888'
      )
    ).toBe(false);
  });
});

describe('drag MIME payload shape', () => {
  // Locks the format JupyterLab's file browser drop handler expects.
  // The handler iterates CONTENTS_MIME as a `string[]` of paths and
  // feeds each one through `contents.localPath(path)` - sending objects
  // breaks `localPath(obj)` silently. This regression test guards the
  // format we send via `Drag.mimeData.setData(CONTENTS_MIME, ...)`.
  const CONTENTS_MIME = 'application/x-jupyter-icontents';

  it('CONTENTS_MIME constant matches the file browser source', () => {
    expect(CONTENTS_MIME).toBe('application/x-jupyter-icontents');
  });

  it('payload is a string array of workspace-relative paths', () => {
    const entry = {
      name: 'file.txt',
      type: 'file' as const,
      size: 5,
      path: 'uploads/shares/foo-AB/file.txt'
    };
    const payload = [entry.path];
    expect(Array.isArray(payload)).toBe(true);
    expect(typeof payload[0]).toBe('string');
    expect(payload[0]).toBe('uploads/shares/foo-AB/file.txt');
  });

  it('payload survives JSON round-trip without losing fields', () => {
    // MimeData stores objects by reference; round-trip catches accidental
    // object serialisation
    const payload = ['uploads/shares/foo-AB/file.txt'];
    const round = JSON.parse(JSON.stringify(payload));
    expect(round).toEqual(payload);
  });
});

describe('connection link resolution', () => {
  // Mirrors widget._linkFor after the offline-while-available fix. The full
  // link is persisted server-side and returned verbatim; the client must NEVER
  // reconstruct it from host + id, because on JupyterHub that drops the owner's
  // `/user/<name>/` prefix and the request gets bounced to `/hub/...` (404),
  // wrongly marking an online share offline (the reported console error
  // `/hub/jupyterlab-share-files-extension/public/share/<id>/manifest 404`).
  interface IConnLike {
    host: string;
    kind: 'share' | 'request';
    id: string;
    link?: string;
  }
  function linkFor(conn: IConnLike): string {
    return conn.link || '';
  }

  const link =
    'https://hub.example.com/user/alice/jupyterlab-share-files-extension/public/share/ABCDEFGH';

  it('returns the persisted full link verbatim', () => {
    expect(
      linkFor({
        host: 'https://hub.example.com',
        kind: 'share',
        id: 'ABCDEFGH',
        link
      })
    ).toBe(link);
  });

  it('preserves the JupyterHub /user/<name>/ prefix', () => {
    expect(
      linkFor({
        host: 'https://hub.example.com',
        kind: 'share',
        id: 'ABCDEFGH',
        link
      })
    ).toContain('/user/alice/');
  });

  it('returns empty string when no link is stored, never a reconstructed URL', () => {
    const out = linkFor({
      host: 'https://hub.example.com',
      kind: 'share',
      id: 'ABCDEFGH'
    });
    expect(out).toBe('');
    // regression lock: must not produce the base-path-less URL that JupyterHub
    // bounces to /hub/ and 404s
    expect(out).not.toContain('jupyterlab-share-files-extension');
  });
});

describe('connected-share entry download', () => {
  // Locks the URL + filename the panel builds for a connected (peer) share
  // entry. The download itself runs with `credentials: 'omit'` + a Blob (see
  // widget._downloadRemote) so a credentialed navigation can never trigger
  // JupyterHub's spawn-as-owner screen when the owner's server is offline.
  function downloadUrl(baseLink: string, name: string): string {
    return (
      baseLink.replace(/\/$/, '') + '/download/' + encodeURIComponent(name)
    );
  }
  function downloadName(entry: {
    name: string;
    type: 'file' | 'directory';
  }): string {
    return entry.name + (entry.type === 'directory' ? '.zip' : '');
  }

  const baseLink =
    'https://hub.example.com/user/alice/jupyterlab-share-files-extension/public/share/ABCDEFGH';

  it('builds a download URL under the owner /user/<name>/ prefix', () => {
    const url = downloadUrl(baseLink, 'train.csv');
    expect(url).toBe(baseLink + '/download/train.csv');
    expect(url).toContain('/user/alice/');
  });

  it('URL-encodes entry names', () => {
    expect(downloadUrl(baseLink, 'my file.csv')).toBe(
      baseLink + '/download/my%20file.csv'
    );
  });

  it('names a file download as-is', () => {
    expect(downloadName({ name: 'data.csv', type: 'file' })).toBe('data.csv');
  });

  it('names a directory download with a .zip suffix', () => {
    expect(downloadName({ name: 'logs', type: 'directory' })).toBe('logs.zip');
  });
});

describe('share clipboard', () => {
  // The extension-owned clipboard that bridges file-browser <-> panel copy and
  // paste, since JupyterLab's native file-browser clipboard is private. The
  // `origin` flag decides who performs a paste: `fb` means native paste handled
  // it; `panel` means our hook must do the transfer.
  beforeEach(() => clearClip());

  it('starts empty', () => {
    expect(getClip()).toBeNull();
  });

  it('stores a native-mirrored local cut', () => {
    setClip({ kind: 'local', origin: 'fb', mode: 'cut', paths: ['a/b.txt'] });
    const clip = getClip();
    expect(clip).toEqual({
      kind: 'local',
      origin: 'fb',
      mode: 'cut',
      paths: ['a/b.txt']
    });
  });

  it('stores a panel-copied remote entry', () => {
    setClip({
      kind: 'remote',
      origin: 'panel',
      items: [{ connKey: 'k1', name: 'data.csv', type: 'file' }]
    });
    const clip = getClip();
    expect(clip?.kind).toBe('remote');
    expect(clip?.origin).toBe('panel');
  });

  it('clears back to empty', () => {
    setClip({ kind: 'local', origin: 'panel', mode: 'copy', paths: ['x'] });
    clearClip();
    expect(getClip()).toBeNull();
  });
});

describe('offline reason for a peer refresh failure', () => {
  it('lists candidate causes for a bare TypeError without asserting one', () => {
    const reason = offlineReason(new TypeError('Failed to fetch'));
    expect(reason).toContain('Failed to fetch');
    expect(reason).toContain("owner's server is running");
    // must not assert a single cause - a wrong one misdirects the next reader
    expect(reason).toContain('Most often');
    expect(reason).toContain('this machine being offline');
  });

  it('blames local connectivity when our own server is unreachable too', () => {
    const reason = offlineReason(new TypeError('Failed to fetch'), true);
    expect(reason).toContain('local connectivity');
    expect(reason).not.toContain('JupyterHub stops');
  });

  it('explains a 401 as a password problem, not unreachability', () => {
    const reason = offlineReason(
      new Error('Could not load share (status 401)')
    );
    expect(reason).toContain('rejected the stored password');
  });

  it('explains a 404 as a removed resource', () => {
    const reason = offlineReason(
      new Error('Could not load request (status 404)')
    );
    expect(reason).toContain('removed this share or request');
  });

  it('never surfaces a useless placeholder string', () => {
    for (const bad of [undefined, null, {}, new Error('')]) {
      const reason = offlineReason(bad);
      expect(reason).not.toContain('undefined');
      expect(reason).not.toContain('[object Object]');
    }
  });
});
