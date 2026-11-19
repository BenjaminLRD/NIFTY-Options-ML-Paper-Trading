"""Tracked-file safety scan: accidental secrets and broker order-placement code/endpoints.

Run:  python -B -m niftypaper.safety.scan --root . [--include-untracked]      (needs PYTHONPATH=src, or an installed package)
Exit: 0 clean, 1 findings, 2 incomplete scan or error (fail closed: unreadable, oversized, binary or non-UTF-8 files are not skipped silently).

What it is: regular-expression rules over the text of files listed by `git ls-files`, plus file-name rules and a .gitignore check.
What it is NOT: a proof of security. It cannot see obfuscated or split strings, encoded blobs, secrets already in git history,
untracked files unless --include-untracked is used, environment variables, or endpoints and SDKs that its lists do not name.
It will also flag some harmless text (false positives). See docs/SAFETY.md.

The patterns are written so that their own source text does not match them (character classes and optional groups), so the scanner
can scan itself without an allow-list.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 2_000_000
REQUIRED_GITIGNORE = (".env", ".env.*", "*.pem", "*.key", "*.db", "data/", "logs/")
# Narrow, reviewed exemptions (path -> rule ids). The read-only market-data client must name its one data host (rule B3) and the
# implementation record documents it; every other rule still applies to these files, and B3 still applies everywhere else.
ALLOWED_RULES = {
    "src/niftypaper/providers/fyers_http.py": frozenset({"B3"}),
    "docs/V6-0.5_RECORD.md": frozenset({"B3"}),
}


class SafetyScanError(RuntimeError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    category: str          # "secret" or "broker"
    pattern: re.Pattern
    redact: bool
    description: str


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    snippet: str           # redacted for secret rules; never contains a full secret

    def __str__(self) -> str:
        return f"{self.rule} {self.path}:{self.line} {self.snippet}"


@dataclass
class ScanReport:
    scanned: int
    findings: list
    skipped: list          # (path, reason)

    @property
    def exit_code(self) -> int:
        if self.skipped:
            return 2
        return 1 if self.findings else 0


_C = re.compile
RULES = (
    Rule("S1", "secret", _C(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"), False, "private key block"),
    Rule("S2", "secret", _C(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), True, "AWS-style access key id"),
    Rule("S3", "secret", _C(r"""(?i)\b(?:api[_-]?key|api[_-]?secret|client[_-]?secret|secret[_-]?key|access[_-]?token|auth[_-]?token|"""
                            r"""refresh[_-]?token|totp[_-]?secret|passw(?:or)?d|pwd|token|secret)\b\s*[:=]\s*['"]?([A-Za-z0-9_\-+/=.]{16,})"""),
         True, "credential-like assignment with a long mixed value"),
    Rule("S4", "secret", _C(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), True, "JWT-like token"),
    Rule("S5", "secret", _C(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{20,}=*"), True, "bearer token"),
    Rule("S6", "secret", _C(r"[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s:@]{3,}@"), True, "URL with embedded credentials"),
    Rule("B1", "broker", _C(r"(?i)\b(?:place|modify|cancel|submit|send)[_\-]?(?:regular[_\-]?|basket[_\-]?|bracket[_\-]?|gtt[_\-]?|super[_\-]?|smart[_\-]?)?orders?\b"),
         False, "order-placement / modification / cancellation verb"),
    Rule("B2", "broker", _C(r"""(?i)https?://[^\s'"]*/(?:v\d+/)?orders?\b"""), False, "order endpoint URL"),
    Rule("B3", "broker", _C(r"(?i)\b(?:api\.kite\.trade|kite\.zerodha\.com|api(?:-v2)?\.upstox\.com|apiconnect\.angelone\.in|api(?:-t1)?\.fyers\.in|"
                            r"api\.dhan\.co|api\.icicidirect\.com|openapi\.5paisa\.com|api\.shoonya\.com)\b"), False, "broker API host (non-exhaustive list)"),
    Rule("B4", "broker", _C(r"(?i)\b(?:kite[c]onnect|upstox[_\-]?client|upstox[_\-]?python[_\-]?sdk|smart[a]pi|fyers[_\-]?api(?:v\d)?|dhan[h]q|"
                            r"breeze[_\-]?connect|py[a]3|noren[r]estapi|ib[k]r|ib_[i]nsync|alpaca[_\-]?trade[_\-]?api)\b"), False, "broker SDK name (non-exhaustive list)"),
)
RULE_IDS = tuple(r.id for r in RULES) + ("F1", "G1")

_PLACEHOLDER = _C(r"(?i)(example|your|placeholder|changeme|dummy|sample|xxxx|<[^>]+>|\*{3,}|\.{3,})")
_NAME_RULES = (
    ("F1", _C(r"(?i)(?:^|/)\.env(?:\.(?!example$|sample$|template$).+)?$"), "environment file"),
    ("F1", _C(r"(?i)\.(?:pem|p12|pfx|key|jks)$"), "key / certificate file"),
    ("F1", _C(r"(?i)(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)$"), "SSH private key file"),
    ("F1", _C(r"(?i)(?:^|/)(?:credentials?|secrets?)\.(?:json|ya?ml|ini|txt|toml)$"), "credentials file"),
    ("F1", _C(r"(?i)token[^/]*\.json$"), "token file"),
)


def _redact(text: str) -> str:
    return text[:4] + "...[redacted]"


def _secret_value_ok(value: str) -> bool:
    return (any(c.isalpha() for c in value) and any(c.isdigit() for c in value) and len(set(value)) > 4 and not _PLACEHOLDER.search(value))


def scan_text(path: str, text: str) -> list:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                if rule.id == "S3" and not _secret_value_ok(match.group(1)):
                    continue
                if rule.id in {"S2", "S4", "S5", "S6", "S3"} and _PLACEHOLDER.search(match.group(0)):
                    continue
                snippet = _redact(match.group(0)) if rule.redact else line.strip()[:100]
                findings.append(Finding(path, number, rule.id, snippet))
    return findings


def scan_paths(root: Path, rel_paths: list) -> ScanReport:
    findings, skipped, scanned = [], [], 0
    for rel in rel_paths:
        rel_posix = rel.replace("\\", "/")
        for rule_id, pattern, why in _NAME_RULES:
            if pattern.search(rel_posix):
                findings.append(Finding(rel_posix, 0, rule_id, why))
        full = Path(root) / rel
        try:
            if full.is_symlink() or not full.is_file():
                skipped.append((rel_posix, "not a regular file"))
                continue
            if full.stat().st_size > MAX_BYTES:
                skipped.append((rel_posix, "larger than scan limit"))
                continue
            data = full.read_bytes()
        except OSError as exc:
            skipped.append((rel_posix, f"unreadable: {exc.__class__.__name__}"))
            continue
        if b"\x00" in data:
            skipped.append((rel_posix, "binary file"))
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append((rel_posix, "not UTF-8"))
            continue
        scanned += 1
        findings.extend(f for f in scan_text(rel_posix, text) if f.rule not in ALLOWED_RULES.get(rel_posix, ()))
    return ScanReport(scanned, findings, skipped)


def _git_lines(root: Path, args: list) -> list:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SafetyScanError(f"git is required and failed: {exc.__class__.__name__}") from exc
    return [p.decode("utf-8") for p in out.split(b"\x00") if p]


def tracked_files(root: Path, include_untracked: bool = False) -> list:
    files = _git_lines(root, ["ls-files", "-z"])
    if include_untracked:
        files += _git_lines(root, ["ls-files", "-z", "--others", "--exclude-standard"])
    return sorted(set(files))


def check_gitignore(root: Path) -> list:
    path = Path(root) / ".gitignore"
    if not path.is_file():
        return [Finding(".gitignore", 0, "G1", "missing .gitignore")]
    lines = {line.strip() for line in path.read_text(encoding="utf-8").splitlines()}
    return [Finding(".gitignore", 0, "G1", f"required ignore entry missing: {entry}") for entry in REQUIRED_GITIGNORE if entry not in lines]


def scan_repo(root: str | Path, include_untracked: bool = False) -> ScanReport:
    root = Path(root)
    report = scan_paths(root, tracked_files(root, include_untracked))
    report.findings.extend(check_gitignore(root))
    return report


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan tracked files for secrets and broker order endpoints.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--include-untracked", action="store_true", help="also scan untracked files that git does not ignore")
    args = parser.parse_args(argv)
    try:
        report = scan_repo(args.root, args.include_untracked)
    except SafetyScanError as exc:
        print(f"SAFETY SCAN ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"scanned {report.scanned} files; findings {len(report.findings)}; skipped {len(report.skipped)}")
    for finding in report.findings:
        print("FINDING", finding)
    for path, why in report.skipped:
        print("SKIPPED", path, "-", why)
    print("Pattern-based check only: it does not prove the repository is free of secrets or order code (see docs/SAFETY.md).")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
