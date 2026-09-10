---
# Scheduled deployment: runs "LinkedIn Outreach (scheduled)" every weekday
# morning without anyone starting the session by hand.
#
# Schema verified against:
#   https://platform.claude.com/docs/en/managed-agents/scheduled-deployments
#     (the page lives there, NOT at .../managed-agents/deployments, which
#     404s - see task-1d-report.md)
#   https://platform.claude.com/docs/en/api/beta/deployments/create
#     (the full parameter reference; confirms vault_ids and budget are
#     both create-time fields on a deployment, not just on a session)
#   https://platform.claude.com/docs/en/cli-sdks-libraries/cli/apply
#     ("Grow it into a project": "A deployment is a Markdown file in
#     deployments/: the frontmatter is the request body and the prose
#     becomes the message that starts each session.")
# The first two are quoted verbatim in task-1d-report.md next to this
# file; the third is quoted in task-1e-report.md.
#
# Applying this file: like every other file under platform/, this is
# applied with `ant apply`, not a one-shot `create` call - deployments are
# an applyable kind like any other. Re-running `ant apply` against an
# edited version of this file updates the same deployment via
# claude-lock.json; it does not create a second, independent deployment on
# the same schedule. See README.md for the exact PowerShell invocation and
# the warning there about what happens if claude-lock.json is ever lost or
# left uncommitted (every resource in this project becomes a duplicate
# risk then, not just this one).

name: LinkedIn Outreach morning review

# Relative path, resolved by `ant apply` to the scheduled agent's real ID
# and sent as {type: agent, id, version} - no more copying an
# `agent_01...` string out of a terminal by hand. `ant apply` pins this to
# whichever version of ../agents/scheduled.md was just applied
# (cli/apply, "Grow it into a project": "ant apply pins agent and skill
# references to the version it just applied, so editing reviewer.md ...
# updates everything that references them in the same run"). The page
# promises that pin moves "in the same run" as the agent edit; it does not
# say what happens if you apply agents/scheduled.md alone, on a different
# day, without also reapplying this file. TODO(verify): until tested,
# don't assume an edited agent propagates here on its own - reapply this
# file (or the whole platform/ directory, as step 8 in README.md does) any
# time you want this deployment to pick up an agent change.
agent: ../agents/scheduled.md

# Relative path, resolved the same way, to the environment's real ID.
environment_id: ../environments/cloud.yaml

# Placeholder, a list, and NOT a relative path: vaults have no
# `vaults/`-style kind directory and are never mentioned in the `ant
# apply` CLI page's list of applyable kinds (agents, environments, skills,
# memory stores, deployments) - vault_ids stays a real ID the user pastes
# by hand, from creating the vault (README.md step 3). One vault holds
# both the outreach service credential and (if you've added it) the
# Unipile credential; maximum 50 vault IDs are accepted, one is all this
# design needs.
vault_ids:
  - REPLACE_WITH_VAULT_ID

schedule:
  type: cron
  # 07:20, Monday through Friday. POSIX cron: minute hour day-of-month
  # month day-of-week; day-of-week 1-5 is Mon-Fri (0 and 7 both mean
  # Sunday - platform.claude.com/docs/en/api/beta/deployments/create).
  # 07:20 falls outside the 1-3 AM local window the docs warn produces
  # missed or duplicate fires across a DST transition, so no special
  # handling is needed for that here.
  expression: "20 7 * * 1-5"
  # Mirrors OUTREACH_TZ in Python/.env, the project's shared environment
  # file. The two must name the same zone: this one decides when the session
  # starts, OUTREACH_TZ decides what the service calls "today" once it has.
  # Disagreeing, a 07:20 run could plan a day that has not started yet where
  # the service is looking. Change both together.
  timezone: America/New_York

budget:
  type: limit
  max_list_cost:
    # Placeholder. `amount` is a whole number of US CENTS, written as a
    # string with no leading zeros - "1500" is $15.00, NOT $1,500
    # (platform.claude.com/docs/en/managed-agents/budgets and the
    # deployments create reference, both quoted in task-1d-report.md).
    # Run the manual session in README.md step 6 a few times first, read
    # each run's usage.list_cost (same units, cents), and set this to
    # roughly 3x the highest figure you see.
    #
    # Do not leave this low "to be safe." A session that reaches this cap
    # does not error - it goes idle with stop_reason: budget_reached and
    # simply stops issuing new model requests, silently, with no
    # exception or failed-job signal anywhere an unattended morning run
    # would surface it. Too low reads exactly like an agent that finished
    # early or quit without doing its job, not like one that hit a limit.
    amount: "REPLACE_WITH_CENTS_AMOUNT"
    currency: USD
---

Run the morning outreach review
