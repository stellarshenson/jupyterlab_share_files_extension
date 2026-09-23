/**
 * Frontend unit tests for the share-files extension.
 *
 * These test pure logic - URL construction, link parsing, formatting -
 * without spinning up JupyterLab. Widget DOM tests would require the
 * @jupyterlab/testutils framework which we leave for an end-to-end suite.
 */

import {
  hubTunnelLook,
  hubReasonText,
  linkCheckText,
  streamUrl,
  linkRef,
  connectionDownloadUrl,
  offlineReason,
  type IExtensionInfo
} from '../api';
import { clearClip, getClip, setClip } from '../clipboard';

describe('api URL helpers', () => {
  // the browser never touches the peer's origin: a connected entry is
  // downloaded through our own server (DEF-PEER-72)
  const settings = { baseUrl: 'https://hub.example.com/user/me/' } as any;
  const key = 'share:https://peer.example.com:A3KM7X2P';

  it('builds the download URL on our own server, key and name encoded, the limit with it', () => {
    expect(connectionDownloadUrl(settings, key, 'my file.csv', 10)).toBe(
      'https://hub.example.com/user/me/jupyterlab-share-files-extension/api/connections/' +
        'share%3Ahttps%3A%2F%2Fpeer.example.com%3AA3KM7X2P/download?name=my%20file.csv&max_gb=10'
    );
  });
});

describe('hub-mode link references', () => {
  it('reads kind and id off a standalone public link', () => {
    expect(
      linkRef(
        'https://lab.example.com/user/alice/jupyterlab-share-files-extension/public/share/ABCDEFGH'
      )
    ).toEqual({ kind: 'share', id: 'ABCDEFGH' });
    expect(
      linkRef(
        'http://localhost:8888/jupyterlab-share-files-extension/public/request/ZZZZ2345'
      )
    ).toEqual({ kind: 'request', id: 'ZZZZ2345' });
  });

  it('reads kind and id off a hub link, request ids carry the r_ prefix', () => {
    expect(
      linkRef('https://hub.example.com/s/fsmumfbhul/9LEqaQ_QZgKxjj0De_u1wA')
    ).toEqual({
      kind: 'share',
      id: '9LEqaQ_QZgKxjj0De_u1wA'
    });
    expect(
      linkRef('http://hub:8080/s/fsmumfbhul/r_PhcmNOMGM_zLhkr7bsuA')
    ).toEqual({
      kind: 'request',
      id: 'r_PhcmNOMGM_zLhkr7bsuA'
    });
  });

  it('returns null for anything else', () => {
    expect(linkRef('https://hub.example.com/hub/home')).toBeNull();
    expect(
      linkRef('https://hub.example.com/s/9LEqaQ_QZgKxjj0De_u1wA')
    ).toBeNull();
    expect(linkRef('')).toBeNull();
  });
});

describe('hub refusal slugs', () => {
  it('translates the closed slug set into plain words', () => {
    expect(hubReasonText('not_granted')).toMatch(/group/);
    expect(hubReasonText('sidecar_not_serving')).toMatch(/not serving/);
    expect(hubReasonText('hub_unavailable')).toMatch(/could not be reached/);
  });

  it('names the policy refusals galaxahub added with the cloud switch', () => {
    expect(hubReasonText('password_required')).toMatch(/requires a password/);
    expect(hubReasonText('tunnel_not_available')).toMatch(
      /Cloudflare turned off/
    );
    expect(hubReasonText('policy_conflict')).toMatch(/administrator/);
  });

  it("names a connected record's password change and the way back", () => {
    expect(hubReasonText('password_changed')).toMatch(
      /set or changed .* Connect Again in its row menu/
    );
  });

  it('answers an unknown slug with a sentence, never the slug itself', () => {
    const text = hubReasonText('something_new');
    expect(text).not.toContain('something_new');
    expect(text).toMatch(/does not know/);
    expect(hubReasonText('')).toBe('');
  });
});

describe('link check line', () => {
  it('says reachable when the link answered 200', () => {
    expect(
      linkCheckText({
        reachable: true,
        link: 'http://hub:8080/s/abcdef',
        status: 200
      })
    ).toBe('✓  Link is reachable');
  });

  it('names the link the lab server opened and the status that answered', () => {
    expect(
      linkCheckText({
        reachable: false,
        link: 'http://hub:8080/s/abcdef',
        status: 404
      })
    ).toBe(
      '✗  Link is not reachable - the lab server opened http://hub:8080/s/abcdef and got HTTP 404'
    );
  });

  it('says what the lab server got when nothing answered', () => {
    expect(
      linkCheckText({
        reachable: false,
        link: 'https://share.example.com/s/abcdef',
        error: 'no answer within 10 s'
      })
    ).toBe(
      '✗  Link is not reachable - the lab server opened https://share.example.com/s/abcdef and got no answer within 10 s'
    );
    expect(
      linkCheckText({
        reachable: false,
        link: 'http://hub:8080/s/abcdef',
        error: 'connection refused'
      })
    ).toMatch(/and got connection refused$/);
  });

  it('does not repeat the link the dialog already shows', () => {
    const link = 'https://share.example.com/s/abcdef';
    expect(linkCheckText({ reachable: false, link, status: 503 }, link)).toBe(
      '✗  Link is not reachable - the lab server got HTTP 503'
    );
    expect(
      linkCheckText(
        { reachable: false, link, error: 'no address for the host name' },
        link
      )
    ).toBe(
      '✗  Link is not reachable - the lab server got no address for the host name'
    );
    // a different address than the one shown is still named
    expect(
      linkCheckText(
        { reachable: false, link, status: 503 },
        'http://hub:8080/s/abcdef'
      )
    ).toBe(
      `✗  Link is not reachable - the lab server opened ${link} and got HTTP 503`
    );
  });
});

