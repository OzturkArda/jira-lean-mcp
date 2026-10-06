# jira-lean

**A token-efficient MCP server for Jira and Confluence Cloud.**

jira-lean gives an AI agent (Claude Code, Claude Desktop, Cursor, or any
[Model Context Protocol](https://modelcontextprotocol.io) client) the everyday Jira and
Confluence operations it needs: reading issues, searching, commenting, logging work,
transitioning, and reading and editing pages. Every response is shaped for a model's
context window, not passed through from the REST API:

- **Reads are field-restricted and flattened.** `status` becomes `"In Progress"`, not a
  nested object with a `self` link, an icon URL and a status category. Avatars, `self`
  links and `expand` hints are dropped. Atlassian Document Format (ADF) bodies are
  rendered to compact markdown.
- **Writes return identifiers only.** Adding a comment returns `{id, url}`, not the
  comment echoed back with two author objects and four avatar sizes each. Creating a
  Confluence page does not echo the whole body you just sent.
- **Edits are granular.** Renaming a page, moving it, labelling it, or appending one
  table row never sends the page body through the model's context.

## Why lean output matters

Whatever a tool returns lands in the model's context. You pay for it once on arrival,
and again on every later turn it stays resident. A pass-through server that hands over
raw REST JSON makes a status check cost thousands of tokens, and the cost compounds
over a long session.

## Measured savings

Up to **~98% fewer tokens** on the most common calls, measured against real Jira and
Confluence data. Each jira-lean tool was compared with the equivalent call through
Atlassian's Rovo MCP server, which at the time of measurement (mid-2026) passed raw
REST payloads through verbatim.

**Methodology.** The size of each tool *result* was measured on the same real issues
and pages through both servers, and converted to tokens at ~4 characters per token for
dense JSON. The reduction ratio does not depend on that divisor. Where the pass-through
server can be tuned with a `fields` parameter, a row narrowed to jira-lean's exact field
set is included, so the comparison is not stacked against an untuned baseline.

### Reads (tokens per call)

| Operation | Pass-through | jira-lean | Reduction |
| --- | ---: | ---: | ---: |
| Get one issue (default fields) | 4,019 | 73 | **98.2%** |
| ↳ same, pass-through narrowed to jira-lean's 6 fields | 535 | 73 | 86.5% |
| Get one issue with its description | 19,079 | 4,509 | 76.4% |
| Review recent comments on an issue ¹ | 84,121 | 1,592 | **98.1%** |
| JQL search, 10 issues | 49,196 | 671 | **98.6%** |
| ↳ same, pass-through narrowed to jira-lean's 6 fields | 6,151 | 671 | 89.1% |
| List an issue's transitions | 1,110 | 99 | 91.1% |
| Get a Confluence page ² | 3,837 | 2,611 | 32.0% |
| Confluence CQL search, 5 results | 1,943 | 353 | 81.8% |

¹ Not like-for-like in scope. Without a comments tool, reading the discussion means
fetching the issue's `comment` field, which returns all 43 comments as ADF. jira-lean
paginated to the 5 most recent. Reading all 43 through jira-lean was ~25,049 tokens,
still ~70% less.
² The weakest case, reported as measured. The pass-through returns storage XHTML,
jira-lean returns markdown, and for prose-heavy pages the two are close in size.

### Writes (tokens in the response)

| Operation | Pass-through (echo) | jira-lean | Reduction |
| --- | ---: | ---: | ---: |
| Add a comment | ~1,300 | 25 | ~98% |
| Add a worklog | ~775 | 5 | ~99% |
| Create a Confluence page | ~5,575 | 23 | ~99.6% |
| Edit an issue | ~2,150 | 14 | ~99% |
| Update a Confluence page | ~200 | 9 | ~96% |
| Transition an issue | ~75 | 8 | ~89% |

The pass-through figures are averages over earlier real calls; jira-lean's are fixed-shape
returns (the comment figure was measured live). Not every pass-through write is heavy:
updating a page and transitioning were already cheap.

### Granular edits (characters per task, arguments + result)

These tasks used to require reading and resending a whole object. The metric is what
reaches the agent: the JSON of the call's arguments plus the JSON of its result.

| Task | Whole-object | jira-lean | Reduction |
| --- | ---: | ---: | ---: |
| Rename 6 Confluence pages (add a `[Draft]` prefix) | 63,762 | 950 | 98.5% |
| Append a row to a Confluence table | 36,488 | 214 | 99.4% |
| Read 19 worklogs on one issue ³ | 63,533 | 15,122 | 76.2% |
| Get one issue (default fields) | 8,422 | 317 | 96.2% |
| Read 10 issues in one call | 3,381 | 2,758 | 18.4% |

³ The baseline is also wrong, not just larger: the issue's `worklog` field stops at 20
entries without saying so. `jira_get_worklogs` pages to the end.

Across one developer's typical week of Jira and Confluence calls, projected arrival
tokens fell by **~86%**. That projection mixes measured per-call sizes with one person's
historical call mix, so treat it as illustrative.

### Caveats

- Token counts are estimates (characters ÷ 4). The ratios are exact relative to each
  other.
- The figures come from one Atlassian site's real content. Your issues and pages will
  differ: long descriptions and prose-heavy pages narrow the gap, because that text is
  signal both servers have to carry.
- The comparison is against a pass-through server as it behaved in mid-2026. If it has
  since started shaping its output, the gap will be smaller.
- jira-lean deliberately covers the hot path, not the whole API (see
  [Limitations](#limitations)).

### Reproduce it offline

`benchmarks/compare.py` runs the same comparison on **synthetic** payloads shaped like
real Atlassian REST responses, through the real tools, with no network and no
credentials:

```console
$ python benchmarks/compare.py
| Scenario | Raw chars | jira-lean chars | Reduction |
| --- | ---: | ---: | ---: |
| Get one issue (default fields) | 3,600 | 275 | 92.4% |
| Get one issue, raw request narrowed to the same 6 fields | 1,837 | 275 | 85.0% |
| JQL search, 10 issues | 36,050 | 2,244 | 93.8% |
| Read an issue's 20 comments | 33,738 | 3,859 | 88.6% |
| Add a comment (response only) | 1,692 | 92 | 94.6% |
| Create a Confluence page (response only) | 2,708 | 92 | 96.6% |
| Read a Confluence page as markdown | 2,708 | 624 | 77.0% |
| **Total** | **82,333** | **7,461** | **90.9%** |
```

The synthetic data is smaller than a real site's (short descriptions, one component,
no custom fields), so these ratios are conservative for Jira reads. The Confluence page
fixture is macro-heavy, which flatters the markdown read compared with the 32%
measured on a real prose page. `tests/test_benchmark.py` runs this in CI so the claim
cannot quietly stop being true.

To check your own site, compare a raw REST call with the matching tool result:

```bash
curl -s -u "$ATLASSIAN_EMAIL:$ATLASSIAN_API_TOKEN" \
  "$ATLASSIAN_SITE/rest/api/3/issue/PROJ-123" | wc -c
```

## Install

Requires Python 3.10+.

```bash
# run without installing (uv)
uvx --from git+https://github.com/OzturkArda/jira-lean-mcp jira-lean-mcp

# or install into an environment
pip install git+https://github.com/OzturkArda/jira-lean-mcp

# or from a clone, for development
git clone https://github.com/OzturkArda/jira-lean-mcp
cd jira-lean-mcp
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip install -e ".[dev]"
```

Each of these provides a `jira-lean-mcp` command that runs the server over stdio.
`python -m jira_lean_mcp` does the same.

## Configure

### 1. Mint an Atlassian API token

1. Sign in at <https://id.atlassian.com/manage-profile/security/api-tokens>.
2. Choose **Create API token**, give it a label (e.g. `jira-lean`), and pick an expiry.
3. Copy the token. It is shown once.

The token acts as **your account**: it can see and change exactly what you can in the
UI, no more. Tokens expire (a year at most). When calls start failing with `401`, mint a
new one and replace it; nothing else needs to change.

> Scoped API tokens use a different base URL (`api.atlassian.com/ex/jira/<cloud-id>`)
> and are not supported yet. Use a classic (unscoped) token.

### 2. Set the environment variables

| Variable | Required | Description |
| --- | :---: | --- |
| `ATLASSIAN_SITE` | yes | Your site, e.g. `https://your-company.atlassian.net` (the `https://` is optional). |
| `ATLASSIAN_EMAIL` | yes | The email address of the Atlassian account that owns the token. |
| `ATLASSIAN_API_TOKEN` | yes | The API token from step 1. |
| `JIRA_LEAN_READ_ONLY` | no | `true` refuses every write (comments, edits, transitions, page changes) before anything is sent. Default `false`. |
| `JIRA_LEAN_DEFAULT_FIELDS` | no | Comma-separated Jira fields returned when a call names none. Default `summary,status,issuetype,assignee,priority,updated`. |
| `JIRA_LEAN_TIMEOUT` | no | HTTP timeout in seconds. Default `30`. |
| `JIRA_LEAN_ENV_FILE` | no | Path to a dotenv file to read the variables above from. Real environment variables take precedence over it. |

The usual place for these is the `env` block of your MCP client's server config (below).
To keep the token out of client config, put it in a file (see
[`.env.example`](.env.example)) and point `JIRA_LEAN_ENV_FILE` at it.

Credentials are read on the first tool call, not at startup, so a missing setting shows
up as a clear error from that call.

## Connect an MCP client

### Claude Code

```bash
claude mcp add jira-lean --scope user \
  -e ATLASSIAN_SITE=https://your-company.atlassian.net \
  -e ATLASSIAN_EMAIL=you@example.com \
  -e ATLASSIAN_API_TOKEN=your-token \
  -- uvx --from git+https://github.com/OzturkArda/jira-lean-mcp jira-lean-mcp
```

`--scope user` makes it available in every project. If you installed with pip, replace
everything after `--` with the full path to the `jira-lean-mcp` executable.

### Claude Desktop, Cursor, Windsurf, VS Code and other JSON-configured clients

Most clients take the same `mcpServers` shape: Claude Desktop in
`claude_desktop_config.json`, Cursor in `~/.cursor/mcp.json`, Windsurf in
`~/.codeium/windsurf/mcp_config.json`.

```json
{
  "mcpServers": {
    "jira-lean": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/OzturkArda/jira-lean-mcp", "jira-lean-mcp"],
      "env": {
        "ATLASSIAN_SITE": "https://your-company.atlassian.net",
        "ATLASSIAN_EMAIL": "you@example.com",
        "ATLASSIAN_API_TOKEN": "your-token"
      }
    }
  }
}
```

VS Code (`.vscode/mcp.json`) uses `"servers"` as the top-level key in place of
`"mcpServers"`, with the same entry inside.

If you also run another Atlassian MCP server, a line in your agent instructions (e.g.
`CLAUDE.md`) such as *"prefer jira-lean for Jira/Confluence reads and writes; use the
other server only for operations jira-lean lacks"* keeps the agent on the cheap path.

## Tools

### Jira

Reads (shaped, markdown bodies, no API noise):

- `jira_get_issue(key, fields?)`: the default fields are summary, status, issuetype, assignee, priority and updated; add `"description"` explicitly when you need it.
- `jira_get_issues(keys, fields?)`: many issues in one call, chunked at 100. Returns {issues (in the order asked for), missing}.
- `jira_get_comments(key, limit?, oldest_first?, start?)`: the comment trail as {id, author, created, body}. `limit` is capped at 50 per call; page with `start`.
- `jira_search(jql, fields?, limit?, next_page_token?)`: JQL search with cursor pagination.
- `jira_get_worklogs(key, author?, started_after?, started_before?)`: every worklog, paged to the end. Dates are `YYYY-MM-DD`, inclusive, and read in the offset the work was logged with.
- `jira_get_transitions(key)`: [{id, name, to}].
- `jira_get_link_types()`: [{id, name, inward, outward}].
- `jira_lookup_account(query)`: [{account_id, display_name, email}].
- `jira_get_field_ids(project, issue_type)`: [{id, name, type, required}]. Use it to look up a custom field's `customfield_NNNNN` id instead of hardcoding it.

Writes (return only ids and urls, no echo):

- `jira_add_comment(key, body_markdown)` → {id, url}
- `jira_update_comment(key, comment_id, body_markdown)` → {id}
- `jira_add_worklog(key, time_spent, started?, comment_markdown?)` → {id}
- `jira_edit_issue(key, fields, markdown_fields?)` → {ok, url}. `markdown_fields` defaults to `["description"]`; name any other rich-text field there, or it is stored as literal markdown.
- `jira_transition(key, transition)`: takes a transition id or name → {ok, status}.
- `jira_link_issues(from_key, to_key, link_type)` → {ok}. Reads in the outward direction: with `"Blocks"`, from_key blocks to_key.

Transition, link-type and issue-type names are resolved against what your site defines,
so nothing is hardcoded to a particular workflow. An unknown name errors with the list
of valid ones.

### Confluence

Reads:

- `confluence_get_page(page_id, body_format?)`: {id, title, version, url, body}.
- `confluence_get_labels(page_id)`: {labels}.
- `confluence_get_space(space_key)`: {id, key, name}.
- `confluence_get_children(page_id, depth?)`: [{id, title, parent_id}].
- `confluence_search(cql, limit?)`: [{id, title, url, lastModified}].

Writes (return {id, title, version} unless noted):

- `confluence_create_page(space_id, title, body_markdown, parent_id?, body_format?)` → {id, url}. `space_id` also accepts a space key.
- `confluence_upsert_page(space_id_or_key, title, body_markdown, ...)` → {id, url, version, action}. Creates the page, or replaces the one with that title; refuses to write if the title is ambiguous.
- `confluence_update_page(page_id, body_markdown?, title?, version_message?, body_format?, expected_version?)`: omit the body to change only metadata.
- `confluence_rename_page(page_id, title, ...)`: changes the title only; the body is untouched.
- `confluence_move_page(page_id, parent_id, ...)`: reparents the page; title and body are untouched.
- `confluence_append_to_page(page_id, body_markdown, ...)`: appends a fragment without resending the page.
- `confluence_append_table_row(page_id, cells, table_index?, ...)`: splices one row into the addressed table.
- `confluence_add_labels(page_id, labels)` / `confluence_remove_label(page_id, label)` → {labels}, the full resulting set. No page version is created.
- `confluence_delete_page(page_id, purge?)` → {id, status}. Moves the page to the trash by default; `purge=True` deletes it permanently.

Bodies are GitHub-flavored markdown in and out. Jira writes convert markdown to wiki
markup (REST v2); Jira reads render ADF to markdown (REST v3).

### Confluence bodies: markdown is an interface, storage is the truth

Confluence stores XHTML ("storage format"). Markdown cannot express panels, status
lozenges, expands, layouts, task lists, media references, or the
`ac:inline-comment-marker` elements that inline comments are anchored to. So
`body_format="markdown"` is a one-way rendering for reading, and writing that rendering
back would strip all of them.

The server enforces two rules that follow from this:

- **A write that is not given a new body resends the stored bytes untouched.** No
  markdown conversion runs in either direction, so `confluence_rename_page`,
  `confluence_move_page`, the label tools and `confluence_update_page(page_id,
  title=...)` cannot damage a page, however it was authored.
- **To edit a body authored in Confluence, read and write `body_format="storage"`.**
  That path is byte-for-byte reversible. Reading markdown, editing it and writing it
  back is not: it silently drops whatever markdown has no syntax for.

`expected_version` on any Confluence write aborts before writing if the page has moved
on since it was read, instead of overwriting the other writer.

## Notes

- Jira comment and worklog writes use REST **v2**, because it accepts wiki markup
  strings directly; reads use v3 and render ADF. Confluence uses API v2, except for
  labels: v2 has no label write, so those use v1 (`/wiki/rest/api`).
- Errors name the cause and the object. A 401 points at the token, a 404 says which
  object was not found, and a 409 gives both version numbers.
- The markdown-to-wiki converter handles two Jira wiki-markup traps that render wrong
  rather than erroring. An empty table cell must contain a space (`|| ||`, not `||||`,
  which drops the column). A brace inside `{{ }}` monospace must be a character entity
  (`&#123;`), because backslash escaping does not work. Braces in *prose* are left
  alone, since escaping them would break wiki macros written on purpose; put `${VAR}` in
  backticks.
- Two things Confluence does that no client can override: it strips `data-local-id` on
  every API write, even one that resends a byte-identical body (inline comments survive,
  because they are anchored by `ac:inline-comment-marker`, which is kept); and label
  writes never create a page version.

## Limitations

- **Atlassian Cloud only.** Jira Server / Data Center are not supported (different
  APIs and auth).
- **Hot path, not the whole API.** There are no tools for creating issues, Confluence
  inline or footer comments, attachments, boards or sprints. Pair jira-lean with another
  Atlassian MCP server if you need those.
- **Basic auth with a classic API token.** OAuth and scoped tokens are not implemented.
- **Markdown conversion is pragmatic, not complete.** Common GitHub-flavored markdown
  converts cleanly; exotic constructs may not. Use `body_format="storage"` for exact
  Confluence content.
- **One site per server instance.** Register the server twice under different names to
  use two sites.
- Built on the `mcp` Python SDK 1.x (`mcp>=1.28,<2`).

## Security

- The API token has your account's full permissions. Prefer `JIRA_LEAN_READ_ONLY=true`
  for agents that only need to read.
- There is no confirmation step: if the agent calls a write or delete tool, it runs.
  `confluence_delete_page` only moves the page to the trash unless `purge=True` is
  passed.
- Never commit a real `.env`; `.gitignore` excludes it.

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q             # offline: every tool runs against a stub HTTP transport
python benchmarks/compare.py    # offline: raw vs lean on synthetic payloads
```

`tests/test_server.py` calls every tool directly against a stub `httpx` transport and
asserts on the request each one builds. Storage fidelity is checked by byte comparison
against `tests/fixtures/page_rich.storage.html`, a synthetic page carrying an info
panel, a status lozenge, an expand, a task list, a layout, an attachment reference and
an inline-comment marker. No test touches the network.

## License

[MIT](LICENSE) © OzturkArda
