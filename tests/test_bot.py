from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

import bot

URL = bot.INTRANET
LOGIN = """<html><body><form class="gb-login-form" action="/prijava/" method="post">
<input type="hidden" name="gimbezigrad_login_nonce" value="fixture-nonce">
<input type="hidden" name="_wp_http_referer" value="/prijava/">
<input name="log"><input name="pwd" type="password">
<button name="gimbezigrad_login_submit" value="1">Potrdi</button>
</form></body></html>"""


def page(text="Tekmovanje iz matematike bo v petek ob 14. uri.", href="/document.pdf"):
    return f'''<html><body><header>Navigation</header><main><h1>Intranet</h1>
    <article><h2>Obvestila</h2><p>{text}</p><a href="{href}">Razpis</a></article>
    <script>nonce=random</script><form><input name="nonce" value="random"></form>
    </main><footer>Copyright changes</footer></body></html>'''


class FakeSchool:
    def __init__(self, html=None, error=False):
        self.html = html or page()
        self.error = error

    def fetch(self, url):
        if self.error:
            raise bot.MonitorError("School unavailable")
        return self.html

    def file_snapshot(self, url, label):
        return {"title": label, "lines": [label], "kind": "file", "sha256": "fixture", "bytes": 100}


class FakeDiscord:
    def __init__(self, fail=False, destination="one"):
        self.messages = []
        self.fail = fail
        self.target = destination

    def destination(self):
        return self.target

    def send(self, text, nonce=""):
        if self.fail:
            raise bot.MonitorError("Network failed")
        self.messages.append(text)


def test_ignore_chrome_nonces_whitespace_and_capture_link_changes():
    config = bot.Config()
    original = bot.extract(page(), URL, config)
    assert original == bot.extract(
        page()
        .replace("Navigation", "Different nav")
        .replace("random", "another_nonce")
        .replace("v petek", "v  \n petek"),
        URL,
        config,
    )
    changed = bot.extract(page(href="/new-document.pdf"), URL, config)
    message = bot.change_message(original, changed, URL, "01. 10. 2026")
    assert "new-document.pdf" in message
    assert "Navigation" not in " ".join(original["lines"])


@pytest.mark.parametrize("html", [LOGIN, "<html><main></main></html>", "<html>error</html>"])
def test_login_empty_and_missing_content_rejected(html):
    with pytest.raises(bot.MonitorError):
        bot.extract(html, URL, bot.Config())


def test_selector_failure_is_not_empty_success():
    with pytest.raises(bot.MonitorError):
        bot.extract(page(), URL, bot.Config(selector=".nonexistent"))


def test_first_run_silent_then_one_edit_alert_and_no_duplicate(tmp_path):
    state = tmp_path / "state.json"
    sender = FakeDiscord()
    bot.check(bot.Config(), path=state, school=FakeSchool(), sender=sender)
    assert not sender.messages
    updated = FakeSchool(page("Tekmovanje je prestavljeno na ponedeljek ob 15. uri."))
    bot.check(bot.Config(), path=state, school=updated, sender=sender)
    bot.check(bot.Config(), path=state, school=updated, sender=sender)
    assert len(sender.messages) == 1
    assert "ponedeljek" in sender.messages[0]
    assert not bot.load_state(state)["outbox"]


def test_failed_discord_delivery_survives_restart(tmp_path):
    state = tmp_path / "state.json"
    bot.check(bot.Config(), path=state, school=FakeSchool(), sender=FakeDiscord())
    updated = FakeSchool(page("Pomembna sprememba: jutri odpadejo vse interesne dejavnosti."))
    with pytest.raises(bot.MonitorError):
        bot.check(bot.Config(), path=state, school=updated, sender=FakeDiscord(fail=True))
    assert len(bot.load_state(state)["outbox"]) == 1
    sender = FakeDiscord()
    bot.check(bot.Config(), path=state, school=updated, sender=sender)
    assert len(sender.messages) == 1
    assert not bot.load_state(state)["outbox"]


def test_partial_delivery_only_retries_unsent_notices(tmp_path):
    state_path = tmp_path / "state.json"
    state = bot.load_state(state_path)
    state["outbox"] = [{"content": s, "destination": "one", "nonce": s} for s in ["a", "b"]]
    bot.save_state(state, state_path)

    class FailsSecond(FakeDiscord):
        def send(self, text, nonce=""):
            if text == "b":
                raise bot.MonitorError("failed")
            super().send(text, nonce)

    with pytest.raises(bot.MonitorError):
        bot.flush(state, FailsSecond(), state_path)
    assert [i["content"] for i in bot.load_state(state_path)["outbox"]] == ["b"]
    with pytest.raises(bot.MonitorError, match="destination changed"):
        bot.flush(bot.load_state(state_path), FakeDiscord(destination="other"), state_path)


