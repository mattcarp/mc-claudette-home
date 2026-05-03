#!/usr/bin/env python3
"""
Claudette Home — wipe-on-uninstall verifier (CLH-25).

Reads brain/sealed_data_audit.md as the single source of truth for every
persistent path the stack writes to, then verifies on-disk reality matches
what the audit promises.

Modes:
    --report         Print the audit table with live status. Exit 0 always.
    --verify         Exit non-zero if any wipe-class path is still present.
    --full --root P  Round-trip: plant sentinels, run the uninstaller under P,
                     then run --verify against P.

Strictly stdlib. Runs on the bare appliance with no third-party deps.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_DOC = REPO_ROOT / "brain" / "sealed_data_audit.md"
UNINSTALL_SCRIPT = REPO_ROOT / "brain" / "uninstall_claudette.sh"

TABLE_BEGIN = "<!-- BEGIN: SEALED-DATA-TABLE -->"
TABLE_END = "<!-- END: SEALED-DATA-TABLE -->"

VALID_CLASSES = {"wipe", "keep", "oos"}


@dataclass(frozen=True)
class AuditEntry:
    raw_path: str
    cls: str
    owner: str
    notes: str

    @property
    def env_var(self) -> str | None:
        """For /etc/environment#VAR rows, return VAR; else None."""
        if "#" in self.raw_path and self.raw_path.startswith("/etc/environment"):
            return self.raw_path.split("#", 1)[1]
        return None

    def resolve(self, root_prefix: str | None, home: Path) -> Path:
        """Return the on-disk path, honouring --root and ~ expansion."""
        logical = self.raw_path.split("#", 1)[0]
        if logical.startswith("~"):
            logical = str(home) + logical[1:]
        if root_prefix:
            return Path(root_prefix.rstrip("/") + logical)
        return Path(logical)


def parse_audit(doc_path: Path = AUDIT_DOC) -> list[AuditEntry]:
    text = doc_path.read_text(encoding="utf-8")
    if TABLE_BEGIN not in text or TABLE_END not in text:
        raise SystemExit(
            f"audit doc {doc_path} is missing {TABLE_BEGIN!r}/{TABLE_END!r} markers"
        )
    block = text.split(TABLE_BEGIN, 1)[1].split(TABLE_END, 1)[0]

    entries: list[AuditEntry] = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        # Skip the header row and the |---|---| separator row.
        if cells[0].lower() == "path" or set(cells[0]) <= {"-", " ", ":"}:
            continue
        path, cls, owner, notes = cells[0], cells[1].lower(), cells[2], cells[3]
        if cls not in VALID_CLASSES:
            raise SystemExit(
                f"audit doc has unknown class {cls!r} for path {path!r}; "
                f"expected one of {sorted(VALID_CLASSES)}"
            )
        entries.append(AuditEntry(raw_path=path, cls=cls, owner=owner, notes=notes))

    if not entries:
        raise SystemExit(f"audit doc {doc_path} has no rows between table markers")
    return entries


def status_for(entry: AuditEntry, root_prefix: str | None, home: Path) -> str:
    """Return 'present', 'absent', or 'n/a' for a single entry."""
    target = entry.resolve(root_prefix, home)
    if entry.env_var is not None:
        if not target.exists():
            return "absent"
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "n/a"
        pattern = re.compile(
            rf"^(export\s+)?{re.escape(entry.env_var)}=", re.MULTILINE
        )
        return "present" if pattern.search(content) else "absent"
    return "present" if target.exists() else "absent"


def cmd_report(entries: list[AuditEntry], root_prefix: str | None, home: Path) -> int:
    rows = [("PATH", "CLASS", "STATUS", "OWNER")]
    for e in entries:
        rows.append((e.raw_path, e.cls, status_for(e, root_prefix, home), e.owner))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for i, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            print("  ".join("-" * w for w in widths))
    return 0


def cmd_verify(entries: list[AuditEntry], root_prefix: str | None, home: Path) -> int:
    offenders: list[str] = []
    for e in entries:
        if e.cls != "wipe":
            continue
        if status_for(e, root_prefix, home) == "present":
            offenders.append(f"residue: {e.raw_path} -> {e.resolve(root_prefix, home)}")
    if offenders:
        for line in offenders:
            print(line, file=sys.stderr)
        return 1
    print("verify: clean — no wipe-class residue found")
    return 0


