"""业务持久状态的纯数据模型，不负责页面交互。

状态文件只保存标识、授权版本、获准字段名和回执，不保存任何联系方式原值。
授权抽屉的原始内容以内容摘要为名存放在受限证据目录，供审计显式读取。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

CHANNELS = ("email", "phone")
VENDORS = ("A", "B")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def now() -> float:
    return time.time()


def stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def new_reference(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex[:16]


def write_private(path: str, data: bytes) -> None:
    """以 0600 创建或截断文件，避免默认 umask 留下可读副本。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class StateError(RuntimeError):
    """状态与页面事实不兼容时抛出；调用方应停止并保留现场。"""


@dataclass
class Consultation:
    cid: str
    recorded_visitor: str
    recorded_trip: str
    recorded_name: str
    consent_versions: list[str] = field(default_factory=list)
    present: dict[str, bool] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    first_seen: float = field(default_factory=now)

    def current_consent(self, vendor: str) -> dict | None:
        """最新授权版本决定一切：该版本缺少展商或列表为空都表示没有许可。"""
        if not self.evidence:
            return None
        latest = self.evidence[-1]
        if vendor not in latest["granted"]:
            return None
        return latest

    def snapshot(self, vendor: str) -> tuple[str, set[str]] | None:
        item = self.current_consent(vendor)
        if item is None:
            return None
        return item["consent_version"], set(item["granted"][vendor])

    def public(self) -> dict:
        return {
            "id": self.cid,
            "recorded_visitor": self.recorded_visitor,
            "recorded_trip": self.recorded_trip,
            "recorded_name": self.recorded_name,
            "consent_versions": self.consent_versions,
            "present": self.present,
            "evidence": self.evidence,
            "first_seen": self.first_seen,
        }

    @classmethod
    def from_public(cls, value: dict) -> "Consultation":
        return cls(
            cid=value["id"],
            recorded_visitor=value["recorded_visitor"],
            recorded_trip=value["recorded_trip"],
            recorded_name=value["recorded_name"],
            consent_versions=list(value["consent_versions"]),
            present={key: bool(flag) for key, flag in value["present"].items()},
            evidence=list(value["evidence"]),
            first_seen=value.get("first_seen", value.get("received_at", now())),
        )


@dataclass
class Line:
    cid: str
    state: str = "candidate"  # candidate | accepted | rejected | withheld | uncertain
    receipt: str | None = None
    note: str | None = None
    fields_sent: list[str] | None = None
    consent_version: str | None = None
    decided_at: float | None = None
    superseded_by: str | None = None

    def public(self) -> dict:
        return {
            "cid": self.cid,
            "state": self.state,
            "receipt": self.receipt,
            "note": self.note,
            "fields_sent": self.fields_sent,
            "consent_version": self.consent_version,
            "decided_at": self.decided_at,
            "superseded_by": self.superseded_by,
        }

    @classmethod
    def from_public(cls, value: dict) -> "Line":
        value = dict(value)
        value.setdefault("consent_version", None)
        value.setdefault("superseded_by", None)
        return cls(**value)


