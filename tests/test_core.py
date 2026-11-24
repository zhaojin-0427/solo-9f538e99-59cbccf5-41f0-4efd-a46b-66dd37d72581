import os
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core


class KeyBoundTests(unittest.TestCase):
    def test_date_cover_never_uses_more_than_15_segments(self):
        for year in (2023, 2024, 2026):
            n = core.days_in_year(year)
            jan1 = date(year, 1, 1)
            worst = 0
            for start_offset in range(n):
                for end_offset in range(start_offset, n):
                    start = jan1 + core.timedelta(days=start_offset)
                    end = jan1 + core.timedelta(days=end_offset)
                    segments = core.date_cover(start, end)
                    worst = max(worst, len(segments))
                    self.assertLessEqual(len(segments), 15)
                    # Segments are disjoint and exactly cover the interval.
                    self.assertEqual(segments[0].lo, start_offset)
                    self.assertEqual(segments[-1].hi, end_offset + 1)
            self.assertLessEqual(worst, 15)

    def test_weight_cover_and_total_query_budget(self):
        worst_greater = 0
        for weight in range(core.WEIGHT_SIZE):
            greater = core.weight_greater_cover(weight)
            worst_greater = max(worst_greater, len(greater))
            self.assertLessEqual(len(greater), 16)

            # root total + greater bands + exact-weight leaf, per date segment
            weight_keys_per_date = 1 + len(greater) + 1
            self.assertLessEqual(weight_keys_per_date, 18)

        self.assertEqual(worst_greater, 16)
        # Two calibers * at most 15 date segments * 18 weight keys.
        self.assertLessEqual(2 * 15 * 18, core.SUBTOTAL_KEY_LIMIT)
        self.assertEqual(2 * 15 * 18, 540)


class RankFlowTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-test-", suffix=".db")
        os.close(fd)
        core.init_db_path(self.db_path)
        self.conn = core.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def upload(self, day="2026-05-01", tickets=None):
        if tickets is None:
            tickets = [("A", 100), ("B", 90), ("C", 90), ("D", 80)]
        return core.replace_day_tickets(self.conn, day, tickets)

    def test_tie_rank_interval_and_access_limit(self):
        self.upload()
        result = core.query_rank(
            self.conn, "2026-05-01", "B", "2026-01-01", "2026-12-31"
        )
        self.assertTrue(result["found"])
        self.assertEqual(result["all"]["total"], 4)
        self.assertEqual(result["all"]["higher"], 1)
        self.assertEqual(result["all"]["equal"], 2)
        self.assertEqual((result["all"]["rank_start"], result["all"]["rank_end"]), (2, 3))
        self.assertEqual(result["active"]["rank_start"], 2)
        self.assertEqual(result["active"]["rank_end"], 3)
        self.assertLessEqual(result["meter"]["accessed"], 576)
        self.assertEqual(result["meter"]["accessed"], len(result["meter"]["details"]))

    def test_held_target_leaves_active_rank_blank(self):
        self.upload()
        core.hold_ticket(self.conn, "2026-05-01", "B", "disputed")
        result = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertTrue(result["held"])
        self.assertEqual(result["all"]["rank_start"], 2)
        self.assertEqual(result["active"]["total"], 3)
        self.assertIsNone(result["active"]["rank_start"])
        self.assertIsNone(result["active"]["rank_end"])
        self.assertEqual(result["active"]["rank_text"], "")
        self.assertLessEqual(result["meter"]["accessed"], 576)

    def test_hold_survives_reupload_removal_and_reappearance(self):
        self.upload()
        core.hold_ticket(self.conn, "2026-05-01", "A")

        # Reupload omits A and changes B; hold row must remain.
        core.replace_day_tickets(
            self.conn,
            "2026-05-01",
            [("B", 95), ("C", 90), ("D", 80), ("E", 70)],
        )
        readback = core.get_day_readback(self.conn, "2026-05-01")
        self.assertEqual([h["ticket_no"] for h in readback["missing_holds"]], ["A"])

        # A reappears in a later whole-day replacement, still held and therefore
        # absent from active totals/ranks.
        core.replace_day_tickets(
            self.conn,
            "2026-05-01",
            [("A", 100), ("B", 95), ("C", 90), ("D", 80), ("E", 70)],
        )
        result = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(result["all"]["total"], 5)
        self.assertEqual(result["all"]["higher"], 1)  # A in all caliber
        self.assertEqual(result["active"]["total"], 4)
        self.assertEqual(result["active"]["higher"], 0)  # A excluded
        self.assertEqual(result["active"]["rank_start"], 1)

        core.release_ticket(self.conn, "2026-05-01", "A")
        result = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(result["active"]["total"], 5)
        self.assertEqual(result["active"]["higher"], 1)

    def test_failed_csv_validation_leaves_old_data_and_queryable(self):
        self.upload()
        before = core.query_rank(self.conn, "2026-05-01", "B")
        with self.assertRaises(core.ValidationError):
            core.parse_csv_text("X,10\nX,11\nY,70000\n")
        # Validation happens before replace_day_tickets; explicitly feed parsed
        # bad data is impossible, so call replacement only with good parsed data.
        after = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(before["all"], after["all"])

    def test_failed_database_delta_rolls_back_ticket_and_hold(self):
        self.upload()
        original_adjust = core.adjust_existing_ticket_active

        def fail(*args, **kwargs):
            raise RuntimeError("injected subtotal failure")

        core.adjust_existing_ticket_active = fail
        try:
            with self.assertRaises(RuntimeError):
                core.hold_ticket(self.conn, "2026-05-01", "B")
        finally:
            core.adjust_existing_ticket_active = original_adjust

        # Transaction rolled back: no hold and ticket still queryable as active.
        result = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertFalse(result["held"])
        self.assertEqual(result["active"]["total"], 4)

    def test_failed_reupload_delta_rolls_back_to_old_day(self):
        self.upload()
        original_apply = core.apply_delta

        def fail(conn, delta):
            if getattr(fail, "armed", False):
                raise RuntimeError("injected replace failure")
            return original_apply(conn, delta)

        fail.armed = True
        core.apply_delta = fail
        try:
            with self.assertRaises(RuntimeError):
                core.replace_day_tickets(
                    self.conn, "2026-05-01", [("Z", 1), ("Y", 2)]
                )
        finally:
            core.apply_delta = original_apply

        rows = self.conn.execute(
            "SELECT ticket_no FROM tickets WHERE slip_date=? ORDER BY ticket_no",
            ("2026-05-01",),
        ).fetchall()
        self.assertEqual([r["ticket_no"] for r in rows], ["A", "B", "C", "D"])
        result = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(result["all"]["total"], 4)

    def test_csv_parsing_headers_headerless_and_bounds(self):
        rows = core.parse_csv_text("单号,净重\nX,0\nY,65535\n")
        self.assertEqual(rows, [("X", 0), ("Y", 65535)])
        rows = core.parse_csv_text("X,12\nY,13\n")
        self.assertEqual(rows, [("X", 12), ("Y", 13)])
        with self.assertRaises(core.ValidationError):
            core.parse_csv_text("X,-1\n")
        with self.assertRaises(core.ValidationError):
            core.parse_csv_text("X,65536\n")
        with self.assertRaises(core.ValidationError):
            core.parse_csv_text("X,1.5\n")
        with self.assertRaises(core.ValidationError):
            core.parse_csv_text("X,1\nX,2\n")

    def test_query_plan_uses_primary_key_index(self):
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM tickets WHERE slip_date=? AND ticket_no=?",
            ("2026-05-01", "B"),
        ).fetchall()
        text = "\n".join(row[3] for row in plan)
        self.assertIn("PRIMARY KEY", text.upper())


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-corr-", suffix=".db")
        os.close(fd)
        core.init_db_path(self.db_path)
        self.conn = core.connect(self.db_path)

    def tearDown(self):
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def upload(self, day="2026-05-01", tickets=None):
        if tickets is None:
            tickets = [("A", 100), ("B", 90), ("C", 90), ("D", 80)]
        return core.replace_day_tickets(self.conn, day, tickets)

    def test_offset_parsing_bounds(self):
        self.assertEqual(core.parse_correction_offset(" +120 "), 120)
        self.assertEqual(core.parse_correction_offset(-65535), -65535)
        self.assertEqual(core.parse_correction_offset(65535), 65535)
        self.assertEqual(core.parse_correction_offset("0"), 0)
        for bad in ("", None, "abc", "1.5", 65536, -65536, "1e3"):
            with self.assertRaises(core.ValidationError):
                core.parse_correction_offset(bad)

    def test_register_keeps_original_and_shifts_ranks(self):
        self.upload()
        result = core.register_correction(self.conn, "2026-05-01", 10)
        self.assertEqual(result["action"], "registered")
        self.assertEqual(result["offset_kg"], 10)

        # Original net weights are untouched in the tickets table.
        rows = self.conn.execute(
            "SELECT ticket_no, net_weight FROM tickets ORDER BY ticket_no"
        ).fetchall()
        self.assertEqual(
            [(r["ticket_no"], r["net_weight"]) for r in rows],
            [("A", 100), ("B", 90), ("C", 90), ("D", 80)],
        )

        readback = core.get_day_readback(self.conn, "2026-05-01")
        self.assertEqual(readback["correction_offset_kg"], 10)
        by_no = {t["ticket_no"]: t for t in readback["tickets"]}
        self.assertEqual(by_no["A"]["net_weight"], 100)
        self.assertEqual(by_no["A"]["effective_weight"], 110)
        self.assertEqual(by_no["B"]["effective_weight"], 100)

        rank = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(rank["original_weight"], 90)
        self.assertEqual(rank["effective_weight"], 100)
        self.assertEqual(rank["correction_offset_kg"], 10)
        # A=110, B/C=100, D=90 -> B still shares rank 2-3.
        self.assertEqual(rank["all"]["higher"], 1)
        self.assertEqual(rank["all"]["equal"], 2)
        self.assertEqual(
            (rank["all"]["rank_start"], rank["all"]["rank_end"]), (2, 3)
        )
        self.assertEqual(rank["active"]["rank_start"], 2)

    def test_negative_offset_and_cancellation_restores_state(self):
        self.upload(tickets=[("A", 100), ("B", 50)])
        core.register_correction(self.conn, "2026-05-01", -30)
        rank = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(rank["effective_weight"], 20)
        self.assertEqual(rank["all"]["total"], 2)
        # Subtotal leaf for 50 must be gone, leaf 20 present.
        leaves = core.read_single_day_leaf_counts(self.conn, "2026-05-01")
        self.assertNotIn((50, core.SCOPE_ALL), leaves)
        self.assertEqual(leaves[(20, core.SCOPE_ALL)], 1)

        cancel = core.register_correction(self.conn, "2026-05-01", 0)
        self.assertEqual(cancel["action"], "cancelled")
        self.assertEqual(
            core.get_correction(self.conn, "2026-05-01"), 0
        )
        leaves = core.read_single_day_leaf_counts(self.conn, "2026-05-01")
        self.assertEqual(leaves[(50, core.SCOPE_ALL)], 1)
        self.assertNotIn((20, core.SCOPE_ALL), leaves)
        rank = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(rank["effective_weight"], 50)

    def test_correction_rejected_when_any_ticket_goes_out_of_range(self):
        self.upload(tickets=[("A", 65530), ("B", 10)])
        with self.assertRaises(core.ValidationError) as ctx:
            core.register_correction(self.conn, "2026-05-01", 10)
        self.assertIn("超出", ctx.exception.message)

        # Nothing changed: no correction row, subtotals still at originals.
        self.assertEqual(core.get_correction(self.conn, "2026-05-01"), 0)
        leaves = core.read_single_day_leaf_counts(self.conn, "2026-05-01")
        self.assertEqual(leaves[(65530, core.SCOPE_ALL)], 1)
        rank = core.query_rank(self.conn, "2026-05-01", "A")
        self.assertEqual(rank["effective_weight"], 65530)

        # Negative side: a ticket at weight 5 shifted by -10 goes below zero.
        core.replace_day_tickets(
            self.conn, "2026-05-01", [("A", 65530), ("B", 5)]
        )
        with self.assertRaises(core.ValidationError):
            core.register_correction(self.conn, "2026-05-01", -10)
        self.assertEqual(core.get_correction(self.conn, "2026-05-01"), 0)
        leaves = core.read_single_day_leaf_counts(self.conn, "2026-05-01")
        self.assertEqual(leaves[(5, core.SCOPE_ALL)], 1)

        # Landing exactly on zero is accepted.
        core.register_correction(self.conn, "2026-05-01", -5)
        rank = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(rank["effective_weight"], 0)

    def test_boundary_zero_and_max_are_allowed(self):
        # Offset 0 on an existing day is a no-op and stores no row.
        self.upload(tickets=[("Z", 0), ("M", 65535)])
        core.register_correction(self.conn, "2026-05-01", 0)
        self.assertEqual(core.get_correction(self.conn, "2026-05-01"), 0)
        self.assertEqual(core.list_corrections(self.conn, 2026), [])

        # Corrections landing exactly on the 0/65535 boundaries are accepted.
        core.replace_day_tickets(self.conn, "2026-05-02", [("Z", 0)])
        core.register_correction(self.conn, "2026-05-02", 65535)
        rank = core.query_rank(self.conn, "2026-05-02", "Z")
        self.assertEqual(rank["effective_weight"], 65535)

        core.replace_day_tickets(self.conn, "2027-05-02", [("M", 65535)])
        core.register_correction(self.conn, "2027-05-02", -65535)
        rank = core.query_rank(self.conn, "2027-05-02", "M")
        self.assertEqual(rank["effective_weight"], 0)

    def test_only_one_correction_day_per_year(self):
        self.upload()
        core.register_correction(self.conn, "2026-05-01", 10)
        with self.assertRaises(core.ValidationError):
            core.register_correction(self.conn, "2026-06-01", 5)
        # A different year is fine.
        core.register_correction(self.conn, "2027-06-01", 5)
        self.assertEqual(core.get_correction(self.conn, "2027-06-01"), 5)

        # The expression unique index also blocks a raw second insert.
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO corrections(slip_date, offset_kg) VALUES (?, ?)",
                ("2026-07-01", 1),
            )

        # Cancelling the first day frees the year for a fresh registration.
        core.register_correction(self.conn, "2026-05-01", 0)
        core.register_correction(self.conn, "2026-08-01", -20)
        self.assertEqual(core.get_correction(self.conn, "2026-08-01"), -20)
        items = core.list_corrections(self.conn, 2026)
        self.assertEqual([c["slip_date"] for c in items], ["2026-08-01"])

    def test_updating_offset_rebases_subtotals(self):
        self.upload(tickets=[("A", 100)])
        core.register_correction(self.conn, "2026-05-01", 10)
        core.register_correction(self.conn, "2026-05-01", 25)
        leaves = core.read_single_day_leaf_counts(self.conn, "2026-05-01")
        self.assertEqual(leaves[(125, core.SCOPE_ALL)], 1)
        self.assertNotIn((110, core.SCOPE_ALL), leaves)
        rank = core.query_rank(self.conn, "2026-05-01", "A")
        self.assertEqual(rank["effective_weight"], 125)

    def test_hold_release_uses_effective_weight(self):
        self.upload(tickets=[("A", 100), ("B", 90)])
        core.register_correction(self.conn, "2026-05-01", 10)
        core.hold_ticket(self.conn, "2026-05-01", "A")
        rank = core.query_rank(self.conn, "2026-05-01", "B")
        # A (effective 110) is held: all-caliber still counts it, active excludes.
        self.assertEqual(rank["all"]["total"], 2)
        self.assertEqual(rank["all"]["higher"], 1)
        self.assertEqual(rank["active"]["total"], 1)
        self.assertEqual(rank["active"]["higher"], 0)
        self.assertEqual(rank["active"]["rank_start"], 1)

        core.release_ticket(self.conn, "2026-05-01", "A")
        rank = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(rank["active"]["total"], 2)
        self.assertEqual(rank["active"]["higher"], 1)

    def test_held_ticket_moves_when_correction_registered_after_hold(self):
        self.upload(tickets=[("A", 100), ("B", 90)])
        core.hold_ticket(self.conn, "2026-05-01", "A")
        core.register_correction(self.conn, "2026-05-01", 10)
        leaves = core.read_single_day_leaf_counts(self.conn, "2026-05-01")
        # Held A moved in all-caliber only; B moved in both calibers.
        self.assertEqual(leaves[(110, core.SCOPE_ALL)], 1)
        self.assertNotIn((110, core.SCOPE_ACTIVE), leaves)
        self.assertEqual(leaves[(100, core.SCOPE_ALL)], 1)
        self.assertEqual(leaves[(100, core.SCOPE_ACTIVE)], 1)
        # Cancel shifts everything back, hold still intact.
        core.register_correction(self.conn, "2026-05-01", 0)
        rank = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertTrue(rank["held"] is False)
        held = core.get_ticket_readback(self.conn, "2026-05-01", "A")
        self.assertTrue(held["held"])
        self.assertEqual(held["effective_weight"], 100)

    def test_reupload_uses_registered_offset_and_rejects_oob_whole_day(self):
        self.upload(tickets=[("A", 100)])
        core.register_correction(self.conn, "2026-05-01", 10)

        # Re-report original weights; offset is re-applied automatically.
        summary = core.replace_day_tickets(
            self.conn, "2026-05-01", [("A", 100), ("B", 90)]
        )
        self.assertEqual(summary["correction_offset_kg"], 10)
        leaves = core.read_single_day_leaf_counts(self.conn, "2026-05-01")
        self.assertEqual(leaves[(110, core.SCOPE_ALL)], 1)
        self.assertEqual(leaves[(100, core.SCOPE_ALL)], 1)
        rank = core.query_rank(self.conn, "2026-05-01", "B")
        self.assertEqual(rank["effective_weight"], 100)
        # Originals stored as entered.
        row = self.conn.execute(
            "SELECT net_weight FROM tickets WHERE ticket_no='B'"
        ).fetchone()
        self.assertEqual(row["net_weight"], 90)

        # A re-upload whose corrected weight overflows rejects the whole day.
        with self.assertRaises(core.ValidationError):
            core.replace_day_tickets(
                self.conn, "2026-05-01", [("A", 100), ("C", 65530)]
            )
        rows = self.conn.execute(
            "SELECT ticket_no FROM tickets WHERE slip_date='2026-05-01' ORDER BY ticket_no"
        ).fetchall()
        self.assertEqual([r["ticket_no"] for r in rows], ["A", "B"])
        rank = core.query_rank(self.conn, "2026-05-01", "A")
        self.assertEqual(rank["all"]["total"], 2)

    def test_correction_on_empty_day_is_allowed(self):
        result = core.register_correction(self.conn, "2026-05-01", 100)
        self.assertEqual(result["action"], "registered")
        self.assertEqual(result["leaf_weights_checked"], 0)
        # Uploading afterwards applies the standing offset.
        core.replace_day_tickets(self.conn, "2026-05-01", [("A", 50)])
        rank = core.query_rank(self.conn, "2026-05-01", "A")
        self.assertEqual(rank["effective_weight"], 150)

    def test_failed_correction_write_rolls_back(self):
        self.upload(tickets=[("A", 100)])
        original_apply = core.shift_day_subtotals

        def fail(conn, day, leaves, shift):
            raise RuntimeError("injected shift failure")

        core.shift_day_subtotals = fail
        try:
            with self.assertRaises(RuntimeError):
                core.register_correction(self.conn, "2026-05-01", 10)
        finally:
            core.shift_day_subtotals = original_apply

        self.assertEqual(core.get_correction(self.conn, "2026-05-01"), 0)
        leaves = core.read_single_day_leaf_counts(self.conn, "2026-05-01")
        self.assertEqual(leaves[(100, core.SCOPE_ALL)], 1)
        rank = core.query_rank(self.conn, "2026-05-01", "A")
        self.assertEqual(rank["effective_weight"], 100)

    def test_meter_access_count_unchanged_by_correction(self):
        self.upload()
        core.register_correction(self.conn, "2026-05-01", 10)
        rank = core.query_rank(
            self.conn, "2026-05-01", "B", "2026-01-01", "2026-12-31"
        )
        self.assertLessEqual(rank["meter"]["accessed"], core.SUBTOTAL_KEY_LIMIT)
        self.assertEqual(
            rank["meter"]["accessed"], len(rank["meter"]["details"])
        )


if __name__ == "__main__":
    unittest.main()
