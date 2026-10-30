# Third-party software

Nightwire itself is MIT licensed (see `LICENSE`). It does not vendor anyone
else's source, but it **depends on** the software below, and the published
container image **bundles** it. Distributing that image means distributing
these, under their own terms.

Versions are those in the `v0.1.0` image. Check your own build with
`docker run --rm --entrypoint sh <image> -c 'ffmpeg -version; go2rtc --version'`
and `pip list` inside the image.

## Bundled in the container image

| Component | Version | License | Notes |
| --- | --- | --- | --- |
| [FFmpeg](https://ffmpeg.org/) | 7.1.5 (Debian `7.1.5-0+deb13u1`) | **GPL-2.0-or-later** | Debian's build, `--enable-gpl`, unmodified. See the note below. |
| [go2rtc](https://github.com/AlexxIT/go2rtc) | 1.9.14 | MIT | Official release binary, unmodified. |
| [go2rtc `video-rtc.js`](https://github.com/AlexxIT/go2rtc) | 1.9.14 | MIT | The browser player element, fetched at image build time. |
| [Debian](https://www.debian.org/) base (`python:3.12-slim`) | Debian 13 | various | `tini` (MIT), `util-linux` (GPL-2.0, LGPL-2.1, BSD), plus the base system. |
| [Python](https://www.python.org/) | 3.12 | PSF-2.0 | |

### The FFmpeg note, which is the one that matters

Debian builds FFmpeg with `--enable-gpl`, so the binary in this image is
**GPL-2.0-or-later**. Shipping the image is distribution, and the GPL requires
that the corresponding source be available to whoever receives it.

Two things keep this simple here:

- **FFmpeg is not linked into Nightwire.** It is a separate executable, invoked
  as a child process over a command line. Nightwire's own code stays MIT; the
  GPL applies to the FFmpeg binary that travels with the image.
- **The binary is Debian's, unmodified.** The corresponding source is Debian's
  `ffmpeg` source package, archived at
  <https://snapshot.debian.org/package/ffmpeg/> and obtainable with
  `apt-get source ffmpeg` on a Debian 13 system with `deb-src` enabled.

If you fork this and patch FFmpeg, that is on you to publish.

## Python dependencies

Installed with `pip`, not vendored. The image contains their wheels.

| Package | Version | License |
| --- | --- | --- |
| fastapi | 0.162.0 | MIT |
| starlette | 1.10.0 | BSD-3-Clause |
| uvicorn | 0.54.0 | BSD-3-Clause |
| pydantic | 2.14.2 | MIT |
| anyio | 4.15.1 | MIT |
| h11 | 0.16.0 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| httpcore | 1.0.9 | BSD-3-Clause |
| certifi | 2026.10.23 | MPL-2.0 |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| PyYAML | 6.0.3 | MIT |
| onvif-zeep-async | 4.3.0 | MIT |
| zeep | 4.3.3 | MIT |
| **WSDiscovery** | 2.1.2 | **LGPL-3.0-or-later** |

`WSDiscovery` is the one copyleft dependency. It is imported as an ordinary
Python module and never modified, which is what the LGPL permits; a recipient
can replace it with their own copy. It is also **optional**: it powers only
"Discover on network", and adding a camera by address works without it.

## Frontend

| Package | Version | License |
| --- | --- | --- |
| [hls.js](https://github.com/video-dev/hls.js) | 1.5.x | Apache-2.0 |
| [Svelte](https://svelte.dev/) | 4.2.x | MIT |
| [Vite](https://vite.dev/) | 5.4.x | MIT |
| `@sveltejs/vite-plugin-svelte` | 3.1.x | MIT |

Only hls.js ships in the built bundle; Svelte and Vite are build-time only.

## Fonts

Fetched at image build time by `scripts/fetch-fonts.sh` and served from
`/fonts`. All three are under the SIL Open Font License 1.1.

| Font | Source | License |
| --- | --- | --- |
| Chakra Petch | [google/fonts](https://github.com/google/fonts/tree/main/ofl/chakrapetch) | OFL-1.1 |
| Inter | [rsms/inter](https://github.com/rsms/inter) v4.1 | OFL-1.1 |
| JetBrains Mono | [JetBrains/JetBrainsMono](https://github.com/JetBrains/JetBrainsMono) v2.304 | OFL-1.1 |

## Protocols and trademarks

ONVIF is a trademark of the ONVIF organisation. Axis is a trademark of Axis
Communications AB. Nightwire is not affiliated with, endorsed by or certified
by either; the names appear only to describe what it talks to and what it has
been tested against.
