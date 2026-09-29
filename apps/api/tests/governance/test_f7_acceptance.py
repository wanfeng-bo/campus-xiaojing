"""No service, browser or model calls: the bounded F7 evidence collector contract."""

import importlib
import json
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest


def module():
    return importlib.import_module("scripts.f7_engineering.acceptance")


def budget(m, **overrides):
    values = dict(
        stage_id="school-facts-f7",
        stage_cap_microyuan=3_000_000,
        total_cap_microyuan=50_000_000,
        stage_occupied_microyuan=100_000,
        total_occupied_microyuan=24_300_000,
        unknown_microyuan=5_668_864,
        in_flight_microyuan=0,
        checked_at=datetime.now(UTC),
        original_ledger_verified=True,
        occupied_includes_unknown_and_in_flight=True,
        model="deepseek-flash",
        reasoning_effort="high",
        thinking="enabled",
        automatic_retries=0,
    )
    return m.BudgetSnapshot(**(values | overrides))


def test_fixed_plan_pins_categories_identity_continuity_and_confirmation():
    m = module()
    assert len(m.CASES) == 13
    assert [c.case_id for c in m.CASES] == [f"Q6-{i:02d}" for i in range(1, 14)]
    assert m.CASES[0].category == "ordinary"
    assert m.CASES[1].conversation == m.CASES[0].conversation
    assert m.CASES[4].conversation == m.CASES[5].conversation == m.CASES[6].conversation
    assert m.CASES[9].action == "package_prepare"
    assert m.CASES[10].action == "package_confirm"
    assert m.CASES[10].category == "formal_control"
    assert m.CASES[12].identity == "teacher"
    with pytest.raises((AttributeError, TypeError)):
        m.CASES[0].category = "complex"


def test_budget_never_assumes_unknown_zero_or_creates_new_caps():
    m = module()
    m.require_budget(budget(m), required_reserve_microyuan=400_000)
    for changed in (
        {"original_ledger_verified": False},
        {"occupied_includes_unknown_and_in_flight": False},
        {"stage_cap_microyuan": 6_000_000},
        {"total_cap_microyuan": 51_000_000},
        {"stage_occupied_microyuan": 2_800_000},
        {"total_occupied_microyuan": 49_800_000},
        {"automatic_retries": 1},
        {"thinking": "disabled"},
        {"reasoning_effort": "low"},
        {"unknown_microyuan": -1},
    ):
        with pytest.raises(ValueError):
            m.require_budget(budget(m, **changed), required_reserve_microyuan=400_000)


def test_budget_accepts_exact_approved_four_yuan_ceiling_with_existing_occupancy():
    m = module()
    snapshot = budget(
        m, stage_cap_microyuan=4_000_000, stage_occupied_microyuan=1_200_000,
        in_flight_microyuan=200_000,
    )
    m.require_budget(snapshot, required_reserve_microyuan=2_621_440)
    assert snapshot.stage_occupied_microyuan == 1_200_000
    assert snapshot.unknown_microyuan == 5_668_864
    assert snapshot.in_flight_microyuan == 200_000
    assert snapshot.total_cap_microyuan == 50_000_000
    with pytest.raises(ValueError):
        m.require_budget(
            replace(snapshot, stage_cap_microyuan=3_000_000),
            required_reserve_microyuan=2_621_440,
        )


@pytest.mark.parametrize("changed", [
    {"stage_cap_microyuan": 3_000_001},
    {"stage_cap_microyuan": 3_100_000},
    {"stage_cap_microyuan": 4_000_001},
    {"stage_cap_microyuan": 5_000_000},
    {"stage_cap_microyuan": 6_000_000},
    {"stage_occupied_microyuan": 1_400_000},
    {"total_occupied_microyuan": 48_000_000},
    {"total_cap_microyuan": 50_000_001},
    {"unknown_microyuan": 24_300_001},
    {"in_flight_microyuan": 24_300_001},
    {"occupied_includes_unknown_and_in_flight": False},
    {"original_ledger_verified": False},
    {"stage_id": "school-facts-f6"},
])
def test_four_yuan_ceiling_keeps_original_budget_and_occupancy_guards(changed):
    m = module()
    snapshot = budget(m, stage_cap_microyuan=4_000_000, stage_occupied_microyuan=1_200_000)
    with pytest.raises(ValueError):
        m.require_budget(replace(snapshot, **changed), required_reserve_microyuan=2_621_440)


