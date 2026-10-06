"""Unit tests for jira-lean.

Every tool is called directly. API calls go through a stub httpx transport, so each
test asserts on the request the tool actually builds and on the value it returns —
not on a mock of a caller. Nothing here touches the live site.

Run: python -m pytest -q
"""
import contextlib
import json
from pathlib import Path

import httpx
import pytest

from conftest import TEST_SITE
from jira_lean_mcp import server as s

FIXTURES = Path(__file__).parent / "fixtures"
# The fixture file ends with a newline for git's sake; Confluence stores the body
# without it, so the byte comparisons run against the stripped value.
RICH_PAGE = (FIXTURES / "page_rich.storage.html").read_text(encoding="utf-8").rstrip("\n")


# ---------------------------------------------------------------- stub transport

class FakeAPI:
    """Routes ("METHOD", "/path") -> (status, payload), a list of those consumed in
    order, or a callable(request) returning one. Every request is recorded."""

    def __init__(self, routes: dict):
        self.routes = dict(routes)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            raise AssertionError(f"unstubbed call: {request.method} {request.url}")
        if isinstance(route, list):
            route = route.pop(0)
        if callable(route):
            route = route(request)
        status, payload = route
        if payload is None:
            return httpx.Response(status)
        return httpx.Response(status, json=payload)

    def calls(self, method: str, path: str | None = None) -> list[httpx.Request]:
        return [r for r in self.requests
                if r.method == method and (path is None or r.url.path == path)]

    def body(self, index: int = -1) -> dict:
        return json.loads(self.calls_all()[index].content)

    def calls_all(self) -> list[httpx.Request]:
        return self.requests


@contextlib.contextmanager
def fake_api(routes: dict):
    api = FakeAPI(routes)
    original = s._client
    s._client = httpx.Client(base_url=TEST_SITE, transport=httpx.MockTransport(api.handler))
    try:
        yield api
    finally:
        s._client.close()
        s._client = original


def json_body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def page_response(page_id="123", title="Handbook", version=7, storage=RICH_PAGE,
                  parent_id="99", status="current"):
    return 200, {
        "id": page_id, "title": title, "status": status, "parentId": parent_id,
        "version": {"number": version},
        "body": {"storage": {"value": storage, "representation": "storage"}},
        "_links": {"webui": f"/spaces/DEMO/pages/{page_id}"},
    }


# ---------------------------------------------------------------- markdown -> wiki

MD_TO_WIKI_CASES = [
    # (name, markdown, expected wiki)
    # Expected wiki markup below is what Jira's own wiki renderer turns back into the
    # intended table/monospace; the collapsed forms (|| || and backslash-escaped braces)
    # render wrong rather than erroring.
    ("table_empty_leading_cell",
     "||Old|New|\n|---|---|---|\n|p95|3s|1s|",
     "|| ||Old||New||\n|p95|3s|1s|"),
    ("table_empty_leading_cell_spaced",
     "|  | Old | New |\n|---|---|---|\n| p95 | 3s | 1s |",
     "|| ||Old||New||\n|p95|3s|1s|"),
    ("table_empty_trailing_cell",
     "|Metric|Old||\n|---|---|---|\n|p95|3s||",
     "||Metric||Old|| ||\n|p95|3s| |"),
    ("table_empty_middle_cell",
     "|Metric|Old|New|\n|---|---|---|\n|p95||1s|",
     "||Metric||Old||New||\n|p95| |1s|"),
    ("table_plain",
     "| Metric | Old |\n|---|---|\n| p95 | 3s |",
     "||Metric||Old||\n|p95|3s|"),
    ("table_escaped_pipe_stays_escaped",
     "| Expr | Note |\n|---|---|\n| a \\| b | or |",
     "||Expr||Note||\n|a \\| b|or|"),
    ("inline_code_with_shell_var",
     "Set `${AWS_REGION}` before running.",
     "Set {{$&#123;AWS_REGION&#125;}} before running."),
    ("inline_code_with_ampersand",
     "Run `a && b` twice.",
     "Run {{a &amp;&amp; b}} twice."),
    ("inline_code_plain",
     "Look at `settings.py` first.",
     "Look at {{settings.py}} first."),
    ("nested_list_four_space",
     "- parent\n    - child\n        - grandchild",
     "* parent\n** child\n*** grandchild"),
    ("nested_list_two_space",
     "- parent\n  - child\n    - grandchild",
     "* parent\n** child\n*** grandchild"),
    ("ordered_list_nested_three_space",
     "1. first\n   1. nested\n2. second",
     "# first\n## nested\n# second"),
    ("headings", "## Verdict\n### Detail", "h2. Verdict\nh3. Detail"),
    ("link", "See [the PR](https://example.com/pr/1).", "See [the PR|https://example.com/pr/1]."),
    ("emphasis", "**bold** and *italic* and ~~struck~~", "*bold* and _italic_ and -struck-"),
    ("code_fence", "```scala\nval x = 1\n```", "{code:scala}\nval x = 1\n{code}"),
    ("code_fence_no_lang", "```\nplain\n```", "{code}\nplain\n{code}"),
    ("rule", "---", "----"),
    ("blockquote", "> quoted", "bq. quoted"),
]


