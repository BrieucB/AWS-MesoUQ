"""Accuracy UI publication: real conditional publisher, offline AWS fixture only."""
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("accuracy_publication_fixtures",ROOT/"tests/test_website_publication.py")
fixtures=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
publication=fixtures.publication
BASE_KEYS=(*publication.REMEMBERED_BASE_KEYS,*publication.REMEMBERED_NEW_KEYS)
NEW_KEYS=("src/benchmark-accuracy.js","src/benchmark-workspace.js")
ORDER=(*NEW_KEYS,"src/benchmark-lab.css","src/app.js")
WORKFLOW=(ROOT/".github/workflows/publish-accuracy-website.yml").read_text()

def materialize(directory,assets):
    for key,raw in assets.items():
        path=directory/key
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(raw)

class AccuracyPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.baseline_dir,self.dist=root/"baseline",root/"dist"
        self.baseline={key:("baseline "+key).encode() for key in BASE_KEYS}
        self.target={**self.baseline,**{key:("accuracy target "+key).encode() for key in ORDER}}
        materialize(self.baseline_dir,self.baseline)
        materialize(self.dist,self.target)
        self.aws=fixtures.FakeAws(self.baseline)
    def publish(self):
        return publication.publish(self.dist,self.baseline_dir,"synthetic-bucket",fixtures.ROLE,aws=self.aws,profile="accuracy")
    def puts(self):
        return [row for row in self.aws.calls if row[:2]==("s3api","put-object")]
    def writes(self):
        return [row[row.index("--key")+1] for row in self.puts()]
    def test_dependencies_precede_entry_and_all16_assets_are_verified(self):
        config=self.aws.objects[publication.CONFIG][0]
        result=self.publish()
        self.assertEqual(result["conditional_writes"],4)
        self.assertEqual(result["assets_verified"],16)
        self.assertEqual(self.writes(),list(ORDER))
        self.assertTrue(all("--if-none-match" in row for row in self.puts()[:2]))
        self.assertTrue(all("--if-match" in row for row in self.puts()[2:]))
        self.assertEqual(self.aws.objects[publication.CONFIG][0],config)
        for key in BASE_KEYS:
            if key not in ORDER and key!=publication.CONFIG:
                self.assertEqual(self.aws.objects[key][0],self.baseline[key])
    def test_partial_and_complete_release_resume_without_rewriting_verified_bytes(self):
        self.aws.store(NEW_KEYS[0],self.target[NEW_KEYS[0]])
        self.aws.store("src/benchmark-lab.css",self.target["src/benchmark-lab.css"])
        first=self.publish()
        self.assertEqual(first["conditional_writes"],2)
        self.assertEqual(self.writes(),[NEW_KEYS[1],"src/app.js"])
        self.aws.calls.clear()
        second=self.publish()
        self.assertEqual(second["conditional_writes"],0)
        self.assertEqual(first["manifest_sha256"],second["manifest_sha256"])
    def test_extra_or_unreviewed_existing_asset_fails_before_any_aws_call(self):
        for key in ("src/unreviewed.js","src/client.js","src/site-session.js"):
            with self.subTest(key=key):
                p=self.dist/key
                old=p.read_bytes() if p.exists() else None
                p.write_bytes(b"unexpected")
                self.aws.calls.clear()
                with self.assertRaises(publication.PublicationError):
                    self.publish()
                self.assertEqual(self.aws.calls,[])
                if old is None:p.unlink()
                else:p.write_bytes(old)
    def test_foreign_new_module_is_rejected_before_any_write(self):
        self.aws.store(NEW_KEYS[0],b"foreign live bytes")
        with self.assertRaises(publication.PublicationError):
            self.publish()
        self.assertEqual(self.writes(),[])
    def test_missing_existing_dependency_is_not_silently_created(self):
        del self.aws.objects["src/benchmark-figures.js"]
        with self.assertRaises(publication.PublicationError):
            self.publish()
        self.assertEqual(self.writes(),[])
    def test_summary_withholds_private_destination_values(self):
        summary=json.dumps(self.publish())
        self.assertNotIn("synthetic-bucket",summary)
        self.assertNotIn("private-live",summary)
    def test_workflow_session_reads16_and_can_write_only_the_reviewed_delta(self):
        read=WORKFLOW.split('"Action": "s3:GetObject"',1)[1].split("]",1)[0]
        write=WORKFLOW.split('"Action": "s3:PutObject"',1)[1].split("]",1)[0]
        read_keys=set(re.findall(r"WEBSITE_BUCKET }}/([^\"\n]+)",read))
        write_keys=set(re.findall(r"WEBSITE_BUCKET }}/([^\"\n]+)",write))
        self.assertEqual(read_keys,set(BASE_KEYS)|set(NEW_KEYS))
        self.assertEqual(write_keys,set(ORDER))
        self.assertIn("--profile accuracy",WORKFLOW)
        self.assertIn("cancel-in-progress: false",WORKFLOW)
        self.assertNotIn("s3:DeleteObject",WORKFLOW)

    def test_session_can_distinguish_absent_new_modules_without_broader_object_writes(self):
        # S3 HeadObject returns403 for absent keys unless ListBucket is permitted.
        raw=WORKFLOW.split("PUBLICATION_SESSION_POLICY: >-",1)[1].split("\n        run: |",1)[0]
        policy=json.loads("\n".join(line.strip() for line in raw.splitlines()))
        allowed=[row for row in policy["Statement"] if row.get("Effect")=="Allow"]
        listing=[row for row in allowed if row["Action"]=="s3:ListBucket"]
        self.assertEqual(len(listing),1)
        self.assertEqual(listing[0]["Resource"],"arn:aws:s3:::${{ env.WEBSITE_BUCKET }}")
        self.assertNotIn("Condition",listing[0])
        self.assertEqual({row["Action"] for row in allowed},{"s3:ListBucket","s3:GetObject","s3:PutObject"})
        write=next(row for row in allowed if row["Action"]=="s3:PutObject")
        self.assertEqual(set(write["Resource"]),{"arn:aws:s3:::${{ env.WEBSITE_BUCKET }}/"+key for key in ORDER})
