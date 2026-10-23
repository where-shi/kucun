"""Direct database interface for assisted workflows; no browser automation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend import DEFAULT_DB, DEFAULT_SEED, AppError, Store


def build_parser():
    parser = argparse.ArgumentParser(description="鞋款补货本地读写接口。保存草稿不等于已采购。")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    common.add_argument("--seed", type=Path, default=argparse.SUPPRESS)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "bootstrap", "audit", "backup"):
        subs.add_parser(name, parents=[common])
    recommend = subs.add_parser("recommend", parents=[common])
    recommend.add_argument("--style")
    recommend.add_argument("--days", type=int, default=35)
    for name in ("new-plan", "update-plan", "adopt", "confirm", "shipment", "receipt", "reconcile", "cancel-purchase", "match-shipment", "cycle", "feedback", "historical-batch", "planning-settings", "style-preference"):
        command = subs.add_parser(name, parents=[common])
        command.add_argument("--json-file", type=Path, help="UTF-8 JSON请求文件；也可用--json")
        command.add_argument("--json", help="JSON请求内容")
        if name not in ("new-plan", "feedback", "planning-settings"):
            command.add_argument("id", help="方案ID、采购ID或SKU")
    imp = subs.add_parser("import", parents=[common])
    imp.add_argument("--json-file", type=Path, required=True)
    exp = subs.add_parser("export", parents=[common])
    exp.add_argument("id")
    exp.add_argument("--output", type=Path, required=True)
    return parser


def read_payload(args):
    if args.json_file is not None and args.json is not None:
        raise AppError("--json-file与--json只能选一个")
    if args.json_file is not None:
        payload = json.loads(args.json_file.read_text(encoding="utf-8-sig"))
    elif args.json is not None:
        payload = json.loads(args.json)
    else:
        raise AppError("请提供--json-file或--json")
    if not isinstance(payload, dict):
        raise AppError("请求JSON必须是对象")
    return payload


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        store = Store(args.db, args.seed, actor="assistant-cli")
        command = args.command
        if command in ("init", "status"):
            state = store.bootstrap()
            result = {"metadata": state["metadata"], "rows": len(state["rows"]),
                      "plans": len(state["plans"]), "purchases": len(state["purchases"]), "db": str(store.path)}
        elif command == "bootstrap":
            result = store.bootstrap()
        elif command == "recommend":
            result = store.recommend(args.style, args.days)
        elif command == "audit":
            result = store.audit()
        elif command == "backup":
            result = store.backup()
        elif command == "import":
            document = json.loads(args.json_file.read_text(encoding="utf-8-sig"))
            result = store.import_snapshot(document.get("document", document))
        elif command == "export":
            if args.output.exists():
                raise AppError("导出文件已存在，请换一个文件名")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(store.export_csv(args.id))
            result = {"path": str(args.output.resolve())}
        else:
            payload = read_payload(args)
            if command == "new-plan":
                result = store.create_plan(payload)
            elif command == "feedback":
                result = store.feedback(payload)
            elif command == "update-plan":
                result = store.update_plan(args.id, payload)
            elif command == "adopt":
                result = store.adopt_plan(args.id, payload)
            elif command == "confirm":
                result = store.confirm_plan(args.id, payload)
            elif command in ("shipment", "receipt"):
                payload["type"] = "shipped" if command == "shipment" else "received"
                result = store.purchase_event(args.id, payload)
            elif command == "reconcile":
                result = store.reconcile_purchase(args.id, payload)
            elif command == "cancel-purchase":
                result = store.cancel_purchase(args.id, payload)
            elif command == "match-shipment":
                result = store.match_lingxing_shipment(args.id, payload)
            elif command == "cycle":
                result = store.update_cycle(args.id, payload)
            elif command == "style-preference":
                result = store.update_style_preference(args.id, payload)
            elif command == "historical-batch":
                result = store.record_historical_batch(args.id, payload)
            elif command == "planning-settings":
                result = store.update_planning_settings(payload)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (AppError, OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "code": getattr(error, "code", "local_error")},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
