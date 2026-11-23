"""跨门户业务：登记同步、按授权交付、收件回执、授权核对与停用回收。"""
from __future__ import annotations

import json
import os

from .browser import Portal, PortalError, permitted, row_for, split_consent
from .model import (
    CHANNELS,
    VENDORS,
    Delivery,
    Line,
    StateError,
    StopRequest,
    Store,
    decision_basis,
    new_reference,
    now,
    stamp,
    write_private_append,
)

DELIVERED_STATES = ("candidate", "uncertain", "accepted", "rejected")


class Journal:
    """普通运行日志：只写标识、字段名和状态，不写任何联系方式原值。"""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    def write(self, event: str, **fields) -> None:
        record = {"at": stamp(), "event": event}
        record.update(fields)
        write_private_append(self.path, json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def line_status(delivery: Delivery) -> str:
    states = [line.state for line in delivery.lines]
    if not states:
        return "nothing_to_send"
    if any(state in ("candidate", "uncertain") for state in states):
        return "prepared" if not delivery.uploads else "uncertain"
    accepted = states.count("accepted")
    if accepted == len(states):
        return "delivered"
    if accepted:
        return "partial"
    if any(state == "rejected" for state in states):
        return "declined"
    return "not_delivered"


class Service:
    def __init__(self, store: Store, portal: Portal, journal: Journal | None = None):
        self.store = store
        self.portal = portal
        self.journal = journal or Journal(os.path.join(os.path.dirname(store.path), "handoff.log"))

    # ---------- 登记 ----------
    def sync(self, ids: list[str] | None = None, overwrite: bool = False) -> dict:
        """读取登记门户所有可打开的咨询卡，逐张展开授权抽屉。

        默认会重读已登记但未在列表中出现的咨询号：授权可能在列表之外撤回，
        旧页面仍可核实。页面上完全找不到时记为 missing，不会伪造授权。
        """
        listed = self.portal.card_ids()
        targets = sorted(set(ids or []) | set(self.store.consultations) | set(listed))
        report = {"listed": listed, "read": [], "unchanged": [], "conflicts": [], "missing": []}
        for cid in targets:
            try:
                page = self.portal.read_card(cid)
            except (PortalError, KeyError, ValueError) as exc:
                report["missing"].append({"id": cid, "reason": str(exc)})
                continue
            try:
                _, changed = self.store.record_consultation(
                    cid,
                    page.card.get("visitor", ""),
                    page.card.get("trip", ""),
                    page.card.get("name", ""),
                    page.consent,
                    present=page.present,
                    overwrite_evidence=overwrite,
                )
            except StateError as exc:
                report["conflicts"].append({"id": cid, "reason": str(exc)})
                continue
            (report["read"] if changed else report["unchanged"]).append(cid)
        self.store.save()
        self.journal.write(
            "sync",
            listed=len(listed),
            read=report["read"],
            unchanged=report["unchanged"],
            missing=[item["id"] for item in report["missing"]],
        )
        return report

    # ---------- 交付 ----------
    def _delivered_ids(self, vendor: str) -> set[str]:
        """已在该展商处产生有效交付行、尚未被后续交付接替的咨询号。"""
        return {
            line.cid
            for delivery in self.store.deliveries.values()
            if delivery.vendor == vendor
            for line in delivery.lines
            if line.superseded_by is None and line.state in DELIVERED_STATES
        }

    def pending_consultations(self, vendor: str) -> list[str]:
        """尚未在该展商处有交付行、当前仍获许可的咨询号。"""
        covered = self._delivered_ids(vendor)
        return [
            cid
            for cid in sorted(self.store.consultations)
            if cid not in covered
            and (self.store.consultations[cid].snapshot(vendor) or (None, set()))[1]
        ]

    def prepare(self, vendor: str, ids: list[str] | None = None) -> Delivery | None:
        if vendor not in VENDORS:
            raise StateError("未知展商 " + vendor)
        chosen = list(ids) if ids is not None else self.pending_consultations(vendor)
        if not chosen:
            return None
        covered = self._delivered_ids(vendor)
        for cid in chosen:
            if cid not in self.store.consultations:
                raise StateError("没有登记咨询 " + cid)
            snapshot = self.store.consultations[cid].snapshot(vendor)
            if snapshot is None or not snapshot[1]:
                raise StateError("咨询 " + cid + " 对展商 " + vendor + " 没有可用授权")
            if cid in covered:
                raise StateError("咨询 " + cid + " 已有该展商的交付行；被拒收请联系后使用 retry 开新引用")
        delivery = self.store.create_delivery(vendor, chosen)
        for line, cid in zip(delivery.lines, chosen):
            version, fields = self.store.consultations[cid].snapshot(vendor)
            line.consent_version = version
            line.fields_sent = sorted(fields)
        self.store.save()
        self.journal.write("prepare", vendor=vendor, reference=delivery.reference, ids=chosen)
        return delivery

    def send(self, vendor: str | None = None, reference: str | None = None, lose_response: bool = False) -> dict:
        """上传或继续交付。

        每次都先按同一引用查收件历史；已经入账的引用只采纳结果，不再提交。
        只有仍有未定论行的交付才会继续，已定论的 partial 不会被重投。
        """
        if reference:
            targets = [self.store.delivery_for(reference)]
        else:
            targets = self.store.open_deliveries(vendor)
        report = {"attempted": [], "adopted": [], "skipped": []}
        for delivery in targets:
            if delivery.status not in ("prepared", "partial", "uncertain"):
                report["skipped"].append({"reference": delivery.reference, "status": delivery.status})
                continue
            adopted = self._attempt(delivery, lose_response=lose_response)
            bucket = "adopted" if adopted else "attempted"
            report[bucket].append({"reference": delivery.reference, "status": delivery.status})
        self.store.save()
        return report

    def _attempt(self, delivery: Delivery, lose_response: bool = False) -> bool:
        existing = self.portal.history(delivery.vendor).get(delivery.reference)
        if existing is not None:
            self._adopt(delivery, existing)
            return True
        rows = []
        for line in delivery.lines:
            if line.state not in ("candidate", "uncertain"):
                continue
            page = self.portal.read_card(line.cid)
            allowed = permitted(page.consent, delivery.vendor)
            row = row_for(page.card, page.consent, delivery.vendor, set(line.fields_sent or [])) if allowed else None
            if row is None:
                line.state = "withheld"
                line.note = "发送前复核：该展商已无可用渠道"
                line.decided_at = now()
                continue
            line.fields_sent = sorted(channel for channel in CHANNELS if channel in row)
            line.consent_version = split_consent(page.consent)[1]
            rows.append(row)
        if not rows:
            delivery.status = "nothing_to_send"
            delivery.updated_at = now()
            self.journal.write("send_empty", vendor=delivery.vendor, reference=delivery.reference)
            return False
        try:
            result = self.portal.upload(delivery.vendor, delivery.reference, rows, lose_response=lose_response)
        except PortalError as exc:
            self._mark_uncertain(delivery, "提交结果未知：" + str(exc), None, len(rows))
            return False
        delivery.uploads.append(
            {"reference": delivery.reference, "status": result["status"], "at": stamp(), "rows": len(rows)}
        )
        if result["item"] is not None:
            self._adopt(delivery, result["item"])
            return False
        self._mark_uncertain(delivery, "确认页缺失；保留同一引用，稍后重查", result["status"], len(rows))
        return False

    def _mark_uncertain(self, delivery: Delivery, note: str, status, rows: int) -> None:
        for line in delivery.lines:
            if line.state in ("candidate", "uncertain"):
                line.state = "uncertain"
                line.note = note
        delivery.status = "uncertain"
        delivery.updated_at = now()
        if not delivery.uploads or delivery.uploads[-1].get("status") != status:
            delivery.uploads.append({"reference": delivery.reference, "status": status, "at": stamp(), "rows": rows})
        self.journal.write("send_unconfirmed", vendor=delivery.vendor, reference=delivery.reference, http=status)

    def _adopt(self, delivery: Delivery, receipt: dict) -> None:
        """把历史页中的实际行结果写回交付；已定论的行不会被再次投递覆盖。"""
        states = {row.get("id"): row.get("state") for row in receipt.get("rows") or []}
        for line in delivery.lines:
            if line.state in ("accepted", "rejected"):
                continue
            state = states.get(line.cid)
            if state == "accepted":
                line.state = "accepted"
                line.receipt = receipt.get("receipt")
                line.decided_at = now()
                line.note = None
            elif state == "rejected":
                line.state = "rejected"
                line.receipt = receipt.get("receipt")
                line.decided_at = now()
                line.note = "收件方拒收；如需重试须开新的交付引用"

        delivery.status = line_status(delivery)
        delivery.updated_at = now()
        self.journal.write(
            "receipt",
            vendor=delivery.vendor,
            reference=delivery.reference,
            receipt=receipt.get("receipt"),
            states=[{"id": line.cid, "state": line.state} for line in delivery.lines],
        )

    def retry_rejected(self, reference: str) -> Delivery:
        """为拒收行开新的交付引用；旧引用内容不变，只在行上记录接替者。

        重试前重新读取授权：已经撤回或不再覆盖的渠道不会被带进新引用。
        """
        old = self.store.delivery_for(reference)
        pending = [line for line in old.lines if line.state == "rejected" and line.superseded_by is None]
        if not pending:
            raise StateError("交付 " + reference + " 没有可重试的拒收行")
        prepared = []
        for line in pending:
            page = self.portal.read_card(line.cid)
            allowed = permitted(page.consent, old.vendor) & set(line.fields_sent or [])
            if not allowed:
                continue
            prepared.append((line, sorted(allowed), split_consent(page.consent)[1]))
        if not prepared:
            raise StateError("被拒收的咨询当前对展商 " + old.vendor + " 没有可用授权；应先核对授权并回收")
        while True:
            fresh_reference = new_reference("DLV")
            if fresh_reference not in self.store.deliveries:
                break
        fresh = Delivery(reference=fresh_reference, vendor=old.vendor, origin=old.reference)
        for line, fields, version in prepared:
            fresh.lines.append(Line(cid=line.cid, fields_sent=fields, consent_version=version))
            line.superseded_by = fresh.reference
        old.updated_at = now()
        self.store.deliveries[fresh.reference] = fresh
        self.store.save()
        self.journal.write(
            "retry",
            vendor=old.vendor,
            origin=old.reference,
            reference=fresh.reference,
            ids=[line.cid for line in fresh.lines],
        )
        return fresh

    # ---------- 授权核对与回收 ----------
    def verify(self, ids: list[str] | None = None) -> dict:
        """重新读取授权，为已交付但当前不再获准的字段登记停用申请。"""
        report = {"sync": self.sync(ids=ids), "exposure": [], "created": [], "duplicates": []}
        for vendor in VENDORS:
            for entry in self.exposures(vendor):
                report["exposure"].append(public_exposure(entry))
                if not entry["uncovered"]:
                    continue
                duplicate = self._pending_duplicate(entry)
                if duplicate is not None:
                    report["duplicates"].append(duplicate)
                    continue
                request = self.store.create_stop(vendor, entry["delivery"], entry["cid"], entry["uncovered"])
                request.baseline = {
                    "delivered": entry["delivered"],
                    "permitted_at_check": entry["allowed_now"],
                    "consent_version": entry["consent_version"],
                }
                report["created"].append(request.public())
                self.journal.write(
                    "stop_requested",
                    vendor=vendor,
                    reference=request.reference,
                    delivery=entry["delivery"],
                    cid=entry["cid"],
                    fields=entry["uncovered"],
                )
        self.store.save()
        return report

    def exposures(self, vendor: str) -> list[dict]:
        """该展商已接收、但当前授权不再覆盖的字段，按原交付引用与咨询号分组。

        只统计实际被该展商接收并带出字段的行，且不看另一家展商的交付。
        """
        groups: dict[tuple[str, str], set[str]] = {}
        for delivery in self.store.deliveries.values():
            if delivery.vendor != vendor:
                continue
            for line in delivery.lines:
                if line.state != "accepted" or not line.fields_sent:
                    continue
                groups.setdefault((delivery.reference, line.cid), set()).update(line.fields_sent)
        result = []
        for (reference, cid), delivered in sorted(groups.items()):
            consultation = self.store.consultations.get(cid)
            snapshot = consultation.snapshot(vendor) if consultation else None
            allowed = set(snapshot[1]) if snapshot else set()
            exposed = sorted((delivered & set(CHANNELS)) - allowed)
            if not exposed:
                continue
            covered = set()
            for request in self.store.stops.values():
                if request.vendor == vendor and request.delivery == reference and request.cid == cid:
                    covered.update(request.fields)
            result.append(
                {
                    "vendor": vendor,
                    "cid": cid,
                    "delivery": reference,
                    "delivered": sorted(delivered),
                    "allowed_now": sorted(allowed),
                    "consent_version": snapshot[0] if snapshot else None,
                    "exposed": exposed,
                    "uncovered": sorted(set(exposed) - covered),
                    "stop_states": sorted(
                        request.state
                        for request in self.store.stops.values()
                        if request.vendor == vendor and request.delivery == reference and request.cid == cid
                    ),
                }
            )
        return result

    def _pending_duplicate(self, entry: dict) -> dict | None:
        for request in self.store.stops.values():
            if (
                request.vendor == entry["vendor"]
                and request.delivery == entry["delivery"]
                and request.cid == entry["cid"]
                and request.state in ("requested", "pending")
            ):
                remaining = sorted(set(entry["uncovered"]) - set(request.fields))
                if remaining:
                    return {"reference": request.reference, "extra_fields": remaining, "cid": entry["cid"]}
                return None
        return None

    def submit_stops(self, vendor: str | None = None) -> dict:
        report = {"submitted": [], "confirmed": [], "pending": [], "failed": []}
        targets = self.store.unfiled_stops(vendor) if vendor else [
            request for request in self.store.stops.values() if request.state in ("requested", "pending")
        ]
        for request in targets:
            receipt = None
            try:
                receipt = self.portal.stop(
                    request.vendor, request.reference, request.delivery, request.cid, request.fields
                )
            except PortalError as exc:
                report["failed"].append({"reference": request.reference, "reason": str(exc)})
                continue
            if receipt is None:
                report["failed"].append({"reference": request.reference, "reason": "回执页没有该引用"})
                continue
            request.submitted_at = request.submitted_at or now()
            request.state = "confirmed" if receipt.get("state") == "confirmed" else "pending"
            request.updated_at = now()
            report["submitted"].append(request.reference)
            self.journal.write("stop_filed", vendor=request.vendor, reference=request.reference)
        self.store.save()
        report["refresh"] = self.refresh_stops(vendor)
        report["confirmed"] = report["refresh"]["confirmed"]
        report["pending"] = report["refresh"]["pending"]
        return report

    def refresh_stops(self, vendor: str | None = None) -> dict:
        """重新读取停用回执页；pending 只表示对方收到请求，confirmed 才是已停用。"""
        report = {"checked": [], "pending": [], "confirmed": []}
        for name in ([vendor] if vendor else list(VENDORS)):
            try:
                receipts = self.portal.stop_receipts(name)
            except PortalError:
                continue
            for request in self.store.stops.values():
                if request.vendor != name:
                    continue
                receipt = receipts.get(request.reference)
                if receipt is None:
                    continue
                report["checked"].append(request.reference)
                request.state = "confirmed" if receipt.get("state") == "confirmed" else "pending"
                request.updated_at = now()
                report["confirmed" if request.state == "confirmed" else "pending"].append(request.reference)
                self.journal.write("stop_state", vendor=name, reference=request.reference, state=request.state)
        self.store.save()
        return report

    # ---------- 查询 ----------
    def status(self, vendor: str | None = None) -> dict:
        report = {}
        for name in ([vendor] if vendor else list(VENDORS)):
            deliveries = [
                {
                    "reference": item.reference,
                    "origin": item.origin,
                    "status": item.status,
                    "created_at": item.created_at,
                    "uploads": item.uploads,
                    "lines": [line.public() for line in item.lines],
                }
                for item in sorted(self.store.deliveries.values(), key=lambda value: value.created_at)
                if item.vendor == name
            ]
            stops = [
                {
                    "reference": request.reference,
                    "delivery": request.delivery,
                    "cid": request.cid,
                    "fields": request.fields,
                    "state": request.state,
                    "baseline": request.baseline,
                }
                for request in sorted(self.store.stops.values(), key=lambda value: value.created_at)
                if request.vendor == name
            ]
            report[name] = {"deliveries": deliveries, "stops": stops}
        return report

    def trace(self, cid: str) -> dict:
        consultation = self.store.consultations.get(cid)
        if consultation is None:
            raise StateError("没有登记咨询 " + cid)
        receipts = []
        for delivery in sorted(self.store.deliveries.values(), key=lambda value: value.created_at):
            for line in delivery.lines:
                if line.cid != cid:
                    continue
                receipts.append(
                    {
                        "vendor": delivery.vendor,
                        "delivery": delivery.reference,
                        "origin": delivery.origin,
                        "delivery_status": delivery.status,
                        "line_state": line.state,
                        "receipt": line.receipt,
                        "fields_delivered": line.fields_sent,
                        "consent_version_sent": line.consent_version,
                        "superseded_by": line.superseded_by,
                        "note": line.note,
                    }
                )
        stops = [
            request.public() for request in self.store.stops.values() if request.cid == cid
        ]
        exposure = [
            public_exposure(entry)
            for vendor in VENDORS
            for entry in self.exposures(vendor)
            if entry["cid"] == cid
        ]
        return {
            "id": cid,
            "recorded": {
                "visitor": consultation.recorded_visitor,
                "trip": consultation.recorded_trip,
                "name": consultation.recorded_name,
                "page_shows_channels": consultation.present,
            },
            "consent_history": [
                {
                    "version": entry["consent_version"],
                    "captured_at": entry["captured_at"],
                    "granted": entry["granted"],
                    "evidence_digest": entry["digest"],
                }
                for entry in consultation.evidence
            ],
            "decisions": [decision_basis(consultation, vendor) for vendor in VENDORS],
            "receipts": receipts,
            "stops": stops,
            "exposure": exposure,
        }

    def risk(self) -> dict:
        entries = []
        for vendor in VENDORS:
            for item in self.exposures(vendor):
                states = set(item["stop_states"])
                if not item["uncovered"] and "confirmed" in states:
                    state = "confirmed"
                elif "pending" in states:
                    state = "pending"
                elif "requested" in states:
                    state = "requested"
                else:
                    state = "none"
                entries.append(
                    {
                        "vendor": vendor,
                        "cid": item["cid"],
                        "delivery": item["delivery"],
                        "exposed_fields": item["exposed"],
                        "uncovered_fields": item["uncovered"],
                        "stop_state": state,
                        "confirmed_stopped": state == "confirmed",
                        "action_needed": state != "confirmed",
                    }
                )
        uncertain = [
            {"vendor": item.vendor, "delivery": item.reference, "origin": item.origin}
            for item in self.store.deliveries.values()
            if item.status == "uncertain"
        ]
        return {
            "exposure": entries,
            "action_needed": [entry for entry in entries if entry["action_needed"]],
            "uncertain_deliveries": uncertain,
            "summary": {
                "exposure": len(entries),
                "not_confirmed_stopped": sum(1 for entry in entries if entry["action_needed"]),
                "uncertain_deliveries": len(uncertain),
            },
        }

    # ---------- 受限证据 ----------
    def reveal_evidence(self, digest: str, reason: str, cid: str | None = None) -> dict:
        """显式读取一条授权原文；业务查询默认只用版本、字段名与摘要。"""
        value = self.store.read_evidence(digest)
        self.store.audit_evidence(digest, reason, cid)
        self.journal.write("evidence_read", digest=digest, reason=reason)
        return value


def public_exposure(entry: dict) -> dict:
    return {
        "vendor": entry["vendor"],
        "cid": entry["cid"],
        "delivery": entry["delivery"],
        "delivered": entry["delivered"],
        "allowed_now": entry["allowed_now"],
        "consent_version": entry["consent_version"],
        "exposed": entry["exposed"],
        "uncovered": entry["uncovered"],
        "stop_states": entry["stop_states"],
    }
