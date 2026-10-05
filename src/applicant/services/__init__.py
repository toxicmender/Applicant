"""The use cases: what each command does, callable without the command line.

Each service orchestrates sources, domain rules and storage, and reports as it
goes by logging commentary and emitting `events` - it never prints. The CLI is
one caller; a program embedding the library is another.

    search      SearchJobs - fan out over the boards, filter, re-read, enrich
    apply       ApplyToJobs - one copy per job, Easy Apply where possible, log
    reviews     fetch_reviews
    financials  track_financials
    rates       the PPP table: what is cached, and refreshing it
    status      what is stored right now
    fanout      fan_out - several sources, each failure isolated
    events      what a service emits while it works
"""
