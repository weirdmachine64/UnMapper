#!/usr/bin/env python3
"""
UnMapper: backtrack through sourcemaps to recover original source.

Discovers and reconstructs source trees from web apps that ship sourcemaps.

Usage:
    unmapper https://github.com -o ./src -t 16 -v
"""

import argparse
import asyncio
import base64
import json
import re
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text


SOURCE_MAPPING_URL_RE = re.compile(
    # Anchor to start-of-line so we don't match `"//# sourceMappingURL=..."`
    # literals embedded in JS that constructs data URIs at runtime.
    rb'(?:^|[\r\n])[ \t]*(?://|/\*)[#@]\s*sourceMappingURL\s*=\s*([^\s\*]+)',
    re.IGNORECASE,
)
CHUNK_MANIFEST_RE = re.compile(
    rb'\{(?:"?\d+"?:"[A-Za-z0-9_-]{4,40}",?){2,}\}'
)
WEBPACK_PUBLIC_PATH_RE = re.compile(
    rb'(?:[a-zA-Z_$][\w$]*)\.p\s*=\s*["\']([^"\']+)["\']'
)
# Captures the exact chunk-URL template from a webpack .u function. Handles:
#   x.u = e => "chunk-" + e + "-" + map[e] + ".js"
#   x.u = function(e){return "PREFIX"+e+"SEP"+{1:"a",...}[e]+"SUFFIX"}
# Each of prefix / sep / suffix is optional (some bundlers omit one).
# Backreferences ensure both `e` references are the same identifier.
CHUNK_TEMPLATE_RE = re.compile(
    rb'\.u\s*=\s*'
    rb'(?:function\s*)?'
    rb'\(?\s*([a-zA-Z_$][\w$]*)\s*\)?\s*'
    rb'(?:=>\s*|\{\s*(?:[^{}]*?\breturn\b\s*)?)'
    rb'(?:"([^"]*)"\s*\+\s*)?'
    rb'\1'
    rb'(?:\s*\+\s*"([^"]*)")?'
    rb'\s*\+\s*'
    rb'(?:\{[^{}]*\}|[a-zA-Z_$][\w$]*)'
    rb'\s*\[\s*\1\s*\]'
    rb'(?:\s*\+\s*"([^"]*)")?',
    re.DOTALL,
)
# Some bundlers (GitHub, custom webpack) emit `.u` as a long if-cascade
# of exact filenames instead of a formula:
#   if(26533===e)return""+e+"-f22c29ae5e9b1ed2.js"
#   if(83465===e)return"primer-react-a3ca68e253d40c8c.js"
# Each branch yields a complete filename, so no manifest/template guessing
# is needed. Group 1 or 2 = chunk id; group 3 = first string literal;
# group 4 = optional second string after `+<var>+`.
CHUNK_IFCASCADE_RE = re.compile(
    rb'if\s*\(\s*'
    rb'(?:(\d+)\s*===\s*[a-zA-Z_$][\w$]*'
    rb'|[a-zA-Z_$][\w$]*\s*===\s*(\d+))'
    rb'\s*\)\s*return\s*'
    rb'"((?:[^"\\]|\\.)*)"'
    rb'(?:\s*\+\s*[a-zA-Z_$][\w$]*\s*\+\s*"((?:[^"\\]|\\.)*)")?',
    re.DOTALL,
)


@dataclass
class Stats:
    pages: int = 0
    js_found: int = 0
    js_fetched: int = 0
    js_404: int = 0
    map_404: int = 0
    maps_found: int = 0
    maps_parsed: int = 0
    files_written: int = 0
    bytes_written: int = 0
    errors: list = field(default_factory=list)


