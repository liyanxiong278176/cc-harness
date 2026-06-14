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