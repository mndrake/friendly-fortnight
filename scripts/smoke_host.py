#!/usr/bin/env python3
"""Live-host smoke test (excluded from CI).

Validates against a real IBM i:
1. JDBC connection comes up and a trivial catalog query answers.
2. QCMDEXC executes a harmless command.
3. A DSPPGMREF outfile round-trips with the expected column layout.
4. One source member retrieves as readable text (CCSID round-trip).

Usage: python scripts/smoke_host.py [config.yaml]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from lineage.config import load_config  # noqa: E402
from lineage.extract.connection import open_session  # noqa: E402
from lineage.extract.xref import OutfileShapeError, PGMREF_LAYOUT  # noqa: E402


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)
    session = open_session(cfg.connection)
    ok = True
    try:
        # 0. Version + capability probe.
        from lineage.extract import hostinfo
        from lineage.extract.source import resolve_strategy
        prof = hostinfo.probe(session)
        print(f"[0] host: {prof.version_label} "
              f"(jdbc: {prof.product_version or 'n/a'}); "
              f"IFS_READ={'yes' if prof.has_ifs_read else 'no'}; "
              f"source strategy ({cfg.source_retrieval}) -> "
              f"{resolve_strategy(cfg, prof)}")
        if not prof.catalog_columns:
            print("    WARNING: catalog introspection empty — extraction "
                  "will use documented default column names")

        # 1. Catalog answers.
        r = session.query(
            "SELECT COUNT(*) FROM QSYS2.SYSTABLES WHERE TABLE_SCHEMA = ?",
            [cfg.libraries[0]])
        print(f"[1] catalog: {r.rows[0][0]} tables in {cfg.libraries[0]}")

        # 2. QCMDEXC.
        session.run_cl(f"CHKOBJ OBJ({cfg.libraries[0]}) OBJTYPE(*LIB)")
        print("[2] QCMDEXC: CHKOBJ ok")

        # 3. DSPPGMREF outfile layout: probe the outfile's actual columns,
        #    resolve our candidate-name layout against them, print the
        #    per-field mapping, then pull a few sample rows through it.
        of = f"{cfg.scratch_lib}/SMOKEPR"
        session.run_cl(
            f"DSPPGMREF PGM({cfg.libraries[0]}/*ALL) OUTPUT(*OUTFILE) "
            f"OUTFILE({of})")
        probe = session.query(
            f"SELECT * FROM {cfg.scratch_lib}.SMOKEPR FETCH FIRST 1 ROWS ONLY")
        try:
            select_list, missing_optional = PGMREF_LAYOUT.resolve(probe.columns)
        except OutfileShapeError as exc:
            print(f"[3] DSPPGMREF layout MISMATCH: {exc}")
            ok = False
        else:
            print(f"[3] DSPPGMREF layout resolved against actual columns "
                  f"{probe.columns}:")
            for part in select_list.split(", "):
                actual, _, raw = part.rpartition(" AS ")
                shown = "NULL (optional, no candidate matched)" \
                    if raw in missing_optional else actual
                print(f"    {raw} <- {shown}")
            r = session.query(
                f"SELECT {select_list} FROM {cfg.scratch_lib}.SMOKEPR "
                f"FETCH FIRST 5 ROWS ONLY")
            print(f"[3] DSPPGMREF layout ok ({len(r.rows)} sample rows)")

        # 4. Source member round-trip via the configured retrieval strategy
        #    (ifs_read needs QSYS2.IFS_READ: IBM i 7.3 TR7 / 7.4+). Member
        #    enumeration goes through the profile-adaptive path — the member
        #    column is TABLE_PARTITION on current releases, PARTITION_NAME
        #    on others; never hardcode either.
        if cfg.source_files:
            from lineage.extract.source import enumerate_members, retrieve_member
            src = cfg.source_files[0]
            members = enumerate_members(session, src, prof)
            if members:
                member = (members[0].get("member") or "").strip()
                lines, strategy = retrieve_member(session, src, member, cfg)
                text = " ".join(t for _, t in lines[:5])
                printable = sum(1 for ch in text if ch.isprintable())
                if text and printable / max(len(text), 1) > 0.9:
                    print(f"[4] source round-trip ok ({src.qualified}/"
                          f"{member}, strategy={strategy})")
                    if strategy == "alias_fallback":
                        print("    note: IFS_READ returned no rows for this "
                              "member (data-PF source file or SRCDTA CCSID "
                              "65535?) — alias fallback was used")
                else:
                    print(f"[4] source text empty or garbled "
                          f"(strategy={strategy}) — check member contents, "
                          "CCSID, and QSYS2.JOBLOG_INFO('*') for the "
                          "underlying IFS_READ message")
                    ok = False
            else:
                print(f"[4] no members in {src.qualified}, skipped")
    finally:
        session.close()
    print("SMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
