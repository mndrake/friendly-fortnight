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
from lineage.extract.xref import PGMREF_LAYOUT  # noqa: E402


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)
    session = open_session(cfg.connection)
    ok = True
    try:
        # 1. Catalog answers.
        r = session.query(
            "SELECT COUNT(*) FROM QSYS2.SYSTABLES WHERE TABLE_SCHEMA = ?",
            [cfg.libraries[0]])
        print(f"[1] catalog: {r.rows[0][0]} tables in {cfg.libraries[0]}")

        # 2. QCMDEXC.
        session.run_cl(f"CHKOBJ OBJ({cfg.libraries[0]}) OBJTYPE(*LIB)")
        print("[2] QCMDEXC: CHKOBJ ok")

        # 3. DSPPGMREF outfile layout.
        of = f"{cfg.scratch_lib}/SMOKEPR"
        session.run_cl(
            f"DSPPGMREF PGM({cfg.libraries[0]}/*ALL) OUTPUT(*OUTFILE) "
            f"OUTFILE({of})")
        r = session.query(
            f"SELECT {PGMREF_LAYOUT.select_list()} FROM "
            f"{cfg.scratch_lib}.SMOKEPR FETCH FIRST 5 ROWS ONLY")
        missing = [c for c in PGMREF_LAYOUT.raw_columns if c.upper() not in
                   {x.upper() for x in r.columns}]
        if missing:
            print(f"[3] DSPPGMREF layout MISMATCH, missing: {missing}")
            ok = False
        else:
            print(f"[3] DSPPGMREF layout ok ({len(r.rows)} sample rows)")

        # 4. Source member round-trip via the configured retrieval strategy
        #    (ifs_read needs QSYS2.IFS_READ: IBM i 7.3 TR7 / 7.4+).
        if cfg.source_files:
            from lineage.extract.source import retrieve_member
            src = cfg.source_files[0]
            r = session.query(
                "SELECT PARTITION_NAME FROM QSYS2.SYSPARTITIONSTAT "
                f"WHERE TABLE_SCHEMA = '{src.library}' AND "
                f"TABLE_NAME = '{src.file}' FETCH FIRST 1 ROWS ONLY")
            if r.rows:
                member = r.rows[0][0].strip()
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
