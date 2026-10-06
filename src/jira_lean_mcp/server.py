"""jira-lean — token-efficient Jira/Confluence MCP server.

Talks directly to the Atlassian Cloud REST API and returns minimal, shaped
responses: reads are field-restricted and rendered to markdown; writes return
only {id, url}.

Configuration comes from environment variables (see README / .env.example),
optionally loaded from the dotenv file named by JIRA_LEAN_ENV_FILE. Nothing is
read until the first API call, so importing this module needs no credentials.
"""

import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from markdownify import markdownify
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------- config

REQUIRED_SETTINGS = ("ATLASSIAN_SITE", "ATLASSIAN_EMAIL", "ATLASSIAN_API_TOKEN")
_DEFAULT_FIELDS = "summary,status,issuetype,assignee,priority,updated"


def _read_env_file(path: Path) -> dict:
    cfg = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_settings(environ: dict | None = None) -> dict:
    """Resolve settings. Real environment variables win over the env file, so an MCP
    client's `env` block always has the last word."""
    environ = dict(os.environ if environ is None else environ)
    cfg: dict = {}
    env_file = environ.get("JIRA_LEAN_ENV_FILE", "").strip()
    if env_file:
        path = Path(env_file).expanduser()
        if not path.is_file():
            raise RuntimeError(f"jira-lean: JIRA_LEAN_ENV_FILE points at {path}, which does not exist")
        cfg.update(_read_env_file(path))
    cfg.update({k: v for k, v in environ.items() if v != ""})
    missing = [k for k in REQUIRED_SETTINGS if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"jira-lean: missing configuration {missing}. Set them as environment "
                           f"variables (e.g. in your MCP client's server config) or in the file "
                           f"named by JIRA_LEAN_ENV_FILE. See .env.example.")
    site = cfg["ATLASSIAN_SITE"].strip().rstrip("/")
    if not re.match(r"^https?://", site):
        site = "https://" + site
    return {
        "site": site,
        "email": cfg["ATLASSIAN_EMAIL"].strip(),
        "token": cfg["ATLASSIAN_API_TOKEN"].strip(),
        "timeout": float(cfg.get("JIRA_LEAN_TIMEOUT") or 30),
        "default_fields": [f.strip() for f in (cfg.get("JIRA_LEAN_DEFAULT_FIELDS") or _DEFAULT_FIELDS)
                           .split(",") if f.strip()],
        "read_only": _truthy(cfg.get("JIRA_LEAN_READ_ONLY", "")),
    }


_settings: dict | None = None
_client: httpx.Client | None = None


def _config() -> dict:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def _site() -> str:
    return _config()["site"]


def _http() -> httpx.Client:
    global _client
    if _client is None:
        cfg = _config()
        _client = httpx.Client(
            base_url=cfg["site"],
            auth=(cfg["email"], cfg["token"]),
            headers={"Accept": "application/json"},
            timeout=cfg["timeout"],
        )
    return _client




