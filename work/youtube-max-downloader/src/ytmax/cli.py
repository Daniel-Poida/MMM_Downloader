from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from dataclasses import asdict
from typing import Any

from .ai_intent import parse_intent_with_ai
from .api_keys import provider_for_secret
from .downloader import YouTubeEngine
from .intent import parse_intent
from .runtime import configure_frozen_runtime


def _emit(event: dict[str, Any]) -> None:
    message = event.get("message")
    if message and event.get("kind") not in {"progress"}:
        print(message, flush=True)
    elif event.get("kind") == "progress" and message:
        print(f"\r{message:70}", end="", flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mmm-downloader",
        description="YouTube → MP4/H.264 downloader for Windows",
    )
    parser.add_argument("request", nargs="*", help="URL(s) or natural-language request")
    parser.add_argument("--to", dest="output", help="Output directory")
    parser.add_argument(
        "--quality",
        choices=("compatible", "max"),
        default="compatible",
        help="compatible = native H.264 (default); max = full resolution with transcoding",
    )
    parser.add_argument("--cookies-from-browser", choices=("chrome", "edge", "firefox"))
    parser.add_argument("--preview", action="store_true", help="Search, but do not download")
    parser.add_argument(
        "--ai",
        action="store_true",
        help="Parse the request with OpenAI or OpenRouter (key in OPENAI_API_KEY)",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Model id; defaults to the provider's own default",
    )
    parser.add_argument("--json", action="store_true", help="Print preview as JSON")
    parser.add_argument("--gui", action="store_true", help="Open the graphical interface")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_frozen_runtime()
    args = _parser().parse_args(argv)
    if args.gui or not args.request:
        from .gui import run_gui

        run_gui()
        return 0
    text = " ".join(args.request).strip()
    if not args.output:
        print("Ошибка: укажите папку через --to", file=sys.stderr)
        return 2
    try:
        if args.ai:
            api_key = os.environ.get("OPENAI_API_KEY") or ""
            intent = parse_intent_with_ai(
                text,
                api_key=api_key,
                provider=provider_for_secret(api_key),
                model=args.model,
                output_path=args.output,
            )
        else:
            intent = parse_intent(text, output_path=args.output)
        engine = YouTubeEngine(
            output_dir=args.output,
            quality_mode="true_max_h264" if args.quality == "max" else "native_h264",
            cookie_browser=args.cookies_from_browser or "",
            emit=_emit,
            cancel_event=threading.Event(),
        )
        candidates = engine.search(intent) if intent.topic else []
        if args.preview:
            if args.json:
                print(
                    json.dumps([asdict(item) for item in candidates], ensure_ascii=False, indent=2)
                )
            else:
                for index, item in enumerate(candidates, 1):
                    print(f"{index:>2}. {item.title} — {item.watch_url}")
            return 0
        urls = list(intent.urls) or [item.watch_url for item in candidates]
        if not urls:
            print("Подходящие видео не найдены", file=sys.stderr)
            return 1
        engine.download_urls(urls)
        print()
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
