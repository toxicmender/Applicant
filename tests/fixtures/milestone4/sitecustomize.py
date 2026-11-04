"""Subprocess fixture: fixed application clock and forbidden Python networking."""

import socket
from datetime import datetime, timezone

from applicant.domain import contracts


def frozen_now():
    return datetime(2026, 11, 4, tzinfo=timezone.utc)


def denied(*args, **kwargs):
    raise RuntimeError('network forbidden in Milestone 4 CLI journey')


contracts.now = frozen_now
socket.create_connection = denied
socket.getaddrinfo = denied
socket.socket.connect = denied
socket.socket.connect_ex = denied