class UI:
    """Encapsulates rich progress + recent-activity feed."""

    def __init__(self, console: Console, recent_size: int = 6):
        self.console = console
        self.recent: deque = deque(maxlen=recent_size)

        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description:<14}"),
            BarColumn(bar_width=40),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console,
            expand=False,
            transient=False,
        )
        self.task_pages = self.progress.add_task("Pages", total=0)
        self.task_js = self.progress.add_task("JS files", total=0)
        self.task_maps = self.progress.add_task("Sourcemaps", total=0)
        self.task_files = self.progress.add_task("Files written", total=None)

        self.live: Optional[Live] = None

    def _render(self):
        if not self.recent:
            log_body = Text("(no activity yet)", style="dim")
        else:
            log_body = Text()
            for line in self.recent:
                log_body.append_text(line)
                log_body.append("\n")
        log_panel = Panel(log_body, title="Recent", border_style="cyan",
                          padding=(0, 1))
        return Group(self.progress, log_panel)

    def start(self):
        self.live = Live(self._render(), console=self.console,
                         refresh_per_second=8, transient=False)
        self.live.__enter__()

    def stop(self):
        if self.live:
            self.live.__exit__(None, None, None)
            self.live = None

    def _refresh(self):
        if self.live:
            self.live.update(self._render())

    def log(self, icon: str, kind: str, msg: str, style: str = ""):
        line = Text()
        line.append(f"{icon} ", style=style or "")
        line.append(f"{kind:<5}", style="bold " + (style or ""))
        line.append(" ")
        line.append(msg, style="dim")
        self.recent.append(line)
        self._refresh()

    def set_total(self, task, total):
        self.progress.update(task, total=total)

    def advance(self, task, n=1):
        self.progress.advance(task, n)
        self._refresh()