@pytest.mark.parametrize("name,md,expected", MD_TO_WIKI_CASES,
                         ids=[c[0] for c in MD_TO_WIKI_CASES])
def test_md_to_wiki(name, md, expected):
    assert s.md_to_wiki(md) == expected


def test_md_to_wiki_leaves_code_fence_contents_alone():
    md = "```python\n- not a list\n| not | a table |\n**not bold**\n```"
    assert s.md_to_wiki(md) == "{code:python}\n- not a list\n| not | a table |\n**not bold**\n{code}"


def test_split_md_row_keeps_empty_edge_cells():
    assert s._split_md_row("||Old|New|") == ["", "Old", "New"]
    assert s._split_md_row("|Metric|Old||") == ["Metric", "Old", ""]
    assert s._split_md_row(r"|a \| b|c|") == [r"a \| b", "c"]


def test_wiki_row_gives_an_empty_cell_a_space():
    assert s._wiki_row(["", "A"], header=True) == "|| ||A||"
    assert s._wiki_row(["A", ""]) == "|A| |"


def test_indent_unit_inferred_from_document():
    assert s._indent_unit("- a\n    - b") == 4
    assert s._indent_unit("- a\n  - b") == 2
    assert s._indent_unit("- a\n- b") == 2
    assert s._indent_unit("```\n    - fenced\n```\n- a\n  - b") == 2


# ---------------------------------------------------------------- errors

@pytest.mark.parametrize("status,expected", [
    (401, "ATLASSIAN_API_TOKEN"),
    (403, "permission denied"),
    (404, "not found"),
    (409, "version conflict"),
])
def test_req_maps_status_to_actionable_message(status, expected):
    with fake_api({("GET", "/rest/api/3/issue/DEMO-1"): (status, {"errorMessages": ["nope"]})}):
        with pytest.raises(s.AtlassianError) as e:
            s.jira_get_issue("DEMO-1")
    assert expected in str(e.value)
    assert e.value.status_code == status


def test_req_error_names_the_object_and_keeps_api_detail():
    with fake_api({("GET", "/rest/api/3/issue/DEMO-1"): (404, {"errorMessages": ["Issue does not exist"]})}):
        with pytest.raises(s.AtlassianError) as e:
            s.jira_get_issue("DEMO-1")
    assert "issue DEMO-1" in str(e.value)
    assert "Issue does not exist" in str(e.value)


# ---------------------------------------------------------------- storage fidelity

def test_get_page_storage_is_byte_identical_to_what_confluence_stores():
    with fake_api({("GET", "/wiki/api/v2/pages/123"): page_response()}):
        page = s.confluence_get_page("123", body_format="storage")
    assert page["body"] == RICH_PAGE
    assert page["version"] == 7


def test_the_fixture_carries_what_markdown_cannot_express():
    for construct in ['ac:name="info"', 'ac:name="status"', 'ac:name="expand"',
                      "ac:task-list", "ac:layout", "ri:attachment",
                      "ac:inline-comment-marker"]:
        assert construct in RICH_PAGE


def test_markdown_rendering_loses_the_inline_comment_anchor():
    with fake_api({("GET", "/wiki/api/v2/pages/123"): page_response()}):
        markdown = s.confluence_get_page("123")["body"]
        storage = s.confluence_get_page("123", body_format="storage")["body"]
    assert "ac:inline-comment-marker" in storage
    assert "ac:inline-comment-marker" not in markdown


def test_get_page_markdown_still_renders_for_reading():
    with fake_api({("GET", "/wiki/api/v2/pages/123"): page_response()}):
        page = s.confluence_get_page("123")
    assert "Overview of the" in page["body"]
    assert "<ac:structured-macro" not in page["body"]


def test_storage_read_write_round_trip_is_byte_identical():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "Handbook",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        read = s.confluence_get_page("123", body_format="storage")
        s.confluence_update_page("123", read["body"], body_format="storage")
    assert json_body(api.calls("PUT")[0])["body"]["value"] == RICH_PAGE


@pytest.mark.parametrize("call", [
    lambda: s.confluence_rename_page("123", "[Draft] Handbook"),
    lambda: s.confluence_move_page("123", "555"),
    lambda: s.confluence_update_page("123", title="[Draft] Handbook"),
])
def test_metadata_only_writes_preserve_the_stored_body_byte_for_byte(call):
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "x",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        call()
    assert json_body(api.calls("PUT")[0])["body"]["value"] == RICH_PAGE


def test_rename_changes_only_the_title_and_bumps_the_version():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "[Draft] Handbook",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        out = s.confluence_rename_page("123", "[Draft] Handbook", version_message="prefix")
    sent = json_body(api.calls("PUT")[0])
    assert sent["title"] == "[Draft] Handbook"
    assert sent["version"] == {"number": 8, "message": "prefix"}
    assert "parentId" not in sent
    assert out == {"id": "123", "title": "[Draft] Handbook", "version": 8}


def test_move_sets_the_parent_and_keeps_the_title():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(title="Handbook"),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "Handbook",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        s.confluence_move_page("123", "555")
    sent = json_body(api.calls("PUT")[0])
    assert sent["parentId"] == "555"
    assert sent["title"] == "Handbook"


