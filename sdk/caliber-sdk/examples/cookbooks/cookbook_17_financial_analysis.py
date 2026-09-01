"""Create, execute, and persist a monthly financial analysis using only the SDK."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import env_client

CSV_FILENAME = "monthly-financials.csv"
RESULT_FILENAME = "monthly-financial-analysis.json"
PROMPT_ALIAS = "prod"
NUMERIC_COLUMNS = (
    "revenue",
    "operating_expenses",
    "payroll",
    "cloud_compute_costs",
    "other_expenses",
    "total_expenses",
    "operating_income",
    "cash_balance",
    "accounts_receivable",
    "accounts_payable",
)
CSV_FIELDS = ("month", *NUMERIC_COLUMNS)

# ``operating_expenses`` is general operating spend excluding the separately
# reported payroll, cloud/compute, and other-expense categories. The last three
# columns are month-end balance-sheet snapshots; all amounts are USD.
MONTHLY_FINANCIALS: tuple[dict[str, str], ...] = (
    {
        "month": "2025-01",
        "revenue": "1240000",
        "operating_expenses": "185000",
        "payroll": "420000",
        "cloud_compute_costs": "92000",
        "other_expenses": "48000",
        "total_expenses": "745000",
        "operating_income": "495000",
        "cash_balance": "2850000",
        "accounts_receivable": "410000",
        "accounts_payable": "215000",
    },
    {
        "month": "2025-02",
        "revenue": "1315000",
        "operating_expenses": "190000",
        "payroll": "425000",
        "cloud_compute_costs": "95000",
        "other_expenses": "51000",
        "total_expenses": "761000",
        "operating_income": "554000",
        "cash_balance": "3010000",
        "accounts_receivable": "432000",
        "accounts_payable": "221000",
    },
    {
        "month": "2025-03",
        "revenue": "1380000",
        "operating_expenses": "198000",
        "payroll": "428000",
        "cloud_compute_costs": "101000",
        "other_expenses": "47000",
        "total_expenses": "774000",
        "operating_income": "606000",
        "cash_balance": "3225000",
        "accounts_receivable": "447000",
        "accounts_payable": "229000",
    },
    {
        "month": "2025-04",
        "revenue": "1290000",
        "operating_expenses": "192000",
        "payroll": "435000",
        "cloud_compute_costs": "104000",
        "other_expenses": "56000",
        "total_expenses": "787000",
        "operating_income": "503000",
        "cash_balance": "3310000",
        "accounts_receivable": "438000",
        "accounts_payable": "236000",
    },
    {
        "month": "2025-05",
        "revenue": "1460000",
        "operating_expenses": "205000",
        "payroll": "442000",
        "cloud_compute_costs": "109000",
        "other_expenses": "52000",
        "total_expenses": "808000",
        "operating_income": "652000",
        "cash_balance": "3540000",
        "accounts_receivable": "469000",
        "accounts_payable": "244000",
    },
    {
        "month": "2025-06",
        "revenue": "1530000",
        "operating_expenses": "212000",
        "payroll": "448000",
        "cloud_compute_costs": "115000",
        "other_expenses": "61000",
        "total_expenses": "836000",
        "operating_income": "694000",
        "cash_balance": "3795000",
        "accounts_receivable": "488000",
        "accounts_payable": "252000",
    },
    {
        "month": "2025-07",
        "revenue": "1490000",
        "operating_expenses": "210000",
        "payroll": "455000",
        "cloud_compute_costs": "121000",
        "other_expenses": "58000",
        "total_expenses": "844000",
        "operating_income": "646000",
        "cash_balance": "3960000",
        "accounts_receivable": "481000",
        "accounts_payable": "261000",
    },
    {
        "month": "2025-08",
        "revenue": "1610000",
        "operating_expenses": "218000",
        "payroll": "462000",
        "cloud_compute_costs": "128000",
        "other_expenses": "54000",
        "total_expenses": "862000",
        "operating_income": "748000",
        "cash_balance": "4215000",
        "accounts_receivable": "512000",
        "accounts_payable": "269000",
    },
    {
        "month": "2025-09",
        "revenue": "1570000",
        "operating_expenses": "216000",
        "payroll": "468000",
        "cloud_compute_costs": "126000",
        "other_expenses": "63000",
        "total_expenses": "873000",
        "operating_income": "697000",
        "cash_balance": "4380000",
        "accounts_receivable": "505000",
        "accounts_payable": "278000",
    },
    {
        "month": "2025-10",
        "revenue": "1720000",
        "operating_expenses": "225000",
        "payroll": "475000",
        "cloud_compute_costs": "134000",
        "other_expenses": "57000",
        "total_expenses": "891000",
        "operating_income": "829000",
        "cash_balance": "4675000",
        "accounts_receivable": "541000",
        "accounts_payable": "287000",
    },
    {
        "month": "2025-11",
        "revenue": "1810000",
        "operating_expenses": "232000",
        "payroll": "482000",
        "cloud_compute_costs": "141000",
        "other_expenses": "69000",
        "total_expenses": "924000",
        "operating_income": "886000",
        "cash_balance": "4950000",
        "accounts_receivable": "568000",
        "accounts_payable": "298000",
    },
    {
        "month": "2025-12",
        "revenue": "1960000",
        "operating_expenses": "245000",
        "payroll": "495000",
        "cloud_compute_costs": "153000",
        "other_expenses": "88000",
        "total_expenses": "981000",
        "operating_income": "979000",
        "cash_balance": "5320000",
        "accounts_receivable": "596000",
        "accounts_payable": "312000",
    },
)

PROMPT_TEMPLATE = """You are a financial analyst. Analyze the CSV supplied in the user message.

