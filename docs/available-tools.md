# Available tools

Complete inventory of tools accessible to AI coding agents (Copilot, Cursor, Claude Code).

## Direct tools

### File & workspace

| Tool | Purpose |
|------|---------|
| `create_directory` | Create directory structure (recursive) |
| `create_file` | Create a new file with content |
| `read_file` | Read file contents (with line ranges) |
| `replace_string_in_file` | Replace a single occurrence in a file |
| `multi_replace_string_in_file` | Batch edits across files |
| `list_dir` | List directory contents |
| `view_image` | View image files (png, jpg, gif, webp) |

### Search

| Tool | Purpose |
|------|---------|
| `file_search` | Find files by glob pattern |
| `grep_search` | Text/regex search across workspace |
| `semantic_search` | Semantic code search |

### Code intelligence

| Tool | Purpose |
|------|---------|
| `get_errors` | Get compile/lint errors |
| `vscode_listCodeUsages` | Find all references/definitions of a symbol |
| `vscode_renameSymbol` | Rename a symbol across workspace |

### Terminal

| Tool | Purpose |
|------|---------|
| `run_in_terminal` | Run shell commands (sync/async) |
| `send_to_terminal` | Send input to an active terminal |
| `get_terminal_output` | Get output from an active terminal |
| `kill_terminal` | Kill a terminal session |
| `terminal_last_command` | Get last command run |
| `terminal_selection` | Get terminal selection |

### Git

| Tool | Purpose |
|------|---------|
| `get_changed_files` | Get git diffs of current changes |

### Notebook

| Tool | Purpose |
|------|---------|
| `create_new_jupyter_notebook` | Create a Jupyter notebook |
| `edit_notebook_file` | Edit notebook cells |
| `run_notebook_cell` | Execute a notebook cell |
| `read_notebook_cell_output` | Read cell output |
| `copilot_getNotebookSummary` | Get notebook cell summary |
| `configure_notebook` | Configure notebook kernel |

### Browser

| Tool | Purpose |
|------|---------|
| `open_browser_page` | Open a URL in browser |
| `read_page` | Get page accessibility snapshot |
| `screenshot_page` | Capture browser screenshot |
| `click_element` | Click an element |
| `type_in_page` | Type text / press keys |
| `hover_element` | Hover over an element |
| `drag_element` | Drag and drop |
| `navigate_page` | Navigate (URL, back, forward, reload) |
| `handle_dialog` | Handle modal/file dialogs |
| `run_playwright_code` | Run custom Playwright code |

### Web

| Tool | Purpose |
|------|---------|
| `fetch_webpage` | Fetch main content from a web page |

### GitHub

| Tool | Purpose |
|------|---------|
| `github_repo` | Semantic search in a GitHub repo |
| `github_text_search` | Keyword search in a repo/org |
| `mcp_github_create_or_update_file` | Create or update a file in a GitHub repo |
| `mcp_github_push_files` | Push multiple files in a single commit |
| `mcp_github_list_tags` | List git tags in a GitHub repo |
| `mcp_github_sub_issue_write` | Add/remove/reprioritize sub-issues |

### GitHub Actions

| Tool | Purpose |
|------|---------|
| `mcp_github-agenti_compile` | Compile markdown workflows to YAML |
| `mcp_github-agenti_fix` | Apply codemod fixes to workflow files |

### VS Code

| Tool | Purpose |
|------|---------|
| `vscode_searchExtensions_internal` | Search VS Code extensions marketplace |
| `get_vscode_api` | VS Code extension API documentation |
| `run_vscode_command` | Run a VS Code command |
| `vscode_askQuestions` | Ask user clarifying questions |
| `create_new_workspace` | Scaffold a new project |
| `get_project_setup_info` | Get project setup info |
| `create_and_run_task` | Create/run VS Code tasks |

### Memory & planning

| Tool | Purpose |
|------|---------|
| `memory` | Persistent notes across sessions |
| `resolve_memory_file_uri` | Resolve memory file paths |
| `manage_todo_list` | Track task progress |
| `runSubagent` | Launch a sub-agent for complex tasks |

### Diagrams

| Tool | Purpose |
|------|---------|
| `renderMermaidDiagram` | Render a Mermaid diagram |
| `mcp_mcp-mermaid_generate_mermaid_diagram` | Generate Mermaid diagrams (base64, svg, file) |

### Documentation

| Tool | Purpose |
|------|---------|
| `mcp_context7_query-docs` | Fetch library docs from Context7 |

### Python (Pylance)

