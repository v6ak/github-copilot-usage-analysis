#!/usr/bin/env python3
import os
import sys
import json
import hashlib
import argparse
import subprocess
from datetime import datetime, timezone
from collections import defaultdict

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


TASKS_PER_PAGE = 1000


def main():
    parser = argparse.ArgumentParser(
        description="Harvest Copilot Agent usage data, resolve repositories, and group costs by customer."
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
        lambda: {"unfiltered_credits": 0.0, "filtered_credits": 0.0, "task_count": 0}
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

            if usage.get("type") == "ai_credits":
                # raw_amount = usage.get("amount", 0)
                raw_amount = usage["amount"]
                credits_val = raw_amount / NANO_FACTOR

                # Accumulate unfiltered total
                repo_stats[repo_name]["unfiltered_credits"] += credits_val

                if belongs_to_window:
                    repo_stats[repo_name]["filtered_credits"] += credits_val
            elif usage.get("type") == "premium_requests":
                if belongs_to_window:
                    raise Exception("Premium requests aren't supported" + str(session))
            else:
                raise Exception("Unknown usage type: " + str(usage.get("type")))

    # Group results by customer
    customer_groups = defaultdict(lambda: {"credits": 0.0, "repos": []})
    unassigned_repos = []

    for repo, data in repo_stats.items():
        customer = customer_map.get(repo)
        # f_usd = data["filtered_usd"]
        f_cred = data["filtered_credits"]

        if customer:
            # customer_groups[customer]["cost_usd"] += f_usd
            customer_groups[customer]["credits"] += f_cred
            customer_groups[customer]["repos"].append((repo, f_cred))
        else:
            unassigned_repos.append((repo, f_cred))

    # --- PRINT SUMMARY REPORT ---
    print("\n" + "=" * 65)
    print(f" COPILOT AGENT COST REPORT ({args.start} (incl.) to {args.end} (excl.)")
    print("=" * 65)

    print(f"Billed cost per credit: ${billed_cost_per_credit}")

    credits_to_usd = create_credits_to_usd(billed_cost_per_credit)

    assigned_credits = 0.0

    for customer, c_data in customer_groups.items():
        if c_data["credits"] == 0:
            continue
        print(f"\nCustomer: {customer}")
        print(f"  Total Credits: {c_data['credits']:.2f}")
        print(f"  Total Cost: ${credits_to_usd(c_data['credits']):.2f}")
        print("  Breakdown by Repo:")
        for r_name, r_cred in c_data["repos"]:
            if r_cred == 0:
                continue
            r_usd = credits_to_usd(r_cred)
            print(f"    - {r_name:35} : ${r_usd:6.2f} ({r_cred:7.2f} credits)")
            assigned_credits += r_cred

    if unassigned_repos:
        print("\nUnassigned Repositories (No Customer Mapped):")
        for r_name, r_cred in unassigned_repos:
            r_usd = credits_to_usd(r_cred)
            print(f"  - {r_name:37} : ${r_usd:6.2f} ({r_cred:7.2f} credits)")
            assigned_credits += r_cred

    unassigned_credits = total_credits - assigned_credits
    print()
    print(
        f"Unassigned credits: {unassigned_credits} (${credits_to_usd(unassigned_credits)})"
    )


if __name__ == "__main__":
    main()
