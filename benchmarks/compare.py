"""Raw Atlassian payload vs jira-lean output, measured offline on synthetic data.

Each scenario builds a payload with the shape the Atlassian Cloud REST API returns
(user objects with four avatar sizes, `self` links, status categories, ADF bodies,
write responses that echo what was sent), serves it through a stub transport, and
calls the real jira-lean tool against it. The metric is the JSON size of what would
reach a model's context: the raw REST payload, which a pass-through MCP server hands
over verbatim, against the tool's shaped result.

Everything here is invented: the site is example.atlassian.net, the project is DEMO.
The figures measured on a real site, and how to check your own, are in the README.

    python benchmarks/compare.py
"""
from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

import httpx

from jira_lean_mcp import server as s

SITE = "https://example.atlassian.net"
FIXTURE = (Path(__file__).resolve().parent.parent / "tests" / "fixtures"
           / "page_rich.storage.html").read_text(encoding="utf-8").rstrip("\n")

# Fields a pass-through server requests by default for an issue read.
PASS_THROUGH_FIELDS = ["summary", "description", "status", "issuetype", "priority", "labels",
                       "components", "assignee", "reporter", "created", "updated",
                       "resolution", "project"]


# ---------------------------------------------------------------- synthetic REST shapes

def _avatars(seed: str) -> dict:
    base = f"https://avatar-management.example.com/avatars/{seed}"
    return {f"{n}x{n}": f"{base}/{n}.png" for n in (48, 24, 16, 32)}


def user(name: str) -> dict:
    slug = name.lower().replace(" ", "-")
    return {"self": f"{SITE}/rest/api/3/user?accountId=acc-{slug}", "accountId": f"acc-{slug}",
            "emailAddress": f"{slug}@example.com", "avatarUrls": _avatars(slug),
            "displayName": name, "active": True, "timeZone": "Etc/UTC", "accountType": "atlassian"}


def adf(*paragraphs: str) -> dict:
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs]}


def raw_issue(n: int) -> dict:
    key = f"DEMO-{n}"
    return {
        "expand": "renderedFields,names,schema,operations,editmeta,changelog,versionedRepresentations",
        "id": str(10000 + n), "self": f"{SITE}/rest/api/3/issue/{10000 + n}", "key": key,
        "fields": {
            "summary": f"Make the export job retry on transient errors ({n})",
            "description": adf("The nightly export fails when the upstream API returns a 503.",
                               "Retry with backoff up to three times, then alert.",
                               "Acceptance: a single 503 no longer fails the run."),
            "status": {"self": f"{SITE}/rest/api/3/status/3", "description": "Work is underway.",
                       "iconUrl": f"{SITE}/images/icons/statuses/inprogress.png",
                       "name": "In Progress", "id": "3",
                       "statusCategory": {"self": f"{SITE}/rest/api/3/statuscategory/4", "id": 4,
                                          "key": "indeterminate", "colorName": "yellow",
                                          "name": "In Progress"}},
            "issuetype": {"self": f"{SITE}/rest/api/3/issuetype/10001", "id": "10001",
                          "description": "A small, distinct piece of work.",
                          "iconUrl": f"{SITE}/rest/api/2/universal_avatar/view/type/issuetype/avatar/10318?size=medium",
                          "name": "Task", "subtask": False, "avatarId": 10318, "hierarchyLevel": 0},
            "priority": {"self": f"{SITE}/rest/api/3/priority/3",
                         "iconUrl": f"{SITE}/images/icons/priorities/medium.svg",
                         "name": "Medium", "id": "3"},
            "labels": ["export", "reliability"],
            "components": [{"self": f"{SITE}/rest/api/3/component/10010", "id": "10010",
                            "name": "Pipeline", "description": "Batch jobs"}],
            "assignee": user("Alice Example"),
            "reporter": user("Bob Sample"),
            "created": "2025-01-10T09:12:44.123+0000",
            "updated": "2025-01-14T16:03:09.456+0000",
            "resolution": None,
            "project": {"self": f"{SITE}/rest/api/3/project/10000", "id": "10000", "key": "DEMO",
                        "name": "Demo Project", "projectTypeKey": "software", "simplified": False,
                        "avatarUrls": _avatars("project-demo")},
        },
    }


def raw_comment(i: int) -> dict:
    author = user("Alice Example" if i % 2 else "Bob Sample")
    return {"self": f"{SITE}/rest/api/3/issue/10001/comment/{20000 + i}", "id": str(20000 + i),
            "author": author, "updateAuthor": author,
            "body": adf(f"Update {i}: reran the job against staging.",
                        "The retry path fired twice and the run completed."),
            "created": "2025-01-12T10:00:00.000+0000", "updated": "2025-01-12T10:00:00.000+0000",
            "jsdPublic": True}


