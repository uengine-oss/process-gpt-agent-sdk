"""에이전트 산출물을 사용자에게 내주는 공용 규약.

에이전트는 이번 턴의 `outputs/` 에 최종본만 놓는다. 그 다음은 모두 서버의 일이다 —
거두고(`ArtifactCollector`), 비공개 버킷에 보관하고(`ArtifactStore`), 본문에 남은 내부
경로를 주소로 바꾼다(`strip_local_paths`). 새 에이전트를 붙일 때 다시 만들 것이 없다.
"""

from .collect import (
    ArtifactCollector,
    DEFAULT_EXTENSIONS,
    DEFAULT_MAX_BYTES,
    sha256_file,
    snapshot,
)
from .links import file_name_of, is_local_path, names_to_urls, strip_local_paths
from .records import (
    CONTENT_TYPES,
    NATIVE_PREVIEW_KINDS,
    build_artifact,
    download_file,
    preview_file,
)
from .store import ArtifactStore, MementoArtifactStore, StoredArtifact

__all__ = [
    "ArtifactCollector",
    "ArtifactStore",
    "CONTENT_TYPES",
    "DEFAULT_EXTENSIONS",
    "DEFAULT_MAX_BYTES",
    "MementoArtifactStore",
    "NATIVE_PREVIEW_KINDS",
    "StoredArtifact",
    "build_artifact",
    "download_file",
    "file_name_of",
    "is_local_path",
    "names_to_urls",
    "preview_file",
    "sha256_file",
    "snapshot",
    "strip_local_paths",
]
