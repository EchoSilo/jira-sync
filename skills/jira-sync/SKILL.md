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
3. Every story, task and bug needs an **Epic**. Run `epics` and pick the open epic the work belongs to. In a GSD repo that is usually the current phase. If none fits, create an epic first.
4. Write the issue so a teammate could pick it up cold:
   - **Summary:** under 70 characters, a plain title of the outcome ("Parlay cards show future timestamps"). No ids, no detail. The script refuses longer ones.
   - **Description:** markdown-style `## ` sections, which become headings in Jira:
     - `## Context`: why it matters or what's broken.
     - `## Scope`: what changes, as `- ` bullets.
     - `## Acceptance criteria`: testable bullets.
     - `## Notes`: links, files, risks. Optional.
     - Bugs use `## Symptoms` (expected vs actual, steps), `## Impact` and `## Acceptance criteria` instead.
5. `create --type <t> --summary "..." --description "..." --parent <EPIC> --start [--branch]`.
   Stories, tasks and bugs go into the active sprint automatically.
   Pass `--branch` when the repo works on feature branches (current branch isn't the default) or the user asked for one. Stay on the current branch otherwise, because the current-issue key still goes onto every commit.
6. Report the key, epic, sprint and link in one line.

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

- **commit-msg hook:** appends ` [KEY]` to every commit subject, picking the most specific issue. First match wins: a key already in the message; a key in the branch name; for a GSD plan commit (`feat(41-04):`), the **sub-task** whose `<files>` overlap the staged files most, then the plan's **story** (its `complete` commit and anything unmatched); for a phase-wide commit (`docs(41):` planning/review/verification), that phase's **"planning, review and verification" task**; `quick-…` to its task; then the current issue. Commits never land on an epic. Never blocks a commit.
- **post-commit hook (GSD repos):** reconciles in the background from `.planning/` and logs to `.git/jira-sync.log`. Statuses only ever move forward, so it never reopens something a person closed. The background run follows these rules:
  - **Text:** summaries are short titles (60 characters at most). Descriptions are built from the GSD files:
    - epic: goal, success criteria, requirements;
    - story: objective, outcome, scope, acceptance criteria, files;
    - sub-task: behaviour, steps, acceptance, verify command;
    - bug: symptoms, root cause, fix, verification.

    Text is written on create only. `reconcile --refresh-text` rewrites it for every synced issue.
  - **Epics:** bugs, quick tasks and todos go under the phase being worked on when they appeared. Open work goes under the current phase; past work is matched by date to the phase whose plans had started by then. A missing epic is filled in, but an issue someone moved by hand is never re-parented.
  - **Sprints:** every story, task and bug sits in a sprint.
    - Open work goes into the active sprint.
    - Finished work goes into the sprint whose dates cover when it finished.
    - Work finished before the first sprint goes into "Sprint 0 (before Jira)". That sprint closes itself at the next rollover, because team-managed boards allow only one active sprint.
    - Sprints are calendar months. When the active sprint's end date passes, the next sync closes it, starts the next one (creating it if needed), and carries unfinished work over.
- **In Review (optional):** set `"statuses": {"review": "In Review"}` in `.jira-sync.json` once the Jira workflow has that status, with transitions into it.
  - A finished GSD plan's Story goes to **In Review**, not Done. It reaches Done only when its phase is verified: the roadmap ticks it, or the verification report says `passed` and the UAT log says `complete`.
  - A debug session whose fix awaits your check (`awaiting_human_verify`) puts its Bug In Review.
  - Sub-tasks still go to Done with their plan.
  - Moves are by status **name** (`"progress"` defaults to "In Progress"), so a workflow with extra working statuses such as "Planning" is never mis-targeted.
  - `reconcile --reopen-unverified` is a one-off that sends Done stories of unverified phases back to In Review.
  - Non-GSD: `/jira-sync review [KEY]` marks work code-complete, awaiting verification.
- **Side quests found during verification:** bugs, quick tasks and todos created after a phase's verification began, plus gap-closure plans, get a "relates to" link to that phase's planning/review/verification Task.
- **Epic roll-up** runs after every sync, `start` and `done`, and checks every child of every open epic, including issues created by hand. If any child is In Progress or Done, the epic moves to at least In Progress. When every child is Done, the epic moves to Done. Like all moves, it is forward-only.
- **`epics`** lists the open epics to file new work under.
- **Auto-push (`init --auto-push on`):** after every commit the branch is pushed in the background so the GitHub for Jira app sees it. Never forces; skips mid-rebase/merge; a failed push is logged and retried on the next commit.
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
