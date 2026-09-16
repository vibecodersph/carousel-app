import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
import sqlite3
import contextlib
import io
from unittest.mock import patch

import aibrief_learning_loop as loop
import source_feedback as sf
from scripts import aibrief_learning_loop as cli
from tests.test_source_feedback import report, candidate, START, AS_OF


class LearningLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "loop"
        self.batches = self.root / "batches"
        feedback = sf.build_feedback(report())
        batch = sf.recommend(feedback, [candidate()], account="aibrief_jp", batch_id="b1", count=1, now=AS_OF)
        batch["exact_binding_required"] = True
        batch["batch_sha256"] = sf.digest({k:v for k,v in batch.items() if k!='batch_sha256'})
        loop.save_immutable(self.batches / "b1.json", batch)
        loop.save_immutable(self.batches / "evidence" / f"{feedback['feedback_id']}.json", feedback)
        self.candidates = self.root / "target00001/candidates.json"
        self.candidates.parent.mkdir()
        self.candidates.write_text(json.dumps({"clips":[{"slug":"clip1"}]}))
        self.spec = {"batch_id":"b1", "source_id":"target00001", "candidates_path":str(self.candidates),
                     "clip_slug":"clip1", "learning_applied":"Concrete mechanism", "selection_reason":"Actual payoff",
                     "hook_or_payoff_change":"Lead with the observed result", "evidence_media_ids":["0"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_seal_and_exact_published_attribution(self):
        plan = loop.plan_production(self.spec, self.batches, self.state, AS_OF)
        media = self.candidates.parent / "clips/clip1/reel.mp4"
        media.parent.mkdir(parents=True)
        media.write_bytes(b'test media')
        (media.parent / "notes.json").write_text('{}')
        with self.assertRaisesRegex(ValueError, "sealed"):
            loop.binding_for_media(media, self.state)
        bound = loop.seal_production(plan["plan_id"], media, "Test fixture QA", self.state, AS_OF)
        self.assertEqual(loop.seal_production(plan["plan_id"], media, "Test fixture QA", self.state, AS_OF+timedelta(hours=1)), bound)
        self.assertEqual(loop.binding_for_media(media, self.state), bound)
        p = {"identity":{"account":"aibrief_jp", "platform":"instagram", "content_hash":bound['content_hash'],
                         "published_at":(AS_OF+timedelta(days=2)).isoformat()},
             "content_metadata":{"source":"target00001", "trial_reel":False}}
        batches = sf.read_batches(self.batches, 'aibrief_jp')
        self.assertIsNone(sf._recommendation_for(p,batches))
        loop.attach_exact_bindings({'posts':[p]},self.state)
        self.assertEqual(sf._recommendation_for(p,batches)[0]['batch_id'],'b1')
        (media.parent / "notes.json").write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError,"notes changed"):
            loop.binding_for_media(media,self.state)

    def test_stale_candidate_unknown_evidence_and_source_rejected(self):
        self.spec['evidence_media_ids']=['invented']
        with self.assertRaisesRegex(ValueError,'unknown evidence'):
            loop.plan_production(self.spec,self.batches,self.state,AS_OF)
        self.spec['evidence_media_ids']=['0']
        plan=loop.plan_production(self.spec,self.batches,self.state,AS_OF)
        self.candidates.write_text('{"clips":[]}')
        media=self.candidates.parent/'clips/clip1/reel.mp4'
        with self.assertRaisesRegex(ValueError,'Candidates changed'):
            loop.seal_production(plan['plan_id'],media,'QA',self.state,AS_OF)

    def test_health_detects_queue_demand_and_stalled_production(self):
        db=self.root/'reels.db'
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE reels(channel_id, content_hash, trial_reel, status, scheduled_at, published_at, media_id)')
            for i in range(56):
                conn.execute('INSERT INTO reels VALUES(?,?,?,?,?,?,?)',('aibrief_jp',str(i),0,'scheduled',(AS_OF+timedelta(hours=i*6+1)).isoformat(),None,None))
        feedback=sf.build_feedback(report())
        self.assertEqual(loop.health(db,feedback,self.state,AS_OF)['next_action'],'BUFFER_HEALTHY')
        with sqlite3.connect(db) as conn:
            conn.execute('DELETE FROM reels')
        self.assertTrue(loop.health(db,feedback,self.state,AS_OF)['source_search_due'])
        loop.plan_production(self.spec,self.batches,self.state,AS_OF)
        health=loop.health(db,feedback,self.state,AS_OF+timedelta(days=8))
        self.assertFalse(health['source_search_due'])
        self.assertTrue(any(x.startswith('STALLED_PRODUCTION') for x in health['alerts']))

    def receipt(self, name, status, age_hours, trigger="scheduled", run_id=None):
        loop.save_immutable(self.state / "runs" / f"{name}.json", {
            "run_id": run_id or name, "trigger": trigger, "status": status,
            "at": (AS_OF - timedelta(hours=age_hours)).isoformat(), "detail": "test"})

    def test_weekly_deadline_and_grace(self):
        self.receipt("completed", "completed", 24)
        result = loop.run_health(self.state, AS_OF)
        self.assertEqual(result["alerts"], [])
        deadline = sf.timestamp(result["scheduled_completion_deadline"])
        self.assertEqual(deadline, AS_OF + timedelta(hours=156))
        self.assertEqual(loop.run_health(self.state, deadline)["alerts"], [])
        self.assertIn("SCHEDULED_LOOP_OVERDUE", loop.run_health(self.state, deadline + timedelta(seconds=1))["alerts"])

    def test_initial_monitoring_does_not_invent_success(self):
        result = loop.run_health(self.state, AS_OF)
        self.assertEqual(result["schedule_monitoring_status"], "NO_RUN_HISTORY")
        self.assertIsNone(result["scheduled_completion_deadline"])
        self.receipt("manual", "completed", 181, trigger="manual")
        result = loop.run_health(self.state, AS_OF)
        self.assertIsNone(result["last_completed_scheduled_run"])
        self.assertIn("NO_COMPLETED_SCHEDULED_RUN", result["alerts"])

    def test_open_runs_are_reconciled_by_identity_and_time(self):
        self.receipt("z-start", "started", 200, run_id="closed")
        self.receipt("a-end", "completed", 190, run_id="closed")
        self.receipt("stalled", "started", 169, trigger="manual")
        self.receipt("active", "started", 1)
        result = loop.run_health(self.state, AS_OF)
        self.assertEqual({e["run_id"] for e in result["open_runs"]}, {"stalled", "active"})
        self.assertIn("STALLED_RUN:manual:stalled", result["alerts"])
        self.assertFalse(any("active" in a or "closed" in a for a in result["alerts"]))

    def test_failed_run_remains_visible_until_scheduled_recovery(self):
        self.receipt("failed", "failed", 4)
        self.receipt("manual", "completed", 3, trigger="manual")
        self.assertIn("SCHEDULED_RUN_FAILED:failed", loop.run_health(self.state, AS_OF)["alerts"])
        self.receipt("recovered", "completed", 1)
        self.assertEqual(loop.run_health(self.state, AS_OF)["alerts"], [])

    def test_cli_persists_consistent_health_and_returns_failure_for_alerts(self):
        db = self.root / "reels.db"
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE reels(channel_id, content_hash, trial_reel, status, scheduled_at, published_at, media_id)')
        feedback = self.root / "feedback.json"
        feedback.write_text(json.dumps(sf.build_feedback(report())))
        real_health = loop.health
        for age, expected_exit in ((24, 0), (181, 1)):
            with self.subTest(age=age):
                for p in (self.state / "runs").glob("*.json"):
                    p.unlink()
                self.receipt("last", "completed", age)
                with patch.object(cli, "ROOT", self.root), patch.object(cli.loop, "health", side_effect=lambda db, feedback, state: real_health(db, feedback, state, AS_OF)), contextlib.redirect_stdout(io.StringIO()) as stdout:
                    code = cli.main(["--state", str(self.state), "health", "--db", str(db), "--feedback", str(feedback)])
                result = json.loads(stdout.getvalue())
                self.assertEqual(code, expected_exit)
                self.assertEqual(result, json.loads((self.root / "out/aibrief_learning_loop.health.json").read_text()))
                self.assertEqual(result["next_action"], "REPAIR" if expected_exit else "SOURCE")
                self.assertEqual(result["source_search_due"], not bool(expected_exit))
                self.assertEqual(result["status"], "NEEDS_ATTENTION" if expected_exit else "OK")


if __name__=='__main__':
    unittest.main()
