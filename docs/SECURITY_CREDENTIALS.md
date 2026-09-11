# Security credentials runbook (Ko-Work / K1)

The robot SSH password used to live in this repo and on the GitHub remote.
Removing it from the tree does **not** unpublish history — **rotate the robot
password** if that credential was ever real.

## Rules

1. **Never commit passwords, askpass scripts, or private keys.**
2. **K1Finder** reads the password only from env `K1PW` (optional). Prefer
   **key-only SSH** (`BatchMode=yes`, no askpass).
3. **Autotune / Offload / Pull** should use key-only SSH. Do not reintroduce
   hardcoded `123456` fallbacks.
4. After any password use via `SSH_ASKPASS`, delete the temporary askpass file.
5. Rotate the robot account password after any leak or suspected leak; update
   operator machines’ `authorized_keys` as needed.

## Operator checklist

- [ ] Password rotated on the robot (if ever published).
- [ ] Laptop SSH key authorized on the robot; `ssh booster@<ip>` works without a password.
- [ ] `$env:K1PW` unset in day-to-day use (key-only).
- [ ] No password literals in `desktop/*.ps1` or logs.

Cited from `desktop/K1Finder.ps1`. Council 2026-09-11 #17.
