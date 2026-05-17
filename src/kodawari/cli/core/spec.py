from __future__ import annotations

import argparse
import json
from pathlib import Path

from kodawari.spec_generator.coverage import CoverageGenerator
from kodawari.spec_generator.generator import SpecGenerator
from kodawari.spec_generator.materializer import SpecMaterializer
from kodawari.spec_generator.parser import PRDParser
from kodawari.spec_generator.validator import SpecValidator


def _save_specs_json(specs: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for spec in specs:
        spec_id = str(spec["spec_id"]).replace("/", "_")
        target = output_dir / f"{spec_id}.json"
        target.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")


def command_generate(args: argparse.Namespace) -> int:
    parser = PRDParser()
    generator = SpecGenerator()

    all_specs = []
    for prd_path in args.prd:
        prd = parser.parse_prd(prd_path)
        p0_clauses = parser.extract_p0_clauses(prd) if args.priority.upper() == "P0" else prd.clauses
        prd_slug = Path(prd_path).stem
        for clause in p0_clauses:
            spec = generator.generate_spec(clause, prd_doc_slug=prd_slug)
            all_specs.append(spec.to_dict())

    _save_specs_json(all_specs, Path(args.output))
    return 0


def command_validate(args: argparse.Namespace) -> int:
    validator = SpecValidator()
    spec_dir = Path(args.spec_dir)
    result = {"valid": True, "items": []}
    for path in sorted(spec_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        # Lightweight loading to Spec via kwargs.
        from kodawari.spec_generator.models import Spec

        spec = Spec(**payload)
        validation = validator.validate_spec(spec).to_dict()
        result["items"].append({"spec": path.name, "validation": validation})
        result["valid"] = result["valid"] and bool(validation["valid"])
    Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["valid"] else 1


def command_coverage(args: argparse.Namespace) -> int:
    parser = PRDParser()
    coverage = CoverageGenerator()

    clauses = []
    for prd_path in args.prd:
        prd = parser.parse_prd(prd_path)
        clauses.extend(parser.extract_p0_clauses(prd))

    specs = _load_specs_from_dir(Path(args.spec_dir))

    matrix = coverage.generate_matrix(clauses, specs)
    if args.format == "markdown":
        coverage.export_markdown(matrix, args.output)
    else:
        coverage.export_json(matrix, args.output)
    return 0


def _load_specs_from_dir(spec_dir: Path) -> list:
    from kodawari.spec_generator.models import Spec

    specs = []
    for path in sorted(spec_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        specs.append(Spec(**payload))
    return specs


def command_materialize(args: argparse.Namespace) -> int:
    spec_dir = Path(args.spec_dir)
    materializer = SpecMaterializer()
    specs = _load_specs_from_dir(spec_dir)
    materializer.materialize(specs, args.output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kodawari spec")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate")
    gen.add_argument("--prd", action="append", required=True)
    gen.add_argument("--output", required=True)
    gen.add_argument("--priority", default="P0")
    gen.set_defaults(func=command_generate)

    validate = sub.add_parser("validate")
    validate.add_argument("--spec-dir", required=True)
    validate.add_argument("--report", required=True)
    validate.set_defaults(func=command_validate)

    cov = sub.add_parser("coverage")
    cov.add_argument("--prd", action="append", required=True)
    cov.add_argument("--spec-dir", required=True)
    cov.add_argument("--output", required=True)
    cov.add_argument("--format", choices=["json", "markdown"], default="json")
    cov.set_defaults(func=command_coverage)

    materialize = sub.add_parser("materialize")
    materialize.add_argument("--spec-dir", required=True)
    materialize.add_argument("--output", required=True)
    materialize.set_defaults(func=command_materialize)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