class AtlassianError(RuntimeError):
    """An HTTP error from the Atlassian API, with the status code kept accessible
    so callers can branch on it instead of parsing the message."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


_TOKEN_HELP = ("ATLASSIAN_API_TOKEN in .env has expired or been revoked — mint a new one at "
               "id.atlassian.com/manage-profile/security/api-tokens and replace it (see README)")


def _req(method: str, path: str, *, context: str = "", **kw) -> httpx.Response:
    """Call the API, mapping failures to a message that names the cause and the object.

    context names the object being acted on (e.g. "Confluence page 123"); it is what
    the 403/404/409 messages point at."""
    what = context or f"{method} {path}"
    if method != "GET" and _config()["read_only"]:
        raise PermissionError(f"refused {method} ({what}): JIRA_LEAN_READ_ONLY is set, so this "
                              f"server only reads. Nothing was sent.")
    r = _http().request(method, path, **kw)
    if r.status_code < 400:
        return r
    if r.status_code == 401:
        msg = f"401 unauthorised ({what}): {_TOKEN_HELP}"
    elif r.status_code == 403:
        msg = (f"403 permission denied ({what}): the account is authenticated but not allowed to "
               f"{method} this object")
    elif r.status_code == 404:
        msg = f"404 not found ({what}): it does not exist, or the account cannot see it"
    elif r.status_code == 409:
        msg = (f"409 version conflict ({what}): it changed since it was read — "
               f"re-read the current version and retry")
    else:
        msg = f"HTTP {r.status_code} ({what})"
    # Keep the API's own message: it carries the field-level detail this cannot infer.
    raise AtlassianError(f"{msg}. API said: {r.text[:300].strip()}", r.status_code)


def _page_all(path: str, key: str, params: dict | None = None, context: str = "",
              page_size: int = 100) -> list:
    """Collect every item from a startAt/maxResults-paged Jira endpoint. key is the
    response field holding the batch (e.g. "worklogs", "values")."""
    items, start = [], 0
    while True:
        r = _req("GET", path, params={**(params or {}), "startAt": start, "maxResults": page_size},
                 context=context).json()
        batch = r.get(key) or []
        items.extend(batch)
        start += len(batch)
        total = r.get("total")
        if not batch or r.get("isLast") or (total is not None and start >= total):
            return items


# ---------------------------------------------------------------- ADF -> markdown

def _adf_text(node: dict) -> str:
    text = node.get("text", "")
    for mark in node.get("marks", []):
        t = mark.get("type")
        if t == "strong":
            text = f"**{text}**"
        elif t == "em":
            text = f"*{text}*"
        elif t == "code":
            text = f"`{text}`"
        elif t == "strike":
            text = f"~~{text}~~"
        elif t == "link":
            text = f"[{text}]({mark.get('attrs', {}).get('href', '')})"
    return text


def _adf_children(node: dict, sep: str = "") -> str:
    return sep.join(adf_to_md(c) for c in node.get("content", []))


def adf_to_md(node) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    t = node.get("type", "")
    if t == "doc":
        return _adf_children(node, "\n\n").strip()
    if t == "paragraph":
        return _adf_children(node)
    if t == "text":
        return _adf_text(node)
    if t == "heading":
        return "#" * node.get("attrs", {}).get("level", 1) + " " + _adf_children(node)
    if t == "bulletList":
        return "\n".join("- " + adf_to_md(li).replace("\n", "\n  ") for li in node.get("content", []))
    if t == "orderedList":
        return "\n".join(f"{i}. " + adf_to_md(li).replace("\n", "\n   ")
                         for i, li in enumerate(node.get("content", []), 1))
    if t == "listItem":
        return _adf_children(node, "\n")
    if t == "codeBlock":
        lang = node.get("attrs", {}).get("language", "") or ""
        return f"```{lang}\n{_adf_children(node)}\n```"
    if t == "blockquote":
        return "\n".join("> " + ln for ln in _adf_children(node, "\n").splitlines())
    if t == "rule":
        return "---"
    if t == "hardBreak":
        return "\n"
    if t == "table":
        rows = []
        for i, row in enumerate(node.get("content", [])):
            cells = [adf_to_md(c).replace("\n", " ").strip() for c in row.get("content", [])]
            rows.append("| " + " | ".join(cells) + " |")
            if i == 0:
                rows.append("|" + "---|" * len(cells))
        return "\n".join(rows)
    if t in ("tableCell", "tableHeader"):
        return _adf_children(node, " ")
    if t == "mention":
        return "@" + node.get("attrs", {}).get("text", "").lstrip("@")
    if t == "emoji":
        return node.get("attrs", {}).get("shortName", "")
    if t == "inlineCard":
        url = node.get("attrs", {}).get("url", "")
        return f"<{url}>"
    if t == "status":
        return f"[{node.get('attrs', {}).get('text', '')}]"
    if t == "date":
        return node.get("attrs", {}).get("timestamp", "")
    if t == "panel":
        return "\n".join("> " + ln for ln in _adf_children(node, "\n\n").splitlines())
    if t in ("mediaGroup", "mediaSingle", "media"):
        return "(attachment)"
    if t in ("expand", "nestedExpand"):
        title = node.get("attrs", {}).get("title", "")
        return f"**{title}**\n{_adf_children(node, chr(10) + chr(10))}"
    if t == "taskList":
        return _adf_children(node, "\n")
    if t == "taskItem":
        mark = "x" if node.get("attrs", {}).get("state") == "DONE" else " "
        return f"- [{mark}] " + _adf_children(node)
    return _adf_children(node, "\n")


def _maybe_adf(value):
    """Render ADF dicts to markdown; pass everything else through."""
    if isinstance(value, dict) and value.get("type") == "doc":
        return adf_to_md(value)
    return value


# ---------------------------------------------------------------- markdown -> Jira wiki markup

_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_LIST_MARKER = re.compile(r"^(\s*)(?:[-*]|\d+\.)\s+\S")


def _split_md_row(line: str) -> list[str]:
    """Cells of a markdown table row. Splits on unescaped pipes and removes only the
    outer delimiters, so an empty leading or trailing cell keeps its column. An escaped
    \\| stays escaped — Jira reads it the same way markdown does."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _wiki_row(cells: list[str], header: bool = False) -> str:
    """A Jira wiki table row. An empty cell is emitted as a space: adjacent delimiters
    collapse and the column is dropped from the rendered table."""
    sep = "||" if header else "|"
    return sep + sep.join(c if c.strip() else " " for c in cells) + sep


def _indent_unit(md: str) -> int:
    """Spaces per nesting level, inferred from the document's own smallest list
    indent — markdown authors use 2, 3 or 4. Falls back to 2 when nothing is nested."""
    widths, in_code = set(), False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = _LIST_MARKER.match(line.replace("\t", "    "))
        if m and m.group(1):
            widths.add(len(m.group(1)))
    return min(widths) if widths else 2


