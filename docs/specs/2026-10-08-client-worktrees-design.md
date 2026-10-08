# Client worktrees — design

- **Date:** 2026-10-08
- **Target version:** 3.1.0

## Goal

Work on several clients at once, each fully isolated, without going back to
one repository copy per form. Automate the whole lifecycle: start, keep up to
date with `main`, deliver, clean up.

## Model

One `git worktree` per client questionnaire, on its own short-lived branch,
sharing the one repository:

```
PBRfactor/
  PersonaBot/                  main — general code changes only
  clients/<client>/<survey>/   worktree on branch survey/<survey>
  clients/_archive/<client>/   output archives written at delivery
```

Each worktree owns its branch (the survey folder is invisible to other clients
until delivered), its `output/`, its `.env` and its `.venv`. A per-worktree
venv is mandatory: an editable install resolves `personabot` to one checkout
only, so a shared venv would run another client's code.

**Shared resources.**
- *Gemini keys:* each process has its own key pool and cannot see another's
  spend, so two clients generating at once on the same keys hit quota sooner.
  `--keys 1-7` gives a client a subset (renumbered in its `.env`); `client list`
  shows each client's keys and any overlap. Default: all keys.
- *Tor:* not split. The same exit IP for two different clients is acceptable;
  within one client, concurrent submissions already get distinct SocksPorts.

## Commands

`personabot client <action>`, with `make` wrappers.

| Action | Make | Effect |
|---|---|---|
| `new CLIENT SURVEY [--from ID] [--keys SPEC] [--no-install]` | `make client CLIENT= SURVEY= [FROM=] [KEYS=]` | worktree + branch from `main`; `.env` from the base `.env` with `SURVEY` set and the key subset; survey scaffolded (or reused if already on `main`) and committed; `make install` in the worktree |
| `list` | `make clients` | every client worktree: branch, commits ahead/behind `main`, uncommitted files, keys, queue counts, key overlaps |
| `sync [CLIENT SURVEY]` | `make sync` | fast-forward `main` from `origin` if possible, then rebase the branch onto `main` (abort cleanly on conflict) |
| `deliver [CLIENT SURVEY] [--no-push] [--keep] [--skip-checks] [--no-archive]` | `make deliver` | see below |

`CLIENT SURVEY` may be omitted when run from inside the worktree.

**Deliver**, in order, stopping at the first failure with nothing half-done:
1. Uncommitted changes under `surveys/<id>/` are committed automatically;
   any other uncommitted change aborts (general code needs a deliberate commit).
2. `sync` (rebase onto the current `main`).
3. Checks in the worktree's own venv: `validate-config` and the test suite.
4. Archive `output/<id>/` to `clients/_archive/<client>/<id>-<timestamp>.tar.gz`.
5. Annotated tag `delivered/<client>/<id>/<YYYY-MM-DD>` (suffix `-2`, … if taken).
6. Fast-forward `main` in the base checkout (which must be on `main`, clean).
7. Push `main` and the tag when an `origin` remote exists.
8. Remove the worktree and delete the merged branch.

## Out of scope

Automatic key re-balancing between running clients; pushing work-in-progress
branches.

## Testing

Real git in a temporary repository (no network, no install): creation,
isolation of `.env`, reuse of an existing survey, refusal cases, rebase onto a
moved `main`, auto-commit vs. abort, tag naming, cleanup, listing.
