from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from agent.ambiguity_detector import AmbiguityDetector
from config import ConfigLoader
from models.enums import EmployeeLevel, InvoiceType
from models.expense import Employee, ExpenseReport, Invoice, LineItem
from skills.skill_03_compliance import process as compliance_process


CASES = yaml.safe_load(
    (Path(__file__).parent / "eval_datasets" / "field_source_trust.yaml").read_text(
        encoding="utf-8",
    )
)


def _employee() -> Employee:
    return Employee(
        id="emp-field-source",
        name="Field Source Eval",
        department="Engineering",
        city="上海",
        level=EmployeeLevel.L1,
        hire_date=date(2020, 1, 1),
        bank_account="6222021234567890123",
    )


def _line_item(amount: float, amount_source: str) -> LineItem:
    expense_date = date(2026, 5, 11)
    return LineItem(
        expense_type="meals",
        amount=amount,
        currency="CNY",
        city="上海",
        date=expense_date,
        description="项目评审会议午餐",
        field_sources={"amount": amount_source},
        invoice=Invoice(
            invoice_code="310012135012",
            invoice_number=f"FS-{amount_source}",
            invoice_type=InvoiceType.NORMAL,
            amount=amount,
            tax_amount=0,
            date=expense_date,
            vendor="测试餐厅",
            city="上海",
            buyer_name="示例科技有限公司",
        ),
    )


def test_field_source_trust_eval_cases() -> None:
    ConfigLoader.reset()
    loader = ConfigLoader()
    loader.load()
    employee = _employee()
    detector = AmbiguityDetector(loader)

    for case in CASES:
        item = _line_item(case["amount"], case["amount_source"])
        report = ExpenseReport(
            report_id=case["id"],
            employee=employee,
            line_items=[item],
            total_amount=case["amount"],
            submit_date=datetime.now(timezone.utc),
        )

        compliance = compliance_process(report, employee=employee)
        issue_text = "\n".join(compliance.issues)
        assert ("金额来源为" in issue_text) is bool(case["expect_trust_warning"])

        ambiguity = detector.evaluate(item, employee, [], [])
        expected_factor = case.get("expect_ambiguity_factor")
        if expected_factor:
            assert expected_factor in ambiguity.triggered_factors
        else:
            assert "field_source_trust" not in ambiguity.triggered_factors
