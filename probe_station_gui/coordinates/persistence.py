"""Versioned, background persistence for measured Design coordinate frames."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import threading
import time
from typing import Protocol
import uuid

from PySide6.QtCore import QObject, Qt, Signal, Slot

from .model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
    VISIBLE_STAGE_AXES,
)
from .transforms import BFrameTransform


COORDINATE_FRAME_DOCUMENT_VERSION = 1
_RECORD_FIELDS = frozenset(
    {
        "frame_id",
        "kind",
        "name",
        "version",
        "transform",
        "readiness",
        "metadata",
    }
)
_TRANSFORM_FIELDS = frozenset(
    {
        "origin_xy_at_reference_b",
        "reference_b_deg",
        "xy_angle_at_reference_b_deg",
        "b_zero_machine_deg",
        "z_zero_machine_mm",
        "a_zero_machine_mm",
    }
)


@dataclass(frozen=True)
class FrameLoadDiagnostic:
    index: int
    message: str


@dataclass(frozen=True)
class CoordinateFrameDocument:
    version: int = COORDINATE_FRAME_DOCUMENT_VERSION
    records: tuple[CoordinateFrameRecord, ...] = ()
    diagnostics: tuple[FrameLoadDiagnostic, ...] = ()
    raw_records: tuple[object, ...] = field(default=(), compare=False, repr=False)
    accepted_record_slots: tuple[tuple[int, str], ...] = field(
        default=(),
        compare=False,
        repr=False,
    )

    def with_records(
        self,
        records: tuple[CoordinateFrameRecord, ...] | list[CoordinateFrameRecord],
    ) -> CoordinateFrameDocument:
        """Replace accepted records while retaining rejected raw document slots."""

        return replace(self, records=tuple(records))

    def to_dict(self) -> dict[str, object]:
        _require_document_version(self.version)
        design_records = tuple(
            record for record in self.records if record.kind is FrameKind.DESIGN
        )
        if not self.raw_records:
            serialized = [_record_to_dict(record) for record in design_records]
        else:
            current_by_id = {record.frame_id: record for record in design_records}
            accepted_by_slot = dict(self.accepted_record_slots)
            emitted: set[str] = set()
            serialized: list[object] = []
            for index, raw_record in enumerate(self.raw_records):
                accepted_id = accepted_by_slot.get(index)
                if accepted_id is None:
                    serialized.append(deepcopy(raw_record))
                    continue
                current = current_by_id.get(accepted_id)
                if current is not None:
                    serialized.append(_record_to_dict(current))
                    emitted.add(accepted_id)
            serialized.extend(
                _record_to_dict(record)
                for record in design_records
                if record.frame_id not in emitted
            )
        return {"version": self.version, "records": serialized}

    @classmethod
    def from_dict(cls, value: object) -> CoordinateFrameDocument:
        if not isinstance(value, dict):
            raise ValueError("Coordinate frame document root must be an object.")
        version = _require_document_version(value.get("version"))
        raw_records = value.get("records")
        if not isinstance(raw_records, list):
            raise ValueError("Coordinate frame document records must be a list.")

        records: list[CoordinateFrameRecord] = []
        diagnostics: list[FrameLoadDiagnostic] = []
        accepted_slots: list[tuple[int, str]] = []
        seen_frame_ids: set[str] = set()
        for index, raw_record in enumerate(raw_records):
            try:
                record = _record_from_dict(raw_record)
                if record.frame_id in seen_frame_ids:
                    raise ValueError(
                        f"Coordinate frame ID {record.frame_id} is duplicated."
                    )
                seen_frame_ids.add(record.frame_id)
                records.append(record)
                accepted_slots.append((index, record.frame_id))
            except Exception as exc:
                diagnostics.append(
                    FrameLoadDiagnostic(index, f"{type(exc).__name__}: {exc}")
                )
        return cls(
            version=version,
            records=tuple(records),
            diagnostics=tuple(diagnostics),
            raw_records=tuple(deepcopy(raw_records)),
            accepted_record_slots=tuple(accepted_slots),
        )


def _require_document_version(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value != COORDINATE_FRAME_DOCUMENT_VERSION
    ):
        raise ValueError(f"Unsupported coordinate frame document version: {value!r}.")
    return value


def _require_json_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string.")
    return value


def _require_json_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer.")
    return value


def _require_json_number(value: object, name: str) -> int | float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a JSON number.")
    return value


def _require_optional_json_number(
    value: object,
    name: str,
) -> int | float | None:
    if value is None:
        return None
    return _require_json_number(value, name)


def _require_exact_fields(
    value: Mapping[str, object],
    expected: frozenset[str],
    name: str,
) -> None:
    actual = set(value)
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    details: list[str] = []
    if missing:
        details.append(f"missing {missing!r}")
    if unknown:
        details.append(f"unknown {unknown!r}")
    raise ValueError(f"{name} fields are unsupported: {', '.join(details)}.")


def _record_to_dict(record: CoordinateFrameRecord) -> dict[str, object]:
    transform = record.transform
    if transform is None:
        raise ValueError("Persisted Design frame must have a transform.")
    return {
        "frame_id": record.frame_id,
        "kind": record.kind.value,
        "name": record.name,
        "version": record.version,
        "transform": {
            "origin_xy_at_reference_b": list(transform.origin_xy_at_reference_b),
            "reference_b_deg": transform.reference_b_deg,
            "xy_angle_at_reference_b_deg": transform.xy_angle_at_reference_b_deg,
            "b_zero_machine_deg": transform.b_zero_machine_deg,
            "z_zero_machine_mm": transform.z_zero_machine_mm,
            "a_zero_machine_mm": transform.a_zero_machine_mm,
        },
        "readiness": {
            axis: {
                "status": record.readiness[axis].status.value,
                "reason": record.readiness[axis].reason,
            }
            for axis in VISIBLE_STAGE_AXES
        },
        "metadata": {
            key: value
            for key, value in record.metadata.items()
            if not (isinstance(key, str) and key.startswith("_runtime_"))
        },
    }


def _record_from_dict(value: object) -> CoordinateFrameRecord:
    if not isinstance(value, dict):
        raise ValueError("Coordinate frame record must be an object.")
    _require_exact_fields(value, _RECORD_FIELDS, "Coordinate frame record")
    frame_id = _require_json_string(value["frame_id"], "Coordinate frame ID")
    kind = FrameKind(_require_json_string(value["kind"], "Coordinate frame kind"))
    if kind is not FrameKind.DESIGN:
        raise ValueError("Coordinate frame document may contain Design records only.")
    name = _require_json_string(value["name"], "Coordinate frame name")
    version = _require_json_integer(value["version"], "Coordinate frame version")
    raw_transform = value["transform"]
    if not isinstance(raw_transform, dict):
        raise ValueError("Coordinate frame transform must be an object.")
    _require_exact_fields(
        raw_transform,
        _TRANSFORM_FIELDS,
        "Coordinate frame transform",
    )
    raw_origin = raw_transform["origin_xy_at_reference_b"]
    if not isinstance(raw_origin, (list, tuple)) or len(raw_origin) != 2:
        raise ValueError("Coordinate frame XY origin must have two values.")
    transform = BFrameTransform(
        origin_xy_at_reference_b=(
            _require_json_number(raw_origin[0], "Coordinate frame X origin"),
            _require_json_number(raw_origin[1], "Coordinate frame Y origin"),
        ),
        reference_b_deg=_require_json_number(
            raw_transform["reference_b_deg"],
            "Coordinate frame reference B",
        ),
        xy_angle_at_reference_b_deg=_require_json_number(
            raw_transform["xy_angle_at_reference_b_deg"],
            "Coordinate frame XY angle",
        ),
        b_zero_machine_deg=_require_json_number(
            raw_transform["b_zero_machine_deg"],
            "Coordinate frame B origin",
        ),
        z_zero_machine_mm=_require_optional_json_number(
            raw_transform.get("z_zero_machine_mm"),
            "Coordinate frame Z origin",
        ),
        a_zero_machine_mm=_require_optional_json_number(
            raw_transform.get("a_zero_machine_mm"),
            "Coordinate frame A origin",
        ),
    )
    raw_readiness = value["readiness"]
    if not isinstance(raw_readiness, dict):
        raise ValueError("Coordinate frame readiness must be an object.")
    readiness: dict[str, AxisReadiness] = {}
    for axis, raw_state in raw_readiness.items():
        if not isinstance(raw_state, dict):
            raise ValueError(f"Coordinate frame {axis} readiness must be an object.")
        readiness[axis] = AxisReadiness(
            ReadinessStatus(
                _require_json_string(
                    raw_state["status"],
                    f"Coordinate frame {axis} readiness status",
                )
            ),
            _require_json_string(
                raw_state.get("reason", ""),
                f"Coordinate frame {axis} readiness reason",
            ),
        )
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("Coordinate frame metadata must be an object.")
    record = CoordinateFrameRecord(
        frame_id=frame_id,
        kind=kind,
        name=name,
        version=version,
        transform=transform,
        readiness=readiness,
        metadata=metadata,
    )
    semantic_error = record.semantic_validation_error()
    if semantic_error is not None:
        raise ValueError(semantic_error)
    return record


class FilesystemCoordinateFrameBackend:
    """Synchronous backend intended for use only on the store thread."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    def load(self) -> CoordinateFrameDocument:
        if not self._path.exists():
            return CoordinateFrameDocument()
        with self._path.open("r", encoding="utf-8") as handle:
            return CoordinateFrameDocument.from_dict(json.load(handle))

    def save(self, document: CoordinateFrameDocument) -> None:
        payload = json.dumps(
            document.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        _write_atomic(self._path, payload)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class CoordinateFrameLoadResult:
    request_id: int
    document: CoordinateFrameDocument
    runtime_records: tuple[CoordinateFrameRecord, ...] | None = None
    provenance_diagnostics: tuple[object, ...] = ()


@dataclass(frozen=True)
class CoordinateFrameStoreSuccess:
    request_id: int
    operation: str


@dataclass(frozen=True)
class CoordinateFrameStoreFailure:
    request_id: int
    operation: str
    message: str


@dataclass(frozen=True)
class _StoreOperation:
    request_id: int
    operation: str
    document: CoordinateFrameDocument | None = None
    machine_profile_id: str | None = None


@dataclass(frozen=True)
class _Publication:
    kind: str
    value: object


class CoordinateFrameBackend(Protocol):
    def load(self) -> CoordinateFrameDocument: ...

    def save(self, document: CoordinateFrameDocument) -> None: ...


BackendFactory = Callable[[], CoordinateFrameBackend]


class CoordinateFrameStoreWorker(QObject):
    """Creator-thread facade around one coalescing persistence thread."""

    loaded = Signal(object)
    saved = Signal(object)
    failed = Signal(object)
    finished = Signal()
    _publication_posted = Signal(object)
    _finished_posted = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        path: str | os.PathLike[str] | None = None,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        super().__init__(parent)
        if backend_factory is None:
            if path is None:
                raise ValueError("A coordinate frame path or backend factory is required.")
            persisted_path = Path(path)

            def backend_factory() -> FilesystemCoordinateFrameBackend:
                return FilesystemCoordinateFrameBackend(persisted_path)
        self._creator_thread_id = threading.get_ident()
        self._backend_factory = backend_factory
        self._condition = threading.Condition(threading.Lock())
        self._pending: deque[_StoreOperation] = deque()
        self._active = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._drop_publications = threading.Event()
        self._drain_thread: threading.Thread | None = None
        self._publication_posted.connect(
            self._deliver_publication,
            Qt.ConnectionType.QueuedConnection,
        )
        self._finished_posted.connect(
            self.finished.emit,
            Qt.ConnectionType.QueuedConnection,
        )

    @property
    def is_idle(self) -> bool:
        with self._condition:
            return not self._active and not self._pending

    @property
    def drain_thread(self) -> threading.Thread | None:
        return self._drain_thread

    def load(
        self,
        request_id: int,
        *,
        machine_profile_id: str | None = None,
    ) -> None:
        self._submit(
            _StoreOperation(
                int(request_id),
                "load",
                machine_profile_id=machine_profile_id,
            )
        )

    def publish(
        self,
        request_id: int,
        document: CoordinateFrameDocument,
    ) -> None:
        if not isinstance(document, CoordinateFrameDocument):
            raise TypeError("Published value must be a CoordinateFrameDocument.")
        self._submit(_StoreOperation(int(request_id), "save", document))

    def stop(self, timeout_s: float = 1.0) -> None:
        self._require_creator_thread()
        timeout = max(0.0, float(timeout_s))
        deadline = time.monotonic() + timeout
        with self._condition:
            self._stopping = True
            if timeout == 0.0:
                self._drop_publications.set()
            thread = self._thread
            self._condition.notify_all()
            post_finished = thread is None
        if post_finished and not self._drop_publications.is_set():
            self._finished_posted.emit()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                self._drop_publications.set()
                self._start_drain_thread(thread)

    def _start_drain_thread(self, worker_thread: threading.Thread) -> None:
        drain = self._drain_thread
        if drain is not None and drain.is_alive():
            return
        drain = threading.Thread(
            target=worker_thread.join,
            name="coordinate-frame-store-drain",
            daemon=False,
        )
        self._drain_thread = drain
        drain.start()

    def _submit(self, operation: _StoreOperation) -> None:
        self._require_creator_thread()
        with self._condition:
            if self._stopping:
                return
            if (
                operation.operation == "save"
                and self._pending
                and self._pending[-1].operation == "save"
            ):
                self._pending[-1] = operation
            else:
                self._pending.append(operation)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run,
                    name="coordinate-frame-store",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify()

    def _run(self) -> None:
        try:
            backend = self._backend_factory()
            while True:
                operation = self._take_pending()
                if operation is None:
                    return
                try:
                    self._execute(backend, operation)
                except Exception as exc:
                    self._post_publication(
                        "failed",
                        CoordinateFrameStoreFailure(
                            operation.request_id,
                            operation.operation,
                            f"{type(exc).__name__}: {exc}",
                        ),
                    )
                finally:
                    with self._condition:
                        self._active = False
                        self._condition.notify_all()
        finally:
            if not self._drop_publications.is_set():
                self._finished_posted.emit()

    def _take_pending(self) -> _StoreOperation | None:
        with self._condition:
            while not self._pending:
                if self._stopping:
                    return None
                self._condition.wait()
            operation = self._pending.popleft()
            self._active = True
            return operation

    def _execute(
        self,
        backend: CoordinateFrameBackend,
        operation: _StoreOperation,
    ) -> None:
        if operation.operation == "load":
            document = backend.load()
            if operation.machine_profile_id is None:
                result = CoordinateFrameLoadResult(operation.request_id, document)
            else:
                from .provenance import validate_design_frame_provenance

                validation = validate_design_frame_provenance(
                    document.records,
                    operation.machine_profile_id,
                )
                result = CoordinateFrameLoadResult(
                    operation.request_id,
                    document,
                    runtime_records=validation.records,
                    provenance_diagnostics=validation.diagnostics,
                )
            self._post_publication(
                "loaded",
                result,
            )
            return
        if operation.operation == "save":
            if operation.document is None:
                raise ValueError("Save operation has no coordinate frame document.")
            backend.save(operation.document)
            self._post_publication(
                "saved",
                CoordinateFrameStoreSuccess(operation.request_id, "save"),
            )
            return
        raise ValueError(f"Unknown coordinate frame operation {operation.operation!r}.")

    def _post_publication(self, kind: str, value: object) -> None:
        if not self._drop_publications.is_set():
            self._publication_posted.emit(_Publication(kind, value))

    @Slot(object)
    def _deliver_publication(self, publication: _Publication) -> None:
        signal = {
            "loaded": self.loaded,
            "saved": self.saved,
            "failed": self.failed,
        }.get(publication.kind)
        if signal is not None:
            signal.emit(publication.value)

    def _require_creator_thread(self) -> None:
        if threading.get_ident() != self._creator_thread_id:
            raise RuntimeError(
                "Coordinate frame store methods must be called from the creator thread."
            )


__all__ = [
    "COORDINATE_FRAME_DOCUMENT_VERSION",
    "CoordinateFrameDocument",
    "CoordinateFrameLoadResult",
    "CoordinateFrameStoreFailure",
    "CoordinateFrameStoreSuccess",
    "CoordinateFrameStoreWorker",
    "FilesystemCoordinateFrameBackend",
    "FrameLoadDiagnostic",
]
