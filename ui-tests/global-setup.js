/**
 * Refuses a JupyterLab this suite did not start.
 *
 * Every config here carries `reuseExistingServer`, and Playwright's only test
 * is whether the port answers - so a run whose port is held by another
 * project's lab attaches to that lab, exercises an extension that is not
 * there, and reports the failures as ours (DEF-TESTS-94; it cost two full
 * diagnoses on 2026-09-20 and 2026-09-21). This asks the server what it is
 * serving before the first test.
 *
 * Only a clear negative stops the run: 404 means the route is not mounted, so
 * the lab on that port is not ours. Nothing listening is the normal case -
 * Playwright starts our own server next. Any other answer (a token-guarded
 * 403, a redirect) is inconclusive and never fails a run.
 */
const http = require('http');

const NAMESPACE = 'jupyterlab-share-files-extension';

function status(url) {
  return new Promise(resolve => {
    const request = http.get(url, res => {
      res.resume();
      resolve(res.statusCode);
    });
    request.on('error', () => resolve(0));
    request.setTimeout(5000, () => {
      request.destroy();
      resolve(0);
    });
  });
}

module.exports = async config => {
  const base = config.projects[0].use.baseURL;
  const url = `${base}/${NAMESPACE}/api/info`;
  if ((await status(url)) === 404) {
    throw new Error(
      `${base} is held by a JupyterLab that does not serve ${NAMESPACE} ` +
        `(GET ${url} answered 404). Stop that server, or set JUPYTER_TEST_PORT to a free port.`
    );
  }
};
