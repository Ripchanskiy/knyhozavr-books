"""Post one book release to the Telegram channel: the EPUB as a file, a short caption, a button to the site.

Usage: python telegram_post.py <tag>   (e.g. arthur-conan-doyle-sir-nigel-v1)
       python telegram_post.py all     (every book on the site, its newest version, oldest first)
Env:   TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (@channel or -100…), GITHUB_REPOSITORY, GITHUB_TOKEN (optional),
       SITE (default https://knyhozavr.org/)
The book's data comes from the release itself: the app writes it as JSON into a comment in the release body.
"""

from __future__ import annotations

import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid

CAPTION_MAX = 1024  # Telegram's limit for a file's caption
PAUSE = 5  # seconds between posts of "all": Telegram limits how fast a bot posts to one channel


def get(url: str, token: str = "") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "knyhozavr-bot"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def release(repo: str, tag: str, token: str) -> dict:
    return json.loads(get(f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag)}", token))


def meta_and_notes(body: str) -> tuple[dict, str]:
    m = re.search(r"<!--\s*knyhozavr\s*(\{.*?\})\s*-->", body or "", re.S)
    meta = json.loads(m.group(1)) if m else {}
    notes = (body[: m.start()] if m else body or "").strip()
    return meta, notes


def caption(meta: dict, notes: str, tag: str) -> str:
    e = html.escape
    version = int(meta.get("version") or re.search(r"-v(\d+)$", tag).group(1))
    lines = [f"<b>{e(meta.get('title') or tag)}</b>"]
    if meta.get("author"):
        lines.append(e(meta["author"]))
    if meta.get("original"):
        lines.append(f"<i>{e(meta['original'])}</i>")
    lines.append("")
    if version > 1:
        lines.append(f"🆕 Нова версія {version}: зі схваленими правками.")
    if meta.get("complete"):
        who = ", ".join(meta.get("reviewers") or [])
        lines.append("✅ Повністю вичитано" + (f": {e(who)}" if who else "") + ".")
    else:
        model = meta.get("models") or "мовна модель"
        lines.append(f"Машинний переклад ({e(model)}), вичитка триває.")
    tags = " ".join("#" + re.sub(r"\W+", "_", c).strip("_") for c in meta.get("categories") or [])
    head = "\n".join(lines)
    tail = f"\n\n{tags}" if tags else ""
    if notes:
        room = CAPTION_MAX - len(head) - len(tail) - 4
        if room > 40:
            n = notes if len(notes) <= room else notes[: room - 1].rstrip() + "…"
            head += "\n\n" + e(n)
    return (head + tail)[:CAPTION_MAX]


def thumbnail(cover: bytes) -> bytes | None:
    """Telegram wants a JPEG up to 320 px and 200 KB; without Pillow: no thumbnail."""
    try:
        from PIL import Image
    except ImportError:
        return None
    img = Image.open(io.BytesIO(cover)).convert("RGB")
    img.thumbnail((320, 320))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def send_document(token: str, fields: dict, files: dict[str, tuple[str, bytes, str]], retry: bool = True) -> dict:
    boundary = uuid.uuid4().hex
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (name, data, ctype) in files.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                   f"Content-Type: {ctype}\r\n\r\n".encode())
        body.write(data)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", body.getvalue(),
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as err:  # Telegram explains what is wrong in the body (never the token)
        detail = err.read().decode(errors="replace")[:500]
        if err.code == 429 and retry:  # too fast: wait as long as Telegram asks, once
            wait = json.loads(detail).get("parameters", {}).get("retry_after", 30)
            print(f"Telegram asks to wait {wait} s", flush=True)
            time.sleep(wait + 1)
            return send_document(token, fields, files, retry=False)
        sys.exit(f"Telegram: HTTP {err.code}: {detail}")


def post(rel: dict, bot: str, chat: str, site: str) -> None:
    tag = rel["tag_name"]
    meta, notes = meta_and_notes(rel.get("body") or "")
    book = meta.get("book") or re.sub(r"-v\d+$", "", tag)
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    epub = next((n for n in assets if n.endswith(".epub")), None)
    if not epub:
        sys.exit(f"{tag}: the release has no EPUB")
    cover = next((n for n in assets if re.search(r"\.cover\.(jpe?g|png|webp)$", n)), None)

    files = {"document": (epub, get(assets[epub]), "application/epub+zip")}
    thumb = thumbnail(get(assets[cover])) if cover else None
    if thumb:
        files["thumbnail"] = ("cover.jpg", thumb, "image/jpeg")
    link = f"{site}?book={urllib.parse.quote(book)}"
    fields = {
        "chat_id": chat,
        "caption": caption(meta, notes, tag),
        "parse_mode": "HTML",
        "reply_markup": json.dumps({"inline_keyboard": [[{"text": "📖 На сайті: MD, усі версії", "url": link}]]},
                                   ensure_ascii=False),
    }
    r = send_document(bot, fields, files)
    if not r.get("ok"):
        sys.exit(f"Telegram: {r}")
    print(f"posted {tag} to {chat}", flush=True)


def site_books(repo: str, token: str) -> list[dict]:
    """The books the site shows (catalog.json: the newest version of each, archived ones left out),
    oldest first: the channel ends with the newest one on top."""
    catalog = json.loads(get(f"https://raw.githubusercontent.com/{repo}/HEAD/catalog.json"))
    tags = [b["tag"] for b in sorted(catalog["books"], key=lambda b: b.get("date") or "")]
    return [release(repo, tag, token) for tag in tags]


def main() -> None:
    arg = sys.argv[1].strip()
    bot, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    repo, gh = os.environ["GITHUB_REPOSITORY"], os.environ.get("GITHUB_TOKEN", "")
    site = os.environ.get("SITE") or "https://knyhozavr.org/"
    if arg == "all":
        books = site_books(repo, gh)
        print(f"posting {len(books)} books", flush=True)
        for n, rel in enumerate(books):
            if n:
                time.sleep(PAUSE)
            post(rel, bot, chat, site)
        return
    if not re.search(r"-v\d+$", arg):
        sys.exit(f"{arg}: not a book release (<book>-v<N>) or \"all\"")
    post(release(repo, arg, gh), bot, chat, site)


if __name__ == "__main__":
    main()