def test_update_page_markdown_path_renders_to_storage():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "Handbook",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        s.confluence_update_page("123", "# Heading\n\nBody text.")
    assert json_body(api.calls("PUT")[0])["body"]["value"] == "<h1>Heading</h1>\n<p>Body text.</p>"


# ---------------------------------------------------------------- optimistic concurrency

def test_expected_version_mismatch_raises_before_writing():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(version=7)}
    with fake_api(routes) as api:
        with pytest.raises(s.AtlassianError) as e:
            s.confluence_rename_page("123", "New title", expected_version=6)
    assert api.calls("PUT") == []
    assert "expected version 6" in str(e.value)
    assert "current version is 7" in str(e.value)


def test_expected_version_match_writes():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(version=7),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "New title",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        s.confluence_rename_page("123", "New title", expected_version=7)
    assert len(api.calls("PUT")) == 1


def test_server_side_conflict_reports_both_version_numbers():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(version=7),
              ("PUT", "/wiki/api/v2/pages/123"): (409, {"errors": [{"title": "conflict"}]})}
    with fake_api(routes):
        with pytest.raises(s.AtlassianError) as e:
            s.confluence_rename_page("123", "New title")
    assert "write version 8" in str(e.value)
    assert "read of version 7" in str(e.value)


# ---------------------------------------------------------------- append

def test_append_to_page_concatenates_without_touching_existing_bytes():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "Handbook",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        s.confluence_append_to_page("123", "Appended note.")
    sent = json_body(api.calls("PUT")[0])["body"]["value"]
    assert sent.startswith(RICH_PAGE)
    assert sent[len(RICH_PAGE):] == "<p>Appended note.</p>"


def test_append_table_row_targets_the_first_table_by_default():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "Handbook",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        s.confluence_append_table_row("123", ["Label writes", "Green"])
    sent = json_body(api.calls("PUT")[0])["body"]["value"]
    row = "<tr><td><p>Label writes</p></td><td><p>Green</p></td></tr>"
    assert row in sent
    assert sent.index(row) < sent.index("2025-01-15")  # in the first table, not the second


def test_append_table_row_addresses_the_second_table():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "Handbook",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        s.confluence_append_table_row("123", ["2025-01-16", "Row appended"], table_index=1)
    sent = json_body(api.calls("PUT")[0])["body"]["value"]
    row = "<tr><td><p>2025-01-16</p></td><td><p>Row appended</p></td></tr>"
    assert sent.index(row) > sent.index("Page created")


def test_append_table_row_changes_exactly_one_region_of_a_40_row_table():
    rows = "".join(f"<tr><td><p>{i}</p></td></tr>" for i in range(40))
    body = f"<p>Lead</p><table><tbody><tr><th><p>N</p></th></tr>{rows}</tbody></table><p>Tail</p>"
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(storage=body),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "t",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        s.confluence_append_table_row("123", ["40"])
    sent = json_body(api.calls("PUT")[0])["body"]["value"]
    inserted = "<tr><td><p>40</p></td></tr>"
    at = sent.index("<tr><td><p>39</p></td></tr>") + len("<tr><td><p>39</p></td></tr>")
    assert sent[at:at + len(inserted)] == inserted
    assert sent[:at] + sent[at + len(inserted):] == body


def test_append_table_row_validates_the_index_before_writing():
    with fake_api({("GET", "/wiki/api/v2/pages/123"): page_response()}) as api:
        with pytest.raises(IndexError) as e:
            s.confluence_append_table_row("123", ["x"], table_index=5)
    assert api.calls("PUT") == []
    assert "has 2 table(s)" in str(e.value)