def test_run_requires_real_reader_lock_and_has_no_transcript_in_repr_or_public_record():
    m = module()
    secret = "synthetic-answer-and-prompt-do-not-persist"
    with pytest.raises(ValueError):
        m.CaseInput(secret, {"name": secret}, False, False)
    locked = m.CaseInput(secret, {"name": secret}, True, True)
    assert secret not in repr(locked)
    observed = m.BrowserObservation(
        case_id="Q6-01",
        outcome="correct_query",
        delivery="completed",
        elapsed_ms=6001,
        answer=secret,
        response={"text": secret},
        assertions=m.Review(True, True, True, True, "correct_query"),
        request_ids=("90dce9e2-c907-425c-9120-7c3ba9f14a57",),
        accepted_ms=4,
        visible_user_ms=2,
        first_usable_ms=6001,
    )
    assert secret not in repr(observed)
    public = m.public_observation(observed)
    assert secret not in json.dumps(public)
    assert public["category"] == "ordinary"
    assert public["observed_slow"] is False
    assert public["checks"]["readable"] is True
    with pytest.raises(ValueError):
        m.public_observation(
            replace(observed, assertions=m.Review(False, True, True, True, "correct_query"))
        )
    with pytest.raises(ValueError):
        m.public_observation(
            replace(observed, assertions=m.Review(secret, True, True, True, "correct_query"))
        )


def test_confirmation_cannot_be_marked_success_from_current_values_only():
    m = module()
    observed = m.BrowserObservation(
        case_id="Q6-11",
        outcome="correct_query",
        delivery="completed",
        elapsed_ms=100,
        assertions=m.Review(True, True, True, True, "correct_query"),
        answer="done",
        response={},
        request_ids=("90dce9e2-c907-425c-9120-7c3ba9f14a57",),
    )
    with pytest.raises(ValueError):
        m.public_observation(observed)
    public = m.public_observation(
        replace(
            observed,
            formal_confirmation_observed=True,
            original_receipt_verified=True,
            current_values_verified=True,
            continuation_observed=True,
        )
    )
    assert public["formal"]["original_receipt_verified"] is True


def test_response_parser_rejects_cross_run_or_error_and_preserves_safe_failure():
    m = module()
    accepted = {
        "event": "accepted",
        "snapshot": {
            "conversation_ref": "conversation-1",
            "generation": 1,
            "active_run": {"run_ref": "run-1", "turn_ref": "turn-1", "generation": 1},
            "turns": [],
        },
    }
    completed = {
        "event": "completed",
        "snapshot": {
            "conversation_ref": "conversation-1",
            "generation": 1,
            "active_run": None,
            "turns": [
                {
                    "run_ref": "run-1",
                    "turn_ref": "turn-1",
                    "generation": 1,
                    "visible_state": "completed",
                    "display_payload": {"text": "ok"},
                }
            ],
        },
    }
    parsed = m.parse_turn_stream("\n".join(map(json.dumps, (accepted, completed))))
    assert parsed["turn_ref"] == "turn-1"
    completed["snapshot"]["turns"][0]["run_ref"] = "other-run"
    with pytest.raises(ValueError):
        m.parse_turn_stream("\n".join(map(json.dumps, (accepted, completed))))
    with pytest.raises(m.TurnFailure) as failure:
        m.parse_turn_stream(
            "\n".join(
                map(
                    json.dumps,
                    (
                        accepted,
                        {"event": "error", "code": "assistant.budget", "prompt": "never-copy"},
                    ),
                )
            )
        )
    assert failure.value.code == "assistant.budget"
    assert "never-copy" not in str(failure.value)


def test_whitelist_timing_no_full_text_and_no_nested_time_addition():
    m = module()
    timing = m.timing_metadata(
        {
            "request_id": "90dce9e2-c907-425c-9120-7c3ba9f14a57",
            "seconds": 8.1,
            "counts": {"model": 2, "tool": 1, "prompt": "never"},
            "inclusive_seconds": {"model": 6.0, "tool": 2.0, "authorization": 1.5},
            "exclusive_seconds": {"model": 6.0, "tool": 0.5, "authorization": 1.5},
            "usage": [{"prompt_tokens": 7000, "reasoning_tokens": 100, "text": "never"}],
            "prompt": "never",
            "answer": "never",
            "thinking": "never",
        }
    )
    assert "never" not in json.dumps(timing)
    assert timing["inclusive_seconds"]["model"] == 6
    assert timing["seconds"] == 8.1
    assert "total_local" not in timing


