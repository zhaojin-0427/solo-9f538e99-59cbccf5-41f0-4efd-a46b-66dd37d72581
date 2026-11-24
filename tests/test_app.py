import io
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import flask  # noqa: F401
    from app import create_app
except ImportError:  # pragma: no cover
    flask = None


@unittest.skipIf(flask is None, "Flask is not installed in this interpreter")
class FlaskPageTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-flask-", suffix=".db")
        os.close(fd)
        self.app = create_app(self.db_path)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def upload_csv(self, content="单号,净重\nA,100\nB,90\nC,90\nD,80\n", day="2026-05-01"):
        data = {
            "date": day,
            "file": (io.BytesIO(content.encode("utf-8")), "tickets.csv"),
        }
        return self.client.post("/upload", data=data, content_type="multipart/form-data")

    def test_health_page_upload_query_and_tie_display(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["ok"])

        resp = self.upload_csv()
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/day?date=2026-05-01", resp.headers["Location"])

        resp = self.client.get(
            "/query?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("第 2-3 名", html)
        match = re.search(r'id="subtotal-access-count"[^>]*>\s*(\d+)\s*<', html)
        self.assertIsNotNone(match)
        accessed = int(match.group(1))
        self.assertLessEqual(accessed, 576)
        self.assertIn("576", html)
        self.assertIn("小计键访问数", html)

        # A balanced-tree worst interval plus weight zero reaches the proven
        # 540-key upper bound for two calibers.
        self.client.post(
            "/upload",
            data={"date": "2026-06-01", "csv_text": "X,0"},
        )
        worst = self.client.get(
            "/query?date=2026-06-01&ticket_no=X&start=2026-01-02&end=2026-12-30"
        ).get_data(as_text=True)
        worst_match = re.search(r'id="subtotal-access-count"[^>]*>\s*(\d+)\s*<', worst)
        self.assertEqual(int(worst_match.group(1)), 540)

        js = self.client.get(
            "/api/rank?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_json()
        self.assertTrue(js["ok"])
        self.assertTrue(js["result"]["meter"]["within_limit"])
        self.assertLessEqual(js["result"]["meter"]["accessed"], 576)

    def test_hold_shows_held_note_and_blank_rank_then_release(self):
        self.upload_csv()
        resp = self.client.post(
            "/hold", data={"date": "2026-05-01", "ticket_no": "B", "reason": "tie check"}
        )
        self.assertEqual(resp.status_code, 303)

        html = self.client.get(
            "/query?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_data(as_text=True)
        self.assertIn("目标单已暂扣", html)
        self.assertIn("留空", html)
        # All-caliber rank remains visible.
        self.assertIn("第 2-3 名", html)

        resp = self.client.post("/release", data={"date": "2026-05-01", "ticket_no": "B"})
        self.assertEqual(resp.status_code, 303)
        html = self.client.get(
            "/query?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_data(as_text=True)
        self.assertNotIn("目标单已暂扣；未暂扣口径只显示总数", html)

    def test_reupload_preserves_hold(self):
        self.upload_csv()
        self.client.post("/hold", data={"date": "2026-05-01", "ticket_no": "A"})
        # Replacement CSV omits A.
        self.upload_csv("单号,净重\nB,95\nC,90\nD,80\n", "2026-05-01")
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn("原单暂缺但仍有效的暂扣记录", day_html)
        self.assertIn(">A<", day_html)

    def test_invalid_upload_does_not_replace_day(self):
        self.upload_csv()
        bad = "单号,净重\nX,10\nX,11\nY,70000\n"
        resp = self.upload_csv(bad)
        # HTML error path redirects back and flashes; follow it.
        self.assertEqual(resp.status_code, 303)
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn(">A<", day_html)
        self.assertIn(">B<", day_html)


@unittest.skipIf(flask is None, "Flask is not installed in this interpreter")
class CorrectionFlaskTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="scale-corr-web-", suffix=".db")
        os.close(fd)
        self.app = create_app(self.db_path)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def upload(self, day, rows_text):
        return self.client.post(
            "/upload", data={"date": day, "csv_text": rows_text}
        )

    def test_correction_form_ranks_readback_and_cancel(self):
        self.upload("2026-05-01", "单号,净重\nA,100\nB,90\nC,90\nD,80\n")

        # The index page exposes the correction form and an empty list.
        index_html = self.client.get("/").get_data(as_text=True)
        self.assertIn("零点纠偏登记", index_html)
        self.assertIn("尚未登记任何纠偏日", index_html)

        resp = self.client.post(
            "/correction", data={"date": "2026-05-01", "offset": "10"}
        )
        self.assertEqual(resp.status_code, 303)
        self.assertIn("/day?date=2026-05-01", resp.headers["Location"])

        # Day readback shows both original and effective weights.
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn("原报净重", day_html)
        self.assertIn("纠偏后净重", day_html)
        self.assertIn(">100<", day_html)   # original of A
        self.assertIn(">110<", day_html)   # effective of A
        self.assertIn("+10", day_html)

        # Ranks use the effective weight: B is 90 -> 100, tied with C.
        js = self.client.get(
            "/api/rank?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_json()
        result = js["result"]
        self.assertEqual(result["original_weight"], 90)
        self.assertEqual(result["correction_offset_kg"], 10)
        self.assertEqual(result["effective_weight"], 100)
        self.assertEqual(result["all"]["rank_start"], 2)
        self.assertEqual(result["all"]["rank_end"], 3)
        self.assertLessEqual(result["meter"]["accessed"], 576)

        # Query page renders the correction summary.
        q_html = self.client.get(
            "/query?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_data(as_text=True)
        self.assertIn("原报净重", q_html)
        self.assertIn("纠偏后净重", q_html)

        # Registered correction days are listed on the index page.
        index_html = self.client.get("/").get_data(as_text=True)
        self.assertIn("2026-05-01", index_html)
        self.assertIn("取消纠偏", index_html)

        # Cancel with offset 0.
        resp = self.client.post(
            "/correction", data={"date": "2026-05-01", "offset": "0"}
        )
        self.assertEqual(resp.status_code, 303)
        js = self.client.get("/api/rank?date=2026-05-01&ticket_no=B").get_json()
        self.assertEqual(js["result"]["effective_weight"], 90)
        self.assertEqual(js["result"]["correction_offset_kg"], 0)

    def test_cross_day_same_effective_weight_tie(self):
        # Different originals on two different days end at the same effective
        # weight: A 100 kg (no correction) vs X 110 kg with a -10 correction.
        self.upload("2027-05-02", "单号,净重\nX,110\n")
        self.client.post(
            "/correction", data={"date": "2027-05-02", "offset": "-10"}
        )
        self.upload("2027-05-01", "单号,净重\nA,100\n")
        js = self.client.get(
            "/api/rank?date=2027-05-01&ticket_no=A&start=2027-01-01&end=2027-12-31"
        ).get_json()
        result = js["result"]
        # A original 100 (no correction on its day) and X 110-10=100 tie.
        self.assertEqual(result["effective_weight"], 100)
        self.assertEqual(result["all"]["total"], 2)
        self.assertEqual(result["all"]["higher"], 0)
        self.assertEqual(result["all"]["equal"], 2)
        self.assertEqual(
            (result["all"]["rank_start"], result["all"]["rank_end"]), (1, 2)
        )

    def test_reupload_out_of_range_after_correction_rejects_whole_day(self):
        self.upload("2026-05-01", "单号,净重\nA,100\n")
        r = self.client.post(
            "/api/correction", json={"date": "2026-05-01", "offset": 10}
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

        # Re-report with an original weight that overflows once corrected.
        resp = self.client.post(
            "/api/upload",
            data={"date": "2026-05-01", "csv_text": "单号,净重\nA,100\nC,65530\n"},
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("超出", body["error"])

        # Whole day rejected: old tickets and subtotals remain queryable.
        day_html = self.client.get("/day?date=2026-05-01").get_data(as_text=True)
        self.assertIn(">A<", day_html)
        self.assertNotIn(">C<", day_html)
        js = self.client.get("/api/rank?date=2026-05-01&ticket_no=A").get_json()
        self.assertEqual(js["result"]["all"]["total"], 1)
        self.assertEqual(js["result"]["effective_weight"], 110)

    def test_correction_rejected_out_of_range_and_one_per_year(self):
        self.upload("2026-05-01", "单号,净重\nA,65530\n")
        resp = self.client.post(
            "/api/correction", json={"date": "2026-05-01", "offset": 10}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("超出", resp.get_json()["error"])
        js = self.client.get("/api/rank?date=2026-05-01&ticket_no=A").get_json()
        self.assertEqual(js["result"]["effective_weight"], 65530)

        # Bad offset values.
        for bad in ("70000", "-70000", "abc", ""):
            resp = self.client.post(
                "/api/correction", json={"date": "2026-05-01", "offset": bad}
            )
            self.assertEqual(resp.status_code, 400, bad)

        # One correction day per year.
        self.client.post(
            "/api/correction", json={"date": "2026-05-01", "offset": 1}
        )
        resp = self.client.post(
            "/api/correction", json={"date": "2026-06-01", "offset": 1}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("每个自然年只能设一个纠偏日", resp.get_json()["error"])

    def test_hold_release_after_correction_uses_effective_weight(self):
        self.upload("2026-05-01", "单号,净重\nA,100\nB,90\n")
        self.client.post(
            "/correction", data={"date": "2026-05-01", "offset": "10"}
        )
        self.client.post("/hold", data={"date": "2026-05-01", "ticket_no": "A"})
        js = self.client.get(
            "/api/rank?date=2026-05-01&ticket_no=B&start=2026-01-01&end=2026-12-31"
        ).get_json()
        result = js["result"]
        self.assertEqual(result["all"]["total"], 2)
        self.assertEqual(result["all"]["higher"], 1)   # held A still in all
        self.assertEqual(result["active"]["total"], 1)
        self.assertEqual(result["active"]["higher"], 0)
        self.assertEqual(result["active"]["rank_start"], 1)

        self.client.post("/release", data={"date": "2026-05-01", "ticket_no": "A"})
        js = self.client.get("/api/rank?date=2026-05-01&ticket_no=B").get_json()
        self.assertEqual(js["result"]["active"]["total"], 2)
        self.assertEqual(js["result"]["active"]["higher"], 1)


if __name__ == "__main__":
    unittest.main()
