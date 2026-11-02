"""Read-only accuracy GETs preserve mature archives; synthetic real-Dynamo fixture."""
from copy import deepcopy
import json
import pytest
from aws_publishing.api import ARCHIVE_RETENTION_SECONDS
from test_benchmark import lab
from test_accuracy import accuracy_lab, call, create_accuracy
from test_accuracy_dynamo_persistence import dynamo_lab

def mature(suite):
    cid=create_accuracy(suite)["id"]
    table=suite[1].client
    item={"id":"aged","storage_key":"synthetic/aged","archive_verified_at":0}
    table.items["artifacts:aged"]={"pk":{"S":"STATE"},"sk":{"S":"artifacts:aged"},
        "payload":{"S":json.dumps(item,separators=(",",":"))}}
    suite[3][0]=ARCHIVE_RETENTION_SECONDS+1
    assert suite[0].archive_due(item)
    return cid,table

@pytest.mark.parametrize("suffix",[
 "/capabilities","/campaigns","/campaigns/{cid}","/campaigns/{cid}/tasks","/campaigns/{cid}/accounting"])
def test_accuracy_get_leaves_mature_archives_and_dynamo_unchanged(dynamo_lab,suffix):
    suite=dynamo_lab;cid,table=mature(suite)
    before=deepcopy(table.items);commits=len(table.commits);tags=deepcopy(suite[2].tags)
    status,_=call(suite,"GET","/v1/projects/p/benchmark-accuracy"+suffix.format(cid=cid))
    assert status==200
    assert table.items==before and len(table.commits)==commits
    assert suite[2].tags==tags

def test_failed_retention_service_cannot_fail_an_accuracy_observation(dynamo_lab):
    suite=dynamo_lab;cid,table=mature(suite);before=deepcopy(table.items)
    def failed(_key):raise OSError("synthetic-maintenance-failure")
    suite[2].archived=failed
    status,_=call(suite,"GET",f"/v1/projects/p/benchmark-accuracy/campaigns/{cid}")
    assert status==200 and table.items==before
    with pytest.raises(OSError,match="synthetic-maintenance"):
        suite[0].maintenance()

def test_explicit_maintenance_still_tags_and_durably_acknowledges(dynamo_lab):
    suite=dynamo_lab;cid,table=mature(suite)
    assert suite[0].maintenance()["tagged"]==1
    assert "synthetic/aged" in suite[2].tags
    assert suite[1].state["artifacts"]["aged"]["archive_tagged"] is True

def test_existing_fixed_budget_get_retains_finalization_behavior(dynamo_lab):
    suite=dynamo_lab;cid,table=mature(suite)
    status,_=call(suite,"GET","/v1/projects/p/benchmarks")
    assert status==200 and "synthetic/aged" in suite[2].tags
    assert suite[1].state["artifacts"]["aged"]["archive_tagged"] is True
