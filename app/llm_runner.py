"""Local summary runner (Apple Silicon, MLX), executed with the summary venv's python.

Usage: python llm_runner.py --input job.json --out summary.md
       python llm_runner.py --download-only
job.json: {"transcript": str, "language": "uk|en|same", "title": str}
Prints "PROGRESS <0-100>" lines; writes the Markdown summary body to --out.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import summary_prompts  # noqa: E402

MODEL = os.environ.get("YTGRAB_LLM_MODEL", "mlx-community/Qwen3-4B-Instruct-2507-4bit")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    try:
        from mlx_lm import generate, load

        model, tokenizer = load(MODEL)
        if args.download_only:
            print("DONE", flush=True)
            return 0
        try:
            from mlx_lm.sample_utils import make_sampler
            sampling = {"sampler": make_sampler(temp=0.3, top_p=0.9)}
        except Exception:  # older/newer mlx-lm without this helper: default sampling
            sampling = {}

        def chat(messages: list, max_tokens: int) -> str:
            prompt = tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)
            return generate(model, tokenizer, prompt=prompt, max_tokens=max_tokens, verbose=False, **sampling)

        with open(args.input, encoding="utf-8") as fh:
            job = json.load(fh)
        summary = summary_prompts.summarize(
            chat, job["transcript"], job.get("language", "uk"), job.get("title", ""),
            lambda p: print(f"PROGRESS {p:.1f}", flush=True))
    except Exception as exc:  # report to the parent process in one line
        print(f"ERROR {type(exc).__name__}: {exc}", flush=True)
        return 1
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(summary)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
