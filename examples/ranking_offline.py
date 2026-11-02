"""Run and replay an offline example in a temporary database.

Pass the retained source/release identity and its uv.lock SHA-256 explicitly.
No network requests are made; the temporary database is removed after reporting.
"""

import argparse
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from applicant.domain.canonical import canonical_json
from applicant.domain.job import Job
from applicant.domain.ranking import Evidence, JobVersion
from applicant.domain.ranking_facts import FactSource, RankingFacts, WorkRoute
from applicant.domain.ranking_v2 import CandidateProfileV2, ScoringConfigurationV2
from applicant.infra.store.sqlite import Store
from applicant.services.preferences import save_profile
from applicant.services.ranking import rank_jobs
from applicant.services.ranking_replay import replay_run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-identity', required=True)
    parser.add_argument('--lockfile-digest', required=True)
    args = parser.parse_args()
    observed = datetime.now(timezone.utc)
    source = FactSource(
        evidence_id='employer-remote-policy',
        source='example-employer',
        observed_at=observed,
        extractor_version='example-1',
        quality='explicit',
        excerpt='This job can be performed remotely worldwide.',
    )
    facts = RankingFacts(
        routes=(
            WorkRoute(route_id='remote', source=source, mode='Remote', remote_scope='worldwide'),
        ),
        routes_complete=True,
    )
    job = JobVersion.capture(
        Job(
            source='example',
            id='one',
            title='Developer',
            salary='EUR 80000-100000 annual gross base',
        ),
        parser_version='example-1',
        evidence=(
            Evidence(
                source='example-employer',
                observed_at=observed,
                extractor_version='example-1',
                payload=facts.model_dump(mode='json'),
            ),
        ),
    )
    with (
        tempfile.TemporaryDirectory() as directory,
        Store(Path(directory) / 'applicant.db') as store,
    ):
        profile = save_profile(
            store,
            CandidateProfileV2(
                profile_id='example',
                experience_years=Decimal(5),
                primary_discipline='software',
                preferred_base_salary=Decimal(120000),
                minimum_base_salary=Decimal(75000),
                preferred_work_modes=('Remote',),
                employment_preference='either',
            ),
            expected_revision=0,
        )
        run = rank_jobs(
            store,
            profile_id=profile.profile_id,
            profile_revision=profile.profile_revision,
            jobs=(job,),
            config=ScoringConfigurationV2(),
            evaluated_at=datetime.now(timezone.utc),
            build_identity=args.build_identity,
            lockfile_digest=args.lockfile_digest,
            offline=True,
        )
        replay = replay_run(
            store,
            run.ranking_run_id,
            build_identity=args.build_identity,
            lockfile_digest=args.lockfile_digest,
        )
        print(
            canonical_json(
                {
                    'eligibility': run.results[0].eligibility,
                    'score': run.results[0].overall_score,
                    'confidence': run.results[0].confidence,
                    'coverage': run.results[0].coverage,
                    'replay_matches': replay.matches,
                }
            )
        )


if __name__ == '__main__':
    main()