def _plant_sentinels(
    entries: list[AuditEntry], root_prefix: str, home: Path
) -> None:
    """Populate every wipe-class path under root_prefix with a sentinel."""
    sentinel = b"sentinel-claudette-CLH-25\n"
    seen_env_file: set[Path] = set()
    for e in entries:
        if e.cls != "wipe":
            continue
        target = e.resolve(root_prefix, home)
        target.parent.mkdir(parents=True, exist_ok=True)
        if e.env_var is not None:
            if target not in seen_env_file:
                # Seed with a couple of unrelated lines that must survive.
                target.write_text(
                    "PATH=/usr/local/sbin:/usr/local/bin\n"
                    "LANG=en_GB.UTF-8\n",
                    encoding="utf-8",
                )
                seen_env_file.add(target)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(f"{e.env_var}=sentinel-value\n")
        else:
            # If the basename has an extension (.service, .json, ...) treat it
            # as a file; otherwise treat it as a directory and drop a sentinel
            # file inside it. This matches every wipe-class row in the audit.
            if Path(e.raw_path).suffix:
                target.write_bytes(sentinel)
            else:
                target.mkdir(parents=True, exist_ok=True)
                (target / "sentinel.bin").write_bytes(sentinel)


def cmd_full(
    entries: list[AuditEntry],
    root_prefix: str,
    home: Path,
    uninstall_script: Path = UNINSTALL_SCRIPT,
) -> int:
    """Plant sentinels under root_prefix, run uninstall, then verify."""
    if not root_prefix:
        print("--full requires --root <sandbox>", file=sys.stderr)
        return 2
    if not uninstall_script.exists():
        print(f"uninstall script not found: {uninstall_script}", file=sys.stderr)
        return 2

    _plant_sentinels(entries, root_prefix, home)

    # Sanity: verify must fail before uninstall, else the sentinels didn't take.
    pre = cmd_verify(entries, root_prefix, home)
    if pre == 0:
        print(
            "--full: sentinels did not register as residue; aborting before "
            "running the uninstaller",
            file=sys.stderr,
        )
        return 2

    env = os.environ.copy()
    env["HOME"] = str(home)
    proc = subprocess.run(
        ["bash", str(uninstall_script), "--root", root_prefix, "--yes"],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.stderr.write(proc.stdout)
        print(
            f"--full: uninstall script exited {proc.returncode}",
            file=sys.stderr,
        )
        return proc.returncode

    post = cmd_verify(entries, root_prefix, home)
    if post != 0:
        return post

    # Idempotency: a second run must also exit 0 with no destructive ops.
    proc2 = subprocess.run(
        ["bash", str(uninstall_script), "--root", root_prefix, "--yes"],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc2.returncode != 0:
        sys.stderr.write(proc2.stderr)
        print(
            f"--full: second uninstall (idempotency) exited {proc2.returncode}",
            file=sys.stderr,
        )
        return proc2.returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wipe_uninstall_verifier",
        description="Verify Claudette Home wipe-on-uninstall promises.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--report", action="store_true", help="print live status table")
    group.add_argument("--verify", action="store_true", help="exit non-zero on residue")
    group.add_argument(
        "--full",
        action="store_true",
        help="plant sentinels + uninstall + verify under --root",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="sandbox root prefix; required for --full",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        default=AUDIT_DOC,
        help=f"path to audit doc (default: {AUDIT_DOC})",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="override $HOME for ~ expansion (testing aid)",
    )
    args = parser.parse_args(argv)

    home = args.home if args.home else Path(os.environ.get("HOME", str(Path.home())))
    entries = parse_audit(args.audit)

    if args.report:
        return cmd_report(entries, args.root, home)
    if args.verify:
        return cmd_verify(entries, args.root, home)
    if args.full:
        if not args.root:
            print("--full requires --root <sandbox>", file=sys.stderr)
            return 2
        return cmd_full(entries, args.root, home)
    return 2  # unreachable due to required group


if __name__ == "__main__":
    sys.exit(main())
