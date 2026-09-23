"""What the planner knows about a dataset between pipeline steps.

Each brick declares what it needs from a DatasetState and how it changes it,
so incompatible combinations fail at planning time instead of hours into a job.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

XKind = Literal["raw_counts", "normalized_log", "scaled", "unknown"]
GeneIds = Literal["symbol", "ensembl", "unknown"]
Species = Literal["human", "mouse", "unknown"]
ColumnKind = Literal["categorical", "numeric", "string", "other"]
DataSource = Literal["h5ad", "fastq"]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ObsColumn(Frozen):
    name: str
    kind: ColumnKind
    n_unique: int | None = None
    top: dict[str, int] = Field(default_factory=dict)
    derived: bool = False  # created by a brick (e.g. clusters), not measured metadata

    @property
    def levels_known(self) -> bool:
        """True when `top` lists every level, so membership checks are exact."""
        return self.n_unique is not None and len(self.top) == self.n_unique


class DatasetState(Frozen):
    n_obs: int
    n_vars: int
    x_kind: XKind
    counts_layer: bool = False
    norm_target: float | None = None
    gene_ids: GeneIds = "unknown"
    species: Species = "unknown"
    mito_genes: int | None = None  # var names starting with MT- (case-insensitive)
    obs: tuple[ObsColumn, ...] = ()
    obsm: tuple[str, ...] = ()
    layers: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    # Raw reads instead of a count matrix: only a counting brick can come first.
    source: DataSource = "h5ad"
    technology: str | None = None  # FASTQ chemistry, e.g. 10xv3
    samples: tuple[str, ...] = ()  # FASTQ sample names
    fastq_gb: float | None = None  # read volume, for time estimates

    def obs_column(self, name: str) -> ObsColumn | None:
        return next((c for c in self.obs if c.name == name), None)

    def has_obs(self, name: str) -> bool:
        return self.obs_column(name) is not None

    def has_raw_counts(self) -> bool:
        return self.counts_layer or self.x_kind == "raw_counts"

    def update(self, **changes: object) -> DatasetState:
        return self.model_copy(update=changes)

    def with_obs(self, *names: str, kind: ColumnKind = "categorical") -> DatasetState:
        added = tuple(
            ObsColumn(name=n, kind=kind, derived=True) for n in names if not self.has_obs(n)
        )
        return self.update(obs=self.obs + added)

    def with_obsm(self, *keys: str) -> DatasetState:
        return self.update(obsm=tuple(sorted({*self.obsm, *keys})))

    def with_layers(self, *names: str) -> DatasetState:
        return self.update(layers=tuple(sorted({*self.layers, *names})))

    def with_flags(self, *flags: str) -> DatasetState:
        return self.update(flags=tuple(sorted({*self.flags, *flags})))


class Issue(Frozen):
    level: Literal["error", "warning"]
    code: str
    message: str
    step: int | None = None


def error(code: str, message: str, step: int | None = None) -> Issue:
    return Issue(level="error", code=code, message=message, step=step)


def warning(code: str, message: str, step: int | None = None) -> Issue:
    return Issue(level="warning", code=code, message=message, step=step)
