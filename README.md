<h1 align="center">UnMapper</h1>

<p align="center">
  Sourcemap crawler and unpacker. Crawls a target, finds its JavaScript, and reconstructs the original source tree from any sourcemaps it ships.
</p>

<p align="center">
  <img src="https://github.com/user-attachments/assets/2df9c7e0-5404-49eb-98b0-0ae02702d312" alt="UnMapper demo" width="900" />
</p>

## Features

- Discovers JS bundles from `<script>` tags during a page crawl
- Recovers webpack chunks from manifests + `.u` functions, including the **if-cascade** shape used by GitHub (where each chunk is enumerated explicitly rather than via a formula)
- Handles both inline (`data:application/json;base64,...`) and external sourcemaps
- Probes `.js.map` paths when no `sourceMappingURL` comment is present (toggle with `--no-guess`)
- Async with configurable concurrency
- Reads targets from stdin; composes with `subfinder`, `httpx`, etc.
- Live progress UI; per-host output directories

## Installation

UnMapper requires Python 3.10+.

```sh
uv tool install git+https://github.com/weirdmachine64/UnMapper
```

From a local checkout (editable):

```sh
git clone https://github.com/weirdmachine64/UnMapper
cd UnMapper
uv tool install --editable .
```

## Usage

```console
$ unmapper --help

usage: unmapper [-h] [-o OUTPUT] [-t THREADS] [--timeout TIMEOUT] [-v] [--no-guess] [URL ...]

Sourcemap crawler and unpacker

positional arguments:
  URL                   One or more target URLs (bare hostnames get https:// added). If
                        omitted, every non-empty line of stdin is used
                        (e.g. `subfinder -d example.com | unmapper`). Targets are
                        processed sequentially.

options:
  -h, --help            show this help message and exit
  -o OUTPUT, --output OUTPUT
                        Output directory (default: ./out)
  -t THREADS, --threads THREADS
                        Worker count: in-flight HTTP requests + map-parsing/file-write
                        threads (default: 10)
  --timeout TIMEOUT     Per-request timeout in seconds (default: 30)
  -v, --verbose         Stream activity log
  --no-guess            Don't probe .js.map paths when no sourceMappingURL comment
                        exists. Probing is on by default.
```

## Running UnMapper

Single target:

```sh
unmapper https://github.com
```

Multiple targets on the command line:

```sh
unmapper https://a.example.com https://b.example.com -o ./out
```

Piped from a subdomain enumerator:

```sh
subfinder -d example.com -silent | unmapper -o ./harvest -t 20
```

Skip the `.js.map` probe and only follow explicit `sourceMappingURL=` comments:

```sh
unmapper https://example.com --no-guess
```

## Output

Each target lands under `<output>/<hostname>/`, mirroring the original source tree as encoded in the sourcemap:

```
out/
└── github.com/
    ├── src/
    ├── node_modules/
    └── webpack/
```

## License

MIT. See [LICENSE](LICENSE).
