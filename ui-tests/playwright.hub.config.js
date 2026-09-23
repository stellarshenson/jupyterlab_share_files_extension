/**
 * Playwright configuration for the hub-mode suite (tests/hub).
 *
 * Two servers: the mock hub (`mock_hub.py`) and a JupyterLab spawned with the
 * contract galaxahub injects into every lab it manages - the mode variable,
 * the hub API address and the lab's token. The extension then mounts only
 * its authenticated `api/*` routes and talks to the mock hub; nothing here
 * needs a real hub or a live Cloudflare tunnel.
 *
 * `JUPYTER_TEST_PORT` and `MOCK_HUB_PORT` move the ports when the defaults
 * are taken. PYTHONPATH points at the repository so the server extension
 * under test is the working tree, not an installed copy.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const baseConfig = require('@jupyterlab/galata/lib/playwright-config');

const PORT = process.env.JUPYTER_TEST_PORT || '8888';
const HUB_PORT = process.env.MOCK_HUB_PORT || '8765';
const BASE_URL = `http://localhost:${PORT}`;
const HUB_URL = `http://127.0.0.1:${HUB_PORT}`;
const REPO = path.resolve(__dirname, '..');

// One root for the lab and the mock hub: the hub writes into a workspace
// through its own mount of the volume, and the mock writes through this
// folder, so a test finds what the hub saved. Made once, in the runner; the
// workers inherit it. Beside jupyter_server_test_config.py's own, ignored by git.
if (!process.env.JUPYTERLAB_GALATA_ROOT_DIR) {
  const parent = path.join(__dirname, '.galata-root');
  fs.mkdirSync(parent, { recursive: true });
  process.env.JUPYTERLAB_GALATA_ROOT_DIR = fs.mkdtempSync(
    path.join(parent, 'hub-')
  );
}

module.exports = {
  ...baseConfig,
  // refuse a lab this suite did not start, see global-setup.js
  globalSetup: require.resolve('./global-setup.js'),
  testDir: './tests/hub',
  // Playwright defaults to half the machine's cores, which on a 64-thread
  // workstation is 32 browsers against one JupyterLab. Galata's own waits are
  // fixed at 15 s, so under that load its file-browser helpers time out and
  // tests fail for the load, not the code. The run is bound by the single
  // server either way - two workers take the same wall-clock as thirty-two
  // and pass repeatably. PLAYWRIGHT_WORKERS overrides it.
  workers: Number(process.env.PLAYWRIGHT_WORKERS || 2),
  use: { ...baseConfig.use, baseURL: BASE_URL },
  webServer: [
    {
      command: 'python mock_hub.py',
      url: `${HUB_URL}/_control/health`,
      timeout: 30 * 1000,
      reuseExistingServer: !process.env.CI,
      env: {
        MOCK_HUB_PORT: HUB_PORT,
        MOCK_HUB_ROOT: process.env.JUPYTERLAB_GALATA_ROOT_DIR
      }
    },
    {
      command: 'jupyter lab --config jupyter_server_test_config.py',
      url: `${BASE_URL}/lab`,
      timeout: 120 * 1000,
      reuseExistingServer: !process.env.CI,
      env: {
        SHARE_FILES_PUBLIC_ZONE: 'hub',
        SHARE_FILES_HUB_API: `${HUB_URL}/hub/api/fileshare`,
        JUPYTERHUB_API_TOKEN: 'test-token',
        JUPYTERHUB_BASE_URL: '/',
        // the user the hub spawned this lab for - the mock hub's own records
        // are alice's
        JUPYTERHUB_USER: 'alice',
        PYTHONPATH: REPO,
        // the cloud toggle persists to the CLI config file - keep it out of
        // the developer's real one
        XDG_CONFIG_HOME: path.join(os.tmpdir(), 'share-files-galata-hub')
      }
    }
  ]
};
