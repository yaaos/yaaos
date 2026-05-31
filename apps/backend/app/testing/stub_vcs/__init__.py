"""plugins/stub_vcs — in-process VCS stub for service tests.

The VCS registry expects a `VCSPlugin` for each plugin_id referenced by a
`pull_requests` row. The real `github` plugin hits `GITHUB_API_BASE_URL` (only
resolves inside the docker test stack), so in-process service tests register
this stub under whichever plugin_id their seeded PR uses — typically `"github"`
to avoid touching seed strings.
"""

from app.testing.stub_vcs.service import StubVCSPlugin, register_stub_vcs

__all__ = ["StubVCSPlugin", "register_stub_vcs"]
