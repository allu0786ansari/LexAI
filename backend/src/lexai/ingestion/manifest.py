"""
Corpus manifest (`data/manifest.yaml`): what is indexed, where it comes from,
and what it must contain.

- `documents`: one entry per source, either downloaded (`urls`) or bundled (`file`).
- `collections`: a glob of bundled files of one type (e.g. judgments), one document each.

Integrity:
- bundled files are checked against `data/bundled/SHA256SUMS`;
- downloaded files are pinned on first download in `data/manifest.lock.json`
  (commit it), so later downloads must match byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from lexai.core.errors import ConfigurationError
from lexai.domain.models import DocType

_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class DocumentSpec(BaseModel):
    id: str
    title: str
    short_name: str
    doc_type: DocType
    year: int | None = None
    in_force_from: date | None = None
    repealed_on: date | None = None
    replaced_by: str | None = None
    urls: list[str] = Field(default_factory=list)
    file: str | None = None
    expect: list[str] = Field(default_factory=list)
    min_units: int = 0

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError(f"id {v!r} must be lowercase letters, digits and hyphens")
        return v

    @model_validator(mode="after")
    def _one_source(self) -> DocumentSpec:
        if bool(self.urls) == bool(self.file):
            raise ValueError(f"{self.id}: give exactly one of `urls` or `file`")
        return self

    @property
    def downloaded(self) -> bool:
        return bool(self.urls)

    @property
    def unit_label(self) -> str:
        return "Article" if self.doc_type == "constitution" else "Section"

    @property
    def status(self) -> str:
        if self.repealed_on:
            replaced = f" (replaced by {self.replaced_by})" if self.replaced_by else ""
            return f"repealed {self.repealed_on.isoformat()}{replaced}"
        return "in force"


class CollectionSpec(BaseModel):
    id_prefix: str
    doc_type: Literal["judgment", "manual"]
    glob: str
    title_prefix: str


class Manifest(BaseModel):
    version: int
    documents: list[DocumentSpec]
    collections: list[CollectionSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references(self) -> Manifest:
        ids = [d.id for d in self.documents]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate document ids: {sorted(dupes)}")
        for d in self.documents:
            if d.replaced_by and d.replaced_by not in ids:
                raise ValueError(f"{d.id}: replaced_by {d.replaced_by!r} is not a document id")
        return self


def load_manifest(path: Path) -> Manifest:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Manifest.model_validate(data)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"Corpus manifest not found: {path}") from exc
    except (yaml.YAMLError, ValueError) as exc:
        raise ConfigurationError(f"Invalid corpus manifest {path}: {exc}") from exc


def judgment_id(prefix: str, path: Path) -> str:
    """`-0___jonew__judis__10693.pdf` -> `sc-judis-10693`."""
    stem = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")
    stem = re.sub(r"^0-jonew-judis-", "judis-", stem)
    return f"{prefix}-{stem}"


def expand(manifest: Manifest, bundled_dir: Path) -> list[DocumentSpec]:
    """All documents, with each collection expanded into one spec per file."""
    docs = list(manifest.documents)
    for coll in manifest.collections:
        for path in sorted(bundled_dir.glob(coll.glob)):
            rel = path.relative_to(bundled_dir).as_posix()
            docs.append(
                DocumentSpec(
                    id=judgment_id(coll.id_prefix, path),
                    title=f"{coll.title_prefix} — {path.stem}",  # replaced by the case name when parsed
                    short_name=coll.title_prefix,
                    doc_type=coll.doc_type,
                    file=rel,
                )
            )
    return docs


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_bundled_sums(bundled_dir: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    path = bundled_dir / "SHA256SUMS"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                digest, name = line.split(maxsplit=1)
                sums[name.strip()] = digest
    return sums


class LockEntry(BaseModel):
    url: str
    sha256: str
    bytes: int
    fetched_at: str


def read_lock(path: Path) -> dict[str, LockEntry]:
    if not path.exists():
        return {}
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return {k: LockEntry.model_validate(v) for k, v in raw.get("documents", {}).items()}


def write_lock(path: Path, entries: dict[str, LockEntry]) -> None:
    payload = {
        "comment": "Pinned downloads for data/manifest.yaml. Commit this file.",
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "documents": {k: v.model_dump() for k, v in sorted(entries.items())},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def source_path(spec: DocumentSpec, bundled_dir: Path, raw_dir: Path) -> Path:
    return raw_dir / f"{spec.id}.pdf" if spec.downloaded else bundled_dir / str(spec.file)


def verify_source(
    spec: DocumentSpec,
    bundled_dir: Path,
    raw_dir: Path,
    bundled_sums: dict[str, str],
    lock: dict[str, LockEntry],
) -> tuple[Path, str]:
    """Return (path, sha256) or raise ConfigurationError explaining what is wrong."""
    path = source_path(spec, bundled_dir, raw_dir)
    if not path.exists():
        hint = "run the fetch step first" if spec.downloaded else "the bundled file is missing"
        raise ConfigurationError(f"{spec.id}: {path} not found ({hint}).")
    digest = sha256_file(path)
    if spec.downloaded:
        entry = lock.get(spec.id)
        if entry is None:
            raise ConfigurationError(f"{spec.id}: not pinned in data/manifest.lock.json; re-run fetch.")
        if entry.sha256 != digest:
            raise ConfigurationError(f"{spec.id}: file does not match its pinned checksum; re-run fetch.")
    else:
        expected = bundled_sums.get(str(spec.file))
        if expected is None:
            raise ConfigurationError(f"{spec.id}: {spec.file} is not listed in data/bundled/SHA256SUMS.")
        if expected != digest:
            raise ConfigurationError(f"{spec.id}: {spec.file} does not match data/bundled/SHA256SUMS.")
    return path, digest
