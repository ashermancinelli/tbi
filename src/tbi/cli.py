from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from io import BytesIO
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from . import __version__

ALIAS_DB_URL = "https://raw.githubusercontent.com/get-gah/gah-db/refs/heads/master/db.json"
ARCHIVE_RE = r"\.zip|\.tar\.gz|\.tgz|\.tar\.xz|\.txz|\.tar\.bz2|\.tbz"
SKIP_RE = re.compile(r"^(license|readme|changelog).*|.*\.(md|txt)$", re.I)
LOG_LINES = 10
console = Console()


def die(message: str, code: int = 1) -> None:
    console.print(f"[red]Error:[/red] {message}", stderr=True)
    raise SystemExit(code)


def make_display(log_lines: deque[str], progress: Progress) -> Panel:
    return Panel(Group(Text("\n".join(log_lines)), progress), height=LOG_LINES + 4)


def config() -> tuple[Path, Path, bool]:
    cache_dir = Path(os.getenv("TBI_CACHE_DIR") or os.getenv("GAH_CACHE_DIR") or "~/.cache/tbi").expanduser()
    install_dir = os.getenv("TBI_INSTALL_DIR") or os.getenv("GAH_INSTALL_DIR")
    if install_dir is None:
        install_dir = "/usr/local/bin" if getattr(os, "geteuid", lambda: 1)() == 0 else "~/.local/bin"
    unattended = (
        os.getenv("TBI_UNATTENDED") == "true"
        or os.getenv("GAH_UNATTENDED") == "true"
        or os.getenv("UNATTENDED") == "true"
        or not sys.stdin.isatty()
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    Path(install_dir).expanduser().mkdir(parents=True, exist_ok=True)
    return cache_dir, Path(install_dir).expanduser(), unattended


def filename_pattern() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        os_part = r"[._-](unknown[._-])?(linux|linux-gnu|linux-musl)"
    elif system == "darwin":
        os_part = r"[._-](apple[._-])?(darwin|macos|osx)"
    else:
        die(f"unsupported OS: {platform.system()}", 10)
    if machine in {"x86_64", "amd64"}:
        arch_part = r"[._-](amd64|x86_64|x64|64bit|universal)"
    elif machine in {"arm64", "aarch64", "armv8"}:
        arch_part = r"[._-](arm64|aarch64|universal)"
    else:
        die(f"unsupported architecture: {platform.machine()}", 11)
    return rf"([a-z][a-z0-9_-]+?)([_-]v?[0-9.]+)?(({os_part}{arch_part})|({arch_part}{os_part}))([_-][a-z0-9_-]+)?({ARCHIVE_RE})?"


def fetch(
    url: str,
    progress: Progress,
    say,
    label: str,
    dest: Path | None = None,
    allow_error: bool = False,
) -> bytes:
    say(label)
    headers = {"User-Agent": f"tbi/{__version__}"}
    if os.getenv("GITHUB_PAT"):
        headers["Authorization"] = f"token {os.environ['GITHUB_PAT']}"
    request = urllib.request.Request(url, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as exc:
        if not allow_error:
            die(f"HTTP {exc.code} while fetching {url}", 13)
        response = exc
    except urllib.error.URLError as exc:
        die(f"could not fetch {url}: {exc.reason}", 19)

    total = int(response.headers.get("Content-Length") or 0) or None
    task = progress.add_task(label, total=total)
    sink = dest.open("wb") if dest else BytesIO()
    done = 0
    with response, sink:
        while chunk := response.read(1024 * 128):
            sink.write(chunk)
            done += len(chunk)
            progress.update(task, advance=len(chunk))
        body = b"" if dest else sink.getvalue()
    if total is None:
        progress.update(task, total=done)
    progress.update(task, completed=done)
    progress.remove_task(task)
    say(f"Done: {label}")
    return body


def github_json(url: str, progress: Progress, say, label: str) -> dict:
    try:
        data = json.loads(fetch(url, progress, say, label, allow_error=True).decode())
    except json.JSONDecodeError:
        die("GitHub returned invalid JSON", 20)
    if isinstance(data, dict) and data.get("message") and not data.get("tag_name"):
        message = data["message"]
        if "rate limit" in message.lower():
            die("GitHub API rate limit exceeded; set GITHUB_PAT to authenticate", 21)
        die(f"GitHub API error: {message}", 13)
    return data


def aliases(progress: Progress, say, refresh: bool = False) -> dict:
    cache_dir, _, _ = config()
    db_path = cache_dir / "db.json"
    stale = not db_path.exists() or time.time() - db_path.stat().st_mtime > 86400
    if refresh or stale:
        fetch(ALIAS_DB_URL, progress, say, "Refreshing aliases", db_path)
    return json.loads(db_path.read_text())


def release_urls(release: dict) -> list[str]:
    pat = re.compile(rf"^{filename_pattern()}$", re.I)
    urls = [
        asset["browser_download_url"]
        for asset in release.get("assets", [])
        if pat.match(asset.get("name", "").lower()) and "linux-android" not in asset.get("name", "").lower()
    ]
    if urls:
        return urls
    md_pat = re.compile(rf"\((https://[a-z0-9./]+/{filename_pattern()})\)", re.I)
    return [m.group(1) for line in (release.get("body") or "").splitlines() if (m := md_pat.search(line.lower()))]


def install(args: argparse.Namespace) -> int:
    cache_dir, install_dir, env_unattended = config()
    repo = args.target
    log_lines = deque(maxlen=LOG_LINES)
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    with Live(make_display(log_lines, progress), refresh_per_second=10, transient=True) as live:
        def say(message: str) -> None:
            log_lines.append(message)
            live.update(make_display(log_lines, progress))

        if "/" not in repo:
            say(f"Resolving alias: {repo}")
            repo = aliases(progress, say)["aliases"].get(repo) or die(f"unknown alias: {repo}", 3)
        if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
            die(f"bad repo name: {repo}", 2)

        tag_path = "latest" if args.tag == "latest" else f"tags/{args.tag}"
        release = github_json(f"https://api.github.com/repos/{repo}/releases/{tag_path}", progress, say, f"Fetching {repo}")
        say(f"Found release: {release.get('name') or release.get('tag_name') or 'unknown'}")

        say("Selecting asset")
        urls = release_urls(release)
        if not urls:
            die(f"no asset matched {platform.system()} {platform.machine()}", 18)
        if len(urls) > 1:
            say(f"Matched {len(urls)} assets")
            if args.unattended or env_unattended:
                if args.unattended_select_index < 1 or args.unattended_select_index > len(urls):
                    die(f"bad selection index: {args.unattended_select_index}", 22)
                url = urls[args.unattended_select_index - 1]
            else:
                for i, candidate in enumerate(urls, 1):
                    say(f"{i}. {candidate}")
                raw = console.input("Select URL: ").strip()
                url = urls[int(raw or "1") - 1]
        else:
            url = urls[0]
        say(f"Selected: {url}")

        filename = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
        digest = next((a.get("digest") for a in release.get("assets", []) if a.get("browser_download_url") == url), None)

        with tempfile.TemporaryDirectory(dir=cache_dir) as work:
            workdir = Path(work)
            download = workdir / filename
            fetch(url, progress, say, f"Downloading {filename}", download)

            if digest:
                algo, expected = digest.split(":", 1)
                say(f"Verifying digest: {algo}")
                h = hashlib.new(algo)
                task = progress.add_task(f"Verifying {algo}", total=download.stat().st_size)
                with download.open("rb") as fh:
                    while chunk := fh.read(1024 * 128):
                        h.update(chunk)
                        progress.update(task, advance=len(chunk))
                if h.hexdigest() != expected:
                    die(f"digest mismatch for {filename}", 17)
                progress.remove_task(task)
                say("Digest OK")
            else:
                say(f"No digest for {filename}; skipping verification")

            if re.search(r"\.tar\.gz$|\.tgz$|\.tar\.xz$|\.txz$|\.tar\.bz2$|\.tbz$", filename, re.I):
                say(f"Extracting: {filename}")
                with tarfile.open(download) as tf:
                    members = tf.getmembers()
                    task = progress.add_task(f"Extracting {filename}", total=len(members))
                    for member in members:
                        tf.extract(member, workdir, filter="data")
                        progress.update(task, advance=1)
                    progress.remove_task(task)
            elif filename.lower().endswith(".zip"):
                say(f"Extracting: {filename}")
                root = workdir.resolve()
                with zipfile.ZipFile(download) as zf:
                    infos = zf.infolist()
                    task = progress.add_task(f"Extracting {filename}", total=len(infos))
                    for info in infos:
                        target = (workdir / info.filename).resolve()
                        if root != target and root not in target.parents:
                            die(f"unsafe zip path: {info.filename}", 16)
                        zf.extract(info, workdir)
                        mode = info.external_attr >> 16
                        if mode:
                            target.chmod(mode & 0o777)
                        progress.update(task, advance=1)
                    progress.remove_task(task)
            else:
                say(f"Preparing binary: {filename}")
                download.chmod(download.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            say("Installing executables")
            bins = [p for p in workdir.rglob("*") if p.is_file() and os.access(p, os.X_OK)]
            task = progress.add_task("Installing", total=len(bins))
            for binary in bins:
                name = binary.name
                if SKIP_RE.match(name):
                    progress.update(task, advance=1)
                    continue
                if m := re.match(rf"^{filename_pattern()}$", name, re.I):
                    name = m.group(1)
                shutil.move(str(binary), install_dir / name)
                say(f"Installed: {install_dir / name}")
                progress.update(task, advance=1)
            progress.remove_task(task)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tbi")
    parser.add_argument("--version", action="version", version=f"tbi {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_install = sub.add_parser("install")
    p_install.add_argument("target")
    p_install.add_argument("--tag", default="latest")
    p_install.add_argument("--unattended", action="store_true")
    p_install.add_argument("--unattended-select-index", type=int, default=1)
    p_install.set_defaults(func=install)

    p_aliases = sub.add_parser("aliases")
    p_aliases.add_argument("action", choices=("show", "refresh"))

    sub.add_parser("help")
    sub.add_parser("version")
    sub.add_parser("update")

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    if args.command == "help":
        parser.print_help()
        return 0
    if args.command == "version":
        console.print(f"tbi {__version__}")
        return 0
    if args.command == "update":
        console.print("tbi is a Python package; update it with pip or pipx.")
        return 0
    if args.command == "aliases":
        log_lines = deque(maxlen=LOG_LINES)
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            console=console,
        )
        with Live(make_display(log_lines, progress), refresh_per_second=10, transient=True) as live:
            def say(message: str) -> None:
                log_lines.append(message)
                live.update(make_display(log_lines, progress))

            data = aliases(progress, say, refresh=args.action == "refresh")
        if args.action == "show":
            console.print_json(data=data["aliases"])
        return 0
    return args.func(args)
