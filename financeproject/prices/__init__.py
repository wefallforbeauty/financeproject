"""Price and CPI sources, and storing them in the ledger database.

Parsing is kept separate from fetching: every ``parse_*`` function takes the raw
response text, so the tests run on saved responses without the network.
"""
