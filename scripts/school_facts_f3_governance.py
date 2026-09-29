"""Reviewed, finite F3 Governance deployment CLI; never an HTTP endpoint."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps/api"))

from app.composition.school_fact_f3_engineering_cli import (
    EngineeringPrivateConfiguration,
    GrantBinding,
    build_grants,
    command_marker,
    compose,
    load_manifest,
    review,
)


def write_new(path: Path, value: object):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")


def current_approval_guard(
    config_path: Path,
    manifest_path: Path,
    *,
    config_sha256: str,
    manifest_sha256: str,
):
    """Recheck the original private pin and closure at each authorization boundary."""
    withdrawn = False

    def current() -> bool:
        nonlocal withdrawn
        if withdrawn:
            return False
        try:
            withdrawn = (
                config_path.with_suffix(config_path.suffix + ".closed").exists()
                or config_path.stat().st_size > 65536
                or manifest_path.stat().st_size > 1_048_576
                or not hmac.compare_digest(
                    sha256(config_path.read_bytes()).hexdigest(), config_sha256
                )
                or not hmac.compare_digest(
                    sha256(manifest_path.read_bytes()).hexdigest(), manifest_sha256
                )
            )
        except OSError:
            withdrawn = True
        return not withdrawn

    return current


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("review", "execute", "receipt", "build-grants", "close")
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--index", type=int)
    parser.add_argument("--confirm")
    parser.add_argument("--bindings", type=Path)
    parser.add_argument("--grant-valid-from")
    parser.add_argument("--execution-valid-to")
    args = parser.parse_args(argv)
    engines = ()
    try:
        if args.config.stat().st_size > 65536:
            raise ValueError("Private configuration too large")
        config_data = args.config.read_bytes()
        config = EngineeringPrivateConfiguration.model_validate_json(config_data)
        manifest = load_manifest(args.manifest, config.approved_manifest_sha256)
        if args.action == "review":
            write_new(args.output, review(manifest, config.approved_manifest_sha256))
            return 0
        closed_path = args.config.with_suffix(args.config.suffix + ".closed")
        if args.action == "close":
            if not hmac.compare_digest(
                args.confirm or "", "CLOSE-F3:" + config.approved_manifest_sha256
            ):
                raise ValueError("Exact closing marker required")
            write_new(
                closed_path, {"closed_manifest_sha256": config.approved_manifest_sha256}
            )
            write_new(
                args.output,
                {"status": "closed", "receipt_lookup_remains_available": True},
            )
            return 0
        if args.action in {"execute", "build-grants"} and closed_path.exists():
            raise ValueError("Engineering execution has been closed")
        if args.action != "build-grants":
            if args.index is None or not 0 <= args.index < len(manifest.commands):
                raise ValueError("Exact approved command index required")
            command = manifest.materialize()[args.index]
            if args.action == "execute" and not hmac.compare_digest(
                args.confirm or "",
                command_marker(config.approved_manifest_sha256, command),
            ):
                raise ValueError("Exact reviewed confirmation marker required")
        if args.output.exists():
            raise ValueError("Output must be a new file")
        approval_is_current = current_approval_guard(
            args.config,
            args.manifest,
            config_sha256=sha256(config_data).hexdigest(),
            manifest_sha256=config.approved_manifest_sha256,
        )
        service, engines = compose(
            config, manifest, approval_is_current=approval_is_current
        )
        credential = config.credential.get_secret_value()
        if args.action == "build-grants":
            if args.bindings is None or args.bindings.stat().st_size > 65536:
                raise ValueError("Finite bindings file required")
            bindings = [
                GrantBinding.model_validate(b)
                for b in json.loads(args.bindings.read_bytes())
            ]
            if args.grant_valid_from is None or args.execution_valid_to is None:
                raise ValueError("Later grant stage timestamps required")
            result = build_grants(
                manifest,
                bindings,
                grant_valid_from=datetime.fromisoformat(args.grant_valid_from),
                execution_valid_to=datetime.fromisoformat(args.execution_valid_to),
                service=service,
                credential=credential,
            )
            write_new(args.output, result.model_dump(mode="json"))
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
                    "command_digest": command_marker(
                        config.approved_manifest_sha256, command
                    ),
                    "receipt": asdict(result) if result is not None else None,
                },
            )
        return 0
    except Exception:  # noqa: BLE001 - secrets and database text must never enter logs
        # Neither validation input, DSN, DB errors, body text nor credentials enter logs.
        print(
            "F3 engineering command rejected or outcome unavailable; "
            "use the original receipt lookup.",
            file=sys.stderr,
        )
        return 2
    finally:
        for engine in engines:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
