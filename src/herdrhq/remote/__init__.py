"""Single-file stdlib scripts shipped to remote machines over ssh.

Nothing in here imports anything outside the standard library: each script's
*source text* is piped to a bare `python3` on the target machine, so the only
remote requirements are python3 and herdr.
"""
