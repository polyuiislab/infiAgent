import os
from pathlib import Path
import shutil
import subprocess
import pytest


def test_same_agent_same_named_tools_match_their_own_call_ids():
    import infiagent.runtime
    source = Path(infiagent.runtime.__file__).parent / 'static/app.js'
    node = shutil.which('node')
    if not node:
        import playwright
        candidate = Path(playwright.__file__).parent / 'driver' / ('node.exe' if os.name == 'nt' else 'node')
        node = str(candidate) if candidate.exists() else None
    if not node:
        pytest.skip('JavaScript runtime unavailable')
    script = r'''
const fs=require('fs'), vm=require('vm'), assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('function findRunningToolCard(');
const end=source.indexOf('\nfunction ', start+1);
const scope={}; vm.runInNewContext(source.slice(start,end),scope);
const a={toolName:'file_read',status:'running',agentId:'agent',toolCallId:'call-a'};
const b={toolName:'file_read',status:'running',agentId:'agent',toolCallId:'call-b'};
assert.strictEqual(scope.findRunningToolCard({toolCards:[a,b]},'file_read','agent','call-a'),a);
assert.strictEqual(scope.findRunningToolCard({toolCards:[a,b]},'file_read','agent','call-b'),b);
'''
    result = subprocess.run([node, '-e', script, str(source)], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
