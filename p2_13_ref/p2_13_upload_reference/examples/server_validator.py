"""Inject the project's real chunk parser into the reusable server."""

from pathlib import Path
from typing import Mapping, Any

from hp_upload_ref.server import IngestServer, ServerConfig


def validate_project_chunk(path: Path, metadata: Mapping[str, Any]) -> None:
    # Replace with the P2 chunk decoder / validator.
    #
    # Required checks should include:
    # - magic and schema version;
    # - internal index bounds;
    # - advertised frame_count;
    # - first / last frame IDs;
    # - RGB and depth record lengths;
    # - codec and calibration identifiers;
    # - no trailing or overlapping records.
    if path.stat().st_size != metadata["payload_bytes"]:
        raise ValueError("container size mismatch")


server = IngestServer(
    ServerConfig(
        root=Path("/srv/hp-upload"),
        host="127.0.0.1",
        port=4170,
        token=None,  # Load from a secret file in the real service.
    ),
    chunk_validator=validate_project_chunk,
)