class SourceMapExtractor:
    def __init__(
        self,
        output_dir: Path,
        threads: int = 10,
        timeout: int = 30,
        verbose: bool = False,
        guess_maps: bool = True,
        ui: Optional[UI] = None,
    ):
        self.output_dir = output_dir
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.verbose = verbose
        self.guess_maps = guess_maps
        self.ui = ui

        self.seen_js: set = set()
        self.seen_maps: set = set()
        self.seen_pages: set = set()
        # Bases derived from ground-truth <script src> URLs, used to
        # construct chunk URLs instead of regex-guessing the public path.
        self.observed_bases: set = set()
        self.stats = Stats()
        self.sem = asyncio.Semaphore(threads)
        self.executor = ThreadPoolExecutor(
            max_workers=threads, thread_name_prefix="unmapper-worker",
        )
        self.write_lock = asyncio.Lock()

    async def fetch(self, session, url, binary=False, kind='other'):
        async with self.sem:
            try:
                async with session.get(url, timeout=self.timeout,
                                       allow_redirects=True) as r:
                    if r.status >= 400:
                        if r.status == 404:
                            if kind == 'js':
                                self.stats.js_404 += 1
                            elif kind == 'map':
                                self.stats.map_404 += 1
                        # 404s on probed chunks are routine noise; only
                        # surface unexpected statuses to the live feed.
                        if self.verbose and r.status != 404:
                            self.ui and self.ui.log(
                                "✗", "http",
                                f"{r.status} {self._short(url)}",
                                style="red",
                            )
                        return None
                    return await (r.read() if binary
                                  else r.text(errors='replace'))
            except Exception as e:
                self.stats.errors.append(
                    f"{url}: {type(e).__name__}: {e}"
                )
                if self.verbose:
                    self.ui and self.ui.log(
                        "✗", "err",
                        f"{type(e).__name__} {self._short(url)}",
                        style="red",
                    )
                return None

    @staticmethod
    def _short(url, width=80):
        if len(url) <= width:
            return url
        return url[:width - 1] + "…"

    async def process_html(self, session, url):
        if url in self.seen_pages:
            return
        self.seen_pages.add(url)
        self.stats.pages += 1
        if self.ui:
            self.ui.set_total(self.ui.task_pages, len(self.seen_pages))

        html = await self.fetch(session, url)
        if self.ui:
            self.ui.advance(self.ui.task_pages)
        if not html:
            return
        if self.verbose:
            self.ui and self.ui.log("→", "page", self._short(url),
                                    style="cyan")

        soup = BeautifulSoup(html, 'html.parser')

        for tag in soup.find_all('script', src=True):
            self.add_js(urljoin(url, tag['src']), observed=True)

        for tag in soup.find_all('link', href=True):
            rel = ' '.join(tag.get('rel', [])).lower()
            if 'modulepreload' in rel:
                self.add_js(urljoin(url, tag['href']), observed=True)
            elif 'preload' in rel and tag.get('as') == 'script':
                self.add_js(urljoin(url, tag['href']), observed=True)

        for tag in soup.find_all('script'):
            if tag.string and len(tag.string) > 500:
                self.extract_chunks(tag.string.encode('utf-8'), url)

    def add_js(self, url, observed: bool = False):
        url = url.split('#')[0]
        if url in self.seen_js:
            return
        path = urlparse(url).path
        if not (path.endswith('.js') or path.endswith('.mjs')):
            return
        self.seen_js.add(url)
        self.stats.js_found += 1
        if observed:
            parsed = urlparse(url)
            dir_path = parsed.path.rsplit('/', 1)[0] + '/'
            if dir_path and dir_path != '/':
                self.observed_bases.add(
                    f"{parsed.scheme}://{parsed.netloc}{dir_path}"
                )
        if self.ui:
            self.ui.set_total(self.ui.task_js, len(self.seen_js))

    def _candidate_bases(self, text: bytes, base_url: str) -> set:
        """Determine where chunks are served from. Priority:
        1. Bases observed from <script src> tags in the HTML (ground truth)
        2. Webpack public path from the JS (if any), resolved against base_url
        3. Directory of the current JS as last resort
        """
        if self.observed_bases:
            return set(self.observed_bases)

        bases = set()
        m = WEBPACK_PUBLIC_PATH_RE.search(text)
        if m:
            pub = m.group(1).decode('utf-8', 'ignore')
            bases.add(urljoin(base_url, pub))

        parsed = urlparse(base_url)
        dir_path = parsed.path.rsplit('/', 1)[0] + '/'
        bases.add(f"{parsed.scheme}://{parsed.netloc}{dir_path}")
        return bases

    def _parse_ifcascade(self, text: bytes) -> list:
        """Extract explicit chunk filenames from a webpack `.u` if-cascade.
        Returns a list of filename strings (already resolved, no template
        substitution needed)."""
        filenames = []
        for m in CHUNK_IFCASCADE_RE.finditer(text):
            chunk_id = (m.group(1) or m.group(2)).decode('ascii')
            first = m.group(3).decode('utf-8', 'ignore')
            second = m.group(4)
            if second is not None:
                filenames.append(
                    first + chunk_id + second.decode('utf-8', 'ignore')
                )
            else:
                filenames.append(first)
        return filenames

    def _parse_chunk_templates(self, text: bytes) -> list:
        """Extract chunk-URL templates from any webpack .u function in `text`.
        Returns a list of (prefix, sep, suffix); each builds a filename via
        f'{prefix}{chunk_id}{sep}{chunk_hash}{suffix}'."""
        templates = []
        for m in CHUNK_TEMPLATE_RE.finditer(text):
            prefix = (m.group(2) or b'').decode('utf-8', 'ignore')
            sep = (m.group(3) or b'').decode('utf-8', 'ignore')
            suffix = (m.group(4) or b'').decode('utf-8', 'ignore')
            templates.append((prefix, sep, suffix))
        # Dedup while preserving order
        seen = set()
        unique = []
        for t in templates:
            if t not in seen:
                seen.add(t)
                unique.append(t)
        return unique

    def extract_chunks(self, text: bytes, base_url: str):
        bases = self._candidate_bases(text, base_url)

        # If the bundle uses an if-cascade `.u` (GitHub-style), each branch
        # yields an exact filename, so skip the manifest+template path entirely.
        cascade = self._parse_ifcascade(text)
        if cascade:
            if self.verbose and self.ui:
                self.ui.log("•", "ifcascade",
                            f"{len(cascade)} explicit chunks",
                            style="yellow")
            for filename in cascade:
                for base in bases:
                    self.add_js(urljoin(base, filename))
            return

        templates = self._parse_chunk_templates(text)
        if not templates:
            return

        if self.verbose and self.ui:
            for p, s, sx in templates:
                self.ui.log("•", "tpl",
                            f"{p}{{id}}{s}{{hash}}{sx}",
                            style="yellow")

        for match in CHUNK_MANIFEST_RE.finditer(text):
            try:
                raw = match.group(0).decode('utf-8', 'ignore')
                jsonish = re.sub(r'(?<![\w"])(\d+)(?=:)', r'"\1"', raw)
                manifest = json.loads(jsonish)
            except (ValueError, json.JSONDecodeError):
                continue
            if len(manifest) < 5:
                continue

            for chunk_id, chunk_hash in manifest.items():
                if (not isinstance(chunk_hash, str)
                        or len(chunk_hash) < 6):
                    continue
                for prefix, sep, suffix in templates:
                    filename = f"{prefix}{chunk_id}{sep}{chunk_hash}{suffix}"
                    for base in bases:
                        self.add_js(urljoin(base, filename))

    async def process_js(self, session, url):
        body = await self.fetch(session, url, binary=True, kind='js')
        if self.ui:
            self.ui.advance(self.ui.task_js)
        if not body:
            return
        self.stats.js_fetched += 1
        if self.verbose:
            self.ui and self.ui.log(
                "✓", "js", f"{self._short(url)} ({len(body)//1024} KiB)",
                style="green",
            )

        self.extract_chunks(body, url)
        # update total in case new chunks were discovered mid-flight
        if self.ui:
            self.ui.set_total(self.ui.task_js, len(self.seen_js))

        map_url = None
        m = SOURCE_MAPPING_URL_RE.search(body[-4000:])
        if m:
            try:
                ref = m.group(1).decode('utf-8')
                map_url = (urljoin(url, ref)
                           if not ref.startswith('data:') else ref)
            except UnicodeDecodeError:
                pass

        if map_url:
            await self.process_map(session, map_url)
        elif self.guess_maps:
            # Only probe `.js.map` when the bundle didn't declare an
            # explicit sourceMappingURL; otherwise we'd 404 every JS
            # whose map is inlined as a data URI.
            await self.process_map(session, url.split('?')[0] + '.map')

    async def process_map(self, session, url):
        url = url.split('#')[0]
        if url in self.seen_maps:
            return
        self.seen_maps.add(url)
        if self.ui:
            self.ui.set_total(self.ui.task_maps, len(self.seen_maps))

        if url.startswith('data:'):
            try:
                header, payload = url.split(',', 1)
                if 'base64' in header:
                    # Real-world data URIs may use URL-safe alphabet,
                    # omit `=` padding, or contain whitespace from
                    # prettifiers; normalize all three before decoding.
                    payload = ''.join(payload.split())
                    payload = payload.replace('-', '+').replace('_', '/')
                    payload += '=' * (-len(payload) % 4)
                    payload = base64.b64decode(payload).decode('utf-8')
                else:
                    payload = unquote(payload)
                data = json.loads(payload)
            except Exception as e:
                self.stats.errors.append(
                    f"inline map decode ({self._short(url, 60)}): {e}"
                )
                if self.ui:
                    self.ui.advance(self.ui.task_maps)
                return
        else:
            text = await self.fetch(session, url, kind='map')
            if not text:
                if self.ui:
                    self.ui.advance(self.ui.task_maps)
                return
            # JSON parsing of multi-MB maps is offloaded to the thread pool
            loop = asyncio.get_running_loop()
            try:
                data = await loop.run_in_executor(
                    self.executor, json.loads, text,
                )
            except json.JSONDecodeError:
                if self.ui:
                    self.ui.advance(self.ui.task_maps)
                return

        self.stats.maps_found += 1
        if self.verbose:
            self.ui and self.ui.log(
                "✓", "map", self._short(url), style="magenta",
            )

        # Parse + write off the event loop
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._parse_map, data)
        if self.ui:
            self.ui.advance(self.ui.task_maps)

    def _parse_map(self, data, depth=0):
        if depth > 5:
            return
        if 'sections' in data and isinstance(data['sections'], list):
            for section in data['sections']:
                if isinstance(section, dict) and 'map' in section:
                    self._parse_map(section['map'], depth + 1)
            return

        sources = data.get('sources') or []
        contents = data.get('sourcesContent') or []
        root = data.get('sourceRoot') or ''

        for i, src in enumerate(sources):
            if i >= len(contents):
                continue
            content = contents[i]
            if content is None or not isinstance(src, str):
                continue

            full = ((root + src) if root and
                    not src.startswith(('http', '/', 'webpack'))
                    else src)
            rel = self.normalize_path(full)
            if not rel:
                continue

            out = self.output_dir / rel
            try:
                out.parent.mkdir(parents=True, exist_ok=True)
                if not out.exists():
                    out.write_text(content, encoding='utf-8')
                    self.stats.files_written += 1
                    self.stats.bytes_written += len(content)
                    if self.ui:
                        self.ui.progress.update(
                            self.ui.task_files,
                            completed=self.stats.files_written,
                        )
            except OSError as e:
                self.stats.errors.append(f"write {out}: {e}")

        self.stats.maps_parsed += 1

    def normalize_path(self, src: str) -> Optional[str]:
        # Order matters: longer prefixes first so file:// matches before file:
        for prefix in ('webpack:///', 'webpack://',
                       'vite://', 'rollup://',
                       'file:///', 'file://', 'file:'):
            if src.startswith(prefix):
                src = src[len(prefix):]
                break

        try:
            src = unquote(src)
        except Exception:
            pass

        src = src.split('?')[0].split('#')[0]

        while True:
            if src.startswith('./'):
                src = src[2:]
            elif src.startswith('../'):
                src = src[3:]
            elif src.startswith('/'):
                src = src[1:]
            else:
                break

        parts = []
        for part in src.split('/'):
            if part in ('', '.', '..'):
                continue
            part = re.sub(r'[<>:"|?*\x00-\x1f]', '_', part)
            if len(part) > 200:
                part = part[:200]
            parts.append(part)

        if not parts:
            return None
        return '/'.join(parts)

    async def run(self, seeds):
        connector = aiohttp.TCPConnector(limit=20, ssl=True)
        headers = {
            'User-Agent': (
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
            ),
            'Accept': '*/*',
        }
        async with aiohttp.ClientSession(connector=connector,
                                         headers=headers) as session:
            await asyncio.gather(*(self.process_html(session, u)
                                   for u in seeds))

            processed = set()
            for round_ in range(1, 11):
                pending = self.seen_js - processed
                if not pending:
                    break
                if self.verbose and self.ui:
                    self.ui.log(
                        "•", "round",
                        f"#{round_}: {len(pending)} new JS files",
                        style="yellow",
                    )
                await asyncio.gather(*(self.process_js(session, u)
                                       for u in pending))
                processed |= pending

        self.executor.shutdown(wait=True)


