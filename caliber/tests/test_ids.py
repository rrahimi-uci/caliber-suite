"""Tests for ID generators."""

from __future__ import annotations

import re

from caliber.ids import (
    new_approval_id,
    new_item_id,
    new_job_id,
    new_knowledge_base_chunk_id,
    new_knowledge_base_entity_id,
    new_knowledge_base_relationship_id,
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


def test_bulk_knowledge_base_ids_are_sixty_four_bit_and_do_not_collide_at_build_scale() -> None:
    """A build writes thousands of chunks, entities and relationships; 32-bit ids collided in practice
    (``UNIQUE constraint failed: caliber_knowledge_base_relationships``). Found running cookbook 06."""
    for make, prefix in (
        (new_knowledge_base_chunk_id, "KBC"),
        (new_knowledge_base_entity_id, "KBE"),
        (new_knowledge_base_relationship_id, "KBRL"),
    ):
        sample = make()
        assert re.match(rf"^{prefix}-[0-9a-f]{{16}}$", sample), sample
        assert len(sample) <= 64  # the String(64) primary-key columns
        ids = {make() for _ in range(200_000)}
        assert len(ids) == 200_000