def test_append_table_row_renders_inline_markdown_and_empty_cells():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(),
              ("PUT", "/wiki/api/v2/pages/123"): (200, {"id": "123", "title": "t",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        s.confluence_append_table_row("123", ["**Done**", "", "[PR](https://example.com/pr/1)"])
    sent = json_body(api.calls("PUT")[0])["body"]["value"]
    assert ("<tr><td><p><strong>Done</strong></p></td><td><p /></td>"
            '<td><p><a href="https://example.com/pr/1">PR</a></p></td></tr>') in sent


def test_append_table_row_rejects_block_content_in_a_cell():
    with fake_api({("GET", "/wiki/api/v2/pages/123"): page_response()}) as api:
        with pytest.raises(ValueError):
            s.confluence_append_table_row("123", ["# Heading"])
    assert api.calls("PUT") == []


# ---------------------------------------------------------------- labels

def label_payload(*names):
    return {"results": [{"prefix": "global", "name": n} for n in names]}


def test_get_labels():
    with fake_api({("GET", "/wiki/rest/api/content/123/label"):
                   (200, label_payload("handbook", "how-to"))}):
        assert s.confluence_get_labels("123") == {"labels": ["handbook", "how-to"]}


def test_add_labels_returns_the_full_resulting_set():
    routes = {("POST", "/wiki/rest/api/content/123/label"):
              (200, label_payload("existing", "handbook", "how-to"))}
    with fake_api(routes) as api:
        out = s.confluence_add_labels("123", ["handbook", "how-to"])
    assert out == {"labels": ["existing", "handbook", "how-to"]}
    assert json_body(api.calls("POST")[0]) == [{"prefix": "global", "name": "handbook"},
                                               {"prefix": "global", "name": "how-to"}]
    assert api.calls("PUT") == []  # labels never create a page version


def test_add_existing_label_is_idempotent():
    routes = {("POST", "/wiki/rest/api/content/123/label"): (200, label_payload("handbook"))}
    with fake_api(routes):
        assert s.confluence_add_labels("123", ["handbook"]) == {"labels": ["handbook"]}


def test_add_labels_rejects_whitespace_before_calling_the_api():
    with fake_api({}) as api:
        with pytest.raises(ValueError):
            s.confluence_add_labels("123", ["two words"])
    assert api.requests == []


def test_remove_label_deletes_by_name_and_returns_the_remainder():
    routes = {("DELETE", "/wiki/rest/api/content/123/label"): (204, None),
              ("GET", "/wiki/rest/api/content/123/label"): (200, label_payload("handbook"))}
    with fake_api(routes) as api:
        assert s.confluence_remove_label("123", "how-to") == {"labels": ["handbook"]}
    assert api.calls("DELETE")[0].url.params["name"] == "how-to"


# ---------------------------------------------------------------- delete

def test_delete_page_trashes_by_default():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(),
              ("DELETE", "/wiki/api/v2/pages/123"): (204, None)}
    with fake_api(routes) as api:
        assert s.confluence_delete_page("123") == {"id": "123", "status": "trashed"}
    assert len(api.calls("DELETE")) == 1
    assert "purge" not in api.calls("DELETE")[0].url.params


def test_purge_trashes_first_then_purges():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(),
              ("DELETE", "/wiki/api/v2/pages/123"): [(204, None), (204, None)]}
    with fake_api(routes) as api:
        assert s.confluence_delete_page("123", purge=True) == {"id": "123", "status": "purged"}
    deletes = api.calls("DELETE")
    assert len(deletes) == 2
    assert deletes[1].url.params["purge"] == "true"


def test_purge_of_an_already_trashed_page_skips_the_trash_step():
    routes = {("GET", "/wiki/api/v2/pages/123"): page_response(status="trashed"),
              ("DELETE", "/wiki/api/v2/pages/123"): (204, None)}
    with fake_api(routes) as api:
        s.confluence_delete_page("123", purge=True)
    assert len(api.calls("DELETE")) == 1


# ---------------------------------------------------------------- space and tree

def test_get_space_by_key():
    routes = {("GET", "/wiki/api/v2/spaces"):
              (200, {"results": [{"id": "4700", "key": "DEMO", "name": "Demo Space"}]})}
    with fake_api(routes) as api:
        assert s.confluence_get_space("DEMO") == {"id": "4700", "key": "DEMO", "name": "Demo Space"}
    assert api.calls("GET")[0].url.params["keys"] == "DEMO"


def test_get_space_by_numeric_id_uses_the_id_parameter():
    routes = {("GET", "/wiki/api/v2/spaces"):
              (200, {"results": [{"id": "4700", "key": "DEMO", "name": "Demo Space"}]})}
    with fake_api(routes) as api:
        s.confluence_get_space("4700")
    assert api.calls("GET")[0].url.params["ids"] == "4700"


def test_unknown_space_raises():
    with fake_api({("GET", "/wiki/api/v2/spaces"): (200, {"results": []})}):
        with pytest.raises(RuntimeError, match="No Confluence space 'NOPE'"):
            s.confluence_get_space("NOPE")


def test_create_page_accepts_a_space_key():
    routes = {("GET", "/wiki/api/v2/spaces"):
              (200, {"results": [{"id": "4700", "key": "DEMO", "name": "Demo Space"}]}),
              ("POST", "/wiki/api/v2/pages"):
              (200, {"id": "900", "_links": {"webui": "/spaces/DEMO/pages/900"}})}
    with fake_api(routes) as api:
        out = s.confluence_create_page("DEMO", "New page", "Body")
    assert json_body(api.calls("POST")[0])["spaceId"] == "4700"
    assert out["id"] == "900"
    assert out["url"].endswith("/wiki/spaces/DEMO/pages/900")


def test_create_page_with_a_numeric_space_id_skips_the_lookup():
    routes = {("POST", "/wiki/api/v2/pages"):
              (200, {"id": "900", "_links": {"webui": "/spaces/DEMO/pages/900"}})}
    with fake_api(routes) as api:
        s.confluence_create_page("4700", "New page", "Body")
    assert json_body(api.calls("POST")[0])["spaceId"] == "4700"
    assert api.calls("GET", "/wiki/api/v2/spaces") == []


