from coding_agent.observability.tracing import TraceContext


def test_child_span_same_trace():
    root = TraceContext.root()
    assert root.child().trace_id == root.trace_id


def test_child_span_new_id():
    root = TraceContext.root()
    assert root.child().span_id != root.span_id


def test_parent_span_link():
    root = TraceContext.root()
    child = root.child()
    grandchild = child.child()
    assert child.parent_span_id == root.span_id
    assert grandchild.parent_span_id == child.span_id
