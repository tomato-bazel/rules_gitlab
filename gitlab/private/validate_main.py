"""Entry point for the `gitlab_ci_validate` Bazel rule.

A wrapper around `check-jsonschema` (a pip dep brought in via the
`@rules_gitlab_tooling` hub) that:

  1. Loads the pinned GitLab CI JSON Schema (passed via argv).
  2. Parses the `.gitlab-ci.yml` with `ruamel.yaml`, registering
     constructors that absorb GitLab's custom CI YAML tags
     (`!reference`, `!file`, `!base64`, …). check-jsonschema's
     default loader rejects these tags with `ConstructorError`,
     making real-world GitLab configs unparseable.
  3. Dumps the parsed structure to a temp JSON file.
  4. Invokes check-jsonschema's CLI with the JSON file +
     `--schemafile` pointing at the pinned schema, bypassing its
     YAML parser entirely.
  5. On success, writes a single-line stamp file so Bazel knows
     the check ran cleanly (caching keys on the file's contents).

Errors are printed to stderr with file:line context. Exit code is
the check-jsonschema runner's exit code — non-zero for any
violation.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.constructor import SafeConstructor

from check_jsonschema.cli import main as check_jsonschema_main


def _absorb_unknown_tag(self, node):  # type: ignore[no-untyped-def]
    """Generic ruamel.yaml constructor that maps any unknown
    `!tag`-prefixed node to its underlying Python value. Used to
    tolerate GitLab's custom CI YAML tags that no third-party
    parser knows about.
    """
    if hasattr(node, "value") and isinstance(node.value, str):
        return self.construct_scalar(node)
    try:
        return self.construct_sequence(node)
    except Exception:
        pass
    try:
        return self.construct_mapping(node, deep=True)
    except Exception:
        pass
    return None


def _make_tolerant_yaml() -> YAML:
    yaml = YAML(typ="safe")
    # The `\0` SafeConstructor.add_multi_constructor pattern
    # catches every tag whose name starts with `!`. ruamel's
    # SafeConstructor matches on tag prefix; passing an empty
    # string means "all tags" (we narrow to the `!`-prefix
    # bucket by registering on the `!` short-form).
    SafeConstructor.add_multi_constructor("!", _absorb_unknown_tag)
    return yaml


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", required=True, type=Path)
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument(
        "--stamp",
        required=True,
        type=Path,
        help="Path the rule expects the check to write on success.",
    )
    args = ap.parse_args(argv)

    yaml = _make_tolerant_yaml()
    try:
        with open(args.src) as f:
            data = yaml.load(f)
    except Exception as e:
        print(
            f"gitlab_ci_validate: failed to parse {args.src} as YAML: {e}",
            file=sys.stderr,
        )
        return 2

    # Dump to a temp JSON file so check-jsonschema's standard JSON
    # path handles it (sidesteps its ruamel-yaml parser, which
    # rejects unknown tags before our constructor would fire).
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        delete=False,
    ) as tmp:
        json.dump(data, tmp, default=str)
        tmp_path = tmp.name

    check_argv = [
        "--schemafile",
        str(args.schema),
        # GitLab uses Perl-style slash-delimited regex literals
        # for fields like `coverage:` (`/^TOTAL\s+\d+/`); strip
        # the regex format check so those don't fail validation.
        # Everything else (`uri`, structural, additionalProperties)
        # stays enforced.
        "--disable-formats",
        "regex",
        "--verbose",
        tmp_path,
    ]
    try:
        check_jsonschema_main(check_argv)
        rc = 0
    except SystemExit as e:
        rc = int(e.code) if e.code is not None else 0
    if rc == 0:
        args.stamp.write_text(
            f"gitlab_ci_validate: OK\nschema={args.schema}\nsrc={args.src}\n",
        )
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
