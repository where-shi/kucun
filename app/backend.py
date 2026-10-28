"""Local replenishment records. Source workbooks are never opened or modified."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB = APP_DIR.parent / "data" / "replenishment.sqlite3"
DEFAULT_SEED = APP_DIR.parent / "examples" / "demo-data.json"
VERSION = "1.0.0"
SUPPLY_FIELDS = [
    ("afn_inbound_shipped_quantity", "系统在途"),
    ("afn_inbound_receiving_quantity", "接收中"),
    ("afn_inbound_working_quantity", "计划中"),
    ("reserved_fc_transfers", "调仓"),
    ("reserved_fc_processing", "仓内处理中"),
]


class AppError(Exception):
    def __init__(self, message, status=400, code="invalid_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def pacific_date(stamp):
    try:
        return stamp.astimezone(ZoneInfo("America/Los_Angeles")).date()
    except ZoneInfoNotFoundError:
        # Windows Python may lack IANA data. US Pacific has used these rules since 2007.
        utc = stamp.astimezone(timezone.utc)
        if utc.year < 2007:
            raise AppError("此时间的美西时区资料不可用，请使用含IANA时区资料的Python环境") from None
        march = date(utc.year, 3, 1)
        november = date(utc.year, 11, 1)
        dst_start = datetime(utc.year, 3, 1 + (6 - march.weekday()) % 7 + 7, 10, tzinfo=timezone.utc)
        dst_end = datetime(utc.year, 11, 1 + (6 - november.weekday()) % 7, 9, tzinfo=timezone.utc)
        offset = -7 if dst_start <= utc < dst_end else -8
        return utc.astimezone(timezone(timedelta(hours=offset))).date()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def copy(value):
    return json.loads(dumps(value))


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def order_multiple(row):
    """Use the verified per-variant packing rule, or one pair when unspecified."""
    value = row.get("orderMultiple", 1)
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 1_000_000 else 1


def purchase_unit_cents(row, preference):
    """Resolve a size-specific quote before the style default; unknown stays unknown."""
    tiers = preference.get("purchase_price_tiers") or []
    if tiers:
        try:
            cn = float(row.get("cnSize"))
        except (ValueError, TypeError):
            return None
        tier = next((t for t in tiers if t["cn_min"] <= cn <= t["cn_max"]), None)
        price = tier.get("unit_cny") if tier else None
    else:
        price = preference.get("purchase_unit_cny")
    return round(price * 100) if price is not None else None


def integer(value, name, minimum=0, maximum=1_000_000):
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise AppError(f"{name}必须是{minimum}至{maximum}之间的整数")
    return value


def text_value(value, name, required=False, limit=2000):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise AppError(f"{name}内容无效")
    return value.strip()


def iso_date(value, name, optional=False):
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str):
        raise AppError(f"{name}必须是YYYY-MM-DD日期")
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError()
    except ValueError:
        raise AppError(f"{name}必须是YYYY-MM-DD日期") from None
    return value


def require_revision(payload, record):
    revision = payload.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision != record["revision"]:
        raise AppError("记录已经更新，请刷新后再保存；你的未保存修改请先保留。", 409, "revision_conflict")


def validate_product_image_path(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 240 or not re.fullmatch(
            r'/product-images/[^\x00-\x1f<>:"/\\|?*%#]+\.(?:png|jpg|jpeg|webp|gif)', value, re.IGNORECASE):
        raise AppError("图片只能关联/product-images/目录内的PNG、JPG、WEBP或GIF文件")
    filename = value.rsplit("/", 1)[1]
    if filename.startswith((".", " ")) or filename.endswith(" ") or re.fullmatch(
            r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', filename, re.IGNORECASE):
        raise AppError("图片文件名无效")
    return value


def batch_timing(record, transit_days=30):
    """Planning estimates are separate from actual shipment/receipt dates."""
    actual = record.get("shipped_date")
    shipped_events = [event["date"] for event in record.get("events", []) if event.get("type") == "shipped"]
    if not actual and shipped_events:
        actual = max(shipped_events)
    system_dates = [m.get("first_observed_date") for m in record.get("inbound_matches", []) if m.get("first_observed_date")]
    observed_date = max(system_dates) if system_dates else None
    ship_date = actual or observed_date or record.get("estimated_ship_date") or record.get("expected_ship_date")
    kind = "actual" if actual else "estimated" if observed_date or record.get("estimated_ship_date") else "planned"
    basis = record.get("ship_date_basis") or ("已记录实际发货日期" if actual else "领星首次显示在途的日期，作为估算起点；实际发出日期未知" if observed_date else "方案中的预计发货日期" if ship_date else "发货时间待确认")
    explicit_available = record.get("expected_available_date")
    available = explicit_available or ((date.fromisoformat(ship_date) + timedelta(days=transit_days)).isoformat() if ship_date else None)
    return dict(ship_date=ship_date, ship_date_kind=kind if ship_date else None,
                ship_date_basis=basis, expected_available_date=available, transit_days=transit_days,
                available_date_basis="已填写的预计可售日期" if explicit_available else f"发货后约{transit_days}天到仓/可售")


def recommend_row(row, days=35):
    """Port of recommendation-rules.js. Waiting-time demand remains excluded."""
    integer(days, "目标天数", 1, 365)
    inv = row.get("inventory") if isinstance(row.get("inventory"), dict) else {}
    stock = inv.get("afn_fulfillable_quantity")
    base = dict(quantity=None, gap=None, target=None, daily=None, lowSample=False, supplies=[])
    if not row.get("inventoryKnown") or not number(stock) or stock < 0:
        return dict(base, status="unknown", label="待核", reason="系统库存缺失，先核对库存；这个尺码不自动带入数量。")
    n14, n28 = row.get("n14"), row.get("n28")
    if not number(n14) or not number(n28) or n14 < 0 or n28 < 0:
        return dict(base, status="unknown", label="待核", reason="销量资料不完整，先补齐数据。")
    daily = max(n14 / 14, n28 / 28)
    if daily == 0:
        return dict(base, status="observe", label="观察", daily=0,
                    reason="近期没有普通销售记录，先观察；也可能受缺货影响，不直接判定不用补。")
    target = math.ceil(daily * days - 1e-9)
    gap = max(0, target - stock)
    supplies = [{"label": label, "quantity": inv[key]} for key, label in SUPPLY_FIELDS
                if number(inv.get(key)) and inv[key] > 0]
    calc = dict(base, gap=gap, target=target, daily=daily, lowSample=n28 < 5, supplies=supplies)
    if gap == 0:
        return dict(calc, quantity=0, status="covered", label="0",
                    reason=f"按当前销量，现货已覆盖这{days}天的参考需求，暂不追加。补货等待期需另算。")
    if supplies:
        desc = "、".join(f"{s['label']}{s['quantity']:g}双" for s in supplies)
        return dict(calc, status="supply", label="先跟货",
                    reason=f"现货相对{days}天需求约差{gap:g}双，但已有{desc}。先核这些货何时可用，再决定新补多少；不把缺口直接当采购数。")
    if any(not number(inv.get(key)) for key, _ in SUPPLY_FIELDS):
        return dict(calc, status="unknown", label="待核",
                    reason=f"现货约差{gap:g}双，但已有供应数据不完整，先核对再带入。")
    multiple = order_multiple(row)
    quantity = math.ceil(gap / multiple) * multiple
    rounding = f"，按该款每变体{multiple}双倍数取{quantity:g}双" if quantity != gap else ""
    low_sample = "销量样本少，先作为小量配码参考。" if n28 < 5 else ""
    return dict(calc, quantity=quantity, status="suggest", label=f"{quantity:g}",
                reason=f"按近期销量参考{days}天需求约{target}双，现货{stock:g}双，初步可考虑补{gap:g}双{rounding}。{low_sample}尚未计入等新货期间的消耗。")


def planning_recommendation(row, days=35, transit_days=30):
    """Decision for review: demand through lead time + cover, less distinct source supply.

    The original prototype rule above remains available for historical comparison.
    No purchase or receipt observation is added to source inventory here.
    """
    integer(days, "到仓后覆盖天数", 1, 365)
    integer(transit_days, "发出后到仓估算天数", 1, 180)
    result = recommend_row(row, days)
    inv = row.get("inventory") or {}
    valid = lambda value: number(value) and value >= 0
    stock = inv.get("afn_fulfillable_quantity") if row.get("inventoryKnown") else None
    keys = [key for key, _ in SUPPLY_FIELDS if key != "afn_inbound_working_quantity"]
    supply_known = all(valid(inv.get(key)) for key in keys)
    pending = sum(inv[key] for key in keys) if supply_known else None
    sales_known = all(valid(row.get(key)) for key in ("n14", "n28"))
    daily = max(row["n14"] / 14, row["n28"] / 28) if sales_known else None
    ceil = lambda value: math.ceil(value - 1e-9)
    target = ceil(daily * days) if daily is not None else None
    wait_demand = ceil(daily * transit_days) if daily is not None else None
    total_target = ceil(daily * (days + transit_days)) if daily is not None else None
    stock_days = round(stock / daily, 1) if valid(stock) and daily else None
    result.update(daily=daily, target=target, gap=max(0, target-stock) if target is not None and valid(stock) else None,
                  pendingQuantity=pending, waitDemand=wait_demand, planningDemand=total_target,
                  coverDays=days, transitDays=transit_days, stockDays=stock_days,
                  provisional=False, bridgeWarning=None,
                  assumption=f"按发出后约{transit_days}天到仓、到仓后覆盖{days}天估算；未额外计工厂生产等待时间。已有已发/接收/调仓/仓内处理中按此期间可用作计划，不承诺实际可售日。")
    if row.get("fbaReplenishmentMode") == "fbm_only":
        return dict(result, quantity=0, status="fbm_only", label="FBM 自发货", bridgeWarning=None,
                    reason="你已确认这个颜色尺码转为FBM自发货，本轮不自动建议补FBA。原FBA库存分项仍保留供核对，你手填的草稿数量继续保留。")
    if row.get("supplyStage") == "planned":
        return dict(result, quantity=None, status="planned", label="计划首发", provisional=True,
                    reason="这款颜色已按你的安排列为计划首发，目前尚未发出。可以在补货方案里逐码填写准备发的数量；不因尚无FBA销量就判定不用发，也不把计划量加进在途或可售。")
    if not valid(stock) or not supply_known:
        return dict(result, quantity=0, status="defer", label="暂缓追加", provisional=True,
                    reason="建议暂缓新增采购：库存资料尚不完整，先避免重复下单。下方已知数量照常展示，空白没有当作0。")
    if not sales_known:
        return dict(result, quantity=0, status="defer", label="先卖现有货" if stock+pending>0 else "暂缓追加", provisional=True,
                    reason=f"建议这次先不追加：现货{stock:g}双，已发及仓内货{pending:g}双，先消化已有货。销量窗口尚未补齐，这是一项保守处理建议，不代表需求为0。")
    if daily == 0:
        if row.get("supplyStage") == "shipped" and pending > 0:
            return dict(result, quantity=0, status="observe", label="等货到仓", provisional=True,
                        reason=f"这一码已有{pending:g}双已发及仓内货，当前可售{stock:g}双。建议先跟这批到仓，之后看FBA销售再决定追加；不把暂时没有FBA销量当作不用发或淘汰的依据。")
        return dict(result, quantity=0, status="observe", label="先卖现有货" if stock+pending>0 else "暂不补", provisional=True,
                    reason=f"建议暂不补：近14/28天未见FBA普通售出，已有现货{stock:g}双、已发及仓内货{pending:g}双，先观察销售。销量为0也可能受库存或刚到仓影响，不据此淘汰款式。")
    shortage = max(0, total_target-stock-pending)
    multiple = order_multiple(row)
    quantity = ceil(shortage/multiple)*multiple
    if stock < wait_demand:
        result["bridgeWarning"] = (f"现货约够{stock_days:g}天；若已有货到得较晚，等货期间可能缺货。优先跟进到货进度，新下单也不能直接解决眼前缺口。" if pending else
                                   f"现货约够{stock_days:g}天，短于约{transit_days}天运输期；新补货到仓前可能缺货。")
    calculation = f"等货约卖{wait_demand}双，到仓后{days}天约卖{target}双；合并按日均销量估算共{total_target}双，减现货{stock:g}双、已发及仓内货{pending:g}双"
    if quantity > 0:
        rounding = f"，按此款每码{multiple}双倍数取{quantity}双" if quantity != shortage else ""
        return dict(result, quantity=quantity, status="suggest", label=f"建议补{quantity}双",
                    reason=f"建议补{quantity}双：{calculation}，还差{shortage:g}双{rounding}。这是待你确认的估算建议。")
    return dict(result, quantity=0, status="covered", label="已有货够，暂不补",
                reason=f"建议暂不补：{calculation}，本次无需追加。已有货仍需按计划到仓。")


class Store:
    def __init__(self, db_path=DEFAULT_DB, seed_path=None, actor="cli"):
        self.path = Path(db_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.actor = actor
        with closing(sqlite3.connect(str(self.path), timeout=15)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    digest TEXT NOT NULL UNIQUE,
                    document TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT NOT NULL,
                    id TEXT NOT NULL,
                    document TEXT NOT NULL,
                    PRIMARY KEY(kind,id)
                );
                CREATE TABLE IF NOT EXISTS purchase_plans (
                    plan_id TEXT PRIMARY KEY,
                    purchase_id TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    entity_id TEXT,
                    before_json TEXT,
                    after_json TEXT,
                    source_json TEXT
                );
            """)
            exists = conn.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone()
            conn.commit()
        if seed_path is not None and not exists:
            with Path(seed_path).open(encoding="utf-8-sig") as handle:
                self.import_snapshot(json.load(handle))

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        if write:
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("BEGIN")
        try:
            yield conn
            if conn.in_transaction:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _snapshot(self, conn):
        rec = conn.execute("SELECT * FROM snapshots ORDER BY seq DESC LIMIT 1").fetchone()
        if rec is None:
            raise AppError("尚未导入库存和销量快照", 409, "no_snapshot")
        return rec["id"], json.loads(rec["document"])

    def _metadata(self, conn):
        snapshot_id, document = self._snapshot(conn)
        return {key: copy(value) for key, value in document.items() if key != "rows"} | {
            "snapshot_id": snapshot_id, "version": VERSION,
        }

    def _get(self, conn, kind, record_id):
        rec = conn.execute("SELECT document FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
        if rec is None:
            raise AppError("找不到这条记录", 404, "not_found")
        return json.loads(rec["document"])

    def _list(self, conn, kind):
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT document FROM records WHERE kind=? ORDER BY rowid DESC", (kind,))]

    def _put(self, conn, kind, record_id, document):
        conn.execute("INSERT INTO records(kind,id,document) VALUES(?,?,?) "
                     "ON CONFLICT(kind,id) DO UPDATE SET document=excluded.document",
                     (kind, record_id, dumps(document)))

    def _audit(self, conn, action, record_id, before, after):
        metadata = self._metadata(conn)
        source = {key: metadata.get(key) for key in ("snapshot_id", "salesEnd", "inventoryCapturedLocal")}
        conn.execute("INSERT INTO audit(created_at,action,actor,entity_id,before_json,after_json,source_json)"
                     " VALUES(?,?,?,?,?,?,?)",
                     (now(), action, self.actor, record_id, dumps(before), dumps(after), dumps(source)))

    def _rows(self, conn):
        return self._snapshot(conn)[1]["rows"]

    def _style_rows(self, conn, style):
        rows = self._rows(conn)
        if style is None:
            return rows
        selected = [row for row in rows if row["style"] == style]
        if not selected:
            raise AppError("款式不存在", 404, "unknown_style")
        return selected

    def _style_preferences(self, conn):
        preferences = {row["style"]: dict(id=row["style"], style=row["style"], revision=0,
                                        replenishment_mode="active", season_note="",
                                        image_path=None, image_source="", arrival_status="",
                                        arrival_note="", arrival_reported_date=None) for row in self._rows(conn)}
        for record in self._list(conn, "style_preference"):
            if record["style"] in preferences:
                preferences[record["style"]].update(record)
        return preferences

    def update_style_preference(self, style, payload):
        if not isinstance(payload, dict):
            raise AppError("款式偏好必须是对象")
        with self.connection(write=True) as conn:
            self._style_rows(conn, style)
            before = self._style_preferences(conn)[style]
            require_revision(payload, before)
            record = copy(before)
            if "replenishment_mode" in payload:
                if payload["replenishment_mode"] not in ("active", "seasonal_hold"):
                    raise AppError("补货状态必须是active或seasonal_hold")
                record["replenishment_mode"] = payload["replenishment_mode"]
            if "season_note" in payload:
                record["season_note"] = text_value(payload["season_note"], "季节补货备注", limit=2000)
            if "image_path" in payload:
                record["image_path"] = validate_product_image_path(payload["image_path"])
            if "image_source" in payload:
                record["image_source"] = text_value(payload["image_source"], "图片来源依据", limit=2000)
            if "color_images" in payload:
                images = payload["color_images"]
                colors = {row["color"] for row in self._style_rows(conn, style)}
                if not isinstance(images, dict) or not set(images).issubset(colors):
                    raise AppError("颜色图片必须对应本款已有颜色")
                validated_images = {}
                for color, image in images.items():
                    if not isinstance(image, dict) or set(image) != {"path", "source"}:
                        raise AppError("颜色图片格式无效")
                    path = validate_product_image_path(image["path"])
                    if not path:
                        raise AppError("颜色图片路径不能为空")
                    validated_images[color] = {"path": path, "source": text_value(image["source"], "图片来源依据", limit=2000)}
                record["color_images"] = validated_images
            if "arrival_status" in payload:
                if payload["arrival_status"] not in ("", "arrived"):
                    raise AppError("到仓确认状态无效")
                record["arrival_status"] = payload["arrival_status"]
            if "arrival_note" in payload:
                record["arrival_note"] = text_value(payload["arrival_note"], "到仓确认备注", limit=2000)
            if "arrival_reported_date" in payload:
                record["arrival_reported_date"] = iso_date(payload["arrival_reported_date"], "到仓反馈日期", optional=True)
            if "purchase_unit_cny" in payload:
                price = payload["purchase_unit_cny"]
                if price is not None and (not number(price) or not 0 <= price <= 100000 or abs(price * 100 - round(price * 100)) > 1e-7):
                    raise AppError("采购单价须为0至100000元、最多两位小数；未知请留空")
                record["purchase_unit_cny"] = price
            if "purchase_price_tiers" in payload:
                tiers = payload["purchase_price_tiers"]
                if not isinstance(tiers, list) or len(tiers) > 50:
                    raise AppError("分尺码采购价必须为最多50组的列表")
                validated = []
                for tier in tiers:
                    if not isinstance(tier, dict):
                        raise AppError("分尺码价格格式无效")
                    lower = integer(tier.get("cn_min"), "中国码下限", 1, 1000)
                    upper = integer(tier.get("cn_max"), "中国码上限", lower, 1000)
                    price = tier.get("unit_cny")
                    if price is not None and (not number(price) or not 0 <= price <= 100000 or abs(price * 100 - round(price * 100)) > 1e-7):
                        raise AppError("分尺码采购单价须为0至100000元、最多两位小数；未知请留空")
                    validated.append(dict(cn_min=lower, cn_max=upper, unit_cny=price))
                validated.sort(key=lambda t: t["cn_min"])
                if any(a["cn_max"] >= b["cn_min"] for a, b in zip(validated, validated[1:])):
                    raise AppError("分尺码价格区间不能重叠")
                record["purchase_price_tiers"] = validated
            if "purchase_price_includes_domestic_freight" in payload:
                if not isinstance(payload["purchase_price_includes_domestic_freight"], bool):
                    raise AppError("是否已含国内运费必须为true或false")
                record["purchase_price_includes_domestic_freight"] = payload["purchase_price_includes_domestic_freight"]
            if "purchase_price_note" in payload:
                record["purchase_price_note"] = text_value(payload["purchase_price_note"], "采购价依据", limit=2000)
            if "purchase_price_date" in payload:
                record["purchase_price_date"] = iso_date(payload["purchase_price_date"], "采购价记录日期", optional=True)
            record.update(revision=before["revision"] + 1, updated_at=now())
            self._put(conn, "style_preference", style, record)
            self._audit(conn, "style_preference.update", style, before, record)
            return record

    def product_image_path(self, image_path):
        if image_path is None:
            raise AppError("图片不存在", 404, "not_found")
        try:
            validate_product_image_path(image_path)
        except AppError:
            raise AppError("图片不存在", 404, "not_found") from None
        with self.connection() as conn:
            if not any(record.get("image_path") == image_path or any(
                       isinstance(image, dict) and image.get("path") == image_path
                       for image in (record.get("color_images") or {}).values())
                       for record in self._list(conn, "style_preference")):
                raise AppError("图片尚未关联到款式", 404, "not_found")
        directory = (self.path.parent / "product-images").resolve()
        target = (directory / image_path.rsplit("/", 1)[1]).resolve()
        if not target.is_relative_to(directory) or not target.is_file():
            raise AppError("图片不存在", 404, "not_found")
        return target

    def _recommend(self, conn, style, days):
        integer(days, "目标天数", 1, 365)
        preferences = self._style_preferences(conn)
        transit_days = self._planning_settings(conn)["transit_days"]
        # A purchase is one supply flow. Never add its quantity to source inventory.
        unmatched = {}
        for purchase in self._list(conn, "purchase"):
            if purchase.get("reconciled_snapshot_id") or purchase.get("status") == "cancelled":
                continue
            if purchase.get("shipment_archived_at"):
                captured = self._snapshot(conn)[1].get("inventoryCapturedAtISO")
                observations = [m["observed_at"] for m in purchase.get("shipment_matches", []) + purchase.get("inbound_matches", [])]
                observed = max(observations) if observations else None
                if captured and observed and datetime.fromisoformat(captured) >= datetime.fromisoformat(observed):
                    continue
            for line in purchase["lines"]:
                if line["ordered"] > 0:
                    unmatched.setdefault(line["sku"], []).append({
                        "purchase_id": purchase["id"], "reference": purchase["reference"],
                        "ordered": line["ordered"], "shipped": line["shipped"], "received": line["received"],
                    })
        evaluated = []
        for row in self._style_rows(conn, style):
            result = planning_recommendation(row, days, transit_days)
            if row["sku"] in unmatched:
                result.update(quantity=None, status="supply", label="暂不追加",
                              localPurchases=unmatched[row["sku"]],
                              reason="本软件已有采购尚未与系统库存核对。请先跟踪该批发货、接收及库存体现，避免再次采购同一缺口。")
            preference = preferences[row["style"]]
            if preference["replenishment_mode"] == "seasonal_hold":
                source_result = copy(result)
                result.update(quantity=None, status="seasonal_hold", label="春夏再看",
                              seasonNote=preference["season_note"], sourceResult=source_result,
                              reason=preference["season_note"] or "该款补货先放一边，春夏再看；恢复补货后再给建议。")
            evaluated.append({"row": row, "result": result})
        return {
            "evaluated": evaluated,
            "quantity": sum(item["result"]["quantity"] or 0 for item in evaluated),
            "suggestedCount": sum(item["result"]["status"] == "suggest" for item in evaluated),
            "pendingSupply": sum(item["result"]["status"] == "supply" for item in evaluated),
            "unknown": sum(item["result"]["status"] == "unknown" for item in evaluated),
            "observe": sum(item["result"]["status"] == "observe" for item in evaluated),
            "deferred": sum(item["result"]["status"] == "defer" for item in evaluated),
            "plannedCount": sum(item["result"]["status"] == "planned" for item in evaluated),
            "noBuyCount": sum(item["result"]["status"] in ("covered", "observe", "defer", "supply", "seasonal_hold", "fbm_only") for item in evaluated),
            "transitDays": transit_days,
            "seasonalHold": sum(item["result"]["status"] == "seasonal_hold" for item in evaluated),
        }

    def bootstrap(self):
        with self.connection() as conn:
            rows = self._rows(conn)
            by_sku = {row["sku"]: row for row in rows}
            cycles = {}
            for cycle in self._list(conn, "cycle"):
                row = by_sku.get(cycle["sku"], {})
                current, baseline = row.get("currentSold"), cycle.get("baseline")
                cycle["currentSold"] = current
                cycle["roundSold"] = current - baseline if number(current) and number(baseline) else None
                cycles[cycle["sku"]] = cycle
            settings = self._planning_settings(conn)
            purchases = [record | {"timing": batch_timing(record, settings["transit_days"])} for record in self._list(conn, "purchase")]
            historical = [record | {"timing": batch_timing(record, settings["transit_days"])} for record in self._list(conn, "historical_batch")]
            return dict(metadata=self._metadata(conn), rows=rows, cycles=cycles, planning_settings=settings,
                        style_preferences=self._style_preferences(conn),
                        plans=self._list(conn, "plan"), purchases=purchases,
                        historical_batches=historical,
                        feedback=self._list(conn, "feedback"))

    def _planning_settings(self, conn):
        return next((record for record in self._list(conn, "settings") if record["id"] == "planning"),
                    dict(id="planning", revision=0, transit_days=30))

    def update_planning_settings(self, payload):
        days = integer(payload.get("transit_days"), "发货后预计到仓/可售天数", 1, 180)
        with self.connection(write=True) as conn:
            before = self._planning_settings(conn)
            require_revision(payload, before)
            record = dict(id="planning", revision=before["revision"] + 1, transit_days=days, updated_at=now())
            self._put(conn, "settings", "planning", record)
            self._audit(conn, "planning.update", "planning", before, record)
            return record

    def recommend(self, style=None, days=35):
        with self.connection() as conn:
            return self._recommend(conn, style, days)

    def import_returns(self, document):
        """Persist complete FBA period reports independently of stock snapshots."""
        if not isinstance(document, dict) or type(document.get("schemaVersion")) is not int or document["schemaVersion"] != 1:
            raise AppError("退货数据必须使用schemaVersion 1")
        configured_sid = os.environ.get("REPLENISHMENT_LINGXING_SID", "").strip()
        if not configured_sid.isdigit() or type(document.get("sid")) is not int or str(document["sid"]) != configured_sid:
            raise AppError("请先配置店铺 SID，且退货数据店铺须与配置一致")
        report_sid = document["sid"]
        if document.get("basis") != "period_fba" or document.get("channel", "FBA") != "FBA":
            raise AppError("退货报告只接受同期FBA普通销量与FBA实际退货口径")
        source = text_value(document.get("source"), "退货来源", required=True, limit=2000)
        sales_end = iso_date(document.get("salesEnd"), "退货数据截止日期")
        captured = document.get("capturedAtISO")
        try:
            stamp = datetime.fromisoformat(captured.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise AppError("capturedAtISO须为含时区的ISO时间") from None
        if date.fromisoformat(sales_end) >= pacific_date(stamp):
            raise AppError("退货窗口须排除未完整结束的美西当天")
        incoming = document.get("reports")
        if not isinstance(incoming, list) or len(incoming) != 3:
            raise AppError("退货数据必须一次提供完整的7、28、90日窗口")
        validated, seen_days = [], set()
        for report in incoming:
            if not isinstance(report, dict):
                raise AppError("退货窗口格式无效")
            days = integer(report.get("days"), "退货窗口天数", 1, 365)
            if days not in (7, 28, 90) or days in seen_days:
                raise AppError("退货数据必须且只能各包含一个7、28、90日窗口")
            seen_days.add(days)
            start = iso_date(report.get("startDate"), "退货窗口开始日期")
            end = iso_date(report.get("endDate"), "退货窗口结束日期")
            if end != sales_end or (date.fromisoformat(end) - date.fromisoformat(start)).days + 1 != days:
                raise AppError("退货窗口日期与天数或截止日期不符")
            total = integer(report.get("sourceTotal"), "退货来源总行数", 0)
            pages, rows = report.get("pages"), report.get("rows")
            if not isinstance(pages, list) or not pages or not isinstance(rows, list):
                raise AppError("退货报告须包含完整分页数量和逐MSKU行")
            if sum(integer(n, "退货分页行数", 0) for n in pages) != total or len(rows) != total:
                raise AppError("退货报告分页未取全或来源行数不一致")
            previous_report = report.get("previousReport")
            previous_by_sku = None
            if previous_report is not None:
                if not isinstance(previous_report, dict):
                    raise AppError("前期退货窗口格式无效")
                previous_start = iso_date(previous_report.get("startDate"), "前期退货窗口开始日期")
                previous_end = iso_date(previous_report.get("endDate"), "前期退货窗口结束日期")
                if (date.fromisoformat(start) - date.fromisoformat(previous_end)).days != 1 or (
                        date.fromisoformat(previous_end) - date.fromisoformat(previous_start)).days + 1 != days:
                    raise AppError("前期退货窗口须紧接本期之前且天数相同")
                previous_total = integer(previous_report.get("sourceTotal"), "前期退货来源总行数", 0)
                previous_pages, previous_rows = previous_report.get("pages"), previous_report.get("rows")
                if not isinstance(previous_pages, list) or not previous_pages or not isinstance(previous_rows, list):
                    raise AppError("前期退货报告须包含完整分页数量和逐MSKU行")
                if sum(integer(n, "前期退货分页行数", 0) for n in previous_pages) != previous_total or len(previous_rows) != previous_total:
                    raise AppError("前期退货报告分页未取全或来源行数不一致")
                previous_by_sku = {}
                for previous_row in previous_rows:
                    if not isinstance(previous_row, dict):
                        raise AppError("前期退货MSKU行格式无效")
                    previous_sku = text_value(previous_row.get("sku"), "前期退货MSKU", required=True, limit=300)
                    if previous_sku != previous_row["sku"] or previous_sku in previous_by_sku:
                        raise AppError("前期退货报告包含重复或无效MSKU")
                    if type(previous_row.get("sid")) is not int or previous_row["sid"] != report_sid:
                        raise AppError("前期退货MSKU行店铺不符")
                    previous_metrics = {key: integer(previous_row.get(key), f"{previous_sku}前期的{key}", 0, 100_000_000)
                                        for key in ("sold", "returned", "fbaReturned")}
                    if previous_metrics["returned"] != previous_metrics["fbaReturned"]:
                        raise AppError("前期FBA退货分子只能包含FBA实际退货数量")
                    previous_by_sku[previous_sku] = dict(sku=previous_sku, sid=report_sid, **previous_metrics)
                previous_report = dict(startDate=previous_start, endDate=previous_end, sourceTotal=previous_total,
                                       pages=previous_pages, rows=sorted(previous_by_sku.values(), key=lambda row: row["sku"]))
            clean, seen_skus = [], set()
            for row in rows:
                if not isinstance(row, dict):
                    raise AppError("退货MSKU行格式无效")
                sku = text_value(row.get("sku"), "退货MSKU", required=True, limit=300)
                if sku != row["sku"] or sku in seen_skus:
                    raise AppError("退货报告包含重复或无效MSKU")
                if type(row.get("sid")) is not int or row["sid"] != report_sid:
                    raise AppError("退货MSKU行店铺不符")
                seen_skus.add(sku)
                metrics = {key: integer(row.get(key), f"{sku}的{key}", 0, 100_000_000)
                           for key in ("sold", "returned", "fbaReturned")}
                if metrics["fbaReturned"] != metrics["returned"]:
                    raise AppError("FBA退货分子只能包含FBA实际退货数量")
                excluded = row.get("excludedFbmReturned", row.get("fbmReturned"))
                if excluded is not None:
                    excluded = integer(excluded, f"{sku}的排除FBM退货量", 0, 100_000_000)
                previous = {key: row.get(key) for key in ("previousSold", "previousReturned")}
                if (previous["previousSold"] is None) != (previous["previousReturned"] is None):
                    raise AppError("前期销量和退货数须同时已核实，未取数时均保持空白")
                for key, value in previous.items():
                    if value is not None:
                        integer(value, f"{sku}的{key}", 0, 100_000_000)
                if previous_by_sku is not None:
                    observed_previous = previous_by_sku.get(sku, {"sold": 0, "returned": 0})
                    expected_previous = dict(previousSold=observed_previous["sold"], previousReturned=observed_previous["returned"])
                    if any(previous[key] is not None and previous[key] != value for key, value in expected_previous.items()):
                        raise AppError("逐码前期数量与完整前期来源不符")
                    previous = expected_previous
                reason = row.get("mainReason")
                if reason is not None:
                    reason = text_value(reason, f"{sku}的退货主因", limit=2000) or None
                source_rate = row.get("sourceRate")
                if source_rate is not None:
                    if not number(source_rate) or source_rate < 0:
                        raise AppError("来源退货率须为非负有限比例或空白")
                    if metrics["sold"] > 0 and abs(source_rate - metrics["returned"] / metrics["sold"]) > 0.0001:
                        raise AppError("来源退货率与FBA退货数及FBA销量不符")
                clean.append(dict(sku=sku, sid=report_sid, **metrics, **previous,
                                  excludedFbmReturned=excluded, mainReason=reason, sourceRate=source_rate))
            clean.sort(key=lambda row: row["sku"])
            record = dict(id=str(days), schemaVersion=1, sid=report_sid, basis="period_fba", channel="FBA",
                          source=source, capturedAtISO=captured, salesEnd=sales_end, days=days,
                          startDate=start, endDate=end, sourceTotal=total, pages=pages, rows=clean)
            if previous_report is not None:
                record["previousReport"] = previous_report
            record["digest"] = hashlib.sha256(dumps(record).encode("utf8")).hexdigest()
            validated.append(record)
        if seen_days != {7, 28, 90}:
            raise AppError("退货报告缺少完整窗口")
        with self.connection(write=True) as conn:
            old = {int(report["days"]): report for report in self._list(conn, "return_report")}
            for record in validated:
                previous = old.get(record["days"])
                if previous and (record["salesEnd"] < previous["salesEnd"] or stamp < datetime.fromisoformat(
                        previous["capturedAtISO"].replace("Z", "+00:00"))):
                    raise AppError("退货报告截止日或采集时间不能早于已保存资料", 409, "older_return_report")
            if all(old.get(record["days"], {}).get("digest") == record["digest"] for record in validated):
                return {"imported": False, "reason": "相同退货报告已导入", "salesEnd": sales_end, "days": [7, 28, 90]}
            for record in validated:
                self._put(conn, "return_report", record["id"], record)
            result = dict(imported=True, salesEnd=sales_end, capturedAtISO=captured,
                          days=[7, 28, 90], source=source,
                          sourceRows={str(r["days"]): r["sourceTotal"] for r in validated})
            before = {str(days): {key: value.get(key) for key in ("salesEnd", "capturedAtISO", "digest")}
                      for days, value in old.items()}
            self._audit(conn, "return_report.import", "7_28_90", before, result)
            return result

    @staticmethod
    def _return_totals(rows):
        def known_sum(key):
            return sum(row[key] for row in rows) if all(row.get(key) is not None for row in rows) else None
        sold = sum(row["sold"] for row in rows)
        returned = sum(row["returned"] for row in rows)
        previous_sold, previous_returned = known_sum("previousSold"), known_sum("previousReturned")
        rate = returned / sold if sold > 0 else None
        previous_rate = previous_returned / previous_sold if previous_sold and previous_returned is not None else None
        return dict(sold=sold, returned=returned, fbaReturned=returned, fbmReturned=0,
                    excludedFbmReturned=known_sum("excludedFbmReturned"), rate=rate,
                    previousSold=previous_sold, previousReturned=previous_returned, previousRate=previous_rate,
                    rateChange=rate - previous_rate if rate is not None and previous_rate is not None else None,
                    rateStatus="no_sales_base" if sold == 0 and returned > 0 else "no_sales" if sold == 0 else "available",
                    noSalesBase=sold == 0 and returned > 0, skuCount=len(rows))

    def returns(self, days=28, style=None):
        days = integer(days, "退货窗口天数", 1, 365)
        if days not in (7, 28, 90):
            raise AppError("退货窗口仅支持7、28、90天")
        with self.connection() as conn:
            all_rows = self._rows(conn)
            if style is not None:
                self._style_rows(conn, style)
            report = next((r for r in self._list(conn, "return_report") if r["days"] == days), None)
            if report is None:
                return dict(available=False, metadata={"days": days, "channel": "FBA", "basis": "period_fba"},
                            summary=None, styles=[], colors=[], rows=[], reasonCoverage=None)
            by_sku = {row["sku"]: row for row in report["rows"]}
            previous_report = report.get("previousReport")
            previous_by_sku = {row["sku"]: row for row in previous_report["rows"]} if previous_report is not None else None
            enriched = []
            for row in all_rows:
                values = by_sku.get(row["sku"])
                metrics = copy(values) if values is not None else dict(sku=row["sku"], sid=report["sid"], sold=0,
                    returned=0, fbaReturned=0, excludedFbmReturned=0, previousSold=None,
                    previousReturned=None, mainReason=None, sourceRate=None)
                if previous_by_sku is not None:
                    previous_values = previous_by_sku.get(row["sku"], {"sold": 0, "returned": 0})
                    metrics.update(previousSold=previous_values["sold"], previousReturned=previous_values["returned"])
                metadata = {key: row.get(key) for key in
                            ("sku", "style", "styleLabel", "color", "colorLabel", "size", "cnSize", "sizeOrder")}
                totals = self._return_totals([metrics])
                enriched.append(dict(metadata, **{k: v for k, v in metrics.items() if k != "sku"},
                                     **{k: totals[k] for k in ("rate", "previousRate", "rateChange", "rateStatus", "noSalesBase")},
                                     sourcePresent=values is not None, fbmReturned=0))
            groups = {}
            color_groups = {}
            for row in enriched:
                groups.setdefault(row["style"], []).append(row)
                if style is None or row["style"] == style:
                    color_groups.setdefault((row["style"], row["color"]), []).append(row)
            styles = [dict(style=key, styleLabel=rows[0]["styleLabel"], **self._return_totals(rows))
                      for key, rows in groups.items()]
            colors = [dict(style=key[0], color=key[1], colorLabel=rows[0]["colorLabel"],
                           **self._return_totals(rows)) for key, rows in color_groups.items()]
            selected = [row for row in enriched if style is None or row["style"] == style]
            with_reason = [r for r in selected if r["returned"] > 0 and r["mainReason"]]
            without_reason = [r for r in selected if r["returned"] > 0 and not r["mainReason"]]
            coverage = dict(withMainReasonSkuCount=len(with_reason), missingMainReasonSkuCount=len(without_reason),
                            withMainReasonReturned=sum(r["returned"] for r in with_reason),
                            withoutMainReasonReturned=sum(r["returned"] for r in without_reason),
                            note="主因覆盖仅表示对应尺码有主因资料，不是完整原因分布；未核实原因保留空白。")
            metadata = {key: report[key] for key in ("sid", "basis", "channel", "source", "capturedAtISO",
                        "salesEnd", "days", "startDate", "endDate", "sourceTotal", "pages")}
            metadata.update(basisLabel="FBA退货双数 ÷ FBA普通售出双数", rateUnit="ratio",
                            definition="按领星订单利润报表日期口径，FBA退货双数 ÷ FBA普通售出双数。只统计软件已跟踪款式；FBM和退款不计入FBA退货，换货不计入普通售出。款式和颜色按合计双数计算比例。",
                            caveat="近期订单的退货可能尚未完全回补，比例会随来源更新；当前比例不代表最终退货率。没有可计算的销量时显示—。",
                            style=style)
            if previous_report is not None:
                metadata.update(previousStartDate=previous_report["startDate"], previousEndDate=previous_report["endDate"],
                                previousSourceTotal=previous_report["sourceTotal"], previousPages=previous_report["pages"])
            return dict(available=True, metadata=metadata, summary=self._return_totals(selected),
                        styles=styles, colors=colors, rows=selected, reasonCoverage=coverage)

    def import_snapshot(self, document):
        if not isinstance(document, dict) or not isinstance(document.get("rows"), list) or not document["rows"]:
            raise AppError("快照必须包含非空的rows列表")
        iso_date(document.get("salesEnd"), "销量截止日期")
        text_value(document.get("inventoryCapturedLocal"), "库存快照日期", required=True)
        seen = set()
        for row in document["rows"]:
            if not isinstance(row, dict):
                raise AppError("变体行格式无效")
            sku = text_value(row.get("sku"), "SKU", required=True, limit=300)
            style = text_value(row.get("style"), "款式", required=True, limit=200)
            if sku != row["sku"] or style != row["style"]:
                raise AppError("SKU和款式首尾不能包含空格")
            for key in ("styleLabel", "color", "colorLabel", "size"):
                text_value(row.get(key), f"{sku}的{key}", required=True, limit=300)
            if row.get("batchNote") is not None:
                text_value(row["batchNote"], f"{sku}的批次说明", limit=10000)
            for key in ("orderMultiple", "minimumOrder", "cartonQuantity"):
                if row.get(key) is not None:
                    integer(row[key], f"{sku}的{key}", 1)
            if sku in seen:
                raise AppError(f"快照重复SKU：{sku}")
            seen.add(sku)
            inv = row.get("inventory")
            if inv is not None and not isinstance(inv, dict):
                raise AppError(f"{sku}的inventory必须是对象或null")
            if isinstance(inv, dict):
                for key in [key for key, _ in SUPPLY_FIELDS] + [
                    "afn_fulfillable_quantity", "afn_reserved_quantity", "reserved_customerorders"
                ]:
                    if inv.get(key) is not None:
                        integer(inv[key], f"{sku}的{key}", 0, 100_000_000)
            for key in ("n7", "p7", "n14", "n28", "fbm7", "currentSold", "baseline"):
                if row.get(key) is not None:
                    integer(row[key], f"{sku}的{key}", 0, 100_000_000)
            if row.get("stockValue") is not None:
                integer(row["stockValue"], f"{sku}的账面库存", -100_000_000, 100_000_000)
            if "inventoryKnown" in row and not isinstance(row["inventoryKnown"], bool):
                raise AppError(f"{sku}的inventoryKnown必须为true或false")
            if row.get("baselineDate") not in (None, ""):
                iso_date(row["baselineDate"], f"{sku}的本轮起点日期")
            if row.get("baselineKind") is not None:
                text_value(row["baselineKind"], f"{sku}的本轮起点依据")
        if document.get("inventoryCapturedAtISO") is not None:
            try:
                captured = datetime.fromisoformat(document["inventoryCapturedAtISO"])
                if captured.tzinfo is None:
                    raise ValueError()
            except (ValueError, TypeError):
                raise AppError("inventoryCapturedAtISO须为含时区的ISO时间") from None
        try:
            encoded = dumps(document)
        except (TypeError, ValueError):
            raise AppError("快照包含无法保存的内容") from None
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection(write=True) as conn:
            existing = conn.execute("SELECT id FROM snapshots WHERE digest=?", (digest,)).fetchone()
            if existing:
                return {"snapshot_id": existing["id"], "imported": False, "reason": "相同快照已导入"}
            previous = conn.execute("SELECT id,document FROM snapshots ORDER BY seq DESC LIMIT 1").fetchone()
            if previous:
                previous_doc = json.loads(previous["document"])
                if document["salesEnd"] < previous_doc["salesEnd"]:
                    raise AppError("新快照销量截止日期不能早于当前快照", 409, "older_snapshot")
                old_skus = {r["sku"] for r in previous_doc["rows"]}
                if not old_skus.issubset(seen):
                    raise AppError("新快照缺少已有SKU；请补齐后导入，避免丢失款尺码对应关系", 409, "incomplete_snapshot")
                old_styles = {r["sku"]: r["style"] for r in previous_doc["rows"]}
                if any(r["sku"] in old_styles and r["style"] != old_styles[r["sku"]] for r in document["rows"]):
                    raise AppError("已有SKU的款式对应关系发生变化，请先核对", 409, "sku_style_changed")
            snapshot_id = "snap_" + digest[:20]
            conn.execute("INSERT INTO snapshots(id,digest,document,created_at) VALUES(?,?,?,?)",
                         (snapshot_id, digest, encoded, now()))
            for row in document["rows"]:
                if not conn.execute("SELECT 1 FROM records WHERE kind='cycle' AND id=?", (row["sku"],)).fetchone():
                    self._put(conn, "cycle", row["sku"], {
                        "sku": row["sku"], "baselineDate": row.get("baselineDate"),
                        "baseline": row.get("baseline"), "baselineKind": row.get("baselineKind"),
                        "revision": 1, "updated_at": now(),
                    })
            result = {"snapshot_id": snapshot_id, "imported": True, "row_count": len(document["rows"])}
            self._audit(conn, "snapshot.import", snapshot_id, {"snapshot_id": previous["id"]} if previous else None, result)
            return result

    def create_plan(self, payload):
        if not isinstance(payload, dict):
            raise AppError("方案格式无效")
        style = text_value(payload.get("style"), "款式", required=True, limit=200)
        name = text_value(payload.get("name") or f"{style} 补货草稿", "方案名称", required=True, limit=200)
        days = integer(payload.get("target_days", 35), "目标天数", 1, 365)
        with self.connection(write=True) as conn:
            self._style_rows(conn, style)
            timestamp = now()
            plan = dict(id="plan_" + uuid.uuid4().hex, style=style, name=name, target_days=days,
                        revision=1, status="draft", snapshot_id=self._snapshot(conn)[0],
                        lines=[], created_at=timestamp, updated_at=timestamp)
            self._put(conn, "plan", plan["id"], plan)
            self._audit(conn, "plan.create", plan["id"], None, plan)
            return plan

    def _editable_plan(self, conn, plan_id, payload):
        plan = self._get(conn, "plan", plan_id)
        require_revision(payload, plan)
        if plan["status"] != "draft":
            raise AppError("该方案已登记采购，请新建方案进行下一轮补货", 409, "confirmed_plan")
        return plan

    def update_plan(self, plan_id, payload):
        with self.connection(write=True) as conn:
            plan = self._editable_plan(conn, plan_id, payload)
            before = copy(plan)
            if "name" in payload:
                plan["name"] = text_value(payload["name"], "方案名称", required=True, limit=200)
            if "target_days" in payload:
                plan["target_days"] = integer(payload["target_days"], "目标天数", 1, 365)
            incoming = payload.get("lines", [])
            if not isinstance(incoming, list):
                raise AppError("配码必须是列表")
            valid_skus = {row["sku"] for row in self._style_rows(conn, plan["style"])}
            lines = {line["sku"]: line for line in plan["lines"]}
            seen = set()
            for line in incoming:
                if not isinstance(line, dict) or line.get("sku") not in valid_skus:
                    raise AppError("配码SKU不存在或不属于当前款式")
                sku = line["sku"]
                if sku in seen:
                    raise AppError("一次保存不能包含重复SKU")
                seen.add(sku)
                quantity = integer(line.get("quantity"), "补货双数")
                if line.get("manual", True) is not True:
                    raise AppError("手动保存的配码必须标记manual=true")
                note = text_value(line.get("note", lines.get(sku, {}).get("note", "")), "备注")
                lines[sku] = {"sku": sku, "quantity": quantity, "manual": True, "note": note}
            plan["lines"] = list(lines.values())
            plan["revision"] += 1
            plan["updated_at"] = now()
            self._put(conn, "plan", plan_id, plan)
            self._audit(conn, "plan.update", plan_id, before, plan)
            return plan

    def adopt_plan(self, plan_id, payload):
        with self.connection(write=True) as conn:
            plan = self._editable_plan(conn, plan_id, payload)
            before = copy(plan)
            lines = {line["sku"]: line for line in plan["lines"] if line["manual"]}
            for evaluated in self._recommend(conn, plan["style"], plan["target_days"])["evaluated"]:
                row, result = evaluated["row"], evaluated["result"]
                if result["status"] == "suggest" and result["quantity"] > 0 and row["sku"] not in lines:
                    lines[row["sku"]] = dict(sku=row["sku"], quantity=result["quantity"],
                                             manual=False, note=result["reason"])
            plan["lines"] = list(lines.values())
            plan["snapshot_id"] = self._snapshot(conn)[0]
            plan["revision"] += 1
            plan["updated_at"] = now()
            self._put(conn, "plan", plan_id, plan)
            self._audit(conn, "plan.adopt", plan_id, before, plan)
            return plan

    def confirm_plan(self, plan_id, payload):
        if payload.get("confirmed") is not True:
            raise AppError("需明确确认已经实际下单，才登记采购", 400, "confirmation_required")
        with self.connection(write=True) as conn:
            existing = conn.execute("SELECT purchase_id FROM purchase_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if existing:
                return self._get(conn, "purchase", existing["purchase_id"])
            plan = self._editable_plan(conn, plan_id, payload)
            reference = text_value(payload.get("reference"), "采购编号或说明", required=True, limit=300)
            ship_date = iso_date(payload.get("expected_ship_date"), "预计发货日期", optional=True)
            available_date = iso_date(payload.get("expected_available_date"), "预计可售日期", optional=True)
            if ship_date and available_date and available_date < ship_date:
                raise AppError("预计可售日期不能早于预计发货日期")
            positive = [line for line in plan["lines"] if line["quantity"] > 0]
            if not positive:
                raise AppError("方案至少需要一个正数配码，才能登记采购")
            before = copy(plan)
            purchase = dict(id="buy_" + uuid.uuid4().hex, plan_id=plan_id, style=plan["style"],
                            reference=reference, revision=1, expected_ship_date=ship_date,
                            expected_available_date=available_date, created_at=now(), updated_at=now(),
                            source_snapshot_id=self._snapshot(conn)[0], status="ordered",
                            reconciled_snapshot_id=None, reconciled_at=None, reconciliation_note=None,
                            lines=[dict(sku=line["sku"], ordered=line["quantity"], shipped=0, received=0)
                                   for line in positive], events=[])
            self._put(conn, "purchase", purchase["id"], purchase)
            conn.execute("INSERT INTO purchase_plans(plan_id,purchase_id) VALUES(?,?)", (plan_id, purchase["id"]))
            plan.update(status="confirmed", revision=plan["revision"] + 1, updated_at=now(),
                        purchase_id=purchase["id"])
            self._put(conn, "plan", plan_id, plan)
            self._audit(conn, "purchase.confirm", purchase["id"], None, purchase)
            self._audit(conn, "plan.confirm", plan_id, before, plan)
            return purchase

    def update_purchase_logistics(self, purchase_id, payload):
        """Record a forwarder handoff/forecast without asserting actual dispatch."""
        fields = {"forwarder_handoff_confirmed", "forwarder_reported_date",
                  "forwarder_note", "expected_ship_date"}
        if not isinstance(payload, dict) or not fields.intersection(payload):
            raise AppError("请提供本批交货代或预计发货资料")
        if set(payload) - fields - {"revision"}:
            raise AppError("物流资料只能修改交货代确认、报告日期、备注和预计发货日期")
        changes = {}
        if "forwarder_handoff_confirmed" in payload:
            value = payload["forwarder_handoff_confirmed"]
            if not isinstance(value, bool):
                raise AppError("交货代确认必须是true或false")
            changes["forwarder_handoff_confirmed"] = value
        if "forwarder_reported_date" in payload:
            reported = iso_date(payload["forwarder_reported_date"], "交货代报告日期", optional=True)
            if reported and reported > datetime.now(timezone(timedelta(hours=8))).date().isoformat():
                raise AppError("报告日期不能晚于北京时间今天；预计发出日期请另填")
            changes["forwarder_reported_date"] = reported
        if "forwarder_note" in payload:
            changes["forwarder_note"] = text_value(payload["forwarder_note"], "交货代说明", limit=2000)
        if "expected_ship_date" in payload:
            changes["expected_ship_date"] = iso_date(payload["expected_ship_date"], "预计发货日期", optional=True)
        with self.connection(write=True) as conn:
            purchase = self._get(conn, "purchase", purchase_id)
            require_revision(payload, purchase)
            if purchase.get("status") == "cancelled":
                raise AppError("该采购登记已取消", 409, "cancelled_purchase")
            if purchase.get("reconciled_snapshot_id"):
                raise AppError("已完成库存核对的批次不能再修改待发物流资料", 409, "reconciled_purchase")
            if purchase.get("shipment_archived_at"):
                raise AppError("已核实整批发出的历史批次不能再修改待发物流资料", 409, "archived_purchase")
            ship_date = changes.get("expected_ship_date", purchase.get("expected_ship_date"))
            available_date = purchase.get("expected_available_date")
            if ship_date and available_date and available_date < ship_date:
                raise AppError("预计可售日期不能早于预计发货日期，请先核对现有预计可售日期")
            before = copy(purchase)
            purchase.update(changes, revision=purchase["revision"] + 1, updated_at=now())
            self._put(conn, "purchase", purchase_id, purchase)
            self._audit(conn, "purchase.logistics.update", purchase_id, before, purchase)
            return purchase

    def purchase_event(self, purchase_id, payload):
        event_id = text_value(payload.get("event_id"), "事件编号", required=True, limit=200)
        event_type = payload.get("type")
        if event_type not in ("shipped", "received"):
            raise AppError("事件必须是shipped或received")
        event_date = iso_date(payload.get("date"), "实际日期")
        if event_date > datetime.now(timezone(timedelta(hours=8))).date().isoformat():
            raise AppError("实际发货或接收日期不能晚于北京时间今天；未来日期请记录为预计日期")
        incoming = payload.get("lines")
        if not isinstance(incoming, list) or not incoming:
            raise AppError("至少填写一个尺码的本次数量")
        validated = []
        seen = set()
        for line in incoming:
            if not isinstance(line, dict):
                raise AppError("本次配码格式无效")
            sku = text_value(line.get("sku"), "SKU", required=True, limit=300)
            if sku in seen:
                raise AppError("一次事件不能包含重复SKU")
            seen.add(sku)
            validated.append({"sku": sku, "quantity": integer(line.get("quantity"), "本次双数", 1)})
        event_content = dict(event_id=event_id, type=event_type, date=event_date,
                             lines=sorted(validated, key=lambda line: line["sku"]))
        with self.connection(write=True) as conn:
            purchase = self._get(conn, "purchase", purchase_id)
            for existing in purchase["events"]:
                if existing["event_id"] == event_id:
                    prior = {key: existing[key] for key in ("event_id", "type", "date", "lines")}
                    if dumps(prior) != dumps(event_content):
                        raise AppError("相同事件编号已使用，但内容不同", 409, "event_conflict")
                    return purchase
            require_revision(payload, purchase)
            if purchase.get("status") == "cancelled":
                raise AppError("该采购登记已取消", 409, "cancelled_purchase")
            if purchase.get("reconciled_snapshot_id"):
                raise AppError("已完成库存核对的批次不能再登记数量", 409, "reconciled_purchase")
            before = copy(purchase)
            by_sku = {line["sku"]: line for line in purchase["lines"]}
            for event_line in validated:
                sku, quantity = event_line["sku"], event_line["quantity"]
                if sku not in by_sku:
                    raise AppError("该SKU不在本次采购中")
                line = by_sku[sku]
                limit = line["ordered"] if event_type == "shipped" else line["shipped"]
                if line[event_type] + quantity > limit:
                    raise AppError("本次发货超过已购数量" if event_type == "shipped" else "本次接收超过已发数量")
                line[event_type] += quantity
            event_content["recorded_at"] = now()
            purchase["events"].append(event_content)
            # Dates describe physical events, including backfilled events.
            # A receipt must be supported by shipments that had occurred by that date.
            dated_totals = {}
            for event in sorted(purchase["events"], key=lambda e: (e["date"], e["type"] == "received")):
                for event_line in event["lines"]:
                    totals_on_date = dated_totals.setdefault(event_line["sku"], {"shipped": 0, "received": 0})
                    totals_on_date[event["type"]] += event_line["quantity"]
                    if totals_on_date["received"] > totals_on_date["shipped"]:
                        raise AppError("接收日期早于对应发货，请核对实际日期与分码数量", 409, "event_date_conflict")
            totals = {key: sum(line[key] for line in purchase["lines"]) for key in ("ordered", "shipped", "received")}
            if totals["received"] == totals["ordered"]:
                purchase["status"] = "received"
            elif totals["received"] > 0:
                purchase["status"] = "receiving"
            elif totals["shipped"] == totals["ordered"]:
                purchase["status"] = "shipped"
            elif totals["shipped"] > 0:
                purchase["status"] = "partially_shipped"
            else:
                purchase["status"] = "ordered"
            purchase["revision"] += 1
            purchase["updated_at"] = now()
            self._put(conn, "purchase", purchase_id, purchase)
            self._audit(conn, "purchase." + event_type, purchase_id, before, purchase)
            return purchase

    def reconcile_purchase(self, purchase_id, payload):
        if payload.get("confirmed") is not True:
            raise AppError("请明确确认该批货已全部体现在当前库存快照")
        note = text_value(payload.get("note"), "库存核对依据", required=True)
        with self.connection(write=True) as conn:
            purchase = self._get(conn, "purchase", purchase_id)
            if purchase.get("status") == "cancelled":
                raise AppError("该采购登记已取消", 409, "cancelled_purchase")
            require_revision(payload, purchase)
            snapshot_id = self._snapshot(conn)[0]
            if payload.get("snapshot_id") != snapshot_id:
                raise AppError("请使用当前库存快照核对", 409, "snapshot_conflict")
            if snapshot_id == purchase["source_snapshot_id"]:
                raise AppError("请先导入该批接收后的新库存快照，再确认库存体现", 409, "new_snapshot_required")
            document = self._snapshot(conn)[1]
            captured_iso = document.get("inventoryCapturedAtISO")
            if captured_iso:
                captured_day = datetime.fromisoformat(captured_iso).astimezone(
                    timezone(timedelta(hours=8))).date().isoformat()
                receipt_dates = [e["date"] for e in purchase["events"] if e["type"] == "received"]
                if receipt_dates and captured_day < max(receipt_dates):
                    raise AppError("当前库存快照早于实际接收日期，请导入接收后的库存快照", 409, "snapshot_before_receipt")
            if not all(line["received"] == line["ordered"] for line in purchase["lines"]):
                raise AppError("整批全部接收后才可完成库存核对", 409, "not_fully_received")
            if purchase.get("reconciled_snapshot_id"):
                raise AppError("该批次已完成库存核对", 409, "already_reconciled")
            before = copy(purchase)
            purchase.update(reconciled_snapshot_id=snapshot_id, reconciled_at=now(),
                            reconciliation_note=note, revision=purchase["revision"] + 1, updated_at=now())
            self._put(conn, "purchase", purchase_id, purchase)
            self._audit(conn, "purchase.reconcile", purchase_id, before, purchase)
            return purchase

    def cancel_purchase(self, purchase_id, payload):
        if payload.get("confirmed") is not True:
            raise AppError("请确认取消这批本地采购登记", 400, "confirmation_required")
        note = text_value(payload.get("note", "用户取消本地采购登记"), "取消说明", required=True)
        with self.connection(write=True) as conn:
            purchase = self._get(conn, "purchase", purchase_id)
            if purchase.get("status") == "cancelled":
                return purchase
            require_revision(payload, purchase)
            before = copy(purchase)
            purchase.update(status="cancelled", cancelled_at=now(), cancel_note=note,
                            revision=purchase["revision"] + 1, updated_at=now())
            self._put(conn, "purchase", purchase_id, purchase)
            plan = self._get(conn, "plan", purchase["plan_id"])
            prior_plan = copy(plan)
            plan.update(status="cancelled", revision=plan["revision"] + 1, updated_at=now())
            self._put(conn, "plan", plan["id"], plan)
            self._audit(conn, "purchase.cancel", purchase_id, before, purchase)
            self._audit(conn, "plan.cancel", plan["id"], prior_plan, plan)
            return purchase

    def auto_match_inbound(self, before_snapshot_id, after_snapshot_id):
        """Match exact, unambiguous per-SKU increases in consecutive Lingxing stock snapshots.

        This is a system-inbound observation, not a carrier shipment ID or actual departure date.
        Partial or overlapping purchases remain pending. One observation cannot be reused.
        """
        before_snapshot_id = text_value(before_snapshot_id, "前一库存快照", required=True, limit=200)
        after_snapshot_id = text_value(after_snapshot_id, "当前库存快照", required=True, limit=200)
        with self.connection(write=True) as conn:
            old = conn.execute("SELECT seq,document FROM snapshots WHERE id=?", (before_snapshot_id,)).fetchone()
            new = conn.execute("SELECT seq,document FROM snapshots WHERE id=?", (after_snapshot_id,)).fetchone()
            previous_seq = conn.execute("SELECT MAX(seq) FROM snapshots WHERE seq < ?", (new[0],)).fetchone()[0] if new else None
            if not old or not new or old[0] != previous_seq or self._snapshot(conn)[0] != after_snapshot_id:
                raise AppError("请使用按时间排序且当前有效的两份库存快照", 409, "snapshot_conflict")
            earlier, later = json.loads(old[1]), json.loads(new[1])
            try:
                old_time = datetime.fromisoformat(earlier["inventoryCapturedAtISO"].replace("Z", "+00:00"))
                new_time = datetime.fromisoformat(later["inventoryCapturedAtISO"].replace("Z", "+00:00"))
                if old_time.tzinfo is None or new_time.tzinfo is None or old_time >= new_time:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise AppError("库存快照采集时间不完整或未按时间排序") from None
            old_rows = {r["sku"]: r for r in earlier["rows"]}
            new_rows = {r["sku"]: r for r in later["rows"]}
            purchases = self._list(conn, "purchase")
            active = [p for p in purchases if p.get("status") != "cancelled" and not p.get("shipment_archived_at") and not p.get("reconciled_snapshot_id")]
            owners = {}
            for p in active:
                for line in p.get("lines", []):
                    if line.get("ordered", 0) > line.get("system_shipped", 0):
                        owners.setdefault(line["sku"], set()).add(p["id"])
            used = set()
            for p in purchases:
                for match in p.get("inbound_matches", []):
                    if match.get("before_snapshot_id") == before_snapshot_id and match.get("after_snapshot_id") == after_snapshot_id:
                        used.update(line["sku"] for line in match.get("lines", []))
            matched, skipped = [], []
            for purchase in active:
                lines = purchase.get("lines", [])
                try:
                    created = datetime.fromisoformat(purchase["created_at"].replace("Z", "+00:00"))
                    eligible = bool(lines) and (created <= old_time or
                        (purchase.get("source_snapshot_id") == before_snapshot_id and created <= new_time))
                except (KeyError, TypeError, ValueError):
                    eligible = False
                changes = []
                for line in lines:
                    sku = line["sku"]
                    old_row, new_row = old_rows.get(sku), new_rows.get(sku)
                    old_inv = old_row.get("inventory", {}) if old_row else {}
                    new_inv = new_row.get("inventory", {}) if new_row else {}
                    previous, current = old_inv.get("afn_inbound_shipped_quantity"), new_inv.get("afn_inbound_shipped_quantity")
                    qty = line.get("ordered")
                    if (not eligible or not old_row or not new_row or not old_row.get("inventoryKnown") or not new_row.get("inventoryKnown")
                            or not number(previous) or not number(current) or not number(qty) or qty <= 0
                            or current - previous != qty or line.get("shipped", 0) or line.get("system_shipped", 0)
                            or len(owners.get(sku, set())) != 1 or sku in used):
                        eligible = False
                        break
                    changes.append(dict(sku=sku, quantity=qty, before=previous, after=current))
                if not eligible:
                    skipped.append(purchase["id"])
                    continue
                prior = copy(purchase)
                observed = new_time.astimezone(timezone.utc).isoformat()
                match = dict(before_snapshot_id=before_snapshot_id, after_snapshot_id=after_snapshot_id,
                             observed_at=observed, first_observed_date=new_time.astimezone(timezone(timedelta(hours=8))).date().isoformat(),
                             basis="领星已发在途逐码增量与本批配码完全一致；非货件号或实际发货日期", lines=changes)
                for line in lines:
                    line.update(system_shipped=line["ordered"], shipped=line["ordered"])
                    used.add(line["sku"])
                purchase.setdefault("inbound_matches", []).append(match)
                purchase.update(status="shipped", shipment_archived_at=now(), revision=purchase["revision"]+1, updated_at=now())
                self._put(conn, "purchase", purchase["id"], purchase)
                self._audit(conn, "purchase.lingxing-inbound-match", purchase["id"], prior, purchase)
                matched.append(dict(id=purchase["id"], style=purchase["style"], quantity=sum(l["quantity"] for l in changes)))
            return dict(matched=matched, skipped=skipped, before_snapshot_id=before_snapshot_id, after_snapshot_id=after_snapshot_id)

    def match_lingxing_shipment(self, purchase_id, payload):
        """Allocate verified external shipment units once; never infer from old inbound totals."""
        match_id = text_value(payload.get("match_id"), "核对编号", required=True, limit=200)
        source_file = text_value(payload.get("source_file"), "领星凭据文件", required=True, limit=1000)
        source = (self.path.parent / source_file).resolve()
        imports = (self.path.parent / "imports").resolve()
        if not source.is_relative_to(imports) or source.suffix != ".json" or not source.is_file():
            raise AppError("领星凭据必须是本库imports目录内的JSON文件")
        proof = json.loads(source.read_text(encoding="utf-8-sig"))
        expected_sid = os.environ.get("REPLENISHMENT_LINGXING_SID", "").strip()
        if not expected_sid:
            raise AppError("请先在本机设置REPLENISHMENT_LINGXING_SID店铺配置，再核对货件")
        if proof.get("source") != "lingxing" or str(proof.get("sid")) != expected_sid or proof.get("dispatch_confirmed") is not True:
            raise AppError("凭据必须确认领星货件已实际发货，库存总在途不能作为批次凭据")
        shipment_id = text_value(proof.get("shipment_id"), "领星货件编号", required=True, limit=200)
        observed = text_value(proof.get("observed_at"), "领星核实时刻", required=True)
        try:
            stamp = datetime.fromisoformat(observed)
            if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
                raise ValueError()
        except ValueError:
            raise AppError("核实时刻须为不晚于现在的含时区ISO时间") from None
        observed = stamp.astimezone(timezone.utc).isoformat()
        ship_date = iso_date(proof.get("ship_date"), "领星实际发货日期", optional=True)
        if ship_date and ship_date > stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat():
            raise AppError("实际发货日期不能晚于核实时刻")
        capacity = {}
        for line in proof.get("lines", []):
            sku = text_value(line.get("sku"), "领星MSKU", required=True, limit=300)
            if sku in capacity:
                raise AppError("领星凭据存在重复MSKU，请先逐码汇总核实")
            capacity[sku] = integer(line.get("quantity"), "货件已发数量", 1)
        note = text_value(payload.get("mapping_note"), "本采购与货件对应依据", required=True)
        incoming = payload.get("lines")
        if not isinstance(incoming, list) or not incoming:
            raise AppError("请提供本批对应的逐码已发数量")
        allocations = {}
        for line in incoming:
            sku = text_value(line.get("sku"), "SKU", required=True, limit=300)
            if sku in allocations:
                raise AppError("本次分配存在重复SKU")
            allocations[sku] = integer(line.get("quantity"), "本批对应已发数量", 1)
        record = dict(match_id=match_id, shipment_id=shipment_id, observed_at=observed,
                      ship_date=ship_date, source_file=source_file,
                      source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), mapping_note=note,
                      lines=[dict(sku=sku, quantity=qty) for sku, qty in sorted(allocations.items())])
        with self.connection(write=True) as conn:
            purchase = self._get(conn, "purchase", purchase_id)
            for prior in purchase.get("shipment_matches", []):
                if prior["match_id"] == match_id:
                    if any(prior.get(key) != value for key, value in record.items()):
                        raise AppError("同一核对编号内容不同", 409, "match_conflict")
                    return purchase
            require_revision(payload, purchase)
            if purchase.get("status") == "cancelled" or purchase.get("reconciled_snapshot_id"):
                raise AppError("已取消或已核对完成的采购不能分配发货", 409, "closed_purchase")
            claimed = {}
            for other in self._list(conn, "purchase"):
                # Cancelled/history claims remain consumed; never reuse physical units.
                for match in other.get("shipment_matches", []):
                    if match["shipment_id"] == shipment_id:
                        for line in match["lines"]:
                            claimed[line["sku"]] = claimed.get(line["sku"], 0) + line["quantity"]
            before = copy(purchase)
            by_sku = {line["sku"]: line for line in purchase["lines"]}
            for sku, qty in allocations.items():
                if sku not in by_sku or claimed.get(sku, 0) + qty > capacity.get(sku, 0):
                    raise AppError("货件数量不足、已分配给别批，或SKU不属于本采购", 409, "shipment_allocation_conflict")
                line = by_sku[sku]
                verified = line.get("system_shipped", 0) + qty
                if verified > line["ordered"]:
                    raise AppError("领星核对数量超过本批已购数量")
                line.update(system_shipped=verified, shipped=max(line["shipped"], verified))
            purchase.setdefault("shipment_matches", []).append(record | {"recorded_at": now()})
            if all(line.get("system_shipped", 0) == line["ordered"] for line in purchase["lines"]):
                purchase["shipment_archived_at"] = now()
            if ship_date:
                dates = [m["ship_date"] for m in purchase["shipment_matches"] if m.get("ship_date")]
                purchase["shipped_date"] = max(dates)
                purchase["ship_date_basis"] = "领星货件实际发货记录（拆分发货按最近一批日期）"
            if not any(line["received"] for line in purchase["lines"]):
                purchase["status"] = "shipped" if all(line["shipped"] == line["ordered"] for line in purchase["lines"]) else "partially_shipped"
            purchase.update(revision=purchase["revision"] + 1, updated_at=now())
            self._put(conn, "purchase", purchase_id, purchase)
            self._audit(conn, "purchase.lingxing-shipment", purchase_id, before, purchase)
            return purchase

    def record_historical_batch(self, batch_id, payload):
        """Save cumulative progress for an existing batch without creating new supply."""
        batch_id = text_value(batch_id, "历史批次编号", required=True, limit=200)
        if payload.get("confirmed") is not True:
            raise AppError("请确认这是原有批次的累计到货进度")
        style = text_value(payload.get("style"), "款式", required=True, limit=200)
        reference = text_value(payload.get("reference"), "批次名称", required=True, limit=300)
        total = integer(payload.get("total_quantity"), "原批次总量", 1)
        shipped = integer(payload.get("shipped_quantity"), "累计已发", 0, total)
        received_date = iso_date(payload.get("received_date"), "实际到货日期", optional=True)
        if received_date and received_date > datetime.now(timezone(timedelta(hours=8))).date().isoformat():
            raise AppError("实际到货日期不能晚于北京时间今天")
        source_note = text_value(payload.get("source_note"), "到货依据", required=True, limit=10000)
        evidence_id = text_value(payload.get("evidence_id"), "凭据编号", required=True, limit=200)
        incoming = payload.get("received_lines")
        if not isinstance(incoming, list) or not incoming:
            raise AppError("请提供累计已到货的颜色尺码数量")
        validated, seen = [], set()
        for line in incoming:
            if not isinstance(line, dict):
                raise AppError("到货配码格式无效")
            sku = text_value(line.get("sku"), "SKU", required=True, limit=300)
            if sku in seen:
                raise AppError("累计到货明细不能包含重复SKU")
            seen.add(sku)
            validated.append(dict(sku=sku, quantity=integer(line.get("quantity"), "累计到货双数", 1)))
        received = sum(line["quantity"] for line in validated)
        if received > shipped:
            raise AppError("累计已到不能超过累计已发")
        content = dict(style=style, reference=reference, total_quantity=total,
                       shipped_quantity=shipped, received_quantity=received,
                       received_lines=sorted(validated, key=lambda line: line["sku"]),
                       received_date=received_date, source_note=source_note)
        for field in ("shipped_date", "estimated_ship_date"):
            if field in payload:
                value = iso_date(payload.get(field), "实际发货日期" if field == "shipped_date" else "估算发货日期", optional=True)
                if value and value > datetime.now(timezone(timedelta(hours=8))).date().isoformat():
                    raise AppError("已发批次的发货日期不能晚于今天")
                content[field] = value
        if "ship_date_basis" in payload:
            content["ship_date_basis"] = text_value(payload["ship_date_basis"], "发货日期依据", required=True)
        if content.get("estimated_ship_date") and not content.get("shipped_date") and not content.get("ship_date_basis"):
            raise AppError("估算发货日期需说明依据")
        if content.get("shipped_date") and received_date and received_date < content["shipped_date"]:
            raise AppError("实际到货日期不能早于实际发货日期")
        digest = hashlib.sha256(dumps(content).encode("utf-8")).hexdigest()
        with self.connection(write=True) as conn:
            known = {row["sku"] for row in self._style_rows(conn, style)}
            if seen - known:
                raise AppError("到货明细含不属于该款的SKU")
            before = next((record for record in self._list(conn, "historical_batch") if record["id"] == batch_id), None)
            if before:
                for observation in before.get("observations", []):
                    if observation["evidence_id"] == evidence_id:
                        if observation["digest"] != digest:
                            raise AppError("同一凭据已有不同内容，请核对后用新凭据记录更正", 409, "evidence_conflict")
                        return before
                require_revision(payload, before)
                if before["style"] != style:
                    raise AppError("历史批次不能改为其他款式")
                effective_ship_date = content.get("shipped_date", before.get("shipped_date"))
                if effective_ship_date and received_date and received_date < effective_ship_date:
                    raise AppError("实际到货日期不能早于实际发货日期")
                previous = {line["sku"]: line["quantity"] for line in before["received_lines"]}
                current = {line["sku"]: line["quantity"] for line in validated}
                correction = total != before["total_quantity"] or shipped < before["shipped_quantity"] or any(
                    current.get(sku, 0) < quantity for sku, quantity in previous.items())
                if correction and payload.get("correction") is not True:
                    raise AppError("累计数量减少或原批总量改变，请明确标记更正并在依据中说明", 409, "correction_required")
            elif payload.get("revision") != 0 or isinstance(payload.get("revision"), bool):
                raise AppError("新历史批次请使用revision 0", 409, "revision_conflict")
            recorded_at = now()
            observations = copy(before.get("observations", [])) if before else []
            observations.append(dict(evidence_id=evidence_id, digest=digest, recorded_at=recorded_at,
                                     received_quantity=received, received_date=received_date))
            record = dict(content, id=batch_id, revision=before["revision"] + 1 if before else 1,
                          created_at=before["created_at"] if before else recorded_at,
                          updated_at=recorded_at, observations=observations)
            if before:
                for field in ("shipped_date", "estimated_ship_date", "ship_date_basis"):
                    if field not in content and field in before:
                        record[field] = before[field]
            self._put(conn, "historical_batch", batch_id, record)
            self._audit(conn, "historical_batch.progress", batch_id, before, record)
            return record

    def update_cycle(self, sku, payload):
        with self.connection(write=True) as conn:
            if sku not in {row["sku"] for row in self._rows(conn)}:
                raise AppError("SKU不存在", 404, "unknown_sku")
            cycle = self._get(conn, "cycle", sku)
            require_revision(payload, cycle)
            before = copy(cycle)
            baseline = payload.get("baseline")
            baseline_date = payload.get("baselineDate")
            baseline_kind = payload.get("baselineKind")
            if baseline is None and baseline_date in (None, ""):
                baseline, baseline_date, baseline_kind = None, None, "待确认"
            else:
                integer(baseline, "起点累计售出", 0, 100_000_000)
                iso_date(baseline_date, "观察起点日期")
                text_value(baseline_kind, "起点依据", required=True)
            cycle.update(baseline=baseline, baselineDate=baseline_date, baselineKind=baseline_kind,
                         revision=cycle["revision"] + 1, updated_at=now())
            self._put(conn, "cycle", sku, cycle)
            self._audit(conn, "cycle.update", sku, before, cycle)
            return cycle

    def feedback(self, payload):
        value = text_value(payload.get("text"), "意见", required=True, limit=10000)
        context = payload.get("context")
        if context is not None and not isinstance(context, (dict, str)):
            raise AppError("意见上下文格式无效")
        rec = dict(id="feedback_" + uuid.uuid4().hex, text=value, context=context, created_at=now())
        with self.connection(write=True) as conn:
            self._put(conn, "feedback", rec["id"], rec)
            self._audit(conn, "feedback.create", rec["id"], None, rec)
        return rec

    def audit(self):
        with self.connection() as conn:
            entries = []
            for row in conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500"):
                entry = dict(row)
                for key in ("before", "after", "source"):
                    entry[key] = json.loads(entry.pop(key + "_json"))
                entries.append(entry)
            return {"entries": entries}

    def backup(self):
        directory = self.path.parent / "backups"
        directory.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        output = directory / f"replenishment_{stamp}_{uuid.uuid4().hex[:8]}.sqlite3"
        source = sqlite3.connect(str(self.path), timeout=15)
        destination = sqlite3.connect(str(output))
        try:
            source.backup(destination)
            result = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise AppError("备份完整性检查未通过", 500, "backup_failed")
        finally:
            destination.close()
            source.close()
        info = {"path": str(output), "created_at": now()}
        with self.connection(write=True) as conn:
            self._audit(conn, "backup.create", output.name, None, info)
        return info

    def export_csv(self, plan_id):
        with self.connection() as conn:
            plan = self._get(conn, "plan", plan_id)
            by_sku = {row["sku"]: row for row in self._rows(conn)}
            preference = self._style_preferences(conn).get(plan["style"], {})
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(["方案", "款式", "颜色", "美码 US", "SKU", "选定双数", "来源", "备注", "目标天数", "状态", "中国码", "参考采购单价人民币", "预计货款人民币", "采购价依据"])
            for line in plan["lines"]:
                row = by_sku.get(line["sku"], {})
                cents = purchase_unit_cents(row, preference)
                values = [plan["name"], plan["style"], row.get("colorLabel", ""), row.get("size", ""),
                          line["sku"], line["quantity"], "手动" if line["manual"] else "建议",
                          line["note"], plan["target_days"], "已取消采购登记" if plan["status"] == "cancelled" else "已登记采购" if plan["status"] == "confirmed" else "草稿，未采购",
                          row.get("cnSize") or "待确认", cents / 100 if cents is not None else "待填",
                          f"{cents * line['quantity'] / 100:.2f}" if cents is not None else "待核算",
                          preference.get("purchase_price_note", "仅核算货款，运费及额外费用另计")]
                writer.writerow([("'" + v) if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
                                 else v for v in values])
            return ("\ufeff" + stream.getvalue()).encode("utf-8")
