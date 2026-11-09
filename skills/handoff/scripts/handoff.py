#!/usr/bin/env python3
"""Store, scrub and find session handoffs. Python 3 standard library only.

Handoffs live outside the repo, under ~/.claude/handoffs/<project-key>/, where
<project-key> is the git top level (or the current directory) with "/" turned
into "-", the same encoding Claude Code uses for its project folders.

Subcommands:
  context            print where handoffs go plus the git state to record
  save FILE [--slug S]
                     scrub secrets from FILE, store it, print the stored path
  latest [--slug S]  print the newest handoff (path on line 1, then contents)
  list [--all]       list handoffs for this project (or every project)
  scrub FILE         print FILE with secrets redacted (dry run, stores nothing)

Override the root with HANDOFF_HOME.
"""
import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("HANDOFF_HOME", Path.home() / ".claude" / "handoffs"))
# Unscrubbed drafts go in the owner-only store folder, never a shared /tmp.
# Not .md, so latest/list never pick one up.
DRAFT = "draft.tmp"

# Each pattern is redacted to [REDACTED:<label>]. Order matters: the specific
# token shapes run before the generic key=value rule.
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe-key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{16,}=*")),
    ("url-credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
]
# key = value / key: value / "key": "value" where the key name looks secret.
GENERIC = re.compile(
    r"""(?ix)
    (?P<key>\b[\w.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|private[_\-]?key|client[_\-]?secret|auth)[\w.\-]*\b["']?)
    (?P<sep>\s*[:=]\s*["']?)
    (?P<val>[^\s"'`,;]{8,})
    """
)


def scrub(text):
    """Return (scrubbed_text, {label: count})."""
    counts = {}

    def sub(label, pattern, s):
        def repl(_m):
            counts[label] = counts.get(label, 0) + 1
            return f"[REDACTED:{label}]"
        return pattern.sub(repl, s)

    for label, pattern in SECRET_PATTERNS:
        text = sub(label, pattern, text)

    def generic_repl(m):
        if m.group("val").startswith("[REDACTED"):
            return m.group(0)
        counts["secret-value"] = counts.get("secret-value", 0) + 1
        return f"{m.group('key')}{m.group('sep')}[REDACTED:secret-value]"

    text = GENERIC.sub(generic_repl, text)
    return text, counts


def git(*args, cwd=None):
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def project_root():
    top = git("rev-parse", "--show-toplevel")
    return Path(top) if top else Path.cwd()


def project_key(root):
    return str(root.resolve()).replace("/", "-")


def project_dir(root=None):
    return ROOT / project_key(root or project_root())


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:50] or "task"


def handoffs(directory, slug=None):
    if not directory.is_dir():
        return []
    files = sorted(directory.glob("*.md"), reverse=True)
    if slug:
        files = [f for f in files if f.stem.split("_", 1)[-1] == slugify(slug)]
    return files


def age(path):
    stamp = path.stem.split("_", 1)[0]
    try:
        then = dt.datetime.strptime(stamp, "%Y-%m-%d-%H%M")
    except ValueError:
        return "unknown age"
    delta = dt.datetime.now() - then
    if delta.days >= 1:
        return f"{delta.days}d old"
    hours = delta.seconds // 3600
    return f"{hours}h old" if hours else f"{delta.seconds // 60}m old"


def cmd_context(_args):
    root = project_root()
    d = project_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    print(f"project: {root}")
    print(f"store:   {d}")
    print(f"draft:   {d / DRAFT}")
    branch = git("branch", "--show-current", cwd=root)
    if branch is None and git("rev-parse", "--git-dir", cwd=root) is None:
        print("git:     not a git repository")
    else:
        head = git("log", "-1", "--format=%h %s", cwd=root)
        status = git("status", "--porcelain", cwd=root) or ""
        upstream = git("rev-parse", "--abbrev-ref", "@{u}", cwd=root)
        print(f"branch:  {branch or '(detached)'}")
        print(f"head:    {head or '(no commits)'}")
        print(f"dirty:   {len(status.splitlines())} changed or untracked paths")
        if upstream:
            ahead = git("rev-list", "--count", f"{upstream}..HEAD", cwd=root)
            print(f"upstream: {upstream} (ahead {ahead})")
    existing = handoffs(d)
    print(f"existing handoffs: {len(existing)}" + (f", newest {existing[0].name} ({age(existing[0])})" if existing else ""))


def cmd_save(args):
    src = Path(args.file)
    text = src.read_text(encoding="utf-8")
    clean, counts = scrub(text)
    slug = args.slug
    if not slug:
        m = re.search(r"^#\s*Handoff:\s*(.+)$", clean, re.M)
        slug = m.group(1) if m else "task"
    d = project_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = d / f"{stamp}_{slugify(slug)}.md"
    out.write_text(clean, encoding="utf-8")
    os.chmod(out, 0o600)
    if src.resolve() != out.resolve():
        src.unlink()  # the unscrubbed draft must not linger
    print(out)
    if counts:
        print("redacted: " + ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())))
    else:
        print("redacted: nothing")


def cmd_latest(args):
    files = handoffs(project_dir(), args.slug)
    if not files:
        print("no handoffs for this project", file=sys.stderr)
        sys.exit(1)
    newest = files[0]
    print(f"{newest} ({age(newest)})")
    others = sorted({f.stem.split("_", 1)[-1] for f in files[1:]} - {newest.stem.split("_", 1)[-1]})
    if others and not args.slug:
        print("other tasks in this project: " + ", ".join(others))
    print("---")
    print(newest.read_text(encoding="utf-8"))


def cmd_list(args):
    dirs = sorted(p for p in ROOT.iterdir() if p.is_dir()) if args.all and ROOT.is_dir() else [project_dir()]
    found = False
    for d in dirs:
        for f in handoffs(d):
            found = True
            label = f"{d.name}/" if args.all else ""
            print(f"{label}{f.name}  ({age(f)})")
    if not found:
        print("no handoffs")


def cmd_scrub(args):
    clean, counts = scrub(Path(args.file).read_text(encoding="utf-8"))
    sys.stdout.write(clean)
    print("\nredacted: " + (", ".join(f"{k} x{v}" for k, v in sorted(counts.items())) or "nothing"), file=sys.stderr)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("context").set_defaults(fn=cmd_context)
    s = sub.add_parser("save")
    s.add_argument("file")
    s.add_argument("--slug")
    s.set_defaults(fn=cmd_save)
    l = sub.add_parser("latest")
    l.add_argument("--slug")
    l.set_defaults(fn=cmd_latest)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    ls.set_defaults(fn=cmd_list)
    sc = sub.add_parser("scrub")
    sc.add_argument("file")
    sc.set_defaults(fn=cmd_scrub)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
