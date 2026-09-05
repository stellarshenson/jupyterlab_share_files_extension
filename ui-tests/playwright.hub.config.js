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
const os = require('os');
const path = require('path');
const baseConfig = require('@jupyterlab/galata/lib/playwright-config');

const PORT = process.env.JUPYTER_TEST_PORT || '8888';
const HUB_PORT = process.env.MOCK_HUB_PORT || '8765';
const BASE_URL = `http://localhost:${PORT}`;
const HUB_URL = `http://127.0.0.1:${HUB_PORT}`;
const REPO = path.resolve(__dirname, '..');

module.exports = {
  ...baseConfig,
  testDir: './tests/hub',
  use: { ...baseConfig.use, baseURL: BASE_URL },
  webServer: [
    {
      command: 'python mock_hub.py',
      url: `${HUB_URL}/_control/health`,
      timeout: 30 * 1000,
      reuseExistingServer: !process.env.CI,
      env: { MOCK_HUB_PORT: HUB_PORT }
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
        PYTHONPATH: REPO,
        // the cloud toggle persists to the CLI config file - keep it out of
        // the developer's real one
        XDG_CONFIG_HOME: path.join(os.tmpdir(), 'share-files-galata-hub')
      }
    }
  ]
};
