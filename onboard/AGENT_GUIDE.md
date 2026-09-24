# The sc-hub setup, for the assistant that runs it

You (Codex, Claude Code, or another coding assistant) run the setup with a student.
The student uses the page. Your job is to start the page, watch it, and unblock it
when something gets stuck: find the cause, fix it, check the fix, and send code
fixes for review so the next student does not hit the same problem.

## The commands you have

Run these from the root of the sc-hub checkout. `start.sh` / `start.ps1` find a
Python (or fetch one with uv) and pass the arguments on.

| What | macOS, Linux | Windows |
|---|---|---|
| Start the page | `sh onboard/start.sh` | `powershell -ExecutionPolicy Bypass -File onboard\start.ps1` |
| Each step: status, detail, hint, last log lines | `sh onboard/start.sh status` (`--json` for all of it) | `... start.ps1 status` |
| Ask the running page to retry | `sh onboard/start.sh retry [STEP]` (no STEP: every failed step) | `... start.ps1 retry [STEP]` |
| Stop the page (to restart it) | `sh onboard/start.sh stop` | `... start.ps1 stop` |
| The helper's own tests | `sh onboard/start.sh check` (ends with `check: passed`) | `... start.ps1 check` (the portable part) |
| The reviewer's task (below) | `sh onboard/start.sh review-prompt "FAILURE"` | `... start.ps1 review-prompt "FAILURE"` |

- **Where they look.** `status`, `retry` and `stop` read `~/.sc-hub/page.json`, which the running page writes for you. If the page was started with `--home DIR`, pass the same `--home DIR` to them, in any order (`status --home DIR`). A relative DIR is taken from the folder you run the command in.
- **No secrets.** None of them prints a password, a sign-in code or the page's token.
- **The steps start with the page.** A form waiting for the student shows only its title and field names: that step is waiting for the student, not stuck.

Things you must not do:
- **Secrets stay out of the chat.** Never ask for the cluster password, a sign-in code or a token in the chat. Never answer the page's forms yourself: they are the student's.
- **The page runs the steps, not you.** Don't run ssh-keygen, the installers, or edits to `~/.ssh/config` yourself. The page does them in order and resumes. Fix the cause, then use `retry`.
- **Keep private files private.** Never read, print or commit `~/.ssh/*` keys, `~/.codex/auth.json`, `~/.claude/.credentials.json` or `~/.sc-hub/page.json`.

## What each step does

The steps run in this order. A step that is done is skipped on the next run.

| Step id | What happens | Where it writes |
|---|---|---|
| `computer` | Looks for ssh, ssh-keygen, codex, claude and VS Code on this computer | nothing |
| `browser` | The student confirms their student accounts are in the default browser | nothing |
| `sign-in` | Login and password on the page. The password goes to ssh through askpass once, to install a dedicated key. On Windows 10, a console window asks for it. | `~/.ssh/config` block `# >>> sc-hub >>>`, `~/.ssh/mbzuai_schub_ed25519`, the cluster's `~/.ssh/authorized_keys` |
| `cluster` | Uploads this checkout to `/l/users/LOGIN/schub/src/sc-hub`, then runs `scripts/bootstrap_cluster.sh`. That runs a Slurm job on ws-ia (1 GPU, 1 h) that builds the environment and downloads the starter datasets (about 6 GB, 10-20 min the first time). | cluster: `/l/users/LOGIN/schub` |
| `hello` | Project `hello`: one kernel cell (starts the workbench job), then one small `%%slurm` job with a check | cluster: `schub/projects/hello` |
| `assistants` | Adds the sc-hub MCP server to Codex, Claude Code and Claude Desktop | `~/.codex/config.toml` block, `claude mcp add`, Claude Desktop's config (backup `.bak-schub`), `~/sc-hub-workspace` |
| `vscode` | `schub ide-setup` on the cluster, the `mbzuai-schub-ide` host, one test connection into the workbench job. Skipped without VS Code. | `~/.ssh/config` block, cluster `schub/ide` |
| `agents` | Installs Codex and Claude Code on the login node and signs them in with the student's accounts: a device code for Codex (or the usual sign-in through a tunnel to 127.0.0.1:1455), a pasted code for Claude. Then the student confirms the accounts. | cluster: `~/.codex`, `~/.claude`, `~/.local/bin` |
| `limit` | Limits the key to sc-hub: the key's line in `authorized_keys` gets `restrict,port-forwarding,command=".../schub-gate"` | cluster `~/.ssh/authorized_keys` (backup `.schub-backup`) |
| `dashboard` | Starts `schub-view` in the workspace | nothing |

