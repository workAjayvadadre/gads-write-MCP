"""Safety core: validation, policy, plans, spend tracking, audit, guards.

Nothing in this package imports the Google Ads client library. It is
deliberately testable without credentials, without a network, and without
a clock that moves on its own.
"""
