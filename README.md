# PathFuzz

PathFuzz is a Python command-line helper for **authorized path-traversal testing**. It reads URLs, identifies query-parameter occurrences, substitutes configured test values, compares bounded HTTP response samples, and writes JSON findings.

> **Authorization and safety:** Use PathFuzz only against systems you own or are explicitly authorized to assess. Start with the included localhost test server. Large or unlimited scans can generate substantial traffic.

## Current capabilities

- Reads and deduplicates HTTP(S) URLs from a text file.
- Restricts scanning to explicitly allowed origins. By default, the CLI allows only `http://127.0.0.1:8000`.
- Identifies query-string parameter occurrences, including repeated names such as `?file=a&file=b`.
- Reads unique test values from a text file and lazily generates cases.
- Defaults to at most 300 test values per parameter occurrence, or fewer if the file contains fewer unique values.
- Reuses one baseline response per original URL during a scan.
- Limits concurrency and spaces requests using a shared rate limiter.
- Supports dry-run planning, a configurable request budget, and JSON reporting.
- Treats response changes as review signals, not proof of a vulnerability. Marker-based high-suspicion evidence requires a unique, known canary string in a controlled test environment.

## Requirements

- Python 3.10 or later recommended
- Dependencies listed in `requirements.txt`

## Setup (Windows PowerShell)

From the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell prevents activation, you can run the virtual environment's Python directly instead: `.\.venv\Scripts\python.exe`.

## Test locally first

The included `local_server.py` binds to `127.0.0.1:8000` and returns a fixed text response. **It is a connectivity/workflow fixture, not a deliberately vulnerable application**, so it cannot prove that traversal detection works.

Open Terminal 1:

```powershell
python local_server.py
```

Open Terminal 2 in the project directory:

```powershell
Set-Content input/local-test-urls.txt 'http://127.0.0.1:8000/?file=sample.txt'
python pathfuzz.py --list input/local-test-urls.txt --allow-origin http://127.0.0.1:8000 --max-requests 20 --dry-run
python pathfuzz.py --list input/local-test-urls.txt --allow-origin http://127.0.0.1:8000 --max-requests 20 --all-findings
```

Stop the server with `Ctrl+C`, then remove the temporary URL file:

```powershell
Remove-Item input/local-test-urls.txt
```

The local scan with three test values and one query parameter should use one baseline request plus three test requests. The exact output depends on the test values in your file.

## Input files

### URL list

Pass one URL per line with `--list`. Blank lines and lines beginning with `#` are ignored. The URL origin must match one of the origins supplied with `--allow-origin`.

Keep real target lists, authenticated URLs, tokens, cookies, and other private data out of Git. The project's `.gitignore` excludes `input/urls.txt` by default.

### Test values

By default, PathFuzz reads `input/test-cases.txt`. Each non-empty, non-comment line is one value. Duplicates are removed. Use `--test-cases PATH` to choose a different file.

## Common commands

Show options:

```powershell
python pathfuzz.py --help
```

Preview the plan without sending HTTP requests or writing a report:

```powershell
python pathfuzz.py --list input/local-test-urls.txt --allow-origin http://127.0.0.1:8000 --dry-run
```

Save high-suspicion findings only (the default report mode):

```powershell
python pathfuzz.py --list input/local-test-urls.txt --allow-origin http://127.0.0.1:8000
```

Include review-level response differences:

```powershell
python pathfuzz.py --list input/local-test-urls.txt --allow-origin http://127.0.0.1:8000 --include-review
```

Save all case results:

```powershell
python pathfuzz.py --list input/local-test-urls.txt --allow-origin http://127.0.0.1:8000 --all-findings
```

Choose a test-value file and lower the per-parameter cap:

```powershell
python pathfuzz.py --list input/local-test-urls.txt --allow-origin http://127.0.0.1:8000 --test-cases input/test-cases.txt --max-payloads-per-parameter 25
```

## Request budget and rate

- `--max-requests 1000` is the default maximum number of HTTP requests, including baseline requests.
- `--max-requests 0` explicitly opts into an unlimited request-count budget and requires at least one explicit `--allow-origin`.
- The default global request rate is five requests per second (`--requests-per-second 5`). The rate limit remains active in unlimited-budget mode.
- `--workers 20` controls maximum concurrent workers; concurrency and request rate are separate controls.
- Run `--dry-run` first for large inputs. Candidate count and required requests can be extremely large.

Do not raise the request budget or rate without confirming the target's authorization and permitted testing limits.

## Reporting and detection limits

The report is written to `output/findings.json` by default. `--include-review` saves `high_suspicion` and `needs_review` results; `--all-findings` saves all results. The default saves only `high_suspicion` results.

A known marker is a unique, harmless canary string that is expected to appear only if a controlled test exposes a canary file. Supply it with `--marker` only when you know what that string should be. An ordinary response difference is not proof of path traversal.

The current scanner tests query-string parameters. It does not yet provide full browser crawling, request-body parameter discovery, or universal support for every application framework. Detection quality depends on the target behavior and the quality of the local test fixture.

## Development check

```powershell
python -m py_compile pathfuzz.py
python pathfuzz.py --help
```
