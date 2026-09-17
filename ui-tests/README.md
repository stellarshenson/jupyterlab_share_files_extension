# Integration Testing

This folder contains the integration tests of the extension.

They are defined using [Playwright](https://playwright.dev/docs/intro) test runner
and [Galata](https://github.com/jupyterlab/jupyterlab/tree/main/galata) helper.

The Playwright configuration is defined in [playwright.config.js](./playwright.config.js).

The JupyterLab server configuration to use for the integration test is defined
in [jupyter_server_test_config.py](./jupyter_server_test_config.py).

The default configuration will produce video for failing tests and an HTML report.

> There is a UI mode that you may like; see [that video](https://www.youtube.com/watch?v=jF0yA-JLQW0).

## Run the tests

> All commands are assumed to be executed from the root directory

To run the tests, you need to:

1. Compile the extension:

```sh
jlpm install
jlpm build:prod
```

> Check the extension is installed in JupyterLab.

2. Install test dependencies (needed only once):

```sh
cd ./ui-tests
jlpm install
jlpm playwright install
cd ..
```

3. Execute the [Playwright](https://playwright.dev/docs/intro) tests:

```sh
cd ./ui-tests
jlpm playwright test
```

Test results will be shown in the terminal. In case of any test failures, the test report
will be opened in your browser at the end of the tests execution; see
[Playwright documentation](https://playwright.dev/docs/test-reporters#html-reporter)
for configuring that behavior.

## Hub-mode suite

`tests/hub` drives a JupyterLab spawned with the contract galaxahub injects into every lab it manages, against the mock hub in `mock_hub.py` - no real hub and no Cloudflare tunnel are needed. The default configuration ignores this folder; run it with its own configuration:

```sh
cd ./ui-tests
jlpm test:hub
```

The mock speaks the hub's fileshare routes including the per-record cloud switch, the password requirement, the change stream and the recipient page `/s/<id>`; `/_control/*` is the test's side door (reset, capabilities, policy, tunnel, page, cloudoff, upload, nudge, calls, streams). `tunnel` sets the tunnel base, its registration delay after the first switch-on and whether it ever registers; `page` sets the status the recipient page answers; `cloudoff` sets the status a Cloudflare switch off answers. `MOCK_HUB_PORT` (default 8765) and `JUPYTER_TEST_PORT` (default 8888) move the ports when the defaults are taken. The configuration sets `SHARE_FILES_CLOUD_CONFIRM_SECONDS=10` on the lab, so its wait for the hub to confirm a Cloudflare switch-on ends after 10s instead of 120s and the timeout is testable. Both configurations serve the working tree's build through `labextensions/` (a symlink to the build output, so `jlpm build` first) and load the server extension from the repository through `PYTHONPATH`.

## Live-hub suite

`tests/livehub` drives a JupyterLab spawned from inside a hub-managed lab against the real hub that lab talks to: the configuration inherits the contract galaxahub injected into the shell (`SHARE_FILES_HUB_API`, `JUPYTERHUB_API_TOKEN`, the hub's base URL) and refuses to start without it. Nothing is mocked - the records exist on the hub until the tests delete them, and a Cloudflare switch-on waits for the hub's real tunnel (about 70 s), so the suite is slow and runs one test at a time. Every fixture carries the `livehub-` prefix; each test removes its own and the first sweeps any an aborted run left behind. Run it from a terminal inside the hub lab; it takes port 8898 unless `JUPYTER_TEST_PORT` says otherwise:

```sh
jlpm test:livehub
```

It covers what the mock cannot vouch for: the create, ready and delete round trip on the hub, the refusal reasons the hub relays, and the Cloudflare switch confirmed by the hub's tunnel with the link moving to the tunnel host and back. The mock-hub suite stays the place for the failure paths (a tunnel that never registers, a switch off the hub refuses, an outage), which a live hub cannot be asked to produce.

## Update the tests snapshots

> All commands are assumed to be executed from the root directory

If you are comparing snapshots to validate your tests, you may need to update
the reference snapshots stored in the repository. To do that, you need to:

1. Compile the extension:

```sh
jlpm install
jlpm build:prod
```

> Check the extension is installed in JupyterLab.

2. Install test dependencies (needed only once):

```sh
cd ./ui-tests
jlpm install
jlpm playwright install
cd ..
```

3. Execute the [Playwright](https://playwright.dev/docs/intro) command:

```sh
cd ./ui-tests
jlpm playwright test -u
```

> Some discrepancy may occurs between the snapshots generated on your computer and
> the one generated on the CI. To ease updating the snapshots on a PR, you can
> type `please update playwright snapshots` to trigger the update by a bot on the CI.
> Once the bot has computed new snapshots, it will commit them to the PR branch.

## Create tests

> All commands are assumed to be executed from the root directory

To create tests, the easiest way is to use the code generator tool of playwright:

1. Compile the extension:

```sh
jlpm install
jlpm build:prod
```

> Check the extension is installed in JupyterLab.

2. Install test dependencies (needed only once):

```sh
cd ./ui-tests
jlpm install
jlpm playwright install
cd ..
```

3. Start the server:

```sh
cd ./ui-tests
jlpm start
```

4. Execute the [Playwright code generator](https://playwright.dev/docs/codegen) in **another terminal**:

```sh
cd ./ui-tests
jlpm playwright codegen localhost:8888
```

## Debug tests

> All commands are assumed to be executed from the root directory

To debug tests, a good way is to use the inspector tool of playwright:

1. Compile the extension:

```sh
jlpm install
jlpm build:prod
```

> Check the extension is installed in JupyterLab.

2. Install test dependencies (needed only once):

```sh
cd ./ui-tests
jlpm install
jlpm playwright install
cd ..
```

3. Execute the Playwright tests in [debug mode](https://playwright.dev/docs/debug):

```sh
cd ./ui-tests
jlpm playwright test --debug
```

## Upgrade Playwright and the browsers

To update the web browser versions, you must update the package `@playwright/test`:

```sh
cd ./ui-tests
jlpm up "@playwright/test"
jlpm playwright install
```
