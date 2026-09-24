# sc-hub

## A student asks you to set up sc-hub

Start the setup page and hand it over; the student does everything else in the browser.

- macOS, Linux: `(umask 077; mkdir -p ~/.sc-hub && nohup sh onboard/start.sh > ~/.sc-hub/onboard.log 2>&1 &)`,
  then read the line `sc-hub setup: open http://127.0.0.1:...` from that log (private: the link's token
  lets whoever has it answer the page).
- Windows: `Start-Process powershell -ArgumentList '-ExecutionPolicy','Bypass','-File','onboard\start.ps1'`
  (it opens its own window and prints the same line there).

The page opens in the student's browser; give them the link as well, in case it did not. It needs
the network and writes `~/.ssh`, `~/.codex` and the assistants' settings, so it cannot run inside a
sandbox: ask the student to approve running it outside. Keep it running; it stops by itself 15
minutes after the setup finished.

- Give the link only to the student. Never ask for the cluster password, a sign-in code or a token in the chat. The page asks for them,
  and they go straight to `ssh` or the agents' sign-in on the cluster.
- Do not do the steps yourself (ssh, ssh-keygen, installers, editing `~/.ssh/config`): the page does
  them in order, checks each one and resumes where it stopped.
- If a step fails, the page says what to do and has a Retry button. Read its details with the
  student if they ask; `~/.sc-hub/onboard.json` has the steps' state (no passwords).
- When the page says Ready, the student works in `~/sc-hub-workspace`: open that folder with them.

## Working on sc-hub itself

`README.md` has the design and the owner's commands. Tests: `.venv/bin/python -m pytest`, then
`tests/e2e/run.sh` for the dashboard.
