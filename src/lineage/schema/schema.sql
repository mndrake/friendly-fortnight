-- DB2 for i Lineage Analyzer — DuckDB schema.
-- Idempotent: safe to run repeatedly. Layered raw -> parsed -> graph -> analysis.

-- =========================================================================
-- Raw layer: verbatim host pulls. Nothing here is interpreted.
-- =========================================================================

CREATE TABLE IF NOT EXISTS raw_systables (
    table_schema   VARCHAR,   -- library
    table_name     VARCHAR,   -- SQL name
    system_name    VARCHAR,   -- 10-char system name
    table_type     VARCHAR,   -- 'T' physical, 'P' phys w/ members, 'V' view, 'L' logical, ...
    file_type      VARCHAR,
    row_count      BIGINT,
    long_comment   VARCHAR
);

CREATE TABLE IF NOT EXISTS raw_syscolumns (
    table_schema   VARCHAR,
    table_name     VARCHAR,
    system_name    VARCHAR,
    column_name    VARCHAR,
    system_column  VARCHAR,
    ordinal        INTEGER,
    data_type      VARCHAR,
    length         INTEGER,
    numeric_scale  INTEGER,
    is_nullable    VARCHAR,
    column_heading VARCHAR
);

CREATE TABLE IF NOT EXISTS raw_sysviews (
    table_schema   VARCHAR,
    table_name     VARCHAR,
    system_name    VARCHAR,
    view_definition VARCHAR
);

CREATE TABLE IF NOT EXISTS raw_sysviewdep (
    view_schema    VARCHAR,
    view_name      VARCHAR,
    object_schema  VARCHAR,   -- referenced object library
    object_name    VARCHAR,   -- referenced object
    object_type    VARCHAR    -- TABLE / VIEW / ...
);

CREATE TABLE IF NOT EXISTS raw_syspartitionstat (
    table_schema   VARCHAR,
    table_name     VARCHAR,
    system_name    VARCHAR,
    partition_name VARCHAR,   -- member name
    number_rows    BIGINT,
    source_type    VARCHAR    -- for source physical files: CLP, RPGLE, ...
);

CREATE TABLE IF NOT EXISTS raw_dsppgmref (
    -- Mapped from QSYS/QADSPPGM outfile (QWHDRFFI family) with explicit names.
    program_lib    VARCHAR,   -- WHLIB
    program_name   VARCHAR,   -- WHPNAM
    object_lib     VARCHAR,   -- WHOLIB referenced object library
    object_name    VARCHAR,   -- WHFNAM referenced object
    object_type    VARCHAR,   -- WHOTYP  (F=file, ...)
    usage_flag     VARCHAR,   -- WHRFNM/ WHFUSG usage (I/O/U/blank)
    ref_count      INTEGER
);

CREATE TABLE IF NOT EXISTS raw_dspdbr (
    -- Database relations: dependent (logical) -> based-on (physical).
    dep_lib        VARCHAR,   -- WHRELI dependent library
    dep_file       VARCHAR,   -- WHRFNM dependent file
    based_lib      VARCHAR,   -- WHRFLI based-on library
    based_file     VARCHAR,   -- WHRFN  based-on file
    dep_type       VARCHAR    -- relation type
);

CREATE TABLE IF NOT EXISTS raw_dspffd (
    -- Field descriptions per file/record format.
    file_lib       VARCHAR,   -- WHLIB
    file_name      VARCHAR,   -- WHFILE
    record_format  VARCHAR,   -- WHNAME (record format name)
    field_name     VARCHAR,   -- WHFLDE / WHFLD
    field_type     VARCHAR,   -- WHFLDT
    field_length   INTEGER,   -- WHFLDB
    field_scale    INTEGER,
    field_text     VARCHAR,   -- WHFTXT
    field_ordinal  INTEGER
);

CREATE TABLE IF NOT EXISTS host_profile (
    -- Capability probe results (version, catalog columns, IFS_READ presence).
    key            VARCHAR,
    value          VARCHAR
);

CREATE TABLE IF NOT EXISTS raw_source_members (
    library        VARCHAR,
    srcfile        VARCHAR,
    member         VARCHAR,
    member_type    VARCHAR,   -- CLP/CLLE/RPGLE/SQLRPGLE/PF/LF/...
    seq            INTEGER,   -- SRCSEQ
    line_text      VARCHAR    -- SRCDTA
);

CREATE TABLE IF NOT EXISTS slice_objects (
    -- Targeted extraction only (extraction_scope=targeted): every object
    -- pulled into the slice, and why. Auditable record of what a targeted
    -- run downloaded and which round/mechanism added it.
    kind           VARCHAR,   -- program | file | member
    library        VARCHAR,
    name           VARCHAR,
    round          INTEGER,   -- 0 = seed/backward-walk/caller, 1..5 = iterative rounds
    reason         VARCHAR    -- seed / backward_walk / caller / dspdbr_based_on / ...
);

-- =========================================================================
-- Parsed layer: interpreted source. Every row is derivable from raw + parser.
-- =========================================================================

CREATE TABLE IF NOT EXISTS parsed_cl_statements (
    program        VARCHAR,   -- library/member
    seq            INTEGER,
    command        VARCHAR,   -- CL command verb (OVRDBF, CALL, ...)
    raw_text       VARCHAR
);

