---
name: rwx
metadata:
  version: "0.1.6"
description: >-
  Can be used to kick off local "run loops" on the RWX CI platform, or when
  understanding, creating or modifying an RWX CI/CD config or any other file in
  a .rwx directory. TRIGGER when: the user asks about RWX; wants to create or
  modify an RWX config; says things like "CI failed", "CI is red", or "tests are
  failing in CI"; asks "what's failing on my PR / branch / commit?"; wants to 
  run tests or build steps in a sandbox; wants to debug test failures, check a 
  run status, fetch logs/artifacts, or start a run loop.
argument-hint: [optional description, e.g. "CI pipeline with tests and deploy"]
allowed-tools:
  Bash(rwx lint *), Bash(rwx docs *), Bash(rwx logs *), Bash(rwx artifacts *),
  Bash(rwx results *), Bash(rwx whoami), Bash(rwx * --help)
---

# Using RWX

Ensure the user is signed in and on the latest version of the RWX CLI before
getting started: `rwx whoami`

Then, fetch the reference docs index with:

    rwx docs pull /docs/rwx/migrating/rwx-reference

If you encounter a question not covered by these references, use
`rwx docs search "<query>"` to find the relevant documentation page, then
`rwx docs pull` the result.

If the user chooses, you can kick off an actual run on RWX:

    rwx run .rwx/<name>.yml --wait

When the run finishes, results will be shown, and you can iterate in that
fashion until the run passes.

No git push is required to invoke a run from the RWX CLI.

## Check CI / run results or status of a branch

If you have been asked to check on run failures or CI failures, or if you have
been asked to check the current run status of the current branch or a given
branch:

    rwx results -h

## Sandboxes

If the user asks to run tests, build steps, or migrations in a sandbox:

    rwx sandbox exec -- <command>

See more with `rwx sandbox --help`.

## Generate or Modify RWX Config

You may have been tasked with creating, modifying, or understanding/explaining
an RWX CI/CD config for this project. Use the same aforementioned documentation.

When making changes, you can run validation on the config:

    rwx lint .rwx/<name>.yml
