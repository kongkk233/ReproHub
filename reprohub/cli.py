"""命令行入口：reprohub <command>"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys

from . import __version__, repo
from .db import get_conn
from .llm import EFFORT_LEVELS, LLM, LLMError, build_provider, clear_overrides, llm_status, load_overrides, provider_catalog, set_overrides, set_process_overrides, test_llm
from .settings import ROOT, get_settings


def _print(msg: str) -> None:
    print(msg, flush=True)


def _banner(settings) -> None:
    from .pipeline.runner import mock_banner

    conn = get_conn(settings)
    b = mock_banner(settings, conn)
    if b:
        _print(b)
    st = llm_status(settings, conn)
    if st.get("mode") == "live":
        _print(f"LLM：{st.get('label')} / {st.get('model')} · 强度 {st.get('effort') or '不适用'}")


def cmd_init(args, settings):
    env = ROOT / ".env"
    if not env.exists() and (ROOT / ".env.example").exists():
        shutil.copy(ROOT / ".env.example", env)
        _print(f"已创建 {env}（请填入 DEEPSEEK_API_KEY 等）")
    conn = get_conn(settings)
    repo.sync_sources(conn, settings)
    _print(f"数据库：{settings.db_path}")
    _print(f"来源：{len(repo.list_sources(conn))} 个（启用 {sum(1 for s in repo.list_sources(conn) if s['enabled'])}）")
    cmd_status(args, settings)


def cmd_status(args, settings):
    conn = get_conn(settings)
    st = llm_status(settings, conn)
    _print(f"ReproHub v{__version__}")
    _print(f"LLM：{st['mode']}  provider={st.get('key') or st.get('provider')} model={st.get('model')} effort={st.get('effort')}" + (f"  原因：{st.get('reason')}" if st.get("reason") else ""))
    c = repo.counts(conn)
    _print(f"条目 {c['items']}（待评分 {c['new']}，已评分 {c['scored']}，入选 {c['selected']}，已过滤 {c['rejected']}，模拟评分 {c['mock_scored']}）")
    _print(f"解读 {c['interpretations']}（模拟 {c['mock_interpretations']}）· 事件 {c['stories']} · 版次 {c['editions']} · 未读精选 {c['unread_selected']} · 缓存 {c['cache_entries']} 条")
    for r in repo.recent_runs(conn, 3):
        _print(f"运行 #{r['id']} {r['started_at'][:16]} {r['status']}")


def cmd_collect(args, settings):
    from .pipeline.collect import collect

    conn = get_conn(settings)
    s = collect(settings, conn, source_keys=args.source or None, force=args.force, since_days=args.days, logger=_print)
    _print(f"采集完成：{s['sources']} 个来源，新增 {s['new']}，重复 {s['duplicate']}，领域外 {s['dropped']}，跳过 {s['skipped']}，错误 {len(s['errors'])}")


def cmd_process(args, settings):
    from .pipeline.score import process_new

    _banner(settings)
    conn = get_conn(settings)
    process_new(settings, conn, LLM(settings, conn), limit=args.limit, retry_errors=args.retry_errors, upgrade_mock=(False if args.no_upgrade_mock else None), logger=_print)


def cmd_upgrade(args, settings):
    from .pipeline.score import upgrade_mock_scores

    _banner(settings)
    conn = get_conn(settings)
    r = upgrade_mock_scores(settings, conn, LLM(settings, conn), limit=args.limit, logger=_print)
    if not r["processed"]:
        _print("没有需要升级的模拟评分（或当前仍是模拟模式）")


def cmd_interpret(args, settings):
    from .pipeline.interpret import auto_interpret, interpret_item

    _banner(settings)
    conn = get_conn(settings)
    llm = LLM(settings, conn)
    if args.item:
        it = repo.get_item(conn, args.item)
        if not it:
            sys.exit(f"条目 {args.item} 不存在")
        try:
            r = interpret_item(settings, conn, llm, it, force=args.force)
        except LLMError as e:
            sys.exit(f"解读失败：{e}")
        _print(r["markdown"] if r else "无")
    else:
        auto_interpret(settings, conn, llm, logger=_print)


def cmd_cluster(args, settings):
    from .pipeline.cluster import cluster_recent

    conn = get_conn(settings)
    cluster_recent(settings, conn, LLM(settings, conn), logger=_print)


def cmd_edition(args, settings):
    from .pipeline.editions import build_daily, build_monthly, build_weekly
    from .push.dispatcher import push_edition

    _banner(settings)
    conn = get_conn(settings)
    llm = LLM(settings, conn)
    builder = {"daily": build_daily, "weekly": build_weekly, "monthly": build_monthly}[args.kind]
    ed = builder(settings, conn, llm, args.key, logger=_print)
    if args.push:
        push_edition(settings, conn, ed, logger=_print, force=True)
    if args.print:
        _print(ed["markdown"])
    else:
        _print(f"已生成：/editions/{ed['kind']}/{ed['date_key']}")


def cmd_pipeline(args, settings):
    from .pipeline.runner import run_pipeline

    conn = get_conn(settings)
    s = run_pipeline(
        settings, conn, do_collect=not args.no_collect, do_process=not args.no_process, do_interpret=not args.no_interpret,
        do_cluster=not args.no_cluster, do_daily=not args.no_daily, do_push=not args.no_push, force=args.force, source_keys=args.source or None, logger=_print,
    )
    if s.get("error"):
        sys.exit(1)


def cmd_push_test(args, settings):
    from .push.dispatcher import test_channel

    conn = get_conn(settings)
    _print(json.dumps(test_channel(settings, conn, args.channel), ensure_ascii=False))


def cmd_serve(args, settings):
    import os

    import uvicorn

    if args.scheduler:
        os.environ["REPROHUB_SCHEDULER"] = "1"
    _banner(settings)
    host = args.host or settings.env("HOST", "127.0.0.1")
    port = args.port or int(settings.env("PORT", "8008"))
    _print(f"http://{host}:{port}  （后台 /admin，API /docs）")
    uvicorn.run("reprohub.web.app:create_app", factory=True, host=host, port=port, reload=args.reload, log_level="info")


def cmd_run_forever(args, settings):
    from .scheduler import Scheduler

    _banner(settings)
    Scheduler(settings).run_forever()


def cmd_demo_seed(args, settings):
    from .demo import seed

    conn = get_conn(settings)
    n = seed(settings, conn)
    _print(f"已写入 {n} 条演示样例（来源 demo）。接着运行 `reprohub pipeline --no-collect` 完成评分/解读/日报。")


def cmd_llm(args, settings):
    conn = get_conn(settings)
    sub = args.llm_cmd
    if sub == "status":
        st = llm_status(settings, conn)
        _print(f"全局：{st['mode']}  {st.get('key') or st.get('provider')} / {st.get('model')}  强度 {st.get('effort')}  来源 {st.get('sources')}")
        if st.get("overrides"):
            _print(f"运行时覆盖：{st['overrides']}")
        _print("各步骤：")
        for step, v in st.get("steps", {}).items():
            flag = "mock" if v.get("is_mock") else ("ERR " + v["error"] if v.get("error") else "live")
            _print(f"  {step:<10} {v.get('provider'):<10} {str(v.get('model')):<24} 强度 {str(v.get('effort')):<7} [{flag}]")
        _print("提供商：")
        for p in st.get("providers", []):
            _print(f"  {'✓' if p['configured'] else '·'} {p['key']:<10} {p['label']:<28} 默认 {p['model'] or '-':<22} 强度 {p['effort_style']:<7} 已知模型 {len(p['models'])}")
    elif sub == "test":
        targets = []
        if args.all:
            targets = [(p["key"], None) for p in provider_catalog(settings) if p["configured"] and p["key"] != "mock"]
        else:
            targets = [(args.provider, args.model)]
        for prov, model in targets:
            r = test_llm(settings, conn, provider=prov, model=model, effort=args.effort, prompt=args.prompt)
            if r.get("ok"):
                tag = "模拟" if r.get("is_mock") else "真实"
                _print(f"✓ [{tag}] {r['provider']}/{r['model']} 强度={r.get('effort')} {r['latency_ms']} ms  tokens in/out/reasoning = {r.get('prompt_tokens')}/{r.get('completion_tokens')}/{r.get('reasoning_tokens')}")
                _print(f"    请求附加字段：{r.get('request_extra')}")
                _print(f"    回复：{r['content'][:160]!r}")
            else:
                _print(f"✗ {r.get('provider')}/{r.get('model')} 强度={r.get('effort')}：{r.get('error')}")
    elif sub == "set":
        cur = set_overrides(conn, provider=args.provider, model=args.model, effort=args.effort)
        _print(f"运行时覆盖已保存：{cur}（优先于 models.yaml 与环境变量；`reprohub llm clear` 清除）")
        cmd_llm(argparse.Namespace(llm_cmd="status"), settings)
    elif sub == "clear":
        clear_overrides(conn)
        _print("运行时覆盖已清除")
    elif sub == "cache":
        if args.clear:
            _print(f"已清除 {repo.cache_clear(conn, args.task)} 条缓存")
        st = repo.cache_stats(conn)
        _print(f"缓存 {st['entries']} 条，累计命中 {st['hits']} 次")
        for t, v in st["by_task"].items():
            _print(f"  {t:<10} {v['entries']:>5} 条  命中 {v['hits']:>5}")
    elif sub == "models":
        key = args.provider or llm_status(settings, conn).get("key")
        p = build_provider(settings, key)
        live = p.list_models() if hasattr(p, "list_models") else []
        known = next((x["models"] for x in provider_catalog(settings) if x["key"] == key), [])
        _print(f"{key} 配置中已知：{', '.join(known) or '-'}")
        _print(f"{key} 端点返回：{', '.join(live) or '（不可用或为模拟）'}")


def cmd_sources(args, settings):
    conn = get_conn(settings)
    repo.sync_sources(conn, settings)
    for s in repo.source_stats(conn):
        flag = "✓" if s["enabled"] else "·"
        _print(f"{flag} {s['key']:<28} {s['type']:<10} T{s['tier']}  条目 {s['n_items']:>4}  上次 {str(s['last_fetch_at'] or '')[:16]:<16} {s['last_status'] or ''} {('— ' + s['last_error'][:60]) if s['last_error'] else ''}")


def cmd_export(args, settings):
    conn = get_conn(settings)
    rows = repo.reading_export(conn)
    _print(json.dumps(rows, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reprohub", description="生殖医学 · 辅助生殖 · 胚胎多组学 文献聚合、解读、阅读管理与推送")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--provider", help="本次运行临时使用的提供商（deepseek / opus / x666 / … / mock）")
    p.add_argument("--model", help="本次运行临时使用的模型，可写 provider/model")
    p.add_argument("--effort", choices=list(EFFORT_LEVELS), help="本次运行的思考强度")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="初始化 .env / 数据库").set_defaults(fn=cmd_init)
    sub.add_parser("status", help="状态与 LLM 模式").set_defaults(fn=cmd_status)

    c = sub.add_parser("collect", help="采集来源")
    c.add_argument("--source", action="append", help="只采集指定来源 key（可重复）")
    c.add_argument("--force", action="store_true", help="忽略采集间隔")
    c.add_argument("--days", type=int, help="回溯天数")
    c.set_defaults(fn=cmd_collect)

    c = sub.add_parser("process", help="预筛与评分")
    c.add_argument("--limit", type=int, default=300)
    c.add_argument("--retry-errors", action="store_true")
    c.add_argument("--no-upgrade-mock", action="store_true", help="不把模拟评分升级为真实评分")
    c.set_defaults(fn=cmd_process)

    c = sub.add_parser("upgrade", help="用真实模型重评仍是模拟评分的条目（已入选/偏好高/新发表优先）")
    c.add_argument("--limit", type=int)
    c.set_defaults(fn=cmd_upgrade)

    c = sub.add_parser("interpret", help="生成文献解读")
    c.add_argument("--item", type=int)
    c.add_argument("--force", action="store_true")
    c.set_defaults(fn=cmd_interpret)

    sub.add_parser("cluster", help="事件聚类与热度").set_defaults(fn=cmd_cluster)

    c = sub.add_parser("edition", help="生成日报/周报/月报")
    c.add_argument("kind", choices=["daily", "weekly", "monthly"])
    c.add_argument("--key", help="daily=YYYY-MM-DD, weekly=YYYY-Www, monthly=YYYY-MM")
    c.add_argument("--push", action="store_true")
    c.add_argument("--print", action="store_true", help="打印 Markdown")
    c.set_defaults(fn=cmd_edition)

    c = sub.add_parser("pipeline", help="完整流水线")
    for flag in ("collect", "process", "interpret", "cluster", "daily", "push"):
        c.add_argument(f"--no-{flag}", action="store_true")
    c.add_argument("--force", action="store_true")
    c.add_argument("--source", action="append")
    c.set_defaults(fn=cmd_pipeline)

    c = sub.add_parser("push-test", help="测试推送渠道")
    c.add_argument("channel")
    c.set_defaults(fn=cmd_push_test)

    c = sub.add_parser("serve", help="启动网站")
    c.add_argument("--host")
    c.add_argument("--port", type=int)
    c.add_argument("--reload", action="store_true")
    c.add_argument("--scheduler", action="store_true", help="同时启动后台调度")
    c.set_defaults(fn=cmd_serve)

    sub.add_parser("run-forever", help="只跑调度（配合 cron/systemd）").set_defaults(fn=cmd_run_forever)

    c = sub.add_parser("llm", help="查看/测试/切换 LLM 提供商、模型与思考强度")
    ls = c.add_subparsers(dest="llm_cmd", required=True)
    ls.add_parser("status", help="解析结果：全局与各步骤用什么")
    t = ls.add_parser("test", help="真实调用一次并报告延迟/tokens/推理 tokens")
    t.add_argument("--provider"); t.add_argument("--model"); t.add_argument("--effort", choices=list(EFFORT_LEVELS)); t.add_argument("--prompt")
    t.add_argument("--all", action="store_true", help="测试所有已配置 key 的提供商")
    st = ls.add_parser("set", help="保存运行时覆盖（优先于 models.yaml 与环境变量）")
    st.add_argument("--provider"); st.add_argument("--model"); st.add_argument("--effort", choices=list(EFFORT_LEVELS))
    ls.add_parser("clear", help="清除运行时覆盖")
    cc = ls.add_parser("cache", help="查看/清理真实模型输出缓存")
    cc.add_argument("--clear", action="store_true"); cc.add_argument("--task")
    m = ls.add_parser("models", help="列出提供商的已知模型与端点返回的模型")
    m.add_argument("--provider")
    c.set_defaults(fn=cmd_llm)
    sub.add_parser("demo-seed", help="写入离线演示样例").set_defaults(fn=cmd_demo_seed)
    sub.add_parser("sources", help="列出来源状态").set_defaults(fn=cmd_sources)
    sub.add_parser("export-reading", help="导出阅读记录 JSON").set_defaults(fn=cmd_export)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if getattr(args, "provider", None) or getattr(args, "model", None) or getattr(args, "effort", None):
        if args.cmd != "llm" or args.llm_cmd not in ("test", "set", "models"):
            set_process_overrides(provider=args.provider, model=args.model, effort=args.effort)
    args.fn(args, settings)


if __name__ == "__main__":
    main()
