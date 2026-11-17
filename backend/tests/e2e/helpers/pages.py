"""Signed-in browser pages for the e2e suite, with failure evidence.

pytest-playwright's --screenshot/--tracing only cover ITS `page` fixture. The
authenticated fixtures in ``tests/e2e/conftest.py`` build their own context
(they have to: Descope's OIDC session is injected via add_init_script before
first paint, Keycloak and Zitadel sign in through their own forms), so those
pages are invisible to it and a UI failure reports only which locator it
waited for, never what the page actually showed. Capture it here instead.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest


def record_console(page) -> list[str]:
    """Collect console output and uncaught errors for the life of the page.

    An SPA that throws during mount leaves `<div id="root"></div>` and nothing
    else, so the DOM says only that nothing rendered. The reason is in the
    console, which is otherwise discarded when the context closes.
    """
    messages: list[str] = []
    page.on("console", lambda m: messages.append(f"{m.type}: {m.text}"))
    page.on("pageerror", lambda e: messages.append(f"pageerror: {e}"))
    return messages


def capture(page, request, label: str, console: list[str], artifact_dir: str) -> None:
    """Save a screenshot, the rendered HTML, the URL and the console under ``artifact_dir``."""
    if not artifact_dir:
        return
    out = Path(artifact_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"{request.node.name}-{label}".replace("/", "_").replace("::", "-")
    try:
        page.screenshot(path=str(out / f"{stem}.png"), full_page=True)
        (out / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        (out / f"{stem}.url.txt").write_text(page.url, encoding="utf-8")
        if console:
            (out / f"{stem}.console.txt").write_text("\n".join(console), encoding="utf-8")
    except Exception as exc:  # pragma: no cover - diagnostics must never fail a test
        print(f"[e2e] could not capture failure evidence for {stem}: {exc}")


def _test_failed(request) -> bool:
    """Whether the test body failed (``pytest_runtest_makereport`` records the call report on the item)."""
    report = getattr(request.node, "_report_call", None)
    return report is not None and report.failed


def signed_in_page(browser, e2e_provider, principal, request, label: str, artifact_dir: str) -> Iterator:
    """Yield a page signed in to the SPA as ``principal``; the context always closes.

    A set-up failure (the provider's sign-in or the page never settling) has no
    call report, so it is captured here before it is raised. A skip is not a
    failure and leaves no evidence.
    """
    context = e2e_provider.signed_in_context(browser, principal, request)
    try:
        page = context.new_page()
        console = record_console(page)
        try:
            e2e_provider.open_app(page, principal)
            page.wait_for_load_state("networkidle")
        except pytest.skip.Exception:
            raise
        except BaseException:
            capture(page, request, label, console, artifact_dir)
            raise
        yield page
        if _test_failed(request):
            capture(page, request, label, console, artifact_dir)
    finally:
        context.close()
