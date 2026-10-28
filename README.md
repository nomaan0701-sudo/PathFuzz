# PathFuzz

PathFuzz is a Python command-line tool for authorized path traversal testing.

## Features

- Test URL parameters using configurable test cases.
- Restrict scans to explicitly allowed origins.
- Apply request limits and timeouts.
- Generate reports for suspicious results.

## Requirements

- Python 3.10 or newer
- Dependencies listed in `requirements.txt`

## Setup

```bash
python -m pip install -r requirements.txt
```

## Usage

Review the command-line options first:

```bash
python pathfuzz.py --help
```

Use PathFuzz only against systems you own or have explicit permission to test. Start with a local test server.

## Status

Work in progress. Detection results require manual verification; suspicious results are not proof of a vulnerability.