def md_to_wiki(md: str) -> str:
    unit = _indent_unit(md)
    out, in_code, prev_was_row = [], False, False
    for line in md.splitlines():
        fence = re.match(r"^```(\w*)\s*$", line)
        if fence:
            if not in_code:
                lang = fence.group(1)
                out.append("{code:%s}" % lang if lang else "{code}")
                in_code = True
            else:
                out.append("{code}")
                in_code = False
            prev_was_row = False
            continue
        if in_code:
            out.append(line)
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            out.append(f"h{len(h.group(1))}. {h.group(2)}")
            prev_was_row = False
            continue
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            out.append("----")
            prev_was_row = False
            continue
        # table separator rows vanish; the header row above becomes ||...||
        if _TABLE_SEPARATOR.match(line):
            if prev_was_row:
                out[-1] = _wiki_row(_split_md_row(out[-1]), header=True)
            prev_was_row = False
            continue
        is_row = bool(_TABLE_ROW.match(line))
        line = _inline_md_to_wiki(line)
        if is_row:
            out.append(_wiki_row(_split_md_row(line)))
            prev_was_row = True
            continue
        m = re.match(r"^(\s*)([-*])\s+(.*)$", line)
        if m:
            depth = len(m.group(1).replace("\t", " " * unit)) // unit + 1
            line = "*" * depth + " " + m.group(3)
        else:
            m = re.match(r"^(\s*)\d+\.\s+(.*)$", line)
            if m:
                depth = len(m.group(1).replace("\t", " " * unit)) // unit + 1
                line = "#" * depth + " " + m.group(2)
        if line.startswith("> "):
            line = "bq. " + line[2:]
        out.append(line)
        prev_was_row = is_row
    if in_code:
        out.append("{code}")
    return "\n".join(out)


def _wiki_monospace(code: str) -> str:
    # {{...}} is monospace, but a brace inside it is read as the start of a macro and
    # tears the span apart. Backslash escaping does not stop that; character entities
    # do, and render as the literal character.
    escaped = code.replace("&", "&amp;").replace("{", "&#123;").replace("}", "&#125;")
    return "{{%s}}" % escaped


def _inline_md_to_wiki(line: str) -> str:
    # protect inline code first
    codes: list[str] = []

    def _stash(m):
        codes.append(m.group(1))
        return f"\x00{len(codes) - 1}\x00"

    line = re.sub(r"`([^`]+)`", _stash, line)
    line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"[\1|\2]", line)
    # Italic first (single *), before bold rewrites ** into single * and confuses it.
    line = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])", r"_\1_", line)
    line = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", line)
    line = re.sub(r"__([^_]+)__", r"*\1*", line)
    line = re.sub(r"~~([^~]+)~~", r"-\1-", line)
    return re.sub(r"\x00(\d+)\x00", lambda m: _wiki_monospace(codes[int(m.group(1))]), line)


# ---------------------------------------------------------------- markdown -> Confluence storage

def _md_to_storage(md: str) -> str:
    """Render markdown to Confluence storage XHTML (block level)."""
    import markdown as _md
    return _md.markdown(md, extensions=["tables", "fenced_code"])


def _md_inline_to_storage(text: str) -> str:
    """Render a markdown fragment that must stay inline (a table cell). Raises if it
    produces block content."""
    if not text.strip():
        return ""
    html = _md_to_storage(text).strip()
    m = re.match(r"^<p>(.*)</p>$", html, re.S)
    if not m or "<p>" in m.group(1):
        raise ValueError(f"cell content must be inline markdown, not block content: {text!r}")
    return m.group(1)


# ---------------------------------------------------------------- response shaping

_NAME_FIELDS = {"status", "issuetype", "priority", "resolution", "project"}
_PERSON_FIELDS = {"assignee", "reporter", "creator"}


def _default_fields() -> list[str]:
    """Fields read when a caller names none; override with JIRA_LEAN_DEFAULT_FIELDS."""
    return _config()["default_fields"]


def _shape_fields(fields: dict) -> dict:
    shaped = {}
    for k, v in (fields or {}).items():
        if v is None:
            shaped[k] = None
        elif k in _NAME_FIELDS:
            shaped[k] = v.get("name")
        elif k in _PERSON_FIELDS:
            shaped[k] = v.get("displayName")
        elif k == "parent":
            shaped[k] = v.get("key")
        elif k == "components":
            shaped[k] = [c.get("name") for c in v]
        elif k == "comment":
            shaped[k] = [_shape_comment(c) for c in v.get("comments", [])]
        else:
            shaped[k] = _maybe_adf(v)
    return shaped


def _shape_comment(c: dict) -> dict:
    return {
        "id": c.get("id"),
        "author": (c.get("author") or {}).get("displayName"),
        "created": c.get("created"),
        "body": _maybe_adf(c.get("body")),
    }


def _issue_url(key: str) -> str:
    return f"{_site()}/browse/{key}"


# ---------------------------------------------------------------- MCP tools

mcp = FastMCP("jira-lean")


@mcp.tool()
def jira_get_issue(key: str, fields: list[str] | None = None) -> dict:
    """Get a Jira issue with minimal, shaped output (markdown bodies, no API noise).

    fields: defaults to summary/status/issuetype/assignee/priority/updated (configurable
    via JIRA_LEAN_DEFAULT_FIELDS).
    Add "description" only when you need the body; use jira_get_comments for the
    comment trail. Custom fields allowed by id (e.g. customfield_10001)."""
    fl = fields or _default_fields()
    r = _req("GET", f"/rest/api/3/issue/{key}", params={"fields": ",".join(fl)},
             context=f"issue {key}").json()
    return {"key": r["key"], "url": _issue_url(r["key"]), **_shape_fields(r.get("fields"))}


@mcp.tool()
def jira_get_comments(key: str, limit: int = 10, oldest_first: bool = False,
                      start: int = 0) -> dict:
    """Get a Jira issue's comment trail (newest first by default), each as
    {id, author, created, body-markdown} — cheaper than reading the issue's comment field,
    which returns every comment as ADF.

    limit is per call and capped at 50; start is the 0-based offset into the trail.
    Returns {total, start, comments} — total is the issue's full comment count, so
    page by calling again with start = start + len(comments)."""
    r = _req("GET", f"/rest/api/3/issue/{key}/comment",
             params={"startAt": max(start, 0), "maxResults": min(limit, 50),
                     "orderBy": "created" if oldest_first else "-created"},
             context=f"comments on {key}").json()
    return {"total": r.get("total"), "start": r.get("startAt", start),
            "comments": [_shape_comment(c) for c in r.get("comments", [])]}


