#!/usr/bin/env python3
"""Run with: python3 test_approve_all.py. With the real SDK: uv run --with "anthropic>=2.3,<3" python test_approve_all.py"""
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
TMP = tempfile.mkdtemp()
for name in [n for n in os.environ if n.startswith("APPROVE_ALL_") or n == "ANTHROPIC_API_KEY"]:
    del os.environ[name]
os.environ["APPROVE_ALL_HOME"] = TMP
os.environ["APPROVE_ALL_SETTINGS"] = f"{TMP}/settings.json"
os.environ["APPROVE_ALL_THREAD_TOOLS"] = "mcp__thread__post"
SOURCE = Path(__file__).with_name("approve-all")


def load_copy(env_text=None):
    """Load a copy of the script from a temp folder, so a real .env next to the original is never read."""
    folder = Path(tempfile.mkdtemp(dir=TMP))
    shutil.copy2(SOURCE, folder / "approve-all")
    if env_text is not None:
        (folder / ".env").write_text(env_text)
    loader = importlib.machinery.SourceFileLoader("approve_all", str(folder / "approve-all"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("approve_all", loader))
    loader.exec_module(module)
    return module, loader


aa, loader = load_copy()
REAL_ASK_YOU = aa.ask_you
aa.ask_you = lambda *args, **kwargs: None  # no dialogs on screen during tests


def human(text):
    return {"type": "user", "message": {"content": text}}


def said(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def tool(name, **data):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": data}]}}


def wrap(ask=None, actions=None):
    return tool("mcp__ccd_turn__wrap_up", recap="done", ask=ask, actions=actions)


def auto_reply(n=1, idle=0, ask_id="abc"):
    text = f"Stop hook feedback:\n[approve-all n={n} idle={idle} id={ask_id} | label]\nYes"
    return {"type": "user", "isMeta": True, "message": {"content": text}}


def decide(*rows):
    return aa.decide(aa.read_turn([human("start"), *rows]))


class Decisions(unittest.TestCase):
    def test_takes_first_button_message(self):
        d = decide(wrap("Enable auto-merge?", [{"label": "Enable", "message": "Yes, enable it."}, {"label": "No"}]))
        self.assertTrue(d["reply"].endswith("Yes, enable it."))

    def test_takes_first_button_label(self):
        d = decide(wrap("Which fix first?", [{"label": "Fix 2"}, {"label": "Fix 1"}]))
        self.assertIn('To "Which fix first?": Fix 2', d["reply"])

    def test_follows_recommendation_even_when_it_is_to_stop(self):
        d = decide(wrap("Keep going?", [{"label": "Stop here", "message": "Stop here."}, {"label": "Go on"}]))
        self.assertTrue(d["reply"].endswith("Stop here."))

    def test_app_button(self):
        d = decide(wrap("Open it?", [{"action": "create_pr"}]))
        self.assertTrue(d["reply"].endswith("Yes, go ahead: create pr."))

    def test_archive_button_is_not_a_question(self):
        self.assertIsNone(decide(wrap("Archive this?", [{"action": "archive"}])))

    def test_finished_turn_gets_no_reply(self):
        self.assertIsNone(decide(said("All done, tests pass."), wrap()))

    def test_no_action_needed_gets_no_reply(self):
        self.assertIsNone(decide(said("PR merged. Nothing else is pending.")))

    def test_question_with_recommendation(self):
        d = decide(said("I recommend the first name. Should I open the PR?"))
        self.assertTrue(d["reply"].endswith(aa.REPLY_RECOMMENDED))

    def test_wrap_up_without_question_means_nothing_pending(self):
        self.assertIsNone(decide(wrap(), said('It asked "Should I fix the config?" and I answered. Should I?')))

    def test_question_quoted_in_earlier_paragraph(self):
        self.assertIsNone(decide(said('It asked "Should I fix the config?" and got an answer.\n\nAll finished.')))

    def test_recommendation_heading(self):
        d = decide(said("**Recommendation:** keep the old branch. Say so and that is your call. Should I?"))
        self.assertTrue(d["reply"].endswith(aa.REPLY_RECOMMENDED))

    def test_plain_question(self):
        self.assertTrue(decide(said("Red or blue?"))["reply"].endswith(aa.REPLY_DEFAULT))

    def test_relay_is_sent_back(self):
        d = decide(said("Please paste this into the other session so it can continue."))
        self.assertTrue(d["reply"].endswith(aa.REPLY_RELAY))

    def test_question_in_project_thread_post(self):
        d = decide(tool("mcp__thread__post", text="The build is ready. That one is your call."), said("Posted."))
        self.assertIn("reply", d)

    def test_note_to_coordinator_is_not_a_question_for_you(self):
        d = decide(tool("mcp__thread__post", visibility="sent_to_shared_agent",
                        message="Should I take this one?"), said("Sent."))
        self.assertIsNone(d)

    def test_older_question_already_answered_is_ignored(self):
        rows = [human("start"), wrap("Merge it?", [{"label": "Yes"}]), human("yes"), said("Merged.")]
        self.assertIsNone(aa.decide(aa.read_turn(rows)))


class Guards(unittest.TestCase):
    def test_left_for_you(self):
        for text, reason in [("Should I deploy this to production?", "production"),
                             ("What's the API key?", "secret"),
                             ("Paste the key you saved, then reply with the result.", "hands-on"),
                             ("Tell me when you're done and I'll delete the snapshot.", "hands-on"),
                             ("Can I force-push over main?", "destructive"),
                             ("Should I drop table users?", "destructive"),
                             ("Should I buy 10 shares?", "money"),
                             ("Should I send the email to customers?", "outbound")]:
            self.assertEqual(decide(said(text)).get("reason"), reason, text)

    FILLER = " This is a long explanation of the plan." * 40

    def test_an_action_far_above_the_question_is_still_guarded(self):
        d = decide(said(f"I propose to run rm -rf /data.{self.FILLER}\n\nShould I proceed?"))
        self.assertEqual(d["reason"], "destructive, earlier in its message")

    def test_an_action_in_the_recap_is_guarded(self):
        d = decide(tool("mcp__ccd_turn__wrap_up", recap="Ready to drop table users.", ask="Go ahead?",
                        actions=[{"label": "Yes", "message": "Yes."}]))
        self.assertEqual(d["reason"], "destructive, earlier in its message")

    def test_the_guard_on_earlier_text_also_binds_the_reviewer(self):
        with mock.patch.object(aa, "can_review", lambda: True), mock.patch.object(
                aa, "ask_model", lambda prompt: verdict(message="Yes, go ahead.")):
            d = decide(said(f"I'll deploy to production.{self.FILLER}\n\nShould I proceed?"))
        self.assertEqual(d["reason"], "production, earlier in its message")

    def test_a_secret_mentioned_earlier_does_not_stop_it(self):
        self.assertIn("reply", decide(said(f"The API key is in .env.{self.FILLER}\n\nShould I open the PR?")))

    def test_mentioning_prod_is_fine(self):
        self.assertIn("reply", decide(said("Prod wasn't touched or restarted. Want me to keep the preview up?")))

    def test_force_push_on_a_branch_is_fine(self):
        self.assertIn("reply", decide(said("Should I force-push my feature branch?")))


class Limits(unittest.TestCase):
    def test_repeat_question_stops(self):
        first = decide(said("Red or blue?"))
        ask_id = aa.TAG.search(first["reply"])[3]
        self.assertIn("asked again", decide(auto_reply(ask_id=ask_id), said("Red or blue?"))["reason"])

    def test_reply_cap(self):
        self.assertIn("replies", decide(auto_reply(n=aa.MAX_REPLIES), tool("Bash"), said("Red?"))["reason"])

    def test_idle_cap(self):
        self.assertIn("no work", decide(auto_reply(idle=aa.MAX_IDLE - 1), said("Red?"))["reason"])

    def test_work_resets_idle(self):
        d = decide(auto_reply(idle=2), tool("Bash"), said("Red?"))
        self.assertEqual(aa.TAG.search(d["reply"])[2], "0")

    def test_counts_continue(self):
        d = decide(auto_reply(n=4), tool("Bash"), said("Red?"))
        self.assertEqual(aa.TAG.search(d["reply"])[1], "5")

    def test_no_message_or_tag_in_reach_hands_back(self):
        self.assertIn("count replies", aa.decide(aa.read_turn([said("Red?")]))["reason"])

    def big_gap(self, *rows):
        result = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 4_100_000}]}}
        path = Path(tempfile.mkdtemp(dir=TMP), "t.jsonl")
        path.write_text("\n".join(json.dumps(r) for r in [human("start"), *rows, result, tool("Bash"), said("Red?")]) + "\n")
        return aa.on_stop({"transcript_path": str(path), "session_id": "s", "cwd": "/x"})

    def test_a_large_tool_result_cannot_reset_the_reply_count(self):
        self.assertIsNone(self.big_gap(auto_reply(n=aa.MAX_REPLIES)))
        self.assertIn("hit 25 replies", aa.LOG.read_text().splitlines()[-1])

    def test_a_large_tool_result_still_gets_the_next_count(self):
        self.assertEqual(aa.TAG.search(self.big_gap(auto_reply(n=4))["reason"])[1], "5")

    def test_a_large_first_turn_is_still_answered(self):
        self.assertEqual(aa.TAG.search(self.big_gap()["reason"])[1], "1")

    def test_when_the_count_is_out_of_reach_it_hands_back(self):
        with mock.patch.object(aa, "FAR_BACK", 4_000_000):
            self.assertIsNone(self.big_gap(auto_reply(n=aa.MAX_REPLIES)))
        self.assertIn("count replies", aa.LOG.read_text().splitlines()[-1])

    def test_tool_output_quoting_a_tag_does_not_count(self):
        quoted = {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "[approve-all n=25 idle=2 id=abc | x]"}]}}
        d = decide(tool("Bash"), quoted, said("Red?"))
        self.assertEqual(aa.TAG.search(d["reply"])[1], "1")


