import argparse
import json
from pathlib import Path

from lab.config import load_settings
from lab.engine import Engine, safe_error


def main():
    parser = argparse.ArgumentParser(description="LightRAG + Qdrant 检索观察台")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="下载本地模型、初始化知识库")
    ingest = sub.add_parser("ingest", help="导入 TXT/Markdown，只建立文本块向量，不抽取图谱")
    ingest.add_argument("paths", nargs="+")
    query = sub.add_parser("query", help="运行一次检索并保存真实记录")
    query.add_argument("question")
    query.add_argument("--top-k", type=int, default=5)
    query.add_argument("--threshold", type=float, default=0.2)
    args = parser.parse_args()
    settings = load_settings()
    engine = Engine(settings)
    try:
        info = engine.ready.result(timeout=600)
        if args.command == "ingest":
            paths = []
            for value in args.paths:
                path = Path(value).expanduser()
                if path.is_dir():
                    paths.extend(p for p in sorted(path.iterdir()) if p.suffix.lower() in {".txt", ".md"})
                elif path.is_file() and path.suffix.lower() in {".txt", ".md"}:
                    paths.append(path)
                else:
                    raise ValueError(f"不是 TXT/Markdown 文件或目录：{path}")
            count = engine.import_files(paths).result(timeout=600)
            print(f"已导入 {count} 个文本块。")
        elif args.command == "query":
            future, trace = engine.submit(args.question, args.top_k, args.threshold)
            record = future.result(timeout=180)
            print(json.dumps({"query_id": trace.id, "status": record["status"], "hits": record["hits"], "request": record["request"]}, ensure_ascii=False, indent=2))
            print(f"记录：{settings.run_dir / (trace.id + '.json')}")
            if record["status"] == "failed":
                raise ValueError(record["error"])
        else:
            print(json.dumps(info, ensure_ascii=False, indent=2))
    except Exception as exc:
        raise SystemExit(safe_error(exc, settings)) from None
    finally:
        engine.close()


if __name__ == "__main__":
    main()