@mcp.tool()
def jira_search(jql: str, fields: list[str] | None = None, limit: int = 20,
                next_page_token: str | None = None) -> dict:
    """Search Jira issues by JQL with minimal, shaped output.

    fields defaults to summary/status/issuetype/assignee/priority/updated —
    only widen when genuinely needed."""
    params = {"jql": jql, "fields": ",".join(fields or _default_fields()),
              "maxResults": min(limit, 100)}
    if next_page_token:
        params["nextPageToken"] = next_page_token
    r = _req("GET", "/rest/api/3/search/jql", params=params, context="JQL search").json()
    issues = [{"key": i["key"], **_shape_fields(i.get("fields"))} for i in r.get("issues", [])]
    out = {"issues": issues}
    if r.get("nextPageToken"):
        out["nextPageToken"] = r["nextPageToken"]
    return out


@mcp.tool()
def jira_add_comment(key: str, body_markdown: str) -> dict:
    """Post a comment on a Jira issue. Body is GitHub-flavored markdown
    (headings, bold, lists, tables, code fences supported). Returns only
    {id, url}."""
    r = _req("POST", f"/rest/api/2/issue/{key}/comment",
             json={"body": md_to_wiki(body_markdown)}, context=f"issue {key}").json()
    return {"id": r.get("id"), "url": f"{_issue_url(key)}?focusedCommentId={r.get('id')}"}


@mcp.tool()
def jira_update_comment(key: str, comment_id: str, body_markdown: str) -> dict:
    """Update an existing Jira comment in place (markdown body). Returns {id}."""
    r = _req("PUT", f"/rest/api/2/issue/{key}/comment/{comment_id}",
             json={"body": md_to_wiki(body_markdown)},
             context=f"comment {comment_id} on {key}").json()
    return {"id": r.get("id")}


@mcp.tool()
def jira_add_worklog(key: str, time_spent: str, started: str | None = None,
                     comment_markdown: str | None = None) -> dict:
    """Log work on a Jira issue. time_spent like "1h 30m"; started as ISO 8601
    with offset (e.g. 2026-07-07T09:00:00.000+1000), defaults to now. Returns
    only {id}."""
    body: dict = {"timeSpent": time_spent}
    if started:
        body["started"] = started
    else:
        body["started"] = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S.000%z")
    if comment_markdown:
        body["comment"] = md_to_wiki(comment_markdown)
    r = _req("POST", f"/rest/api/2/issue/{key}/worklog", json=body, context=f"issue {key}").json()
    return {"id": r.get("id")}


@mcp.tool()
def jira_get_worklogs(key: str, author: str | None = None, started_after: str | None = None,
                      started_before: str | None = None) -> dict:
    """Get every worklog on a Jira issue, paging until exhausted — no 20-entry cap.

    author matches an accountId exactly, or a display name case-insensitively as a
    substring ("alice"). started_after / started_before are YYYY-MM-DD and both
    inclusive, compared against the date the work was logged in its own UTC offset.
    Returns {total, worklogs: [{id, author, started, time_spent, time_spent_seconds,
    comment}]}, where total is the number returned after filtering."""
    raw = _page_all(f"/rest/api/3/issue/{key}/worklog", "worklogs",
                    context=f"worklogs on {key}", page_size=1000)
    after = date.fromisoformat(started_after) if started_after else None
    before = date.fromisoformat(started_before) if started_before else None
    out = []
    for w in raw:
        who = w.get("author") or {}
        if author and not (who.get("accountId") == author
                           or author.lower() in (who.get("displayName") or "").lower()):
            continue
        started = w.get("started") or ""
        if after or before:
            day = _parse_started(started)
            if day is None or (after and day < after) or (before and day > before):
                continue
        out.append({
            "id": w.get("id"),
            "author": who.get("displayName"),
            "started": started,
            "time_spent": w.get("timeSpent"),
            "time_spent_seconds": w.get("timeSpentSeconds"),
            "comment": _maybe_adf(w.get("comment")),
        })
    return {"total": len(out), "worklogs": out}


def _parse_started(started: str) -> date | None:
    """Date component of a Jira timestamp, read in the offset it was written with —
    not by string prefix, so an offset never shifts work onto the wrong day."""
    s = started.strip()
    # Jira writes +1000; fromisoformat wants +10:00 before 3.11 and tolerates both after.
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


@mcp.tool()
def jira_get_issues(keys: list[str], fields: list[str] | None = None) -> dict:
    """Read many Jira issues in one call, shaped like jira_get_issue. Use instead of
    looping jira_get_issue: one request per 100 keys rather than one per issue.

    Returns {issues, missing} — issues in the order requested, and missing listing any
    key that does not exist or is not visible to the account, rather than dropping it."""
    fl = fields or _default_fields()
    found: dict[str, dict] = {}
    for i in range(0, len(keys), 100):
        found.update(_read_issue_batch(keys[i:i + 100], fl))
    return {"issues": [found[k] for k in keys if k in found],
            "missing": [k for k in keys if k not in found]}


