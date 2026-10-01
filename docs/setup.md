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
3. **Your own key.** A second key, `~/.ssh/mbzuai_schub_login_ed25519`, goes onto the
   cluster with the first one, so no password is asked again. It is a normal key, for
   you alone: `schub login` in a terminal (or `ssh mbzuai-login`, also for `scp` and
   `rsync`) logs you in to the login node. Unlike sc-hub's key it is not limited
   (step 8), and the assistants' instructions tell them not to use it. It has no
   passphrase, like sc-hub's: whoever can read your files can use it, so keep the
   computer's disk encrypted and its screen locked. To revoke it, delete its line
   (comment `sc-hub-login`) from `~/.ssh/authorized_keys` on the cluster. The page
   uses it only to look (does it work; is the workbench running). Updating the cluster
   side still asks your password once sc-hub's key is limited, so a human is in the loop.
   If the own key cannot be set up, the step says so and the rest goes on; an
   unreachable cluster (the VPN) stops the page for a Retry instead, because a skipped
   step is not tried again by itself. `start.sh retry login-key` tries again. An account
   set up before this step existed gets it on the next start: the page asks your
   password once, says what it installs (a normal key, no passphrase) and has a "Not
   now" button, which comes every time, whatever password the page already holds.
4. **sc-hub on the cluster**, in `/l/users/LOGIN/schub`, set up in a Slurm job:
   your own environment with the analysis tools (scanpy, CellTypist, DE, Jupyter;
   about 1.3 GB) and the starter datasets (about 150 MB): a few minutes. The
   deep-learning tools (torch with its CUDA libraries and scvi-tools: up to 6.5 GB, most
   of the download) follow in a background job you do not wait for; it starts ten
   minutes later and takes a few minutes to half an hour, depending on the network.
   The dashboard, `cluster()` and `schub doctor` say while it is on its way.
5. **A first run**: a kernel cell and a small Slurm job with a check, in the
   project `hello`.
6. **A terminal and VS Code in your workbench job**: in a terminal, `schub` lands you
   in your own workbench job on the cluster, like a workstation command: it logs in with
   your own key and runs `schub shell` on the login node, which starts the workbench
   if it is not running and then `srun --pty`s into it. You are on the compute node where
   your kernels run, with `codex` and `claude` there. `schub status` lists your jobs,
   `schub view` and `schub lab` open the dashboard and a Jupyter session. `~/.sc-hub/bin`
   is put on the PATH of new terminal windows (a marked block of its own in `~/.zshrc`,
   or in `~/.bashrc` and the login file for bash; `$ZDOTDIR` is honoured); open a new
   window to use it. Windows has `ssh schub` and `ssh mbzuai-login` instead. The setup
   only looks at the workbench through your own key (it starts nothing). The step is
   made of two parts that do not wait for each other, the terminal and VS Code, and a
   problem in one is told in the step's line with how to try again (`retry terminal`;
   `retry cluster` first if sc-hub on the cluster is older and has no `schub shell`):
   the step is skipped only if nothing came out of it, so it never holds back the key
   limit. The host `schub` is written into `~/.ssh/config` once it was seen to work. The
   assistants' key gets no way into the job from this. Only where there is VS Code, the
   host `mbzuai-schub-ide` (sc-hub's key through the gate into the job's own sshd) is set
   up as before, with the Remote-SSH extension: that part starts the workbench job if it
   is not running, takes the password after an update, and is not done again by a rerun.
7. **Codex and Claude Code on the cluster**, for the lab agent and for you. Both are
   installed with their official installers, put on your PATH there (a marked
   block at the end of `~/.bashrc` and of `~/.profile` or `~/.bash_profile`; the
   step checks that a new login finds them), and signed in from your browser:
   Codex shows a one-time code (if your workspace turned device codes off, it
   signs in the usual way through a short-lived tunnel to `localhost:1455`),
   Claude shows a code to paste back into the page. The page then shows which
   account each one uses, so you can check it is the student one. The same block keeps
   Codex's SQLite files in one folder per host (`~/.codex-sqlite/<host>`): `/home` is
   NFS, shared by every node, and the lab agent does the same, so your own `codex` is
   as safe on any node as it is. The sign-ins sit in `~/.codex` and `~/.claude` in that
   home: the lab agent and your own shells use the same accounts, nothing is kept for
   sc-hub apart.
