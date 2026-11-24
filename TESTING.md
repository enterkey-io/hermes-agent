# Enterkey testing standard (v1.0, Reese with Sloane, agreed 2026-11-24)

## What a valuable test is
A test earns a place only if all three are true:
1. It checks behavior a user or caller can observe (an API response, a stored record, a rendered page, an auth decision), not internals like private helpers, call counts or mock wiring.
2. It can fail. Someone has broken the code it protects and watched it go red.
3. Nothing else already catches the same break. If two tests go red on exactly the same mutations, one goes.

Priority order for what to protect: auth and permission boundaries, data loss or corruption, money and external side effects, then core user flows, then everything else.

## What to cut
- Tests that can't fail: assert on a mock's own return value, `expect(true)`, snapshot of a constant, no assertion at all, assertions inside a branch that never runs, try/catch that swallows the failure.
- Duplicates: the same behavior checked at unit, worker and e2e level. Keep the cheapest layer that still exercises the real boundary; e2e only for flows that need a browser to prove (navigation, auth redirect, one golden path per product).
- Tests of the framework or a library rather than our code.
- Tests pinned to implementation (exact SQL strings, internal function names, CSS class lists) unless that detail is the contract.
- Flaky tests: fix within a week or delete. A test that sometimes fails teaches people to ignore red.

## How a Herdr test agent proves a test can fail
For every new or changed test (existing untouched tests are covered by audits, not this rule), before the PR is called ready:
1. Name the behavior and the line(s) of product code that implement it.
2. Break that code on purpose: invert the condition, drop the auth check, return early, off-by-one, remove the write. One mutation per run.
3. Run only that test file. It must go red with a failure message that points at the behavior.
4. Revert, run again, it goes green.
5. Put one line per test in the PR body: `test name, mutation applied, red output (first line), green after revert`.
No red evidence, no new test. Mutations stay local and are never committed.

## What CI runs on every PR
- Typecheck.
- Lint, with the format check in the same step (`prettier --check` then `eslint`, back to back; no eslint-plugin-prettier, no separate job).
- The full fast suite (unit and worker tests) while it finishes in under about 3 minutes. Changed-path test selection is considered only past that, because it lets a PR go green after breaking code two folders over. Path filters may still skip CI entirely for docs-only PRs.
- One e2e smoke per product: the sign-in gate plus one core write path (create a project or task and see it show up). The full e2e suite runs nightly or before a release on the Mac mini (self-hosted runner or Herdr cron, Alina's call), never as a scheduled GitHub Action.
- Cancel superseded runs. No full re-run on main after merge: the PR run already tested that code, as long as the branch is up to date with main when it merges (require that, or the PR run tested a different tree). Deploy runs typecheck and build, not the suite.

## Ongoing
- Reese audits each product's suites periodically and flags junk tests to the PM and Sloane.
- When a bug reaches main or production that a test should have caught, Reese names the missing test and the mutation that would have shown it, and the PM's Herdr agent adds it with red evidence.