Before `limit` is done, you can look at the cluster with the key:
`ssh -o BatchMode=yes mbzuai-schub '<command>'`. Useful commands:
- `squeue -u $USER` shows jobs waiting or running.
- `~/schub/bin/schub doctor` checks the installation.
- `~/schub/bin/schub bench-status` shows the workbench.
- `tail -50 ~/schub/logs/<file>` shows recent logs.

Look only, and change nothing there by hand. After `limit`, the key runs only
sc-hub's own commands. Use the MCP tools (`cluster_overview`) or ask the student
to run a command in their own terminal with their password.

## When a step fails or seems stuck

1. **Read.** Run `start.sh status` and read the failed step's detail, hint and log lines. `waiting for the student` is not stuck.
2. **Find the cause.** Most failures are one of these:

| Symptom | Cause | What to do |
|---|---|---|
| `ssh is not installed` | No OpenSSH client | macOS/Linux: install openssh. Windows: Settings → System → Optional features → OpenSSH Client. Then `retry`. |
| `cannot be reached ... campus Wi-Fi or the VPN?` | Off campus | The student joins the VPN or campus Wi-Fi. Then `retry`. |
| `refused the login` | Wrong login or password | The student types them again on the page |
| `too old to take the password` | OpenSSH < 8.4 outside Windows | Update OpenSSH |
| `your cluster shell prints N bytes` | The cluster's `~/.bashrc` prints on non-interactive logins | The student adds the line below near the top of `~/.bashrc` on the cluster. Then `retry cluster`. |
| `cluster` or `hello` waits long | The job waits for a slot: ws-ia allows 2 running jobs per user | `squeue -u $USER`. If the student's own jobs hold the slots, they decide what to stop. Never cancel jobs yourself. |
| `the setup on the cluster failed` | The bootstrap log shows it: disk quota, a download, a package | Read the log lines. If sc-hub is at fault, it is a code fix (below). |
| VS Code connection did not open | The workbench job did not start in time | `retry vscode` later |
| `port 1455 is taken` | Another sign-in on the same login node | Wait a few minutes, `retry agents` |
| `the key still opens a shell` | The limit did not apply | `retry limit`. If it stays, report it. |

The `~/.bashrc` line, before any command that prints:

```bash
[[ $- == *i* ]] || return
```

3. **Fix the cause, not the symptom.**
   - Anything on the student's computer or cluster account (installing OpenSSH, the `~/.bashrc` line, the VPN) is done by the student or with their consent. It is not committed.
   - A bug in sc-hub itself (this repository) is fixed in the code, and the fix goes out for review (next section).
   - Never make a check pass by weakening it: the key limit, the page token, the Host check, the password handling and the account confirmation stay as they are.