| Tool | Purpose |
|------|---------|
| `mcp_pylance_mcp_s_pylanceInvokeRefactoring` | Automated Python refactoring |

## Activation-gated tools

Tools unlocked on demand via activation functions. Grouped by domain.

### Jupyter & Python environments

#### `activate_notebook_configuration_and_management_tools`

| Tool | Purpose |
|------|---------|
| `configure_notebook` | Configure notebook kernel |
| `configure_python_notebook` | Configure Python notebook kernel |
| `configure_non_python_notebook` | Configure non-Python notebook kernel |
| `notebook_install_packages` | Install packages in notebook kernel |
| `notebook_list_packages` | List packages in notebook kernel |
| `restart_notebook_kernel` | Restart notebook kernel |

#### `activate_python_environment_management_tools`

| Tool | Purpose |
|------|---------|
| `configure_python_environment` | Set up Python environment |
| `get_python_environment_details` | Get current env details (type, version, packages) |
| `get_python_executable_details` | Get correct Python executable path |
| `install_python_packages` | Install packages via pip/conda |

#### `activate_python_syntax_validation_tools`

| Tool | Purpose |
|------|---------|
| Pylance doc search | Search Pylance docs for configuration/troubleshooting |
| Python syntax validation | Check files for syntax errors with line numbers |
| Code snippet validation | Validate code without saving to file |

#### `activate_python_import_analysis_tools`

| Tool | Purpose |
|------|---------|
| Workspace import analysis | Identify top-level imports and missing dependencies |
| Available module listing | List installed top-level modules |

#### `activate_python_environment_management_tools_2`

| Tool | Purpose |
|------|---------|
| Active env info | Get active Python environment details |
| All available envs | List all Python environments |
| Analysis settings | Check/modify Python analysis settings |
| Environment switching | Switch Python installations or venvs |

#### `activate_workspace_structure_tools`

| Tool | Purpose |
|------|---------|
| Workspace roots | Get workspace root directories |
| User Python files | List user-created Python files (excludes libraries) |

### GitHub (MCP)

#### `activate_github_pull_request_management`

| Tool | Purpose |
|------|---------|
| Active PR details | Title, description, changed files, comments, state |
| Visible PR info | PR currently visible to user |
| CI check status | Check run statuses, workflow names, failures |

#### `activate_github_issue_and_notification_tools`

| Tool | Purpose |
|------|---------|
| Issue/PR details | Structured JSON of issue or PR |
| Repo labels | All labels for a repository |
| Notifications | GitHub notification details |

#### `activate_github_review_comment_tools`

| Tool | Purpose |
|------|---------|
| `mcp_github_add_comment_to_pending_review` | Add comment to pending PR review |
| `mcp_github_add_issue_comment` | Comment on issue or PR |

#### `activate_pull_request_management_tools`

| Tool | Purpose |
|------|---------|
| Create branch | Create a new branch |
| Create PR | Initiate a pull request |
| Delegate to Copilot | Delegate PR task to Copilot |
| List PRs | List existing pull requests |
| Merge PR | Merge a pull request |
| Update PR | Update PR content |

#### `activate_github_repository_management_tools`

| Tool | Purpose |
|------|---------|
| `mcp_github_get_commit` | Get commit details |
| `mcp_github_get_file_contents` | View file/directory contents |
| `mcp_github_list_issues` | List repo issues |
| `mcp_github_issue_write` | Create or update issues |
| Repo labels | List repository labels |
| Releases | Get release info |
| User info | Get GitHub user details |

#### `activate_release_management_tools`

| Tool | Purpose |
|------|---------|
| Get release by tag | Retrieve a specific release |
| List tags | List all tags in a repo |

#### `activate_search_and_discovery_tools`

| Tool | Purpose |
|------|---------|
| Search code | Search for code across GitHub |
| Search repos | Search repositories |
| Search users | Search GitHub users |

#### `activate_branch_and_commit_tools`

| Tool | Purpose |
|------|---------|
| List branches | List all branches in a repo |
| List commits | Get commits for a branch |

#### `activate_github_workflow_management_tools`

| Tool | Purpose |
|------|---------|
| Add workflows | Import workflows from remote repos |
| Audit | Investigate workflow runs (jobs, errors, warnings) |
| Compile | Convert markdown workflows to YAML |
| Fix | Apply codemod fixes for deprecated fields |
| Logs | Download and analyze workflow logs |
| Inspect | Inspect MCP server capabilities |
| Status | Check workflow status |
| Update | Update workflows to latest versions |

### Git (local)

#### `activate_commit_management_tools`

