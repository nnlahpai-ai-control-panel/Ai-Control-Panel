# Contributing

Thanks for helping make AI Server Control Panel better! Every kind of help counts: trying it on your hardware, reporting bugs, suggesting apps, or sending code.

## Easiest ways to help

1. **Try it and tell us how it went.** Open an issue with the **Hardware report** template. Include your GPUs, Ubuntu version and what worked. Reports from mixed NVIDIA + AMD rigs and older cards (GTX 10xx, Tesla P40/M40) are especially useful.
2. **Report a bug.** Use the **Bug report** template. The panel's Tasks drawer and `journalctl -u ai-panel -n 100` usually show what went wrong.
3. **Request an app for the App Store.** Use the **App request** template, ideally with a working `docker-compose.yml`.
4. **Ask questions or share ideas** in [Discussions](../../discussions).

## Adding an app to the catalog

Apps live in `apps_catalog.json`. The README's "Catalog entry format" section explains the format. Use `${APP_PORT}`, `${DATA_DIR}` and `${TZ}` in the compose file, and use `{{random}}` for passwords. Please test with `docker compose config`, and install the app from the panel once before opening a pull request.

## Code layout

| File | What it is |
|---|---|
| `app.py` | FastAPI backend (one file) |
| `index.html` | Dashboard (one file, vanilla JS + Tailwind) |
| `static/` | Local copies of Tailwind CSS and xterm.js |
| `install_app.sh` | Installer and upgrader |

After changing Tailwind classes in `index.html`, rebuild the CSS:
```bash
npx tailwindcss@3.4.16 -c static/tailwind.config.js -i static/tailwind.src.css -o static/tailwind.css --minify
```

To run it locally for development:
```bash
python3 -m venv venv && ./venv/bin/pip install fastapi "uvicorn[standard]" httpx psutil pyyaml
AIPANEL_HOME=$PWD ./venv/bin/python app.py --set-password admin devpass123
AIPANEL_HOME=$PWD sudo -E ./venv/bin/python app.py      # http://localhost:1234
```

## Pull requests

Keep each PR focused on one thing, describe how you tested it, and mention your hardware if it touches GPU code. By contributing, you agree your work is released under the Apache-2.0 license.
