# Security

## What Nightwire assumes

Nightwire is **LAN-only software**. It assumes it runs on a trusted network
segment, and it is not hardened for exposure to the internet. In particular:

- **No TLS.** Traffic, including the login, is plain HTTP. Put it behind a
  reverse proxy if you need encryption on the wire.
- **One shared password**, not per-user accounts, and no audit log of who did
  what. The threat model is a household or a small site, not a tenanted one.
- **Camera credentials are visible to anyone with a shell on the host.** They
  are encrypted at rest with Fernet and never stored in a stream URI, but
  ffmpeg and go2rtc accept RTSP credentials only inside the URL, so they
  appear in `/proc/<pid>/cmdline`. This is inherent to those tools, and is
  documented rather than worked around.
- **`/metrics` is open by default**, because a Prometheus scraper cannot hold
  a session cookie. It exposes camera ids, bitrates and disk usage, never
  footage. Set `NVR_METRICS_TOKEN` to require a bearer token.
- **`uvicorn` runs without `--proxy-headers` on purpose.** Add it only behind
  a trusted reverse proxy; otherwise a client can set its own address through
  `X-Forwarded-For` and walk past the login throttle.

Everything else requires a session: the whole API, recorded footage under
`/media`, and the live WebSocket.

## Reporting a vulnerability

Open a **private security advisory** through GitHub's "Report a vulnerability"
button on the Security tab. If that is not available to you, open a normal
issue that says only that you have found a security problem and how to reach
you - **no details in a public issue**.

This is a small project with no CVE process, no security team and no response
commitment. Expect a best-effort reply rather than an SLA. Fixes land on
`main` and in a new image tag; there are no backported patch releases.

## Supported versions

The latest tag only. There is no long-term support branch.

## Hardening worth doing yourself

- Put the cameras and the NVR on their own VLAN, and let nothing else reach
  the cameras' RTSP and ONVIF ports.
- Give each camera a dedicated ONVIF user with the least privilege it needs,
  and do not reuse the camera's admin account.
- Back up `/config`. It holds the camera list and the encrypted camera
  passwords, and losing `NVR_SECRET_KEY` makes those passwords unrecoverable.
- Keep `go2rtc.yaml` as Nightwire writes it: mode 0600, because it carries
  camera credentials in its stream URLs.
