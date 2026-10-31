#!/usr/bin/env python3
import os
import sys
import json
import hashlib
import argparse
import subprocess
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from urllib.parse import quote

# --- CONVERSION HELPERS ---
NANO_FACTOR = 1_000_000_000
EXTRA_CREDIT_USD_RATE = 0.01


def create_credits_to_usd(rate: float) -> float:
    def _credits_to_usd(raw_amount: int) -> float:
        """Converts API nano-credits to USD."""
        return (raw_amount) * rate

    return _credits_to_usd


# --- CACHED API CLIENT ---
class CachedGitHubClient:
    def __init__(self, cache_dir: str = None):
        self.cache_dir = cache_dir
        if self.cache_dir:
            os.makedirs(self.cache_dir, exist_ok=True)

    def _get_cache_path(self, endpoint: str) -> str:
        # Hash the endpoint string to eliminate special character path issues
        hashed_key = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, f"{hashed_key}.json")

    def call_api(self, endpoint: str) -> dict:
        if self.cache_dir:
            cache_file = self._get_cache_path(endpoint)
            if os.path.exists(cache_file):
                with open(cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)

        # Call GitHub CLI
        cmd = ["gh", "api", endpoint]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"gh api error on {endpoint}: {res.stderr.strip()}")

        data = json.loads(res.stdout)

        if self.cache_dir:
            cache_file = self._get_cache_path(endpoint)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

        return data

    def resolve_repo_name(self, repo_id: int) -> str:
        try:
            data = self.call_api(f"repositories/{repo_id}")
            # return data.get("full_name", f"UnknownRepo_{repo_id}")
            return data["full_name"]
        except Exception as e:
            # return f"UnknownRepo_{repo_id}"
            raise Exception("Cannot resolve repo name for {repo_id}") from e


# --- MAIN HARVESTER ---
def parse_iso8601(date_str: str) -> datetime:
    """Parses ISO dates, ensuring timezone awareness (UTC default)."""
    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fetch_copilot_billing_usage(
    client: CachedGitHubClient,
    organization: str,
    start_dt: datetime,
    end_dt: datetime,
) -> dict:
    """Fetch daily Copilot billing costs, retaining repository attribution."""
    start_utc = start_dt.astimezone(timezone.utc)
    end_utc = end_dt.astimezone(timezone.utc)
    if end_utc <= start_utc:
        return {}

    start_day = start_utc.date()
    end_day = (end_utc - timedelta(microseconds=1)).date()
    month = start_day.replace(day=1)
    last_month = end_day.replace(day=1)
    costs = defaultdict(float)

    while month <= last_month:
        report = client.call_api(
            f"/organizations/{quote(organization, safe='')}/settings/billing/usage"
            f"?year={month.year}&month={month.month}"
        )
        for item in report.get("usageItems", []):
            product_and_sku = f"{item.get('product', '')} {item.get('sku', '')}"
            if "copilot" not in product_and_sku.casefold():
                continue

            usage_day = datetime.fromisoformat(item["date"][:10]).date()
            if not start_day <= usage_day <= end_day:
                continue

            repository = item.get("repositoryName") or None
            costs[repository] += float(item.get("netAmount") or 0)

        if month.month == 12:
            month = month.replace(year=month.year + 1, month=1)
        else:
            month = month.replace(month=month.month + 1)

    return dict(costs)


def add_session_usage(repo_stats, repo_name, usage, belongs_to_window):
    if usage.get("type") == "ai_credits":
        credits = usage["amount"] / NANO_FACTOR
        repo_stats[repo_name]["unfiltered_credits"] += credits
        if belongs_to_window:
            repo_stats[repo_name]["filtered_credits"] += credits
    elif usage.get("type") == "premium_requests":
        if belongs_to_window:
            repo_stats[repo_name]["premium_requests"] += usage["amount"]
    else:
        raise Exception("Unknown usage type: " + str(usage.get("type")))


TASKS_PER_PAGE = 1000


