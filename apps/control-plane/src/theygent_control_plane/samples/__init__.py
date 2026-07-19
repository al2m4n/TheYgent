"""Sample-agent catalog: shipped example agents installable via ``GET/POST /samples``."""

from .catalog import (
    SampleConnection,
    SampleModelsError,
    SampleModelSlot,
    SampleRagSource,
    SampleSpec,
    get_sample,
    load_catalog,
    render_connection_config,
    render_ir,
    seed_demo_file,
    seed_document,
)

__all__ = [
    "SampleConnection",
    "SampleModelSlot",
    "SampleModelsError",
    "SampleRagSource",
    "SampleSpec",
    "get_sample",
    "load_catalog",
    "render_connection_config",
    "render_ir",
    "seed_demo_file",
    "seed_document",
]
