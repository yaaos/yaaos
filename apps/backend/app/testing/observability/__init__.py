"""In-process OTel span capture for tests.

Usage (inside a `@pytest.mark.service` test)::

    from app.testing.observability import span_capture

    async def test_something() -> None:
        with span_capture() as exporter:
            # drive the code under test
            ...
        spans = exporter.get_finished_spans()
        err_spans = [s for s in spans if s.name == "agent_command.dispatch.MyKind"]
        assert err_spans[0].status.status_code == StatusCode.ERROR
"""

from app.testing.observability.span_capture import span_capture

__all__ = ["span_capture"]
