from makeprov.prov import ActivityNode, FileEntity

EX = "https://example.org/"


def test_jsonld_entity_or_id_union_roundtrip():
    payload = {
        "id": f"{EX}activity",
        "type": "prov:Activity",
        "wasAssociatedWith": {"@id": f"{EX}agent"},
        "used": [
            {"@id": f"{EX}external-entity"},
            {
                "id": f"{EX}file-entity",
                "type": "prov:Entity",
                "wasGeneratedBy": {"@id": f"{EX}activity"},
            },
        ],
    }

    activity = ActivityNode.from_jsonld(payload)

    assert activity.wasAssociatedWith == {"@id": f"{EX}agent"}
    assert isinstance(activity.used, list)
    assert activity.used[0] == {"@id": f"{EX}external-entity"}
    assert isinstance(activity.used[1], FileEntity)
    assert activity.used[1].wasGeneratedBy == {"@id": f"{EX}activity"}

    encoded = activity.to_jsonld(with_context=False)

    assert encoded["wasAssociatedWith"] == {"@id": f"{EX}agent"}
    assert {"@id": f"{EX}external-entity"} in encoded["used"]
    assert any(entry.get("id") == f"{EX}file-entity" for entry in encoded["used"])
