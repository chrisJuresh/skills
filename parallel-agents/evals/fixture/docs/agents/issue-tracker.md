# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues. Use the `gh` CLI for all operations.

- **Create**: `gh issue create --title "..." --body "..."`
- **Read**: `gh issue view <number> --comments`
- **List**: `gh issue list --state open --json number,title,labels,assignees`
- **Claim**: `gh issue edit <n> --add-assignee @me`
- **Close**: `gh issue close <n> --comment "..."`

Blocking edges use GitHub's native issue dependencies:
`gh api repos/acme/console/issues/<n> --jq .issue_dependencies_summary.blocked_by`