def test_get_children_walks_depth_and_follows_the_cursor():
    routes = {
        ("GET", "/wiki/api/v2/pages/1/children"): [
            (200, {"results": [{"id": "2", "title": "Child A"}],
                   "_links": {"next": "/wiki/api/v2/pages/1/children?cursor=abc"}}),
            (200, {"results": [{"id": "3", "title": "Child B"}], "_links": {}}),
        ],
        ("GET", "/wiki/api/v2/pages/2/children"): (200, {"results": [{"id": "4", "title": "Grandchild"}]}),
        ("GET", "/wiki/api/v2/pages/3/children"): (200, {"results": []}),
    }
    with fake_api(routes):
        out = s.confluence_get_children("1", depth=2)
    assert out == [{"id": "2", "title": "Child A", "parent_id": "1"},
                   {"id": "4", "title": "Grandchild", "parent_id": "2"},
                   {"id": "3", "title": "Child B", "parent_id": "1"}]


def test_get_children_depth_one_does_not_recurse():
    routes = {("GET", "/wiki/api/v2/pages/1/children"):
              (200, {"results": [{"id": "2", "title": "Child A"}]})}
    with fake_api(routes) as api:
        s.confluence_get_children("1")
    assert len(api.calls("GET")) == 1


# ---------------------------------------------------------------- upsert

SPACE_ROUTE = {("GET", "/wiki/api/v2/spaces"):
               (200, {"results": [{"id": "4700", "key": "DEMO", "name": "Demo Space"}]})}


def search_payload(*pages):
    return {"results": [{"content": {"id": pid}, "title": t, "url": f"/pages/{pid}"}
                        for pid, t in pages]}


def test_upsert_creates_when_no_page_has_the_title():
    routes = {**SPACE_ROUTE,
              ("GET", "/wiki/rest/api/search"): (200, search_payload()),
              ("POST", "/wiki/api/v2/pages"):
              (200, {"id": "900", "_links": {"webui": "/spaces/DEMO/pages/900"}})}
    with fake_api(routes) as api:
        out = s.confluence_upsert_page("DEMO", "Release Notes", "Body")
    assert out["action"] == "created"
    assert api.calls("PUT") == []


def test_upsert_updates_the_single_match():
    routes = {**SPACE_ROUTE,
              ("GET", "/wiki/rest/api/search"): (200, search_payload(("321", "Release Notes"))),
              ("GET", "/wiki/api/v2/pages/321"): page_response(page_id="321", title="Release Notes"),
              ("PUT", "/wiki/api/v2/pages/321"): (200, {"id": "321", "title": "Release Notes",
                                                        "version": {"number": 8}})}
    with fake_api(routes) as api:
        out = s.confluence_upsert_page("DEMO", "Release Notes", "New body")
    assert out["action"] == "updated"
    assert out["version"] == 8
    assert api.calls("POST") == []


def test_upsert_refuses_to_write_when_the_title_is_ambiguous():
    routes = {**SPACE_ROUTE,
              ("GET", "/wiki/rest/api/search"):
              (200, search_payload(("321", "Release Notes"), ("322", "Release Notes")))}
    with fake_api(routes) as api:
        with pytest.raises(RuntimeError, match="matches 2 pages"):
            s.confluence_upsert_page("DEMO", "Release Notes", "Body")
    assert api.calls("PUT") == []
    assert api.calls("POST") == []


def test_upsert_ignores_partial_title_matches_from_cql():
    routes = {**SPACE_ROUTE,
              ("GET", "/wiki/rest/api/search"):
              (200, search_payload(("321", "Release Notes Archive"))),
              ("POST", "/wiki/api/v2/pages"):
              (200, {"id": "900", "_links": {"webui": "/spaces/DEMO/pages/900"}})}
    with fake_api(routes):
        assert s.confluence_upsert_page("DEMO", "Release Notes", "Body")["action"] == "created"


# ---------------------------------------------------------------- worklogs

def worklog(wid, account, name, started, seconds=3600, comment="Investigated the failure"):
    return {"id": str(wid), "author": {"accountId": account, "displayName": name},
            "started": started, "timeSpent": f"{seconds // 3600}h",
            "timeSpentSeconds": seconds,
            "comment": {"type": "doc", "version": 1,
                        "content": [{"type": "paragraph",
                                     "content": [{"type": "text", "text": comment}]}]}}


def paged_worklogs(entries, page_size=20):
    def route(request):
        start = int(request.url.params.get("startAt", 0))
        return 200, {"startAt": start, "maxResults": page_size, "total": len(entries),
                     "worklogs": entries[start:start + page_size]}
    return route


def test_get_worklogs_pages_past_twenty_entries():
    entries = [worklog(i, "acc-1", "Alice Example", "2026-07-07T09:00:00.000+1000")
               for i in range(47)]
    routes = {("GET", "/rest/api/3/issue/DEMO-1/worklog"): paged_worklogs(entries)}
    with fake_api(routes) as api:
        out = s.jira_get_worklogs("DEMO-1")
    assert out["total"] == 47 == len(out["worklogs"])
    assert len(api.calls("GET")) == 3
    assert [w["id"] for w in out["worklogs"]] == [str(i) for i in range(47)]


def test_get_worklogs_shapes_each_entry_and_renders_the_comment():
    entries = [worklog(1, "acc-1", "Alice Example", "2026-07-07T09:00:00.000+1000")]
    with fake_api({("GET", "/rest/api/3/issue/DEMO-1/worklog"): paged_worklogs(entries)}):
        out = s.jira_get_worklogs("DEMO-1")
    assert out["worklogs"][0] == {"id": "1", "author": "Alice Example",
                                  "started": "2026-07-07T09:00:00.000+1000",
                                  "time_spent": "1h", "time_spent_seconds": 3600,
                                  "comment": "Investigated the failure"}


