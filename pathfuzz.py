import json
import hashlib
import requests
import argparse
from pathlib import Path
from urllib.parse import (
    urlsplit,
    urlunsplit,
    parse_qsl,
    urlencode,
)
from concurrent.futures import (
    ThreadPoolExecutor,
    wait,
    FIRST_COMPLETED,
)
from threading import Lock
import time



def load_urls(filename):
    """Read, validate, and deduplicate HTTP(S) URLs."""
    unique_urls = []
    seen = set()
    rejected = 0

    with open(filename, "r", encoding="utf-8") as file:
        for line in file:
            url = line.strip()

            # Ignore blank lines and comments.
            if not url or url.startswith("#"):
                continue

            try:
                parsed = urlsplit(url)

                # Accept only complete HTTP(S) URLs with a hostname.
                if (
                    parsed.scheme.lower() not in ("http", "https")
                    or not parsed.hostname
                ):
                    rejected += 1
                    continue

                # Keep the original URL; use it for deduplication.
                if url not in seen:
                    seen.add(url)
                    unique_urls.append(url)

            except ValueError:
                rejected += 1

    return unique_urls, rejected


def inspect_url(url):
    """Extract query parameter names without sending requests."""
    parsed = urlsplit(url)
    parameters = parse_qsl(
        parsed.query,
        keep_blank_values=True
    )

    names = sorted({name for name, value in parameters})

    return names


def replace_parameter(url, target_parameter, test_value):
    """Return a URL with one query parameter changed."""
    parsed = urlsplit(url)

    parameters = parse_qsl(
        parsed.query,
        keep_blank_values=True
    )

    updated_parameters = []
    found = False

    for name, value in parameters:
        if name == target_parameter and not found:
            updated_parameters.append((name, test_value))
            found = True
        else:
            updated_parameters.append((name, value))

    if not found:
        raise ValueError(
            f"Parameter '{target_parameter}' was not found"
        )

    updated_query = urlencode(updated_parameters)

    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        updated_query,
        parsed.fragment,
    ))

def load_test_cases(filename):
    """Load unique, non-empty test values from a file."""
    test_cases = []
    seen = set()

    with open(filename, "r", encoding="utf-8") as file:
        for line in file:
            value = line.strip()

            if not value or value.startswith("#"):
                continue

            if value not in seen:
                seen.add(value)
                test_cases.append(value)

    return test_cases


def generate_test_cases(urls, test_values):
    """Generate URL variations for each query parameter."""
    cases = []

    for url in urls:
        parameters = inspect_url(url)

        for parameter in parameters:
            for test_value in test_values:
                try:
                    test_url = replace_parameter(
                        url,
                        parameter,
                        test_value
                    )
                except ValueError:
                    continue

                cases.append({
                    "original_url": url,
                    "parameter": parameter,
                    "test_value": test_value,
                    "test_url": test_url,
                })

    return cases
class RateLimiter:
    """Space out requests across all worker threads."""

    def __init__(self, requests_per_second=5):
        if requests_per_second <= 0:
            raise ValueError(
                "requests_per_second must be greater than 0"
            )

        self.interval = 1.0 / requests_per_second
        self.lock = Lock()
        self.next_request_time = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.next_request_time - now

            if delay > 0:
                time.sleep(delay)

            self.next_request_time = (
                time.monotonic() + self.interval
            )
def iter_test_cases(urls, test_values):
    """Generate test cases one at a time."""

    for url in urls:
        for parameter in inspect_url(url):
            for test_value in test_values:
                try:
                    test_url = replace_parameter(
                        url,
                        parameter,
                        test_value,
                    )
                except ValueError:
                    continue

                yield {
                    "original_url": url,
                    "parameter": parameter,
                    "test_value": test_value,
                    "test_url": test_url,
                }