def _read_issue_batch(keys: list[str], fields: list[str]) -> dict[str, dict]:
    """Read up to 100 issues by key. A key JQL rejects fails the whole query, so on a
    400 the batch is halved until the offending keys are isolated and reported missing."""
    if not keys:
        return {}
    try:
        r = _req("GET", "/rest/api/3/search/jql",
                 params={"jql": "key in (" + ",".join(keys) + ")",
                         "fields": ",".join(fields), "maxResults": len(keys)},
                 context="issue batch read").json()
    except AtlassianError as e:
        if e.status_code != 400:
            raise
        if len(keys) == 1:
            return {}
        mid = len(keys) // 2
        return {**_read_issue_batch(keys[:mid], fields), **_read_issue_batch(keys[mid:], fields)}
    return {i["key"]: {"key": i["key"], **_shape_fields(i.get("fields"))}
            for i in r.get("issues", [])}


@mcp.tool()
def jira_edit_issue(key: str, fields: dict, markdown_fields: list[str] | None = None) -> dict:
    """Edit Jira issue fields. fields is the raw Jira fields dict. Returns {ok, url}.

    markdown_fields names the string fields to convert from markdown to Jira wiki
    markup, and defaults to ["description"]. Name any other rich-text field here
    (acceptance criteria, custom textareas) or it is stored as literal markdown;
    pass [] to convert nothing."""
    names = ["description"] if markdown_fields is None else markdown_fields
    converted = {k: md_to_wiki(v) for k, v in fields.items()
                 if k in names and isinstance(v, str)}
    if converted:
        fields = {**fields, **converted}
    _req("PUT", f"/rest/api/2/issue/{key}", json={"fields": fields}, context=f"issue {key}")
    return {"ok": True, "url": _issue_url(key)}


@mcp.tool()
def jira_get_transitions(key: str) -> list[dict]:
    """List available transitions for a Jira issue as [{id, name, to}]."""
    r = _req("GET", f"/rest/api/3/issue/{key}/transitions", context=f"issue {key}").json()
    return [{"id": t["id"], "name": t["name"], "to": t.get("to", {}).get("name")}
            for t in r.get("transitions", [])]


@mcp.tool()
def jira_transition(key: str, transition: str) -> dict:
    """Transition a Jira issue. transition is an id or (case-insensitive) name.
    Returns {ok, status}."""
    avail = jira_get_transitions(key)
    match = next((t for t in avail if t["id"] == transition
                  or t["name"].lower() == transition.lower()), None)
    if not match:
        raise RuntimeError(f"No transition '{transition}' on {key}. Available: "
                           + ", ".join(f"{t['name']} ({t['id']})" for t in avail))
    _req("POST", f"/rest/api/3/issue/{key}/transitions",
         json={"transition": {"id": match["id"]}}, context=f"issue {key}")
    return {"ok": True, "status": match["to"]}


@mcp.tool()
def jira_get_link_types() -> list[dict]:
    """List issue link types as [{id, name, inward, outward}] — the names
    jira_link_issues accepts."""
    r = _req("GET", "/rest/api/3/issueLinkType", context="issue link types").json()
    return [{"id": t.get("id"), "name": t.get("name"),
             "inward": t.get("inward"), "outward": t.get("outward")}
            for t in r.get("issueLinkTypes", [])]


@mcp.tool()
def jira_link_issues(from_key: str, to_key: str, link_type: str) -> dict:
    """Link two Jira issues, reading in the outward direction: from_key <link_type
    outward phrase> to_key. With link_type "Blocks", from_key blocks to_key.

    link_type is an id or a (case-insensitive) name; an unknown one errors with the
    available list. Returns {ok}."""
    avail = jira_get_link_types()
    match = next((t for t in avail if t["id"] == link_type
                  or (t["name"] or "").lower() == link_type.lower()), None)
    if not match:
        raise RuntimeError(f"No issue link type '{link_type}'. Available: "
                           + ", ".join(t["name"] for t in avail))
    _req("POST", "/rest/api/3/issueLink",
         json={"type": {"id": match["id"]},
               "outwardIssue": {"key": from_key}, "inwardIssue": {"key": to_key}},
         context=f"link {from_key} -> {to_key}")
    return {"ok": True}


@mcp.tool()
def jira_lookup_account(query: str) -> list[dict]:
    """Find Jira accounts by name or email, as [{account_id, display_name, email}].
    email is null when the account's
    privacy settings hide it."""
    r = _req("GET", "/rest/api/3/user/search", params={"query": query, "maxResults": 20},
             context=f"account search '{query}'").json()
    return [{"account_id": u.get("accountId"), "display_name": u.get("displayName"),
             "email": u.get("emailAddress")} for u in r]


@mcp.tool()
def jira_get_field_ids(project: str, issue_type: str) -> list[dict]:
    """List the fields available on an issue type as [{id, name, type, required}].
    Use this to resolve a custom field's customfield_NNNNN id instead of hardcoding it.

    project is a key or id; issue_type is a name (case-insensitive) or id."""
    types = _page_all(f"/rest/api/3/issue/createmeta/{project}/issuetypes", "issueTypes",
                      context=f"issue types in project {project}")
    match = next((t for t in types if t.get("id") == issue_type
                  or (t.get("name") or "").lower() == issue_type.lower()), None)
    if not match:
        raise RuntimeError(f"No issue type '{issue_type}' in project {project}. Available: "
                           + ", ".join(t.get("name") or "" for t in types))
    fields = _page_all(f"/rest/api/3/issue/createmeta/{project}/issuetypes/{match['id']}",
                       "fields", context=f"fields on {project}/{match.get('name')}")
    return [{"id": f.get("fieldId"), "name": f.get("name"),
             "type": (f.get("schema") or {}).get("type"), "required": f.get("required")}
            for f in fields]