def test_failed_fetch_preserves_previous_snapshot(tmp_path):
    state = tmp_path / "state.json"
    bot.check(bot.Config(), path=state, school=FakeSchool(), sender=FakeDiscord())
    before = state.read_bytes()
    with pytest.raises(bot.MonitorError):
        bot.check(bot.Config(), path=state, school=FakeSchool(error=True), sender=FakeDiscord())
    assert state.read_bytes() == before


def test_preview_is_read_only(tmp_path):
    path = tmp_path / "state.json"
    bot.check(bot.Config(), path=path, preview=True, school=FakeSchool())
    assert not path.exists()


@pytest.mark.parametrize("date", ["2026-03-29", "2026-10-25", "2026-09-30"])
def test_daily_time_uses_ljubljana_and_persists_across_restarts(date):
    config = bot.Config()
    local = ZoneInfo("Europe/Ljubljana")
    state = {"outbox": [], "last_success": "2026-01-01T18:00:00+01:00"}
    before = datetime.fromisoformat(date + "T17:59:00").replace(tzinfo=local)
    after = datetime.fromisoformat(date + "T18:00:00").replace(tzinfo=local)
    assert not bot.due(config, state, before)
    assert bot.due(config, state, after)
    state["last_success"] = after.isoformat()
    assert not bot.due(config, state, after.replace(hour=23))
    state["outbox"] = [{"content": "pending"}]
    assert bot.due(config, state, after)


def response(url, html, status=200, headers=None):
    result = requests.Response()
    result.url = url
    result.status_code = status
    result._content = html.encode()
    result._content_consumed = True
    result.encoding = "utf-8"
    result.headers.update({"Content-Type": "text/html", **(headers or {})})
    return result


