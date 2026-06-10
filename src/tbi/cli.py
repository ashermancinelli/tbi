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
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from importlib.resources import files
from pathlib import Path
from types import TracebackType
from threading import Lock

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
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

ARCHIVE_RE = r"\.zip|\.tar\.gz|\.tgz|\.tar\.xz|\.txz|\.tar\.bz2|\.tbz"
SKIP_RE = re.compile(r"^(license|readme|changelog).*|.*\.(md|txt)$", re.I)
LOG_LINES = 10
console = Console()
err_console = Console(stderr=True)


class TbiError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def die(message: str, code: int = 1) -> None:
    raise TbiError(message, code)


def make_display(log_lines: deque[str], progress: Progress) -> Panel:
    return Panel(Group(Text("\n".join(log_lines)), progress))


def config(prefix: str | None = None) -> tuple[Path, Path, Path, bool]:
    cache_dir = Path(os.getenv("TBI_CACHE_DIR") or os.getenv("GAH_CACHE_DIR") or "~/.cache/tbi").expanduser()
    if prefix:
        install_prefix = Path(prefix).expanduser()
        install_dir = install_prefix / "bin"
    else:
        install_dir_env = os.getenv("TBI_INSTALL_DIR") or os.getenv("GAH_INSTALL_DIR")
        install_dir = Path(install_dir_env or ("/usr/local/bin" if getattr(os, "geteuid", lambda: 1)() == 0 else "~/.local/bin"))
        install_dir = install_dir.expanduser()
        install_prefix = install_dir.parent
    unattended = (
        os.getenv("TBI_UNATTENDED") == "true"
        or os.getenv("GAH_UNATTENDED") == "true"
        or os.getenv("UNATTENDED") == "true"
        or not sys.stdin.isatty()
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    install_prefix.mkdir(parents=True, exist_ok=True)
    install_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir, install_prefix, install_dir, unattended


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


def clean_yaml_scalar(value: str) -> str:
    value = value.strip().split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_aliases(source) -> dict[str, str]:
    aliases, _ = read_config(source)
    return aliases


def read_config(source) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    aliases: dict[str, str] = {}
    install_rules: dict[str, dict[str, str]] = {}
    section = None
    install_key = None
    for line in source.read_text().splitlines():
        raw = line.rstrip()
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0 and stripped.endswith(":"):
            section = clean_yaml_scalar(stripped[:-1])
            install_key = None
            continue
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = clean_yaml_scalar(key)
        value = clean_yaml_scalar(value)
        if section == "install":
            if indent <= 2 and key:
                install_key = key
                install_rules.setdefault(install_key, {})
            elif install_key and key and value:
                install_rules.setdefault(install_key, {})[key] = value
        elif section in (None, "aliases") and key and value and key not in {"$schema", "aliases"}:
            aliases[key] = value
    return aliases, install_rules


def alias_sources():
    yield files("tbi").joinpath("aliases.yaml")
    yield Path("~/.config/tbi/aliases.yaml").expanduser()
    yield Path.cwd() / "tbi.yaml"
    yield Path.cwd() / ".tbi.yaml"


def aliases(say=None, refresh: bool = False) -> dict[str, str]:
    alias_map, _ = load_config(say, refresh)
    return alias_map


def load_config(say=None, refresh: bool = False) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    if refresh and say:
        say("Aliases are loaded from YAML files")
    alias_map = {}
    install_rules: dict[str, dict[str, str]] = {}
    for source in alias_sources():
        if source.is_file():
            if say:
                say(f"Loading aliases: {source}")
            source_aliases, source_install_rules = read_config(source)
            alias_map.update(source_aliases)
            for package, rules in source_install_rules.items():
                install_rules.setdefault(package, {}).update(rules)
    return alias_map, install_rules


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


def install_candidates(workdir: Path) -> list[Path]:
    executables = [p for p in workdir.rglob("*") if p.is_file() and os.access(p, os.X_OK)]
    bin_executables = [p for p in executables if "bin" in p.relative_to(workdir).parts[:-1]]
    return bin_executables or executables


def safe_relative_path(value: str, label: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        die(f"unsafe {label} path: {value}", 16)
    return path


def find_install_source(workdir: Path, source: str) -> Path:
    relative = safe_relative_path(source, "install source")
    candidates = [workdir / relative]
    candidates.extend(child / relative for child in workdir.iterdir() if child.is_dir())
    for candidate in candidates:
        if candidate.exists():
            return candidate
    die(f"install source not found in archive: {source}", 23)


def install_mapped_paths(
    target: str,
    workdir: Path,
    install_prefix: Path,
    rules: dict[str, str],
    say,
) -> list[str]:
    names = []
    prefix = install_prefix.resolve()
    for source, destination in rules.items():
        src = find_install_source(workdir, source)
        relative_destination = safe_relative_path(destination, "install destination")
        dst = (install_prefix / relative_destination).resolve()
        if prefix != dst and prefix not in dst.parents:
            die(f"unsafe install destination: {destination}", 16)
        say(f"{target}: installing {source} to {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
        if relative_destination.parts and relative_destination.parts[0] == "bin":
            installed = dst / src.name if dst.is_dir() and src.is_file() else dst
            if installed.is_file():
                names.append(installed.name)
            elif src.is_dir():
                names.extend(path.name for path in install_candidates(src))
    return names


class WorkDir:
    def __init__(self, parent: Path, keep: bool) -> None:
        self.parent = parent
        self.keep = keep
        self._tempdir: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None

    def __enter__(self) -> Path:
        if self.keep:
            self.path = Path(tempfile.mkdtemp(dir=self.parent))
        else:
            self._tempdir = tempfile.TemporaryDirectory(dir=self.parent)
            self.path = Path(self._tempdir.__enter__())
        return self.path

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._tempdir is not None:
            self._tempdir.__exit__(exc_type, exc, traceback)


def install(args: argparse.Namespace) -> int:
    cache_dir, install_prefix, install_dir, env_unattended = config(args.prefix)
    targets = args.targets
    installed_names = []
    errors = []
    kept_workdirs = []
    ui_lock = Lock()
    log_lines = deque([f"Installing {', '.join(targets)}"], maxlen=LOG_LINES)
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
            with ui_lock:
                log_lines.append(message)
                live.update(make_display(log_lines, progress))

        alias_map, install_rules = load_config(say)

        def install_one(target: str) -> list[str]:
            repo = target
            names = []
            if "/" not in repo:
                say(f"{target}: resolving alias")
                repo = alias_map.get(repo) or die(f"unknown alias: {repo}", 3)
            if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
                die(f"bad repo name: {repo}", 2)

            tag_path = "latest" if args.tag == "latest" else f"tags/{args.tag}"
            release = github_json(f"https://api.github.com/repos/{repo}/releases/{tag_path}", progress, say, f"{target}: fetching {repo}")
            say(f"{target}: found release {release.get('name') or release.get('tag_name') or 'unknown'}")

            say(f"{target}: selecting asset")
            urls = release_urls(release)
            if not urls:
                die(f"{target}: no asset matched {platform.system()} {platform.machine()}", 18)
            if len(urls) > 1:
                say(f"{target}: matched {len(urls)} assets")
                if args.unattended_select_index < 1 or args.unattended_select_index > len(urls):
                    die(f"bad selection index: {args.unattended_select_index}", 22)
                if args.unattended or env_unattended or len(targets) > 1:
                    url = urls[args.unattended_select_index - 1]
                else:
                    with ui_lock:
                        live.stop()
                    try:
                        for i, candidate in enumerate(urls, 1):
                            console.print(f"{i}. {candidate}")
                        raw = Prompt.ask(
                            "Select URL",
                            console=console,
                            choices=[str(i) for i in range(1, len(urls) + 1)],
                            default="1",
                        )
                    finally:
                        with ui_lock:
                            live.start(refresh=True)
                    url = urls[int(raw) - 1]
            else:
                url = urls[0]
            say(f"{target}: selected {url}")

            filename = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
            digest = next((a.get("digest") for a in release.get("assets", []) if a.get("browser_download_url") == url), None)

            with WorkDir(cache_dir, args.keep_temp) as workdir:
                if args.keep_temp:
                    with ui_lock:
                        kept_workdirs.append(workdir)
                    say(f"{target}: keeping work directory {workdir}")
                download = workdir / filename
                fetch(url, progress, say, f"{target}: downloading {filename}", download)

                if digest:
                    algo, expected = digest.split(":", 1)
                    say(f"{target}: verifying digest {algo}")
                    h = hashlib.new(algo)
                    task = progress.add_task(f"{target}: verifying {algo}", total=download.stat().st_size)
                    with download.open("rb") as fh:
                        while chunk := fh.read(1024 * 128):
                            h.update(chunk)
                            progress.update(task, advance=len(chunk))
                    if h.hexdigest() != expected:
                        die(f"{target}: digest mismatch for {filename}", 17)
                    progress.remove_task(task)
                    say(f"{target}: digest OK")
                else:
                    say(f"{target}: no digest for {filename}; skipping verification")

                if re.search(r"\.tar\.gz$|\.tgz$|\.tar\.xz$|\.txz$|\.tar\.bz2$|\.tbz$", filename, re.I):
                    say(f"{target}: extracting {filename}")
                    with tarfile.open(download) as tf:
                        members = tf.getmembers()
                        task = progress.add_task(f"{target}: extracting {filename}", total=len(members))
                        for member in members:
                            tf.extract(member, workdir, filter="data")
                            progress.update(task, advance=1)
                        progress.remove_task(task)
                elif filename.lower().endswith(".zip"):
                    say(f"{target}: extracting {filename}")
                    root = workdir.resolve()
                    with zipfile.ZipFile(download) as zf:
                        infos = zf.infolist()
                        task = progress.add_task(f"{target}: extracting {filename}", total=len(infos))
                        for info in infos:
                            extract_to = (workdir / info.filename).resolve()
                            if root != extract_to and root not in extract_to.parents:
                                die(f"{target}: unsafe zip path: {info.filename}", 16)
                            zf.extract(info, workdir)
                            mode = info.external_attr >> 16
                            if mode:
                                extract_to.chmod(mode & 0o777)
                            progress.update(task, advance=1)
                        progress.remove_task(task)
                else:
                    say(f"{target}: preparing binary {filename}")
                    download.chmod(download.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

                package_install_rules = install_rules.get(target) or install_rules.get(repo)
                if package_install_rules:
                    names.extend(install_mapped_paths(target, workdir, install_prefix, package_install_rules, say))
                else:
                    say(f"{target}: installing executables")
                    bins = install_candidates(workdir)
                    task = progress.add_task(f"{target}: installing", total=len(bins))
                    for binary in bins:
                        name = binary.name
                        if SKIP_RE.match(name):
                            progress.update(task, advance=1)
                            continue
                        if m := re.match(rf"^{filename_pattern()}$", name, re.I):
                            name = m.group(1)
                        destination = install_dir / name
                        if args.keep_temp:
                            shutil.copy2(binary, destination)
                        else:
                            shutil.move(str(binary), destination)
                        names.append(name)
                        say(f"{target}: installed {destination}")
                        progress.update(task, advance=1)
                    progress.remove_task(task)
            return names

        if len(targets) == 1:
            installed_names.extend(install_one(targets[0]))
        else:
            with ThreadPoolExecutor(max_workers=len(targets)) as executor:
                futures = {executor.submit(install_one, target): target for target in targets}
                for future in as_completed(futures):
                    target = futures[future]
                    try:
                        installed_names.extend(future.result())
                    except TbiError as exc:
                        errors.append((target, exc))
                        say(f"{target}: failed: {exc}")
                    except Exception as exc:
                        errors.append((target, TbiError(str(exc), 1)))
                        say(f"{target}: failed: {exc}")
    if installed_names:
        console.print(f"Finished installing {', '.join(installed_names)} to {install_dir}")
    if kept_workdirs:
        console.print("Kept work directories:")
        for workdir in kept_workdirs:
            console.print(f"  {workdir}")
    if errors:
        target, exc = errors[0]
        raise TbiError(f"{target}: {exc}", exc.code)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(prog="tbi")
        parser.add_argument("--version", action="version", version=f"tbi {__version__}")
        sub = parser.add_subparsers(dest="command")

        p_install = sub.add_parser("install")
        p_install.add_argument("targets", nargs="+")
        p_install.add_argument("--tag", default="latest")
        p_install.add_argument("--prefix")
        p_install.add_argument("--unattended", action="store_true")
        p_install.add_argument("--unattended-select-index", type=int, default=1)
        p_install.add_argument("--keep-temp", action="store_true", help="keep download/extract work directories for inspection")
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
            log_lines = deque(["Loading aliases"], maxlen=LOG_LINES)
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

                data = aliases(say, refresh=args.action == "refresh")
            if args.action == "show":
                console.print("aliases:")
                for alias, repo in sorted(data.items()):
                    console.print(f"  {alias}: {repo}")
            return 0
        return args.func(args)
    except TbiError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        return exc.code
