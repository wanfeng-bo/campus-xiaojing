"""Live deployment CLI. Run from an OS-protected POSIX operator directory.

The config digest comes from the independent reviewer-controlled launcher, not
from the submitted config itself. This command never enables technical LOGIN.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import stat
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.composition import school_fact_live_governance_cli as api

from scripts.school_facts_f3_governance import current_approval_guard


def protected(path: Path, *, output=False):
    if os.name != "posix" or not path.is_absolute():
        raise ValueError("POSIX protected deployment path required")
    uid = os.geteuid()
    for parent in (path.parent, *path.parent.parents):
        metadata = parent.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, uid}
            or metadata.st_mode & 0o022
        ):
            raise ValueError("Unsafe deployment directory")
    if path.parent.stat().st_mode & 0o077:
        raise ValueError("Private deployment directory required")
    if output:
        if path.exists() or path.is_symlink():
            raise ValueError("New output required")
    else:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in {0, uid}
            or metadata.st_mode & 0o077
        ):
            raise ValueError("Private regular input required")


def write_new(path, value):
    protected(path, output=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("review", "execute", "receipt", "build-grants", "close")
    )
    for name in ("config", "manifest", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--index", type=int)
    parser.add_argument("--confirm")
    parser.add_argument("--bindings", type=Path)
    args = parser.parse_args(argv)
    engines = ()
    try:
        protected(args.config)
        protected(args.manifest)
        protected(args.output, output=True)
        raw = api.read_pinned(args.config, args.config_sha256, 65536)
        config = api.EngineeringPrivateConfiguration.model_validate_json(raw)
        if args.manifest.resolve() != Path(config.manifest_path).resolve():
            raise ValueError("Pinned manifest path differs")
        evidence = Path(config.evidence_path)
        protected(evidence)
        manifest = api.load_manifest(args.manifest, config.approved_manifest_sha256)
        api.verify_evidence(config, manifest)
        if (
            manifest.school_id,
            manifest.actor_reference,
            manifest.approval_reference,
            manifest.grant_epoch,
        ) != (
            config.expected_school_id,
            config.actor_reference,
            config.approval_reference,
            config.grant_epoch,
        ):
            raise ValueError("Config binding differs")
        if args.action == "review":
            write_new(
                args.output, api.review(manifest, config.approved_manifest_sha256)
            )
            return 0
        closed = args.config.with_suffix(args.config.suffix + ".closed")
        if args.action == "close":
            if not hmac.compare_digest(
                args.confirm or "", f"CLOSE-LIVE:{config.approved_manifest_sha256}"
            ):
                raise ValueError("Exact close marker required")
            write_new(
                closed, {"closed_manifest_sha256": config.approved_manifest_sha256}
            )
            write_new(
                args.output,
                {"status": "closed", "receipt_lookup_remains_available": True},
            )
            return 0
        if args.action != "build-grants":
            if args.index is None or not 0 <= args.index < len(manifest.commands):
                raise ValueError("Exact index required")
            command = manifest.materialize()[args.index]
            if args.action == "execute" and not hmac.compare_digest(
                args.confirm or "",
                api.command_marker(config.approved_manifest_sha256, command),
            ):
                raise ValueError("Exact confirm marker required")
        guard = current_approval_guard(
            args.config,
            args.manifest,
            config_sha256=args.config_sha256,
            manifest_sha256=config.approved_manifest_sha256,
        )

        def current():
            try:
                protected(args.config)
                protected(args.manifest)
                protected(evidence)
                return guard()
            except Exception:  # noqa: BLE001 - fail closed without exposing private input
                return False

        service, engines = api.compose(
            config,
            manifest,
            approval_is_current=current,
            receipt_only=args.action in {"receipt", "build-grants"},
        )
        credential = config.credential.get_secret_value()
        if args.action == "build-grants":
            if args.bindings is None:
                raise ValueError("Bindings required")
            protected(args.bindings)
            with args.bindings.open("rb") as stream:
                bound = stream.read(65537)
            if len(bound) > 65536:
                raise ValueError("Too many bindings")
            values = json.loads(bound, object_pairs_hook=api.unique_object)
            if not isinstance(values, list) or not 1 <= len(values) <= 300:
                raise ValueError("Finite bindings required")
            result = api.build_grants(
                manifest,
                [api.GrantBinding.model_validate(b) for b in values],
                grant_valid_from=manifest.valid_from,
                execution_valid_to=manifest.valid_to,
                service=service,
                credential=credential,
            )
            write_new(
                args.output,
                {
                    "requires_independent_review_before_execution": True,
                    "manifest": result.model_dump(mode="json"),
                },
            )
        else:
            result = service.receipt(command, credential=credential)
            if result is None and args.action == "execute":
                prepared = service.prepare(command, credential=credential)
                result = service.confirm(
                    prepared.confirmation_token, credential=credential
                )
            write_new(
                args.output,
                {
                    "command_digest": api.command_marker(
                        config.approved_manifest_sha256, command
                    ),
                    "receipt": asdict(result) if result is not None else None,
                },
            )
        return 0
    except Exception:  # noqa: BLE001 - fail closed without exposing private input
        print(
            "Live deployment rejected or outcome unavailable; inspect original protected receipts.",
            file=sys.stderr,
        )
        return 2
    finally:
        for engine in engines:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