BANNER = r"""
██╗   ██╗███╗   ██╗███╗   ███╗ █████╗ ██████╗ ██████╗ ███████╗██████╗
██║   ██║████╗  ██║████╗ ████║██╔══██╗██╔══██╗██╔══██╗██╔════╝██╔══██╗
██║   ██║██╔██╗ ██║██╔████╔██║███████║██████╔╝██████╔╝█████╗  ██████╔╝
██║   ██║██║╚██╗██║██║╚██╔╝██║██╔══██║██╔═══╝ ██╔═══╝ ██╔══╝  ██╔══██╗
╚██████╔╝██║ ╚████║██║ ╚═╝ ██║██║  ██║██║     ██║     ███████╗██║  ██║
 ╚═════╝ ╚═╝  ╚═══╝╚═╝     ╚═╝╚═╝  ╚═╝╚═╝     ╚═╝     ╚══════╝╚═╝  ╚═╝"""


def render_banner(console):
    console.print(f"[bold cyan]{BANNER}[/]")
    console.print(
        "[dim] Sourcemap crawler and unpacker  ·  "
        "Mohamed Benchikh ([cyan]@weirdmachine64[/])[/]\n"
    )


def render_header(console, seeds, output, concurrency):
    body = Text()
    body.append("Targets:  ", style="bold")
    body.append(", ".join(seeds[:3]) +
                (f"  (+{len(seeds)-3} more)" if len(seeds) > 3 else ""))
    body.append("\nOutput:   ", style="bold")
    body.append(str(output))
    body.append("\nWorkers:  ", style="bold")
    body.append(str(concurrency))
    console.print(Panel(
        body, title="[bold cyan]Sourcemap crawler and unpacker[/]",
        border_style="cyan", padding=(1, 2),
    ))


