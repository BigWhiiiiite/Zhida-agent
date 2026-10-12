import importlib.util
from pathlib import Path
import sqlite3

import pytest

spec = importlib.util.spec_from_file_location("cutover_stage_test", Path(__file__).resolve().parents[1] / "scripts/cutover_workbench.py")
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)


def make_source(tmp_path):
    source = tmp_path / "original.db"
    with sqlite3.connect(source) as cx:
        cx.execute("CREATE TABLE resumes(id TEXT, user_id TEXT, profile_json TEXT, stored_filename TEXT)")
        cx.execute("CREATE TABLE candidate_profiles(id TEXT, user_id TEXT, profile_json TEXT)")
        cx.execute("INSERT INTO resumes VALUES('r','u','{}','synthetic.txt')")
    uploads = tmp_path / "original-uploads"
    uploads.mkdir()
    (uploads / "synthetic.txt").write_text("Only synthetic text")
    return source, uploads


def test_stage_keeps_originals_and_verifies_independent_attachment_copy(tmp_path):
    source, uploads = make_source(tmp_path)
    before = cutover.checksum(source)
    target = tmp_path / "stage"
    result = cutover.stage(source, uploads, target, frozen=True)
    assert result["attachments_verified"] == 1
    assert result["status"] == "staged_not_imported"
    assert cutover.checksum(source) == before
    assert (target / "uploads/synthetic.txt").read_bytes() == (uploads / "synthetic.txt").read_bytes()
    assert (target / "staging.json").stat().st_mode & 0o777 == 0o600
    assert (target / "uploads/synthetic.txt").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="已存在"):
        cutover.stage(source, uploads, target, frozen=True)
    with pytest.raises(ValueError, match="停止旧后端"):
        cutover.apply(target, frozen=False)


def test_changed_original_or_missing_attachment_blocks_cutover(tmp_path):
    source, uploads = make_source(tmp_path)
    target = tmp_path / "stage"
    cutover.stage(source, uploads, target, frozen=True)
    with sqlite3.connect(source) as cx:
        cx.execute("INSERT INTO candidate_profiles VALUES('p','u','{}')")
    with pytest.raises(ValueError, match="改变"):
        cutover.apply(target, frozen=True)
    (uploads / "synthetic.txt").rename(uploads / "moved.txt")
    with pytest.raises(ValueError, match="附件缺失"):
        cutover.stage(source, uploads, tmp_path / "stage-two", frozen=True)


def test_stage_refuses_live_source_and_detects_mutation_during_backup(tmp_path, monkeypatch):
    source, uploads = make_source(tmp_path)
    with pytest.raises(ValueError, match="停止旧后端"):
        cutover.stage(source, uploads, tmp_path / "live")
    backup = cutover.backup_sqlite
    def concurrent_write(original, destination):
        result = backup(original, destination)
        with sqlite3.connect(original) as cx:
            cx.execute("INSERT INTO candidate_profiles VALUES('p','u','{}')")
        return result
    monkeypatch.setattr(cutover, "backup_sqlite", concurrent_write)
    target = tmp_path / "racing-stage"
    with pytest.raises(ValueError, match="期间改变"):
        cutover.stage(source, uploads, target, frozen=True)
    assert not (target / "staging.json").exists()