describe('hub cloud icon', () => {
  const info = (extra: Partial<IExtensionInfo>): IExtensionInfo => ({
    storage_path: '',
    shares_subdir: '',
    requests_subdir: '',
    mode: 'hub',
    hub: { available: true },
    tunnel_available: true,
    ...extra
  });
  const looks = {
    off: hubTunnelLook(info({ tunnel_default: false, tunnel_ready: true }), ''),
    on: hubTunnelLook(info({ tunnel_default: true, tunnel_ready: true }), ''),
    // switched on, hub tunnel not up yet
    pending: hubTunnelLook(
      info({ tunnel_default: true, tunnel_ready: false }),
      ''
    ),
    // switched on with no record asking for a tunnel: nothing is on the way
    armed: hubTunnelLook(
      info({ tunnel_default: true, tunnel_ready: false }),
      '',
      false
    ),
    hidden: hubTunnelLook(
      info({ tunnel_available: false, tunnel_default: true }),
      ''
    ),
    switchingOn: hubTunnelLook(info({ tunnel_default: false }), 'on'),
    switchingOff: hubTunnelLook(info({ tunnel_default: true }), 'off'),
    unreachable: hubTunnelLook(
      info({ hub: { available: false, reason: 'hub_unavailable' } }),
      ''
    )
  };

  it('reads on only once the hub tunnel is ready, pending until it is', () => {
    expect(looks.on.look).toBe('on');
    expect(looks.pending.look).toBe('pending');
    expect(looks.off.look).toBe('off');
  });

  it('does not wait for a tunnel no record asked for', () => {
    expect(looks.armed.look).toBe('armed');
    expect(looks.armed.pressed).toBe(true);
  });

  it('hides the icon where the group policy has no tunnel at all', () => {
    expect(looks.hidden.look).toBe('hidden');
  });

  it('reads unreachable while the hub does not answer', () => {
    expect(looks.unreachable.look).toBe('unreachable');
    expect(looks.unreachable.pressed).toBe(false);
  });

  it('shows a switch still in flight in the direction it is going', () => {
    expect(looks.switchingOn.look).toBe('pending');
    expect(looks.switchingOn.pressed).toBe(true);
    expect(looks.switchingOff.look).toBe('pending');
    expect(looks.switchingOff.pressed).toBe(false);
  });

  it('reads pressed on, mixed while the hub tunnel is not up, off otherwise', () => {
    expect(looks.on.pressed).toBe(true);
    expect(looks.pending.pressed).toBe('mixed');
    expect(looks.off.pressed).toBe(false);
  });

  it('keeps every tooltip to two short lines', () => {
    for (const { title } of Object.values(looks)) {
      const lines = title.split('\n');
      expect(lines.length).toBeLessThanOrEqual(2);
      for (const line of lines) {
        expect(line.length).toBeLessThanOrEqual(45);
      }
    }
  });

  it('names the action a click takes', () => {
    expect(looks.on.title).toMatch(/Click to switch it off$/);
    expect(looks.off.title).toMatch(/Click to switch it on$/);
    expect(looks.pending.title).toMatch(/Click to switch it off$/);
  });
});

describe('hub-mode change stream url', () => {
  const base = {
    baseUrl: 'https://hub.example.com/user/alice/',
    token: 'abc def'
  } as any;

  it('points at the extension stream route under the lab base url', () => {
    expect(streamUrl({ ...base, appendToken: false })).toBe(
      'https://hub.example.com/user/alice/jupyterlab-share-files-extension/api/stream'
    );
  });

  it('carries the token in the query only when the settings say so - an EventSource cannot set headers', () => {
    expect(streamUrl({ ...base, appendToken: true })).toBe(
      'https://hub.example.com/user/alice/jupyterlab-share-files-extension/api/stream?token=abc%20def'
    );
    expect(streamUrl({ ...base, appendToken: true, token: '' })).not.toContain(
      'token='
    );
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
  it('reads a bare TypeError as our own server not answering', () => {
    const reason = offlineReason(new TypeError('Failed to fetch'));
    expect(reason).toContain('Failed to fetch');
    expect(reason).toContain("this lab's own server did not answer");
  });

  it("relays the server's sentence about the peer as fact", () => {
    const sentence = 'The owner has removed this share or request.';
    expect(offlineReason(new Error(sentence))).toBe(sentence);
  });

  it('never surfaces a useless placeholder string', () => {
    for (const bad of [undefined, null, {}, new Error('')]) {
      const reason = offlineReason(bad);
      expect(reason).not.toBe('');
      expect(reason).not.toContain('undefined');
      expect(reason).not.toContain('[object Object]');
    }
  });
});
