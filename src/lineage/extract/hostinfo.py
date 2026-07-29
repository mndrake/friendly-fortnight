"""Host capability probe.

IBM i catalog views gain columns with Technology Refreshes, not only version
bumps, so hardcoding a SELECT list breaks across estates. The probe records,
once per extract:

* the DB2/OS version — from JDBC ``DatabaseMetaData`` when the session
  exposes it, enriched by ``SYSIBMADM.ENV_SYS_INFO``;
* which columns each QSYS2 catalog view actually has (asked from
  ``QSYS2.SYSCOLUMNS`` itself — self-describing and TR-accurate);
* whether ``QSYS2.IFS_READ`` exists (drives source-retrieval ``auto`` mode).

The resulting :class:`HostProfile` is persisted to the ``host_profile``
DuckDB table and drives adaptive SELECT building in
:mod:`lineage.extract.catalog` and strategy resolution in
:mod:`lineage.extract.source`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .connection import HostSession

# Catalog views whose column sets we introspect.
CATALOG_VIEWS = ("SYSTABLES", "SYSCOLUMNS", "SYSVIEWS", "SYSVIEWDEP",
                 "SYSPARTITIONSTAT", "SYSROUTINES")


@dataclass
class HostProfile:
    product_name: Optional[str] = None
    product_version: Optional[str] = None   # e.g. "07.04.0000 V7R4m0"
    os_version: Optional[str] = None        # e.g. "7"
    os_release: Optional[str] = None        # e.g. "4"
    catalog_columns: dict[str, set[str]] = field(default_factory=dict)
    has_ifs_read: bool = False

    @property
    def version_label(self) -> str:
        if self.os_version and self.os_release:
            return f"IBM i {self.os_version}.{self.os_release}"
        return self.product_version or "unknown"

    def columns_of(self, catalog_view: str) -> set[str]:
        return self.catalog_columns.get(catalog_view.upper(), set())

    def save(self, con) -> None:
        con.execute("DELETE FROM host_profile")
        rows = [
            ("product_name", self.product_name),
            ("product_version", self.product_version),
            ("os_version", self.os_version),
            ("os_release", self.os_release),
            ("has_ifs_read", json.dumps(self.has_ifs_read)),
            ("catalog_columns", json.dumps(
                {k: sorted(v) for k, v in self.catalog_columns.items()})),
        ]
        con.executemany(
            "INSERT INTO host_profile (key, value) VALUES (?, ?)",
            [(k, v) for k, v in rows if v is not None])

    @classmethod
    def load(cls, con) -> "HostProfile":
        data = dict(con.execute(
            "SELECT key, value FROM host_profile").fetchall())
        return cls(
            product_name=data.get("product_name"),
            product_version=data.get("product_version"),
            os_version=data.get("os_version"),
            os_release=data.get("os_release"),
            has_ifs_read=json.loads(data.get("has_ifs_read", "false")),
            catalog_columns={
                k: set(v) for k, v in
                json.loads(data.get("catalog_columns", "{}")).items()},
        )


def _fetch(session: HostSession, tag: str, sql: str):
    if hasattr(session, "with_tag"):
        return session.with_tag(tag).query(sql)
    return session.query(sql)


def probe(session: HostSession) -> HostProfile:
    """Interrogate the host. Every step degrades gracefully — a probe failure
    leaves the corresponding field empty rather than aborting extraction."""
    profile = HostProfile()

    # 1. JDBC driver metadata (no SQL, no authority requirements).
    if hasattr(session, "database_version"):
        try:
            info = session.database_version()
            profile.product_name = info.get("product_name")
            profile.product_version = info.get("product_version")
        except Exception:  # noqa: BLE001
            pass

    # 2. OS version/release via SQL.
    try:
        res = _fetch(session, "probe.env",
                     "SELECT OS_VERSION, OS_RELEASE FROM SYSIBMADM.ENV_SYS_INFO")
        if res.rows:
            profile.os_version = str(res.rows[0][0]).strip()
            profile.os_release = str(res.rows[0][1]).strip()
    except Exception:  # noqa: BLE001
        pass

    # 3. Catalog self-description: which columns do the QSYS2 views have here?
    try:
        in_list = ", ".join(f"'{v}'" for v in CATALOG_VIEWS)
        res = _fetch(session, "probe.catalog_columns", f"""
            SELECT TABLE_NAME, COLUMN_NAME FROM QSYS2.SYSCOLUMNS
            WHERE TABLE_SCHEMA = 'QSYS2' AND TABLE_NAME IN ({in_list})
        """)
        for table, column in res.rows:
            profile.catalog_columns.setdefault(
                str(table).strip().upper(), set()).add(
                str(column).strip().upper())
    except Exception:  # noqa: BLE001
        pass

    # 4. Is QSYS2.IFS_READ available on this release?
    try:
        res = _fetch(session, "probe.ifs_read", """
            SELECT COUNT(*) FROM QSYS2.SYSROUTINES
            WHERE ROUTINE_SCHEMA = 'QSYS2' AND ROUTINE_NAME = 'IFS_READ'
        """)
        profile.has_ifs_read = bool(res.rows and res.rows[0][0])
    except Exception:  # noqa: BLE001
        profile.has_ifs_read = False

    return profile
