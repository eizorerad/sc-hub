# Setting sc-hub up

The setup page in detail, how the student's assistant repairs it, the older one-command installer, the key that only opens sc-hub, and the optional shared library. Back to the [README](../README.md).

## Set up (student): the setup page

The recommended way. From a copy of sc-hub on your laptop, run one command (or
paste the sc-hub link into Codex or Claude Code and ask it to set you up: the
repository's `AGENTS.md` tells it to start this page and hand it to you):

```bash
sh onboard/start.sh                                             # macOS, Linux
onboard\start.cmd                                               # Windows 10/11
```

A page opens on this computer only (`127.0.0.1`, with a one-time token in the
link). It shows a progress bar and one line per step, and asks you for
something only where it has to:

1. **Your browser for the sign-ins.** The agents on the cluster sign in with
   your MBZUAI student ChatGPT and Claude accounts: make the browser where those
   are signed in your default one (or copy the links into it).
2. **Sign in to the cluster, once.** Your login and password. The password goes
   straight to `ssh` on this computer to install a dedicated key
   (`~/.ssh/mbzuai_schub_ed25519`, alias `mbzuai-schub`); it is never stored and
   never reaches the assistant. On Windows 10, whose OpenSSH does not take a
   password from a program, a console window opens and you type it there.
3. **sc-hub on the cluster**, in `/l/users/LOGIN/schub`, set up in a Slurm job:
   your own environment and the starter datasets (about 6 GB, 10-20 minutes the
   first time).
4. **A first run**: a kernel cell and a small Slurm job with a check, in the
   project `hello`.
5. **Your assistants**: Codex (also in the ChatGPT desktop app), Claude Code and
   Claude Desktop get the sc-hub MCP server; `~/sc-hub-workspace` is created.
6. **VS Code in your workbench job**: the host `mbzuai-schub-ide` gives VS Code
   (Remote-SSH) a shell and files inside your own workbench job, which it starts
   if needed; set up once, it works for every later job.
7. **Codex and Claude Code on the cluster**, for the lab agent. Both are
   installed with their official installers and signed in from your browser:
   Codex shows a one-time code (if your workspace turned device codes off, it
   signs in the usual way through a short-lived tunnel to `localhost:1455`),
   Claude shows a code to paste back into the page. The page then shows which
   account each one uses, so you can check it is the student one.
8. **The key only opens sc-hub** (see [the installer section](#install-student-one-command)); your password login is unchanged.
9. **The dashboard mirror** starts and opens in your browser.

A step that fails says what to do and has a Retry button; running the page again
skips what is done. `--home DIR` writes everything under `DIR` instead of your
home (for trials), `--no-browser` opens nothing by itself.

### When the assistant runs it: fixes that come back

Students set up through their coding assistant, and the repository's `AGENTS.md`
and `onboard/AGENT_GUIDE.md` explain the whole setup to it. It gets these commands:
`start.sh status`, `open`, `retry <step>`, `stop`, `check` and `review-prompt`. None of
them shows a password, a sign-in code or the page's token.

When a step gets stuck, the assistant finds the cause and fixes it:
- **On the student's side** (OpenSSH, the cluster `~/.bashrc`, the VPN), with the student's consent. Nothing is committed.
- **A bug in sc-hub**: it fixes the code, runs the tests and retries the step.
  1. A second, independent assistant reviews the change against `onboard/REVIEW.md`.
  2. The assistant commits it on a branch `fix/onboard-...` and opens a pull request. It never pushes to `main` or to the branch it started from.
  3. Without push access, it writes patch files for the student to send.
  4. Without git, it installs git or copies the changed files.

This routine is for the setup. Afterwards the assistant works in
`~/sc-hub-workspace` on research. If sc-hub's plumbing fails there, it tells the
student in one line, works around the problem and notes it in
`sc-hub-issues.md`. It fixes sc-hub only when the student asks (for example
because a problem blocks the research). The goal is a working tool for the
biologist, not a perfect sc-hub.

GitHub Actions (`.github/workflows/tests.yml`) runs the tests on every pull
request: all of them on Linux, the helper's portable ones on Windows. The pilot
owner merges what is green and correct, and every student's assistant picks it
up with `git pull --ff-only` at its next start.

## Install (student): one command

The older installer, without the page (no VS Code or cluster agents setup).

Run it on your laptop in a terminal. Replace `LOGIN` with your cluster login;
your cluster password is asked once or twice, never stored.

macOS / Linux:

```bash
ssh LOGIN@login-student-lab.mbzu.ae cat LIBRARY/install/install-sc-hub.sh | bash -s -- LOGIN
```

Windows 10/11 (PowerShell, built-in OpenSSH client):

```powershell
ssh LOGIN@login-student-lab.mbzu.ae cat LIBRARY/install/install-sc-hub.ps1 | Out-String | iex
```

`LIBRARY` is the cluster folder where the pilot owner published the installer
(`SCHUB_LIBRARY_ROOT=... bash scripts/release.sh`); they give you the path. Only
people with a cluster account can download it. It:

1. creates a dedicated SSH key (no passphrase, so the assistants can connect on
   their own) and the alias `mbzuai-schub`, and installs the key on the login
   node; at the end it limits the key to sc-hub (see below);
2. copies sc-hub to `/l/users/LOGIN/schub` and sets up the workspace there in a
   Slurm job (its own environment, or a shared library's if one is set);
3. connects sc-hub to every assistant it finds: **Codex** (also Codex inside the
   ChatGPT desktop app), **Claude Code** and **Claude Desktop**;
4. creates `~/sc-hub-workspace` with instructions for the assistant (`AGENTS.md`,
   `CLAUDE.md`) and `schub-view` for the dashboard (`schub-view.cmd` on Windows).

The key opens sc-hub only: its line in `~/.ssh/authorized_keys` on the cluster
starts with `restrict,port-forwarding,command="<root>/bin/schub-gate"`, which
lets through the MCP server, the dashboard mirror (`schub dashboard` + a
read-only rsync of `view/`) and `schub-lab` sessions (their tunnel), and
refuses the rest, a shell on the login node included; every decision goes to
`<root>/logs/gate.log`. This keeps an assistant on sc-hub's checked, logged
tools (quotas and jobs come from the `cluster` tool, not from a shell).

It is a guard rail, not a security boundary:
- **VS Code's shell.** With VS Code, the same key opens a shell inside the student's own workbench job (`mbzuai-schub-ide`, for the editor).
- **Cells and tunnels.** Cells run the student's code on a compute node that shares the home folder, and the key may open tunnels.
- **Agent instructions.** The assistants' instructions tell them not to use either for anything else.

The student's own login (password) is unaffected. Delete that line to revoke the
key; re-running the installer with `SCHUB_KEY_UNRESTRICTED=1` makes it a normal
key again (not recommended). The setup page limits the key on every system; the
older Windows installer does not.

Running it again is safe: it replaces its own blocks in `~/.ssh/config` and the
Codex config (between `# >>> sc-hub >>>` markers), so a mistyped login is fixed
by rerunning with the right one. `SCHUB_KEY_PASSPHRASE=ask` makes the macOS/Linux
installer ask for a key passphrase instead (then keep the key in an ssh agent).

Then start your assistant there and just ask:

```bash
cd ~/sc-hub-workspace && codex      # or: claude
```

> "Create a project for my question: how does the IFN-beta response differ
> between PBMC cell types? Start from Kang 2018 in the library."

ChatGPT in the browser cannot connect: it only talks to public HTTPS MCP
servers, which this pilot deliberately does not run.

The MCP server is a short-lived process on the login node that talks JSON-RPC
over the SSH channel. It only reads and writes small files and calls Slurm;
cells run in the workbench job. Your remote `~/.bashrc` must not print anything
for non-interactive shells (the installer checks this). The same operations
exist as a CLI on the cluster:

```bash
~/schub/bin/schub bench-run ifn --code 'print(1)' --why "a first cell" --expect "1"
~/schub/bin/schub bench-journal ifn
~/schub/bin/schub bench-status
```

## Own environment, or a shared library

By default every student's workspace is self-contained: `bootstrap_cluster.sh`
builds a private environment and downloads the starter datasets and models into
`$SCHUB_ROOT/library-local` (about 6 GB). Tools that only a library provides
(micromamba for conda packages, cellxgene, R + Seurat, kallisto indices, Cell
Ranger) are then missing, and the tools that need them say so. Sharing datasets
between students is for later.

Optionally, someone can publish a read-only library (`scripts/publish_library.sh`)
and point a workspace at it with `SCHUB_LIBRARY`:

```
<library>/                          (default /l/users/$USER/sc-hub-library for its publisher)
  envs/<version>/  envs/current     Python env (students pin the resolved version)
  hub/<version>/                    sc-hub source snapshot
  datasets/<name>/                  data.h5ad + dataset.yaml, or FASTQ + fastq.yaml (license, sha256)
  models/celltypist/                CellTypist models
  refs/kallisto/<organism>/         prebuilt kallisto|bustools index + t2g
  refs/cellranger/<organism>/       10x references (only with Cell Ranger)
  tools/cellxgene/<version>/        cellxgene in its own venv (it pins numpy 2.0.1)
  tools/r-seurat/<version>/         R + Seurat (conda-forge, pinned micromamba)
  tools/cellranger/<version>/       optional, installed by the owner
```

With `SCHUB_LIBRARY` set and readable, `bootstrap_cluster.sh` uses it: no private
environment (saves ~6 GB each) and no duplicate datasets. If it is missing or
unreadable, bootstrap builds a private environment as above. If the library lacks
one asset, only that asset is downloaded into `library-local`. At any time `bench.fetch` in a cell or `schub fetch` in a job fetches a
download job. Datasets with a recorded checksum get the same cache keys whether
they come from the library or a fallback copy.

## The pilot owner's commands, all of them

| Task | Command |
|---|---|
| Publish or update the shared library (env, datasets, models) | `bash scripts/publish_library.sh` on the cluster |
| ...inside an allocation you already hold | `SCHUB_SRUN_ARGS="--jobid=<id> --overlap" SCHUB_GPU_CHECK=0 bash scripts/publish_library.sh` |
| Build and publish the installers | `SCHUB_LIBRARY_ROOT=<cluster folder> bash scripts/release.sh` on your laptop (`--gist` also updates a secret gist) |
| Review fixes from students' assistants | pull requests `fix/onboard-*`: the `tests` check must be green; merge into the branch students clone |
| Use a shared library in your own workspace | `SCHUB_LIBRARY=<library> bash scripts/bootstrap_cluster.sh` on the cluster (students build their own by default) |
| Install Cell Ranger (10x license; link from the 10x downloads page) | `bash scripts/install_cellranger.sh '<link>' human` on the cluster |
| Choose the lab agents' engines (owner only) | `schub engine-policy set mixed --primary claude`, `... grant <project> codex`, `schub engine-probe` |
| Give a project a lab agent | `schub goal-start <project> --goal goal.md`; `schub goal-status` / `goal-stop` |
| Write the report of a finished project | `schub goal-report <project>` (a writer turn in a Slurm slice) |
| Run the breadth evaluation | `schub eval-run evals/requests.yaml --engines claude,codex`, later `schub eval-score evals/requests.yaml --markdown` |
| Run the tests | `.venv/bin/python -m pytest --cov=schub`, then `tests/e2e/run.sh` for the dashboard |

Code changes reach students through the repository: their assistant updates the checkout with
`git pull --ff-only`, and `start.sh retry cluster` uploads it. Once the key is limited, the page asks
the password once for that. With a shared library, publishing it and re-running `bootstrap_cluster.sh`
pins the new environment.
