"""Tests for ID generators."""

from __future__ import annotations

import re

from caliber.ids import (
    new_approval_id,
    new_item_id,
    new_job_id,
    new_quality_review_id,
    new_rework_task_id,
)


def test_item_id_has_fb_prefix_and_hex_suffix() -> None:
    item_id = new_item_id()
    assert re.match(r"^FB-[0-9a-f]{8}$", item_id), item_id


def test_job_id_has_rfn_prefix_and_hex_suffix() -> None:
    job_id = new_job_id()
    assert re.match(r"^RFN-[0-9a-f]{8}$", job_id), job_id


def test_approval_id_has_ap_prefix_and_hex_suffix() -> None:
    approval_id = new_approval_id()
    assert re.match(r"^AP-[0-9a-f]{8}$", approval_id), approval_id


def test_rework_task_id_has_rwt_prefix_and_hex_suffix() -> None:
    task_id = new_rework_task_id()
    assert re.match(r"^RWT-[0-9a-f]{8}$", task_id), task_id


def test_quality_review_id_has_qrv_prefix_and_hex_suffix() -> None:
    review_id = new_quality_review_id()
    assert re.match(r"^QRV-[0-9a-f]{8}$", review_id), review_id


def test_ids_are_unique() -> None:
    # 1000 IDs at 32 bits of entropy should never collide in practice.
    ids = {new_item_id() for _ in range(1000)}
    assert len(ids) == 1000
