from makeprov.prov import ActivityNode, FileEntity


def test_jsonld_entity_or_id_union_roundtrip():
    payload = {
        "id": "urn:activity",
        "type": "prov:Activity",
        "wasAssociatedWith": {"@id": "urn:agent"},
        "used": [
            {"@id": "urn:external-entity"},
            {
                "id": "urn:file-entity",
                "type": "prov:Entity",
                "wasGeneratedBy": {"@id": "urn:activity"},
            },
        ],
    }

    activity = ActivityNode.from_jsonld(payload)

    assert activity.wasAssociatedWith == {"@id": "urn:agent"}
    assert isinstance(activity.used, list)
    assert activity.used[0] == {"@id": "urn:external-entity"}
    assert isinstance(activity.used[1], FileEntity)
    assert activity.used[1].wasGeneratedBy == {"@id": "urn:activity"}

    encoded = activity.to_jsonld(with_context=False)

    assert encoded["wasAssociatedWith"] == {"@id": "urn:agent"}
    assert {"@id": "urn:external-entity"} in encoded["used"]
    assert any(entry.get("id") == "urn:file-entity" for entry in encoded["used"])