def test_failed_and_slow_samples_not_filtered_from_summary():
    m = module()
    rows = [
        m.public_observation(
            m.BrowserObservation(
                case_id=case,
                outcome=outcome,
                delivery=delivery,
                elapsed_ms=duration,
                request_ids=("90dce9e2-c907-425c-9120-7c3ba9f14a57",),
                assertions=m.Review(outcome == "correct_query", True, True, True, outcome),
            )
        )
        for case, outcome, delivery, duration in (
            ("Q6-01", "correct_query", "completed", 3000),
            ("Q6-04", "correct_query", "completed", 14000),
            ("Q6-05", "timeout", "unknown", 180000),
        )
    ]
    result = m.summarize(rows)
    assert result["ordinary"]["successful_queries"] == 2
    assert result["ordinary"]["max_complete_ms"] == 14000
    assert result["ordinary"]["median_complete_ms"] == 8500
    assert result["ordinary"]["latency_pass"] is False
    assert result["failures"] == ["Q6-05"]
    assert result["slow"] == ["Q6-04", "Q6-05"]


def test_tracker_does_not_repeat_unknown_or_allow_later_category_changes():
    m = module()
    run = m.RunTracker(source_sha256="a" * 64, image_sha256="b" * 64)
    run.arm("Q6-01")
    with pytest.raises(ValueError):
        run.arm("Q6-01")
    run.record(
        m.BrowserObservation(
            case_id="Q6-01", outcome="timeout", delivery="unknown", elapsed_ms=180000
        )
    )
    with pytest.raises(ValueError):
        run.arm("Q6-02")
    run.reconcile_original("Q6-01", "failed")
    run.arm("Q6-02")
    assert "prompt" not in json.dumps(run.metadata())


def test_one_fast_success_is_not_a_fixed_group_pass_and_uuid_is_required():
    m = module()
    observed = m.BrowserObservation(
        case_id="Q6-01",
        outcome="correct_query",
        delivery="completed",
        elapsed_ms=1,
        assertions=m.Review(True, True, True, True, "correct_query"),
    )
    with pytest.raises(ValueError):
        m.public_observation(observed)
    observed.request_ids = ("90dce9e2-c907-425c-9120-7c3ba9f14a57",)
    result = m.summarize([m.public_observation(observed)])["ordinary"]
    assert result["observed_successful_subset_latency_pass"] is True
    assert result["fixed_cases_complete"] is False
    assert result["latency_pass"] is False


def test_context_binding_and_request_timing_cannot_cross_cases():
    m = module()
    run = m.RunTracker(source_sha256="a" * 64, image_sha256="b" * 64)
    run.bind_context("Q6-01", "staff", "conversation-1")
    run.bind_context("Q6-02", "staff", "conversation-1")
    with pytest.raises(ValueError):
        run.bind_context("Q6-02", "staff", "conversation-2")
    with pytest.raises(ValueError):
        run.bind_context("Q6-08", "staff", "conversation-1")
    with pytest.raises(ValueError):
        run.attach_timing({"request_id": "90dce9e2-c907-425c-9120-7c3ba9f14a57"})


