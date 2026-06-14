# Resume, Sentinel, and OCR Hardening Implementation Plan

> **Archived:** This plan describes the removed CSV, SQLite, and resume workflow. It is retained only as history. See `README.md` and `docs/DESIGN.md` for the current implementation.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make real-send runs resumable while preventing boundary over-send when OCR or sentinel trimming is uncertain.

**Architecture:** Keep the existing `ForwardFlow` orchestration and SQLite `StateStore`. Add one target status, `uncertain`, filter resumable work before batching, and tighten sentinel trimming so real sends stop when the left checked recipient list cannot be verified before or after trimming.

**Tech Stack:** Python 3.11+, unittest, SQLite, existing PowerShell-backed screenshot/OCR helpers.

---

### Task 1: Status Model and Resume Filtering

**Files:**
- Modify: `src/wecom_rpa/models.py`
- Modify: `src/wecom_rpa/storage.py`
- Modify: `src/wecom_rpa/forward_flow.py`
- Test: `tests/test_storage_flow.py`

- [ ] Add `TargetStatus.UNCERTAIN = "uncertain"`.
- [ ] Add storage helpers to list statuses for the current CSV target names.
- [ ] At run start, stop if any current input target is `uncertain`.
- [ ] Build batches only from targets whose status is not `sent` or `skipped`.
- [ ] Add tests proving sent/skipped targets are not reprocessed and uncertain targets block resume.

### Task 2: Sentinel Trim Verification

**Files:**
- Modify: `src/wecom_rpa/forward_flow.py`
- Test: `tests/test_storage_flow.py`

- [ ] After unchecking sentinel and above-boundary recipients, verify the left-side checked count again.
- [ ] Confirm the post-trim count equals the number that will be sent.
- [ ] If post-trim verification fails and `stop_on_detection_failure` is true, mark this batch `uncertain`, screenshot, and stop before clicking send.
- [ ] Add tests for successful post-trim verification and failed post-trim verification.

### Task 3: Post-Send Evidence and Uncertain State

**Files:**
- Modify: `src/wecom_rpa/forward_flow.py`
- Test: `tests/test_storage_flow.py`

- [ ] Mark batch targets `selected` after recipient checkbox clicks.
- [ ] Click send.
- [ ] Save a post-send checkpoint before marking `sent`.
- [ ] If post-send evidence is unavailable, mark the clicked batch `uncertain` and raise.
- [ ] Add a test that simulates missing post-send evidence and verifies `uncertain`.

### Task 4: Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/DESIGN.md`
- Modify: `docs/SENTINEL_BOUNDARY_PLAN.md`

- [ ] Document real-send flags and the meaning of `uncertain`.
- [ ] Document resume behavior: `pending` continues, `sent/skipped` are skipped, `uncertain` blocks.
- [ ] Document sentinel OCR failure behavior and the expected manual recovery path.

### Verification

- [ ] Run `python -m unittest discover -s tests -v`.
- [ ] Confirm all tests pass or only existing optional dependency skips remain.
