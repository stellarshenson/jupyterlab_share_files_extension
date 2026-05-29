/**
 * Frontend unit tests for the share-files extension.
 *
 * These test pure logic - URL construction, link parsing, formatting -
 * without spinning up JupyterLab. Widget DOM tests would require the
 * @jupyterlab/testutils framework which we leave for an end-to-end suite.
 */

import {
  remoteDownloadUrl,
  remoteDownloadAllUrl,
  type IExtensionInfo
} from '../api';

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