def test_school_login_submits_current_nonce_and_checks_authenticated_page(monkeypatch):
    school = bot.School(bot.Config(email="student@example.invalid", password="test-only"))
    responses = iter(
        [
            response("https://www.gimb.org/prijava/", LOGIN),
            response(URL, page()),
            response(URL, page()),
        ]
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return next(responses)

    monkeypatch.setattr(school, "request", request)
    assert "Obvestila" in school.fetch(URL)
    assert calls[1][0] == "POST"
    assert calls[1][2]["data"]["gimbezigrad_login_nonce"] == "fixture-nonce"
    assert calls[1][2]["data"]["gimbezigrad_login_submit"] == "1"


def test_failed_login_never_returns_login_html(monkeypatch):
    school = bot.School(bot.Config(email="student@example.invalid", password="test-only"))
    monkeypatch.setattr(school, "request", lambda *a, **kw: response(URL, LOGIN))
    with pytest.raises(bot.MonitorError, match="login failed"):
        school.fetch(URL)


def test_cross_origin_redirect_never_forwards_credentials(monkeypatch):
    school = bot.School(bot.Config())
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        return response(URL, "", 307, {"Location": "https://example.invalid/steal"})

    monkeypatch.setattr(school.session, "request", request)
    with pytest.raises(bot.MonitorError):
        school.request("POST", URL, data={"pwd": "test-only"})
    assert len(calls) == 1


def test_discord_waits_for_delivery_and_disables_mentions(monkeypatch):
    sender = bot.Discord(bot.Config(webhook="https://discord.com/api/webhooks/123/fixture"))
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response(url, '{"id":"1"}')

    monkeypatch.setattr(bot.requests, "post", post)
    sender.send("@everyone")
    assert calls[0][0].endswith("?wait=true")
    assert calls[0][1]["json"]["allowed_mentions"] == {"parse": []}


def test_discord_failure_does_not_expose_secret(monkeypatch):
    sender = bot.Discord(bot.Config(webhook="https://discord.com/api/webhooks/123/secret-value"))

    def fail(*args, **kwargs):
        raise requests.ConnectionError("https://discord.com/api/webhooks/123/secret-value")

    monkeypatch.setattr(bot.requests, "post", fail)
    with pytest.raises(bot.MonitorError) as caught:
        sender.send("test")
    assert "secret-value" not in str(caught.value)


def test_discord_rate_limit_retry(monkeypatch):
    sender = bot.Discord(bot.Config(token="test-only", channel="123"))
    replies = iter([response("", '{"retry_after":0}', 429), response("", '{"id":"1"}')])
    calls = []
    monkeypatch.setattr(bot.requests, "post", lambda *a, **kw: next(replies))
    monkeypatch.setattr(bot.time, "sleep", calls.append)
    sender.send("hello", "nonce")
    assert calls == [0.1]


def test_long_alert_stays_within_discord_limit():
    old = {"lines": ["Old school notice"], "title": "Intranet"}
    new = {"lines": ["*" * 500 for _ in range(20)], "title": "x" * 500}
    assert len(bot.change_message(old, new, URL, "today")) <= 2000


def test_corrupt_state_does_not_reset_silently(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("broken")
    with pytest.raises(bot.MonitorError, match="invalid"):
        bot.load_state(path)


def test_repository_has_no_configured_secrets():
    # Only a distributable example should ship with the source.
    example = bot.dotenv_values(Path(bot.ROOT) / ".env.example")
    assert not example["GIMB_PASSWORD"]
    assert not example["DISCORD_BOT_TOKEN"]


@pytest.mark.parametrize(
    "link",
    [
        "#fragment",
        "mailto:teacher@gimb.org",
        "javascript:alert(1)",
        "https://external.example/notice/",
        "/spremeni-geslo/",
        "/odjava/",
        "/wp-admin/",
        "/wp-login.php?action=logout",
        "/notice/?action=delete",
        "/notice/?_wpnonce=secret",
        "/%73premeni-geslo/",
        "/wp-content/image.png",
        "/",
        "/obvestila/",
        "https://www.gimb.org.evil.example/notice",
        "https://user:pass@www.gimb.org/notice/",
    ],
)
def test_discovery_excludes_navigation_actions_and_unrelated_sites(link):
    assert bot.linked_url(link, URL) is None


def test_link_normalization_and_document_classification():
    assert bot.linked_url("/obvestila/example/?utm_source=test#details", URL) == (
        "https://www.gimb.org/obvestila/example/",
        "page",
    )
    assert bot.linked_url("/wp-content/uploads/menu.pdf", URL)[1] == "file"
    assert bot.linked_url("/wp-content/uploads/form.docx", URL)[1] == "file"


def test_elementor_content_excludes_related_notices_account_and_comment_noise():
    html = """<html><head><title>Actual notice – Gimnazija Bežigrad</title></head><body>
    <h1>Obvestila</h1><div class="gimb-account-navigation"><a href="/spremeni-geslo/">Password</a></div>
    <div class="elementor-widget-theme-post-content"><p>This is the useful announcement body.</p>
    <a href="/details/">Full details</a><!-- changing counter --> </div>
    <div><h2>Sorodno</h2><a href="/unrelated/">Random related notice</a></div></body></html>"""
    result = bot.extract(html, URL, bot.Config())
    assert result["title"] == "Actual notice"
    assert [link["url"] for link in result["links"]] == ["https://www.gimb.org/details/"]
    assert not any("Random" in line or "counter" in line for line in result["lines"])


class MappedSchool(FakeSchool):
    def __init__(self, routes):
        self.routes = routes
        self.fetched = []

    def fetch(self, url):
        self.fetched.append(url)
        result = self.routes[url]
        if isinstance(result, Exception):
            raise result
        return result


def test_scan_follows_two_levels_deduplicates_and_does_not_follow_third_level():
    first = "https://www.gimb.org/first/"
    second = "https://www.gimb.org/second/"
    site = MappedSchool(
        {
            URL: page(href=first) + f'<a href="{first}#anchor">duplicate</a>',
            first: page(href=second),
            second: page(href="/third/"),
        }
    )
    pages, report = bot.scan(bot.Config(track_files=False, request_delay=0), site)
    assert site.fetched == [URL, first, second]
    assert len(pages) == 3
    assert not report["errors"]


def test_page_limit_is_explicit_and_cannot_expand_indefinitely():
    first = "https://www.gimb.org/first/"
    site = MappedSchool({URL: page(href=first), first: page(href="/second/")})
    pages, report = bot.scan(bot.Config(max_pages=2, request_delay=0), site)
    assert len(pages) == 2
    assert report["page_limit_reached"]


def test_linked_edit_alert_and_unavailable_child_preserves_its_snapshot(tmp_path):
    first = "https://www.gimb.org/first/"
    second = "https://www.gimb.org/second/"
    site = MappedSchool({URL: page(href=first), first: page(href=second), second: page()})
    config = bot.Config(track_files=False, request_delay=0)
    sender = FakeDiscord()
    state = tmp_path / "state.json"
    bot.check(config, path=state, school=site, sender=sender)
    old_second = bot.load_state(state)["pages"][second]
    site.routes[first] = page("Deadline changed to next Friday at noon.", href=second)
    site.routes[second] = bot.MonitorError("HTTP 404")
    bot.check(config, path=state, school=site, sender=sender)
    assert len(sender.messages) == 1
    assert "Deadline changed" in sender.messages[0]
    assert bot.load_state(state)["pages"][second] == old_second
    assert second in bot.load_state(state)["last_scan"]["errors"]


def test_same_url_document_replacement_produces_alert(monkeypatch):
    school = bot.School(bot.Config())
    url = "https://www.gimb.org/wp-content/uploads/menu.pdf"

    def pdf_response(data):
        r = response(url, "", headers={"Content-Type": "application/pdf"})
        r._content = data
        r._content_consumed = True
        return r

    monkeypatch.setattr(school, "request", lambda *a, **kw: pdf_response(b"%PDF-fixture-1"))
    old = school.file_snapshot(url, "Menu")
    assert bot.change_message(old, school.file_snapshot(url, "Menu"), url, "today") is None
    monkeypatch.setattr(school, "request", lambda *a, **kw: pdf_response(b"%PDF-fixture-2"))
    message = bot.change_message(old, school.file_snapshot(url, "Menu"), url, "today")
    assert "posodobljen dokument" in message
    assert url in message


def test_document_login_response_does_not_replace_file_fingerprint(monkeypatch):
    school = bot.School(bot.Config())
    r = response(URL, LOGIN)
    r._content_consumed = True
    monkeypatch.setattr(school, "request", lambda *a, **kw: r)
    with pytest.raises(bot.MonitorError, match="HTML/login"):
        school.file_snapshot(URL, "Document")


SUBSTITUTIONS = """<table><thead><tr><th>Razred</th><th>Ura</th><th>Nadomešča</th>
<th>Učilnica</th><th>Opombe</th></tr></thead><tbody>
<tr><td rowspan="2">2. C</td><td>1.</td><td>odpade</td><td>/</td><td>/</td></tr>
<tr><td>3.</td><td>Teacher, MAT</td><td>208</td><td>Bring notes</td></tr>
<tr><td>2. B</td><td>5.</td><td>Other teacher</td><td>101</td><td>/</td></tr>
</tbody></table>"""


def test_substitutions_filter_class_and_handle_rowspans():
    result = bot.parse_substitutions(SUBSTITUTIONS, "2C", "2026-10-01")
    assert len(result["lines"]) == 2
    assert "odpade" in result["lines"][0]
    assert "učilnica 208" in result["lines"][1]
    assert not any("Other teacher" in line for line in result["lines"])
    assert result["notify_new"]
    assert not bot.parse_substitutions(SUBSTITUTIONS, "4C", "2026-10-01")["lines"]


def test_substitutions_distinguish_empty_day_from_broken_page():
    assert bot.parse_substitutions("Za ta dan ni nadomeščanj.", "2C", "2026-10-05")["lines"] == []
    with pytest.raises(bot.MonitorError):
        bot.parse_substitutions("<h1>Service unavailable</h1>", "2C", "2026-10-05")


def test_substitutions_fetch_five_school_days_and_do_not_send_school_credentials(monkeypatch):
    config = bot.Config(email="private", password="private", request_delay=0)
    monkeypatch.setattr(
        bot.Config,
        "now",
        lambda self: datetime(2026, 10, 2, 18, tzinfo=ZoneInfo("Europe/Ljubljana")),
    )
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response(url, '{"data":"Za ta dan ni nadomeščanj."}')

    monkeypatch.setattr(bot.requests, "get", get)
    base = "https://urniki.easistent.com/nadomescanja/" + "a" * 40
    pages = bot.fetch_substitutions(base, config)
    assert [p["date"] for p in pages.values()] == [
        "2026-10-02",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
        "2026-10-08",
    ]
    assert all(set(kwargs) == {"timeout", "allow_redirects"} for _, kwargs in calls)


def test_new_upcoming_day_with_substitutions_alerts_after_initial_baseline(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    config = bot.Config(request_delay=0)
    school = FakeSchool()
    sender = FakeDiscord()
    bot.check(config, path=state, school=school, sender=sender)
    new_day = bot.parse_substitutions(SUBSTITUTIONS, "2C", "2026-10-06")
    monkeypatch.setattr(
        bot,
        "scan",
        lambda *a: (
            {"https://example.invalid/day": new_day},
            {"errors": {}, "counts": {"substitutions": 1}},
        ),
    )
    bot.check(config, path=state, school=school, sender=sender)
    assert len(sender.messages) == 1
    assert "2026-10-06" in sender.messages[0]


def test_deeper_general_forms_are_not_downloaded():
    direct = "https://www.gimb.org/documents/"
    general = "https://www.gimb.org/general-forms/"
    site = MappedSchool(
        {URL: page(href=direct), direct: page(href=general), general: page(href="/agreement.pdf")}
    )
    site.file_snapshot = lambda *args: pytest.fail("Must not download a deeper agreement")
    pages, report = bot.scan(bot.Config(request_delay=0), site)
    assert len(pages) == 3
    assert report["counts"]["file"] == 0
    assert pages[general]["links"][0]["url"].endswith("agreement.pdf")
