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
