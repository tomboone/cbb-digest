from pathlib import Path

import pytest

from cbb_digest.atomic import write_atomic


def test_creates_parent_directory_and_writes(tmp_path: Path) -> None:
    path = tmp_path / "out" / "digest.txt"

    write_atomic(path, "hello")

    assert path.read_text() == "hello"


def test_replaces_existing_file_without_leaving_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "digest.txt"
    path.write_text("old")

    write_atomic(path, "new")

    assert path.read_text() == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["digest.txt"]


def test_failed_write_keeps_the_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "digest.txt"
    path.write_text("old")

    def boom(src: str, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("cbb_digest.atomic.os.replace", boom)

    with pytest.raises(OSError, match="disk full"):
        write_atomic(path, "new")

    assert path.read_text() == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["digest.txt"]


def test_writes_bytes_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "blob"
    data = bytes(range(256))

    write_atomic(path, data)

    assert path.read_bytes() == data


def test_text_is_written_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "digest.txt"

    write_atomic(path, "CBB digest — week of Nov 9")

    assert path.read_bytes() == "CBB digest — week of Nov 9".encode()
