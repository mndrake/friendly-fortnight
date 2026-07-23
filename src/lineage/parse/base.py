"""Shared parsing primitives: the source member model and member loading."""
from __future__ import annotations

from dataclasses import dataclass, field


# Member types grouped by the parser that handles them.
CL_TYPES = {"CLP", "CLLE", "CL"}
RPG_TYPES = {"RPGLE", "SQLRPGLE", "RPG", "RPG38", "RPT", "SQLRPG"}
DDS_TYPES = {"PF", "LF", "PF38", "LF38"}
SQL_TYPES = {"SQL", "TABLE", "VIEW"}


@dataclass
class SourceMember:
    library: str
    srcfile: str
    member: str
    member_type: str | None
    lines: list[str] = field(default_factory=list)

    @property
    def program_id(self) -> str:
        """Stable object identity: library/member (the compiled object name)."""
        return f"{self.library}/{self.member}"

    @property
    def type_upper(self) -> str:
        return (self.member_type or "").strip().upper()

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def is_cl(self) -> bool:
        return self.type_upper in CL_TYPES

    def is_rpg(self) -> bool:
        return self.type_upper in RPG_TYPES

    def is_dds(self) -> bool:
        return self.type_upper in DDS_TYPES

    def has_embedded_sql(self) -> bool:
        return self.type_upper in {"SQLRPGLE", "SQLRPG"}


def load_members(con) -> list[SourceMember]:
    """Reconstruct source members from ``raw_source_members`` in seq order."""
    rows = con.execute(
        """
        SELECT library, srcfile, member, any_value(member_type) AS member_type,
               list(line_text ORDER BY seq) AS lines
        FROM raw_source_members
        GROUP BY library, srcfile, member
        ORDER BY library, srcfile, member
        """
    ).fetchall()
    members = []
    for library, srcfile, member, member_type, lines in rows:
        members.append(SourceMember(
            library=library, srcfile=srcfile, member=member,
            member_type=member_type,
            lines=[("" if ln is None else str(ln)) for ln in (lines or [])],
        ))
    return members


def member_index(members: list[SourceMember]) -> dict[str, SourceMember]:
    """Index members by uppercased member name for /COPY and call resolution.

    Later duplicates do not overwrite earlier ones; callers needing library
    disambiguation should filter the list themselves.
    """
    idx: dict[str, SourceMember] = {}
    for m in members:
        idx.setdefault(m.member.upper(), m)
    return idx
