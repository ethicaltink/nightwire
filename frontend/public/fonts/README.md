# Bundled fonts

Nightwire is LAN-only, so it never loads Google Fonts at runtime. The three
families are vendored here as `.woff2` and served from `/fonts/` by FastAPI.

Run `scripts/fetch-fonts.sh` from the repository root to populate this
directory. The files are deliberately **not** committed (see `.gitignore`);
the container build runs the script, and `npm run build` copies whatever is
here into `dist/fonts/`.

**The script's upstream URLs have not been verified against the network.**
They were written offline, so a 404 is ordinary maintenance: the script says
which file it could not get and the build carries on.

Expected filenames, referenced by `@font-face` in `src/lib/tokens.css`:

| Basename | Family | Weight |
| --- | --- | --- |
| `chakra-petch-600` | Chakra Petch | 600 |
| `chakra-petch-700` | Chakra Petch | 700 |
| `inter-400` | Inter | 400 |
| `inter-500` | Inter | 500 |
| `inter-600` | Inter | 600 |
| `jetbrains-mono-400` | JetBrains Mono | 400 |

Each `@font-face` accepts **either** `<basename>.woff2` or `<basename>.ttf`,
so a file downloaded by hand in whichever format upstream offers will work
without a code change. Chakra Petch is TrueType upstream; the other two ship
woff2.

**If the files are missing the UI still renders**, falling back to the system
sans and mono stacks declared alongside each family. Headings will look
wrong, nothing will break.