# ---------------------------------------------------------------- Confluence internals
#
# Storage XHTML is the canonical representation: it is what Confluence stores, and it
# is the only form that carries panels, status lozenges, expands, layouts, task lists,
# media references and the ac:inline-comment-marker elements inline comments hang off.
# Markdown is an authoring convenience on the way in and a reading convenience on the
# way out. A write that is not given a new body resends the stored bytes untouched, so
# nothing is round-tripped through markdown and nothing is silently dropped.

def _page_url(page: dict) -> str:
    return _site() + "/wiki" + ((page.get("_links") or {}).get("webui") or "")


def _get_page_raw(page_id: str) -> dict:
    """Current state of a page: {storage, version, title, parent_id, status, url}.
    storage is the stored XHTML verbatim."""
    r = _req("GET", f"/wiki/api/v2/pages/{page_id}", params={"body-format": "storage"},
             context=f"Confluence page {page_id}").json()
    return {
        "storage": ((r.get("body") or {}).get("storage") or {}).get("value", ""),
        "version": (r.get("version") or {}).get("number"),
        "title": r.get("title"),
        "parent_id": r.get("parentId"),
        "status": r.get("status"),
        "url": _page_url(r),
    }


def _write_page(page_id: str, *, title: str | None = None, storage: str | None = None,
                parent_id: str | None = None, version_message: str = "",
                expected_version: int | None = None, current: dict | None = None) -> dict:
    """Write a page, carrying forward everything not being changed. storage=None means
    resend the stored body byte for byte."""
    cur = current or _get_page_raw(page_id)
    if expected_version is not None and cur["version"] != expected_version:
        raise AtlassianError(
            f"409 version conflict (Confluence page {page_id}): expected version "
            f"{expected_version}, but the current version is {cur['version']}. Someone else "
            f"wrote to the page — re-read it and retry.", 409)
    payload: dict = {
        "id": page_id,
        "status": "current",
        "title": cur["title"] if title is None else title,
        "body": {"representation": "storage",
                 "value": cur["storage"] if storage is None else storage},
        "version": {"number": (cur["version"] or 0) + 1, "message": version_message},
    }
    if parent_id is not None:
        payload["parentId"] = parent_id
    try:
        r = _req("PUT", f"/wiki/api/v2/pages/{page_id}", json=payload,
                 context=f"Confluence page {page_id}").json()
    except AtlassianError as e:
        if e.status_code != 409:
            raise
        raise AtlassianError(
            f"409 version conflict (Confluence page {page_id}): tried to write version "
            f"{(cur['version'] or 0) + 1} from a read of version {cur['version']}, which is no "
            f"longer current. Re-read the page and retry.", 409) from e
    return {"id": r.get("id"), "title": r.get("title"),
            "version": (r.get("version") or {}).get("number")}


def _resolve_space(space_id_or_key: str) -> dict:
    """Look up a space by numeric id or by key, returning {id, key, name}."""
    v = str(space_id_or_key).strip()
    params = {"ids": v} if v.isdigit() else {"keys": v}
    r = _req("GET", "/wiki/api/v2/spaces", params=params,
             context=f"Confluence space {v}").json()
    results = r.get("results") or []
    if not results:
        raise RuntimeError(f"No Confluence space '{v}' (searched by "
                           f"{'id' if v.isdigit() else 'key'})")
    s = results[0]
    return {"id": s.get("id"), "key": s.get("key"), "name": s.get("name")}


def _space_id(space_id_or_key: str) -> str:
    """Space id from either an id or a key, without a lookup when it is already an id."""
    v = str(space_id_or_key).strip()
    return v if v.isdigit() else _resolve_space(v)["id"]


def _cql_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _table_spans(html: str) -> list[tuple[int, int]]:
    """(start, end) offsets of each <table> element in storage XHTML, in document order."""
    spans, pos = [], 0
    while True:
        m = re.compile(r"<table\b", re.I).search(html, pos)
        if not m:
            return spans
        close = html.find("</table>", m.end())
        if close == -1:
            return spans
        pos = close + len("</table>")
        spans.append((m.start(), pos))


def _insert_table_row(html: str, table_index: int, row_html: str) -> str:
    """Splice one row into the addressed table's last row position, leaving every other
    byte of the document untouched."""
    spans = _table_spans(html)
    if not spans:
        raise IndexError("page has no table to append to")
    if table_index < 0 or table_index >= len(spans):
        raise IndexError(f"table_index {table_index} is out of range: the page has "
                         f"{len(spans)} table(s)")
    start, end = spans[table_index]
    table = html[start:end]
    at = table.rfind("</tbody>")
    if at == -1:
        at = table.rfind("</table>")
    return html[:start] + table[:at] + row_html + table[at:] + html[end:]