8. **sc-hub's key only opens sc-hub** (see [the installer section](#install-student-one-command)); your own key and your password login are unchanged. On an account that is limited already, a key that replaces the old one (a new laptop) is written limited from the start, and with the gate script not there nothing is written; the page also looks at the key itself each time it signs in, and limits it again if it opens a shell.
9. **Your assistants**, after the limit (an assistant connected sooner would hold a key that is still a full shell if the run stopped in between, at a sign-in you pressed Stop on): `~/sc-hub-workspace` is created, and sc-hub is switched on
   there only. Codex gets the skill `$schub` and Claude Code `/schub`; in other folders
   they work without sc-hub. Claude Desktop (a chat app that reaches the cluster only
   through sc-hub) gets the server as before.
10. **Your dashboard** starts on this computer, in the background, at
   `http://sc-hub.localhost:27182` (the next free port if that one is taken), and
   the page takes you to its welcome: what was installed and how to work with it,
   in brief. The cluster guide for newcomers to Slurm is linked from there and from
   the dashboard's cluster overview. After a restart of the computer,
   `~/.sc-hub/bin/schub-view` (or `schub view`) brings it back (Windows: `~\.sc-hub\bin\schub-view.cmd`). The
   dashboard's program lives there, outside the folders your assistants write in.


A step that fails says what to do and has a Retry button; running the page again
skips what is done. The page also says how to open itself again (`sh onboard/start.sh open`,
or the link, shown bare: in zsh a `?` or `!` in a pasted command is read as shell, so the
page never asks for `open <link>`); it opens in your default browser by itself, with the
system's own opener (`open`, `xdg-open`) when Python's cannot. `--home DIR` writes everything under `DIR` instead of your
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
people with a cluster account can download it. The folder and every folder above it
must be open to other accounts (o+x): a private home (0700) is not, and
`release.sh` / `publish_library.sh` warn when that is the case. It:

1. creates a dedicated SSH key (no passphrase, so the assistants can connect on
   their own) and the alias `mbzuai-schub`, and installs the key on the login
   node; at the end it limits the key to sc-hub (see below);
2. copies sc-hub to `/l/users/LOGIN/schub` and sets up the workspace there in a
   Slurm job (its own environment, or a shared library's if one is set);
3. creates `~/sc-hub-workspace` with instructions for the assistant (`AGENTS.md`,
   `CLAUDE.md`), and puts the dashboard's program in `~/.sc-hub/bin` (`schub-view`,
   `schub-view.cmd` on Windows; it needs Python 3.9+);
4. connects sc-hub there, and only there: **Codex** (its server is off in
   `~/.codex/config.toml` and on in the workspace's `.codex/config.toml`, a trusted
   folder; skill `~/.codex/skills/schub`, typed as `$schub`), **Claude Code** (server
   registered for the workspace folder, `claude mcp add -s local`; skill
   `~/.claude/skills/schub`, typed as `/schub`) and **Claude Desktop**.

Working without sc-hub stays open: in any other folder the assistants do not see it,
and on the cluster you can use your own login (`ssh LOGIN@login-student-lab.mbzu.ae`,
your password) with Codex or Claude Code in your own folders. sc-hub's key opens only
sc-hub, and sc-hub keeps to `/l/users/LOGIN/schub`.

The key opens sc-hub only: its line in `~/.ssh/authorized_keys` on the cluster
starts with `restrict,port-forwarding,command="<root>/bin/schub-gate"`, which
lets through the MCP server, the dashboard mirror (`schub dashboard` + a
read-only rsync of `view/`) and `schub-lab` sessions (their tunnel), and
refuses the rest, a shell on the login node included; every decision goes to
`<root>/logs/gate.log`. This keeps an assistant on sc-hub's checked, logged
tools (quotas and jobs come from the `cluster` tool, not from a shell).

