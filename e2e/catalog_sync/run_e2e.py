"""Explicit Linux E2E entry point; collect/skip/zero-case never means pass."""

import argparse
import json
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from catalog_sync.environment import (
        EVIDENCE,
        BoundaryUnverified,
        PrerequisiteError,
        Redactor,
        evidence_output,
        preflight,
        save_json,
    )
else:
    from .environment import (
        EVIDENCE,
        BoundaryUnverified,
        PrerequisiteError,
        Redactor,
        evidence_output,
        preflight,
        save_json,
    )


class Results:
    def __init__(self, ac=None):
        self.ac = ac
        self.collected = 0
        self.executed = set()
        self.skipped = set()
        self.failed = set()
        self.blocked = set()
        self.failures = {}
        self.prerequisite_error = False
        self.cleanup_error = False

    def pytest_collection_modifyitems(self, session, config, items):
        if self.ac:
            selected = [item for item in items
                        if item.name.split("[", 1)[0].endswith("__" + self.ac)]
            deselected = [item for item in items if item not in selected]
            config.hook.pytest_deselected(items=deselected)
            items[:] = selected

    def pytest_exception_interact(self, node, call, report):
        self.failures[node.nodeid] = {"type": type(call.excinfo.value).__name__,
                                      "message": str(call.excinfo.value), "phase": report.when}
        if isinstance(call.excinfo.value, BoundaryUnverified):
            self.blocked.add(node.nodeid)
        if isinstance(call.excinfo.value, PrerequisiteError):
            self.prerequisite_error = True
        if "cleanup failed" in str(call.excinfo.value).lower():
            self.cleanup_error = True

    def pytest_collection_finish(self, session):
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report):
        if report.when == "call":
            self.executed.add(report.nodeid)
        if report.skipped:
            self.skipped.add(report.nodeid)
        if report.failed:
            self.failed.add(report.nodeid)

    def exit_code(self, pytest_code):
        if self.cleanup_error:
            return 1
        if self.prerequisite_error:
            return 2
        if (pytest_code or not self.collected or len(self.executed) != self.collected
                or self.skipped or self.failed):
            return 1
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--case", choices=[f"AC{i}" for i in range(1, 11)])
    args = parser.parse_args(argv)
    redact = Redactor()
    started = time.monotonic()
    try:
        endpoint = preflight()
    except PrerequisiteError as exc:
        print(redact(f"NOT RUN (exit 2): {exc}"))
        save_json(EVIDENCE / "test-catalog-result.json",
                  {"exit": 2, "executed": 0, "reason": str(exc)}, redact)
        return 2
    if args.preflight:
        print("Preflight ready; no E2E executed (exit 2).")
        return 2
    import pytest
    results = Results(args.case)
    pytest_args = [str(Path(__file__).with_name("catalog_sync_cases.py")), "-q", "--tb=short",
                   "-p", "no:cacheprovider"]
    # Pytest captures no production stdout: all subprocess output goes through Redactor.
    with evidence_output(EVIDENCE / "test-catalog-e2e.log", redact):
        code = results.exit_code(pytest.main(pytest_args, plugins=[results]))
    summary = {"exit": code, "scope": args.case or "full", "partial": bool(args.case),
               "collected": results.collected, "executed": len(results.executed),
               "failed": sorted(results.failed), "skipped": sorted(results.skipped),
               "blocked": sorted(results.blocked), "failures": results.failures,
               "passed": sorted(results.executed - results.failed - results.skipped),
               "elapsed": time.monotonic() - started, "docker_endpoint": endpoint}
    save_json(EVIDENCE / "test-catalog-result.json", summary, redact)
    print(redact(json.dumps(summary)))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
