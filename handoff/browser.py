"""在 Chromium 页面上读取与提交，不调用来源的隐藏接口。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .model import CHANNELS, VENDORS


class PortalError(RuntimeError):
    """页面没有给出预期内容；调用方应停止并保留现场。"""


@dataclass
class CardPage:
    card: dict
    consent: dict
    present: dict[str, bool] = field(default_factory=dict)


class Portal:
    """两个门户的唯一业务客户端。

    登记卡、折叠的授权抽屉、收件历史页和停用回执页都通过页面读取；
    名单上传与停用申请都填写页面上可见的表单。
    """

    def __init__(self, page, base: str):
        self.page = page
        self.base = base.rstrip("/")

    # ---------- 通用 ----------
    def _goto(self, path: str) -> None:
        try:
            response = self.page.goto(self.base + path, wait_until="load")
        except Exception as exc:  # Playwright 的网络错误类型不稳定，统一转成 PortalError
            raise PortalError("页面 " + path + " 无法打开：" + str(exc).splitlines()[0]) from None
        if response is None or response.status >= 500:
            raise PortalError("页面 " + path + " 无法打开")
        self.page.wait_for_load_state("load")

    def _pre(self, index: int = 0):
        blocks = self.page.locator("pre")
        if blocks.count() <= index:
            raise PortalError("页面缺少可读结构")
        try:
            return json.loads(blocks.nth(index).inner_text())
        except json.JSONDecodeError:
            raise PortalError("页面内容不是预期格式") from None

    # ---------- 登记门户 ----------
    def card_ids(self) -> list[str]:
        self._goto("/")
        hrefs = self.page.locator('a[href^="/consultation/"]').evaluate_all(
            "nodes => nodes.map(node => node.getAttribute('href'))"
        )
        return [href.rsplit("/", 1)[-1] for href in hrefs]

    def read_card(self, cid: str) -> CardPage:
        """打开咨询卡并实际展开授权抽屉。

        present 只说明页面上是否仍显示电邮/电话原值，用于发现联系方式撤下；
        原值由调用方用于构造当次交付行，不写入持久状态。
        """
        self._goto("/consultation/" + cid)
        card = self._pre(0)
        details = self.page.locator("details")
        if details.count() != 1:
            raise PortalError("咨询 " + cid + " 没有授权抽屉")
        if not details.evaluate("node => node.open"):
            self.page.get_by_text("查看签署授权", exact=True).click()
        if not details.evaluate("node => node.open"):
            raise PortalError("咨询 " + cid + " 的授权抽屉打不开")
        text = details.inner_text()
        if "\n" not in text:
            raise PortalError("咨询 " + cid + " 的授权抽屉没有内容")
        try:
            consent = json.loads(text.split("\n", 1)[1])
        except json.JSONDecodeError:
            raise PortalError("咨询 " + cid + " 的授权内容格式不正确") from None
        if not isinstance(consent, dict):
            raise PortalError("咨询 " + cid + " 的授权内容格式不正确")
        present = {channel: card.get(channel) not in (None, "") for channel in CHANNELS}
        return CardPage(card=card, consent=consent, present=present)

    # ---------- 展商收件门户 ----------
    def _receiver_page(self, vendor: str) -> None:
        self._goto("/receiver/" + vendor)

    def history(self, vendor: str) -> dict:
        self._receiver_page(vendor)
        value = self._pre(0)
        return value if isinstance(value, dict) else {}

    def upload(self, vendor: str, reference: str, rows: list[dict], lose_response: bool = False) -> dict:
        """提交名单，返回 {'status': HTTP 状态, 'item': 脱敏历史记录或 None}。

        即使响应是 5xx，对方也可能已经保存；调用方必须再用同一引用查历史页。
        """
        self._receiver_page(vendor)
        form = self.page.locator('form[action="/receiver/' + vendor + '"]')
        form.locator("[name=reference]").fill(reference)
        form.locator("[name=file]").set_input_files(
            {
                "name": "leads.json",
                "mimeType": "application/json",
                "buffer": json.dumps(rows, ensure_ascii=False).encode("utf-8"),
            }
        )
        if lose_response:
            form.locator("[name=lose_response]").fill("yes")
        with self.page.expect_response(
            lambda item: item.request.method == "POST" and item.url.endswith("/receiver/" + vendor)
        ) as caught:
            form.get_by_role("button", name="交付名单").click()
        status = caught.value.status
        return {"status": status, "item": summarize_receipt(self.history(vendor).get(reference))}

    def stop(self, vendor: str, reference: str, delivery: str, cid: str, fields: list[str]) -> dict | None:
        self._receiver_page(vendor)
        form = self.page.locator('form[action="/stop/' + vendor + '"]')
        form.locator("[name=reference]").fill(reference)
        form.locator("[name=delivery]").fill(delivery)
        form.locator("[name=id]").fill(cid)
        form.locator("[name=fields]").fill(",".join(fields))
        form.get_by_role("button", name="申请停用").click()
        self.page.wait_for_load_state("load")
        return summarize_receipt(self.stop_receipts(vendor).get(reference))

    def stop_receipts(self, vendor: str) -> dict:
        self._goto("/stop/" + vendor)
        value = self._pre(0)
        return value if isinstance(value, dict) else {}


def summarize_receipt(item: dict | None) -> dict | None:
    """保留回执事实，去掉远端保存的名单原值。"""
    if item is None:
        return None
    return {
        "reference": item.get("reference"),
        "receipt": item.get("receipt"),
        "state": item.get("state"),
        "rows": [
            {"id": row.get("id"), "state": row.get("state")}
            for row in (item.get("rows") or [])
            if isinstance(row, dict)
        ],
    }


def split_consent(consent: dict) -> tuple[dict[str, list[str]], str | None]:
    """把抽屉内容拆成 {展商: 获准渠道} 与授权版本；忽略无法识别的渠道。"""
    version = consent.get("version") if isinstance(consent.get("version"), str) else None
    granted: dict[str, list[str]] = {}
    for vendor in VENDORS:
        fields = consent.get(vendor)
        if isinstance(fields, list):
            granted[vendor] = [field for field in fields if field in CHANNELS]
    return granted, version


def permitted(consent: dict, vendor: str) -> set[str]:
    granted, _ = split_consent(consent)
    return set(granted.get(vendor, []))


def row_for(card: dict, consent: dict, vendor: str, fields: set[str] | None = None) -> dict | None:
    """构造一行只含该展商获准字段的名单。

    fields 可进一步缩小到本次计划补交的渠道（默认取全部获准渠道）。
    未获准渠道的键根本不出现在返回行里，因此原值不会进入交付文件。
    """
    allowed = permitted(consent, vendor)
    chosen = allowed if fields is None else allowed & fields
    if not chosen:
        return None
    _, version = split_consent(consent)
    row = {
        "id": card["id"],
        "visitor": card.get("visitor"),
        "trip": card.get("trip"),
        "consent_version": version,
    }
    for channel in CHANNELS:
        if channel in chosen:
            value = card.get(channel)
            if value in (None, ""):
                raise PortalError("咨询 " + card["id"] + " 获准使用 " + channel + "，但卡片没有该值")
            row[channel] = value
    return row
