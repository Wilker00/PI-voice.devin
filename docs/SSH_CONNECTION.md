# Raspberry Pi SSH connection

This project is intended to connect to a Raspberry Pi over SSH from the local
development terminal.

## Connection details

The most recent Raspberry Pi connection found in the terminal history is:

```powershell
ssh hacker3@pi-03.local
```

| Setting | Value |
| --- | --- |
| Device hostname | `pi-03.local` |
| Known IP (team network) | `10.104.0.55` |
| SSH user | `hacker3` |
| Service | SSH on port `22` |
| Primary command | `ssh hacker3@pi-03.local` |

An older connection also appears in the terminal history:

```powershell
ssh hacker7@pi-07.local
```

Treat `pi-07.local` as a previous or alternate device unless the project is
intentionally moved back to it.

## Current status

Checked on October 18, 2026 from the Windows development machine:

- No active `ssh.exe` process or established TCP connection to port `22` was
  detected.
- `pi-03.local` and `pi-07.local` did not resolve on the current network.
- No per-user SSH configuration file was present at `%USERPROFILE%\.ssh\config`.

This usually means the Pi is powered off, connected to another network, or its
`.local` hostname is not being discovered by Windows.

## Connect

1. Make sure the computer and Pi are on the same local network (or the required
   VPN).
2. Confirm that SSH is enabled on the Pi.
3. From PowerShell, run:

   ```powershell
   ssh hacker3@pi-03.local
   ```

4. On a first connection, verify the displayed host-key fingerprint before
   accepting it.
5. Authenticate with the configured SSH key or the Pi user's password.

Leave the remote shell with:

```bash
exit
```

## Verify and troubleshoot

Check hostname resolution and SSH availability from PowerShell:

```powershell
ping pi-03.local
Test-NetConnection pi-03.local -Port 22
```

If `.local` resolution fails, find the Pi's current IP address from the router's
device list or by running `hostname -I` directly on the Pi. Then connect with:

```powershell
ssh hacker3@<PI_IP_ADDRESS>
```

If the host is reachable but login fails, check the username, SSH key, and the
Pi's SSH service. Do not place passwords, private keys, access tokens, or `.env`
contents in this repository.

## Get this project onto the Pi

Because this GitHub repository is private, the Pi needs approved GitHub access
before cloning it. Once authentication is configured on the Pi, use:

```bash
git clone https://github.com/Wilker00/PI-voice.devin.git
cd PI-voice.devin
```

Prefer a repository-scoped deploy key or another least-privilege credential for
the Pi. Never commit the credential itself.