4. **Check.**
   - Run `start.sh check`. The tests must pass. Add a test for the bug when practical: `tests/test_onboard.py` has a fake cluster in `tests/onboard_fakes/ssh`.
   - **A fix under `onboard/sc_hub_onboard/`** (the page's own code) needs a page restart: the running page still has the old code. Run `start.sh stop`, then start the page again as in `AGENTS.md`. Give the student the new link. The steps resume, and a failed step runs again.
   - **A fix anywhere else** (`scripts/`, `src/`, `templates/`) reaches the cluster on `start.sh retry cluster`: that step uploads this checkout.
   - Then watch `status` until the step is done.

## Sending a code fix for review

Fixes go to a branch and a pull request, never straight into the branch everyone
updates from. The pilot owner reviews and merges. After that, every student's
assistant gets the fix on its next update.

1. **Independent review first.** A second assistant reviews your change against `onboard/REVIEW.md`. `review-prompt` prints its whole task: the checklist, where the checkout is, and the failure you give it. The failure is the step and its detail from `status`, or the test that failed in `check`.
   - Claude Code: start a subagent whose task is that output.
   - From a terminal, pipe it into another assistant. On Windows, use `onboard\start.ps1 review-prompt "..."` in the same way.
     ```bash
     sh onboard/start.sh review-prompt "hello: the job ended as ['COMPLETED'] with checks ['pass']" \
       | claude -p --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git status:*),Bash(sh onboard/start.sh check:*)"
     sh onboard/start.sh review-prompt "..." | codex exec --sandbox read-only -
     ```
   - **Did it run?** A reviewer's answer ends with `APPROVE` or `CHANGES`. Anything else is no review, for example `Failed to authenticate` or a usage limit. Then use the other reviewer. If none can run, say so in the pull request.

   If it asks for changes, make them and review again, at most twice. If it still does not approve, do not submit: tell the student what is wrong and write it into the report (step 4).
2. **Commit on a new branch.** Use the student's own git identity: if `git config user.name` is empty, ask the student what name and e-mail to use.
   ```bash
   git switch -c fix/onboard-<short-name>
   git add <only the files of the fix>
   git diff --cached          # check: no keys, tokens, passwords, onboard.json, logs, personal paths
   git commit -m "fix: <what was wrong>" -m "<cause, the fix, how it was checked>"
   ```
3. **Push and open the pull request.**
   - Run `git push -u origin fix/onboard-<short-name>`.
   - With GitHub CLI signed in: `gh pr create --base <the branch you started from> --fill`.
   - Without it: give the student the link `git push` printed. If there is none, give `https://github.com/eizorerad/sc-hub/compare/<base>...fix/onboard-<short-name>?expand=1`. From a fork, the last part is `<fork owner>:fix/onboard-<short-name>`.

   Never push to `main` or the branch you started from. Never force-push, and never merge your own pull request.
4. **If pushing is refused** (no write access):
   - Run `git format-patch <base>..HEAD -o ~/sc-hub-fixes/`.
   - Write `~/sc-hub-fixes/REPORT.md` with the failed step, the cause, the fix and how it was checked.
   - Ask the student to send that folder to the pilot owner, the person who gave them the link.

### No git on this computer

Install it with the student's consent:
- macOS: `git --version` opens the Command Line Tools installer, or `brew install git`.
- Windows: `winget install --id Git.Git -e`, then open a new terminal.
- Linux: `sudo apt-get install git` or `sudo dnf install git`. The student types their password in their own terminal.

If sc-hub was downloaded as a ZIP, turn the folder into a checkout without losing the fix:
```bash
git init && git remote add origin https://github.com/eizorerad/sc-hub.git
git fetch origin <the branch the ZIP came from> && git reset --mixed FETCH_HEAD   # your edits stay as changes
```
Then continue from step 2 above. If git cannot be installed:
- Copy the changed files into `~/sc-hub-fixes/` with the same `REPORT.md`.
- Ask the student to send that folder to the pilot owner.

## Updating sc-hub

At the start, if this is a git checkout with no local changes, update it with
`git pull --ff-only`. Then run `start.sh check`.

If `check` fails before the page has started:
1. Fix the bug and get `check` green first.
2. Then start the page. It runs from this folder, whether the fix is committed or not.
3. Do the review and the pull request while the student goes through the page.

Before you change anything, note the commit that `status` prints: that is the
version the student ran. Put it in the report and the pull request.