The CSV contains monthly USD values. `operating_expenses` excludes payroll,
cloud/compute, and other expenses. `total_expenses` is their sum, and
`operating_income` is revenue minus total expenses. Cash, receivables, and
payables are month-end balance-sheet snapshots.

For every numeric column, calculate mean, median, P25, P50, P75, P90, minimum,
and maximum. Use linear interpolation with rank `(n - 1) * percentile`; P50 must
equal the median. Minimum and maximum must include both month and value.

Return only one valid JSON object with this exact top-level shape:
{
  "currency": "USD",
  "periods": 12,
  "percentile_method": "linear_interpolation_rank_(n-1)*p",
  "statistics": {
    "<numeric_column>": {
      "mean": 0,
      "median": 0,
      "p25": 0,
      "p50": 0,
      "p75": 0,
      "p90": 0,
      "minimum": {"month": "YYYY-MM", "value": 0},
      "maximum": {"month": "YYYY-MM", "value": 0}
    }
  },
  "observations": ["two or three concise findings grounded in the numbers"]
}

Do not omit columns, add commentary outside the JSON, or invent data."""


def build_financial_csv() -> bytes:
    """Return the complete input fixture without writing a local file."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(MONTHLY_FINANCIALS)
    return stream.getvalue().encode("utf-8")


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def expected_statistics() -> dict[str, dict[str, Any]]:
    """Deterministic reference used to reject plausible-looking bad model math."""
    statistics: dict[str, dict[str, Any]] = {}
    for column in NUMERIC_COLUMNS:
        values = [float(row[column]) for row in MONTHLY_FINANCIALS]
        minimum = min(enumerate(values), key=lambda pair: pair[1])
        maximum = max(enumerate(values), key=lambda pair: pair[1])
        statistics[column] = {
            "mean": sum(values) / len(values),
            "median": _percentile(values, 0.50),
            "p25": _percentile(values, 0.25),
            "p50": _percentile(values, 0.50),
            "p75": _percentile(values, 0.75),
            "p90": _percentile(values, 0.90),
            "minimum": {
                "month": MONTHLY_FINANCIALS[minimum[0]]["month"],
                "value": minimum[1],
            },
            "maximum": {
                "month": MONTHLY_FINANCIALS[maximum[0]]["month"],
                "value": maximum[1],
            },
        }
    return statistics


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_analysis(content: str) -> dict[str, Any]:
    """Decode and validate the model's strict JSON financial-analysis result."""
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, count=1, flags=re.I)
        candidate = re.sub(r"\s*```$", "", candidate, count=1)
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("prompt result must be a JSON object")
    required_top_level = {
        "currency",
        "periods",
        "percentile_method",
        "statistics",
        "observations",
    }
    if set(parsed) != required_top_level:
        raise ValueError("prompt result does not match the required top-level shape")
    if parsed.get("currency") != "USD" or parsed.get("periods") != len(MONTHLY_FINANCIALS):
        raise ValueError("prompt result has the wrong currency or period count")
    if parsed.get("percentile_method") != "linear_interpolation_rank_(n-1)*p":
        raise ValueError("prompt result has the wrong percentile method")
    observations = parsed.get("observations")
    if (
        not isinstance(observations, list)
        or not 2 <= len(observations) <= 3
        or any(not isinstance(item, str) or not item.strip() for item in observations)
    ):
        raise ValueError("prompt result must contain two or three non-empty observations")
    statistics = parsed.get("statistics")
    if not isinstance(statistics, dict) or set(statistics) != set(NUMERIC_COLUMNS):
        raise ValueError("prompt result statistics do not match the required numeric columns")
    metric_names = ("mean", "median", "p25", "p50", "p75", "p90")
    required_metric_names = {*metric_names, "minimum", "maximum"}
    reference = expected_statistics()
    for column in NUMERIC_COLUMNS:
        metrics = statistics.get(column)
        if not isinstance(metrics, dict) or set(metrics) != required_metric_names:
            raise ValueError(f"prompt result has the wrong statistics shape for {column}")
        if any(not _is_number(metrics.get(name)) for name in metric_names):
            raise ValueError(f"prompt result has non-numeric statistics for {column}")
        if float(metrics["p50"]) != float(metrics["median"]):
            raise ValueError(f"prompt result p50 does not equal median for {column}")
        for name in metric_names:
            if not math.isclose(
                float(metrics[name]), float(reference[column][name]), rel_tol=0, abs_tol=0.01
            ):
                raise ValueError(f"prompt result has an incorrect {name} for {column}")
        for extreme in ("minimum", "maximum"):
            value = metrics.get(extreme)
            if (
                not isinstance(value, dict)
                or set(value) != {"month", "value"}
                or not _is_number(value.get("value"))
            ):
                raise ValueError(f"prompt result has an invalid {extreme} for {column}")
            expected_extreme = reference[column][extreme]
            if value.get("month") != expected_extreme["month"]:
                raise ValueError(f"prompt result has an invalid {extreme} month for {column}")
            if not math.isclose(
                float(value["value"]), float(expected_extreme["value"]), rel_tol=0, abs_tol=0.01
            ):
                raise ValueError(f"prompt result has an incorrect {extreme} for {column}")
    return parsed


