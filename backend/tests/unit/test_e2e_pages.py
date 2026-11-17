"""A signed-in page fixture closes its context and keeps evidence however set-up ends (``helpers/pages.py``)."""

from types import SimpleNamespace

import pytest

from tests.e2e.helpers import pages


class FakePage:
    url = "http://spa.test/login"

    def __init__(self, fails_loading: bool = False) -> None:
        self._fails_loading = fails_loading

    def on(self, event, handler) -> None:
        pass

    def wait_for_load_state(self, state) -> None:
        if self._fails_loading:
            raise TimeoutError("networkidle")

    def screenshot(self, path, full_page) -> None:
        with open(path, "wb") as file:
            file.write(b"png")

    def content(self) -> str:
        return "<div id='root'></div>"


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = 0

    def new_page(self) -> FakePage:
        return self.page

    def close(self) -> None:
        self.closed += 1


class FakeProvider:
    def __init__(self, context: FakeContext, opening=None) -> None:
        self._context = context
        self._opening = opening

    def signed_in_context(self, browser, principal, request) -> FakeContext:
        return self._context

    def open_app(self, page, principal) -> None:
        if self._opening is not None:
            raise self._opening


def _request(call_failed: bool | None = None):
    node = SimpleNamespace(name="test_x")
    if call_failed is not None:
        node._report_call = SimpleNamespace(failed=call_failed)
    return SimpleNamespace(node=node)


def _run(provider, request, artifacts):
    fixture = pages.signed_in_page(None, provider, "member", request, "auth_page", str(artifacts))
    page = next(fixture)
    with pytest.raises(StopIteration):
        next(fixture)
    return page


def _evidence(artifacts) -> list[str]:
    return sorted(path.name for path in artifacts.iterdir()) if artifacts.exists() else []


class TestSetUpFails:
    @pytest.mark.parametrize(
        ("opening", "fails_loading", "raised"),
        [
            (pytest.fail.Exception("sign-in refused"), False, pytest.fail.Exception),
            (RuntimeError("locator timed out"), False, RuntimeError),
            (None, True, TimeoutError),
        ],
    )
    def test_the_context_closes_and_the_page_is_captured(self, tmp_path, opening, fails_loading, raised):
        context = FakeContext(FakePage(fails_loading=fails_loading))
        artifacts = tmp_path / "artifacts"

        with pytest.raises(raised):
            next(
                pages.signed_in_page(
                    None, FakeProvider(context, opening), "member", _request(), "auth_page", str(artifacts)
                )
            )

        assert context.closed == 1
        assert _evidence(artifacts) == [
            "test_x-auth_page.html",
            "test_x-auth_page.png",
            "test_x-auth_page.url.txt",
        ]

    def test_a_skip_closes_the_context_without_evidence(self, tmp_path):
        context = FakeContext(FakePage())
        artifacts = tmp_path / "artifacts"
        provider = FakeProvider(context, pytest.skip.Exception("not here"))

        with pytest.raises(pytest.skip.Exception):
            next(pages.signed_in_page(None, provider, "member", _request(), "auth_page", str(artifacts)))

        assert context.closed == 1
        assert _evidence(artifacts) == []


class TestAfterTheTest:
    def test_a_failed_test_is_captured_and_the_context_closes(self, tmp_path):
        context = FakeContext(FakePage())
        artifacts = tmp_path / "artifacts"

        page = _run(FakeProvider(context), _request(call_failed=True), artifacts)

        assert page is context.page
        assert context.closed == 1
        assert _evidence(artifacts) == [
            "test_x-auth_page.html",
            "test_x-auth_page.png",
            "test_x-auth_page.url.txt",
        ]

    def test_a_passed_test_leaves_no_evidence(self, tmp_path):
        context = FakeContext(FakePage())
        artifacts = tmp_path / "artifacts"

        _run(FakeProvider(context), _request(call_failed=False), artifacts)

        assert context.closed == 1
        assert _evidence(artifacts) == []

    def test_without_an_artifact_directory_nothing_is_written(self, tmp_path):
        context = FakeContext(FakePage())

        fixture = pages.signed_in_page(
            None, FakeProvider(context), "member", _request(call_failed=True), "auth_page", ""
        )
        next(fixture)
        with pytest.raises(StopIteration):
            next(fixture)

        assert context.closed == 1
        assert list(tmp_path.iterdir()) == []