def fetch_response(
    url,
    timeout=5,
    max_body_bytes=65536,
    expected_marker=None,
):
    """Fetch a response and inspect a bounded body sample."""
    response = None

    if max_body_bytes < 1:
        raise ValueError("max_body_bytes must be at least 1")

    try:
        response = requests.get(
            url,
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )

        body_sample = bytearray()
        marker_bytes = (
            expected_marker.encode("utf-8")
            if expected_marker
            else None
        )
        marker_overlap = b""
        marker_found = False
        body_truncated = False

        for chunk in response.iter_content(chunk_size=8192):
            if not chunk:
                continue

            remaining = max_body_bytes - len(body_sample)

            if remaining <= 0:
                body_truncated = True
                break

            piece = chunk[:remaining]
            body_sample.extend(piece)

            # Search across chunk boundaries for the known marker.
            if marker_bytes:
                candidate = marker_overlap + piece

                if marker_bytes in candidate:
                    marker_found = True

                overlap_size = len(marker_bytes) - 1
                marker_overlap = (
                    candidate[-overlap_size:]
                    if overlap_size > 0
                    else b""
                )

            if len(piece) < len(chunk):
                body_truncated = True
                break

            if len(body_sample) >= max_body_bytes:
                body_truncated = True
                break

        sampled_body = bytes(body_sample)

        return {
            "status_code": response.status_code,
            "content_type": response.headers.get(
                "Content-Type", ""
            ),
            "body_bytes_sampled": len(sampled_body),
            "body_sha256": hashlib.sha256(
                sampled_body
            ).hexdigest(),
            "body_truncated": body_truncated,
            "expected_marker_found": marker_found,
            "elapsed_seconds": response.elapsed.total_seconds(),
            "error": None,
        }

    except requests.RequestException as exc:
        return {
            "status_code": None,
            "content_type": "",
            "body_bytes_sampled": 0,
            "body_sha256": None,
            "body_truncated": False,
            "expected_marker_found": False,
            "elapsed_seconds": None,
            "error": str(exc),
        }

    finally:
        if response is not None:
            response.close()


def compare_responses(baseline, test):
    """Identify measurable differences between responses."""
    differences = []

    if baseline["status_code"] != test["status_code"]:
        differences.append("status_code_changed")

    if baseline["content_type"] != test["content_type"]:
        differences.append("content_type_changed")

    if (
        baseline["body_bytes_sampled"]
        != test["body_bytes_sampled"]
    ):
        differences.append("sample_size_changed")

    if (
        baseline["body_sha256"] is not None
        and test["body_sha256"] is not None
        and baseline["body_sha256"] != test["body_sha256"]
    ):
        differences.append("body_sample_changed")

    return differences


def normalize_origin(url):
    """Return a normalized HTTP(S) origin or reject the URL."""
    parsed = urlsplit(url)

    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError("Only HTTP and HTTPS URLs are allowed")

    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Missing hostname or URL credentials are present")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid URL port") from exc

    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80

    return (
        parsed.scheme.lower(),
        parsed.hostname.lower().rstrip("."),
        port,
    )


def is_allowed_url(url, allowed_origins):
    """Check whether a URL belongs to an explicitly allowed origin."""
    try:
        return normalize_origin(url) in allowed_origins
    except ValueError:
        return False


def analyze_response(baseline, test):
    """Classify response evidence conservatively."""

    differences = compare_responses(baseline, test)

    # Failed requests cannot provide reliable comparisons.
    if baseline.get("error") or test.get("error"):
        return {
            "status": "inconclusive",
            "confidence": "none",
            "reasons": ["request_error"],
        }

    marker_in_baseline = baseline.get(
        "expected_marker_found", False
    )
    marker_in_test = test.get(
        "expected_marker_found", False
    )

    # Require a marker unique to the test response
    # and a successful HTTP status.
    if marker_in_test and not marker_in_baseline:
        status_code = test.get("status_code")

        if (
            isinstance(status_code, int)
            and 200 <= status_code < 300
        ):
            return {
                "status": "high_suspicion",
                "confidence": "high",
                "reasons": [
                    "known_marker_present_only_in_test_response",
                    "test_response_successful",
                ],
            }

        return {
            "status": "needs_review",
            "confidence": "low",
            "reasons": [
                "known_marker_present_only_in_test_response",
                "test_response_not_successful",
            ],
        }

    # Other response changes are clues, not proof.
    if differences:
        return {
            "status": "needs_review",
            "confidence": "low",
            "reasons": differences,
        }

    return {
        "status": "no_obvious_difference",
        "confidence": "none",
        "reasons": [],
    }






