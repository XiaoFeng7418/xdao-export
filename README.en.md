# Xdao Thread Exporter

[简体中文](README.md) ｜ **English**

**Download a whole X island thread and keep it on disk.** Unzip the portable build, double click,
done — no Python install, no copying cookies out of the browser devtools.

[![tests](https://github.com/XiaoFeng7418/xdao-export/actions/workflows/tests.yml/badge.svg)](https://github.com/XiaoFeng7418/xdao-export/actions/workflows/tests.yml)
[![latest release](https://img.shields.io/github/v/release/XiaoFeng7418/xdao-export)](https://github.com/XiaoFeng7418/xdao-export/releases/latest)
[![license](https://img.shields.io/github/license/XiaoFeng7418/xdao-export)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

**What it is**: a Windows desktop tool (also usable as a plain command line program) that pulls a
thread on X island (nmbxd1.com / nmbxd.com) from the first reply to the last and writes it to a
local file.

**What it solves**: a guest session only sees the first 100 pages of a long thread, so the tail end
is simply unreachable; saving pages one by one is slow and loses the images, the reply tree and the
quote links; the saved pages often show missing images once opened elsewhere.

**How it differs from the usual workarounds**:

- **Portable**: download one zip, unzip, double click. No Python, no environment setup.
- **No devtools needed to log in**: click once, log in through the system browser, and the tool
  picks the session back up. The captcha is still solved by a human, once.
- **HTML is a single self-contained file**: images are embedded, so it opens offline and on another
  machine, and it prints or saves to PDF directly.
- **Five formats, resumable downloads, update watch**: HTML / PDF / EPUB / Markdown / TXT, resume
  where the last run stopped, and get notified when a watched thread gains new replies.

![Screenshot](docs/screenshot.png)

## Download

Windows portable build: see the [Releases](https://github.com/XiaoFeng7418/xdao-export/releases)
page. Each release ships `xdao-export-vX.Y.Z-win64.zip` plus a `.sha256` file; verify the hash
before running if in doubt.

## Run from source

Python 3.10+ with tkinter, standard library only:

```powershell
python main.py                              # GUI
python main.py 50000001 -f markdown -o .    # CLI
```

## Formats

| Format | Notes |
|---|---|
| HTML | single file with embedded images, or images in a sibling folder |
| PDF | rendered with the system Chrome/Edge; can also print from the HTML |
| EPUB | e-book, images embedded or linked |
| Markdown | plain text with image links or a folder |
| TXT | plain text, smallest output |

## Be a good citizen

The tool keeps a 0.08 s gap between requests, retries at most twice, honours `Retry-After`, and only
exports what the user is already allowed to read. Please keep those defaults.

## License

MIT. Copyright (c) 2026 XiaoFeng7418 (GitHub [@XiaoFeng7418](https://github.com/XiaoFeng7418)).
See [`LICENSE`](LICENSE).

This is an unofficial third-party tool; it is not affiliated with, endorsed by or sponsored by
X island. The Chinese README (`README.md`) is the authoritative document — the policies, the privacy
note and the code signing policy are only maintained there.
