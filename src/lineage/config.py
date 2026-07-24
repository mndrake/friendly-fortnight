"""Configuration loading and validation.

Libraries, output seeds, and assumed library lists are *configuration, not
discovery* (design principle): they are supplied here and never inferred from
the host.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class SourceFileRef:
    library: str
    file: str

    @property
    def qualified(self) -> str:
        return f"{self.library}/{self.file}"


@dataclass(frozen=True)
class OutputSeed:
    id: str
    library: str
    file: str

    @property
    def node_id(self) -> str:
        return f"file:{self.library}/{self.file}"


@dataclass(frozen=True)
class ConnectionConfig:
    host: str | None = None
    jar: str | None = None
    driver_class: str = "com.ibm.as400.access.AS400JDBCDriver"
    url: str | None = None
    user: str | None = None
    password: str | None = None
    properties: dict[str, str] = field(default_factory=dict)

    def resolved_url(self) -> str:
        if self.url:
            return self.url
        if not self.host:
            raise ConfigError("connection.url or connection.host is required")
        return f"jdbc:as400://{self.host}"

    def resolved_password(self) -> str | None:
        # Environment variable always wins over a value stored in config.
        return os.environ.get("LINEAGE_DB_PASSWORD", self.password)


@dataclass(frozen=True)
class StorageConfig:
    duckdb: str = "data/lineage.duckdb"
    parquet_dir: str = "data/parquet"


# How source member text is retrieved from the host:
# - "ifs_read": QSYS2.IFS_READ over the member's /QSYS.LIB path. Stateless,
#   no scratch objects; requires IBM i 7.3 TR7 / 7.4 or later.
# - "alias": CREATE ALIAS in scratch_lib -> SELECT -> DROP ALIAS. Works on
#   older releases; creates a temporary object per member read.
SOURCE_RETRIEVAL_MODES = ("ifs_read", "alias")


@dataclass(frozen=True)
class Config:
    connection: ConnectionConfig
    scratch_lib: str
    libraries: tuple[str, ...]
    source_files: tuple[SourceFileRef, ...]
    output_seeds: tuple[OutputSeed, ...]
    liblists: dict[str, tuple[str, ...]]
    storage: StorageConfig
    source_retrieval: str = "ifs_read"
    root: Path = Path(".")

    def liblist(self, name: str | None) -> tuple[str, ...]:
        """Return the configured library list for a job/subsystem name.

        Falls back to the ``default`` liblist, then to the plain library scan
        order if no ``default`` is configured.
        """
        if name and name in self.liblists:
            return self.liblists[name]
        if "default" in self.liblists:
            return self.liblists["default"]
        return self.libraries

    @property
    def duckdb_path(self) -> Path:
        return self.root / self.storage.duckdb

    @property
    def parquet_dir(self) -> Path:
        return self.root / self.storage.parquet_dir


class ConfigError(ValueError):
    """Raised when configuration is missing required fields or malformed."""


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping or mapping[key] is None:
        raise ConfigError(f"missing required config key '{key}' in {where}")
    return mapping[key]


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return from_dict(raw, root=path.parent if path.parent != Path("") else Path("."))


def from_dict(raw: dict[str, Any], root: Path = Path(".")) -> Config:
    conn_raw = raw.get("connection") or {}
    connection = ConnectionConfig(
        host=conn_raw.get("host"),
        jar=conn_raw.get("jar"),
        driver_class=conn_raw.get("driver_class", "com.ibm.as400.access.AS400JDBCDriver"),
        url=conn_raw.get("url"),
        user=conn_raw.get("user"),
        password=conn_raw.get("password"),
        properties={str(k): str(v) for k, v in (conn_raw.get("properties") or {}).items()},
    )

    scratch_lib = _require(raw, "scratch_lib", "config root")

    libraries = tuple(str(x) for x in (raw.get("libraries") or []))
    if not libraries:
        raise ConfigError("at least one library must be configured under 'libraries'")

    source_files = tuple(
        SourceFileRef(library=str(_require(sf, "library", "source_files[]")),
                      file=str(_require(sf, "file", "source_files[]")))
        for sf in (raw.get("source_files") or [])
    )

    seeds_raw = raw.get("output_seeds") or []
    if not seeds_raw:
        raise ConfigError("at least one output seed must be configured under 'output_seeds'")
    seen_ids: set[str] = set()
    output_seeds_list = []
    for s in seeds_raw:
        sid = str(_require(s, "id", "output_seeds[]"))
        if sid in seen_ids:
            raise ConfigError(f"duplicate output seed id: {sid}")
        seen_ids.add(sid)
        output_seeds_list.append(
            OutputSeed(id=sid,
                       library=str(_require(s, "library", "output_seeds[]")),
                       file=str(_require(s, "file", "output_seeds[]")))
        )
    output_seeds = tuple(output_seeds_list)

    liblists = {
        str(name): tuple(str(lib) for lib in libs)
        for name, libs in (raw.get("liblists") or {}).items()
    }

    storage_raw = raw.get("storage") or {}
    storage = StorageConfig(
        duckdb=storage_raw.get("duckdb", "data/lineage.duckdb"),
        parquet_dir=storage_raw.get("parquet_dir", "data/parquet"),
    )

    source_retrieval = str(raw.get("source_retrieval", "ifs_read")).lower()
    if source_retrieval not in SOURCE_RETRIEVAL_MODES:
        raise ConfigError(
            f"source_retrieval must be one of {SOURCE_RETRIEVAL_MODES}, "
            f"got '{source_retrieval}'")

    return Config(
        connection=connection,
        scratch_lib=str(scratch_lib),
        libraries=libraries,
        source_files=source_files,
        output_seeds=output_seeds,
        liblists=liblists,
        storage=storage,
        source_retrieval=source_retrieval,
        root=root,
    )
