/**
 * Configuration for Playwright using default from @jupyterlab/galata
 *
 * Galata pins `c.ServerApp.port = 8888` with `port_retries = 0`, so the test
 * server dies rather than move when that port is taken - a developer running
 * their own lab on 8888 cannot run the suite at all, and the failure reads as
 * "Process from config.webServer was not able to start". `JUPYTER_TEST_PORT`
 * threads one port through both this config and jupyter_server_test_config.py.
 * CI leaves the default.
 */
const os = require('os');
const path = require('path');
const baseConfig = require('@jupyterlab/galata/lib/playwright-config');

const PORT = process.env.JUPYTER_TEST_PORT || '8888';
const BASE_URL = `http://localhost:${PORT}`;

module.exports = {
  ...baseConfig,
  // refuse a lab this suite did not start, see global-setup.js
  globalSetup: require.resolve('./global-setup.js'),
  // tests/hub runs under playwright.hub.config.js against a mock hub,
  // tests/livehub under playwright.livehub.config.js against the real one
  testIgnore: ['**/hub/**', '**/livehub/**'],
  // Playwright defaults to half the machine's cores, which on a 64-thread
  // workstation is 32 browsers against one JupyterLab. Galata's own waits are
  // fixed at 15 s, so under that load its file-browser helpers time out and
  // tests fail for the load, not the code. The run is bound by the single
  // server either way - two workers take the same wall-clock as thirty-two
  // and pass repeatably. PLAYWRIGHT_WORKERS overrides it.
  workers: Number(process.env.PLAYWRIGHT_WORKERS || 2),
  use: { ...baseConfig.use, baseURL: BASE_URL },
  webServer: {
    command: 'jupyter lab --config jupyter_server_test_config.py',
    url: `${BASE_URL}/lab`,
    timeout: 120 * 1000,
    reuseExistingServer: !process.env.CI,
    env: {
      // the server extension under test is the working tree, not an installed copy
      PYTHONPATH: path.resolve(__dirname, '..'),
      // standalone mode even when the developer's own lab is hub-managed
      SHARE_FILES_PUBLIC_ZONE: '',
      // the Cloudflare config file - keep the developer's real one, with its
      // credentials and autostart, out of the test lab
      XDG_CONFIG_HOME: path.join(os.tmpdir(), 'share-files-galata')
    }
  }
};
