#!/usr/bin/env python3
"""Operate Jarvis's guarded, local-only model improvement pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from local_runtime.local_improvement import GuardedImprovementPipeline


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--root", type=Path, default=None, help="Override artifact root (tests/ops only)")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("dry-run")
    commands.add_parser("fleet")

    capture = commands.add_parser("capture")
    capture.add_argument("--payload", type=Path, required=True, help="Explicit approved-example JSON")
    capture.add_argument("--dry-run", action="store_true")

    curate = commands.add_parser("curate")
    curate.add_argument("--semantic-threshold", type=float, default=0.985)

    split = commands.add_parser("split")
    split.add_argument("--curated", type=Path, required=True)
    split.add_argument("--seed", type=int, default=42)

    teach = commands.add_parser("teach")
    teach.add_argument("--dataset-id", required=True)

    train = commands.add_parser("train")
    train.add_argument("--dataset-id", required=True)
    train.add_argument("--teach-dir", type=Path, required=True)
    train.add_argument("--human-approved", action="store_true")
    train.add_argument("--iters", type=int, default=400)
    train.add_argument("--resume-adapter-file", type=Path, default=None)
    train.add_argument("--run-id", default=None, help="Resume/replay one guarded run ID")
    train.add_argument("--dry-run", action="store_true")

    export = commands.add_parser("export-candidate")
    export.add_argument("--run-id", required=True)
    export.add_argument("--adapter-path", type=Path, required=True)
    export.add_argument("--human-approved", action="store_true")
    export.add_argument("--confirmation", required=True)
    export.add_argument("--dry-run", action="store_true")

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--candidate", required=True)
    evaluate.add_argument("--candidate-digest", required=True)
    evaluate.add_argument("--baseline", default="jarvis-local:latest")

    approve = commands.add_parser("approve")
    approve.add_argument("--eval-id", required=True)
    approve.add_argument("--confirmation", required=True)
    approve.add_argument("--approver", required=True)

    canary = commands.add_parser("canary")
    canary.add_argument("--eval-id", required=True)
    canary.add_argument("--prompt", action="append", required=True)

    plan = commands.add_parser("promotion-plan")
    plan.add_argument("--eval-id", required=True)

    promote = commands.add_parser("promote")
    promote.add_argument("--eval-id", required=True)
    promote.add_argument("--confirmation", required=True)

    rollback = commands.add_parser("rollback")
    rollback.add_argument("--confirmation", required=True)
    return root


def execute(args: argparse.Namespace) -> dict:
    pipeline = GuardedImprovementPipeline(root=args.root)
    if args.command == "status":
        return {"ok": True, "status": pipeline.status()}
    if args.command == "dry-run":
        return pipeline.dry_run()
    if args.command == "fleet":
        return pipeline.verify_fleet()
    if args.command == "capture":
        payload = json.loads(args.payload.read_text(encoding="utf-8"))
        return pipeline.capture(payload, dry_run=args.dry_run)
    if args.command == "curate":
        return pipeline.curate(semantic_threshold=args.semantic_threshold)
    if args.command == "split":
        return pipeline.split(args.curated, seed=args.seed)
    if args.command == "teach":
        return pipeline.teach(args.dataset_id)
    if args.command == "train":
        return pipeline.train(
            args.dataset_id,
            args.teach_dir,
            human_approved=args.human_approved,
            num_iters=args.iters,
            resume_adapter_file=args.resume_adapter_file,
            run_id=args.run_id,
            dry_run=args.dry_run,
        )
    if args.command == "export-candidate":
        return pipeline.export_candidate(
            args.run_id,
            args.adapter_path,
            human_approved=args.human_approved,
            confirmation=args.confirmation,
            dry_run=args.dry_run,
        )
    if args.command == "evaluate":
        return pipeline.evaluate(
            args.candidate,
            candidate_digest=args.candidate_digest,
            baseline_tag=args.baseline,
        )
    if args.command == "approve":
        return pipeline.approve(args.eval_id, args.confirmation, approver=args.approver)
    if args.command == "canary":
        return pipeline.canary(args.eval_id, args.prompt)
    if args.command == "promotion-plan":
        return pipeline.promotion_commands(args.eval_id)
    if args.command == "promote":
        return pipeline.promote(args.eval_id, args.confirmation)
    if args.command == "rollback":
        return pipeline.rollback(args.confirmation)
    raise AssertionError(args.command)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    result = execute(args)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
