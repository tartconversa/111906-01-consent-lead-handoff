"""工作人员入口：通过 Chromium 操作两个门户并查询交付经历。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .browser import Portal, PortalError
from .model import VENDORS, StateError, Store
from .service import Journal, Service


def emit(value) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=1, sort_keys=True)
    sys.stdout.write("\n")


def dispatch(args, service: Service):
    command = args.command
    if command == "sync":
        return service.sync(args.id or None, overwrite=args.overwrite_evidence)
    if command == "prepare":
        delivery = service.prepare(args.vendor, args.id or None)
        return delivery.public() if delivery else {"reference": None, "note": "没有可交付的咨询"}
    if command == "send":
        return service.send(args.vendor, args.reference, lose_response=args.lose_response)
    if command == "resume":
        return resume(service)
    if command == "retry":
        return service.retry_rejected(args.reference).public()
    if command == "verify":
        return service.verify(args.id or None)
    if command == "stops":
        return service.submit_stops(args.vendor) if args.submit else service.refresh_stops(args.vendor)
    if command == "status":
        return service.status(args.vendor)
    if command == "trace":
        return service.trace(args.id)
    if command == "risk":
        return service.risk()
    if command == "evidence":
        return reveal(args, service)
    if command == "run":
        return run_cycle(service)
    raise StateError("未知命令 " + command)


def reveal(args, service: Service) -> dict:
    """受限读取授权原文：必须落在 0600 文件，且写入审计；不回显到终端。"""
    target = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    value = service.reveal_evidence(args.digest, args.reason, args.cid)
    payload = json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    from .model import write_private

    write_private(target, payload)
    return {"digest": args.digest, "written_to": target, "audit": service.store.audit_path}


def resume(service: Service) -> dict:
    """继续所有未完成的交付：重新读取收件历史，只补投尚未定论的行。"""
    deliveries = service.store.open_deliveries(None)
    report = {"open": [item.reference for item in deliveries], "results": [], "skipped": []}
    for delivery in deliveries:
        if delivery.status not in ("prepared", "uncertain", "partial"):
            report["skipped"].append({"reference": delivery.reference, "status": delivery.status})
            continue
        report["results"].append(service.send(delivery.vendor, delivery.reference))
    return report


def run_cycle(service: Service) -> dict:
    """从登记同步到收件、授权核对、回收与风险汇总的一轮完整工作。"""
    result = {"sync": service.sync(None), "deliver": {}, "verify": None, "stops": None, "risk": None}
    for vendor in VENDORS:
        delivery = service.prepare(vendor)
        result["deliver"][vendor] = (
            service.send(vendor, delivery.reference) if delivery else {"note": "没有可交付的咨询"}
        )
    result["verify"] = service.verify(None)
    result["stops"] = service.submit_stops(None)
    result["risk"] = service.risk()
    return result


def execute(args) -> int:
    store = Store(args.state, args.evidence)
    journal = Journal(args.journal or os.path.join(os.path.dirname(store.path), "handoff.log"))
    from playwright.sync_api import sync_playwright

    runtime = sync_playwright().start()
    browser = None
    try:
        browser = runtime.chromium.launch(
            headless=not args.headed, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(accept_downloads=True)
        try:
            page = context.new_page()
            service = Service(store, Portal(page, args.base), journal)
            with store:
                value = dispatch(args, service)
        finally:
            context.close()
    except (StateError, PortalError) as exc:
        sys.stderr.write("无法继续：" + str(exc) + "\n")
        return 2
    finally:
        if browser is not None:
            browser.close()
        runtime.stop()
    emit(value)
    return 0


def parser() -> argparse.ArgumentParser:
    target = argparse.ArgumentParser(description="展会咨询的授权交付、收件回执与撤回回收")
    target.add_argument("--base", default=os.environ.get("HANDOFF_BASE", "http://127.0.0.1:8765"))
    target.add_argument("--state", default=os.environ.get("HANDOFF_STATE", "handoff-state/state.json"))
    target.add_argument("--evidence", default=os.environ.get("HANDOFF_EVIDENCE_DIR"))
    target.add_argument("--journal", default=os.environ.get("HANDOFF_JOURNAL"))
    target.add_argument("--headed", action="store_true", help="显示 Chromium 窗口，便于观察页面操作")
    commands = target.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync", help="打开登记门户，逐张读取咨询卡与授权抽屉")
    sync.add_argument("--id", action="append")
    sync.add_argument("--overwrite-evidence", action="store_true")

    prepare = commands.add_parser("prepare", help="按当前授权组装一次交付（不上传）")
    prepare.add_argument("--vendor", choices=VENDORS, required=True)
    prepare.add_argument("--id", action="append")

    send = commands.add_parser("send", help="上传或继续未确认的交付")
    send.add_argument("--vendor", choices=VENDORS)
    send.add_argument("--reference")
    send.add_argument("--lose-response", action="store_true", help="演练收件方保存后返回 503")

    commands.add_parser("resume", help="继续所有未完成的交付，重查确认页后只补投未定论的行")

    retry = commands.add_parser("retry", help="为被拒收的行开一个新的交付引用")
    retry.add_argument("--reference", required=True)

    verify = commands.add_parser("verify", help="重新核对授权，为缩小或撤回的字段登记停用")
    verify.add_argument("--id", action="append")

    stops = commands.add_parser("stops", help="提交停用申请或刷新停用回执")
    stops.add_argument("--vendor", choices=VENDORS)
    stops.add_argument("--submit", action="store_true")

    status = commands.add_parser("status", help="按展商查看交付与停用状态")
    status.add_argument("--vendor", choices=VENDORS)

    trace = commands.add_parser("trace", help="查看单条咨询的授权、交付与收件经历")
    trace.add_argument("--id", required=True)

    commands.add_parser("risk", help="列出权限变化后尚未确认停用的风险范围")

    evidence = commands.add_parser("evidence", help="受限读取一条授权证据原文（写入审计、只写 0600 文件）")
    evidence.add_argument("--digest", required=True)
    evidence.add_argument("--reason", required=True)
    evidence.add_argument("--cid")
    evidence.add_argument("--output", required=True, help="受限保存位置；文件不会回显到终端")

    commands.add_parser("run", help="执行一轮完整工作：同步、交付、核对、回收、风险汇总")
    return target


def main(argv=None) -> int:
    return execute(parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