| Tool | Purpose |
|------|---------|
| `git add` | Stage changes for commit |
| `git commit` | Record changes with message |
| `git log` / `git diff` | Show commit logs or compare commits |

#### `activate_branch_and_workflow_tools`

| Tool | Purpose |
|------|---------|
| `git branch` | List or create branches |
| `git switch` / `git checkout` | Switch branches or restore files |
| `git pull` | Fetch and integrate remote changes |
| `git stash` | Stash dirty working directory |
| `git status` | Show working tree status |

#### `activate_repository_interaction_tools`

| Tool | Purpose |
|------|---------|
| `git fetch` | Download objects/refs from remote |
| `git push` | Update remote refs |
| Get file content | Access files from a repository |

### GitLens / PR workflow

#### `activate_pull_request_management_tools_2`

| Tool | Purpose |
|------|---------|
| Commit Composer | Organize changes into coherent commits |
| Launchpad | Prioritize open PRs by status |
| PR worktree review | Create worktree for AI-assisted review |
| Start Work | Link branch to issue |

#### `activate_issue_commenting_tools`

| Tool | Purpose |
|------|---------|
| Add issue comment | Comment on an issue |
| Get PR comments | Retrieve PR discussion |
| Get PR details | Detailed PR information |

#### `activate_issue_and_pull_request_management_tools`

| Tool | Purpose |
|------|---------|
| Fetch assigned issues | Issues assigned to the user |
| Search authored/assigned PRs | PRs by author or assignee |

### Atlassian — Confluence

#### `activate_confluence_content_management_tools`

| Tool | Purpose |
|------|---------|
| Create/update page | Create or edit Confluence pages |
| Move page | Reorganize page hierarchy |
| Add comment / reply | Page-level and inline comments |
| Add labels | Categorize pages |
| Page history | View change history and diffs |
| Manage attachments | Upload/update/retrieve files |

#### `activate_confluence_attachment_management_tools`

| Tool | Purpose |
|------|---------|
| Download attachment | Get individual attachment (base64) |
| Download all | Get all attachments for a content item |
| List attachments | Metadata: size, type, dates |
| Upload attachments | Bulk upload files |
| Get images | Filter attachments to images only |

### Atlassian — Jira

#### `activate_jira_issue_management_tools`

| Tool | Purpose |
|------|---------|
| Create/update/delete issues | Full issue CRUD |
| Bulk create | Create multiple issues at once |
| Add to sprint | Assign issues to sprints |
| Link issues | Create relationships between issues |
| Add comments/watchers | Collaborate on issues |
| Get worklogs | Retrieve time tracking data |

#### `activate_jira_search_and_filter_tools`

| Tool | Purpose |
|------|---------|
| JQL search | Search issues with Jira Query Language |
| Confluence CQL search | Search Confluence content |
| User search | Find users |

#### `activate_jira_comment_and_link_tools`

| Tool | Purpose |
|------|---------|
| Add comment | Comment on issues with visibility control |
| Create link | Link issues (dependencies, blocks, etc.) |

#### `activate_jira_version_management_tools`

| Tool | Purpose |
|------|---------|
| Create version | Create a release version |
| Batch create | Create multiple versions |
| List versions | Get all versions for a project |

#### `activate_jira_sprint_management_tools`

| Tool | Purpose |
|------|---------|
| Create sprint | Create a new sprint |
| Get sprints | Retrieve sprints (filter by state) |
| Update sprint | Modify sprint details |

#### `activate_jira_issue_date_and_status_tools`

| Tool | Purpose |
|------|---------|
| Issue dates | Creation, update, resolution dates |
| Status history | Track status transitions and time in each |

#### `activate_jira_proforma_form_management_tools`

| Tool | Purpose |
|------|---------|
| Get form | Retrieve ProForma form details |
| Update form | Update form field answers |

#### `activate_jira_service_desk_management_tools`

| Tool | Purpose |
|------|---------|
| Get service desk | Retrieve associated service desk |
| Manage queues | Access request queues |

### Notion

#### `activate_notion_comment_management`

| Tool | Purpose |
|------|---------|
| `notion-create-comment` | Add page or block comments, reply to threads |
| `notion-get-comments` | Retrieve comments in XML format |

#### `activate_notion_database_and_page_management`

| Tool | Purpose |
|------|---------|
| `notion-create-database` | Define database with SQL DDL syntax |
| `notion-create-pages` | Create pages with properties and content |
| `notion-fetch` | Retrieve entity details |

#### `activate_notion_view_management`

| Tool | Purpose |
|------|---------|
| `notion-create-view` | Create table, board, calendar views |
| `notion-update-view` | Modify existing views |

