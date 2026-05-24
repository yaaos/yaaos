"""SSE endpoint for the events module."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.auth import public_route
from app.core.events.service import EventFilter, stream_events_for_filter
from app.core.webserver import RouteSpec, register_routes

# Default-deny: SSE endpoint declares `public_route`.
router = APIRouter(dependencies=[Depends(public_route)])


@router.get("")
async def stream(
    ticket_id: UUID | None = None,
    kinds: list[str] | None = Query(None),
) -> StreamingResponse:
    filter_ = EventFilter(ticket_id=ticket_id, kinds=kinds)
    return StreamingResponse(stream_events_for_filter(filter_), media_type="text/event-stream")


register_routes(RouteSpec(module_name="events", router=router))
