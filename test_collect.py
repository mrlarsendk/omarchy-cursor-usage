#!/usr/bin/python3
"""Tests for the untrusted-path handling in collect.py.

Credentials, Cursor's database and our own cache all sit at fixed,
guessable paths, so every read has to survive another same-user process
swapping something in. Run with: python3 -m unittest test_collect -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from pathlib import Path

import collect


def make_state_db(path: Path, token: str = "tok-db", membership: str = "pro") -> None:
  conn = sqlite3.connect(path)
  try:
    conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
    conn.executemany(
      "INSERT INTO ItemTable VALUES (?, ?)",
      [("cursorAuth/accessToken", token), ("cursorAuth/stripeMembershipType", membership)],
    )
    conn.commit()
  finally:
    conn.close()


class TempDirTest(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp = Path(tempfile.mkdtemp())
    self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))


class AuthJsonTest(TempDirTest):
  def write_auth(self, name: str = "auth.json", **payload) -> Path:
    path = self.tmp / name
    path.write_text(json.dumps(payload or {"accessToken": "tok-abc", "membershipType": "pro"}))
    return path

  def test_reads_a_plain_file(self):
    creds, error = collect.load_credentials_from_auth_json(self.write_auth())
    self.assertIsNone(error)
    self.assertEqual(creds["accessToken"], "tok-abc")
    self.assertEqual(creds["membershipType"], "pro")

  def test_refuses_a_symlink(self):
    real = self.write_auth("real.json")
    link = self.tmp / "link.json"
    link.symlink_to(real)
    self.assertEqual(collect.load_credentials_from_auth_json(link), (None, None))

  def test_refuses_an_oversized_file(self):
    fat = self.tmp / "fat.json"
    fat.write_bytes(b"x" * (collect.MAX_LOCAL_JSON_BYTES + 1))
    self.assertEqual(collect.load_credentials_from_auth_json(fat), (None, None))

  def test_missing_file_falls_through_quietly(self):
    self.assertEqual(collect.load_credentials_from_auth_json(self.tmp / "gone.json"), (None, None))

  def test_a_fifo_does_not_block(self):
    fifo = self.tmp / "fifo.json"
    os.mkfifo(fifo)
    started = time.monotonic()
    creds, _ = collect.load_credentials_from_auth_json(fifo)
    self.assertIsNone(creds)
    self.assertLess(time.monotonic() - started, 2.0, "open blocked on a planted FIFO")

  def test_malformed_json_reports_an_error(self):
    bad = self.tmp / "bad.json"
    bad.write_text("{not json")
    creds, error = collect.load_credentials_from_auth_json(bad)
    self.assertIsNone(creds)
    self.assertTrue(error["authHelpText"])


class StateDbTest(TempDirTest):
  def test_reads_a_plain_database(self):
    db = self.tmp / "state.vscdb"
    make_state_db(db)
    creds, error = collect.load_credentials_from_state_db(db)
    self.assertIsNone(error)
    self.assertEqual(creds["accessToken"], "tok-db")

  def test_refuses_a_symlinked_database(self):
    planted = self.tmp / "planted.vscdb"
    make_state_db(planted, token="tok-ATTACKER")
    link = self.tmp / "state.vscdb"
    link.symlink_to(planted)
    creds, _ = collect.load_credentials_from_state_db(link)
    self.assertIsNone(creds, "followed a symlink planted over the database")

  def test_missing_database_falls_through_to_the_agent_credentials(self):
    # load_credentials() stops at the first error, so a missing IDE database
    # must stay quiet or it strands cursor-agent-only users.
    self.assertEqual(collect.load_credentials_from_state_db(self.tmp / "gone.vscdb"), (None, None))

  def test_a_non_file_falls_through_rather_than_failing_the_collector(self):
    fifo = self.tmp / "fifo.vscdb"
    os.mkfifo(fifo)
    started = time.monotonic()
    self.assertEqual(collect.load_credentials_from_state_db(fifo), (None, None))
    self.assertLess(time.monotonic() - started, 2.0, "open blocked on a planted FIFO")
    directory = self.tmp / "dir.vscdb"
    directory.mkdir()
    self.assertEqual(collect.load_credentials_from_state_db(directory), (None, None))

  def test_corrupt_database_reports_an_error(self):
    corrupt = self.tmp / "corrupt.vscdb"
    corrupt.write_text("this is not a database")
    creds, error = collect.load_credentials_from_state_db(corrupt)
    self.assertIsNone(creds)
    self.assertTrue(error["authHelpText"])

  def test_a_relative_path_still_resolves(self):
    make_state_db(self.tmp / "rel.vscdb", token="tok-rel")
    cwd = os.getcwd()
    os.chdir(self.tmp)
    try:
      creds, _ = collect.load_credentials_from_state_db(Path("rel.vscdb"))
    finally:
      os.chdir(cwd)
    self.assertEqual(creds["accessToken"], "tok-rel")

  def test_a_live_wal_database_stays_readable(self):
    db = self.tmp / "wal.vscdb"
    make_state_db(db, token="tok-wal")
    conn = sqlite3.connect(db)
    try:
      conn.execute("PRAGMA journal_mode=WAL")
      conn.execute("INSERT INTO ItemTable VALUES ('k', 'v')")
      conn.commit()
      creds, _ = collect.load_credentials_from_state_db(db)
      self.assertEqual(creds["accessToken"], "tok-wal")
    finally:
      conn.close()


class CacheTest(TempDirTest):
  def setUp(self) -> None:
    super().setUp()
    self.previous = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(self.tmp / "xdg")
    self.record = collect.usage_record_path()
    self.record.parent.mkdir(parents=True, exist_ok=True)
    self.addCleanup(self.restore_env)

  def restore_env(self) -> None:
    if self.previous is None:
      os.environ.pop("XDG_STATE_HOME", None)
    else:
      os.environ["XDG_STATE_HOME"] = self.previous

  def test_reads_a_plain_cache(self):
    self.record.write_text(json.dumps({"totalPrompts": 5}))
    self.assertEqual(collect.load_cached_stats().get("totalPrompts"), 5)

  def test_refuses_a_symlinked_cache(self):
    elsewhere = self.tmp / "elsewhere.json"
    elsewhere.write_text(json.dumps({"totalPrompts": 5}))
    self.record.symlink_to(elsewhere)
    self.assertEqual(collect.load_cached_stats(), {})

  def test_refuses_an_oversized_cache(self):
    self.record.write_bytes(b"x" * (collect.MAX_LOCAL_JSON_BYTES + 1))
    self.assertEqual(collect.load_cached_stats(), {})

  def test_the_cap_leaves_real_records_ample_headroom(self):
    # Real records measure a few KB; the cap exists to bound a swapped-in
    # file, not to constrain ordinary growth.
    self.assertGreater(collect.MAX_LOCAL_JSON_BYTES, 50 * 3570)


class WriteJsonTest(TempDirTest):
  def test_writes_content_at_mode_0600(self):
    target = self.tmp / "state" / "cursor.json"
    collect.write_json(target, {"b": 2, "a": 1})
    self.assertEqual(json.loads(target.read_text()), {"a": 1, "b": 2})
    self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

  def test_a_symlink_at_the_old_predictable_temp_name_is_not_followed(self):
    target = self.tmp / "state" / "cursor.json"
    collect.write_json(target, {"a": 1})
    victim = self.tmp / "victim.txt"
    victim.write_text("PRECIOUS\n")
    target.with_name(f".{target.name}.{os.getpid()}.tmp").symlink_to(victim)
    collect.write_json(target, {"a": 99})
    self.assertEqual(victim.read_text(), "PRECIOUS\n", "wrote through a planted symlink")
    self.assertEqual(json.loads(target.read_text()), {"a": 99})

  def test_leaves_no_temp_files_behind(self):
    target = self.tmp / "state" / "cursor.json"
    collect.write_json(target, {"a": 1})
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    self.assertEqual(leftovers, [])

  def test_the_temp_file_is_removed_when_the_write_fails(self):
    # A non-empty directory where the record belongs makes the final rename
    # fail, which is the only step that can fail after the temp file exists.
    target = self.tmp / "state" / "cursor.json"
    target.mkdir(parents=True)
    (target / "occupied").write_text("x")
    with self.assertRaises(OSError):
      collect.write_json(target, {"a": 1})
    leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    self.assertEqual(leftovers, [], "left a temp file behind after a failed write")


if __name__ == "__main__":
  unittest.main()
