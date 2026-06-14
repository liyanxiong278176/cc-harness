from eval.datasets.gaia_loader import GaiaTask, HARD_GAP_SUFFIXES, SOFT_GAP_SUFFIXES

def test_gaia_task_fields():
    t = GaiaTask(
        task_id="abc-123", question="What is 2+2?", level=1,
        ground_truth="4", file_name=None,
    )
    assert t.task_id == "abc-123"
    assert t.level == 1
    assert t.file_name is None

def test_suffix_constants():
    assert ".png" in HARD_GAP_SUFFIXES
    assert ".mp3" in HARD_GAP_SUFFIXES
    assert ".mp4" in HARD_GAP_SUFFIXES
    assert ".pdf" in SOFT_GAP_SUFFIXES
    assert ".xlsx" in SOFT_GAP_SUFFIXES
    # Disjoint sets
    assert HARD_GAP_SUFFIXES.isdisjoint(SOFT_GAP_SUFFIXES)

def test_filter_tasks_separates_text_soft_hard():
    from eval.datasets.gaia_loader import filter_tasks
    tasks = [
        GaiaTask("t1", "q1", 1, "a1", None),          # text-only
        GaiaTask("t2", "q2", 1, "a2", "doc.pdf"),     # soft (pdf)
        GaiaTask("t3", "q3", 2, "a3", "data.xlsx"),   # soft (excel)
        GaiaTask("t4", "q4", 1, "a4", "img.png"),     # hard (image)
        GaiaTask("t5", "q5", 1, "a5", "tune.mp3"),    # hard (audio)
        GaiaTask("t6", "q6", 1, "a6", "weird.xyz"),   # unknown -> hard (safe)
    ]
    runnable, skipped = filter_tasks(tasks, include_attachments=True)
    assert {t.task_id for t in runnable} == {"t1", "t2", "t3"}
    assert {t.task_id for t in skipped} == {"t4", "t5", "t6"}

def test_filter_tasks_text_only_when_attachments_disabled():
    from eval.datasets.gaia_loader import filter_tasks
    tasks = [
        GaiaTask("t1", "q1", 1, "a1", None),
        GaiaTask("t2", "q2", 1, "a2", "doc.pdf"),
    ]
    runnable, skipped = filter_tasks(tasks, include_attachments=False)
    assert {t.task_id for t in runnable} == {"t1"}
    assert {t.task_id for t in skipped} == {"t2"}

def test_stratified_sample_balances_levels():
    from eval.datasets.gaia_loader import stratified_sample
    tasks = (
        [GaiaTask(f"L1-{i}", "q", 1, "a", None) for i in range(50)]
        + [GaiaTask(f"L2-{i}", "q", 2, "a", None) for i in range(50)]
        + [GaiaTask(f"L3-{i}", "q", 3, "a", None) for i in range(50)]
    )
    out = stratified_sample(tasks, limit=30, seed=42)
    assert len(out) == 30
    counts = {1: 0, 2: 0, 3: 0}
    for t in out:
        counts[t.level] += 1
    # Roughly balanced (10 each, +/- 1 due to rounding)
    assert all(9 <= c <= 11 for c in counts.values())

def test_stratified_sample_deterministic():
    from eval.datasets.gaia_loader import stratified_sample
    tasks = [GaiaTask(f"t-{i}", "q", 1, "a", None) for i in range(100)]
    a = stratified_sample(tasks, limit=10, seed=42)
    b = stratified_sample(tasks, limit=10, seed=42)
    assert [t.task_id for t in a] == [t.task_id for t in b]
    c = stratified_sample(tasks, limit=10, seed=43)
    assert [t.task_id for t in a] != [t.task_id for t in c]

def test_stratified_sample_limit_exceeds_pool():
    from eval.datasets.gaia_loader import stratified_sample
    tasks = [GaiaTask(f"t-{i}", "q", 1, "a", None) for i in range(5)]
    out = stratified_sample(tasks, limit=10, seed=42)
    assert len(out) == 5  # cap at pool size

def test_load_gaia_validation_constructs_tasks(monkeypatch):
    """load_gaia_validation maps HF rows -> GaiaTask correctly."""
    from eval.datasets import gaia_loader

    fake_rows = [
        {"task_id": "id1", "Question": "Q1", "Level": "1",
         "Final answer": "42", "file_name": ""},
        {"task_id": "id2", "Question": "Q2", "Level": "2",
         "Final answer": "yes", "file_name": "data.csv"},
    ]
    class FakeSplit(list):
        pass
    monkeypatch.setattr(
        gaia_loader, "_hf_load_dataset",
        lambda: {"validation": FakeSplit(fake_rows)},
    )

    tasks = gaia_loader.load_gaia_validation()
    assert len(tasks) == 2
    assert tasks[0].task_id == "id1"
    assert tasks[0].level == 1
    assert tasks[0].file_name is None       # empty string -> None
    assert tasks[1].file_name == "data.csv"
    assert tasks[1].level == 2