def test_get_worklogs_filters_by_account_id_and_display_name():
    entries = [worklog(1, "acc-1", "Alice Example", "2026-07-07T09:00:00.000+1000"),
               worklog(2, "acc-2", "Bob Sample", "2026-07-07T09:00:00.000+1000")]
    routes = {("GET", "/rest/api/3/issue/DEMO-1/worklog"): paged_worklogs(entries)}
    with fake_api(routes):
        assert s.jira_get_worklogs("DEMO-1", author="acc-2")["worklogs"][0]["id"] == "2"
    with fake_api(routes):
        assert s.jira_get_worklogs("DEMO-1", author="bob")["worklogs"][0]["id"] == "2"
    with fake_api(routes):
        assert s.jira_get_worklogs("DEMO-1", author="nobody")["total"] == 0


def test_get_worklogs_dates_are_read_in_their_own_offset():
    # 08:00+10:00 is the previous day in UTC; 20:00-07:00 is the next day in UTC.
    entries = [worklog(1, "a", "A", "2026-07-02T08:00:00.000+1000"),
               worklog(2, "a", "A", "2026-07-01T20:00:00.000-0700")]
    routes = {("GET", "/rest/api/3/issue/DEMO-1/worklog"): paged_worklogs(entries)}
    with fake_api(routes):
        assert [w["id"] for w in s.jira_get_worklogs("DEMO-1", started_after="2026-07-02")["worklogs"]] == ["1"]
    with fake_api(routes):
        assert [w["id"] for w in s.jira_get_worklogs("DEMO-1", started_before="2026-07-01")["worklogs"]] == ["2"]


def test_get_worklogs_date_bounds_are_inclusive():
    entries = [worklog(1, "a", "A", "2026-07-07T09:00:00.000+1000")]
    routes = {("GET", "/rest/api/3/issue/DEMO-1/worklog"): paged_worklogs(entries)}
    with fake_api(routes):
        out = s.jira_get_worklogs("DEMO-1", started_after="2026-07-07", started_before="2026-07-07")
    assert out["total"] == 1


def test_parse_started_handles_both_offset_spellings():
    from datetime import date
    assert s._parse_started("2026-07-07T09:00:00.000+1000") == date(2026, 7, 7)
    assert s._parse_started("2026-07-07T09:00:00.000+10:00") == date(2026, 7, 7)
    assert s._parse_started("not a date") is None


# ---------------------------------------------------------------- links, accounts, fields

