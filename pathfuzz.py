import json
import hashlib
import requests
import argparse
from pathlib import Path
from urllib.parse import (
    urlsplit,
    urlunsplit,
    parse_qsl,
    quote,
    unquote_plus,
)
from concurrent.futures import (
    Future,
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


def inspect_parameter_instances(url):
    """Return each query parameter occurrence as (name, zero_based_index, total)."""
    parsed = urlsplit(url)
    parameters = parse_qsl(parsed.query, keep_blank_values=True)

    totals = {}
    for name, _value in parameters:
        totals[name] = totals.get(name, 0) + 1

    seen = {}
    instances = []
    for name, _value in parameters:
        occurrence_index = seen.get(name, 0)
        seen[name] = occurrence_index + 1
        instances.append((name, occurrence_index, totals[name]))

    return instances


def inspect_url(url):
    """Extract unique query parameter names without sending requests."""
    return sorted({name for name, _index, _total in inspect_parameter_instances(url)})


def replace_parameter(url, target_parameter, test_value, occurrence_index=0):
    """Replace one query-value occurrence while preserving intentional %XX encoding.

    Payload values are treated as URL-query values: existing percent escapes in
    the payload are preserved, while characters that could change query structure
    (such as &, =, #, and +) are encoded. This prevents accidental double-encoding
    of payloads like %2e%2e%2f while keeping the query string structurally valid.
    """
    if occurrence_index < 0:
        raise ValueError("occurrence_index cannot be negative")

    parsed = urlsplit(url)
    raw_parts = parsed.query.split("&") if parsed.query else []
    updated_parts = []
    seen_target = 0
    found = False

    for part in raw_parts:
        raw_name, separator, raw_value = part.partition("=")
        decoded_name = unquote_plus(raw_name)

        if decoded_name == target_parameter:
            if seen_target == occurrence_index:
                # Preserve payload-provided %XX sequences and path separators.
                # Encode query delimiters so a payload cannot accidentally add
                # new query parameters or a fragment.
                encoded_value = quote(test_value, safe="%/\\")
                updated_parts.append(raw_name + (separator or "=") + encoded_value)
                found = True
            else:
                updated_parts.append(part)
            seen_target += 1
        else:
            updated_parts.append(part)

    if not found:
        raise ValueError(
            f"Parameter '{target_parameter}' occurrence {occurrence_index + 1} was not found"
        )

    updated_query = "&".join(updated_parts)
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


def generate_test_cases(urls, test_values, max_payloads_per_parameter=None):
    """Generate URL variations, optionally limiting values per parameter."""
    if max_payloads_per_parameter is not None and max_payloads_per_parameter < 1:
        raise ValueError("max_payloads_per_parameter must be at least 1")

    selected_values = (
        test_values[:max_payloads_per_parameter]
        if max_payloads_per_parameter is not None
        else test_values
    )
    cases = []

    for url in urls:
        for parameter, occurrence_index, occurrence_total in inspect_parameter_instances(url):
            for test_value in selected_values:
                try:
                    test_url = replace_parameter(
                        url, parameter, test_value, occurrence_index=occurrence_index
                    )
                except ValueError:
                    continue

                cases.append({
                    "original_url": url,
                    "parameter": parameter,
                    "parameter_occurrence": occurrence_index + 1,
                    "parameter_occurrence_total": occurrence_total,
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
def iter_test_cases(urls, test_values, max_payloads_per_parameter=None):
    """Generate URL variations lazily with an optional per-parameter cap."""
    if max_payloads_per_parameter is not None and max_payloads_per_parameter < 1:
        raise ValueError("max_payloads_per_parameter must be at least 1")

    selected_values = (
        test_values[:max_payloads_per_parameter]
        if max_payloads_per_parameter is not None
        else test_values
    )

    for url in urls:
        for parameter, occurrence_index, occurrence_total in inspect_parameter_instances(url):
            for test_value in selected_values:
                try:
                    test_url = replace_parameter(
                        url, parameter, test_value, occurrence_index=occurrence_index
                    )
                except ValueError:
                    continue

                yield {
                    "original_url": url,
                    "parameter": parameter,
                    "parameter_occurrence": occurrence_index + 1,
                    "parameter_occurrence_total": occurrence_total,
                    "test_value": test_value,
                    "test_url": test_url,
                }


def estimate_scan_plan(urls, test_values, max_payloads_per_parameter, max_requests):
    """Estimate candidate coverage and HTTP request costs without networking.

    A max_requests value of 0 means the request budget is unlimited.
    """
    effective_payloads = min(len(test_values), max_payloads_per_parameter)
    case_counts_by_url = []
    parameters_discovered = 0

    for url in urls:
        parameter_count = len(inspect_parameter_instances(url))
        parameters_discovered += parameter_count
        case_counts_by_url.append(parameter_count * effective_payloads)

    candidate_cases = sum(case_counts_by_url)
    baseline_urls = sum(1 for count in case_counts_by_url if count > 0)
    full_scan_requests = candidate_cases + baseline_urls

    planned_cases = 0
    urls_started = 0
    urls_fully_covered = 0

    if max_requests == 0:
        # Explicit unlimited mode can schedule all candidate cases.
        planned_cases = candidate_cases
        urls_started = baseline_urls
        urls_fully_covered = baseline_urls
    else:
        remaining_requests = max_requests

        # Cases are generated URL-by-URL. Each URL with cases costs one
        # baseline request plus one request per test case.
        for url_case_count in case_counts_by_url:
            if url_case_count <= 0:
                continue
            if remaining_requests < 2:
                break

            cases_for_url = min(url_case_count, remaining_requests - 1)
            if cases_for_url <= 0:
                break

            urls_started += 1
            planned_cases += cases_for_url
            remaining_requests -= cases_for_url + 1

            if cases_for_url == url_case_count:
                urls_fully_covered += 1
            else:
                break

    return {
        "parameters_discovered": parameters_discovered,
        "candidate_cases": candidate_cases,
        "baseline_urls": baseline_urls,
        "full_scan_requests": full_scan_requests,
        "planned_cases_under_budget": planned_cases,
        "urls_started_under_budget": urls_started,
        "urls_fully_covered_under_budget": urls_fully_covered,
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
    max_requests=1000,
    timeout=5,
    workers=20,
    expected_marker=None,
    requests_per_second=5,
    allowed_origins=None,
):
    """Yield findings with bounded concurrency and one baseline per URL.

    Request accounting reserves one baseline request for each original URL
    that is actually tested, then one request per test case. Reservations are
    made before work is submitted, so workers cannot exceed a finite
    max_requests. Passing 0 explicitly disables the scan-wide budget.
    """
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if max_requests < 0:
        raise ValueError("max_requests cannot be negative")
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0")
    if requests_per_second <= 0:
        raise ValueError("requests_per_second must be greater than 0")

    # Never run without an explicit scope. Direct callers default to the
    # local test server; main() supplies the CLI-validated allowlist.
    if allowed_origins is None:
        allowed_origins = {normalize_origin("http://127.0.0.1:8000")}
    if not allowed_origins:
        raise ValueError("At least one allowed origin is required")

    case_iter = iter(cases)
    limiter = RateLimiter(requests_per_second)
    pending = set()
    submitted = 0
    exhausted = False
    budget_exhausted = False

    reservation_lock = Lock()
    metrics_lock = Lock()
    baseline_futures = {}
    reserved_requests = 0
    requests_attempted = 0
    baseline_requests_attempted = 0

    def reserve_case(original_url):
        """Reserve budget and return (baseline future, owns baseline)."""
        nonlocal reserved_requests
        with reservation_lock:
            baseline_future = baseline_futures.get(original_url)
            if baseline_future is None:
                # A new URL needs one baseline request plus this test request.
                if max_requests > 0 and reserved_requests + 2 > max_requests:
                    return None
                baseline_future = Future()
                baseline_futures[original_url] = baseline_future
                reserved_requests += 2
                return baseline_future, True

            # This URL's baseline is already reserved or completed.
            if max_requests > 0 and reserved_requests + 1 > max_requests:
                return None
            reserved_requests += 1
            return baseline_future, False

    def mark_request_attempted(is_baseline):
        nonlocal requests_attempted, baseline_requests_attempted
        with metrics_lock:
            requests_attempted += 1
            if is_baseline:
                baseline_requests_attempted += 1

    def process_case(case, baseline_future, owns_baseline):
        original_url = case["original_url"]
        test_url = case["test_url"]

        # Validate scope before any request is sent.
        if not is_allowed_url(original_url, allowed_origins):
            raise ValueError("Original URL is outside the allowed origins")
        if not is_allowed_url(test_url, allowed_origins):
            raise ValueError("Generated test URL is outside the allowed origins")

        if owns_baseline:
            limiter.wait()
            mark_request_attempted(is_baseline=True)
            try:
                baseline = fetch_response(
                    original_url,
                    timeout=timeout,
                    expected_marker=expected_marker,
                )
            except Exception as exc:
                # Wake any waiting workers and classify this baseline as failed.
                baseline = {
                    "status_code": None,
                    "content_type": "",
                    "body_bytes_sampled": 0,
                    "body_sha256": None,
                    "body_truncated": False,
                    "expected_marker_found": False,
                    "elapsed_seconds": None,
                    "error": str(exc),
                }
            baseline_future.set_result(baseline)
        else:
            baseline = baseline_future.result()

        limiter.wait()
        mark_request_attempted(is_baseline=False)
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
            nonlocal submitted, exhausted, budget_exhausted
            while len(pending) < workers and not exhausted:
                try:
                    case = next(case_iter)
                except StopIteration:
                    exhausted = True
                    break

                original_url = case["original_url"]
                test_url = case["test_url"]
                if not is_allowed_url(original_url, allowed_origins):
                    raise ValueError("Original URL is outside the allowed origins")
                if not is_allowed_url(test_url, allowed_origins):
                    raise ValueError("Generated test URL is outside the allowed origins")

                reservation = reserve_case(original_url)
                if reservation is None:
                    exhausted = True
                    budget_exhausted = True
                    break

                baseline_future, owns_baseline = reservation
                pending.add(pool.submit(
                    process_case,
                    case,
                    baseline_future,
                    owns_baseline,
                ))
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

    # Expose counters to main() without changing the yielded finding shape.
    run_scan.last_stats = {
        "requests_attempted": requests_attempted,
        "baseline_requests_attempted": baseline_requests_attempted,
        "cases_submitted": submitted,
        "budget_exhausted": budget_exhausted,
        "reserved_requests": reserved_requests,
    }

def save_report(
    findings,
    filename="output/findings.json",
    include_all=False,
    include_review=False,
):
    """Save high-suspicion findings, optionally adding review-level signals."""
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

            # Preserve high-suspicion-only defaults; review findings are opt-in.
            allowed_statuses = {"high_suspicion"}
            if include_review:
                allowed_statuses.add("needs_review")
            if not include_all and status not in allowed_statuses:
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
        help="Maximum HTTP requests including baselines (default: 1000; use 0 for an explicit unlimited budget)",
    )
    parser.add_argument(
        "--max-payloads-per-parameter",
        type=int,
        default=300,
        help="Maximum test values used per query parameter on each URL (default: 300)",
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
        help="Unique expected canary text from a controlled test file; enables marker-based high-suspicion evidence",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show the scan plan without sending HTTP requests or writing a report",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show lower-confidence signals and scan progress",
    )
    parser.add_argument(
        "--include-review",
        action="store_true",
        help="Save high-suspicion and needs-review results (without no-difference results)",
    )
    parser.add_argument(
        "--all-findings",
        action="store_true",
        help="Save all results instead of only high-suspicion findings",
    )

    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.max_requests < 0:
        parser.error("--max-requests cannot be negative; use 0 for unlimited")
    if args.max_requests == 0 and args.allow_origin is None:
        parser.error(
            "--max-requests 0 requires at least one explicit --allow-origin. "
            "Unlimited mode is opt-in; keep the target scope narrow and authorized."
        )
    if args.max_payloads_per_parameter < 1:
        parser.error("--max-payloads-per-parameter must be at least 1")
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
        parser.error(f"Test-case file does not exist: {test_case_path}")

    # Never overwrite the input files with the report.
    resolved_output = output_path.resolve()
    if resolved_output in {input_path.resolve(), test_case_path.resolve()}:
        parser.error("Output path must not overwrite an input file")

    try:
        urls, rejected = load_urls(input_path)
        test_values = load_test_cases(test_case_path)
    except OSError as exc:
        parser.error(f"Could not read input: {exc}")

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
            f"{len(blocked_urls)} URL(s) are outside the allowed origins. "
            "No scan was started. Check --allow-origin and your input file."
        )

    plan = estimate_scan_plan(
        urls,
        test_values,
        args.max_payloads_per_parameter,
        args.max_requests,
    )
    effective_payloads = min(
        len(test_values), args.max_payloads_per_parameter
    )

    print(
        f"[PathFuzz] Loaded {len(urls)} unique URLs; "
        f"rejected lines: {rejected}"
    )
    print(f"[PathFuzz] Query parameters discovered: {plan['parameters_discovered']}")
    print(
        f"[PathFuzz] Configured test values: {len(test_values)}; "
        f"limit per parameter: {args.max_payloads_per_parameter}; "
        f"values used per parameter: {effective_payloads}"
    )
    print(f"[PathFuzz] Candidate cases after cap: {plan['candidate_cases']}")
    print(
        f"[PathFuzz] Estimated requests for a complete scan: "
        f"{plan['full_scan_requests']} "
        f"({plan['baseline_urls']} baseline + {plan['candidate_cases']} tests)"
    )
    budget_label = (
        "unlimited (explicit opt-in)"
        if args.max_requests == 0
        else str(args.max_requests)
    )
    print(
        f"[PathFuzz] Request budget: {budget_label}; "
        f"estimated cases schedulable: {plan['planned_cases_under_budget']}"
    )

    if args.dry_run:
        print("[DRY RUN] No HTTP requests sent; no report written.")
        if plan["candidate_cases"] > plan["planned_cases_under_budget"]:
            print(
                f"[DRY RUN] The current budget would leave "
                f"{plan['candidate_cases'] - plan['planned_cases_under_budget']} "
                "candidate cases untested."
            )
        return

    if plan["candidate_cases"] == 0:
        print("[PathFuzz] No query parameters found; nothing to test.")
        return

    if args.marker is None:
        print(
            "[Info] No canary marker supplied. Marker-based high-suspicion evidence "
            "is unavailable. Use --include-review or --all-findings to save "
            "response-difference signals in the report."
        )

    stats = {
        "processed": 0,
        "high_suspicion": 0,
        "needs_review": 0,
        "inconclusive": 0,
        "no_obvious_difference": 0,
    }
    started_at = time.monotonic()
    cases = iter_test_cases(
        urls,
        test_values,
        max_payloads_per_parameter=args.max_payloads_per_parameter,
    )
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
            safe_target = f"{parsed.scheme}://{safe_netloc}{parsed.path}"
            parameter = finding.get("parameter", "<unknown>")
            occurrence = finding.get("parameter_occurrence", 1)
            occurrence_total = finding.get("parameter_occurrence_total", 1)
            parameter_label = (
                f"{parameter} (occurrence {occurrence}/{occurrence_total})"
                if occurrence_total > 1
                else parameter
            )

            if status == "high_suspicion":
                print(f"\n[HIGH SUSPICION] {safe_target}")
                print(f"  Parameter: {parameter_label}")
                print(f"  Evidence: {', '.join(reasons)}")
            elif args.verbose and status in {"needs_review", "inconclusive"}:
                print(f"\n[{status.upper()}] {safe_target}")
                print(f"  Parameter: {parameter_label}")
                print(f"  Reasons: {', '.join(reasons)}")

            if args.verbose and stats["processed"] % 500 == 0:
                print(f"[Progress] Processed {stats['processed']} cases")
            yield finding

    saved_count = save_report(
        observed_findings(),
        filename=str(output_path),
        include_all=args.all_findings,
        include_review=args.include_review,
    )
    elapsed = time.monotonic() - started_at
    run_stats = getattr(run_scan, "last_stats", {})
    requests_attempted = run_stats.get("requests_attempted", 0)
    baseline_requests = run_stats.get("baseline_requests_attempted", 0)

    print(f"\n{'=' * 40}")
    print("PathFuzz scan summary")
    print(f"{'=' * 40}")
    print(f"URLs loaded:                    {len(urls)}")
    print(f"Input lines rejected:           {rejected}")
    print(f"Candidate cases after cap:      {plan['candidate_cases']}")
    print(f"Cases processed:                {stats['processed']}")
    print(f"Baseline requests attempted:    {baseline_requests}")
    print(f"Test requests attempted:        {stats['processed']}")
    print(f"HTTP requests attempted:        {requests_attempted}")
    print(f"High suspicion:                 {stats['high_suspicion']}")
    print(f"Needs review:                   {stats['needs_review']}")
    print(f"Inconclusive:                   {stats['inconclusive']}")
    print(f"No obvious difference:          {stats['no_obvious_difference']}")
    print(f"Elapsed time:                   {elapsed:.2f} seconds")
    print(f"Findings saved:                 {saved_count}")
    print(f"Report path:                    {output_path}")

    if args.max_requests > 0 and stats["processed"] < plan["candidate_cases"]:
        remaining = plan["candidate_cases"] - stats["processed"]
        print(
            f"\n[Request budget or scan limit reached] "
            f"{remaining} candidate cases were not processed."
        )
        print(
            "Increase --max-requests only within the target's explicit "
            "authorization and rate limits."
        )

    if args.all_findings:
        print("Report mode: all findings")
    elif args.include_review:
        print("Report mode: high-suspicion and needs-review findings")
    else:
        print("Report mode: high-suspicion findings only")


if __name__ == "__main__":
    main()