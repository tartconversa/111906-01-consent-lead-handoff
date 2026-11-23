"""使用 Playwright 实际驱动 Chromium 的端到端检查。

每个检查都在独立临时目录启动来源服务，通过页面完成登记、交付、回执、
授权核对与回收，并核对业务文件、普通日志和受限证据的边界。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from source.app import App  # noqa: E402
from source.common import serve  # noqa: E402

from handoff.browser import Portal  # noqa: E402
from handoff.model import StateError, Store  # noqa: E402
from handoff.service import Journal, Service  # noqa: E402


class Harness(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.server = serve(self.app, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        from playwright.sync_api import sync_playwright

        self.runtime = sync_playwright().start()
        self.browser = self.runtime.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        self.context = self.browser.new_context(accept_downloads=True)
        self.addCleanup(self._close)

    def _close(self):
        self.context.close()
        self.browser.close()
        self.runtime.stop()
        self.server.shutdown()
        self.server.server_close()

    def service(self, store=None):
        store = store or Store(self.root / "state.json")
        portal = Portal(self.context.new_page(), self.base)
        return Service(store, portal, Journal(self.root / "handoff.log"))

    def stage(self, number):
        response = self.context.request.post(
            self.base + "/__control", data={"stage": number}, headers={"X-Operator": "local-operator"}
        )
        self.assertEqual(response.status, 200)

    def remote(self):
        response = self.context.request.get(
            self.base + "/__inspect", headers={"X-Operator": "local-operator"}
        )
        self.assertEqual(response.status, 200)
        return response.json()

    def read_state(self) -> str:
        return (self.root / "state.json").read_text(encoding="utf-8")

    def read_journal(self) -> str:
        return (self.root / "handoff.log").read_text(encoding="utf-8")

    def evidence_files(self) -> list[Path]:
        return sorted((self.root / "restricted").iterdir())


class Delivery(Harness):
    def test_rows_only_carry_consented_channels(self):
        service = self.service()
        service.sync(None, overwrite=False)
        delivery = service.prepare("A")
        self.assertIsNotNone(delivery)
        self.assertEqual(set(delivery.lines[0].fields_sent), {"email", "phone"})
        service.send("A", delivery.reference)
        uploaded = self.remote()["remote"]["delivery-A"][delivery.reference]["payload"]
        by_id = {row["id"]: row for row in uploaded}
        self.assertEqual(by_id["L-101"]["consent_version"], "c1")
        self.assertEqual(by_id["L-101"]["phone"], "000-101")
        self.assertNotIn("L-103", by_id)  # B 的授权，不能进 A 的名单
        self.assertEqual(by_id["L-102"], {
            "id": "L-102",
            "visitor": "V-2",
            "trip": "暑假",
            "consent_version": "c1",
            "email": "family@example.test",
        })

    def test_partial_acceptance_and_no_duplicate_delivery(self):
        service = self.service()
        service.sync(None)
        delivery = service.prepare("A")
        service.send("A", delivery.reference)
        states = {line.cid: line.state for line in delivery.lines}
        self.assertEqual(states, {"L-101": "accepted", "L-102": "rejected"})
        self.assertEqual(delivery.status, "partial")
        service.send("A", delivery.reference)  # 再次继续不会重投
        uploads = self.remote()["remote"]["delivery-A"]
        self.assertEqual(list(uploads), [delivery.reference])
        self.assertEqual(
            [row["state"] for row in uploads[delivery.reference]["rows"]],
            ["accepted", "rejected"],
        )

    def test_unconsented_channel_value_never_enters_delivery(self):
        service = self.service()
        service.sync(None)
        delivery = service.prepare("B")  # B 只获准 L-101 的 email
        service.send("B", delivery.reference)
        uploaded = self.remote()["remote"]["delivery-B"][delivery.reference]["payload"]
        row = next(item for item in uploaded if item["id"] == "L-101")
        self.assertEqual(row, {
            "id": "L-101",
            "visitor": "V-1",
            "trip": "春节",
            "consent_version": "c1",
            "email": "family@example.test",
        })
        serialized = json.dumps(row, ensure_ascii=False)
        self.assertNotIn("000-101", serialized)
        self.assertNotIn("phone", row)
        by_id = {item["id"]: item for item in uploaded}
        # L-103 获准 B 使用 phone，只有这一行的电话值可以出现
        self.assertEqual(by_id["L-103"]["phone"], "000-101")
        self.assertNotIn("000-102", json.dumps(uploaded, ensure_ascii=False))

    def test_shared_email_and_two_trips_stay_separate_rows(self):
        service = self.service()
        service.sync(None)
        delivery = service.prepare("A")
        service.send("A", delivery.reference)
        uploaded = self.remote()["remote"]["delivery-A"][delivery.reference]["payload"]
        self.assertEqual([row["id"] for row in uploaded], ["L-101", "L-102"])
        self.assertEqual(uploaded[0]["visitor"], "V-1")
        self.assertEqual(uploaded[1]["visitor"], "V-2")
        self.assertEqual(uploaded[0]["email"], uploaded[1]["email"])

    def test_missing_confirmation_is_reconciled_by_same_reference(self):
        service = self.service()
        service.sync(None)
        delivery = service.prepare("B")
        result = service.send("B", delivery.reference, lose_response=True)
        self.assertEqual(result["attempted"][0]["status"], "delivered")
        self.assertEqual(delivery.uploads[0]["status"], 503)
        self.assertEqual({line.state for line in delivery.lines}, {"accepted"})
        remote = self.remote()["remote"]["delivery-B"]
        self.assertEqual(list(remote), [delivery.reference])
        service.send("B")  # 已定论的交付不再上传
        self.assertEqual(len(self.remote()["remote"]["delivery-B"]), 1)

    def test_rejected_row_retried_under_new_reference(self):
        service = self.service()
        service.sync(None)
        delivery = service.prepare("A")
        service.send("A", delivery.reference)
        self.stage(1)  # L-102 此时可接受
        fresh = service.retry_rejected(delivery.reference)
        self.assertEqual(fresh.reference != delivery.reference, True)
        self.assertEqual([line.cid for line in fresh.lines], ["L-102"])
        service.send("A", fresh.reference)
        self.assertEqual(fresh.lines[0].state, "accepted")
        old = service.store.delivery_for(delivery.reference)
        self.assertEqual({line.state for line in old.lines}, {"accepted", "rejected"})
        self.assertEqual(old.lines[1].superseded_by, fresh.reference)
        stored = self.remote()["remote"]["delivery-A"]
        self.assertEqual(len(stored), 2)

    def test_retry_respects_current_consent(self):
        service = self.service()
        service.sync(None)
        delivery = service.prepare("B")
        service.send("B", delivery.reference)
        self.assertEqual({line.state for line in delivery.lines}, {"accepted"})  # B 没有拒收行
        self.stage(1)  # L-103 对 B 的许可被撤掉；没有任何行可重试
        with self.assertRaises(StateError):
            service.retry_rejected(delivery.reference)


class Privacy(Harness):
    def test_state_and_journal_hold_no_contact_values(self):
        service = self.service()
        service.sync(None)
        for vendor in ("A", "B"):
            delivery = service.prepare(vendor)
            if delivery:
                service.send(vendor, delivery.reference)
        service.verify(None)
        combined = self.read_state() + self.read_journal()
        for value in ("family@example.test", "000-101", "000-102"):
            self.assertNotIn(value, combined)
        self.assertIn("L-101", combined)

    def test_evidence_is_restricted_and_audited(self):
        service = self.service()
        service.sync(None)
        files = self.evidence_files()
        self.assertEqual(len(files), 3)
        for item in files:
            self.assertEqual(item.stat().st_mode & 0o777, 0o600)
            self.assertEqual(item.parent.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("family@example.test", item.read_text(encoding="utf-8"))
        digest = service.store.consultations["L-101"].evidence[0]["digest"]
        target = self.root / "restricted-copy.json"
        value = service.reveal_evidence(digest, "核对签署原文", "L-101")
        self.assertEqual(value["A"], ["email", "phone"])
        self.assertFalse(target.exists())
        audit = (self.root / "evidence-access.log").read_text(encoding="utf-8")
        self.assertIn(digest, audit)
        self.assertIn("核对签署原文", audit)
        self.assertNotIn("family@example.test", audit)

    def test_trace_explains_decisions_without_masking_bypass(self):
        service = self.service()
        service.sync(None)
        trace = service.trace("L-101")
        self.assertEqual([item["version"] for item in trace["consent_history"]], ["c1"])
        self.assertEqual(trace["consent_history"][0]["granted"], {"A": ["email", "phone"], "B": ["email"]})
        self.assertEqual(trace["decisions"][0]["allowed_channels"], ["email", "phone"])
        self.assertNotIn("family@example.test", json.dumps(trace, ensure_ascii=False))
        self.assertNotIn("000-101", json.dumps(trace, ensure_ascii=False))

    def test_trace_after_reduction_shows_old_and_new_versions(self):
        service = self.service()
        service.sync(None)
        delivery = service.prepare("A")
        service.send("A", delivery.reference)
        self.stage(1)
        service.verify(None)
        trace = service.trace("L-101")
        self.assertEqual([item["version"] for item in trace["consent_history"]], ["c1", "c2"])
        self.assertEqual(trace["decisions"][0]["allowed_channels"], ["email"])
        self.assertEqual(trace["exposure"][0]["exposed"], ["phone"])
        for value in ("family@example.test", "000-101"):
            self.assertNotIn(value, json.dumps(trace, ensure_ascii=False))


class Withdrawal(Harness):
    def _deliver_all(self):
        service = self.service()
        service.sync(None)
        for vendor in ("A", "B"):
            delivery = service.prepare(vendor)
            service.send(vendor, delivery.reference)
        return service

    def test_reduction_creates_stop_only_for_affected_vendor(self):
        service = self._deliver_all()
        self.stage(1)  # L-101 对 A 只剩 email；L-103 对 B 全部撤回
        report = service.verify(None)
        affected = {(entry["vendor"], entry["cid"], tuple(entry["exposed"])) for entry in report["exposure"]}
        self.assertEqual(affected, {("A", "L-101", ("phone",)), ("B", "L-103", ("phone",))})
        created = {(item["vendor"], item["cid"], tuple(item["fields"])) for item in report["created"]}
        self.assertEqual(created, {("A", "L-101", ("phone",)), ("B", "L-103", ("phone",))})
        # B 对 L-101 的 email 仍获授权，不应被收回
        self.assertFalse(any(item["vendor"] == "B" and item["cid"] == "L-101" for item in report["created"]))

    def test_pending_is_not_confirmed(self):
        service = self._deliver_all()
        self.stage(1)
        service.verify(None)
        submitted = service.submit_stops(None)
        self.assertEqual(submitted["confirmed"], [])
        self.assertEqual(len(submitted["pending"]), 2)
        risk = service.risk()
        self.assertEqual(risk["summary"]["not_confirmed_stopped"], 2)
        self.stage(2)
        refreshed = service.refresh_stops(None)
        self.assertEqual(len(refreshed["confirmed"]), 2)
        self.assertEqual(service.risk()["summary"]["not_confirmed_stopped"], 0)
        self.assertTrue(all(entry["confirmed_stopped"] for entry in service.risk()["exposure"]))

    def test_verify_is_idempotent_and_files_once(self):
        service = self._deliver_all()
        self.stage(1)
        service.verify(None)
        second = service.verify(None)
        self.assertEqual(second["created"], [])
        self.assertEqual(len(service.store.stops), 2)

    def test_unconfirmed_stop_blocks_false_confirmation(self):
        service = self._deliver_all()
        self.stage(1)
        service.verify(None)
        before = service.risk()
        self.assertEqual(before["summary"]["not_confirmed_stopped"], 2)
        # 只提交 A 的停用，B 保持未提交
        service.submit_stops("A")
        risk = service.risk()
        entries = {entry["vendor"]: entry for entry in risk["exposure"]}
        self.assertEqual(entries["A"]["stop_state"], "pending")
        self.assertEqual(entries["B"]["stop_state"], "requested")
        self.assertTrue(all(entry["action_needed"] for entry in risk["exposure"]))

    def test_other_vendor_exposure_stays_untouched(self):
        service = self._deliver_all()
        self.stage(1)
        service.verify(None)
        stops = [item for item in service.store.stops.values()]
        self.assertTrue(all(item.vendor in ("A", "B") for item in stops))
        b_delivery = next(
            delivery.reference
            for delivery in service.store.deliveries.values()
            if delivery.vendor == "B"
        )
        for item in stops:
            if item.cid == "L-101":
                self.assertEqual(item.vendor, "A")
            self.assertNotEqual((item.vendor, item.cid), ("B", "L-101"))
        self.assertIn("L-101", [line.cid for delivery in service.store.deliveries.values()
                                if delivery.reference == b_delivery for line in delivery.lines])


class Resume(Harness):
    def test_unprocessed_delivery_can_be_resumed_after_restart(self):
        service = self.service()
        service.sync(None)
        delivery = service.prepare("A")
        # 模拟进程中断：只保存状态、不上传
        reopened = self.service(Store(self.root / "state.json"))
        result = reopened.send("A", delivery.reference)
        self.assertEqual(result["attempted"][0]["status"], "partial")
        reopened.verify(None)
        self.assertTrue((self.root / "state.json").exists())

    def test_repeated_send_does_not_redeliver(self):
        service = self.service()
        service.sync(None)
        delivery = service.prepare("A")
        service.send("A", delivery.reference)
        before = len(self.remote()["remote"]["delivery-A"])
        service.send("A")
        service.send("A", delivery.reference)
        self.assertEqual(len(self.remote()["remote"]["delivery-A"]), before)

    def test_uncertain_send_is_adopted_on_restart(self):
        service = self.service()
        service.sync(None)
        delivery = service.prepare("B")
        # 只上传但崩溃：模拟状态文件停在 prepared，远端已保存
        rows = [
            {"id": "L-101", "visitor": "V-1", "trip": "春节", "consent_version": "c1", "email": "family@example.test"},
            {"id": "L-103", "visitor": "V-1", "trip": "秋季", "consent_version": "c1", "phone": "000-101"},
        ]
        service.portal.upload("B", delivery.reference, rows)
        reopened = self.service(Store(self.root / "state.json"))
        result = reopened.send("B", delivery.reference)
        self.assertEqual(result["adopted"][0]["reference"], delivery.reference)
        line = reopened.store.delivery_for(delivery.reference).lines[0]
        self.assertEqual(line.state, "accepted")
        self.assertIsNotNone(line.receipt)

    def test_sync_notices_consent_change_and_records_conflict(self):
        service = self.service()
        service.sync(None)
        self.stage(1)
        again = service.sync(None)
        self.assertEqual(sorted(again["read"]), ["L-101", "L-102", "L-103"])
        versions = {cid: item.consent_versions for cid, item in service.store.consultations.items()}
        self.assertEqual(versions["L-101"], ["c1", "c2"])
        self.assertEqual(versions["L-103"], ["c1", "c2"])
        self.assertEqual(service.store.consultations["L-103"].snapshot("B"), ("c2", set()))


class Pages(Harness):
    def test_business_uses_only_rendered_pages(self):
        service = self.service()
        report = service.sync(None)
        self.assertEqual(report["listed"], ["L-101", "L-102", "L-103"])
        page = service.portal.read_card("L-101")
        self.assertEqual(page.consent["version"], "c1")
        self.assertEqual(page.present, {"email": True, "phone": True})
        service.prepare("A")
        delivery = service.store.open_deliveries("A")[0]
        service.send("A", delivery.reference)
        # 历史页中解析出的回执与远端保存的一致
        item = service.portal.history("A")[delivery.reference]
        self.assertEqual(item["receipt"], delivery.lines[0].receipt)


if __name__ == "__main__":
    unittest.main()
