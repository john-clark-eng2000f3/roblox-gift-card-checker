import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import httpx
except ImportError as _exc:
    sys.exit(f"missing dependency '{_exc.name}'. run: pip install -r requirements.txt")

ROBLOX_REDEEM_URL = "https://billing.roblox.com/v1/gift-card/redeem"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3

# ANSI codes for terminal colors
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_YELLOW = "\x1b[33m"
_RESET = "\x1b[0m"

def _parse_codes(source: Path) -> list[str]:
    if not source.exists():
        print(f"file not found: {source}", file=sys.stderr)
        sys.exit(1)

    raw = source.read_text(encoding="utf-8", errors="ignore")
    codes = []
    for line in raw.splitlines():
        line = line.strip().upper()
        if not line:
            continue
        for match in re.findall(r"\b[A-Z0-9]{10}\b", line):
            codes.append(match)
    seen = set()
    out = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

def _check_code(client: httpx.Client, code: str, xsrf: str | None) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Origin": "https://www.roblox.com",
        "Referer": "https://www.roblox.com/giftcards",
    }
    if xsrf:
        headers["X-CSRF-TOKEN"] = xsrf

    payload = {"pinCode": code}
    resp = client.post(ROBLOX_REDEEM_URL, json=payload, headers=headers)
    # print(f"status {resp.status_code}: {resp.text[:200]}")  # debug
    resp.raise_for_status()
    return resp.json()

def _extract_status(body: dict, code: str) -> dict:
    errors = body.get("errors", [])
    if errors:
        err = errors[0]
        msg = err.get("message", "").lower()
        if "already been redeemed" in msg or "redeemed" in msg:
            return {"code": code, "status": "redeemed", "balance": None, "detail": msg}
        if "invalid" in msg or "not found" in msg:
            return {"code": code, "status": "invalid", "balance": None, "detail": msg}
        if "expired" in msg:
            return {"code": code, "status": "expired", "balance": None, "detail": msg}
        return {"code": code, "status": "error", "balance": None, "detail": msg}

    data = body.get("data", {})
    balance = data.get("balance")
    currency = data.get("currency", "USD")
    if balance is not None:
        return {"code": code, "status": "active", "balance": f"{balance} {currency}", "detail": None}

    return {"code": code, "status": "unknown", "balance": None, "detail": str(body)[:200]}

def _write_csv(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["code", "status", "balance", "detail"])
        writer.writeheader()
        writer.writerows(rows)

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch-check Roblox gift card balances.",
        usage="python check.py [--token XSRF] -i codes.txt [-o results.csv]",
    )
    parser.add_argument("-i", "--input", type=Path, required=True, help="file containing gift card codes")
    parser.add_argument("-o", "--output", type=Path, default=None, help="optional CSV to write results")
    parser.add_argument("--token", default=os.environ.get("ROBLOX_XSRF"), help="XSRF token (or set ROBLOX_XSRF)")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds between requests")
    parser.add_argument("--json", action="store_true", dest="json_out", help="emit results as JSON to stdout")
    parser.add_argument("--no-color", action="store_true", help="disable colored output")
    args = parser.parse_args()

    codes = _parse_codes(args.input)
    if not codes:
        print("no valid codes found in input file")
        return 0

    print(f"found {len(codes)} code(s) to check")

    results = []
    xsrf = args.token
    use_color = not args.no_color and sys.stdout.isatty()

    with httpx.Client(http2=True, timeout=DEFAULT_TIMEOUT) as client:
        for idx, code in enumerate(codes, 1):
            print(f"[{idx}/{len(codes)}] checking {code}...", end=" ")

            result = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    body = _check_code(client, code, xsrf)
                    result = _extract_status(body, code)
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429:
                        retry_after = exc.response.headers.get("retry-after")
                        if retry_after:
                            try:
                                sleep_for = float(retry_after)
                            except ValueError:
                                sleep_for = 5.0
                        else:
                            sleep_for = min(2 ** (attempt - 1), 30)
                        if attempt < MAX_RETRIES:
                            print(f"429, retrying in {sleep_for:.0f}s...")
                            time.sleep(sleep_for)
                            continue
                        result = {"code": code, "status": "error", "balance": None, "detail": "rate limited"}
                    elif exc.response.status_code == 403:
                        result = {"code": code, "status": "error", "balance": None, "detail": "403 - check XSRF token"}
                    else:
                        result = {"code": code, "status": "error", "balance": None, "detail": f"http {exc.response.status_code}"}
                except httpx.RequestError as exc:
                    result = {"code": code, "status": "error", "balance": None, "detail": str(exc)}
                except Exception as exc:
                    result = {"code": code, "status": "error", "balance": None, "detail": str(exc)}

            if result is None:
                result = {"code": code, "status": "error", "balance": None, "detail": "max retries exceeded"}

            status = result["status"]
            balance = result["balance"]
            if use_color:
                if status == "active":
                    status_str = f"{_GREEN}{status}{_RESET}"
                elif status in ("redeemed", "expired", "invalid"):
                    status_str = f"{_YELLOW}{status}{_RESET}"
                else:
                    status_str = f"{_RED}{status}{_RESET}"
            else:
                status_str = status

            if balance:
                print(f"{status_str} ({balance})")
            else:
                print(status_str)
            results.append(result)

            if idx < len(codes):
                time.sleep(args.delay)

    if args.json_out:
        print(json.dumps(results, indent=2))

    if args.output:
        _write_csv(args.output, results)
        print(f"wrote {len(results)} result(s) to {args.output}")

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        sys.exit(130)
