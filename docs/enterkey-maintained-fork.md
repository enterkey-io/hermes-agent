# Enterkey Maintained Fork

The Enterkey deployment uses the stock Hermes update command and dashboard
update action. The production checkout has these remotes:

```text
origin    https://github.com/enterkey-io/hermes-agent.git
upstream  https://github.com/NousResearch/hermes-agent.git
```

`main` tracks `origin/main`. That branch is the deployable release line and
contains both reviewed Nous changes and the Enterkey features required by the
running agent fleet.

## Deploying an approved release

Use the normal Hermes interface:

```bash
hermes update --check
hermes update --yes
```

The dashboard's **Update now** action invokes the same updater. Do not add an
Enterkey-specific update script or alter the dashboard action. The stock
updater backs up state, fast-forwards the checkout from `origin/main`, refreshes
dependencies and bundled assets, and restarts managed Hermes services.

Production must remain on a clean `main` checkout. Develop fixes on named
branches and merge them into the Enterkey fork before deployment; never depend
on an uncommitted production edit surviving an update.

## Incorporating Nous releases

The stock updater intentionally does not merge `upstream/main` into a fork that
has fork-only commits. That merge is a code-integration operation and must be
reviewed and tested before it reaches the deployable branch.

1. Create an upgrade branch from current Enterkey `main`.
2. Fetch and merge `upstream/main` without rebasing or discarding Enterkey
   history.
3. Resolve conflicts in favor of current behavior only after checking both
   implementations and their tests.
4. Run focused tests for conflict areas, the updater and cron suites, the web
   test/build, and broader tests proportional to the affected surface.
5. Merge the tested branch into Enterkey `main` and push it to `origin`.
6. Deploy through the stock Hermes update action and verify every managed
   gateway and the dashboard.

This separation makes the routine update path safe while keeping upstream code
integration observable and reversible.

## Verification

After an update, confirm:

```bash
git status --short --branch
git rev-parse HEAD origin/main
systemctl --user --failed --no-pager
systemctl --user list-units --type=service 'hermes*' --no-pager
```

`HEAD` must equal `origin/main`, the tree must be clean, and all enabled Hermes
gateway and dashboard units must be active.