It is a guard rail, not a security boundary:
- **VS Code's shell.** Where there is VS Code, the same key opens a shell inside the student's own workbench job (`mbzuai-schub-ide`), for the editor. The terminal (`schub`) does not use this key.
- **Cells and tunnels.** Cells run the student's code on a compute node that shares the home folder, and the key may open tunnels.
- **The student's own key.** The setup page also installs a normal key of the student's own (step 3: `~/.ssh/mbzuai_schub_login_ed25519`, `schub login`, `schub`), so they log in without a password. It has no passphrase, so anything that runs as the student on their computer could use it. sc-hub's own alias never shares a connection with it (`ControlMaster no`, `ControlPath none` in its block): a connection the own key made would carry the assistants' commands past the gate.
- **Agent instructions.** The assistants' instructions (the workspace's `AGENTS.md` and the server's) tell them not to use any of these for anything else.

The student's own login (password) is unaffected. Delete that line to revoke the
key; re-running the installer with `SCHUB_KEY_UNRESTRICTED=1` makes it a normal
key again (not recommended). The setup page limits the key on every system; the
older Windows installer does not. The older installers create only sc-hub's key:
the student's own key, the `schub` command and the PATH entry for `~/.sc-hub/bin`
come from the setup page. Running an older installer over a page setup replaces
sc-hub's block in `~/.ssh/config` and with it the hosts `mbzuai-login` and `schub`:
`start.sh retry sign-in` and `retry terminal` put them back.

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
`$SCHUB_ROOT/library-local` (`scripts/setup_steps.sh quick`), then a background job
(`setup_steps.sh extras`, state in `$SCHUB_ROOT/setup-extras.json`) adds the
deep-learning stack (about 8 GB in all). Both parts install the versions of one
resolution (`$SCHUB_ROOT/env-lock.txt`), so the background part never changes what a
running kernel uses; running the setup again keeps those versions unless sc-hub's
requirements changed. One install into the environment at a time (a lock folder,
`.env-install.lock`). Re-running the setup's cluster step (`start.sh retry cluster`)
restarts a background part that failed or ended without finishing. micromamba (for a project's conda packages) and R + Seurat
(for `bench.import_seurat`) are installed there the first time a cell needs them;
cellxgene, kallisto indices and Cell Ranger come only with a library, and the tools that
need them say so. Sharing datasets between students is for later.

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
environment (saves about 8 GB each) and no duplicate datasets. If it is missing or
unreadable, bootstrap builds a private environment as above. If the library lacks
one asset, only that asset is downloaded into `library-local`. At any time `bench.fetch` in a cell or `schub fetch` in a job fetches a
download job. Datasets with a recorded checksum get the same cache keys whether
they come from the library or a fallback copy.

## Updating an installed setup

A student whose setup is finished updates with `git pull --ff-only` and `sh onboard/start.sh`:
the page runs the steps that are new (`login-key`, `terminal`) and skips the rest. Two
things need a nudge. The new `schub shell` command is part of sc-hub on the cluster, so
until `retry cluster` uploads it (the page asks the password once) the `terminal` step
says so and is skipped; `retry cluster` then sets the terminal up too. And the cluster's shell
block, which now also keeps Codex's SQLite files per host, is rewritten by `retry agents`
(which asks to confirm the accounts again).

## Where things live on the cluster

`/home` (NFS) is for what runs often and quickly: scripts, environments, sign-ins, shell
settings. `/l` (Lustre) is for datasets and files. What the setup adds follows that: the
sign-ins (`~/.codex`, `~/.claude`), Codex's SQLite files (`~/.codex-sqlite/<host>`), the
PATH block in the shell's files and the keys are in the home; datasets, projects, runs,
journals, downloads and logs are under `/l/users/LOGIN/schub`.

Not following it yet: sc-hub's own code (`src/`), its wrappers (`bin/`) and its Python
environment (`env/`, with the uv-managed Python under `.cache/`) sit in that same Lustre
folder, and so does a shared library's environment. Moving them into the home needs
`SCHUB_ROOT` to split into a code folder and a data folder, and the home's per-user quota
is not visible from a client (an environment is about 8 GB): ask HPC about it first.

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