def _label_names(page_id: str) -> list[str]:
    r = _req("GET", f"/wiki/rest/api/content/{page_id}/label", params={"limit": 200},
             context=f"labels on Confluence page {page_id}").json()
    return [lb.get("name") for lb in r.get("results", [])]


# ---------------------------------------------------------------- Confluence tools

@mcp.tool()
def confluence_get_page(page_id: str, body_format: Literal["markdown", "storage"] = "markdown") -> dict:
    """Get a Confluence page: {id, title, version, url, body}.

    body_format="markdown" renders the body for reading and drops constructs markdown
    cannot express (panels, lozenges, expands, layouts, task lists, media). Use
    "storage" to get the stored XHTML verbatim — required if the body is going to be
    edited and written back, since the markdown rendering is not reversible."""
    r = _req("GET", f"/wiki/api/v2/pages/{page_id}", params={"body-format": "storage"},
             context=f"Confluence page {page_id}").json()
    html = ((r.get("body") or {}).get("storage") or {}).get("value", "")
    return {
        "id": r.get("id"),
        "title": r.get("title"),
        "version": (r.get("version") or {}).get("number"),
        "url": _page_url(r),
        "body": html if body_format == "storage" else markdownify(html, heading_style="ATX").strip(),
    }


@mcp.tool()
def confluence_create_page(space_id: str, title: str, body_markdown: str,
                           parent_id: str | None = None,
                           body_format: Literal["markdown", "storage"] = "markdown") -> dict:
    """Create a Confluence page. Returns only {id, url} — never the body.

    space_id accepts a numeric space id or a space key. body_format="storage" takes
    body_markdown as raw storage XHTML instead of markdown."""
    html = body_markdown if body_format == "storage" else _md_to_storage(body_markdown)
    payload: dict = {"spaceId": _space_id(space_id), "status": "current",
                     "title": title, "body": {"representation": "storage", "value": html}}
    if parent_id:
        payload["parentId"] = parent_id
    r = _req("POST", "/wiki/api/v2/pages", json=payload,
             context=f"new page '{title}'").json()
    return {"id": r.get("id"), "url": _page_url(r)}


@mcp.tool()
def confluence_update_page(page_id: str, body_markdown: str | None = None,
                           title: str | None = None, version_message: str = "",
                           body_format: Literal["markdown", "storage"] = "markdown",
                           expected_version: int | None = None) -> dict:
    """Replace a Confluence page's content. Returns {id, title, version}.

    Omit body_markdown to change only metadata: the stored body is resent byte for
    byte, so nothing is lost. body_format="storage" takes body_markdown as raw storage
    XHTML. expected_version, when given, aborts before writing if the page is no longer
    at that version."""
    storage = None
    if body_markdown is not None:
        storage = body_markdown if body_format == "storage" else _md_to_storage(body_markdown)
    return _write_page(page_id, title=title, storage=storage,
                       version_message=version_message, expected_version=expected_version)


@mcp.tool()
def confluence_rename_page(page_id: str, title: str, version_message: str = "",
                           expected_version: int | None = None) -> dict:
    """Change a Confluence page's title, leaving the body untouched — the stored XHTML
    is resent verbatim rather than re-rendered. Returns {id, title, version}. Use this
    instead of confluence_update_page when only the title changes."""
    return _write_page(page_id, title=title, version_message=version_message,
                       expected_version=expected_version)


@mcp.tool()
def confluence_move_page(page_id: str, parent_id: str, version_message: str = "",
                         expected_version: int | None = None) -> dict:
    """Move a Confluence page under a new parent, leaving title and body untouched.
    Returns {id, title, version}."""
    return _write_page(page_id, parent_id=parent_id, version_message=version_message,
                       expected_version=expected_version)


@mcp.tool()
def confluence_append_to_page(page_id: str, body_markdown: str, version_message: str = "",
                              body_format: Literal["markdown", "storage"] = "markdown",
                              expected_version: int | None = None) -> dict:
    """Append a fragment to the end of a Confluence page without resending or
    re-rendering what is already there. Returns {id, title, version}.

    body_markdown is the new fragment only. body_format="storage" takes it as raw
    storage XHTML."""
    cur = _get_page_raw(page_id)
    fragment = body_markdown if body_format == "storage" else _md_to_storage(body_markdown)
    return _write_page(page_id, storage=cur["storage"] + fragment, current=cur,
                       version_message=version_message, expected_version=expected_version)


@mcp.tool()
def confluence_append_table_row(page_id: str, cells: list[str], table_index: int = 0,
                                version_message: str = "",
                                expected_version: int | None = None) -> dict:
    """Append one row to a table on a Confluence page, leaving every other byte of the
    page as it was. Returns {id, title, version}.

    cells is one markdown string per column, inline only (bold, links, code — no lists
    or headings); it errors rather than writing block content into a cell. table_index
    addresses the table by document order, 0 being the first, and is validated before
    anything is written."""
    cur = _get_page_raw(page_id)
    row = "<tr>" + "".join(
        f"<td><p>{_md_inline_to_storage(c)}</p></td>" if str(c).strip() else "<td><p /></td>"
        for c in cells) + "</tr>"
    updated = _insert_table_row(cur["storage"], table_index, row)
    return _write_page(page_id, storage=updated, current=cur,
                       version_message=version_message, expected_version=expected_version)