def render_summary(console, stats: Stats, output: Path, elapsed: float):
    t = Table(title="Summary", title_style="bold cyan",
              border_style="cyan", show_header=False, expand=False,
              padding=(0, 2))
    t.add_column("Metric", style="dim", no_wrap=True)
    t.add_column("Value", justify="right")

    attempted = stats.js_fetched + stats.js_404
    hit_rate = (stats.js_fetched / attempted * 100) if attempted else 0.0
    hit_style = ("green" if hit_rate >= 80
                 else "yellow" if hit_rate >= 50 else "red")

    rows = [
        ("Pages crawled",        f"{stats.pages:,}"),
        ("JS files discovered",  f"{stats.js_found:,}"),
        ("JS files fetched",     f"{stats.js_fetched:,}"),
        ("JS 404s",
         f"[{'red' if stats.js_404 else 'dim'}]{stats.js_404:,}[/]"),
        ("Map probe 404s",       f"[dim]{stats.map_404:,}[/]"),
        ("Fetch hit rate",       f"[{hit_style}]{hit_rate:.1f}%[/]"),
        ("Sourcemaps found",     f"{stats.maps_found:,}"),
        ("Sourcemaps parsed",    f"{stats.maps_parsed:,}"),
        ("Source files written", f"[green]{stats.files_written:,}[/]"),
        ("Bytes written",        f"{stats.bytes_written/1_048_576:,.1f} MiB"),
        ("Errors",
         f"[{'red' if stats.errors else 'dim'}]{len(stats.errors):,}[/]"),
        ("Elapsed",              f"{elapsed:.1f}s"),
    ]
    for label, val in rows:
        t.add_row(label, val)

    console.print()
    console.print(t)
    console.print(f"\n[dim]→ Output: {output}[/]")

    if stats.errors:
        console.print(f"\n[red]First {min(5, len(stats.errors))} errors:[/]")
        for e in stats.errors[:5]:
            console.print(f"  [dim]·[/] {e}")


