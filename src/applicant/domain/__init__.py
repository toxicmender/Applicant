"""The pure core: what a job is, whether it matches, and which copy to keep.

Nothing in here touches the network, a file or a browser, and nothing imports
the layers above it (`tests/test_domain.py` checks both). What the domain needs
from outside - exchange rates, which board publishes what, today's date - is
handed in, which is what lets every rule here be tested in isolation.
"""
