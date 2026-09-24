# sc-hub

This folder is sc-hub's own code, and this file is about **setting sc-hub up**
(and fixing sc-hub). The student's research happens in `~/sc-hub-workspace`, whose
own `AGENTS.md` applies there. Once the setup is done, come back here only when the
student asks to update or fix sc-hub.

## A student asks you to set up sc-hub

Read `onboard/AGENT_GUIDE.md` first. It says what every step does, how to see
where the setup stands, and what to do when a step gets stuck. In short:

1. **Update and check.**
   - If this folder is a git checkout without local changes, run `git pull --ff-only`.
   - Then run `sh onboard/start.sh check` (Windows: `onboard\start.cmd check`). A failure is a bug to fix first.
   - No git? The guide says how to install it.
2. **Start the setup page and hand it over.** The student does the rest in the browser.
   - macOS, Linux: `(umask 077; mkdir -p ~/.sc-hub && nohup sh onboard/start.sh > ~/.sc-hub/onboard.log 2>&1 &)`.
   - Windows: `Start-Process onboard\start.cmd`. It opens its own window.

   The page opens in the student's browser by itself. Check it runs with `start.sh status`. If the browser did not open, run `start.sh open`.
   - **Never paste the page's link into the chat.** It carries a token that lets whoever has it answer the page. Don't read the link from the log either.
   - **Outside the sandbox.** The page needs the network and writes `~/.ssh`, `~/.codex` and the assistants' settings, so it cannot run inside a sandbox: ask the student to approve running it outside.
   - **Leave it running** until the setup is done. `start.sh stop` ends it. It also stops by itself 15 minutes after the setup finished and the page was closed.
3. **Watch it.**
   - Run `sh onboard/start.sh status` (Windows: `onboard\start.cmd status`) now and then, and when the student says something is wrong.
   - A page started with `--home DIR` (trials) needs the same `--home DIR` on `status`, `retry` and `stop`.
   - A step waiting for the student is not stuck.
4. **Unblock it.** When a step fails:
   - Find the cause (the guide has a table of the usual ones).
   - Fix it, run `start.sh check`, then `start.sh retry <step>`.
   - If you changed the page's own code (`onboard/sc_hub_onboard/`), restart the page: `start.sh stop`, then start it again and give the student the new link.
   - Things on the student's computer or cluster account are done with their consent and are not committed.
5. **Send code fixes for review.**
   - A bug in sc-hub itself is fixed in the code, then checked by a second, independent assistant with `onboard/REVIEW.md`.
   - Then commit it on a new branch `fix/onboard-...` and open a pull request (the guide has the commands).
   - Never push to `main` or the branch you started from. Never merge it yourself.
   - The pilot owner reviews it, and every student gets it on their next update.

Always:
- **No secrets in the chat.** Never ask for the cluster password, a sign-in code or a token in the chat, and never answer the page's forms yourself. The page asks the student, and those values go straight to `ssh` or the agents' sign-in on the cluster.
- **Let the page do the steps.** Don't run ssh-keygen, the installers, key installs or edits to `~/.ssh/config` yourself. The page runs them in order, checks each one and resumes where it stopped. Looking at the cluster read-only is described in the guide.
- **Don't cancel jobs.** Never cancel the student's cluster jobs.
- **Commit only the fix.** Never commit keys, tokens, `~/.sc-hub/*.json` or logs.
- **After Ready.** When the page says Ready, the student works in `~/sc-hub-workspace`: open that folder with them.

## Working on sc-hub itself

`README.md` has the overview and the owner's commands; `docs/` has the details. Tests: `.venv/bin/python -m pytest`, then
`tests/e2e/run.sh` for the dashboard. The setup helper alone: `sh onboard/start.sh check`.