def test_one_browser_click_collects_same_http_result_without_second_model_request():
    import asyncio

    m = module()
    prompt = "verified-synthetic-prompt-only-in-memory"
    answer = "verified-answer-only-in-memory"
    turns = {"turn_ref": "turn-1", "run_ref": "run-1", "generation": 1}
    body = "\n".join(
        map(
            json.dumps,
            (
                {
                    "event": "accepted",
                    "snapshot": {
                        "conversation_ref": "conv-1",
                        "active_run": turns,
                        "generation": 1,
                        "turns": [],
                    },
                },
                {
                    "event": "completed",
                    "snapshot": {
                        "conversation_ref": "conv-1",
                        "active_run": None,
                        "generation": 1,
                        "turns": [
                            turns
                            | {"visible_state": "completed", "display_payload": {"text": answer}}
                        ],
                    },
                },
            ),
        )
    )

    class Response:
        url = "http://127.0.0.1:5174/api/v1/assistant/conversations/conv-1/turns"
        status, ok = 200, True
        request = type("Request", (), {"method": "POST"})()
        headers = {"x-request-id": "90dce9e2-c907-425c-9120-7c3ba9f14a57"}

        async def finished(self):
            return None

        async def text(self):
            return body

    class Page:
        url = "http://127.0.0.1:5174/school-facts.html"
        clicks = 0
        listener = None

        def locator(self, selector):
            return Locator(self, selector)

        def on(self, event, callback):
            self.listener = callback

        def remove_listener(self, event, callback):
            self.listener = None

    class Locator:
        def __init__(self, page, selector):
            self.page, self.selector = page, selector

        def locator(self, selector):
            return Locator(self.page, selector)

        async def fill(self, value):
            assert value == prompt

        async def click(self, **kwargs):
            self.page.clicks += 1
            self.page.listener(Response())

        async def count(self):
            return 1

        async def is_visible(self):
            return True

        async def all_text_contents(self):
            return ["已受理，正在等待回答"]

        @property
        def last(self):
            return self

        async def inner_text(self):
            return answer if self.selector == ".answer-text" else prompt

    async def budget_reader():
        return budget(m)

    async def review(locked, observed):
        assert locked.reader_expected == {"answer": answer}
        assert observed.answer == answer
        return m.Review(True, True, True, True, "correct_query")

    page, saved = Page(), []
    tracker = m.RunTracker(source_sha256="a" * 64, image_sha256="b" * 64)
    observed = asyncio.run(
        m.run_browser_case(
            page=page,
            tracker=tracker,
            case_id="Q6-01",
            locked=m.CaseInput(prompt, {"answer": answer}, True, True),
            conversation_ref="conv-1",
            identity="staff",
            budget_reader=budget_reader,
            required_reserve_microyuan=400_000,
            reviewer=review,
            persist_metadata=saved.append,
        )
    )
    assert observed.outcome == "correct_query"
    assert page.clicks == 1
    assert saved[0]["armed_not_resendable"] == "Q6-01"
    assert saved[-1]["observations"][0]["request_ids"] == [Response.headers["x-request-id"]]
    assert prompt not in json.dumps(saved) and answer not in json.dumps(saved)
    tracker.attach_timing({"request_id": Response.headers["x-request-id"], "seconds": 2})
    with pytest.raises(ValueError):
        tracker.attach_timing({"request_id": Response.headers["x-request-id"], "seconds": 2})


