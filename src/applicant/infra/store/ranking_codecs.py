"""Explicit schema dispatch; unknown versions never fall back to current defaults."""

from typing import Annotated

from pydantic import Field, TypeAdapter

from ...domain.fx import FXSnapshot
from ...domain.preferences import CandidateProfile
from ...domain.ranking import JobRanking, RankingRun, ScoringConfiguration
from ...domain.ranking_v2 import (
    CandidateProfileV2,
    FXSnapshotV2,
    JobRankingV2,
    RankingRunV2,
    RankingRunV3,
    ScoringConfigurationV2,
)

Profile = CandidateProfile | CandidateProfileV2
FX = FXSnapshot | FXSnapshotV2
Config = ScoringConfiguration | ScoringConfigurationV2
Result = JobRanking | JobRankingV2
Run = RankingRun | RankingRunV2 | RankingRunV3

PROFILE_CODEC = TypeAdapter(Annotated[Profile, Field(discriminator='schema_version')])
FX_CODEC = TypeAdapter(Annotated[FX, Field(discriminator='schema_version')])
CONFIG_CODEC = TypeAdapter(Annotated[Config, Field(discriminator='schema_version')])
RESULT_CODEC = TypeAdapter(Annotated[Result, Field(discriminator='schema_version')])
RUN_CODEC = TypeAdapter(Annotated[Run, Field(discriminator='schema_version')])