@dataclass
class Delivery:
    reference: str
    vendor: str
    lines: list[Line] = field(default_factory=list)
    status: str = "prepared"  # prepared | partial | delivered | declined | uncertain | nothing_to_send
    uploads: list[dict] = field(default_factory=list)
    origin: str | None = None
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)

    def public(self) -> dict:
        return {
            "reference": self.reference,
            "vendor": self.vendor,
            "status": self.status,
            "lines": [line.public() for line in self.lines],
            "uploads": self.uploads,
            "origin": self.origin,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_public(cls, value: dict) -> "Delivery":
        item = cls(
            reference=value["reference"],
            vendor=value["vendor"],
            status=value["status"],
            uploads=list(value["uploads"]),
            origin=value.get("origin"),
            created_at=value["created_at"],
            updated_at=value["updated_at"],
        )
        item.lines = [Line.from_public(line) for line in value["lines"]]
        return item


@dataclass
class StopRequest:
    reference: str
    vendor: str
    delivery: str
    cid: str
    fields: list[str]
    state: str = "requested"  # requested | pending | confirmed
    submitted_at: float | None = None
    baseline: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=now)
    updated_at: float = field(default_factory=now)

    def public(self) -> dict:
        return {
            "reference": self.reference,
            "vendor": self.vendor,
            "delivery": self.delivery,
            "cid": self.cid,
            "fields": self.fields,
            "state": self.state,
            "submitted_at": self.submitted_at,
            "baseline": self.baseline,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_public(cls, value: dict) -> "StopRequest":
        value = dict(value)
        value.setdefault("submitted_at", None)
        value.setdefault("baseline", {})
        return cls(**value)


class Store:
    """一个 JSON 状态文件加一个受限证据目录。

    证据目录默认位于状态文件旁的 restricted/，可用 HANDOFF_EVIDENCE_DIR
    或构造参数指向受限卷；目录权限 0700，文件 0600。授权原文不进入状态
    文件、业务查询输出或普通运行日志。
    """

    FORMAT = 1

    def __init__(self, path: str | os.PathLike, evidence_dir: str | os.PathLike | None = None):
        self.path = os.path.abspath(path)
        base = os.path.dirname(self.path)
        self.evidence_dir = os.path.abspath(
            evidence_dir or os.environ.get("HANDOFF_EVIDENCE_DIR") or os.path.join(base, "restricted")
        )
        self.audit_path = os.path.join(os.path.dirname(self.evidence_dir), "evidence-access.log")
        os.makedirs(base, exist_ok=True)
        os.makedirs(self.evidence_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.evidence_dir, 0o700)
        except OSError:
            pass
        self.consultations: dict[str, Consultation] = {}
        self.deliveries: dict[str, Delivery] = {}
        self.stops: dict[str, StopRequest] = {}
        self._lock_handle = None
        self.load()

    # ---------- 进程间互斥 ----------
    def __enter__(self) -> "Store":
        self._lock_handle = open(self.path + ".lock", "a+")
        fcntl.flock(self._lock_handle, fcntl.LOCK_EX)
        self.load()
        return self

    def __exit__(self, *exc) -> None:
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    # ---------- 持久化 ----------
    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if raw.get("format") != self.FORMAT:
            raise StateError("状态文件格式不是 " + str(self.FORMAT))
        self.consultations = {
            key: Consultation.from_public(value) for key, value in raw.get("consultations", {}).items()
        }
        self.deliveries = {key: Delivery.from_public(value) for key, value in raw.get("deliveries", {}).items()}
        self.stops = {key: StopRequest.from_public(value) for key, value in raw.get("stops", {}).items()}

    def save(self) -> None:
        raw = {
            "format": self.FORMAT,
            "consultations": {key: item.public() for key, item in self.consultations.items()},
            "deliveries": {key: item.public() for key, item in self.deliveries.items()},
            "stops": {key: item.public() for key, item in self.stops.items()},
        }
        directory = os.path.dirname(self.path)
        handle, temporary = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(raw, stream, ensure_ascii=False, indent=1, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # ---------- 受限证据 ----------
    def put_evidence(self, consent: dict) -> str:
        blob = canonical(consent).encode("utf-8")
        digest = hashlib.sha256(blob).hexdigest()
        target = os.path.join(self.evidence_dir, digest + ".json")
        if not os.path.exists(target):
            handle, temporary = tempfile.mkstemp(dir=self.evidence_dir, prefix=".evidence-")
            try:
                os.fchmod(handle, 0o600)
                with os.fdopen(handle, "wb") as stream:
                    stream.write(blob)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return digest

    def read_evidence(self, digest: str) -> dict:
        if not isinstance(digest, str) or not HEX64.match(digest):
            raise StateError("证据摘要格式不正确")
        target = os.path.join(self.evidence_dir, digest + ".json")
        if not os.path.exists(target):
            raise StateError("没有该授权证据")
        with open(target, encoding="utf-8") as handle:
            return json.load(handle)

    def audit_evidence(self, digest: str, reason: str, cid: str | None = None) -> None:
        record = {"at": stamp(), "digest": digest, "reason": reason}
        if cid:
            record["cid"] = cid
        write_private_append(self.audit_path, canonical(record) + "\n")

    # ---------- 授权与咨询 ----------
    def record_consultation(
        self,
        cid: str,
        visitor: str,
        trip: str,
        name: str,
        consent: dict,
        present: dict[str, bool] | None = None,
        overwrite_evidence: bool = False,
    ) -> tuple[Consultation, bool]:
        """登记一张咨询卡，返回 (consultation, 是否新增证据)。

        只有打开授权抽屉读到的内容才应传进来。present 只记录页面上是否仍
        显示某个渠道的原值，原值本身不写入状态。版本号变化视为新证据；
        版本号相同但内容不同视为冲突，需要操作人员明确覆盖。
        """
        digest = self.put_evidence(consent)
        record = self.consultations.get(cid)
        if record is None:
            record = Consultation(cid, visitor, trip, name)
            self.consultations[cid] = record
        elif (record.recorded_visitor, record.recorded_trip, record.recorded_name) != (visitor, trip, name):
            raise StateError("咨询 " + cid + " 的页面身份与已登记内容不同，拒绝合并")
        if present:
            record.present.update({channel: bool(flag) for channel, flag in present.items()})
        for seen in record.evidence:
            if seen["digest"] == digest:
                return record, False
        version = consent.get("version") if isinstance(consent.get("version"), str) else None
        granted = {
            vendor: sorted(set(consent[vendor]))
            for vendor in VENDORS
            if isinstance(consent.get(vendor), list)
        }
        if record.evidence and not overwrite_evidence:
            latest = record.evidence[-1]
            if latest["consent_version"] == version and latest["granted"] != granted:
                raise StateError(
                    "咨询 " + cid + " 的授权版本 " + str(version) + " 内容与已存证据不同；"
                    "确认页面后可用 --overwrite-evidence 重新登记"
                )
        entry = {
            "consent_version": version,
            "digest": digest,
            "captured_at": stamp(),
            "granted": granted,
        }
        if record.evidence:
            previous = record.evidence[-1]
            if previous["consent_version"] == version and previous["granted"] == granted:
                return record, False
        record.evidence.append(entry)
        record.consent_versions.append(version or "?")
        return record, True

    # ---------- 交付 ----------
    def delivery_for(self, reference: str) -> Delivery:
        try:
            return self.deliveries[reference]
        except KeyError:
            raise StateError("没有交付引用 " + reference) from None

    def create_delivery(self, vendor: str, ids: list[str]) -> Delivery:
        delivery = Delivery(reference=new_reference("DLV"), vendor=vendor)
        delivery.lines = [Line(cid) for cid in ids]
        self.deliveries[delivery.reference] = delivery
        self.save()
        return delivery

    def open_deliveries(self, vendor: str | None = None) -> list[Delivery]:
        """仍需继续处理的交付：未上传、结果未知，或仍有未定论的行。"""
        return sorted(
            (
                item
                for item in self.deliveries.values()
                if (vendor is None or item.vendor == vendor)
                and (
                    item.status in ("prepared", "uncertain")
                    or any(line.state in ("candidate", "uncertain") for line in item.lines)
                )
            ),
            key=lambda item: item.created_at,
        )

    # ---------- 停用回收 ----------
    def create_stop(self, vendor: str, delivery: str, cid: str, fields: list[str]) -> StopRequest:
        request = StopRequest(new_reference("STP"), vendor, delivery, cid, sorted(set(fields)))
        self.stops[request.reference] = request
        self.save()
        return request

    def unfiled_stops(self, vendor: str | None = None) -> list[StopRequest]:
        """已登记、尚未向展商提交的停用申请。"""
        return sorted(
            (
                item
                for item in self.stops.values()
                if item.state == "requested" and (vendor is None or item.vendor == vendor)
            ),
            key=lambda item: item.created_at,
        )


def write_private_append(path: str, text: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(handle, "a", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def decision_basis(consultation: Consultation, vendor: str) -> dict:
    """给业务查询的解释：只用版本、字段名与摘要说明许可，不带原值。"""
    item = consultation.current_consent(vendor)
    if item is None:
        return {
            "vendor": vendor,
            "consent_version": None,
            "decision": "no_consent_recorded",
            "allowed_channels": [],
            "evidence_digest": None,
            "captured_at": None,
        }
    allowed = sorted(item["granted"][vendor])
    return {
        "vendor": vendor,
        "consent_version": item["consent_version"],
        "decision": "may_contact" if allowed else "no_channel_consented",
        "allowed_channels": allowed,
        "evidence_digest": item["digest"],
        "captured_at": item["captured_at"],
    }