def normalize_url(u: str) -> str:
    u = u.strip()
    if not re.match(r'^https?://', u, re.IGNORECASE):
        u = f'https://{u}/'
    return u


def main():
    console = Console(stderr=True)
    render_banner(console)

    p = argparse.ArgumentParser(
        description="Sourcemap crawler and unpacker",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('url', nargs='*', metavar='URL',
                   help='One or more target URLs (bare hostnames get '
                        'https:// added). If omitted, every non-empty line '
                        'of stdin is used (e.g. '
                        '`subfinder -d example.com | unmapper`). Targets '
                        'are processed sequentially.')
    p.add_argument('-o', '--output', default='./out',
                   help='Output directory')
    p.add_argument('-t', '--threads', type=int, default=10,
                   help='Worker count (in-flight HTTP requests + '
                        'map-parsing/file-write threads)')
    p.add_argument('--timeout', type=int, default=30,
                   help='Per-request timeout in seconds')
    p.add_argument('-v', '--verbose', action='store_true',
                   help='Stream activity log')
    p.add_argument('--no-guess', action='store_true',
                   help="Don't probe .js.map paths when no sourceMappingURL "
                        "comment exists. Probing is on by default.")
    args = p.parse_args()

    base_output = Path(args.output).resolve()
    base_output.mkdir(parents=True, exist_ok=True)

    # Collect seeds from CLI + stdin (in order, deduped).
    raw_seeds = list(args.url)
    if not sys.stdin.isatty():
        for line in sys.stdin:
            line = line.strip()
            if line and not line.startswith('#'):
                raw_seeds.append(line)

    seeds = []
    seen = set()
    for r in raw_seeds:
        s = normalize_url(r)
        if s not in seen:
            seen.add(s)
            seeds.append(s)

    if not seeds:
        p.error("no URL provided (pass as argument or pipe to stdin)")

    total = len(seeds)

    for i, seed in enumerate(seeds, 1):
        # Per-target output: ./out/<hostname>/
        hostname = urlparse(seed).hostname or 'unknown'
        target_output = base_output / hostname
        target_output.mkdir(parents=True, exist_ok=True)

        if total > 1:
            console.rule(f"[bold cyan]Target {i}/{total}[/]  {seed}",
                         style="cyan")

        render_header(console, [seed], target_output, args.threads)

        # Live UI only when stderr is an interactive terminal; skip it in
        # CI, pipes, redirects, etc. so progress frames don't pollute logs.
        ui = UI(console) if console.is_terminal else None
        extractor = SourceMapExtractor(
            output_dir=target_output,
            threads=args.threads,
            timeout=args.timeout,
            verbose=args.verbose,
            guess_maps=not args.no_guess,
            ui=ui,
        )

        start = time.monotonic()
        try:
            if ui:
                ui.start()
            asyncio.run(extractor.run([seed]))
        except KeyboardInterrupt:
            if ui:
                ui.stop()
            console.print("\n[yellow]Interrupted by user[/]")
            break
        finally:
            if ui:
                ui.stop()

        elapsed = time.monotonic() - start
        render_summary(console, extractor.stats, target_output, elapsed)


if __name__ == '__main__':
    main()
