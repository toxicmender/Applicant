"""Where jobs, applications and ratings are kept.

`sqlite.Store` is the record; `repositories` gives the services one interface
over it and over the plain files (`store = "files"` in Settings), so the choice
is a setting rather than a code path.
"""