@mcp.tool()
def confluence_upsert_page(space_id_or_key: str, title: str, body_markdown: str,
                           parent_id: str | None = None,
                           body_format: Literal["markdown", "storage"] = "markdown",
                           version_message: str = "",
                           expected_version: int | None = None) -> dict:
    """Create a Confluence page, or replace the body of the existing page with that
    title in that space. Use instead of confluence_create_page when a re-run must not
    leave a duplicate. Returns {id, url, version, action} where action is "created" or
    "updated"; errors without writing if the title matches more than one page."""
    space = _resolve_space(space_id_or_key)
    matches = confluence_search(
        f'type = page AND space = "{_cql_quote(space["key"])}" '
        f'AND title = "{_cql_quote(title)}"', limit=25)
    exact = [m for m in matches if m["title"] == title]
    if len(exact) > 1:
        raise RuntimeError(f"'{title}' matches {len(exact)} pages in space {space['key']} "
                           f"({', '.join(m['id'] for m in exact)}) — nothing written. Update "
                           f"the intended page by id.")
    if not exact:
        created = confluence_create_page(space["id"], title, body_markdown,
                                         parent_id=parent_id, body_format=body_format)
        return {**created, "version": 1, "action": "created"}
    page_id = exact[0]["id"]
    updated = confluence_update_page(page_id, body_markdown, version_message=version_message,
                                     body_format=body_format, expected_version=expected_version)
    return {"id": page_id, "url": exact[0]["url"], "version": updated["version"],
            "action": "updated"}


@mcp.tool()
def confluence_delete_page(page_id: str, purge: bool = False) -> dict:
    """Move a Confluence page to the trash, where it can be restored from the space's
    trash view. Returns {id, status}.

    purge=True permanently deletes it instead, with no way back — the page is trashed
    first if it is not already, then purged."""
    cur = _get_page_raw(page_id)
    if cur["status"] != "trashed":
        _req("DELETE", f"/wiki/api/v2/pages/{page_id}",
             context=f"Confluence page {page_id}")
    if not purge:
        return {"id": page_id, "status": "trashed"}
    _req("DELETE", f"/wiki/api/v2/pages/{page_id}", params={"purge": "true"},
         context=f"Confluence page {page_id} (purge)")
    return {"id": page_id, "status": "purged"}


@mcp.tool()
def confluence_get_labels(page_id: str) -> dict:
    """Get a Confluence page's labels as {labels: [...]}."""
    return {"labels": _label_names(page_id)}


@mcp.tool()
def confluence_add_labels(page_id: str, labels: list[str]) -> dict:
    """Add labels to a Confluence page. Returns {labels: [...]} — the full resulting
    set, so no second read is needed to confirm.

    Adding a label the page already has is a no-op, not an error. Labels cannot contain
    whitespace. This does not create a page version."""
    clean = [str(lb).strip() for lb in labels]
    bad = [lb for lb in clean if not lb or re.search(r"\s", lb)]
    if bad:
        raise ValueError(f"labels cannot be empty or contain whitespace: {bad}")
    r = _req("POST", f"/wiki/rest/api/content/{page_id}/label",
             json=[{"prefix": "global", "name": lb} for lb in clean],
             context=f"labels on Confluence page {page_id}").json()
    return {"labels": [lb.get("name") for lb in r.get("results", [])]}


@mcp.tool()
def confluence_remove_label(page_id: str, label: str) -> dict:
    """Remove one label from a Confluence page. Returns {labels: [...]} — the full
    remaining set. Does not create a page version."""
    _req("DELETE", f"/wiki/rest/api/content/{page_id}/label", params={"name": label},
         context=f"label '{label}' on Confluence page {page_id}")
    return {"labels": _label_names(page_id)}


@mcp.tool()
def confluence_get_space(space_key: str) -> dict:
    """Look up a Confluence space by key (or numeric id): {id, key, name}. The id is
    what confluence_create_page needs, though it also accepts the key directly."""
    return _resolve_space(space_key)


@mcp.tool()
def confluence_get_children(page_id: str, depth: int = 1) -> list[dict]:
    """List a Confluence page's child pages as [{id, title, parent_id}], in tree order.

    depth=1 is direct children only; higher values walk that many levels down."""
    out: list[dict] = []

    def walk(pid: str, level: int) -> None:
        path = f"/wiki/api/v2/pages/{pid}/children?limit=250"
        while path:
            r = _req("GET", path, context=f"children of Confluence page {pid}").json()
            for c in r.get("results", []):
                out.append({"id": c.get("id"), "title": c.get("title"), "parent_id": pid})
                if level < depth:
                    walk(c.get("id"), level + 1)
            path = (r.get("_links") or {}).get("next") or ""
    walk(page_id, 1)
    return out


@mcp.tool()
def confluence_search(cql: str, limit: int = 10) -> list[dict]:
    """Search Confluence with CQL; returns [{id, title, url, lastModified}].
   """
    r = _req("GET", "/wiki/rest/api/search",
             params={"cql": cql, "limit": min(limit, 50)}, context="CQL search").json()
    out = []
    for res in r.get("results", []):
        content = res.get("content") or {}
        out.append({
            "id": content.get("id"),
            "title": res.get("title", "").replace("@@@hl@@@", "").replace("@@@endhl@@@", ""),
            "url": _site() + "/wiki" + (res.get("url") or ""),
            "lastModified": res.get("lastModified"),
        })
    return out


def main() -> None:
    """Run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
