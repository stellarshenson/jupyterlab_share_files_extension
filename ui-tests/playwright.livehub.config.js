/**
 * Playwright configuration for the live-hub suite (tests/livehub).
 *
 * One server: a JupyterLab spawned from inside a hub-managed lab, inheriting
 * the contract galaxahub injected into this process - the mode variable, the
 * hub API address and the lab's token - so the extension under test talks to
 * the REAL hub the developer's own lab talks to. Nothing here is mocked: the
 * records the tests create exist on the hub until the tests delete them, and
 * a Cloudflare switch-on waits for the hub's real tunnel. Fixtures carry the
 * `livehub-` prefix and every test removes its own.
 *
 * Refuses to run outside a hub-managed lab: without the hub variables the
 * lab would come up standalone and every test would fail for the wrong
 * reason. The mock-hub suite (`jlpm test:hub`) needs no hub at all.
 */
const os = require('os');
const path = require('path');
const baseConfig = require('@jupyterlab/galata/lib/playwright-config');

for (const name of ['SHARE_FILES_HUB_API', 'JUPYTERHUB_API_TOKEN']) {
  if (!process.env[name]) {
    throw new Error(
      `${name} is not set - the live-hub suite runs only inside a hub-managed lab (use \`jlpm test:hub\` for the mock hub)`
    );
  }
}

const PORT = process.env.JUPYTER_TEST_PORT || '8898';
const BASE_URL = `http://localhost:${PORT}`;
const REPO = path.resolve(__dirname, '..');

module.exports = {
  ...baseConfig,
  // refuse a lab this suite did not start, see global-setup.js
  globalSetup: require.resolve('./global-setup.js'),
  testDir: './tests/livehub',
  // one real hub, shared by every test - no parallel workers, no retries
  // that would re-run a switch against records a first attempt left behind
  workers: 1,
  retries: 0,
  use: { ...baseConfig.use, baseURL: BASE_URL },
  webServer: {
    command: 'jupyter lab --config jupyter_server_test_config.py',
    url: `${BASE_URL}/lab`,
    timeout: 120 * 1000,
    reuseExistingServer: !process.env.CI,
    env: {
      // the hub variables come from the process environment; only the mode
      // is pinned, so a lab whose env lost it still comes up in hub mode
      SHARE_FILES_PUBLIC_ZONE: 'hub',
      // the server reads the port from this variable; without it the lab
      // binds 8888 while Playwright waits on PORT
      JUPYTER_TEST_PORT: PORT,
      PYTHONPATH: REPO,
      // the hub copies a share's bytes out of the owner's workspace volume,
      // resolving the panel's paths against that volume's root - the test
      // lab must be rooted where the hub-managed lab is, or every share the
      // tests create ends refused as unreadable; each test's folder lives
      // there for the length of the test
      JUPYTERLAB_GALATA_ROOT_DIR: path.join(os.homedir(), 'workspace'),
      // the cloud toggle persists to the CLI config file - keep it out of
      // the developer's real one
      XDG_CONFIG_HOME: path.join(os.tmpdir(), 'share-files-galata-livehub')
    }
  }
};
