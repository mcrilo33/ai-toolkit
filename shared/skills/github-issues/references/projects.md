# Projects V2

GitHub Projects V2 is managed via GraphQL, run with `gh api graphql`.

## Listing projects and items

**List projects** (use `user(login: "USER")` for a user):

```bash
gh api graphql -f query='{ organization(login: "ORG") { projectsV2(first: 50) { nodes { id title number } } } }' --jq '.data.organization.projectsV2.nodes'
```

**List project items** (while `hasNextPage`, repeat with `after:` set to the last edge's `cursor`):

```bash
gh api graphql -f query='{
  node(id: "PROJECT_ID") {
    ... on ProjectV2 {
      items(first: 100) {
        edges { cursor node { id content { ... on Issue { number title } } } }
        pageInfo { hasNextPage }
      }
    }
  }
}' --jq '.data.node.items'
```

**Add an issue/PR to a project** (`CONTENT_NODE_ID` is the issue/PR node ID):

```bash
gh api graphql -f query='mutation { addProjectV2ItemById(input: {projectId: "PROJECT_ID", contentId: "CONTENT_NODE_ID"}) { item { id } } }'
```

## Finding a project by name

> **⚠️ Known issue:** `projectsV2(query: "…")` does keyword search, not exact name match, and returns results sorted by recency. Common words like "issue" or "bug" return hundreds of false positives. The actual project may be buried dozens of pages deep.

Use this priority order:

### 1. Direct lookup (if you know the number)

```bash
gh api graphql -f query='{
  organization(login: "ORG") {
    projectV2(number: 5) { id title }
  }
}' --jq '.data.organization.projectV2'
```

### 2. Reverse lookup from a known issue (most reliable)

```bash
gh api graphql -f query='{
  repository(owner: "OWNER", name: "REPO") {
    issue(number: 123) {
      projectItems(first: 5) {
        nodes { project { title number id } }
      }
    }
  }
}' --jq '.data.repository.issue.projectItems.nodes[].project'
```

### 3. GraphQL name search with client-side filtering (fallback)

```bash
gh api graphql -f query='{
  organization(login: "ORG") {
    projectsV2(query: "My Project", first: 20) {
      nodes { id title number }
    }
  }
}' --jq '.data.organization.projectsV2.nodes[] | select(.title == "My Project")'
```

## Project discovery for progress reports

When a user asks for a progress update on a project (e.g., "Give me a progress update for Project X"), follow this workflow:

1. **Find the project** — use the [finding a project](#finding-a-project-by-name) strategies above. Ask the user for a known issue number if name search fails.

2. **Discover fields** - query the project's fields to find the Status field (its options tell you the workflow stages) and any Iteration field (to scope to the current sprint).

3. **Get all items** - list the project items. For large projects (100+ items), paginate through all pages. Each item includes its field values (status, iteration, assignees).

4. **Build the report** - group items by Status field value and count them. For iteration-based projects, filter to the current iteration first. Present a breakdown like:

   ```
   Project: Issue Fields (Iteration 42, Mar 2-8)
   15 actionable items:
     🎉 Done:        4 (27%)
     In Review:      3
     In Progress:    3
     Ready:          2
     Blocked:        2
   ```

5. **Add context** - if items have sub-issues, include `subIssuesSummary` counts. If items have dependencies, note blocked items and what blocks them.

## OAuth Scope Requirements

Projects V2 operations require the `project` OAuth scope. If you get a permission error, the token may lack this scope.

## Finding an Issue's Project Item ID

When you know the issue but need its project item ID (e.g., to update its Status), query from the issue side:

```bash
gh api graphql -f query='
{
  repository(owner: "OWNER", name: "REPO") {
    issue(number: 123) {
      projectItems(first: 5) {
        nodes {
          id
          project { title number }
          fieldValues(first: 10) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}' --jq '.data.repository.issue.projectItems.nodes'
```

This returns the item ID, project info, and current field values in one query.

## Project fields

**Find a project and its Status field options:**
```bash
gh api graphql -f query='
{
  organization(login: "ORG") {
    projectV2(number: 5) {
      id
      title
      field(name: "Status") {
        ... on ProjectV2SingleSelectField {
          id
          options { id name }
        }
      }
    }
  }
}' --jq '.data.organization.projectV2'
```

**List all fields (including iterations):**
```bash
gh api graphql -f query='
{
  node(id: "PROJECT_ID") {
    ... on ProjectV2 {
      fields(first: 20) {
        nodes {
          ... on ProjectV2Field { id name }
          ... on ProjectV2SingleSelectField { id name options { id name } }
          ... on ProjectV2IterationField { id name configuration { iterations { id startDate } } }
        }
      }
    }
  }
}' --jq '.data.node.fields.nodes'
```

## End-to-End Example: Set Issue Status to "In Progress"

```bash
# 1. Get the issue's project item ID, project ID, and current status
gh api graphql -f query='{
  repository(owner: "github", name: "planning-tracking") {
    issue(number: 2574) {
      projectItems(first: 1) {
        nodes { id project { id title } }
      }
    }
  }
}' --jq '.data.repository.issue.projectItems.nodes[0]'

# 2. Get the Status field ID and "In Progress" option ID
gh api graphql -f query='{
  node(id: "PROJECT_ID") {
    ... on ProjectV2 {
      field(name: "Status") {
        ... on ProjectV2SingleSelectField { id options { id name } }
      }
    }
  }
}' --jq '.data.node.field'

# 3. Update the status
gh api graphql -f query='mutation {
  updateProjectV2ItemFieldValue(input: {
    projectId: "PROJECT_ID"
    itemId: "ITEM_ID"
    fieldId: "FIELD_ID"
    value: { singleSelectOptionId: "IN_PROGRESS_OPTION_ID" }
  }) { projectV2Item { id } }
}'
```
