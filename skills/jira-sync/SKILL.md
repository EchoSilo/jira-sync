---
name: jira-sync
description: Track code work in Jira with GitHub links. Use in any git repo that has a .jira-sync.json file when starting, switching or finishing a piece of work (bug fix, feature, chore), when the user asks to sync, start, close or check Jira issues, or to set a repo up for Jira tracking. GSD repos (.planning/) sync automatically on every commit; milestones become fix versions, phases epics, plans stories and plan tasks sub-tasks.
argument-hint: "[start \"<what you're doing>\" | done [KEY] | sync | status | doctor | init --site <x.atlassian.net> --project <KEY>]"
allowed-tools: [Bash, Read]
---

<arguments>$ARGUMENTS</arguments>

All work goes through one script. Run it with the repo as the working directory:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/jira-sync/jira_sync.py" <verb> ...   # `python` if python3 is missing
```

If the repo has no `.jira-sync.json` and the user didn't ask for `init`, say Jira tracking isn't set up here and stop.

## Verbs

| User intent | Do this |
|---|---|
| `init` | `init --site <site> --project <KEY>`, then `doctor`. Tell the user to commit `.jira-sync.json`. |
| `doctor` | Run it and relay every FAIL line with what fixes it (see Setup below). |
| `sync` | `reconcile --dry-run`, show the summary, then `reconcile`. GSD repos only. |
| `status` | `status` |
| `start "<work>"` | Follow **Starting work** below. |
| `done [KEY]` | `done [KEY]`. With no key it closes the current issue. |

## Starting work (non-GSD, or ad-hoc work in a GSD repo)

1. Run `search "<3-5 distinctive words>"`. If an open issue clearly matches the work, reuse it with `use <KEY> [--branch]`. If it's ambiguous, ask the user which one.
2. Otherwise pick the type from what the work *is*, not how big it feels:
   - **bug**: existing behaviour is wrong or broken.
   - **story**: new behaviour a user will see or use.
   - **task**: chore, refactor, docs, config, dependency or research with no user-visible change.
   - **epic**: the request will clearly need several stories. Create the epic, then its stories with `--parent <EPIC>`, and start the first one.
3. `create --type <t> --summary "<imperative, <80 chars>" --description "<why + acceptance in 1-3 lines>" --start [--branch]`.
   Pass `--branch` when the repo works on feature branches (current branch isn't the default) or the user asked for one. Stay on the current branch otherwise, because the current-issue key still goes onto every commit.
4. Report the key and link in one line.

When the work is finished (tests pass, the user is happy, or a PR was merged), run `done`. Don't close an issue on your own judgement while the user is still iterating.

## Story points (relative estimation against anchors)

Stories are sized on the Fibonacci scale **1, 2, 3, 5, 8, 13** by comparing them to 3-5 already-finished reference stories (the anchors), never by guessing hours.

- **Anchors** live in `.jira-sync.json` under `estimation.anchors`.
  - `anchors` shows them, or proposes a set if there are none.
  - `anchors --accept` saves the proposal. `anchors --set ID=PTS,...` hand-picks them.
  - Always show proposed anchors to the user and get agreement before saving. They stay fixed afterwards, so a 5 keeps meaning the same thing.
- **Automatic first guess (GSD stories):** the sync scores each plan on CUE:
  - effort: the plan's token estimate;
  - complexity: files and tasks;
  - unknowns/risk: human checkpoints, hands-on plans, stated confidence.

  It places the story between the two anchors it falls between, picking the closer one; a full anchor-step beyond the largest anchor gives 13. It sets the points, labels the story `points-auto`, and comments the reason. It never overwrites points that are already set.
- **`estimate` (review pass, run after a round of GSD planning or when asked):**
  1. Run `estimate`. It lists the anchors and every not-yet-Done `points-auto` story with its plan path.
  2. For each story, read its PLAN.md and the anchors' plans, then ask the relative questions. Is it bigger or smaller than the 3-point anchor? More complex than the 5, simpler than the 8? Weigh complexity, unknowns and effort together (CUE), not coding time alone.
  3. Snap to the nearest scale value. Keep the automatic number when it holds up; that still counts as reviewed.
  4. Run `estimate --set KEY=N --reason "<which anchors it sits between and why>"`.
  5. Report one line per story, plus how many changed.
- **Non-GSD stories:** before `create --type story`, run `anchors` and size the work against them in the same way. Then add `--points N --reason "..."`.

## What happens automatically

- **commit-msg hook:** appends ` [KEY]` to every commit subject. It uses, first match wins: a key already in the message, then a key in the branch name, then the GSD scope (`feat(41-04):` maps to that plan's story, `docs(41):` to the phase epic, `quick-…` to its task), then the current issue. It never blocks a commit.
- **post-commit hook (GSD repos):** reconciles in the background from `.planning/` and logs to `.git/jira-sync.log`. Statuses only ever move forward, so it never reopens something a person closed.
- **PR hook:** after `gh pr create`, the title is prefixed with the issue key.
- The **GitHub for Jira** app turns those keys into the Development panel on each issue.

## Reporting rule

After every action, report in one line what changed. Also state every WARNING, FAIL or skipped item the script printed. Never summarise a partial sync as done. If `doctor` says `GSD parse ... unrecognized format`, GSD's file layout has changed: report it and don't attempt a reconcile.

## Setup the user must do (relay when doctor fails)

- **Credentials:** create an API token at https://id.atlassian.com/manage-profile/security/api-tokens, then
  - Windows: `setx JIRA_EMAIL you@x.com` and `setx JIRA_API_TOKEN <token>`. The script also reads these from the registry, so no restart is needed.
  - macOS/Linux: export both variables in your shell profile.
- **Hooks:** if the git hooks are missing (new clone or new machine), run `init` with no arguments.
- **Links:** install "GitHub for Jira" from the Atlassian Marketplace and connect the repo.
- **Fix versions:** if they are skipped, a team-managed project needs Releases turned on (Project settings → Features).
