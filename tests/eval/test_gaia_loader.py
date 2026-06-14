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