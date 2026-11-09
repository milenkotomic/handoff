# handoff

A Claude Code skill that saves the live state of a task to a handoff file, so
a fresh session can continue it without a long context window or a lossy
auto-compaction. It also resumes from the latest handoff at the start of a
session.

```
/handoff:handoff                         # write a handoff for the current task
/handoff:handoff next session: tests     # write one, tailored to what comes next
/handoff:handoff resume                  # pick up the newest handoff for this project
/handoff:handoff list                    # list this project's handoffs
```

Plain language works too: "write a handoff", "I'm running out of context",
"where were we?", and in Spanish "guarda el contexto", "pausa",
"dónde quedamos", "retomemos".

## What a handoff holds

Goal, state (done, in progress, not started), decisions with who made them and
why, open questions, next steps (the first one concrete), key files with line
numbers, what was tried and failed, references, and the skills the next
session should load. Specs, PRs, commits, memory files and docs are linked,
not copied. A typical handoff is 40 to 80 lines.

On resume the skill checks the handoff against the repo (branch, new commits
since it was written, moved files), reports any drift, gives a short brief and
asks before touching anything.

## Where handoffs live

`~/.claude/handoffs/<project>/<YYYY-MM-DD-HHMM>_<task-slug>.md`, outside the
repo so they never land in a commit. `<project>` is the git top level with
`/` turned into `-`. Files are owner-only (`0600`). Set `HANDOFF_HOME` to use
another folder.

Before storing, the helper script redacts common secret shapes: private keys,
AWS access keys, GitHub, Anthropic, OpenAI, Slack, Google and Stripe tokens,
JWTs, bearer tokens, credentials in URLs, and `key=value` pairs whose key looks
like a password, secret, token or API key. The skill tells the model not to
write secrets in the first place; the scrubber is a backstop. Run it by hand
with `python3 skills/handoff/scripts/handoff.py scrub <file>`.

## Install

Requires Claude Code and Python 3.

The plugin is published through my plugin marketplace,
[milenkotomic/claude-plugins](https://github.com/milenkotomic/claude-plugins).
From a shell:

```sh
claude plugin marketplace add milenkotomic/claude-plugins
claude plugin install handoff@milenkotomic
```

Or inside Claude Code: `/plugin marketplace add milenkotomic/claude-plugins`,
then `/plugin install handoff@milenkotomic`. Start a new session and type
`/handoff:handoff`.

### Update

```sh
claude plugin marketplace update milenkotomic
claude plugin update handoff@milenkotomic
```

Restart Claude Code afterwards. The plugin runs from a cached copy, so
changes pushed to this repo reach you only through an update.

### Developing the skill

```sh
git clone https://github.com/milenkotomic/handoff.git ~/Developer/handoff
~/Developer/handoff/install.sh
```

It links `skills/handoff` into `~/.claude/skills/`, so the command is
`/handoff` and edits take effect in the next session. Use one install method
per machine, not both.

## Layout

```
skills/handoff/
  SKILL.md             the skill: write and resume workflows, template
  scripts/handoff.py   store paths, secret scrubbing, latest/list; Python 3 stdlib only
  evals/               test cases for `claude plugin eval`
install.sh             symlink install for developing the skill
.claude-plugin/        plugin.json
```

## Credits

The "link artifacts instead of copying them" and "suggested skills" ideas come
from Matt Pocock's
[handoff](https://github.com/mattpocock/skills/tree/main/skills/productivity/handoff)
skill (MIT). This is an independent implementation; no text was copied.

## License

MIT, see [LICENSE](LICENSE).
