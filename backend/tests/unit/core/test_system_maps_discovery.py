from farna.core.system_maps_discovery import DiscoverySource, discover_draft


def test_discovery_is_deterministic_and_marks_every_edge_suggested() -> None:
    sources = [
        DiscoverySource(kind="pipeline", identifier="p2", name="improve-tests"),
        DiscoverySource(kind="pipeline", identifier="p1", name="build", folder_id="f1"),
        DiscoverySource(kind="folder", identifier="f1", name="Delivery"),
        DiscoverySource(kind="trigger", identifier="t1", name="push", pipeline_id="p1", trigger_type="webhook"),
    ]

    first = discover_draft(sources)
    second = discover_draft(reversed(sources))

    assert first == second
    assert first.edge_count == 2
    assert all(edge["config_json"]["suggested"] is True for edge in first.relationships)
    assert {edge["relationship_type"] for edge in first.relationships} == {"trigger", "flow"}
    assert first.skipped == ({"source": "p2", "reason": "improvement loop has no supported non-self relationship"},)


def test_unsupported_and_ambiguous_sources_are_reported() -> None:
    draft = discover_draft(
        [
            DiscoverySource(kind="unknown", identifier="x", name="Unknown"),
            DiscoverySource(kind="trigger", identifier="t", name="orphan", pipeline_id="missing"),
        ]
    )

    assert draft.node_count == 1
    assert draft.skipped
    assert draft.warnings


def test_connector_sources_become_connector_nodes() -> None:
    draft = discover_draft(
        [
            DiscoverySource(
                kind="connector",
                identifier="c1",
                name="GitHub",
                connector_type_id="github",
            ),
        ]
    )

    assert draft.node_count == 1
    node = draft.nodes[0]
    assert node["node_type"] == "connector"
    assert node["connector_instance_id"] == "c1"
    assert node["config_json"]["connector_type_id"] == "github"


def test_trigger_without_pipeline_binding_is_skipped() -> None:
    draft = discover_draft([DiscoverySource(kind="trigger", identifier="t1", name="manual")])

    assert draft.edge_count == 0
    assert ({"source": "t1", "reason": "trigger has no pipeline binding"}) in draft.skipped


def test_pipeline_folder_missing_from_inventory_is_skipped() -> None:
    draft = discover_draft([DiscoverySource(kind="pipeline", identifier="p1", name="build", folder_id="missing")])

    assert draft.edge_count == 0
    assert ({"source": "p1", "reason": "pipeline folder is not in inventory"}) in draft.skipped


def test_explicit_system_map_tag_skips_improvement_loop_relationship() -> None:
    draft = discover_draft(
        [
            DiscoverySource(kind="pipeline", identifier="p1", name="build", explicit_system_map_tag=True),
        ]
    )

    assert draft.node_count == 1
    assert ({"source": "p1", "reason": "improvement loop has no supported non-self relationship"}) in draft.skipped


def test_empty_inventory_reports_warning_without_skipped_sources() -> None:
    draft = discover_draft([])

    assert draft.node_count == 0
    assert draft.edge_count == 0
    assert not draft.skipped
    assert draft.warnings == ("No supported organisation sources were available for discovery",)


def test_supported_sources_without_skips_do_not_warn() -> None:
    draft = discover_draft([DiscoverySource(kind="folder", identifier="f1", name="Delivery", folder_id="f1")])

    assert draft.node_count == 1
    assert not draft.skipped
    assert not draft.warnings


def test_rescan_tracks_added_and_removed_sources_by_node_identity() -> None:
    pipeline = DiscoverySource(kind="pipeline", identifier="p1", name="build")
    trigger = DiscoverySource(kind="trigger", identifier="t1", name="push", pipeline_id="p1")

    initial = discover_draft([pipeline, trigger])
    rescanned = discover_draft([pipeline, trigger, DiscoverySource(kind="pipeline", identifier="p2", name="deploy")])
    removed = discover_draft([pipeline])

    initial_keys = {node["node_key"] for node in initial.nodes}
    rescanned_keys = {node["node_key"] for node in rescanned.nodes}
    removed_keys = {node["node_key"] for node in removed.nodes}

    assert rescanned_keys - initial_keys == {"pipeline:p2"}
    assert not initial_keys - rescanned_keys
    assert initial_keys - removed_keys == {"trigger:t1"}


def test_rescan_rename_keeps_node_identity_and_updates_its_name() -> None:
    before = discover_draft([DiscoverySource(kind="pipeline", identifier="p1", name="build")])
    after = discover_draft([DiscoverySource(kind="pipeline", identifier="p1", name="build-v2")])

    assert [node["node_key"] for node in after.nodes] == [node["node_key"] for node in before.nodes]
    assert after.nodes[0]["display_name"] == "build-v2"


# ---------------------------------------------------------------------------
# FAR-2595: bounded discovery source cap
# ---------------------------------------------------------------------------

_CAP_WARNING = "Discovery is capped at {cap} sources; additional organisation sources beyond the cap were not scanned"


def _pipelines(count: int) -> list[DiscoverySource]:
    return [DiscoverySource(kind="pipeline", identifier=f"p{index:04d}", name=f"pipeline-{index:04d}") for index in range(count)]


def test_source_cap_scans_exactly_the_cap_and_warns() -> None:
    """Over the cap, the draft is bounded to exactly the cap and warns."""
    draft = discover_draft(_pipelines(5), max_sources=3)

    assert draft.node_count == 3
    assert _CAP_WARNING.format(cap=3) in draft.warnings


def test_source_cap_at_the_boundary_does_not_warn() -> None:
    """Exactly at the cap is not a truncation, so no warning is emitted."""
    draft = discover_draft(_pipelines(3), max_sources=3)

    assert draft.node_count == 3
    assert _CAP_WARNING.format(cap=3) not in draft.warnings


def test_source_cap_below_the_cap_does_not_warn() -> None:
    draft = discover_draft(_pipelines(2), max_sources=3)

    assert draft.node_count == 2
    assert not any("capped at" in warning for warning in draft.warnings)


def test_source_cap_selection_is_order_independent() -> None:
    """The same capped subset is chosen regardless of input order."""
    sources = _pipelines(5)
    shuffled = [sources[3], sources[0], sources[4], sources[2], sources[1]]

    forward = discover_draft(sources, max_sources=3)
    reversed_draft = discover_draft(list(reversed(sources)), max_sources=3)
    shuffled_draft = discover_draft(shuffled, max_sources=3)

    forward_keys = [node["node_key"] for node in forward.nodes]
    assert forward_keys == [node["node_key"] for node in reversed_draft.nodes]
    assert forward_keys == [node["node_key"] for node in shuffled_draft.nodes]
    # Deterministic order: the first three identifiers, not input order.
    assert forward_keys == ["pipeline:p0000", "pipeline:p0001", "pipeline:p0002"]


def test_truncation_warning_does_not_invent_an_unmeasured_total() -> None:
    """The warning must not claim an "N of M" total the query never measured."""
    from farna.core.system_maps_discovery import discovery_truncation_warning

    warning = discovery_truncation_warning(1000)

    assert warning == _CAP_WARNING.format(cap=1000)
    assert " of " not in warning