#### `activate_notion_team_and_user_management`

| Tool | Purpose |
|------|---------|
| `notion-get-teams` | List teams, membership, roles |
| `notion-get-users` | List workspace members and guests |

### macOS automation (Peekaboo)

#### `activate_ui_interaction_tools`

| Tool | Purpose |
|------|---------|
| `mcp_peekaboo_click` | Click UI elements or coordinates |
| `mcp_peekaboo_drag` | Drag and drop operations |
| `mcp_peekaboo_hotkey` | Simulate keyboard shortcuts |
| `mcp_peekaboo_menu` | Interact with application menus |
| `mcp_peekaboo_move` | Precise cursor positioning |
| `mcp_peekaboo_scroll` | Scroll through content |
| `mcp_peekaboo_see` | Capture UI state and element map |
| `mcp_peekaboo_swipe` | Gesture-based interactions |
| `mcp_peekaboo_type` | Text input |

#### `activate_clipboard_management_tools`

| Tool | Purpose |
|------|---------|
| `mcp_peekaboo_clipboard` | Get/set/clear/save/restore clipboard |
| `mcp_peekaboo_paste` | Atomic set → paste → restore |

#### `activate_window_and_space_management_tools`

| Tool | Purpose |
|------|---------|
| `mcp_peekaboo_space` | List/switch/move windows across Spaces |
| `mcp_peekaboo_window` | Close/minimize/maximize/move/resize/focus windows |

### Web research

#### `activate_context7_documentation_tools`

| Tool | Purpose |
|------|---------|
| `mcp_context7_resolve-library-id` | Resolve package name to Context7 library ID |
| `mcp_context7_query-docs` | Fetch current docs and code examples |

#### `activate_web_content_extraction_and_research_tools`

| Tool | Purpose |
|------|---------|
| `mcp_tavily_tavily_search` | Web search for current information |
| `mcp_tavily_tavily_extract` | Extract raw content from URLs |
| `mcp_tavily_tavily_crawl` | Crawl website from a starting URL |
| `mcp_tavily_tavily_map` | Map website structure (URL listing) |
| `mcp_tavily_tavily_research` | In-depth multi-source research |

#### `activate_web_navigation_tools`

| Tool | Purpose |
|------|---------|
| Browser click | Click elements in browser |
| Browser navigate | Navigate to URLs |
| Keyboard input | Press keys in browser |

#### `activate_web_capture_tools`

| Tool | Purpose |
|------|---------|
| Screenshot | Capture current browser view |
| Accessibility snapshot | Capture layout with element references |

### Code intelligence (codebase-memory)

Persistent knowledge graph built from tree-sitter AST analysis (66 languages). **Prefer these tools over grep/read for structural code questions** — call chains, architecture, impact analysis.

| Tool | Purpose |
|------|---------|
| `index_repository` | Index a repo into the knowledge graph |
| `list_projects` | List all indexed projects with node/edge counts |
| `search_graph` | Structured search by label, name pattern, file pattern |
| `trace_call_path` | BFS traversal — who calls a function and what it calls |
| `detect_changes` | Map git diff to affected symbols with risk classification |
| `query_graph` | Execute Cypher-like graph queries (read-only) |
| `get_graph_schema` | Node/edge counts, relationship patterns. Run this first. |
| `get_code_snippet` | Read source code for a function by qualified name |
| `get_architecture` | Codebase overview: languages, packages, routes, hotspots, clusters |
| `search_code` | Grep-like text search within indexed project files |
| `manage_adr` | CRUD for Architecture Decision Records |
| `delete_project` | Remove a project and its graph data |
| `index_status` | Check indexing status of a project |
| `ingest_traces` | Ingest runtime traces to validate HTTP_CALLS edges |

### File conversion (markitdown)

Convert any file format to Markdown for LLM consumption. **Use this instead of reading binary files directly** — supports PDF, DOCX, PPTX, XLSX, HTML, images, audio, CSV, JSON, XML, ZIP, YouTube URLs, EPubs, and more.

| Tool | Purpose |
|------|---------|
| `convert_to_markdown` | Convert a URI (file://, http://, https://, data:) to Markdown |

### Cloud (GCP)

#### `activate_billing_management_tools`

| Tool | Purpose |
|------|---------|
| Billing budgets | Current budget limits |
| Billing info | Detailed billing data |
| Cost forecasts | Predicted costs based on usage |

#### `activate_project_management_tools`

| Tool | Purpose |
|------|---------|
| List GKE clusters | Clusters in selected project |
| List GCP projects | All accessible projects |
| Select project | Set active project for subsequent calls |