LINK_TYPES = {"issueLinkTypes": [
    {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
    {"id": "10001", "name": "Relates", "inward": "relates to", "outward": "relates to"}]}


def test_get_link_types():
    with fake_api({("GET", "/rest/api/3/issueLinkType"): (200, LINK_TYPES)}):
        out = s.jira_get_link_types()
    assert out[0] == {"id": "10000", "name": "Blocks",
                      "inward": "is blocked by", "outward": "blocks"}


def test_link_issues_resolves_the_name_and_sends_from_as_outward():
    routes = {("GET", "/rest/api/3/issueLinkType"): (200, LINK_TYPES),
              ("POST", "/rest/api/3/issueLink"): (201, None)}
    with fake_api(routes) as api:
        assert s.jira_link_issues("DEMO-1", "DEMO-2", "blocks") == {"ok": True}
    assert json_body(api.calls("POST")[0]) == {"type": {"id": "10000"},
                                               "outwardIssue": {"key": "DEMO-1"},
                                               "inwardIssue": {"key": "DEMO-2"}}


def test_link_issues_lists_the_options_when_the_type_is_unknown():
    routes = {("GET", "/rest/api/3/issueLinkType"): (200, LINK_TYPES)}
    with fake_api(routes) as api:
        with pytest.raises(RuntimeError, match="Blocks, Relates"):
            s.jira_link_issues("DEMO-1", "DEMO-2", "Duplicates")
    assert api.calls("POST") == []


def test_lookup_account():
    routes = {("GET", "/rest/api/3/user/search"):
              (200, [{"accountId": "acc-1", "displayName": "Alice Example",
                      "emailAddress": "alice@example.com"},
                     {"accountId": "acc-2", "displayName": "Alice Test"}])}
    with fake_api(routes) as api:
        out = s.jira_lookup_account("alice")
    assert out[0] == {"account_id": "acc-1", "display_name": "Alice Example",
                      "email": "alice@example.com"}
    assert out[1]["email"] is None
    assert api.calls("GET")[0].url.params["query"] == "alice"


ISSUE_TYPES = (200, {"startAt": 0, "total": 2,
                     "issueTypes": [{"id": "10001", "name": "Task"},
                                    {"id": "10002", "name": "Bug"}]})


def test_get_field_ids_resolves_the_issue_type_and_pages_the_fields():
    def fields(request):
        start = int(request.url.params.get("startAt", 0))
        values = [{"fieldId": "summary", "name": "Summary", "required": True,
                   "schema": {"type": "string"}},
                  {"fieldId": "customfield_12345", "name": "Acceptance Criteria",
                   "required": False, "schema": {"type": "string"}}]
        return 200, {"startAt": start, "total": 2, "fields": values[start:start + 1]}

    routes = {("GET", "/rest/api/3/issue/createmeta/DEMO/issuetypes"): ISSUE_TYPES,
              ("GET", "/rest/api/3/issue/createmeta/DEMO/issuetypes/10001"): fields}
    with fake_api(routes):
        out = s.jira_get_field_ids("DEMO", "task")
    assert out == [{"id": "summary", "name": "Summary", "type": "string", "required": True},
                   {"id": "customfield_12345", "name": "Acceptance Criteria",
                    "type": "string", "required": False}]


def test_get_field_ids_lists_the_issue_types_when_unknown():
    with fake_api({("GET", "/rest/api/3/issue/createmeta/DEMO/issuetypes"): ISSUE_TYPES}):
        with pytest.raises(RuntimeError, match="Task, Bug"):
            s.jira_get_field_ids("DEMO", "Epic")


# ---------------------------------------------------------------- batch read

def issue_payload(*keys):
    return {"issues": [{"key": k, "fields": {"summary": f"Summary {k}",
                                             "status": {"name": "In Progress"}}} for k in keys]}


def test_get_issues_returns_the_requested_order():
    routes = {("GET", "/rest/api/3/search/jql"): (200, issue_payload("DEMO-2", "DEMO-1", "DEMO-3"))}
    with fake_api(routes) as api:
        out = s.jira_get_issues(["DEMO-3", "DEMO-1", "DEMO-2"])
    assert [i["key"] for i in out["issues"]] == ["DEMO-3", "DEMO-1", "DEMO-2"]
    assert out["missing"] == []
    assert len(api.calls("GET")) == 1
    assert api.calls("GET")[0].url.params["jql"] == "key in (DEMO-3,DEMO-1,DEMO-2)"


def test_get_issues_shapes_fields_like_a_single_read():
    routes = {("GET", "/rest/api/3/search/jql"): (200, issue_payload("DEMO-1"))}
    with fake_api(routes):
        out = s.jira_get_issues(["DEMO-1"])
    assert out["issues"][0] == {"key": "DEMO-1", "summary": "Summary DEMO-1", "status": "In Progress"}


def test_get_issues_reports_keys_the_search_omits():
    routes = {("GET", "/rest/api/3/search/jql"): (200, issue_payload("DEMO-1"))}
    with fake_api(routes):
        out = s.jira_get_issues(["DEMO-1", "DEMO-404"])
    assert [i["key"] for i in out["issues"]] == ["DEMO-1"]
    assert out["missing"] == ["DEMO-404"]


def test_get_issues_isolates_a_key_that_jql_rejects():
    def route(request):
        keys = request.url.params["jql"].split("(")[1].rstrip(")").split(",")
        if "NOPE-1" in keys:
            return 400, {"errorMessages": ["An issue with key 'NOPE-1' does not exist"]}
        return 200, issue_payload(*keys)

    with fake_api({("GET", "/rest/api/3/search/jql"): route}):
        out = s.jira_get_issues(["DEMO-1", "DEMO-2", "NOPE-1", "DEMO-3"])
    assert [i["key"] for i in out["issues"]] == ["DEMO-1", "DEMO-2", "DEMO-3"]
    assert out["missing"] == ["NOPE-1"]


def test_get_issues_chunks_at_one_hundred_keys():
    keys = [f"DEMO-{i}" for i in range(250)]

    def route(request):
        batch = request.url.params["jql"].split("(")[1].rstrip(")").split(",")
        assert len(batch) <= 100
        return 200, issue_payload(*batch)

    with fake_api({("GET", "/rest/api/3/search/jql"): route}) as api:
        out = s.jira_get_issues(keys)
    assert len(api.calls("GET")) == 3
    assert [i["key"] for i in out["issues"]] == keys


def test_get_issues_propagates_non_400_errors():
    with fake_api({("GET", "/rest/api/3/search/jql"): (401, {"message": "no"})}):
        with pytest.raises(s.AtlassianError):
            s.jira_get_issues(["DEMO-1"])


# ---------------------------------------------------------------- comments and edits

def test_get_comments_pages_from_the_given_offset():
    routes = {("GET", "/rest/api/3/issue/DEMO-1/comment"):
              (200, {"total": 42, "startAt": 20,
                     "comments": [{"id": "1", "author": {"displayName": "Alice"},
                                   "created": "2026-08-01T09:00:00.000+1000", "body": "hi"}]})}
    with fake_api(routes) as api:
        out = s.jira_get_comments("DEMO-1", limit=10, start=20)
    assert api.calls("GET")[0].url.params["startAt"] == "20"
    assert api.calls("GET")[0].url.params["maxResults"] == "10"
    assert out["total"] == 42 and out["start"] == 20
    assert out["comments"][0]["author"] == "Alice"


def test_get_comments_caps_the_page_size_at_fifty():
    routes = {("GET", "/rest/api/3/issue/DEMO-1/comment"): (200, {"total": 0, "comments": []})}
    with fake_api(routes) as api:
        s.jira_get_comments("DEMO-1", limit=500)
    assert api.calls("GET")[0].url.params["maxResults"] == "50"


def test_edit_issue_converts_description_by_default():
    with fake_api({("PUT", "/rest/api/2/issue/DEMO-1"): (204, None)}) as api:
        s.jira_edit_issue("DEMO-1", {"description": "## Heading", "summary": "## not markdown"})
    sent = json_body(api.calls("PUT")[0])["fields"]
    assert sent["description"] == "h2. Heading"
    assert sent["summary"] == "## not markdown"


def test_edit_issue_converts_the_named_fields():
    with fake_api({("PUT", "/rest/api/2/issue/DEMO-1"): (204, None)}) as api:
        s.jira_edit_issue("DEMO-1", {"description": "## D", "customfield_12345": "## AC"},
                          markdown_fields=["description", "customfield_12345"])
    sent = json_body(api.calls("PUT")[0])["fields"]
    assert sent == {"description": "h2. D", "customfield_12345": "h2. AC"}


def test_edit_issue_converts_nothing_when_given_an_empty_list():
    with fake_api({("PUT", "/rest/api/2/issue/DEMO-1"): (204, None)}) as api:
        s.jira_edit_issue("DEMO-1", {"description": "## D"}, markdown_fields=[])
    assert json_body(api.calls("PUT")[0])["fields"] == {"description": "## D"}


def test_edit_issue_leaves_non_string_fields_alone():
    with fake_api({("PUT", "/rest/api/2/issue/DEMO-1"): (204, None)}) as api:
        s.jira_edit_issue("DEMO-1", {"labels": ["a"], "assignee": {"accountId": "acc-1"}})
    assert json_body(api.calls("PUT")[0])["fields"] == {"labels": ["a"],
                                                        "assignee": {"accountId": "acc-1"}}


# ---------------------------------------------------------------- configuration

def test_import_needs_no_credentials_and_the_first_call_names_what_is_missing(monkeypatch):
    monkeypatch.delenv("ATLASSIAN_API_TOKEN")
    with pytest.raises(RuntimeError, match="ATLASSIAN_API_TOKEN"):
        s.jira_get_issue("DEMO-1")


def test_site_without_a_scheme_gets_https():
    cfg = s.load_settings({"ATLASSIAN_SITE": "example.atlassian.net/", "ATLASSIAN_EMAIL": "e",
                           "ATLASSIAN_API_TOKEN": "t"})
    assert cfg["site"] == "https://example.atlassian.net"


def test_env_file_is_read_and_real_environment_wins(tmp_path):
    env_file = tmp_path / "jira-lean.env"
    env_file.write_text("# comment\nATLASSIAN_SITE=https://from-file.example.com\n"
                        "ATLASSIAN_EMAIL=file@example.com\nATLASSIAN_API_TOKEN='file-token'\n",
                        encoding="utf-8")
    cfg = s.load_settings({"JIRA_LEAN_ENV_FILE": str(env_file),
                           "ATLASSIAN_EMAIL": "env@example.com"})
    assert cfg["site"] == "https://from-file.example.com"
    assert cfg["email"] == "env@example.com"
    assert cfg["token"] == "file-token"


def test_a_missing_env_file_is_an_error_not_a_silent_fallback(tmp_path):
    with pytest.raises(RuntimeError, match="does not exist"):
        s.load_settings({"JIRA_LEAN_ENV_FILE": str(tmp_path / "nope.env")})


def test_default_fields_are_configurable(monkeypatch):
    monkeypatch.setenv("JIRA_LEAN_DEFAULT_FIELDS", "summary, status")
    routes = {("GET", "/rest/api/3/issue/DEMO-1"):
              (200, {"key": "DEMO-1", "fields": {"summary": "S", "status": {"name": "Done"}}})}
    with fake_api(routes) as api:
        out = s.jira_get_issue("DEMO-1")
    assert api.calls("GET")[0].url.params["fields"] == "summary,status"
    assert out == {"key": "DEMO-1", "url": f"{TEST_SITE}/browse/DEMO-1",
                   "summary": "S", "status": "Done"}


@pytest.mark.parametrize("call", [
    lambda: s.jira_add_comment("DEMO-1", "hi"),
    lambda: s.jira_edit_issue("DEMO-1", {"summary": "x"}),
    lambda: s.confluence_add_labels("123", ["x"]),
    lambda: s.confluence_rename_page("123", "New title"),
])
def test_read_only_mode_refuses_writes_before_sending_anything(monkeypatch, call):
    monkeypatch.setenv("JIRA_LEAN_READ_ONLY", "true")
    with fake_api({("GET", "/wiki/api/v2/pages/123"): page_response()}) as api:
        with pytest.raises(PermissionError, match="JIRA_LEAN_READ_ONLY"):
            call()
    assert [r.method for r in api.requests if r.method != "GET"] == []


def test_read_only_mode_still_allows_reads(monkeypatch):
    monkeypatch.setenv("JIRA_LEAN_READ_ONLY", "1")
    with fake_api({("GET", "/wiki/api/v2/pages/123"): page_response()}):
        assert s.confluence_get_page("123")["id"] == "123"