CREATE TABLE IF NOT EXISTS parsed_cl_overrides (
    program        VARCHAR,
    seq            INTEGER,
    file           VARCHAR,   -- FILE() overridden name
    to_file        VARCHAR,   -- TOFILE() target (library/file or file)
    to_library     VARCHAR,
    to_member      VARCHAR,   -- MBR()
    scope          VARCHAR,   -- OVRSCOPE value
    resolved       BOOLEAN,   -- false when name is runtime-dependent
    expr           VARCHAR    -- captured expression when unresolved
);

CREATE TABLE IF NOT EXISTS parsed_cl_calls (
    program        VARCHAR,
    seq            INTEGER,
    called_lib     VARCHAR,
    called_pgm     VARCHAR,
    via            VARCHAR,   -- CALL / SBMJOB / RUNSQLSTM / CPYF ...
    params         VARCHAR,   -- captured PARM list (JSON)
    resolved       BOOLEAN,
    expr           VARCHAR
);

CREATE TABLE IF NOT EXISTS parsed_dds_files (
    library        VARCHAR,
    file           VARCHAR,   -- member name = file name
    dds_type       VARCHAR,   -- PF / LF
    record_format  VARCHAR,
    based_on       VARCHAR,   -- PFILE/JFILE targets (JSON list)
    is_join        BOOLEAN
);

CREATE TABLE IF NOT EXISTS parsed_dds_fields (
    library        VARCHAR,
    file           VARCHAR,
    record_format  VARCHAR,
    field_name     VARCHAR,
    renamed_from   VARCHAR,   -- RENAME source field
    ref_field      VARCHAR,   -- for LF: PF field it derives from
    ref_file       VARCHAR,   -- PF file for the ref (join logicals)
    concat_fields  VARCHAR,   -- CONCAT source fields (JSON)
    usage          VARCHAR    -- I/O/B/N (input/output/both/neither)
);

CREATE TABLE IF NOT EXISTS parsed_rpg_files (
    program        VARCHAR,
    file           VARCHAR,   -- file name as declared
    usage          VARCHAR,   -- input/output/update/combined
    extname        VARCHAR,   -- EXTNAME / EXTFILE resolved external file
    rename_rec     VARCHAR,   -- RENAME record format
    declared_via   VARCHAR    -- fspec / dclf
);

CREATE TABLE IF NOT EXISTS parsed_rpg_io_ops (
    program        VARCHAR,
    seq            INTEGER,
    opcode         VARCHAR,   -- CHAIN/READ/WRITE/UPDATE/DELETE/SETLL/...
    file           VARCHAR,
    direction      VARCHAR    -- read / write
);

CREATE TABLE IF NOT EXISTS parsed_sql_statements (
    program        VARCHAR,
    seq            INTEGER,
    stmt_type      VARCHAR,   -- SELECT/INSERT/UPDATE/DELETE/MERGE/CREATE_VIEW...
    ast_json       VARCHAR,   -- sqlglot AST as JSON (may be null on parse fail)
    tables_read    VARCHAR,   -- JSON list of library/table
    tables_written VARCHAR,   -- JSON list
    column_lineage VARCHAR,   -- JSON: [{target, sources:[...]}]
    parse_error    VARCHAR,   -- null when parsed cleanly
    raw_sql        VARCHAR
);

CREATE TABLE IF NOT EXISTS program_classification (
    program        VARCHAR,
    program_class  VARCHAR,   -- sql_pure / ext_described_io / program_described_or_complex
    reasons        VARCHAR,   -- JSON list of heuristics that fired
    needs_review   BOOLEAN
);

-- =========================================================================
-- Graph layer.
-- =========================================================================

CREATE TABLE IF NOT EXISTS nodes (
    id             VARCHAR PRIMARY KEY,  -- e.g. file:LIB/NAME, column:LIB/NAME.FLD
    kind           VARCHAR,   -- program|file|column|member|view
    library        VARCHAR,
    name           VARCHAR,
    attrs          VARCHAR    -- JSON
);

CREATE TABLE IF NOT EXISTS edges (
    src            VARCHAR,
    dst            VARCHAR,
    kind           VARCHAR,   -- reads|writes|overrides|calls|derives_from|defines
    provenance     VARCHAR,   -- catalog|xref|source_cl|source_rpg|source_sql|dds
    confidence     VARCHAR,   -- confirmed|parsed|inferred|unresolved
    context        VARCHAR    -- JSON (e.g. CL call-stack scope of an override)
);

-- =========================================================================
-- Analysis layer.
-- =========================================================================

CREATE TABLE IF NOT EXISTS output_lineage (
    output_id      VARCHAR,
    source_file    VARCHAR,   -- base physical file node id
    source_column  VARCHAR,   -- nullable
    path_len       INTEGER,
    min_confidence VARCHAR
);

CREATE TABLE IF NOT EXISTS commonality_matrix (
    output_id      VARCHAR,
    source_id      VARCHAR,   -- base PF or column node id
    present        BOOLEAN
);

CREATE TABLE IF NOT EXISTS complexity_scores (
    output_id      VARCHAR,
    path_depth     INTEGER,
    complex_pgms   INTEGER,
    override_depth INTEGER,
    unresolved     INTEGER,
    bucket         VARCHAR    -- replicate_as_view / moderate / full_reengineering
);

CREATE TABLE IF NOT EXISTS gaps (
    kind           VARCHAR,   -- unresolved_dynamic_name / missing_source / outside_scope / ambiguous_liblist / parse_error / no_lineage
    object_id      VARCHAR,   -- the affected node/object
    detail         VARCHAR,
    context        VARCHAR    -- JSON
);