@pytest.mark.parametrize(
    ("mode", "delivery", "outcome", "eof", "parsed", "dom"),
    [
        ("complete", "completed", "unknown", True, True, True),
        ("cdp_pending", "completed", "unknown", True, True, True),
        ("cdp_failure", "completed", "unknown", True, True, True),
        ("request_object", "completed", "unknown", True, True, True),
        ("stream_pending_old", "unknown", "timeout", False, False, False),
        ("stream_pending_new", "unknown", "timeout", False, False, True),
        ("wrong_path", "unknown", "timeout", False, False, True),
        ("wrong_conversation", "accepted_failed", "failure", True, False, True),
        ("wrong_run", "accepted_failed", "failure", True, False, True),
        ("old_turn", "accepted_failed", "failure", True, False, True),
        ("failed_stream", "accepted_failed", "failure", False, False, True),
        ("http_failure", "accepted_failed", "failure", False, False, True),
        ("oversized", "accepted_failed", "failure", False, False, True),
    ],
)
def test_node_collector_observes_same_fetch_eof_without_cdp_completion(
    mode, delivery, outcome, eof, parsed, dom,
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; no runtime or dependency installed by the test")
    path = Path(__file__).resolve().parents[4] / "scripts/f7_engineering/acceptance_browser.cjs"
    source = r"""
const assert=require('node:assert/strict');
const vm=require('node:vm');
const {observeTurn, completedTurn}=require(process.argv[1]);
const mode=process.argv[2];
const id='90dce9e2-c907-425c-9120-7c3ba9f14a57';
const run={turn_ref:mode==='old_turn'?'turn-old':'turn-1',run_ref:'run-1',generation:1};
const conversation=mode==='wrong_conversation'?'conv-other':'conv-1';
const body=[{event:'accepted',snapshot:{conversation_ref:conversation,active_run:run}},
 {event:'completed',snapshot:{conversation_ref:conversation,active_run:null,turns:[{...run,
 run_ref:mode==='wrong_run'?'run-other':run.run_ref,
 visible_state:'completed',display_payload:{text:'memory-only-answer'}}]}}].map(JSON.stringify).join('\n');
const requestConversation=mode==='wrong_path'?'conv-other':'conv-1';
const requestPath='/api/v1/assistant/conversations/'+requestConversation+'/turns';
const requestUrl='http://127.0.0.1:5174'+requestPath;
let clicked=false, listener, finish, controller, returnedOriginal=false, fetchCalls=0, armed=0;
const pending=new Promise(resolve=>{finish=resolve});
const cdpResponse={url:()=>requestUrl,
 request:()=>({method:()=> 'POST'}),finished:()=>mode==='complete'?Promise.resolve():
  mode==='cdp_failure'?Promise.reject(new Error('private-CDP-error')):pending,
 allHeaders:async()=>({'x-request-id':id}),ok:()=>true,
 text:async()=>body};
const pendingStream=mode.startsWith('stream_pending');
const stream=new ReadableStream({start(value){
 controller=value;
 if(mode==='failed_stream'){value.error(new Error('private-stream-error'));return;}
 value.enqueue(new TextEncoder().encode(mode==='oversized'?'x'.repeat(2000001):body));
 if(!pendingStream)value.close();
}});
const originalResponse=new Response(stream,{status:mode==='http_failure'?503:200,
 headers:{'content-type':'application/x-ndjson','x-request-id':id}});
async function originalFetch(){fetchCalls++;listener(cdpResponse);return originalResponse;}
const browser=vm.createContext({URL,Request,Response,TextDecoder,
 location:{origin:'http://127.0.0.1:5174',href:'http://127.0.0.1:5174/school-facts.html'},
 fetch:originalFetch});
function locator(selector,turnRef){
 const match=selector.match(/^\[data-turn-ref="([^"]+)"\]$/);
 if(match)turnRef=match[1];
 const answerVisible=selector!=='.answer-text'||turnRef==='turn-old'||mode!=='stream_pending_old';
 return {locator:child=>locator(child,turnRef),last(){return this},fill:async()=>{},
  count:async()=>answerVisible?1:0,isVisible:async()=>answerVisible,
  innerText:async()=>selector==='.answer-text'?'memory-only-answer':'memory-only-prompt',
  evaluateAll:async()=>clicked?['turn-old','turn-1']:['turn-old'],
  allTextContents:async()=>['已受理'],click:async()=>{
   clicked=true;
   const response=mode==='request_object'?
    await browser.fetch(new Request(requestUrl,{method:'POST'})):
    await browser.fetch(requestPath,{method:'POST'});
   returnedOriginal=response===originalResponse;
   void response.text().catch(()=>{});
  }};
}
const page={url:()=> 'http://127.0.0.1:5174/school-facts.html',locator,
 evaluate:async(fn,arg)=>{
  browser.arg=arg;
  return structuredClone(await vm.runInContext('('+fn.toString()+')(arg)',browser));
 },
 on:(event,callback)=>{listener=callback},removeListener:()=>{listener=undefined}};
observeTurn(page,{caseId:'Q6-01',conversationRef:'conv-1',prompt:'memory-only-prompt',
 beforeDispatch:async()=>{armed++},deadlineMs:100}).then(async result=>{
 assert.equal(fetchCalls,1);
 assert.equal(armed,1);
 assert.equal(returnedOriginal,true);
 assert.equal(browser.fetch,originalFetch);
 assert.deepEqual(Object.keys(browser).filter(key=>key.startsWith('__f7_stream_')),[]);
 assert.ok(Object.values(result.diagnostics).every(value=>
  typeof value==='boolean'||(typeof value==='number'&&Number.isFinite(value))));
 if(result.delivery!=='completed'){assert.equal(result.answer,'');assert.equal(result.response,null);}
 const before=JSON.stringify(result);
 if(pendingStream)controller.close();
 finish();await new Promise(resolve=>setImmediate(resolve));
 assert.equal(JSON.stringify(result),before);
 process.stdout.write(JSON.stringify({delivery:result.delivery,outcome:result.outcome,
  request_id_retained:result.request_ids.includes(id),diagnostics:result.diagnostics}));
}).catch(error=>{process.stderr.write(error.message);process.exitCode=1});
"""
    completed = subprocess.run(
        [node, "-e", source, str(path), mode],
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["delivery"] == delivery
    assert result["outcome"] == outcome
    assert result["request_id_retained"] is (mode != "wrong_path")
    assert result["diagnostics"]["matched_response"] is (mode != "wrong_path")
    assert result["diagnostics"]["finished_resolved"] is (mode == "complete")
    assert result["diagnostics"]["browser_stream_eof"] is eof
    assert result["diagnostics"]["body_parsed"] is parsed
    assert result["diagnostics"]["dom_answer_visible"] is dom
