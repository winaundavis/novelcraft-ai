from pathlib import Path
import zipfile

from scripts.package_source import build_archive, validate_archive


def test_source_archive_has_posix_entries_and_extracts_key_paths(tmp_path):
    root = Path(__file__).resolve().parents[2]
    archive = build_archive(root, tmp_path / "novelcraft-source.zip")
    with zipfile.ZipFile(archive) as zipped:
        assert all("\\" not in name for name in zipped.namelist())
    report = validate_archive(archive, tmp_path / "extract")
    assert report["valid"]
