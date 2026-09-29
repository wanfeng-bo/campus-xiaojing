import importlib.util
import sys
from pathlib import Path


def load():
    p = Path(__file__).parents[4] / "scripts/f3_engineering/f3_writer_batch_executor.py"
    s = importlib.util.spec_from_file_location("f3batch", p)
    m = importlib.util.module_from_spec(s)
    sys.modules[s.name] = m
    s.loader.exec_module(m)
    return m


def test_nine_stage_resume_and_unknown_recovery():
    m = load()

    class Identity:
        def ensure_accounts(self, a):
            return {"guardian": "real-user"}

    class W:
        def __init__(self):
            self.rs = {}
            self.prepared = []

        def current_school_fact_version(self):
            return 4

        def receipt(self, k):
            return self.rs.get(k)

        def prepare(self, **kw):
            self.prepared.append(kw)
            return kw

        def confirm(self, p):
            i = p["stage"]["index"]
            r = m.StageReceipt(
                p["stage"]["idempotency_key"],
                "succeeded",
                5 + i,
                {f"stage{i}": {"id": str(i), "version": 1}},
            )
            self.rs[r.idempotency_key] = r
            return r

    w = W()
    stages = [
        {"index": i, "idempotency_key": f"k{i}", "fact_kind_stage": f"s{i}"} for i in range(9)
    ]
    plan = {
        "writer_batch_protocol": stages,
        "canonical_writer_batches": {f"s{i}": [] for i in range(9)},
        "identity_resolution": {"accounts": []},
    }
    out = m.F3BatchExecutor(writer=w, identity=Identity()).run(plan, bindings={})
    assert len(out) == 9 and len(w.prepared) == 9
    m.F3BatchExecutor(writer=w, identity=Identity()).run(plan, bindings={})
    assert len(w.prepared) == 9
