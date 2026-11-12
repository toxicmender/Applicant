"""Provider profile ownership through browser teardown."""

from ..errors import ProfileBusy
from .advisory_lock import advisory_lock


def profile_lock(path):
    return advisory_lock(
        path,
        lambda: ProfileBusy(
            'profile_busy: provider profile is in use; retry after the other process exits'
        ),
        retain_on_cleanup_failure=True,
    )
