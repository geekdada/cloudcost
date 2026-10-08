import argparse
from importlib.resources import files
import json
import logging
import os
from pathlib import Path
import threading
from . import __version__
from .config import load_config
from .db import Database
from .models import current_month, month_key
from .service import check, collect_all, safe_error, summary


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser():
    root = argparse.ArgumentParser(prog="cloudcost", description="当月未出账费用监控 / SQLite / 多渠道预算报警")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("-c", "--config", default="config.toml", help="TOML 配置文件（默认 config.toml）")
    subs = root.add_subparsers(dest="command", required=True)
    init = subs.add_parser("init", help="生成配置；--demo 创建可直接查询的演示数据")
    init.add_argument("--demo", action="store_true")
    init.add_argument("--force", action="store_true", help="覆盖现有配置")
    for command, help_text in [("collect", "采集一轮当月未出账费用"), ("check", "判断阈值并报警"),
                               ("status", "查询月度累计费用"), ("history", "查询采集快照"),
                               ("alerts", "查询报警及投递记录")]:
        sub = subs.add_parser(command, help=help_text)
        sub.add_argument("--month", default=None, help="YYYY-MM，默认 UTC 当月；原生采集只支持当月")
        if command == "collect":
            sub.add_argument("--check", action="store_true", help="采集后执行预算判断")
        if command in {"check", "collect"}:
            sub.add_argument("--dry-run", action="store_true", help="只预览报警，不写报警状态或投递通知")
        if command == "history":
            sub.add_argument("--provider")
            sub.add_argument("--limit", type=int, default=100)
    run = subs.add_parser("run", help="常驻进程：立即采集并按间隔循环采集/判断预算")
    run.add_argument("--once", action="store_true", help="执行一轮后退出，适合 cron")
    run.add_argument("--dry-run", action="store_true")
    serve = subs.add_parser("serve", help="启动只读 API 与 SPA")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--token-env", default="CLOUDCOST_API_TOKEN", help="API Bearer token 的环境变量名")
    serve.add_argument("--monitor", action="store_true", help="同时运行后台定时采集与报警")
    for sub in subs.choices.values():
        sub.add_argument("-c", "--config", default=argparse.SUPPRESS)
    return root


def run_loop(config, db, stop, dry_run=False):
    while not stop.is_set():
        try:
            month = current_month()  # Recompute after every wakeup for month rollover.
            collect_all(config, db, month)
            check(config, db, month, dry_run)
        except Exception as e:
            logging.getLogger("cloudcost").error("监控循环失败：%s", safe_error(e))
        stop.wait(config.interval_seconds)


def one_round(config, db, dry_run=False):
    month = current_month()
    collections = collect_all(config, db, month)
    alerts = check(config, db, month, dry_run)
    output({"collections": collections, "alerts": alerts})
    return int(any(not row["ok"] for row in collections) or any(not row["ok"] for row in alerts["deliveries"]) or bool(alerts["skipped"]))


def main(argv=None):
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Signed Aliyun URLs and bot webhook URLs contain secrets. HTTP libraries
    # log complete URLs at INFO, so keep their logs quiet in CLI/server mode.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.command == "init":
            path = Path(args.config).expanduser().resolve()
            if path.exists() and not args.force:
                raise ValueError("配置已存在；请指定新路径或显式使用 --force")
            path.parent.mkdir(parents=True, exist_ok=True)
            if args.demo:
                from .demo import initialize
                initialize(path)
            else:
                path.write_text(files("cloudcost").joinpath("config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
            print(f"配置已创建：{path}")
            if args.demo:
                print(f"演示数据已创建，运行：cloudcost serve -c {path}")
            return 0
        config = load_config(args.config)
        db = Database(config.database)
        month = month_key(getattr(args, "month", None) or current_month())
        if args.command == "collect":
            rows = collect_all(config, db, month)
            result = {"collections": rows}
            if args.check:
                result["alerts"] = check(config, db, month, args.dry_run)
            output(result)
            return int(any(not r["ok"] for r in rows) or any(not r["ok"] for r in result.get("alerts", {}).get("deliveries", [])) or bool(result.get("alerts", {}).get("skipped")))
        if args.command == "check":
            result = check(config, db, month, args.dry_run)
            output(result)
            return int(any(not d["ok"] for d in result["deliveries"]) or bool(result["skipped"]))
        if args.command == "status":
            output(summary(config, db, month))
        elif args.command == "history":
            if not 1 <= args.limit <= 5000:
                raise ValueError("limit 必须为 1–5000")
            if args.provider:
                config.provider(args.provider)
            output(db.history(month, args.provider, args.limit))
        elif args.command == "alerts":
            output(db.alerts(month))
        elif args.command == "run":
            if args.once:
                return one_round(config, db, args.dry_run)
            run_loop(config, db, threading.Event(), args.dry_run)
        elif args.command == "serve":
            import uvicorn
            from .api import create_app
            token = os.environ.get(args.token_env)
            if args.host not in {"127.0.0.1", "::1", "localhost"} and not token:
                raise ValueError(f"对外监听须配置 {args.token_env}，用于保护账单查询 API")
            stop, thread = threading.Event(), None
            if args.monitor:
                thread = threading.Thread(target=run_loop, args=(config, db, stop), daemon=True)
                thread.start()
            try:
                uvicorn.run(create_app(config, token), host=args.host, port=args.port)
            finally:
                stop.set()
                if thread:
                    thread.join(timeout=5)
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, FileNotFoundError) as error:
        # These are local config errors; no SDK response or credential is printed.
        logging.error("%s", error if isinstance(error, ValueError) else "配置文件不存在")
        return 2
    except Exception as error:
        logging.error("%s", safe_error(error))
        return 2
