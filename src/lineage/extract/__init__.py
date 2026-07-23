"""Extraction layer: pull host data into the local raw store.

All host access goes through the :class:`~lineage.extract.connection.HostSession`
interface so parsers, graph building, and tests never touch the LPAR. A
fixture-backed session stands in for CI.
"""