def _required_mapping_value(payload: Any, key: str) -> Any:
    if not isinstance(payload, dict) or payload.get(key) in (None, ""):
        raise RuntimeError(f"SDK response is missing {key!r}")
    return payload[key]


def parse_turn_analysis(turn: Any) -> dict[str, Any]:
    """Surface provider failures before validating a successful JSON response."""
    assistant_message = _required_mapping_value(turn, "assistant_message")
    content = str(_required_mapping_value(assistant_message, "content"))
    metadata = assistant_message.get("metadata_", {})
    if isinstance(metadata, dict) and metadata.get("error") is True:
        raise RuntimeError(f"prompt execution failed: {content}")
    return parse_analysis(content)


def run(caliber: CaliberClient, *, run_key: str | None = None) -> dict[str, Any]:
    """Run the end-to-end financial workflow against one CALIBER deployment."""
    identity = caliber.me.get()
    if identity.is_anonymous:
        raise RuntimeError("CALIBER_TOKEN does not resolve to an authenticated user")

    key = run_key or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,47}", key):
        raise ValueError("run_key must be 3-48 lowercase letters, digits, or hyphens")

    project = caliber.projects.create(
        f"SDK Financial Analysis {key}",
        description="SDK-only monthly financial analysis with persisted input and output",
    )
    csv_bytes = build_financial_csv()
    bucket = f"sdk-financial-{key}"
    input_object_key = f"projects/{project.project_id}/inputs/{CSV_FILENAME}"
    output_object_key = f"projects/{project.project_id}/outputs/{RESULT_FILENAME}"

    with caliber.project_scope(project.project_id):
        store_status = caliber.object_store.status()
        if not isinstance(store_status, dict) or store_status.get("connected") is not True:
            raise RuntimeError("CALIBER object store is not connected")
        caliber.object_store.create_bucket(bucket)
        source = caliber.object_store.upload(
            bucket,
            filename=CSV_FILENAME,
            content=csv_bytes,
            key=input_object_key,
            media_type="text/csv",
        )
        stored_input_key = str(_required_mapping_value(source, "key"))
        stored_csv = caliber.object_store.download(bucket, stored_input_key)
        if stored_csv != csv_bytes:
            raise RuntimeError("downloaded CSV does not match the SDK-uploaded input")
        managed_source = caliber.object_store.import_object(
            bucket,
            stored_input_key,
            path=f"inputs/{CSV_FILENAME}",
        )
        managed_file_id = str(_required_mapping_value(managed_source, "file_id"))

        prompt_name = f"sdk-financial-analysis-{key}"
        created_prompt = caliber.prompts.create(
            prompt_name,
            PROMPT_TEMPLATE,
            commit_message="Create SDK financial-analysis cookbook prompt",
        )
        prompt_version = int(_required_mapping_value(created_prompt, "version"))
        registered_prompt = caliber.prompts.version(prompt_name, prompt_version)
        registered_template = str(_required_mapping_value(registered_prompt, "template"))
        promotion = caliber.prompts.promote(prompt_name, prompt_version, alias=PROMPT_ALIAS)
        if (
            _required_mapping_value(promotion, "name") != prompt_name
            or _required_mapping_value(promotion, "alias") != PROMPT_ALIAS
            or int(_required_mapping_value(promotion, "version")) != prompt_version
        ):
            raise RuntimeError("prompt promotion response does not match the created version")
        dashboard_prompt = next(
            (item for item in caliber.prompts.list() if item.agent_id == prompt_name),
            None,
        )
        if (
            dashboard_prompt is None
            or dashboard_prompt.has_prompt is not True
            or dashboard_prompt.alias != PROMPT_ALIAS
            or dashboard_prompt.version != prompt_version
        ):
            raise RuntimeError("promoted prompt is not visible as final in the prompt dashboard")

        session = caliber.aria.sessions.create(
            title=f"SDK Financial Analysis {key}",
            goal=(f"Follow this registered system prompt exactly:\n\n{registered_template}"),
            metadata_={
                "source": "sdk-cookbook-17",
                "project_id": project.project_id,
                "prompt_ref": f"prompts:/{prompt_name}@{PROMPT_ALIAS}",
                "blob_bucket": bucket,
                "blob_key": stored_input_key,
                "managed_file_id": managed_file_id,
            },
            artifact_type="prompt",
            skill_mode="off",
        )
        session_id = str(_required_mapping_value(session, "session_id"))
        turn = caliber.aria.sessions.send_message(
            session_id,
            (
                f"Analyze the SDK-uploaded file {CSV_FILENAME}. Its exact stored content is:\n\n"
                f"```csv\n{stored_csv.decode('utf-8')}\n```"
            ),
            artifact_type="prompt",
            skill_mode="off",
        )
        analysis = parse_turn_analysis(turn)

        result_document = {
            "scenario": "monthly_company_financial_analysis",
            "project_id": project.project_id,
            "blob_bucket": bucket,
            "input_object_key": stored_input_key,
            "managed_file_id": managed_file_id,
            "prompt_name": prompt_name,
            "prompt_version": prompt_version,
            "prompt_alias": PROMPT_ALIAS,
            "assistant_session_id": session_id,
            "analysis": analysis,
        }
        result_bytes = (json.dumps(result_document, indent=2, sort_keys=True) + "\n").encode()
        output = caliber.object_store.upload(
            bucket,
            filename=RESULT_FILENAME,
            content=result_bytes,
            key=output_object_key,
            media_type="application/json",
        )
        stored_output_key = str(_required_mapping_value(output, "key"))
        if caliber.object_store.download(bucket, stored_output_key) != result_bytes:
            raise RuntimeError("downloaded result does not match the SDK-uploaded output")

    return {
        "project_id": project.project_id,
        "blob_bucket": bucket,
        "input_object_key": stored_input_key,
        "managed_file_id": managed_file_id,
        "prompt_name": prompt_name,
        "prompt_version": prompt_version,
        "prompt_alias": PROMPT_ALIAS,
        "assistant_session_id": session_id,
        "output_object_key": stored_output_key,
        "analysis": analysis,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