def run_scan(
    cases,
    max_requests=50,
    timeout=5,
    workers=20,
    expected_marker=None,
    requests_per_second=5,
    allowed_origins=None,
):
    """Yield findings incrementally with bounded concurrency."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if max_requests < 0:
        raise ValueError("max_requests cannot be negative")
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0")

    # Never run without an explicit scope. Direct callers default to the
    # local test server; main() supplies the CLI-validated allowlist.
    if allowed_origins is None:
        allowed_origins = {normalize_origin("http://127.0.0.1:8000")}
    if not allowed_origins:
        raise ValueError("At least one allowed origin is required")

    # Each case uses two requests: baseline and test.
    max_cases = max_requests // 2
    case_iter = iter(cases)
    limiter = RateLimiter(requests_per_second)
    pending = set()
    submitted = 0
    exhausted = False

    def process_case(case):
        original_url = case["original_url"]
        test_url = case["test_url"]

        # Check scope before either request is sent.
        if not is_allowed_url(original_url, allowed_origins):
            raise ValueError("Original URL is outside the allowed origins")
        if not is_allowed_url(test_url, allowed_origins):
            raise ValueError("Generated test URL is outside the allowed origins")

        limiter.wait()
        baseline = fetch_response(
            original_url,
            timeout=timeout,
            expected_marker=expected_marker,
        )
        limiter.wait()
        test = fetch_response(
            test_url,
            timeout=timeout,
            expected_marker=expected_marker,
        )

        return {
            "original_url": original_url,
            "test_url": test_url,
            "parameter": case["parameter"],
            "test_value": case["test_value"],
            "baseline": baseline,
            "test_response": test,
            "analysis": analyze_response(baseline, test),
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        def fill_pending():
            nonlocal submitted, exhausted
            while (
                len(pending) < workers
                and submitted < max_cases
                and not exhausted
            ):
                try:
                    case = next(case_iter)
                except StopIteration:
                    exhausted = True
                    break
                pending.add(pool.submit(process_case, case))
                submitted += 1

        fill_pending()
        while pending:
            done, unfinished = wait(
                pending,
                return_when=FIRST_COMPLETED,
            )
            pending = set(unfinished)
            completed = [future.result() for future in done]
            fill_pending()
            for finding in completed:
                yield finding

def save_report(
    findings,
    filename="output/findings.json",
    include_all=False,
):
    """Save selected findings to a JSON report."""
    output_path = Path(filename)
    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    saved_count = 0

    # Write findings incrementally to avoid creating
    # another large list in memory.
    with output_path.open("w", encoding="utf-8") as report:
        report.write("[\n")
        first_item = True

        for finding in findings:
            analysis = finding.get("analysis", {})
            status = analysis.get("status")

            # By default, only save high-suspicion findings.
            if not include_all and status != "high_suspicion":
                continue

            if not first_item:
                report.write(",\n")

            json.dump(
                finding,
                report,
                indent=2,
                ensure_ascii=False,
            )

            first_item = False
            saved_count += 1

        report.write("\n]\n")

    return saved_count


def main():
    parser = argparse.ArgumentParser(
        description="PathFuzz - path traversal testing assistant"
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=None,
        help=(
            "Explicitly allowed origin, e.g. "
            "http://127.0.0.1:8000. Can be repeated."
        ),
    )

    parser.add_argument(
        "-l", "--list",
        required=True,
        help="File containing one URL per line",
    )
    parser.add_argument(
        "--test-cases",
        default="input/test-cases.txt",
        help="File containing test values",
    )
    parser.add_argument(
        "-o", "--output",
        default="output/findings.json",
        help="JSON report path (default: output/findings.json)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=20,
        help="Maximum concurrent workers (default: 20)",
    )
    parser.add_argument(
        "--max-requests",
        type=int,
        default=1000,
        help="Maximum HTTP requests (default: 1000; two requests per case)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Request timeout in seconds (default: 5)",
    )
    parser.add_argument(
        "--requests-per-second",
        type=float,
        default=5.0,
        help="Shared request rate limit (default: 5)",
    )
    parser.add_argument(
        "--marker",
        default=None,
        help="Known harmless marker for controlled test environments",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show lower-confidence signals and scan progress",
    )
    parser.add_argument(
        "--all-findings",
        action="store_true",
        help="Save all results instead of only high-suspicion findings",
    )

    args = parser.parse_args()

    # Validate configuration before starting.
    if args.workers < 1:
        parser.error("--workers must be at least 1")

    if args.max_requests < 0:
        parser.error("--max-requests cannot be negative")

    if args.timeout <= 0:
        parser.error("--timeout must be greater than 0")

    if args.requests_per_second <= 0:
        parser.error("--requests-per-second must be greater than 0")

    input_path = Path(args.list)
    test_case_path = Path(args.test_cases)
    output_path = Path(args.output)

    if not input_path.is_file():
        parser.error(f"Input file does not exist: {input_path}")

    if not test_case_path.is_file():
        parser.error(
            f"Test-case file does not exist: {test_case_path}"
        )

    # Never overwrite the input files with the report.
    resolved_output = output_path.resolve()

    if resolved_output in {
        input_path.resolve(),
        test_case_path.resolve(),
    }:
        parser.error("Output path must not overwrite an input file")

    # Load and validate inputs.
    urls, rejected = load_urls(input_path)
    test_values = load_test_cases(test_case_path)

    if not urls:
        parser.error("No valid HTTP(S) URLs were found")

    if not test_values:
        parser.error("No test values were found")
    allowed_origin_strings = (
        args.allow_origin
        if args.allow_origin is not None
        else ["http://127.0.0.1:8000"]
    )

    try:
        allowed_origins = {
            normalize_origin(origin)
            for origin in allowed_origin_strings
        }
    except ValueError as exc:
        parser.error(f"Invalid --allow-origin value: {exc}")

    blocked_urls = [
        url for url in urls
        if not is_allowed_url(url, allowed_origins)
    ]

    if blocked_urls:
        parser.error(
            f"{len(blocked_urls)} URL(s) are outside the "
            "allowed origins. No scan was started. "
            "Check --allow-origin and your input file."
        )

    # Estimate the number of candidate cases without generating
    # and storing every case.
    potential_cases = (
        sum(len(inspect_url(url)) for url in urls)
        * len(test_values)
    )

    stats = {
        "processed": 0,
        "high_suspicion": 0,
        "needs_review": 0,
        "inconclusive": 0,
        "no_obvious_difference": 0,
    }

    print(
        f"[PathFuzz] Loaded {len(urls)} unique URLs; "
        f"rejected lines: {rejected}"
    )
    print(
        f"[PathFuzz] Candidate cases: {potential_cases}; "
        f"workers: {args.workers}; "
        f"request budget: {args.max_requests}"
    )

    if args.marker is None:
        print(
            "[Info] Marker-based high-suspicion detection is disabled. "
            "Use --marker with a known harmless marker in a "
            "controlled test environment."
        )

    started_at = time.monotonic()

    # Generate cases lazily and process results as they arrive.
    cases = iter_test_cases(urls, test_values)

    findings_stream = run_scan(
        cases,
        max_requests=args.max_requests,
        timeout=args.timeout,
        workers=args.workers,
        expected_marker=args.marker,
        requests_per_second=args.requests_per_second,
        allowed_origins=allowed_origins,

    )

    def observed_findings():
        """Print selected results while streaming them to the report."""

        for finding in findings_stream:
            stats["processed"] += 1

            analysis = finding.get("analysis", {})
            status = analysis.get("status", "unknown")
            reasons = analysis.get("reasons", [])

            if status in stats:
                stats[status] += 1

            # Avoid printing query strings, which may contain tokens.
            parsed = urlsplit(finding["original_url"])
            safe_netloc = parsed.netloc.rsplit("@", 1)[-1]
            safe_target = (
                f"{parsed.scheme}://{safe_netloc}{parsed.path}"
            )
            parameter = finding.get("parameter", "<unknown>")

            if status == "high_suspicion":
                print(f"\n[HIGH SUSPICION] {safe_target}")
                print(f"  Parameter: {parameter}")
                print(f"  Evidence: {', '.join(reasons)}")

            elif args.verbose and status in {
                "needs_review",
                "inconclusive",
            }:
                print(f"\n[{status.upper()}] {safe_target}")
                print(f"  Parameter: {parameter}")
                print(f"  Reasons: {', '.join(reasons)}")

            if (
                args.verbose
                and stats["processed"] % 500 == 0
            ):
                print(
                    f"[Progress] Processed "
                    f"{stats['processed']} cases"
                )

            yield finding

    # save_report consumes the stream incrementally.
    # Default: save high-suspicion findings only.
    saved_count = save_report(
        observed_findings(),
        filename=str(output_path),
        include_all=args.all_findings,
    )

    elapsed = time.monotonic() - started_at

    print(f"\n{'=' * 40}")
    print("PathFuzz scan summary")
    print(f"{'=' * 40}")
    print(f"URLs loaded:               {len(urls)}")
    print(f"Input lines rejected:      {rejected}")
    print(f"Candidate cases:           {potential_cases}")
    print(f"Cases processed:           {stats['processed']}")
    print(f"HTTP requests attempted:   {stats['processed'] * 2}")
    print(f"High suspicion:            {stats['high_suspicion']}")
    print(f"Needs review:              {stats['needs_review']}")
    print(f"Inconclusive:              {stats['inconclusive']}")
    print(
        f"No obvious difference:     "
        f"{stats['no_obvious_difference']}"
    )
    print(f"Elapsed time:              {elapsed:.2f} seconds")
    print(f"Findings saved:            {saved_count}")
    print(f"Report path:               {output_path}")

    if stats["processed"] < potential_cases:
        remaining = potential_cases - stats["processed"]
        print(
            f"\n[Request budget reached] "
            f"{remaining} candidate cases were not processed."
        )
        print(
            "Increase --max-requests only when permitted by "
            "the target's authorization and rate limits."
        )

    if args.all_findings:
        print("Report mode: all findings")
    else:
        print("Report mode: high-suspicion findings only")


if __name__ == "__main__":
    main()