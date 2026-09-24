# Review of a fix to sc-hub (for the reviewing assistant)

You review a change another assistant made to this repository while setting up
sc-hub for a student. You did not write it. Its author gives you the checkout and the
failure it fixes, below this checklist. Work in that checkout. If the failure is
missing, judge from the code and say so.

Read the uncommitted changes (`git diff`, and `git status` for new files) and the
files they touch. Change nothing yourself.

Check each point:

1. **It fixes the cause.** The change fixes the reported failure where it starts,
   not by hiding it. Nothing is weakened: the key limit (`schub-gate`), the page's
   token and Host check, the password path (askpass, never stored), the account
   confirmation and the checks in the steps stay as strict as before.
2. **It is only the fix.** It touches only the files the fix needs. There are no
   unrelated edits, no reformatting, and no personal data: logins, home paths,
   e-mail addresses.
3. **No secrets.** There are no keys, tokens, passwords, sign-in codes, `auth.json`
   or `.credentials.json`, `~/.sc-hub/*.json`, or logs with any of these, in the
   code, the tests or the commit.
4. **It works everywhere.** It works on macOS, Linux and Windows: paths, quoting,
   and `ssh` differences such as Windows OpenSSH. `onboard/` stays standard
   library only (Python 3.9+).
5. **Steps can run again.** A step that is re-run still resumes and does not
   repeat work or break a finished setup.
6. **The cluster is safe.** Commands sent there quote every value. Nothing writes
   outside the student's own sc-hub folder and home, and nothing touches other
   users' files or jobs.
7. **It is tested.** `sh onboard/start.sh check` passes. A test covers the fix
   when practical (`tests/test_onboard.py`, fake cluster `tests/onboard_fakes/ssh`).

Answer with one of:
- `APPROVE`, plus one line saying why.
- `CHANGES`, plus a numbered list: file:line, what is wrong, and what to do.
