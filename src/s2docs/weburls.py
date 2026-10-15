"""Public web URLs for the UI pages (jobs / test runs / projects / builds / deploys / screenshots).

The web UI is a hash-route SPA, so a *page* URL is ``<base>/#/<route>`` (e.g. ``#/jobs/j-1a2b3c4d``,
``#/cs2/projects/p-1a2b3c4d``, ``#/activity``). The base URL is resolved once, in :func:`public_base_url`:

* ``S2DOCS_PUBLIC_URL`` (the ``Settings.public_url``) wins outright when set;
* otherwise, for a REST call, it is derived from the incoming request (``scheme://host:port``) — so a
  browser hitting ``http://10.88.15.208:8812`` gets URLs back on that same origin;
* otherwise (MCP tools, the CLI, reporters with no request) it falls back to
  :data:`s2docs.config.DEFAULT_PUBLIC_URL` — the LAN address the user views the UI from.

Routes are only defined for entities that have a page. Test runs / builds / deploys have no dedicated
page (they appear on ``#/activity``), so their ``web_url`` points at that page.
"""

from __future__ import annotations

from s2docs.config import DEFAULT_PUBLIC_URL, get_settings


def public_base_url(request=None) -> str:
    """The base URL for the web UI, with no trailing slash (see the module docstring for the order)."""
    setting = get_settings().public_url
    if setting:
        return setting.rstrip("/")
    if request is not None:
        try:
            host = request.headers.get("host")
            if host:
                return f"{request.url.scheme}://{host}".rstrip("/")
        except Exception:  # a request-like object without headers/url: fall back
            pass
    return DEFAULT_PUBLIC_URL


def _page(base: str, route: str) -> str:
    return f"{base.rstrip('/')}/#{route}"


def job_url(base: str, job_id: str) -> str:
    """``<base>/#/jobs/<id>`` — the job detail page."""
    return _page(base, f"/jobs/{job_id}")


def test_run_url(base: str, run_id: str) -> str:  # noqa: ARG001 — route carries no per-run id
    """Test runs have no dedicated page; they are shown on the Activity page."""
    return _page(base, "/activity")


def project_url(base: str, game: str, project_id: str) -> str:
    """``<base>/#/<game>/projects/<id>`` — the Orchestration (project) page."""
    return _page(base, f"/{game}/projects/{project_id}")


def build_url(base: str, run_id: str) -> str:  # noqa: ARG001
    """Build runs are shown on the Activity page (no dedicated route)."""
    return _page(base, "/activity")


def deploy_url(base: str, deploy_id: str) -> str:  # noqa: ARG001
    """Deployments are shown on the Activity page (no dedicated route)."""
    return _page(base, "/activity")


def screenshot_url(base: str, job_id: str) -> str:
    """A screenshot's ``web_url`` is its own job page (where it is grouped under its step)."""
    return job_url(base, job_id)


def artifact_url(base: str, job_id: str, name: str) -> str:
    """The absolute URL the bytes are served from (``/api/artifacts/<job>/<name>``)."""
    return f"{base.rstrip('/')}/api/artifacts/{job_id}/{name}"
