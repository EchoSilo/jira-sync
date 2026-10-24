# jira-sync

A Claude Code plugin that keeps a Jira project in step with a git repo.

- **Every commit and PR carries its Jira key.** The GitHub for Jira app then links them on the issue.
- **GSD projects sync automatically on every commit:**

  | GSD | Jira |
  |---|---|
  | milestone | Fix Version (released when archived) |
  | phase | Epic |
  | plan | Story |
  | plan task | Sub-task |
  | debug session | Bug |
  | quick task / todo | Task |

  Statuses follow GSD's own files and only ever move forward.
- **Any other work:** `/jira-sync start "fix the login redirect"`. Claude reuses a matching open issue or creates a Bug, Story, Task or Epic, moves it to In Progress, and tags your commits with its key. Close it with `/jira-sync done`.

It needs only the Python standard library, and has no Jira site, project or path hardcoded.

## Install

```
/plugin marketplace add EchoSilo/jira-sync
/plugin install jira-sync@jira-sync
```

1. Create an API token at https://id.atlassian.com/manage-profile/security/api-tokens.
2. Set the credentials:
   - Windows: `setx JIRA_EMAIL you@example.com` and `setx JIRA_API_TOKEN <token>`
   - macOS/Linux: export both variables in your shell profile.
3. Install **GitHub for Jira** on your Atlassian site and connect the repo.

## Turn it on for a repo

```
/jira-sync init --site yoursite.atlassian.net --project KEY
```

This writes `.jira-sync.json` (commit it) and installs the `commit-msg` and `post-commit` git hooks. Any existing hooks are chained, not replaced. On a fresh clone or a new machine, run `/jira-sync init` again to reinstall the hooks.

Check everything with `/jira-sync doctor`. In GSD repos, preview the first sync with `/jira-sync sync`.

## How it works

| Piece | Where |
|---|---|
| Jira target | `<repo>/.jira-sync.json` (committed) |
| GSD id → issue key | Jira labels `jira-sync` + `gsd-<id>`, cached in `.git/jira-sync-map.json` |
| Current issue (non-GSD) | `.git/jira-sync-active` |
| Background sync log | `.git/jira-sync.log` |
| Script used by git hooks | `~/.jira-sync/jira_sync.py`, refreshed from the plugin on every manual run |

Re-running a sync never duplicates issues, because Jira's labels are the source of truth.

## Test

```
python tests/test_jira_sync.py
```
