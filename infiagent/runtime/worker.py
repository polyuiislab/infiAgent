"""One SDK run per process; stdout is reserved for structured events."""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
from pathlib import Path


def main():
    wire = sys.stdout
    if hasattr(wire, 'reconfigure'):
        wire.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
    sys.stdout = sys.stderr
    lock = threading.Lock()

    def emit(value):
        with lock:
            wire.write(json.dumps(value, ensure_ascii=False, default=str) + '\n')
            wire.flush()

    from utils.execution_scope import ExecutionScope, AgentCancelled, AgentStopped, bind
    from infiagent import infiagent
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    scope = ExecutionScope()

    def controls():
        for line in sys.stdin:
            try:
                if json.loads(line).get('action') == 'stop':
                    scope.stop.set()
            except ValueError:
                continue
        # The controller owns this worker. Losing its control pipe must not
        # leave an unattended agent making further model calls.
        scope.stop.set()
        def abandon():
            if os.name != 'nt' and os.getpgrp() == os.getpid():
                os.killpg(os.getpgrp(), signal.SIGKILL)
            os._exit(1)
        timer = threading.Timer(5, abandon)
        timer.daemon = True
        timer.start()

    threading.Thread(target=controls, daemon=True).start()
    try:
        agent = infiagent(user_data_root=request['user_root'], agent_library_dir=request['user_root'],
                         default_agent_system=request['agent_system'], default_agent_name=request['agent_name'],
                         seed_builtin_resources=False, direct_tools=True)
        message = request['message']
        if request.get('resume'):
            from core.hierarchy_manager import get_hierarchy_manager
            manager = get_hierarchy_manager(request['task_path'])
            original = request.get('resume_input') or manager.get_runtime_metadata().get('user_input') or request.get('previous_message')
            if original:
                manager.append_instruction(message, source='user')
                message = original
        with bind(scope):
            options = {'stream_llm_tokens': True, 'auto_resume_attempts': 0, **request.get('sdk_overrides', {})}
            result = agent.run(message, task_id=request['task_path'],
                agent_system=request['agent_system'], agent_name=request['agent_name'],
                workspace_id=request.get('workspace_id'),
                on_event=lambda event: emit({'type': 'event', 'event': event}),
                **options)
        emit({'type': 'result', 'result': result})
    except BaseException as exc:
        if isinstance(exc, AgentCancelled):
            status = 'stopped' if isinstance(exc, AgentStopped) else 'cancelled'
            emit({'type': 'result', 'result': {'status': status, 'output': '', 'error_information': 'Run stopped before agent execution'}})
            return 0
        emit({'type': 'result', 'result': {'status': 'error', 'output': '',
              'error_code': getattr(exc, 'code', 'WORKER_ERROR'), 'error_information': str(exc)}})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