def main():
    parser = argparse.ArgumentParser(
        description="Harvest Copilot usage data, resolve repositories, and group costs by customer."
    )
    parser.add_argument(
        "--copilot-bill", type=float, required=False, help="Total bill for Copilot"
    )
    parser.add_argument(
        "--copilot-credits",
        type=float,
        required=False,
        help="Total credits used in Copilot",
    )
    parser.add_argument(
        "--start",
        type=str,
        required=True,
        help="Start date (ISO format, e.g. 2026-08-01T00:00:00Z)",
    )
    parser.add_argument(
        "--end",
        type=str,
        required=True,
        help="End date (ISO format, e.g. 2026-10-01T23:59:59Z)",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help="Directory path to enable persistent API disk cache",
    )
    parser.add_argument(
        "--customer-map",
        type=str,
        default=None,
        help="Path to JSON file mapping 'owner/repo' -> 'Customer Name'",
    )
    parser.add_argument(
        "--organization",
        type=str,
        default=None,
        help="Organization to query for daily Copilot billing usage",
    )

    args = parser.parse_args()

    match args.copilot_bill, args.copilot_credits:
        case None, None:
            actual_cost_per_credit = 0
            total_credits = None
        case None, credits:
            actual_cost_per_credit = 0
            total_credits = credits
        case bill, None:
            raise Exception("--copilot-bill without --copilot-credits is useless")
        case bill, credits:
            actual_cost_per_credit = bill / credits
            total_credits = credits

    billed_cost_per_credit = min(actual_cost_per_credit, EXTRA_CREDIT_USD_RATE)

    print(f"actual_cost_per_credit: {actual_cost_per_credit}")
    print(f"billed_cost_per_credit: {billed_cost_per_credit}")

    start_dt = parse_iso8601(args.start)
    end_dt = parse_iso8601(args.end)

    # Load customer map if provided
    customer_map = {}
    if args.customer_map:
        with open(args.customer_map, "r", encoding="utf-8") as f:
            customer_map = json.load(f)

    client = CachedGitHubClient(cache_dir=args.cache_dir)

    print(
        f"--> Harvesting Copilot Agent tasks from {start_dt} (incl.) to {end_dt} (excl.)..."
    )
    # Fetch task list (using pagination per_page=100)
    try:
        raw_tasks = client.call_api(f"/agents/tasks?per_page={TASKS_PER_PAGE}")
        tasks = (
            raw_tasks.get("tasks", raw_tasks)
            if isinstance(raw_tasks, dict)
            else raw_tasks
        )
        if len(tasks) >= TASKS_PER_PAGE:
            # TODO: fix pagination
            raise Exception(
                f"Fetching more that {TASKS_PER_PAGE} tasks not yet supported"
            )
    except Exception as e:
        print(f"Fatal error fetching tasks: {e}")
        sys.exit(1)

    repo_stats = defaultdict(
        lambda: {
            "unfiltered_credits": 0.0,
            "filtered_credits": 0.0,
            "premium_requests": 0.0,
            "task_count": 0,
        }
    )

    print(f"--> Found {len(tasks)} tasks. Processing session usage metrics...")

    for task_summary in tasks:
        task_id = task_summary.get("id")
        if not task_id:
            raise Exception(f"No task id for {task_summary}")

        task_detail = client.call_api(f"/agents/tasks/{task_id}")

        repo_id = task_detail.get("repository", {}).get("id")
        if not repo_id:
            raise Exception(f"No repo id for task {task_detail}")

        repo_name = client.resolve_repo_name(repo_id)
        # sessions = task_detail.get("sessions", [])
        sessions = task_detail["sessions"]

        repo_stats[repo_name]["task_count"] += 1

        for session in sessions:
            # Check if session completion falls into the target window
            if not "completed_at" in session:
                if session["state"] == "queued":
                    continue
                else:
                    raise Exception("Session without completed_at: " + str(session))
            completed_str = session["completed_at"]
            completed_dt = parse_iso8601(completed_str)
            belongs_to_window = start_dt <= completed_dt < end_dt

            # usage = session.get("usage", {})
            if not "usage" in session:
                if "error" in session:
                    if belongs_to_window:
                        # just verbose: print("session error", session["error"])
                        continue
                    else:
                        continue
                else:
                    if belongs_to_window:
                        print(
                            "WARN: session without usage and error: "
                            + str(session["task_id"])
                        )
                        continue
                    else:
                        continue
            usage = session["usage"]

            add_session_usage(repo_stats, repo_name, usage, belongs_to_window)

    billing_costs = {}
    if args.organization:
        print(f"--> Fetching Copilot billing usage for {args.organization}...")
        billing_costs = fetch_copilot_billing_usage(
            client, args.organization, start_dt, end_dt
        )

    # Group results by customer
    customer_groups = defaultdict(
        lambda: {
            "credits": 0.0,
            "premium_requests": 0.0,
            "billing_cost": 0.0,
            "repos": [],
        }
    )
    unassigned_repos = []

    repos = list(repo_stats)
    repos.extend(repo for repo in billing_costs if repo not in repo_stats)
    for repo in repos:
        data = repo_stats.get(repo, {})
        customer = customer_map.get(repo) if repo is not None else None
        f_cred = data.get("filtered_credits", 0.0)
        premium_requests = data.get("premium_requests", 0.0)
        billing_cost = billing_costs.get(repo, 0.0)

        if customer:
            customer_groups[customer]["credits"] += f_cred
            customer_groups[customer]["premium_requests"] += premium_requests
            customer_groups[customer]["billing_cost"] += billing_cost
            customer_groups[customer]["repos"].append(
                (repo, f_cred, premium_requests, billing_cost)
            )
        else:
            unassigned_repos.append((repo, f_cred, premium_requests, billing_cost))

    # --- PRINT SUMMARY REPORT ---
    print("\n" + "=" * 65)
    print(f" COPILOT USAGE REPORT ({args.start} (incl.) to {args.end} (excl.)")
    print("=" * 65)

    print(f"Billed cost per credit: ${billed_cost_per_credit}")

    credits_to_usd = create_credits_to_usd(billed_cost_per_credit)

    assigned_credits = 0.0

    for customer, c_data in customer_groups.items():
        if (
            c_data["credits"] == 0
            and c_data["premium_requests"] == 0
            and c_data["billing_cost"] == 0
        ):
            continue
        print(f"\nCustomer: {customer}")
        print(f"  Total Credits: {c_data['credits']:.2f}")
        if c_data["premium_requests"]:
            print(f"  Premium Requests: {c_data['premium_requests']:.2f}")
        print(f"  Total Cost: ${credits_to_usd(c_data['credits']):.2f}")
        if c_data["billing_cost"]:
            print(
                "  Organization Copilot Billing Usage "
                f"(separate source): ${c_data['billing_cost']:.2f}"
            )
        print("  Breakdown by Repo:")
        for r_name, r_cred, premium_requests, billing_cost in c_data["repos"]:
            display_name = r_name or "Unattributed Copilot usage"
            if r_cred == 0 and premium_requests == 0 and billing_cost == 0:
                continue
            if r_cred:
                r_usd = credits_to_usd(r_cred)
                print(
                    f"    - {display_name:35} : "
                    f"${r_usd:6.2f} ({r_cred:7.2f} credits)"
                )
            if premium_requests:
                print(
                    f"    - {display_name:35} : "
                    f"{premium_requests:7.2f} premium requests"
                )
            if billing_cost:
                print(
                    f"    - {display_name:35} : "
                    f"${billing_cost:6.2f} (organization billing usage)"
                )
            assigned_credits += r_cred

    if unassigned_repos:
        print("\nUnassigned Repositories (No Customer Mapped):")
        for r_name, r_cred, premium_requests, billing_cost in unassigned_repos:
            display_name = r_name or "Unattributed Copilot usage"
            if r_cred:
                r_usd = credits_to_usd(r_cred)
                print(
                    f"  - {display_name:37} : "
                    f"${r_usd:6.2f} ({r_cred:7.2f} credits)"
                )
            if premium_requests:
                print(
                    f"  - {display_name:37} : "
                    f"{premium_requests:7.2f} premium requests"
                )
            if billing_cost:
                print(
                    f"  - {display_name:37} : "
                    f"${billing_cost:6.2f} (organization billing usage)"
                )
            assigned_credits += r_cred

    unassigned_credits = total_credits - assigned_credits
    print()
    print(
        f"Unassigned credits: {unassigned_credits} (${credits_to_usd(unassigned_credits)})"
    )


if __name__ == "__main__":
    main()
