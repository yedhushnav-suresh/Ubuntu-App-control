# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 2.x (Rust) | Yes |
| 1.x (Python prototype, `legacy/`) | No |

## Reporting a vulnerability

Please do **not** open a public issue for a security problem. Send a private
report to the maintainers with:

- the affected version and how it was installed (user script or `.deb`);
- a short description of the impact;
- reproduction steps or a proof of concept;
- any suggested fix.

If the repository has GitHub private vulnerability reporting enabled, use the
**Security → Report a vulnerability** button. Otherwise contact the maintainer
listed in `Cargo.toml` / the repository profile.

You can expect an acknowledgement within a few days. Please allow time for a
fix and a coordinated release before disclosing the issue publicly.

## Threat model and guarantees

Ubuntu App Control runs as the logged-in user. It is not a sandbox and does not
try to protect against a compromised user account or a malicious desktop file
that the user chooses to run.

The application does aim to prevent these mistakes:

- deleting or moving files outside a fixed allow-list of per-user data folders;
- following symlinks out of an app folder;
- treating a shared folder (`~/.config`, `~/snap`, the home directory) as one
  app's private data;
- invoking a user-controlled program through `pkexec`;
- passing an unvalidated package or Snap name to `apt-get`/`snap`;
- overwriting a user's only desktop launcher and losing it on reset;
- leaving a quarantine manifest inconsistent with the files on disk.

## Privileged operations

Only Snap and Debian package removal use `pkexec`. The command path is resolved
from root-owned system directories (`/usr/bin`, `/bin`, `/usr/sbin`, `/sbin`)
and must be owned by uid 0 and not group/world-writable. No shell is involved.
The authenticated command is limited to `snap remove <validated-name>` or
`apt-get remove -y <validated-package>`.

## Secrets

The project does not require API keys, tokens or cloud credentials. Do not add
any to the repository. Build artifacts (`target/`, `dist/`, `.deb`) are ignored
by git.