class Hooks(unittest.TestCase):
    def test_ask_takes_recommended(self):
        q = {"question": "Which?", "options": [{"label": "red"}, {"label": "blue (Recommended)"}]}
        out = aa.on_ask({"tool_input": {"questions": [q]}})["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["answers"], {"Which?": "blue (Recommended)"})

    def test_ask_defaults_to_first(self):
        q = {"question": "Which?", "options": [{"label": "red"}, {"label": "blue"}]}
        out = aa.on_ask({"tool_input": {"questions": [q]}})["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["answers"], {"Which?": "red"})

    def test_single_select_gets_exactly_one_real_option(self):
        q = {"question": "Which?", "options": [{"label": "Quick (Recommended)"}, {"label": "Thorough (not recommended)"},
                                               {"label": "Other (Recommended)"}]}
        out = aa.on_ask({"tool_input": {"questions": [q]}})["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["answers"], {"Which?": "Quick (Recommended)"})

    def test_multi_select_takes_every_recommended_option(self):
        q = {"question": "Which?", "multiSelect": True,
             "options": [{"label": "A (Recommended)"}, {"label": "B (not recommended)"}, {"label": "C (Recommended)"}]}
        out = aa.on_ask({"tool_input": {"questions": [q]}})["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["answers"], {"Which?": "A (Recommended), C (Recommended)"})

    def test_never_defaults_to_an_option_marked_not_recommended(self):
        q = {"question": "Which?", "options": [{"label": "A (not recommended)"}, {"label": "B"}]}
        out = aa.on_ask({"tool_input": {"questions": [q]}})["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["answers"], {"Which?": "B"})

    def test_ask_guarded(self):
        q = {"question": "Deploy to production?", "options": [{"label": "Yes (Recommended)"}]}
        self.assertIsNone(aa.on_ask({"tool_input": {"questions": [q]}}))

    def test_permission_allowed(self):
        out = aa.on_permission({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "allow")

    def test_permission_guarded(self):
        self.assertIsNone(aa.on_permission({"tool_name": "Bash", "tool_input": {"command": "rm -rf /data"}}))

    def test_guard_reads_the_whole_permission_input(self):
        command = f"echo {'x' * 100_000}; rm -rf /data"
        self.assertIsNone(aa.on_permission({"tool_name": "Bash", "tool_input": {"command": command}}))


def verdict(action="reply", index=-1, message="", reason="because"):
    return {"action": action, "option_index": index, "message": message, "reason": reason}


class Review(unittest.TestCase):
    def setUp(self):
        for patcher in (mock.patch.object(aa, "can_review", lambda: True),):
            patcher.start()
            self.addCleanup(patcher.stop)

    def answer(self, result):
        patcher = mock.patch.object(aa, "ask_model", side_effect=[result] if not callable(result) else result)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_rules_out_a_quoted_question(self):
        self.answer(verdict("nothing_pending"))
        self.assertIsNone(decide(said("Red or blue?")))

    def test_can_override_the_first_button(self):
        self.answer(verdict(index=1, reason="the second matches the request"))
        d = decide(wrap("Which?", [{"label": "A", "message": "Do A."}, {"label": "B", "message": "Do B."}]))
        self.assertTrue(d["reply"].endswith("Do B."))
        self.assertEqual(d["why"], "the second matches the request")

    def test_writes_a_reply_when_there_are_no_buttons(self):
        self.answer(verdict(message="Use blue, it matches the theme."))
        self.assertTrue(decide(said("Red or blue?"))["reply"].endswith("Use blue, it matches the theme."))

    def test_leaves_it_for_you_with_a_reason(self):
        self.answer(verdict("leave_for_user", reason="The debug port is still open; close it."))
        self.assertEqual(decide(said("Want me to leave the debug port open?"))["reason"], "The debug port is still open; close it.")

    def test_never_lifts_the_guard(self):
        self.answer(verdict(message="Go ahead and deploy."))
        self.assertEqual(decide(said("Should I deploy to production?"))["reason"], "production")

    def test_guard_covers_what_the_model_wrote(self):
        self.answer(verdict(message="Yes, then drop table users."))
        self.assertEqual(decide(said("Red or blue?"))["reason"], "destructive")

    def test_sees_a_question_after_a_wrap_up_with_none(self):
        model = self.answer(verdict(message="Keep it on."))
        self.assertTrue(decide(wrap(), said("Want me to keep it on?"))["reply"].endswith("Keep it on."))
        self.assertEqual(model.call_count, 1)

    def test_finished_turn_costs_nothing(self):
        model = self.answer(verdict())
        self.assertIsNone(decide(said("All done, tests pass."), wrap()))
        self.assertEqual(model.call_count, 0)

    def test_failure_falls_back_to_rules(self):
        self.answer(None)
        d = decide(wrap("Merge it?", [{"label": "Yes", "message": "Yes, merge."}]))
        self.assertTrue(d["reply"].endswith("Yes, merge."))

    def test_failure_on_a_loose_match_stays_quiet(self):
        self.answer(None)
        self.assertIsNone(decide(wrap(), said("Want me to keep it on?")))

    def test_bad_option_falls_back_to_rules(self):
        self.answer(verdict(index=9))
        d = decide(wrap("Merge it?", [{"label": "Yes", "message": "Yes, merge."}]))
        self.assertTrue(d["reply"].endswith("Yes, merge."))

    def test_limits_still_apply(self):
        self.answer(verdict(message="Blue."))
        self.assertIn("replies", decide(auto_reply(n=aa.MAX_REPLIES), tool("Bash"), said("Red?"))["reason"])

    def test_card_uses_the_reviewer(self):
        self.answer(verdict(index=0))
        q = {"question": "Which?", "options": [{"label": "red"}, {"label": "blue (Recommended)"}]}
        out = aa.on_ask({"tool_input": {"questions": [q]}})["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["answers"], {"Which?": "red"})

    def test_card_reviewer_reads_the_descriptions(self):
        model = self.answer(verdict(index=1))
        q = {"question": "Which layout?", "options": [{"label": "Option A (Recommended)", "description": "One shared table"},
                                                      {"label": "Option B", "description": "A table per tenant"},
                                                      {"label": "Option C"}]}
        out = aa.on_ask({"tool_input": {"questions": [q]}})["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["answers"], {"Which layout?": "Option B"})
        prompt = model.call_args[0][0]
        for expected in ("0: Option A (Recommended): One shared table", "1: Option B: A table per tenant", "2: Option C\n"):
            self.assertIn(expected, prompt)

    def test_card_left_for_you(self):
        self.answer(verdict("leave_for_user"))
        q = {"question": "Which?", "options": [{"label": "red"}]}
        self.assertIsNone(aa.on_ask({"tool_input": {"questions": [q]}}))

    def test_permission_left_for_you(self):
        self.answer(verdict("leave_for_user"))
        self.assertIsNone(aa.on_permission({"tool_name": "Bash", "tool_input": {"command": "ls"}}))

    def test_reviewer_is_told_when_a_huge_input_is_cut(self):
        model = self.answer(verdict("reply"))
        command = f"START {'x' * 100_000} END"
        aa.on_permission({"tool_name": "Bash", "tool_input": {"command": command}})
        prompt = model.call_args[0][0]
        for expected in ("START", "END", "characters not shown"):
            self.assertIn(expected, prompt)
        self.assertLess(len(prompt), 45_000)

    def test_reviewer_sees_all_of_a_normal_input(self):
        model = self.answer(verdict("reply"))
        command = f"START {'x' * 30_000} END"
        aa.on_permission({"tool_name": "Bash", "tool_input": {"command": command}})
        self.assertIn(command, model.call_args[0][0])
        self.assertNotIn("not shown", model.call_args[0][0])

    def test_permission_allowed_when_review_fails(self):
        self.answer(None)
        out = aa.on_permission({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "allow")

    def test_prompt_has_the_goal_and_recent_work(self):
        rows = [human("Port the API"), tool("Bash", command="make test"), said("Tests pass. Merge?")]
        prompt = aa.build_prompt("Merge?", ["Yes"], rows, "/x")
        for expected in ("Port the API", "make test", "Tests pass", "0: Yes", "/x"):
            self.assertIn(expected, prompt)


class Dialog(unittest.TestCase):
    def setUp(self):
        self.shown = []
        self.opened = mock.Mock()
        for patcher in (mock.patch.object(aa, "ask_you", REAL_ASK_YOU), mock.patch.object(aa, "open_session", self.opened),
                        mock.patch.object(aa.shutil, "which", lambda name: "/usr/bin/osascript")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.transcript = f"{TMP}/dialog.jsonl"

    def pick(self, *picks):
        script = iter(picks)

        def fake(_script, args, _abandoned):
            self.shown.append(args)
            return next(script)

        patcher = mock.patch.object(aa, "run_dialog", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stop(self, *rows):
        Path(self.transcript).write_text("\n".join(json.dumps(r) for r in [human("start"), *rows]) + "\n")
        return aa.on_stop({"transcript_path": self.transcript, "session_id": "s", "cwd": "/x/project"})

    def test_sends_the_button_you_picked(self):
        self.pick("Ship it")
        out = self.stop(wrap("Should I deploy to production?", [{"label": "Ship it", "message": "Yes, deploy now."},
                                                                {"label": "Wait", "message": "Not yet."}]))
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].endswith("Yes, deploy now."))
        self.assertIn(aa.PICKED, out["reason"])
        self.assertEqual(aa.TAG.search(out["reason"])[1], "0")
        self.assertEqual(self.shown[0][2:4], ["Ship it", "Wait"])

    def test_later_leaves_it_waiting(self):
        self.pick(None)
        self.assertIsNone(self.stop(said("Should I deploy to production?")))

    def test_generic_answers_when_the_session_offers_none(self):
        self.pick("Yes, go ahead")
        out = self.stop(said("Should I deploy to production?"))
        self.assertTrue(out["reason"].endswith("Yes, go ahead"))

    def test_own_answer(self):
        self.pick(aa.TYPE_ANSWER, "Only the staging one")
        self.assertTrue(self.stop(said("Should I deploy to production?"))["reason"].endswith("Only the staging one"))

    def test_open_session(self):
        self.pick(aa.OPEN_SESSION)
        self.assertIsNone(self.stop(said("Should I deploy to production?")))
        self.opened.assert_called_once()

    def test_no_typing_when_a_secret_is_asked_for(self):
        self.pick(None)
        self.stop(said("What's the API key?"))
        self.assertNotIn(aa.TYPE_ANSWER, self.shown[0])
        self.assertIn(aa.ANSWERS_DONE[0], self.shown[0])

    def test_offers_the_reviewers_answers(self):
        self.pick("Leave it open")
        with mock.patch.object(aa, "can_review", lambda: True), mock.patch.object(
                aa, "ask_model", lambda prompt: {**verdict("leave_for_user"), "choices": ["Leave it open", "I'll close it"]}):
            out = self.stop(said("The debug port is open. Want me to leave it?"))
        self.assertEqual(self.shown[0][2:4], ["Leave it open", "I'll close it"])
        self.assertTrue(out["reason"].endswith("Leave it open"))

    def test_permission_answers(self):
        event = {"tool_name": "Bash", "tool_input": {"command": "rm -rf /data"}}
        self.pick("Allow")
        self.assertEqual(aa.on_permission(event)["hookSpecificOutput"]["decision"]["behavior"], "allow")
        self.pick("Deny")
        self.assertEqual(aa.on_permission(event)["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.pick(None)
        self.assertIsNone(aa.on_permission(event))

    def test_card_answer(self):
        self.pick("Yes (Recommended)")
        q = {"question": "Deploy to production?", "options": [{"label": "Yes (Recommended)"}, {"label": "No"}]}
        out = aa.on_ask({"tool_input": {"questions": [q]}})["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["answers"], {"Deploy to production?": "Yes (Recommended)"})

    def test_a_queued_message_counts_but_a_dequeue_does_not(self):
        path = f"{TMP}/queue.jsonl"
        Path(path).write_text('{"type":"queue-operation","operation":"enqueue"}\n')
        start = os.path.getsize(path)
        self.assertFalse(aa.queued_since(path, start))
        with open(path, "a") as f:
            f.write('{"type":"queue-operation","operation":"dequeue"}\n')
        self.assertFalse(aa.queued_since(path, start))
        with open(path, "a") as f:
            f.write('{"type":"queue-operation","operation":"enqueue"}\n')
        self.assertTrue(aa.queued_since(path, start))

    @unittest.skipUnless(shutil.which("osascript"), "needs macOS")
    def test_abandoning_closes_the_dialog(self):
        started = time.time()
        self.assertIsNone(aa.run_dialog("delay 30", [], lambda: True))
        self.assertLess(time.time() - started, 5)


class Budget(unittest.TestCase):
    def setUp(self):
        for path in (aa.KEY_FILE, aa.SPEND):
            path.unlink(missing_ok=True)
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in (aa.KEY_FILE, aa.SPEND)])

    def test_no_key_means_no_review(self):
        self.assertFalse(aa.can_review())

    def test_set_key_is_private(self):
        with mock.patch("sys.stdin", io.StringIO("sk-test\n")):
            aa.cmd_set_key(None)
        self.assertEqual(aa.KEY_FILE.read_text(), "sk-test")
        self.assertEqual(aa.KEY_FILE.stat().st_mode & 0o777, 0o600)
        self.assertTrue(aa.can_review())

    def test_budget_stops_reviews(self):
        aa.KEY_FILE.write_text("sk-test")
        aa.SPEND.write_text(json.dumps({"cost": aa.BUDGET}) + "\n")
        self.assertFalse(aa.can_review())

    def test_cost_estimate(self):
        with mock.patch.object(aa, "MODEL", "claude-opus-5-5"):
            aa.record_spend(types.SimpleNamespace(input_tokens=1000, output_tokens=100))
        self.assertAlmostEqual(aa.spent(), 0.006)


@unittest.skipUnless(importlib.util.find_spec("anthropic"), "run through uv for the real SDK")
class RealSdk(unittest.TestCase):
    """Drives the hook through the real SDK against a local stand-in for the API."""

    def setUp(self):
        test = self
        test.requests, test.status = [], 200

        class Stub(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                test.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                text = json.dumps(verdict(index=1, reason="B fits"))
                reply = {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
                         "content": [{"type": "text", "text": text}], "stop_reason": test.stop, "stop_sequence": None,
                         "usage": {"input_tokens": 1000, "output_tokens": 100}}
                self.send_response(test.status)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode())

            def log_message(self, *args):
                pass

        test.stop = "end_turn"
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.home = tempfile.mkdtemp()
        rows = [human("Port the API"), wrap("Which?", [{"label": "A", "message": "Do A."},
                                                       {"label": "B", "message": "Do B."}])]
        self.transcript = f"{self.home}/t.jsonl"
        Path(self.transcript).write_text("\n".join(json.dumps(r) for r in rows))

    def hook(self, **extra):
        env = {**os.environ, "APPROVE_ALL_HOME": self.home, "APPROVE_ALL_ENTRYPOINTS": "*", "ANTHROPIC_API_KEY": "sk-test",
               "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{self.server.server_port}", **extra}
        event = {"hook_event_name": "Stop", "transcript_path": self.transcript, "session_id": "s", "cwd": "/x"}
        done = subprocess.run([sys.executable, loader.path, "hook"], input=json.dumps(event), text=True,
                              capture_output=True, env=env, timeout=120)
        self.assertEqual(done.stderr, "")
        return json.loads(done.stdout)

    def test_review_reaches_the_session(self):
        self.assertTrue(self.hook()["reason"].endswith("Do B."))
        path, headers, body = self.requests[0]
        self.assertTrue(path.startswith("/v1/messages"))
        self.assertEqual(headers["authorization"], "Bearer sk-test")
        self.assertIn("server-side-fallback-2026-07-01", headers["anthropic-beta"])
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(body["output_config"]["effort"], "medium")
        self.assertIn("Port the API", body["messages"][0]["content"])
        spend = json.loads(Path(self.home, "spend.jsonl").read_text())
        self.assertAlmostEqual(spend["cost"], 0.006)

    def test_api_down_falls_back_to_first_button(self):
        self.status = 500
        self.assertTrue(self.hook()["reason"].endswith("Do A."))
        self.assertIn("review failed", Path(self.home, "log.jsonl").read_text())

    def test_refusal_falls_back_to_first_button(self):
        self.stop = "refusal"
        self.assertTrue(self.hook()["reason"].endswith("Do A."))

    def test_unknown_model_gets_no_fallback_and_is_estimated_at_opus_rates(self):
        self.assertTrue(self.hook(APPROVE_ALL_MODEL="claude-future-9")["reason"].endswith("Do B."))
        _, headers, body = self.requests[0]
        self.assertEqual(body["model"], "claude-future-9")
        self.assertNotIn("fallbacks", body)
        self.assertNotIn("anthropic-beta", headers)
        self.assertAlmostEqual(json.loads(Path(self.home, "spend.jsonl").read_text())["cost"], 0.006)


def fake_sdk(create):
    messages = types.SimpleNamespace(create=create)
    client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=messages))
    return types.SimpleNamespace(Anthropic=lambda **kwargs: client)


def fake_response(text, stop="end_turn"):
    return types.SimpleNamespace(stop_reason=stop, content=[types.SimpleNamespace(type="text", text=text)],
                                 usage=types.SimpleNamespace(input_tokens=1000, output_tokens=100))


class EnvFile(unittest.TestCase):
    def read(self, data):
        path = Path(tempfile.mkdtemp(dir=TMP), ".env")
        path.write_bytes(data)
        return aa.read_env_file(path)

    def test_reads_the_usual_forms(self):
        self.assertEqual(self.read(b"""\xef\xbb\xbf# a comment
A=1
  B = two
export C=3
D="quoted # kept" # gone
E='single'
F=plain # gone
G=
"""), {"A": "1", "B": "two", "C": "3", "D": "quoted # kept", "E": "single", "F": "plain", "G": ""})

    def test_skips_what_it_cannot_read(self):
        self.assertEqual(self.read(b"no equals\n=novalue\n1BAD=x\n# A=1\n\xff\xfe\x00garbage\nGOOD=yes\n"), {"GOOD": "yes"})

    def test_missing_or_a_folder_means_no_settings(self):
        self.assertEqual(aa.read_env_file(f"{TMP}/nope"), {})
        self.assertEqual(aa.read_env_file(TMP), {})

    def test_settings_come_from_the_file(self):
        module, _ = load_copy("APPROVE_ALL_MODEL=claude-haiku-5-5\nAPPROVE_ALL_BUDGET_USD=5\nAPPROVE_ALL_MAX_REPLIES=2\n")
        self.assertEqual((module.MODEL, module.BUDGET, module.MAX_REPLIES), ("claude-haiku-5-5", 5.0, 2))

    def test_the_model_defaults_to_opus(self):
        self.assertEqual(load_copy()[0].MODEL, "claude-opus-5-5")
        self.assertEqual(load_copy("APPROVE_ALL_MODEL=\n")[0].MODEL, "claude-opus-5-5")

    def test_the_environment_beats_the_file(self):
        with mock.patch.dict(os.environ, {"APPROVE_ALL_MODEL": "claude-sonnet-5-5"}):
            self.assertEqual(load_copy("APPROVE_ALL_MODEL=claude-haiku-5-5\n")[0].MODEL, "claude-sonnet-5-5")

    def test_a_bad_number_falls_back_to_the_default(self):
        module, _ = load_copy("APPROVE_ALL_BUDGET_USD=lots\nAPPROVE_ALL_MAX_REPLIES=2.5\nAPPROVE_ALL_ANSWER_WAIT=\n")
        self.assertEqual((module.BUDGET, module.MAX_REPLIES, module.ANSWER_WAIT), (150.0, 25, 3600))

    def test_the_file_cannot_lift_the_guard(self):
        self.assertIn("production", load_copy("APPROVE_ALL_NO_GUARD=1\n")[0].NEVER)

    def test_a_broken_file_still_loads(self):
        self.assertEqual(load_copy("\x00\x01 not=a=setting ===\nAPPROVE_ALL_EFFORT\n")[0].EFFORT, "medium")


class ApiKey(unittest.TestCase):
    def setUp(self):
        aa.KEY_FILE.unlink(missing_ok=True)
        self.addCleanup(lambda: aa.KEY_FILE.unlink(missing_ok=True))
        for patcher in (mock.patch.dict(aa.ENV_FILE, clear=True), mock.patch.dict(os.environ)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_env_file_then_key_file_then_environment(self):
        os.environ["ANTHROPIC_API_KEY"] = "from-environment"
        self.assertEqual(aa.api_key(), "from-environment")
        aa.KEY_FILE.write_text("from-set-key\n")
        self.assertEqual(aa.api_key(), "from-set-key")
        aa.ENV_FILE["ANTHROPIC_API_KEY"] = "from-env-file"
        self.assertEqual(aa.api_key(), "from-env-file")

    def test_empty_keys_mean_rules_only(self):
        aa.ENV_FILE["ANTHROPIC_API_KEY"] = ""
        aa.KEY_FILE.write_text("  \n")
        os.environ["ANTHROPIC_API_KEY"] = " "
        self.assertEqual(aa.api_key(), "")
        self.assertFalse(aa.can_review())

    def test_unreadable_key_file_is_skipped(self):
        os.environ["ANTHROPIC_API_KEY"] = "from-environment"
        aa.KEY_FILE.write_bytes(b"\xff\xfe")
        self.assertEqual(aa.api_key(), "from-environment")
        aa.KEY_FILE.unlink()
        aa.KEY_FILE.mkdir()
        self.addCleanup(aa.KEY_FILE.rmdir)
        self.assertEqual(aa.api_key(), "from-environment")


class Failures(unittest.TestCase):
    """Nothing here may raise or hold a session up. Failures fall back to the rules and get logged."""

    def setUp(self):
        aa.LOG.unlink(missing_ok=True)
        aa.SPEND.unlink(missing_ok=True)
        self.addCleanup(lambda: aa.SPEND.unlink(missing_ok=True))
        self.folder = tempfile.mkdtemp(dir=TMP)
        self.transcript = f"{self.folder}/t.jsonl"

    def logged(self):
        return aa.LOG.read_text() if aa.LOG.exists() else ""

    def stop(self, **event):
        return aa.on_stop({"transcript_path": self.transcript, "session_id": "s", "cwd": "/x", **event})

    def test_log_never_raises(self):
        blocker = Path(self.folder, "file")
        blocker.write_text("")
        with mock.patch.object(aa, "STATE", blocker), mock.patch.object(aa, "LOG", blocker / "log.jsonl"):
            aa.log(event="error", error="x")
        aa.log(event="error", error=object())

    def run_hook(self, text, **extra):
        env = {**os.environ, "APPROVE_ALL_ENTRYPOINTS": "*", **extra}
        return subprocess.run([sys.executable, loader.path, "hook"], input=text, text=True, capture_output=True,
                              env=env, timeout=60)

    def test_hook_with_nowhere_to_log_exits_cleanly(self):
        blocker = Path(self.folder, "file")
        blocker.write_text("")
        done = self.run_hook("not json", APPROVE_ALL_HOME=str(blocker))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_hook_logs_a_bad_event(self):
        home = Path(self.folder, "home")
        done = self.run_hook("not json", APPROVE_ALL_HOME=str(home))
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        self.assertIn("JSONDecodeError", (home / "log.jsonl").read_text())

    def test_hook_answers_by_the_rules_with_a_broken_env_file(self):
        module, copy = load_copy("\xff garbage\nAPPROVE_ALL_MAX_REPLIES=lots\nANTHROPIC_API_KEY=\n")
        Path(self.transcript).write_text("\n".join(json.dumps(r) for r in [human("start"), said("Red or blue?")]))
        event = {"hook_event_name": "Stop", "transcript_path": self.transcript, "session_id": "s", "cwd": "/x"}
        done = subprocess.run([sys.executable, copy.path, "hook"], input=json.dumps(event), text=True, capture_output=True,
                              env={**os.environ, "APPROVE_ALL_ENTRYPOINTS": "*"}, timeout=60)
        self.assertEqual(done.returncode, 0)
        self.assertTrue(json.loads(done.stdout)["reason"].endswith(aa.REPLY_DEFAULT))

    def test_corrupt_ledger_lines_are_skipped(self):
        aa.SPEND.write_bytes(b'not json\n[1, 2]\n42\n{"cost": "abc"}\n{"no": 1}\n{"cost": -5}\n{"cost": NaN}\n'
                             b'{"cost": Infinity}\n\xff\xfe\n{"cost": 1.5}\n')
        self.assertAlmostEqual(aa.spent(), 1.5)

    def test_a_ledger_we_cannot_use_stops_reviews(self):
        with mock.patch.object(aa, "SPEND", Path(self.folder)), mock.patch.dict(aa.ENV_FILE, {"ANTHROPIC_API_KEY": "k"}):
            self.assertEqual(aa.spent(), float("inf"))
            self.assertFalse(aa.can_review())

    def test_no_paid_review_when_the_ledger_rejects_writes(self):
        create = mock.Mock(return_value=fake_response(json.dumps(verdict())))
        soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        with mock.patch.dict(sys.modules, {"anthropic": fake_sdk(create)}):
            resource.setrlimit(resource.RLIMIT_FSIZE, (0, hard))
            try:
                self.assertIsNone(aa.ask_model("x"))
            finally:
                resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        create.assert_not_called()

    def test_check_works_on_a_fresh_install(self):
        state = Path(self.folder, "new", "state")
        with mock.patch.object(aa, "STATE", state), mock.patch.object(aa, "SPEND", state / "spend.jsonl"), mock.patch.object(
                aa, "LOG", state / "log.jsonl"), mock.patch.dict(aa.ENV_FILE, {"ANTHROPIC_API_KEY": "k"}), mock.patch.dict(
                sys.modules, {"anthropic": fake_sdk(lambda **_: fake_response(json.dumps(verdict())))}), mock.patch(
                "sys.stdout", io.StringIO()) as out:
            aa.cmd_check(None)
        self.assertIn("spent so far: $0.0060", out.getvalue())

    def test_the_write_check_leaves_the_total_alone(self):
        aa.SPEND.write_text('{"cost": 1.5}\n')
        aa.check_ledger_writes()
        self.assertAlmostEqual(aa.spent(), 1.5)

    def test_missing_sdk_falls_back(self):
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            self.assertIsNone(aa.ask_model("x"))
        self.assertIn("review failed", self.logged())

    def test_api_error_falls_back(self):
        def boom(**_):
            raise RuntimeError("500")
        with mock.patch.dict(sys.modules, {"anthropic": fake_sdk(boom)}):
            self.assertIsNone(aa.ask_model("x"))
        self.assertIn("500", self.logged())

    def test_bad_verdicts_fall_back(self):
        for text in ("not json", "[]", '{"action": "reply"}', json.dumps({**verdict(), "option_index": "1"})):
            with mock.patch.dict(sys.modules, {"anthropic": fake_sdk(lambda **_: fake_response(text))}):
                self.assertIsNone(aa.ask_model("x"), text)

    def test_only_known_models_get_the_refusal_fallback(self):
        sent = []

        def create(**kwargs):
            sent.append(kwargs)
            return fake_response(json.dumps({**verdict(), "choices": ["a", 1]}))

        with mock.patch.dict(sys.modules, {"anthropic": fake_sdk(create)}):
            self.assertEqual(aa.ask_model("x")["choices"], ["a"])
            with mock.patch.object(aa, "MODEL", "claude-haiku-5-5"):
                aa.ask_model("x")
        self.assertEqual(sent[0]["fallbacks"], "default")
        self.assertNotIn("fallbacks", sent[1])

    def test_unknown_model_cost_falls_back_to_opus_rates(self):
        with mock.patch.object(aa, "MODEL", "claude-future-9"):
            aa.record_spend(types.SimpleNamespace(input_tokens=1000, output_tokens=100))
        self.assertAlmostEqual(aa.spent(), 0.006)

    def test_unreadable_transcript_leaves_the_session_alone(self):
        os.mkdir(self.transcript)
        self.assertIsNone(self.stop(last_assistant_message="Red or blue?"))
        self.assertIn("can't read transcript", self.logged())

    def test_missing_transcript_leaves_the_session_alone(self):
        self.assertIsNone(self.stop(last_assistant_message="Red or blue?"))

    def test_empty_transcript_cannot_dodge_the_loop_limits(self):
        Path(self.transcript).write_text("")
        self.assertIsNone(self.stop(last_assistant_message="Red or blue?"))

    def test_corrupt_rows_are_skipped(self):
        bad = ['42', '"text"', '[1]', 'not json', '{"type": "assistant", "message": "nope"}',
               '{"type": "assistant", "message": {"content": 7}}',
               '{"type": "assistant", "message": {"content": [{"type": "text", "text": 5}, '
               '{"type": "tool_use", "name": 9, "input": 3}, "str"]}}']
        Path(self.transcript).write_text("\n".join([json.dumps(human("start")), *bad, json.dumps(said("Red or blue?"))]))
        self.assertTrue(self.stop()["reason"].endswith(aa.REPLY_DEFAULT))

    def test_cards_and_permissions_still_use_the_rules_without_a_transcript(self):
        os.mkdir(self.transcript)
        q = {"question": "Which?", "options": [{"label": "red"}]}
        out = aa.on_ask({"tool_input": {"questions": [q]}, "transcript_path": self.transcript})["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"]["answers"], {"Which?": "red"})
        out = aa.on_permission({"tool_name": "Bash", "tool_input": {"command": "ls"}, "transcript_path": self.transcript})
        self.assertEqual(out["hookSpecificOutput"]["decision"]["behavior"], "allow")

    def test_no_osascript_means_no_dialog(self):
        Path(self.transcript).write_text("\n".join(json.dumps(r) for r in [human("start"), said("Deploy to production?")]))
        with mock.patch.object(aa, "ask_you", REAL_ASK_YOU), mock.patch.object(aa.shutil, "which", lambda name: None):
            self.assertIsNone(self.stop())
        self.assertIn("left for you", self.logged())

    def test_a_failing_dialog_leaves_it_with_the_session(self):
        for error in (OSError("gone"), ValueError("embedded null byte")):
            with mock.patch.object(aa.shutil, "which", lambda name: "/usr/bin/osascript"), mock.patch.object(
                    aa.subprocess, "Popen", side_effect=error):
                self.assertIsNone(REAL_ASK_YOU({"cwd": "/x"}, "why", "ask", [("Yes", "Yes")]))
        self.assertIn("dialog failed", self.logged())

    def test_opening_a_session_never_raises(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_HOST_SESSION_ID": "h"}), mock.patch.object(
                aa.subprocess, "run", side_effect=OSError("no open here")):
            aa.open_session()

    def test_queue_check_survives_an_unreadable_transcript(self):
        self.assertFalse(aa.queued_since(self.folder, 0))
        self.assertFalse(aa.queued_since(f"{self.folder}/gone", 0))

    def test_log_command_skips_bad_lines(self):
        aa.LOG.write_bytes(b'not json\n42\n\xff\n{"t": "now", "action": "replied", "ask": "Red?"}\n')
        with mock.patch("sys.stdout", io.StringIO()) as out:
            aa.cmd_log(types.SimpleNamespace(n=20))
        self.assertIn("replied", out.getvalue())


class Labels(unittest.TestCase):
    def test_auto_replies_say_what_they_are(self):
        self.assertIn("not typed by them", aa.LABEL)
        self.assertIn(aa.LABEL, decide(said("Red or blue?"))["reply"])


class Install(unittest.TestCase):
    OLD = '"/x/approve-all/.venv/bin/python" "/x/approve-all/approve-all" hook'

    def setUp(self):
        for path in (aa.SETTINGS, aa.SETTINGS.with_name(aa.SETTINGS.name + ".approve-all.bak")):
            path.unlink(missing_ok=True)
        patcher = mock.patch.object(aa.shutil, "which", lambda name: "/fake/bin/uv")
        patcher.start()
        self.addCleanup(patcher.stop)

    def installed(self, event="Stop"):
        return [h["command"] for g in json.loads(aa.SETTINGS.read_text())["hooks"][event] for h in g["hooks"]]

    def test_round_trip_keeps_everything_else(self):
        original = {"model": "fable", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}
        aa.SETTINGS.write_text(json.dumps(original))
        aa.cmd_install(None)
        aa.cmd_install(None)
        installed = json.loads(aa.SETTINGS.read_text())
        self.assertEqual(len(installed["hooks"]["Stop"]), 2)
        self.assertEqual(installed["hooks"]["PreToolUse"][0]["matcher"], "AskUserQuestion")
        aa.cmd_uninstall(None)
        self.assertEqual(json.loads(aa.SETTINGS.read_text()), original)

    def test_hook_runs_through_uv_and_can_never_block(self):
        aa.cmd_install(None)
        command = self.installed()[0]
        self.assertTrue(command.startswith("/fake/bin/uv run --quiet --script "), command)
        self.assertTrue(command.endswith(f"{Path(loader.path).resolve()} hook || true"), command)

    def test_replaces_a_hook_from_before_uv(self):
        aa.SETTINGS.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": self.OLD}]}]}}))
        aa.cmd_install(None)
        self.assertEqual(len(self.installed()), 1)
        self.assertNotIn(".venv", self.installed()[0])
        aa.SETTINGS.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": self.OLD}]}]}}))
        aa.cmd_uninstall(None)
        self.assertEqual(json.loads(aa.SETTINGS.read_text()), {})

    def test_no_uv_is_a_clear_error(self):
        with mock.patch.object(aa.shutil, "which", lambda name: None):
            with self.assertRaisesRegex(SystemExit, "uv not found"):
                aa.cmd_install(None)
        self.assertFalse(aa.SETTINGS.exists())

    def test_refuses_to_touch_malformed_settings(self):
        backup = aa.SETTINGS.with_name(aa.SETTINGS.name + ".approve-all.bak")
        for text in ("", "{not json", "[]", '{"hooks": []}', '{"hooks": {"Stop": {}}}', '{"hooks": {"PreToolUse": 1}}'):
            aa.SETTINGS.write_text(text)
            for command in (aa.cmd_install, aa.cmd_uninstall):
                with self.assertRaisesRegex(SystemExit, "Nothing was changed"):
                    command(None)
                self.assertEqual(aa.SETTINGS.read_text(), text)
                self.assertFalse(backup.exists())

    def test_keeps_other_commands_that_share_a_group(self):
        other = {"type": "command", "command": "other"}
        ours = {"type": "command", "command": self.OLD}
        aa.SETTINGS.write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [ours, other], "matcher": "x"}, {"hooks": [ours]}],
            "PreToolUse": [{"hooks": [other]}]}}))
        aa.cmd_uninstall(None)
        self.assertEqual(json.loads(aa.SETTINGS.read_text()), {"hooks": {
            "Stop": [{"hooks": [other], "matcher": "x"}], "PreToolUse": [{"hooks": [other]}]}})
        aa.SETTINGS.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [ours, other]}]}}))
        aa.cmd_install(None)
        self.assertEqual(self.installed()[0], "other")
        self.assertEqual(len(self.installed()), 2)
        self.assertNotIn(".venv", self.installed()[1])

    def test_leaves_odd_hooks_it_does_not_own_alone(self):
        odd = [{"hooks": 5}, "text", {"hooks": ["text", {"command": 7}]}]
        aa.SETTINGS.write_text(json.dumps({"hooks": {"Stop": odd}}))
        aa.cmd_install(None)
        aa.cmd_uninstall(None)
        self.assertEqual(json.loads(aa.SETTINGS.read_text()), {"hooks": {"Stop": odd}})

    def test_status_survives_malformed_settings(self):
        aa.SETTINGS.write_text("{not json")
        with mock.patch("sys.stdout", io.StringIO()) as out:
            aa.cmd_status(None)
        self.assertIn("installed: unknown", out.getvalue())

    def test_keeps_a_symlink_and_its_mode(self):
        real = Path(tempfile.mkdtemp(dir=TMP), "real.json")
        real.write_text("{}")
        real.chmod(0o600)
        aa.SETTINGS.symlink_to(real)
        aa.cmd_install(None)
        self.assertTrue(aa.SETTINGS.is_symlink())
        self.assertEqual(real.stat().st_mode & 0o777, 0o600)
        self.assertIn("hooks", json.loads(real.read_text()))

    def test_a_negative_wait_cannot_write_a_negative_timeout(self):
        with mock.patch.object(aa, "ANSWER_WAIT", -999):
            aa.cmd_install(None)
        self.assertEqual(json.loads(aa.SETTINGS.read_text())["hooks"]["Stop"][0]["hooks"][0]["timeout"], 120)


