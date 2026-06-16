# Dead test — `ActivityEvent` was removed from `core/coding_agent` when the
# in-process streaming surface was retired. The activity publisher now receives
# any Pydantic model with a `model_dump()` method; the live progress streaming
# path is covered by the e2e suite.