def raw_page(page_id: str, title: str, storage: str) -> dict:
    return {"parentType": "page", "parentId": "1000", "spaceId": "2000", "ownerId": "acc-alice-example",
            "lastOwnerId": None, "createdAt": "2025-01-15T08:00:00.000Z", "authorId": "acc-alice-example",
            "position": 1, "version": {"number": 1, "message": "", "minorEdit": False,
                                       "authorId": "acc-alice-example", "createdAt": "2025-01-15T08:00:00.000Z"},
            "body": {"storage": {"representation": "storage", "value": storage}},
            "status": "current", "title": title, "id": page_id,
            "_links": {"editui": f"/pages/resumedraft.action?draftId={page_id}",
                       "webui": f"/spaces/DEMO/pages/{page_id}/Demo+page",
                       "edituiv2": f"/spaces/DEMO/pages/edit-v2/{page_id}",
                       "tinyui": "/x/AbCd", "base": f"{SITE}/wiki"}}


# ---------------------------------------------------------------- harness

@contextmanager
def stub(routes: dict):
    """Serve routes {(method, path): payload or callable(request) -> payload} as 200s."""
    def handler(request: httpx.Request) -> httpx.Response:
        route = routes[(request.method, request.url.path)]
        return httpx.Response(200, json=route(request) if callable(route) else route)

    saved = s._settings, s._client
    s._settings = s.load_settings({"ATLASSIAN_SITE": SITE, "ATLASSIAN_EMAIL": "dev@example.com",
                                   "ATLASSIAN_API_TOKEN": "not-a-real-token"})
    s._client = httpx.Client(base_url=SITE, transport=httpx.MockTransport(handler))
    try:
        yield
    finally:
        s._client.close()
        s._settings, s._client = saved


def only_fields(issue: dict, names: list[str]) -> dict:
    """What Jira returns for ?fields=...: the requested fields and nothing else."""
    return {**issue, "fields": {k: v for k, v in issue["fields"].items() if k in names}}


def size(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def scenarios() -> list[tuple[str, int, int]]:
    rows = []
    issue = raw_issue(1)

    def issue_route(request):
        return only_fields(issue, request.url.params["fields"].split(","))

    with stub({("GET", "/rest/api/3/issue/DEMO-1"): issue_route}):
        rows.append(("Get one issue (default fields)",
                     size(only_fields(issue, PASS_THROUGH_FIELDS)), size(s.jira_get_issue("DEMO-1"))))
        lean_fields = s._default_fields()
        rows.append(("Get one issue, raw request narrowed to the same 6 fields",
                     size(only_fields(issue, lean_fields)), size(s.jira_get_issue("DEMO-1"))))

    issues = [raw_issue(n) for n in range(1, 11)]

    def search_route(request):
        names = request.url.params["fields"].split(",")
        return {"issues": [only_fields(i, names) for i in issues], "isLast": True}

    with stub({("GET", "/rest/api/3/search/jql"): search_route}):
        raw = {"issues": [only_fields(i, PASS_THROUGH_FIELDS) for i in issues], "isLast": True}
        rows.append(("JQL search, 10 issues", size(raw),
                     size(s.jira_search("project = DEMO ORDER BY updated DESC", limit=10))))

    comments = [raw_comment(i) for i in range(20)]
    raw_trail = {**issue, "fields": {"comment": {"comments": comments, "self": f"{SITE}/rest/api/3/issue/10001/comment",
                                                 "maxResults": 20, "total": 20, "startAt": 0}}}
    with stub({("GET", "/rest/api/3/issue/DEMO-1/comment"):
               {"startAt": 0, "maxResults": 20, "total": 20, "comments": comments}}):
        rows.append(("Read an issue's 20 comments", size(raw_trail),
                     size(s.jira_get_comments("DEMO-1", limit=20))))

    echo = raw_comment(99)
    with stub({("POST", "/rest/api/2/issue/DEMO-1/comment"): echo}):
        rows.append(("Add a comment (response only)", size(echo),
                     size(s.jira_add_comment("DEMO-1", "Reran the job; retry path works."))))

    created = raw_page("3000", "Demo page", FIXTURE)
    with stub({("POST", "/wiki/api/v2/pages"): created}):
        rows.append(("Create a Confluence page (response only)", size(created),
                     size(s.confluence_create_page("2000", "Demo page", FIXTURE, body_format="storage"))))

    with stub({("GET", "/wiki/api/v2/pages/3000"): created}):
        rows.append(("Read a Confluence page as markdown", size(created),
                     size(s.confluence_get_page("3000"))))
    return rows


def main() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    rows = scenarios()
    print("| Scenario | Raw chars | jira-lean chars | Reduction |")
    print("| --- | ---: | ---: | ---: |")
    for name, raw, lean in rows:
        print(f"| {name} | {raw:,} | {lean:,} | {(1 - lean / raw) * 100:.1f}% |")
    raw_total, lean_total = sum(r[1] for r in rows), sum(r[2] for r in rows)
    print(f"| **Total** | **{raw_total:,}** | **{lean_total:,}** | "
          f"**{(1 - lean_total / raw_total) * 100:.1f}%** |")
    print("\nTokens are roughly chars / 4 for dense JSON; the ratio does not depend on the divisor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
