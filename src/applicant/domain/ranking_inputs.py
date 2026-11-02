"""Versioned CLI input envelopes; archived engine contracts remain independent."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import Identity
from .ranking import JobVersion


class ProvenanceDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    kind: Literal['ranking_provenance']
    schema_version: Literal[1]
    build_identity: Identity
    lockfile_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class JobVersionBundle(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    kind: Literal['ranking_job_versions']
    schema_version: Literal[1]
    jobs: tuple[JobVersion, ...]
