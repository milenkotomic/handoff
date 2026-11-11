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
# token shapes run before the generic key=value rule. A group named "keep" is
# the part of the match that stays (a header name, a user name, a URL prefix).
SECRET_PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----", re.S)),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("gitlab-token", re.compile(r"\bgl(?:pat|dt|rt|cbt|ptt)-[A-Za-z0-9_\-]{20,}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,}")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("slack-token", re.compile(r"\b(?:xox[abposre]|xapp)-[A-Za-z0-9\-]{10,}")),
    ("webhook-url", re.compile(r"(?P<keep>https://(?:hooks\.slack\.com/services|(?:discord|discordapp)\.com/api/webhooks)/)[A-Za-z0-9/_\-]+")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("google-oauth-token", re.compile(r"\bya29\.[A-Za-z0-9_\-.]{20,}")),
    ("sendgrid-key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}")),
    ("stripe-key", re.compile(r"\b(?:[rs]k_(?:live|test)|whsec)_[A-Za-z0-9]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{16,}=*")),
    ("basic-auth", re.compile(r"(?i)(?P<keep>\bauthorization[\"']?\s*[:=]\s*[\"']?basic\s+)[A-Za-z0-9+/]{8,}={0,2}")),
    ("url-credentials", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    # curl -u user:pass (but not docker -u 1000:1000)
    ("cli-credentials", re.compile(r"(?P<keep>(?<![\w\-])(?:-u|--user)[\s=]+[\"']?[^\s:\"']+:)(?!\d+(?![^\s\"']))[^\s\"']+")),
    # mysql -pSECRET
    ("cli-password", re.compile(r"(?P<keep>\b(?:mysql|mysqldump|mysqladmin|mariadb)\b[^\n|;&]*?\s-p)[^\s\-]\S*")),
]
# key = value / key: value / "key": "value" / "the password is value", where
# the key name looks secret. The key is matched from the keyword on (no leading
# wildcard) and its tail is bounded, so a long unbroken token stays linear.
GENERIC = re.compile(
    r"""(?ix)
    (?P<key>(?:(?<!1)password|passwd|pwd|passphrase|secret|token|api[_\-]?key|apikey
        |access[_\-]?key|private[_\-]?key|credentials?|cookie|session[_\-]?id
        |auth(?!or(?!i[sz])))[\w.\-]{0,40}["']?)
    (?P<sep>\s*[:=]\s*|\s+(?:is|was)\s+)
    (?P<val>"[^"\n]{4,}"|'[^'\n]{4,}'|[^\s"'`,;]{6,})
    """
)
# Values that are plainly not secrets: literals and references to a secret
# that lives somewhere else.
LITERALS = {"true", "false", "none", "null", "nil", "undefined", "required", "optional"}
REFERENCE = re.compile(r"(?i)(?:\$|<|\{\{|os\.environ|os\.getenv|process\.env|env\.|ENV\[|System\.getenv|secrets\.|vault:)")
CONFIG_LINE = re.compile(r"\s*(?:export\s+)?[\"']?[\w.\-]*")


def scrub(text):
    """Return (scrubbed_text, {label: count})."""
    counts = {}

    def sub(label, pattern, s):
        def repl(m):
            counts[label] = counts.get(label, 0) + 1
            keep = m.groupdict().get("keep") or ""
            return f"{keep}[REDACTED:{label}]"
        return pattern.sub(repl, s)

    for label, pattern in SECRET_PATTERNS:
        text = sub(label, pattern, text)

    def generic_repl(m):
        key, sep, val = m.group("key").lower(), m.group("sep"), m.group("val")
        quoted = val[0] in "\"'"
        bare = val.strip("\"'")
        if bare.startswith("[REDACTED") or bare.lower() in LITERALS or REFERENCE.match(bare):
            return m.group(0)
        if bare.isdigit() and "token" in key:
            return m.group(0)  # max_tokens: 4096 is a count
        has_digit = any(c.isdigit() for c in bare)
        if sep.strip() not in (":", "="):
            # prose ("the password is ..."): only values that look generated
            if len(bare) < 8 or not has_digit or not any(c.isalpha() for c in bare):
                return m.group(0)
        elif not quoted and ":" in sep and bare.rstrip(".").isalpha():
            # "auth: session cookies" in a sentence is prose; "password: swordfish"
            # at the start of a line is config
            line_start = m.string.rfind("\n", 0, m.start()) + 1
            if not CONFIG_LINE.fullmatch(m.string, line_start, m.start()):
                return m.group(0)
        counts["secret-value"] = counts.get("secret-value", 0) + 1
        return f"{m.group('key')}{sep}[REDACTED:secret-value]"

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
    root = root or project_root()
    store = ROOT.expanduser().resolve()
    if "HANDOFF_HOME" in os.environ:
        # A repo that manages to set HANDOFF_HOME must not be able to pull the
        # store inside itself: it could then plant the handoff a session
        # resumes from, and saved handoffs would land in commits.
        proj = root.resolve()
        if store == proj or proj in store.parents:
            sys.exit(f"refusing HANDOFF_HOME={store}: it is inside the project ({proj}); handoffs must live outside the repo")
    return store / project_key(root)


def ensure_store(d):
    """Create the store root and the project folder, both owner-only."""
    for p in (d.parent, d):
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(p, 0o700)


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
    ensure_store(d)
    print(f"project: {root}")
    print(f"store:   {d}")
    print(f"draft:   {d / DRAFT}")
    if (d / DRAFT).exists():
        print("stale draft: an unscrubbed draft from an interrupted save is at the draft path; overwrite or delete it")
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
    ensure_store(d)
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    out = d / f"{stamp}_{slugify(slug)}.md"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never world-readable, even briefly
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(clean)
    os.chmod(out, 0o600)
    # Delete only our own draft. Any other path is the caller's file.
    if src.resolve() == (d / DRAFT).resolve():
        src.unlink()  # the unscrubbed draft must not linger
    else:
        print(f"note: {src} is not the draft path, so it was left in place (it is unscrubbed)", file=sys.stderr)
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
