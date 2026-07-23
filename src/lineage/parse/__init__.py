"""Source parsing layer.

Parsers are pure functions over :class:`~lineage.parse.base.SourceMember`
values and are tested directly against fixture text — no host, no DuckDB.
Orchestration functions load members from the raw store, run the parsers, and
write the parsed-layer tables.
"""
