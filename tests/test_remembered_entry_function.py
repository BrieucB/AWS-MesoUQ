"""Exact entry routing only; CloudFront retains all signature validation."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

SOURCE=Path(__file__).resolve().parents[1]/"infra/aws_publishing/functions/remembered-entry.js"
NAMES=("CloudFront-Policy","CloudFront-Signature","CloudFront-Key-Pair-Id")


@unittest.skipUnless(shutil.which("node"),"CloudFront JavaScript contract requires Node in allocated/CI QA")
class RememberedEntryTests(unittest.TestCase):
    def run_requests(self,requests):
        script=(
            "const fs=require('fs'),vm=require('vm');"
            "const input=JSON.parse(fs.readFileSync(0,'utf8'));"
            "const sandbox={};vm.runInNewContext(input.source,sandbox);"
            "const output=input.requests.map(request=>{"
            "const result=sandbox.handler({request});return {unchanged:result===request,result};});"
            "process.stdout.write(JSON.stringify(output));")
        response=subprocess.run([shutil.which("node"),"-e",script],
            input=json.dumps({"source":SOURCE.read_text(),"requests":requests}),
            capture_output=True,text=True,timeout=10,check=True)
        return json.loads(response.stdout)

    def test_missing_or_empty_grant_redirects_only_root_and_index_get_or_head(self):
        requests=[]
        for uri in ("/","/index.html"):
            for method in ("GET","HEAD"):
                for omitted in NAMES:
                    for empty in (False,True):
                        cookies={name:{"value":"synthetic-present"} for name in NAMES}
                        if empty:cookies[omitted]["value"]=""
                        else:cookies.pop(omitted)
                        requests.append({"uri":uri,"method":method,"cookies":cookies})
        for response in self.run_requests(requests):
            self.assertFalse(response["unchanged"])
            self.assertEqual(response["result"],{"statusCode":302,"statusDescription":"Found",
                "headers":{"location":{"value":"/auth/login"},"cache-control":{"value":"no-store"}}})

    def test_populated_but_invalid_signatures_are_left_for_native_cloudfront(self):
        cookies={name:{"value":"not-a-valid-signed-grant"} for name in NAMES}
        for result in self.run_requests([
            {"uri":uri,"method":method,"cookies":cookies}
            for uri in ("/","/index.html") for method in ("GET","HEAD")]):
            self.assertTrue(result["unchanged"])

    def test_assets_auth_paths_other_methods_and_similar_paths_are_never_redirected(self):
        requests=[{"uri":uri,"method":"GET","cookies":{}}
            for uri in ("/app.js","/assets/private.png","/runtime-config.json",
                        "/auth/login","/index.html/","/%69ndex.html")]
        requests += [{"uri":uri,"method":method,"cookies":{}}
            for uri in ("/","/index.html") for method in ("POST","PUT","OPTIONS")]
        for result in self.run_requests(requests):
            self.assertTrue(result["unchanged"])