@unittest.skipUnless(shutil.which("uv"), "needs uv")
class ThroughUv(unittest.TestCase):
    """Runs the exact command install writes, through a shell, as Claude Code does. The first run needs the network."""

    def setUp(self):
        self.module, self.copy = load_copy()
        self.home = tempfile.mkdtemp(dir=TMP)
        self.module.SETTINGS = Path(self.home, "settings.json")
        with mock.patch("sys.stdout", io.StringIO()):
            self.module.cmd_install(None)
        settings = json.loads(self.module.SETTINGS.read_text())
        self.command = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.transcript = f"{self.home}/t.jsonl"
        Path(self.transcript).write_text("\n".join(json.dumps(r) for r in [human("start"), said("Red or blue?")]))

    def run_command(self):
        event = {"hook_event_name": "Stop", "transcript_path": self.transcript, "session_id": "s", "cwd": "/x"}
        env = {**os.environ, "APPROVE_ALL_HOME": self.home, "APPROVE_ALL_ENTRYPOINTS": "*"}
        return subprocess.run(["sh", "-c", self.command], input=json.dumps(event), text=True, capture_output=True,
                              env=env, timeout=180, cwd=self.home)

    def test_stop_event_gets_a_labeled_reply(self):
        done = self.run_command()
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        out = json.loads(done.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn(aa.LABEL, out["reason"])
        self.assertTrue(out["reason"].endswith(aa.REPLY_DEFAULT))

    def test_a_moved_script_never_blocks(self):
        Path(self.copy.path).unlink()
        done = self.run_command()
        self.assertEqual((done.returncode, done.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
