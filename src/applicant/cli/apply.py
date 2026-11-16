"""`applicant apply`: the stored jobs that match, applied to or listed."""

from __future__ import annotations

import argparse
import sys

from ..domain.job import Job
from ..errors import ApplicationBlocked, AttestationRequired, ConfigError
from ..log import get
from ..settings import FilterOptions, Settings
from .common import add_filters, configured_path, filters_from
from .dispatch import AddParser, Request
from .render import shown, terminal

logger = get(__name__)


class Apply(FilterOptions, Request):
    input: str | None = None
    log: str | None = None
    cookies: str | None = None
    dry_run: bool = False
    show: bool = False
    yes: bool = False
    reconcile: str | None = None
    outcome: str | None = None
    retry_attempt: str | None = None
    reopen_attempt: str | None = None
    rotate_attestation_key: bool = False
    acknowledge_duplicate_risk: bool = False


def add_parser(add: AddParser) -> argparse.ArgumentParser:
    apply_ = add('apply', help='apply to stored jobs and log them for a spreadsheet')
    apply_.add_argument(
        '-i',
        '--input',
        help='file path to the scraped jobs (default: [files] listing, job_listing.json)',
    )
    apply_.add_argument(
        '--log',
        help='CSV to append applications to (default: [files] applications, applied_jobs.csv)',
    )
    apply_.add_argument(
        '-l', '--location', default=None, help='only apply to jobs in this location'
    )
    apply_.add_argument(
        '--dry-run', action='store_true', help='show what would be applied to, without applying'
    )
    apply_.add_argument('--show', action='store_true', help='run the browser visibly')
    apply_.add_argument(
        '--cookies',
        help='the signed-in session `applicant jobs` saved (default: [files] cookies, '
        'cookies.json)',
    )
    add_filters(apply_)
    mode = apply_.add_mutually_exclusive_group()
    mode.add_argument('--reconcile', metavar='ATTEMPT_ID')
    mode.add_argument('--retry-attempt', metavar='ATTEMPT_ID')
    mode.add_argument('--reopen-attempt', metavar='ATTEMPT_ID')
    mode.add_argument('--rotate-attestation-key', action='store_true')
    apply_.add_argument('--outcome', choices=('submitted', 'not-submitted', 'abandoned'))
    apply_.add_argument('--acknowledge-duplicate-risk', action='store_true')
    apply_.add_argument(
        '-y',
        '--yes',
        action='store_true',
        help='submit without asking first. Without it, the applications about to '
        'be sent are listed and you are asked to confirm; with no terminal to ask '
        'on, nothing is submitted',
    )
    return apply_


def run(args: Apply, settings: Settings) -> int:
    from ..search import Jobs
    from ..services.application_attempts import HISTORY, AttemptJournal
    from ..services.apply import worklist

    source, log = (
        configured_path(settings, args.input, settings.files.listing),
        configured_path(settings, args.log, settings.files.applications),
    )
    session = configured_path(settings, args.cookies, settings.files.cookies)
    if bool(args.reconcile) != bool(args.outcome):
        raise ConfigError('--reconcile and --outcome must be used together')
    if bool(args.retry_attempt) != args.acknowledge_duplicate_risk:
        raise ConfigError('--retry-attempt requires --acknowledge-duplicate-risk')
    if args.reconcile and (args.dry_run or args.retry_attempt):
        raise ConfigError('--reconcile cannot be combined with --dry-run or --retry-attempt')
    if args.dry_run and (args.reopen_attempt or args.rotate_attestation_key):
        raise ConfigError('journal maintenance cannot be combined with --dry-run')
    needs_attestation = args.reopen_attempt or args.outcome in ('not-submitted', 'abandoned')
    if needs_attestation and not sys.stdin.isatty():
        raise AttestationRequired(
            'use a terminal to attest to the original LinkedIn portal history'
        )
    journal = AttemptJournal(log, session, args.retry_attempt, backend=settings.store)
    if args.rotate_attestation_key:
        journal.rotate()
        print('Attestation key rotated. Attest again before retrying an application.')
        return 0
    if identifier := args.reconcile or args.reopen_attempt:
        entry = journal.detail(identifier)
        print(f'{shown(entry["job"]["url"])} · account {shown(entry["account"])}')
        print(f'Check Jobs → Job tracker → Applied using the original account: {HISTORY}')
        attested = False
        if needs_attestation:
            attested = (
                terminal()
                .ask(
                    'If the history is conclusive, type verified not submitted. '
                    'Retrying may create a duplicate: '
                )
                .strip()
                == 'verified not submitted'
            )
        with journal.profile_owner():
            if args.reopen_attempt and journal.detail(identifier)['state'] != 'abandoned':
                raise ConfigError('only an abandoned attempt can be reopened')
            journal.resolve(identifier, args.outcome or 'not-submitted', attested=attested)
        if args.outcome == 'submitted':
            journal.recover_confirmed()
        return 0
    if not args.dry_run and journal.exists():
        journal.recover_armed()
        journal.recover_confirmed()
    if args.retry_attempt:
        from ..services.apply import Worklist, select

        entry = journal.detail(args.retry_attempt)
        if entry['state'] != 'not-submitted':
            raise ConfigError('attempt must be reconciled as not-submitted before retrying')
        work = Worklist(1, select([Job.model_validate(entry['job'])], filters_from(args)))
    else:
        work = worklist(source, filters_from(args), settings.store)
    if not work.read:
        print('no jobs to apply to in {}'.format(source))
        return 1

    print('{} of {} stored jobs match'.format(len(work.matching), work.read))
    if not work.matching:
        return 1

    logger.info(
        f'apply: {len(work.matching)} job(s) to {log}' + (' (dry run)' if args.dry_run else '')
    )
    declined: list[Job] = []

    def confirm(targets: list[Job]) -> bool:
        if args.yes:
            return True
        print('about to submit {} application(s) in your name:'.format(len(targets)))
        for job in targets:
            # scraped text, escaped: nothing in a posting may rewrite this list
            print(
                '  {} - {} ({})'.format(shown(job.title), shown(job.company or '?'), shown(job.url))
            )
        if not sys.stdin.isatty():
            print('no terminal to confirm on: nothing submitted. Pass --yes to submit anyway.')
            declined.extend(targets)
            return False
        answered = terminal().ask('type yes to submit them: ').strip().lower() == 'yes'
        if not answered:
            declined.extend(targets)
        return answered

    with Jobs(headless=not args.show, backend=settings.store, session=session) as board:
        board.attempts = journal
        results = board.apply(work.matching, log=log, dry_run=args.dry_run, confirm=confirm)
    if declined:
        print('not submitted: {} application(s); nothing was logged for them'.format(len(declined)))
    if isinstance(results, list) and any(
        note.startswith('application_uncertain:')
        for _, status, note in results
        if status == 'failed'
    ):
        raise ApplicationBlocked('inspect the application log and reconcile the blocked attempt')
    return 0
