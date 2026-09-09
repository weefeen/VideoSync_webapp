"""The seam between the process that takes the order and the one that works.

Today both are the same process and the messages are handed straight across
(`ledger.apply` called from the worker thread). That is deliberate: the
shapes and the rules are what this package is for, and they are settled
while everything is still in one place and easy to check. A broker changes
where the messages travel, not what they say.

The rule that makes the second host possible: **the worker never opens
`jobs.sqlite`.** Everything it needs arrives in a `RenderTask`, and
everything it learns leaves as an `Event`. The web side owns the table.
"""
