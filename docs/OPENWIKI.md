# OpenWiki — self-updating code wiki for tap-agent-service

[OpenWiki](https://github.com/langchain-ai/openwiki) is a CLI that uses an AI
agent to generate and maintain a linked Markdown wiki (`openwiki/`) over this
codebase. This repo ships the wiring; nothing runs until you install the CLI
and opt in. **It is additive** — it does not replace the hand-written
`docs/` or the ADRs; it adds the auto-drawn, always-current code map.

## What's in the repo

| File | Purpose |
|---|---|
| `.openwikiignore` | Read boundary — secrets, `data/`, VM/model config are never scanned. |
| `openwiki/INSTRUCTIONS.md` | Scope + priorities for the doc agent (pre-seeded). |
| `scripts/openwiki-refresh.sh` | Portable wrapper: runs `openwiki --update`, never commits. |
| `scripts/git-hooks/post-merge` | Optional hook: reminds (or auto-runs) a refresh after `git pull`. Off by default. |
| `.github/workflows/openwiki-update.yml` | Optional CI: manual-dispatch only until you enable the schedule. |

## One-time setup (per machine)

```bash
npm install -g openwiki
mkdir -p ~/.openwiki && printf 'ANTHROPIC_API_KEY=sk-...\n' >> ~/.openwiki/.env
cd <this repo>
openwiki --init          # KEEP the pre-seeded openwiki/INSTRUCTIONS.md when prompted
```

Credentials live in `~/.openwiki/.env`, outside the repo — never commit keys.

## Everyday use

```bash
scripts/openwiki-refresh.sh     # regenerate; review the diff; commit openwiki/ yourself
openwiki visualize              # open the interactive graph
```

Optional automation (both opt-in):

```bash
git config core.hooksPath scripts/git-hooks   # enable the post-merge nudge
export OPENWIKI_AUTO=1                         # make that hook auto-refresh
```

CI: add `ANTHROPIC_API_KEY` as a repo secret, run the workflow manually once,
review the PR, then uncomment the `schedule:` trigger.

## ⚠️ Two things to get right first

1. **CLAUDE.md collision.** `openwiki --init` wants to write its own pointer
   into a root `CLAUDE.md` / `AGENTS.md`. This repo already has a hand-written
   `CLAUDE.md` that owns stack conventions. Let OpenWiki own **`AGENTS.md`**,
   and only *append* a "see `openwiki/`" pointer to `CLAUDE.md` — do not let it
   overwrite the conventions file.
2. **Review before automating.** Each `--update` sends source to the model
   provider. Run it manually and eyeball the first generated `openwiki/` for
   any leaked secret/PII **before** enabling the git hook or CI schedule.
